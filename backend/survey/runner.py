#!/usr/bin/env python3
"""
SurveyRun 的唯一执行入口。

定时触发与手动触发都走 `execute_survey_run`，不各写一份 —— 这是 review/runner.py
用一次真实的状态机漂移换来的教训（见 AGENTS.md「目录职责」）。
"""

import logging
from typing import Dict, List, Optional, Tuple

from ..review.skills import load_available_review_skills
from ..storage import repo
from ..storage.models import (
    CODEGRAPH_ENABLED,
    DEGRADE_BUDGET_EXHAUSTED,
    DEGRADE_INDEX_FAILED,
    DEGRADE_PROFILE_FALLBACK,
    DEGRADE_REPO_FETCH_FAILED,
    DEGRADE_REPOS_TRUNCATED,
    ERROR_SURVEY,
    ERROR_UNEXPECTED,
    PROFILE_MANIFEST,
    PUSH_TRIGGER_AUTO,
    REPO_FETCH_FAILED,
    REPO_INDEX_FAILED,
    REPO_OK,
    RUN_FAILED,
    RUN_SUCCEEDED,
    SURVEY_PHASE_FETCHING,
    SURVEY_PHASE_INSPECTING,
    SURVEY_PHASE_INTEGRATING,
    SURVEY_PHASE_MATCHING_SKILL,
    SURVEY_PHASE_PROFILING,
    SURVEY_PHASE_SUMMARIZING,
)
from .analysis import (
    Budget, focus_key, inspect_focus, l1_repo_budget, load_skill_prompt, match_skills, merge_focuses, plan_focus,
    summarize,
)
from .clues import annotate_focuses, summarize_codegraph
from .common import SurveyError, finding_fingerprint
from .config import load_max_repos, load_survey_config, resolve_budget
from .crossrepo import build_cross_repo_map, render_cross_repo_map
from .ledger import plan_rechecks, update_ledger_for_run
from .profile import build_profile, codegraph_status, save_profile
from .reach import collect_reach
from .push import PushRejected, begin_pushes, execute_pushes
from .sources import resolve_sources
from .workspace import artifacts_dir, count_files, delete_workspace, prepare_repo

logger = logging.getLogger(__name__)


def _candidate_skills(survey: dict, skills_dir: str = "") -> List[str]:
    """
    Survey 的 skill 候选池 = 全部可用 skill 减去被取消勾选的。

    存的是排除集而不是勾选集，所以新增一个 skill 会自动进入所有巡检的候选池 ——
    反过来的话，新 skill 会被历史配置永久排除，而且没人会想起来去补勾。
    """
    available = sorted(load_available_review_skills(skills_dir=skills_dir).keys())
    excluded = {str(s).strip() for s in (survey.get("excluded_skills") or [])}
    return [name for name in available if name not in excluded]


def _prepare_workspaces(
    survey: dict,
    run_uid: str,
    targets: List[dict],
    budget_cfg: dict,
) -> List[dict]:
    """
    拉取全部仓库并生成画像。单个仓库失败记降级后继续。

    返回成功处理的仓库上下文列表 [{slug, dir, profile}]。
    """
    prepared: List[dict] = []
    artifacts = artifacts_dir(survey["slug"])
    # 整轮只查一次，并传给每个仓库的画像：状态要在快照、降级与仓库状态之间保持一致
    run_codegraph_status = codegraph_status()
    codegraph_ok = run_codegraph_status == CODEGRAPH_ENABLED
    if not codegraph_ok:
        # 画像退化成 manifest 级，L1 只知道有哪些文件、不知道有哪些接口与类型。
        # 巡检照常完成，但这是实打实的产出质量损失，必须让看报告的人知道。
        repo.add_survey_degradation(run_uid, DEGRADE_PROFILE_FALLBACK)
        logger.warning("codegraph unavailable, all profiles degraded to manifest level")

    for index, target in enumerate(targets, start=1):
        slug = target.get("slug") or target.get("url", "")
        if target.get("error"):
            # 组织展开就失败了，连仓库地址都没拿到
            repo.record_survey_repo(
                run_uid, slug, target.get("url", ""), status=REPO_FETCH_FAILED,
                error_message=target["error"],
            )
            repo.add_survey_degradation(run_uid, DEGRADE_REPO_FETCH_FAILED)
            continue

        try:
            repo_dir, branch, sha = prepare_repo(
                survey["slug"], target["url"], target.get("branch", ""),
                timeout=budget_cfg.get("fetch_timeout_seconds", 600),
            )
        except SurveyError as e:
            logger.warning("Repo fetch failed: %s: %s", target["url"], e)
            repo.record_survey_repo(
                run_uid, slug, target["url"], target.get("branch", ""),
                status=REPO_FETCH_FAILED, error_message=str(e),
            )
            repo.add_survey_degradation(run_uid, DEGRADE_REPO_FETCH_FAILED)
            continue

        repo.update_survey_progress(run_uid, phase=SURVEY_PHASE_PROFILING, repos_done=index)
        profile = build_profile(
            slug, repo_dir, index_timeout=budget_cfg["index_timeout_seconds"], status=run_codegraph_status
        )
        status = REPO_OK
        index_error = ""
        if codegraph_ok and profile["kind"] == PROFILE_MANIFEST:
            # codegraph 装着但这个仓库没建成索引，是单仓库级别的问题。
            # 状态单独记成 index_failed 而不是 ok：它不影响台账判定（那只看文件是否被取证过），
            # 但运行详情里要能看出这个仓库的画像退化了、L1 的点名可能选偏。
            repo.add_survey_degradation(run_uid, DEGRADE_INDEX_FAILED)
            status = REPO_INDEX_FAILED
            index_error = (profile.get("index") or {}).get("error", "")
        save_profile(artifacts, profile)

        has_structure = profile["kind"] != PROFILE_MANIFEST
        repo.record_survey_repo(
            run_uid, slug, target["url"], branch, sha, status,
            profile_kind=profile["kind"], file_count=count_files(repo_dir),
            error_message=index_error, index=profile.get("index"),
            # 只有结构图画像才有"抽出了几条"可言；退化画像留 NULL，与"抽出 0 条"区分开
            route_count=len(profile["routes"]) if has_structure else None,
            type_count=len(profile["types"]) if has_structure else None,
        )
        prepared.append({"slug": slug, "dir": repo_dir, "profile": profile})

    return prepared


def _collect_findings(
    focuses: List[dict],
    prepared: List[dict],
    skill_prompt: str,
    budget: Budget,
    run_uid: str,
) -> Tuple[List[dict], List[dict]]:
    """
    L2：逐个关注点取证，返回 (Finding 列表, 交给过 L2 的文件)。

    第二个列表每项 {"repo_slug", "file_path", "conclusive"}，conclusive 为 False 的文件
    名下的台账行保持原状态。撞到预算上限就停下并记降级，已产出的照常保留；
    没轮到的文件不进第二个列表，下一轮复核时排在前面。
    """
    dirs = {item["slug"]: item["dir"] for item in prepared}
    findings: List[dict] = []
    inspected: List[dict] = []

    for focus in focuses:
        if not budget.check():
            repo.add_survey_degradation(run_uid, DEGRADE_BUDGET_EXHAUSTED)
            logger.warning("Budget exhausted, stopping inspection with %s findings so far", len(findings))
            break
        repo_dir = dirs.get(focus["repo_slug"])
        if repo_dir is None:
            continue
        repo.survey_heartbeat(run_uid)
        record = {"repo_slug": focus["repo_slug"], "file_path": focus["file_path"], "conclusive": False}
        inspected.append(record)
        try:
            items, conclusive = inspect_focus(focus, repo_dir, skill_prompt, budget)
        except SurveyError as e:
            # 单个关注点取证失败不该让整轮失败：其余关注点还有价值。
            # 它记为没有结论，名下的台账行保持原状态，不会被错标成本轮未发现
            logger.warning("Focus inspection failed (%s/%s): %s", focus["repo_slug"], focus["file_path"], e)
            continue
        for item in items:
            # Finding 继承关注点的线索来源：L2 只读这一个文件，位置是关注点指过来的
            item["clue_source"] = focus.get("clue_source", "")
        findings.extend(items)
        record["conclusive"] = conclusive

    return findings, inspected


def _record_reach(run_uid: str, prepared: List[dict], cross_map: dict, budget: Budget) -> None:
    """
    统计每个仓库的 Reach 并写到本轮的仓库记录上。

    放在画像生成之后、L1 之前：此时画像与 L1 的预算分配都已确定，统计口径与 L1 实际拿到的输入一致。
    """
    try:
        per_repo = l1_repo_budget(len(prepared), len(render_cross_repo_map(cross_map)), budget.l1_max_chars)
    except Exception as e:
        # 统计只是排查辅助：失败的后果是这一轮所有仓库显示"无记录"，巡检本身照常进行
        logger.warning("Reach budget split failed, skipping reach: %s", e)
        return
    for item in prepared:
        # 超大仓库要遍历全部文件，不刷心跳的话这段时间可能被误判为疑似中断
        repo.survey_heartbeat(run_uid)
        try:
            reach = collect_reach(item["profile"], item["dir"], per_repo)
            repo.record_survey_repo_reach(run_uid, item["slug"], reach)
        except Exception as e:
            # 同上：失败的后果是界面上这个仓库显示"无记录"
            logger.warning("Reach measurement failed: repo=%s: %s", item["slug"], e)


def execute_survey_run(survey_uid: str, trigger: str) -> Optional[str]:
    """
    执行一次巡检，返回 run_uid；巡检不存在返回 None。

    失败语义：单个仓库失败记降级并继续，**全部仓库都失败**才判整轮失败。
    一个仓库改了默认分支就让整轮颗粒无收，会让这个功能连续几周产出为零。
    """
    survey = repo.get_survey(survey_uid)
    if survey is None:
        logger.warning("Survey not found: %s", survey_uid)
        return None

    run_uid = repo.start_survey_run(survey_uid, trigger)
    if run_uid is None:
        return None
    succeeded = False

    cfg = load_survey_config()
    budget_cfg = resolve_budget(survey)
    budget = Budget(
        wall_clock_minutes=budget_cfg["wall_clock_minutes"],
        l1_max_chars=budget_cfg["l1_max_chars"],
        l2_max_focus=budget_cfg["l2_max_focus"],
        l2_max_chars_per_focus=budget_cfg["l2_max_chars_per_focus"],
    )
    logger.info(
        "SurveyRun started: survey=%s run=%s trigger=%s budget=%s",
        survey["slug"], run_uid, trigger, budget_cfg,
    )

    try:
        repo.update_survey_progress(run_uid, phase=SURVEY_PHASE_FETCHING)
        targets, truncated = resolve_sources(survey["sources"], load_max_repos())
        if not targets:
            raise SurveyError("巡检没有任何可用的仓库来源")
        if truncated:
            repo.add_survey_degradation(run_uid, DEGRADE_REPOS_TRUNCATED, count=truncated)
        repo.update_survey_progress(run_uid, repos_total=len(targets))

        prepared = _prepare_workspaces(survey, run_uid, targets, budget_cfg)
        if not prepared:
            # 全部仓库都失败：这时候产出必然为零，判失败而不是"成功但零发现"
            raise SurveyError(f"全部 {len(targets)} 个仓库均拉取失败")

        profiles = [item["profile"] for item in prepared]
        cross_map = build_cross_repo_map(profiles)
        _record_reach(run_uid, prepared, cross_map, budget)
        _record_codegraph_stats(run_uid, profiles, cross_map)

        repo.update_survey_progress(run_uid, phase=SURVEY_PHASE_MATCHING_SKILL)
        candidates = _candidate_skills(survey, cfg.get("skills_dir", ""))
        matched = match_skills(profiles, candidates)
        repo.update_survey_progress(run_uid, matched_skills=matched)
        # 巡检不执行 skill scripts：那些脚本是为「一次 MR 的变更」设计的，
        # 输入契约里根本没有全量仓库这种形态，硬喂给它们只会拿到一堆报错输出。
        skill_prompt = load_skill_prompt(matched)

        repo.update_survey_progress(run_uid, phase=SURVEY_PHASE_INTEGRATING)
        repo.survey_heartbeat(run_uid)
        planned = plan_focus(profiles, cross_map, skill_prompt, budget)
        # 复核名额与 L1 的关注点上限相同、另算，不挤占 L1 的名额：
        # 共用一个上限的话，台账一大，新问题就再也进不了取证。
        # 不单独开配置项，是因为它和 l2_max_focus 控制的是同一种成本（L2 调用次数）
        prepared_slugs = {item["slug"] for item in prepared}
        # 本轮没拉到的仓库复核不了，先剔除再截名额，否则它们会白占名额
        rechecks, pending = plan_rechecks(
            [e for e in repo.list_survey_ledger_by_uid(survey_uid) if e["repo_slug"] in prepared_slugs],
            budget.l2_max_focus,
            ignored=repo.survey_ignored_fingerprints(survey_uid),
        )
        focuses = merge_focuses(rechecks, planned, pending)
        # 在合并之后标注：合并会把 L1 与复核对同一文件的点名并成一条，标注要落在实际取证的那一条上
        annotate_focuses(focuses, profiles, named_by_l1={focus_key(f) for f in planned})
        _record_codegraph_stats(run_uid, profiles, cross_map, focuses)

        repo.update_survey_progress(run_uid, phase=SURVEY_PHASE_INSPECTING)
        raw_findings, inspected = _collect_findings(focuses, prepared, skill_prompt, budget, run_uid)
        repo.record_survey_inspected_files(run_uid, inspected)

        for item in raw_findings:
            item["fingerprint"] = finding_fingerprint(
                item["repo_slug"], item["file_path"], item["category"]
            )
        counters = repo.record_survey_findings(run_uid, raw_findings)

        repo.update_survey_progress(run_uid, phase=SURVEY_PHASE_SUMMARIZING)
        summary = summarize(raw_findings, cross_map, budget)

        repo.finish_survey_run(run_uid, RUN_SUCCEEDED, summary=summary)
        succeeded = True
        logger.info(
            "SurveyRun finished: survey=%s run=%s repos=%s findings=%s",
            survey["slug"], run_uid, len(prepared), counters,
        )
    except SurveyError as e:
        logger.warning("SurveyRun failed: survey=%s run=%s: %s", survey["slug"], run_uid, e)
        repo.finish_survey_run(run_uid, RUN_FAILED, error_kind=ERROR_SURVEY, error_message=str(e))
    except Exception as e:
        logger.exception("SurveyRun crashed: survey=%s run=%s", survey["slug"], run_uid)
        repo.finish_survey_run(run_uid, RUN_FAILED, error_kind=ERROR_UNEXPECTED, error_message=str(e))
    finally:
        _cleanup(survey)

    if succeeded:
        _publish(run_uid)
    return run_uid


def _record_codegraph_stats(
    run_uid: str, profiles: List[dict], cross_map: dict, focuses: Optional[List[dict]] = None
) -> None:
    """
    落库 codegraph 快照。L1 前后各写一次：L1 调用失败时，画像与跨仓库事实仍然能在页面上看到。

    写入失败只记日志 —— 这份快照是观测数据，丢了它不影响任何一条 Finding。
    """
    try:
        repo.set_survey_codegraph_stats(
            run_uid, summarize_codegraph(profiles, cross_map, focuses)
        )
    except Exception:
        logger.warning("Recording codegraph stats failed: run=%s", run_uid, exc_info=True)


def _publish(run_uid: str) -> None:
    """
    成功的运行结束后：更新 Ledger，再推送到该 Survey 绑定的全部 Destination。

    放在运行收尾之后而不是之内：两者的失败都不该改写已经落库的运行状态 ——
    分析产出本身没有受损。台账更新失败时不推送，否则推出去的是一份旧台账，
    表里看起来"这一轮什么都没变"，比推送失败更难被发现。
    """
    try:
        update_ledger_for_run(run_uid)
    except Exception:
        logger.exception("Ledger update failed, skipping push: run=%s", run_uid)
        return

    try:
        started = begin_pushes(run_uid, PUSH_TRIGGER_AUTO)
    except PushRejected as e:
        # 没有配置输出目标是常态，不是问题
        logger.info("No auto push for run %s: %s", run_uid, e)
        return
    except Exception:
        logger.exception("Registering pushes failed: run=%s", run_uid)
        return
    if started["busy"]:
        logger.warning("Auto push skipped for busy bindings: run=%s bindings=%s", run_uid, started["busy"])
    execute_pushes(started["started"])


def _cleanup(survey: dict) -> None:
    """
    收尾：按配置清理工作区，并按次数清理历史运行记录。

    清理失败只记录不重抛 —— 分析结果已经落库，为了一次删目录失败把整轮标成
    失败会让人以为报告不可信。
    """
    try:
        repo.purge_survey_runs_by_uid(survey["survey_uid"])
    except Exception as e:
        logger.warning("Purging old survey runs failed: %s", e)

    if not survey.get("delete_workspace_after"):
        return
    try:
        delete_workspace(survey["slug"])
        logger.info("Workspace deleted per survey config: %s", survey["slug"])
    except Exception as e:
        logger.warning("Workspace cleanup failed: %s", e)
