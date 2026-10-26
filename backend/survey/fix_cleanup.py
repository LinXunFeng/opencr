#!/usr/bin/env python3
"""
清理修复运行留在代码平台上的东西：关闭它提出的合并请求、删除修复分支。单条与批量清理都走这里（cleanup_fixes）。

清理而不是删除合并请求：平台删除合并请求要 Owner 权限，还会把上面的讨论一起抹掉；关闭后需要时仍能重新打开。
FixRun 记录本身保留并记下清理时刻，这个问题仍算修复过，批量修复不会再给它发起 ——
否则清理掉的合并请求，下一轮巡检后自动修复又会原样提一个出来。

巡检配置了自动清理修复（fix_cleanup_after_days）时，每轮巡检结束后按同一套规则清理结束已久、合并请求也久无动静的修复（auto_cleanup_fixes）。

判定逻辑（cleanup_rejection / branch_owned_by_fix / mr_active_since）是纯函数，不碰网络与数据库。
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..review.common import ReviewError
from ..review.config import load_gitlab_config
from ..review.gitlab import (
    close_merge_request, delete_branch, get_branch, get_latest_merge_request_note, get_merge_request,
)
from ..storage import repo
from ..storage.models import RUN_RUNNING, utcnow
from .fix import FIX_BRANCH_PREFIX, fix_unsupported_reason, project_path_from_url, republish_ledger

logger = logging.getLogger(__name__)

# 一次请求最多清理几条。每条要三四次平台接口调用，且在请求里同步执行；一页列表最多勾 50 条，再多就分几次点
MAX_CLEANUP_PER_REQUEST = 50

CLEANUP_CLEANED = "cleaned"
# 这一条不该被清理（还在进行、没有可清理的、分支被人接手了），平台上什么都没动
CLEANUP_REJECTED = "rejected"
# 平台接口出错，可能做了一半（例如合并请求已关闭、分支没删掉）；再清理一次会接着做完
CLEANUP_FAILED = "failed"

# 自动清理每轮最多处理几条。逐条同步调平台接口，刚打开开关时积压的可能有几百条，一轮全做完会撞上平台限流；
# 剩下的留给下一轮，候选按最久没看过的优先轮换（见 repo.list_survey_fix_cleanup_candidates）
MAX_AUTO_CLEANUP_PER_RUN = 50


def cleanup_rejection(fix: Optional[dict]) -> str:
    """这条修复运行为什么不能清理；能清理时返回空串。"""
    if fix is None:
        return "修复记录不存在"
    if fix.get("status") == RUN_RUNNING:
        # 疑似中断的也在其中：它可能正要推分支，这时删掉分支，它随后提出的合并请求会指向一个不存在的分支
        return "修复还在进行，结束或确认中断后再清理"
    if not fix.get("source_branch") and not fix.get("mr_iid"):
        return "这次修复没有推送过分支，也没有合并请求，没有可清理的"
    branch = fix.get("source_branch") or ""
    if branch and not branch.startswith(FIX_BRANCH_PREFIX):
        # 记录里的分支名只会是我们自己起的；不是这个前缀说明库被改过，宁可不删也不要删错别人的分支
        return f"分支 {branch} 不是 OpenCR 的修复分支，不做删除"
    return ""


def branch_owned_by_fix(branch: dict, commit_sha: str, base_sha: str) -> bool:
    """
    分支头是否仍是这次修复推上去的那个提交。不是的话说明有人在分支上接着提交了，删分支会丢掉他的工作。

    旧记录没有存 commit_sha，退回看分支头的父提交是不是 base_sha，也就是分支上只有 OpenCR 的那一个提交。
    两样都没有时认不出来，按"不是"处理。
    """
    commit = branch.get("commit") or {}
    head = str(commit.get("id") or "")
    if commit_sha:
        return head == commit_sha
    if base_sha:
        return list(commit.get("parent_ids") or []) == [base_sha]
    return False


def _parse_platform_time(raw) -> Optional[datetime]:
    """把平台给的 ISO 时间（带 Z 或时区偏移）转成 naive UTC；读不出时返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        # Python 3.11 之前的 fromisoformat 不认末尾的 Z
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def mr_active_since(mr: dict, latest_note: Optional[dict], cutoff: datetime) -> bool:
    """
    合并请求在 cutoff（naive UTC）之后是否有过动静：最新一条评论（含改派、改标题这类系统记录）的时刻，没有评论时看提出时刻。

    不看合并请求的 updated_at：目标分支有新提交时平台会重算可合并状态，活跃仓库里没人理的合并请求也会一直显得"刚更新过"，
    自动清理就永远轮不到它们。源分支上的新提交不用在这里看，清理前已经按分支头挡过（branch_owned_by_fix）。
    读不出时间按"有动静"处理：自动清理宁可这一轮不关，也不要关掉一个可能有人正在看的合并请求。
    """
    raw = latest_note.get("created_at") if latest_note else (mr or {}).get("created_at")
    moment = _parse_platform_time(raw)
    return moment is None or moment > cutoff


def cleanup_fix(run_uid: str, idle_cutoff: Optional[datetime] = None) -> dict:
    """
    清理一条修复运行，返回 {"run_uid", "outcome", "message", "already_cleaned"}，outcome 取 CLEANUP_*。任何错误都收敛进返回值，不向上抛。

    先查分支、再关合并请求、最后删分支：分支被人接手时整条拒绝，合并请求也不关 —— 他多半正在那个合并请求上继续改。
    已经清理过的再清理一次视为成功，不重复调用平台。
    idle_cutoff（naive UTC）只由自动清理传入：合并请求还开着、且在它之后有过动静时整条拒绝，说明还有人在处理。
    """
    result = {"run_uid": run_uid, "outcome": CLEANUP_REJECTED, "message": "", "already_cleaned": False}
    fix = repo.get_survey_fix(run_uid)
    if fix is not None and fix.get("cleaned_at"):
        result.update(outcome=CLEANUP_CLEANED, message="已经清理过", already_cleaned=True)
        return result
    reason = cleanup_rejection(fix) or fix_unsupported_reason()
    if reason:
        result["message"] = reason
        return result
    project_path = project_path_from_url(fix.get("repo_url") or "", load_gitlab_config().get("url", ""))
    if not project_path:
        result["message"] = f"无法从仓库地址解析出项目路径：{fix.get('repo_url')}"
        return result

    branch_name = fix.get("source_branch") or ""
    notes: List[str] = []
    state = ""
    try:
        branch = get_branch(project_path, branch_name) if branch_name else None
        if branch is not None and not branch_owned_by_fix(branch, fix.get("commit_sha") or "", fix.get("base_sha") or ""):
            # 也可能是平台上做了变基：那同样改了分支上的提交，宁可让人去平台上看一眼再手动删
            result["message"] = (
                f"分支 {branch_name} 的最新提交已不是 OpenCR 推送的那个（有人追加了提交，或在平台上变基过），未清理"
            )
            return result
        if fix.get("mr_iid"):
            mr = get_merge_request(project_path, fix["mr_iid"])
            state = str((mr or {}).get("state") or "").lower()
            # locked 是平台在合并过程中的短暂状态，同样还没结束，按打开处理
            if state in ("opened", "locked"):
                if idle_cutoff is not None and mr_active_since(
                    mr, get_latest_merge_request_note(project_path, fix["mr_iid"]), idle_cutoff
                ):
                    # 放在关闭之前、且此前没有动过平台：拒绝时分支与合并请求都原样留着
                    result["message"] = "合并请求最近还有动静（评论、改派等），这一轮不自动清理"
                    return result
                close_merge_request(project_path, fix["mr_iid"])
                state = "closed"
                notes.append("已关闭合并请求")
            elif state == "merged":
                notes.append("合并请求已合并")
            elif mr is None:
                notes.append("合并请求已不存在")
            else:
                notes.append("合并请求已是关闭状态")
        if branch is not None:
            # 已合并的合并请求通常已由平台删掉源分支（提出时设了合并后删除），走到这里多半是有人取消了那个选项
            delete_branch(project_path, branch_name)
            notes.append("已删除分支")
        elif branch_name:
            notes.append("分支已不存在")
    except ReviewError as e:
        logger.warning("Survey fix cleanup failed: run=%s: %s", run_uid, e)
        result.update(outcome=CLEANUP_FAILED, message=str(e))
        return result
    except Exception as e:
        logger.exception("Survey fix cleanup crashed: run=%s", run_uid)
        result.update(outcome=CLEANUP_FAILED, message=str(e))
        return result

    # 返回 False 只会是另一个请求刚刚也清理完了它：平台上的动作是幂等的，两边都算清理成功
    repo.mark_survey_fix_cleaned(run_uid, state)
    logger.info("Survey fix cleaned: run=%s project=%s: %s", run_uid, project_path, "，".join(notes))
    result.update(outcome=CLEANUP_CLEANED, message="，".join(notes))
    return result


def cleanup_fixes(run_uids: List[str], idle_cutoff: Optional[datetime] = None) -> List[dict]:
    """
    逐条清理，返回每条的结果（见 cleanup_fix，idle_cutoff 同）；清理成功的巡检在后台重推一次台账，把已清理的链接从外部表格里撤掉。

    逐条而不是并行：每条只是几次平台接口调用，并行省下的时间不多，却会在限流时让一半失败。
    """
    results: List[dict] = []
    survey_ids: Dict[int, None] = {}
    for run_uid in dict.fromkeys(run_uids):
        outcome = cleanup_fix(run_uid, idle_cutoff)
        results.append(outcome)
        # 早就清理过的不用重推：上次清理时已经推过了
        if outcome["outcome"] == CLEANUP_CLEANED and not outcome["already_cleaned"]:
            fix = repo.get_survey_fix(run_uid)
            if fix is not None:
                survey_ids[fix["survey_id"]] = None
    # 手动与自动清理都记下没清理掉的原因：页面上标的是"最近一次为什么没清掉"，只记自动的话，
    # 手动清理刚被拒绝，页面上还挂着上一次自动清理的旧原因。只有自动清理才记"看过的时刻"，它决定的是自动清理的轮换
    repo.record_survey_fix_cleanup_checks(
        reasons={r["run_uid"]: "" if r["outcome"] == CLEANUP_CLEANED else r["message"] for r in results},
        checked=idle_cutoff is not None,
    )
    if survey_ids:
        _republish_in_background(list(survey_ids))
    return results


def auto_cleanup_fixes(survey: dict) -> List[dict]:
    """
    巡检配置了自动清理修复时，清理它名下结束超过 fix_cleanup_after_days 天、合并请求也这么久没有动静的修复，
    返回每条的结果；没开、或当前平台不支持修复时返回空列表。

    规则与 Admin 手动清理完全相同（同一个 cleanup_fix），只多一道"合并请求最近有没有动静"（见 mr_active_since）：
    提出得早不等于没人管，有人还在评论、改派的不关。已合并、已关闭的不看动静，照常收尾（删分支、标为已清理）。
    没清理掉的原因记在修复记录上（cleanup_note，见 cleanup_fixes），「修复合并请求」页据此标出哪些一直卡着、为什么。
    """
    days = survey.get("fix_cleanup_after_days")
    if not days or int(days) <= 0:
        return []
    unsupported = fix_unsupported_reason()
    if unsupported:
        logger.warning("Auto fix cleanup skipped: survey=%s: %s", survey.get("slug"), unsupported)
        return []
    cutoff = utcnow() - timedelta(days=int(days))
    run_uids = repo.list_survey_fix_cleanup_candidates(survey["survey_uid"], cutoff, MAX_AUTO_CLEANUP_PER_RUN)
    if not run_uids:
        return []
    results = cleanup_fixes(run_uids, idle_cutoff=cutoff)
    logger.info(
        "Auto fix cleanup finished: survey=%s candidates=%s cleaned=%s",
        survey.get("slug"), len(run_uids), sum(1 for r in results if r["outcome"] == CLEANUP_CLEANED),
    )
    return results


def _republish_in_background(survey_ids: List[int]) -> None:
    """在后台线程里逐个重推台账：输出目标正忙时 republish_ledger 会等上几分钟，不能占住 HTTP 请求。"""

    def _target():
        """执行体。republish_ledger 内部已收敛异常，这里只兜最后一层。"""
        for survey_id in survey_ids:
            try:
                republish_ledger(survey_id)
            except Exception:
                logger.exception("Republishing ledger after cleanup crashed: survey=%s", survey_id)

    threading.Thread(target=_target, name="opencr-survey-fix-cleanup", daemon=True).start()
