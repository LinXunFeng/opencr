#!/usr/bin/env python3
"""
SurveyRun 的唯一执行入口。

定时触发与手动触发都走 `execute_survey_run`，不各写一份 —— 这是 review/runner.py
用一次真实的状态机漂移换来的教训（见 AGENTS.md「目录职责」）。
"""

import logging
from typing import Dict, List, Optional

from ..review.skills import load_available_review_skills
from ..storage import repo
from ..storage.models import (
    DEGRADE_BUDGET_EXHAUSTED,
    DEGRADE_INDEX_FAILED,
    DEGRADE_PROFILE_FALLBACK,
    DEGRADE_REPO_FETCH_FAILED,
    ERROR_SURVEY,
    ERROR_UNEXPECTED,
    PROFILE_MANIFEST,
    REPO_FETCH_FAILED,
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
from .analysis import Budget, inspect_focus, load_skill_prompt, match_skills, plan_focus, summarize
from .common import SurveyError, finding_fingerprint
from .config import load_survey_config, resolve_budget
from .crossrepo import build_cross_repo_map
from .profile import build_profile, codegraph_available, save_profile
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
    codegraph_ok = codegraph_available()
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
        profile = build_profile(slug, repo_dir, index_timeout=budget_cfg["index_timeout_seconds"])
        if codegraph_ok and profile["kind"] == PROFILE_MANIFEST:
            # codegraph 装着但这个仓库没建成索引，是单仓库级别的问题
            repo.add_survey_degradation(run_uid, DEGRADE_INDEX_FAILED)
        save_profile(artifacts, profile)

        repo.record_survey_repo(
            run_uid, slug, target["url"], branch, sha, REPO_OK,
            profile_kind=profile["kind"], file_count=count_files(repo_dir),
        )
        prepared.append({"slug": slug, "dir": repo_dir, "profile": profile})

    return prepared


def _collect_findings(
    focuses: List[dict],
    prepared: List[dict],
    skill_prompt: str,
    budget: Budget,
    run_uid: str,
) -> List[dict]:
    """L2：逐个关注点取证。撞到预算上限就停下并记降级，已产出的照常保留。"""
    dirs = {item["slug"]: item["dir"] for item in prepared}
    findings: List[dict] = []

    for focus in focuses:
        if not budget.check():
            repo.add_survey_degradation(run_uid, DEGRADE_BUDGET_EXHAUSTED)
            logger.warning("Budget exhausted, stopping inspection with %s findings so far", len(findings))
            break
        repo_dir = dirs.get(focus["repo_slug"])
        if repo_dir is None:
            continue
        repo.survey_heartbeat(run_uid)
        try:
            findings.extend(inspect_focus(focus, repo_dir, skill_prompt, budget))
        except SurveyError as e:
            # 单个关注点取证失败不该让整轮失败：其余关注点还有价值
            logger.warning("Focus inspection failed (%s/%s): %s", focus["repo_slug"], focus["file_path"], e)

    return findings


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
        targets = resolve_sources(survey["sources"])
        if not targets:
            raise SurveyError("巡检没有任何可用的仓库来源")
        repo.update_survey_progress(run_uid, repos_total=len(targets))

        prepared = _prepare_workspaces(survey, run_uid, targets, budget_cfg)
        if not prepared:
            # 全部仓库都失败：这时候产出必然为零，判失败而不是"成功但零发现"
            raise SurveyError(f"全部 {len(targets)} 个仓库均拉取失败")

        profiles = [item["profile"] for item in prepared]
        cross_map = build_cross_repo_map(profiles)

        repo.update_survey_progress(run_uid, phase=SURVEY_PHASE_MATCHING_SKILL)
        candidates = _candidate_skills(survey, cfg.get("skills_dir", ""))
        matched = match_skills(profiles, candidates)
        repo.update_survey_progress(run_uid, matched_skills=matched)
        # 巡检不执行 skill scripts：那些脚本是为「一次 MR 的变更」设计的，
        # 输入契约里根本没有全量仓库这种形态，硬喂给它们只会拿到一堆报错输出。
        skill_prompt = load_skill_prompt(matched)

        repo.update_survey_progress(run_uid, phase=SURVEY_PHASE_INTEGRATING)
        repo.survey_heartbeat(run_uid)
        focuses = plan_focus(profiles, cross_map, skill_prompt, budget)

        repo.update_survey_progress(run_uid, phase=SURVEY_PHASE_INSPECTING)
        raw_findings = _collect_findings(focuses, prepared, skill_prompt, budget, run_uid)

        for item in raw_findings:
            item["fingerprint"] = finding_fingerprint(
                item["repo_slug"], item["file_path"], item["category"]
            )
        counters = repo.record_survey_findings(run_uid, raw_findings)

        repo.update_survey_progress(run_uid, phase=SURVEY_PHASE_SUMMARIZING)
        summary = summarize(raw_findings, cross_map, budget)

        repo.finish_survey_run(run_uid, RUN_SUCCEEDED, summary=summary)
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

    return run_uid


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
