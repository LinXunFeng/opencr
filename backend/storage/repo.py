#!/usr/bin/env python3
"""
ReviewRun / Finding 的存取与聚合。

上层（review_server、admin）只通过本模块访问数据库，不直接持有 Session。
"""

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from sqlalchemy import delete, func, select, text, update

from .db import session_scope
from .models import (
    DELIVERY_SUMMARY_ONLY,
    FIX_MIN_SEVERITIES,
    FIX_TRIGGER_MANUAL,
    ERROR_INTERRUPTED,
    ERROR_REVIEW,
    ERROR_UNEXPECTED,
    PHASE_DONE,
    REASON_NOT_TRACKABLE,
    REPO_FETCH_FAILED,
    REPO_IGNORED,
    REPO_TRUNCATED,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SKIPPED,
    RUN_SUCCEEDED,
    SETTLED_VERDICTS,
    SEVERITY_UNKNOWN,
    SEVERITY_WARNING,
    TRACKABLE_DELIVERIES,
    VERDICT_ACCEPTED,
    VERDICT_UNDECIDED,
    VERDICT_UNTRACKABLE,
    AppSetting,
    Finding,
    LeaderLease,
    MrSettlement,
    ReviewRun,
    utcnow,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# ReviewRun 生命周期
# --------------------------------------------------------------------------

def start_run(
    project_id: int,
    mr_iid: int,
    trigger: str,
    review_mode: str,
    mr_title: str = "",
    project_path: str = "",
) -> str:
    """登记一次 ReviewRun，返回 run_uid。"""
    run_uid = str(uuid.uuid4())
    now = utcnow()
    with session_scope() as session:
        session.add(
            ReviewRun(
                run_uid=run_uid,
                project_id=int(project_id),
                project_path=project_path or None,
                mr_iid=int(mr_iid),
                mr_title=mr_title or None,
                trigger=trigger,
                review_mode=review_mode,
                status=RUN_RUNNING,
                started_at=now,
                heartbeat_at=now,
            )
        )
    return run_uid


def save_review_input(run_uid: str, review_input: dict) -> None:
    """保存本次运行的原始选择参数与已确定的提交范围。"""
    with session_scope() as session:
        session.execute(update(ReviewRun).where(ReviewRun.run_uid == run_uid).values(
            review_input=json.dumps(review_input, ensure_ascii=False)))


def _original_input(run: ReviewRun) -> Optional[dict]:
    """返回可恢复的原范围参数；旧记录或不完整记录返回 None。"""
    try:
        value = json.loads(run.review_input or "null")
    except (ValueError, TypeError):
        # 参数损坏只禁用原范围，仍允许按当前配置审查最新全量。
        return None
    if not isinstance(value, dict) or run.review_mode not in {"overall", "file", "hybrid"}:
        return None
    if not all(isinstance(value.get(k), str) and value[k] for k in ("from_sha", "to_sha")):
        return None
    if not isinstance(value.get("review_skill"), str):
        return None
    refs = value.get("diff_refs")
    if not isinstance(refs, dict) or not all(isinstance(refs.get(k), str) and refs[k] for k in ("base_sha", "start_sha", "head_sha")):
        return None
    return value


class RetryRejected(ValueError):
    """重新触发被拒绝，携带响应状态与阻塞运行标识。"""

    def __init__(self, message: str, status: int = 409, active_run_uid: str = ""):
        """构造可向调用方展示的拒绝原因。"""
        super().__init__(message)
        self.status = status
        self.active_run_uid = active_run_uid


def start_retry_run(source_uid: str, scope: str, review_mode: str, review_skill: str) -> dict:
    """原子检查失败来源与同 MR 运行，登记重试并返回执行参数。"""
    if scope not in {"latest", "original"}:
        raise RetryRejected("重试范围必须为 latest 或 original", 400)
    with session_scope() as session:
        # SQLite 先获得写锁再读取，避免不同 gunicorn worker 同时检查为空后各自插入。
        # 事务内不访问 GitLab；旧入口仍可在提交后接收新推送，不建立全入口互斥。
        session.execute(text("BEGIN IMMEDIATE"))
        source = session.scalar(select(ReviewRun).where(ReviewRun.run_uid == source_uid))
        if source is None:
            raise RetryRejected("原审查运行不存在或已清理", 404)
        if source.status != RUN_FAILED:
            raise RetryRejected("仅明确失败的审查运行可以重新触发")
        review_input = _original_input(source) if scope == "original" else None
        if scope == "original" and review_input is None:
            raise RetryRejected("原运行缺少完整范围或选择参数，请选择最新全量")
        active = session.scalar(select(ReviewRun.run_uid).where(
            ReviewRun.project_id == source.project_id, ReviewRun.mr_iid == source.mr_iid,
            ReviewRun.status == RUN_RUNNING).order_by(ReviewRun.started_at.desc()).limit(1))
        # Stale 没有终止保证，必须与正常 running 一样阻止重试。
        if active:
            raise RetryRejected("该合并请求已有审查正在运行（含疑似中断），请查看运行记录", active_run_uid=active)
        if review_input is not None:
            review_mode = source.review_mode
            review_skill = review_input["review_skill"]
        run_uid = str(uuid.uuid4())
        session.add(ReviewRun(
            run_uid=run_uid, project_id=source.project_id, mr_iid=source.mr_iid,
            project_path=source.project_path, mr_title=source.mr_title,
            trigger="manual", review_mode=review_mode, status=RUN_RUNNING,
            retry_of_uid=source_uid, retry_scope=scope,
            review_input=json.dumps(review_input, ensure_ascii=False) if review_input else None,
        ))
        return {"run_uid": run_uid, "project_id": source.project_id, "mr_iid": source.mr_iid,
                "mr_title": source.mr_title or "", "review_mode": review_mode,
                "review_skill": review_skill, "original_input": review_input}


def record_skipped_run(
    project_id: int,
    mr_iid: int,
    trigger: str,
    skip_reason: str,
    mr_title: str = "",
    project_path: str = "",
) -> str:
    """
    登记一次被跳过的 ReviewRun。

    跳过不是错误，但"为什么这个 MR 没被审"是真实的运维问题，值得留痕。
    """
    run_uid = str(uuid.uuid4())
    now = utcnow()
    with session_scope() as session:
        session.add(
            ReviewRun(
                run_uid=run_uid,
                project_id=int(project_id),
                project_path=project_path or None,
                mr_iid=int(mr_iid),
                mr_title=mr_title or None,
                trigger=trigger,
                review_mode="",
                status=RUN_SKIPPED,
                skip_reason=skip_reason,
                phase=PHASE_DONE,
                started_at=now,
                heartbeat_at=now,
                finished_at=now,
            )
        )
    return run_uid


def update_progress(
    run_uid: str,
    phase: str = "",
    files_total: Optional[int] = None,
    files_done: Optional[int] = None,
    review_skills: Optional[List[str]] = None,
) -> None:
    """推进阶段/文件计数，并顺带刷新心跳。已结束（含已确认中断）的运行不再改写。"""
    values: Dict[str, object] = {"heartbeat_at": utcnow()}
    if phase:
        values["phase"] = phase
    if files_total is not None:
        values["files_total"] = int(files_total)
    if files_done is not None:
        values["files_done"] = int(files_done)
    if review_skills is not None:
        values["review_skills"] = json.dumps(sorted(set(review_skills)), ensure_ascii=False)

    with session_scope() as session:
        session.execute(
            update(ReviewRun)
            .where(ReviewRun.run_uid == run_uid, ReviewRun.status == RUN_RUNNING)
            .values(**values)
        )


def heartbeat(run_uid: str) -> None:
    """仅刷新心跳。长时间停留在同一阶段时（例如单次大模型调用）用它保活。"""
    with session_scope() as session:
        session.execute(
            update(ReviewRun)
            .where(ReviewRun.run_uid == run_uid, ReviewRun.status == RUN_RUNNING)
            .values(heartbeat_at=utcnow())
        )


def add_degradation(run_uid: str, kind: str, count: int = 1) -> None:
    """
    累加一条降级记录。

    Degradation 与 status 正交：一次 run 可以既 succeeded 又带多条降级。
    """
    with session_scope() as session:
        run = session.scalar(select(ReviewRun).where(ReviewRun.run_uid == run_uid))
        if run is None:
            return
        try:
            items = json.loads(run.degradations) if run.degradations else []
        except (ValueError, TypeError):
            items = []
        if not isinstance(items, list):
            items = []

        for item in items:
            if isinstance(item, dict) and item.get("kind") == kind:
                item["count"] = int(item.get("count", 0)) + int(count)
                break
        else:
            items.append({"kind": kind, "count": int(count)})

        run.degradations = json.dumps(items, ensure_ascii=False)
        # 降级是真实发生过的事，照记；心跳只给进行中的运行续，已确认中断的不能被它"救活"
        if run.status == RUN_RUNNING:
            run.heartbeat_at = utcnow()


def finish_run(
    run_uid: str,
    status: str,
    error_kind: str = "",
    error_message: str = "",
) -> bool:
    """
    收尾一次 ReviewRun，返回是否由本次调用收尾。error_message 截断到 4000 字符，避免超长堆栈撑爆行。

    只收尾仍在进行中的运行：被确认中断的运行可能其实还活着，它迟到的收尾不能把
    "已确认中断"改写回成功或另一种失败。返回 False 时调用方应跳过收尾之后的对外动作。
    """
    now = utcnow()
    values: Dict[str, object] = {
        "status": status,
        "phase": PHASE_DONE,
        "heartbeat_at": now,
        "finished_at": now,
    }
    if error_kind:
        values["error_kind"] = error_kind
    if error_message:
        values["error_message"] = str(error_message)[:4000]

    with session_scope() as session:
        result = session.execute(
            update(ReviewRun)
            .where(ReviewRun.run_uid == run_uid, ReviewRun.status == RUN_RUNNING)
            .values(**values)
        )
        return result.rowcount > 0


INTERRUPTED_MESSAGE = "管理员确认中断：心跳超时后由人工关闭了这条记录，系统没有停止任何正在执行的工作。"

# 确认中断的三种结果
INTERRUPT_OK = "ok"
INTERRUPT_NOT_FOUND = "not_found"
INTERRUPT_NOT_STALE = "not_stale"


def _confirm_interrupted(model, run_uid: str, stale_after_seconds: int) -> str:
    """
    把一个 Stale 运行记为失败（已确认中断），ReviewRun、SurveyRun 与修复运行（SurveyFix）共用。

    返回 INTERRUPT_OK、INTERRUPT_NOT_FOUND 或 INTERRUPT_NOT_STALE。阶段与最后心跳保留原值，
    它们是"卡在哪一步、何时失联"的唯一线索。
    """
    now = utcnow()
    cutoff = now - timedelta(seconds=stale_after_seconds)
    with session_scope() as session:
        exists = session.scalar(select(model.id).where(model.run_uid == run_uid))
        if exists is None:
            return INTERRUPT_NOT_FOUND
        # 判定与改写放进同一条 UPDATE：先查后改的话，查完那一刻心跳刚好恢复的运行也会被关掉
        result = session.execute(
            update(model)
            .where(
                model.run_uid == run_uid,
                model.status == RUN_RUNNING,
                model.heartbeat_at < cutoff,
            )
            .values(
                status=RUN_FAILED,
                finished_at=now,
                error_kind=ERROR_INTERRUPTED,
                error_message=INTERRUPTED_MESSAGE,
            )
        )
        return INTERRUPT_OK if result.rowcount > 0 else INTERRUPT_NOT_STALE


def confirm_run_interrupted(run_uid: str, stale_after_seconds: int) -> str:
    """确认一个 Stale 的 ReviewRun 已中断。返回值见 ``_confirm_interrupted``。"""
    return _confirm_interrupted(ReviewRun, run_uid, stale_after_seconds)


# --------------------------------------------------------------------------
# Finding
# --------------------------------------------------------------------------

def record_finding(
    run_uid: str,
    file_path: str,
    line: int,
    body: str,
    delivery: str,
    severity: str = SEVERITY_UNKNOWN,
    discussion_id: str = "",
    note_id: Optional[int] = None,
) -> None:
    """
    落库一条 Finding。

    没有 discussion_id 的（overall 整体评论里的、行内投递失败降级的）仍然入库，
    但 verdict 直接定为 untrackable —— 它们要计入 Coverage 的分母，不计入采纳率的分母。
    """
    trackable = delivery in TRACKABLE_DELIVERIES and bool(discussion_id)
    with session_scope() as session:
        run = session.scalar(select(ReviewRun).where(ReviewRun.run_uid == run_uid))
        if run is None:
            logger.warning("record_finding: run %s not found, skip", run_uid)
            return
        session.add(
            Finding(
                run_id=run.id,
                project_id=run.project_id,
                mr_iid=run.mr_iid,
                discussion_id=discussion_id or None,
                note_id=note_id,
                file_path=file_path or None,
                line=max(int(line or 0), 0),
                severity=severity or SEVERITY_UNKNOWN,
                body=(body or "")[:8000],
                delivery=delivery,
                verdict=VERDICT_UNDECIDED if trackable else VERDICT_UNTRACKABLE,
                verdict_reason=None if trackable else REASON_NOT_TRACKABLE,
                settled_at=None if trackable else utcnow(),
            )
        )


# --------------------------------------------------------------------------
# 结算
# --------------------------------------------------------------------------

def list_undecided_findings(project_id: int, mr_iid: int) -> List[dict]:
    """取出某 MR 下所有待结算的 Finding（已带 discussion_id 的）。"""
    with session_scope() as session:
        rows = session.scalars(
            select(Finding).where(
                Finding.project_id == int(project_id),
                Finding.mr_iid == int(mr_iid),
                Finding.verdict == VERDICT_UNDECIDED,
                Finding.discussion_id.isnot(None),
            )
        ).all()
        return [
            {
                "id": r.id,
                "discussion_id": r.discussion_id,
                "note_id": r.note_id,
                "file_path": r.file_path,
                "line": r.line,
            }
            for r in rows
        ]


def apply_verdicts(verdicts: Dict[int, tuple]) -> int:
    """批量写入 Verdict。入参：{finding_id: (verdict, reason)}。返回更新条数。"""
    if not verdicts:
        return 0
    now = utcnow()
    updated = 0
    with session_scope() as session:
        for finding_id, (verdict, reason) in verdicts.items():
            result = session.execute(
                update(Finding)
                .where(Finding.id == int(finding_id))
                .values(verdict=verdict, verdict_reason=reason, settled_at=now)
            )
            updated += result.rowcount or 0
    return updated


def mark_mr_settled(project_id: int, mr_iid: int, mr_state: str) -> None:
    """记录 MR 已结算，避免重复结算（webhook 与 reconciler 可能都会触发）。"""
    with session_scope() as session:
        existing = session.get(MrSettlement, (int(project_id), int(mr_iid)))
        if existing is None:
            session.add(
                MrSettlement(
                    project_id=int(project_id),
                    mr_iid=int(mr_iid),
                    mr_state=mr_state,
                    settled_at=utcnow(),
                )
            )
        else:
            existing.mr_state = mr_state
            existing.settled_at = utcnow()


def is_mr_settled(project_id: int, mr_iid: int) -> bool:
    """该 MR 是否已经结算过。"""
    with session_scope() as session:
        return session.get(MrSettlement, (int(project_id), int(mr_iid))) is not None


def list_mrs_pending_settlement(limit: int = 50) -> List[dict]:
    """
    找出还有 undecided Finding、且尚未结算过的 MR。

    reconciler 用它兜底 —— 服务宕机期间错过的 merge webhook 不会让数据永远悬空。
    """
    with session_scope() as session:
        settled_keys = set(
            session.execute(select(MrSettlement.project_id, MrSettlement.mr_iid)).all()
        )
        rows = session.execute(
            select(Finding.project_id, Finding.mr_iid)
            .where(
                Finding.verdict == VERDICT_UNDECIDED,
                Finding.discussion_id.isnot(None),
            )
            .group_by(Finding.project_id, Finding.mr_iid)
        ).all()

    pending = [
        {"project_id": r[0], "mr_iid": r[1]}
        for r in rows
        if (r[0], r[1]) not in settled_keys
    ]
    return pending[: max(int(limit), 1)]


# --------------------------------------------------------------------------
# 后台查询
# --------------------------------------------------------------------------

def _is_stale(run, stale_after_seconds: int) -> bool:
    """一条运行记录（ReviewRun、SurveyRun 或修复运行）是否疑似中断：仍在进行中，但心跳已超过阈值。"""
    return (
        run.status == RUN_RUNNING
        and run.heartbeat_at is not None
        and (utcnow() - run.heartbeat_at) > timedelta(seconds=stale_after_seconds)
    )


def _run_to_dict(run: ReviewRun, stale_after_seconds: int = 600) -> dict:
    """
    把 ReviewRun 转成可直接返回给前端的字典。

    JSON 字段（degradations / review_skills）解析失败时退化为空列表而不是抛错：
    后台是只读视图，一条脏数据不该让整个面板打不开。
    """
    try:
        degradations = json.loads(run.degradations) if run.degradations else []
    except (ValueError, TypeError):
        degradations = []
    try:
        skills = json.loads(run.review_skills) if run.review_skills else []
    except (ValueError, TypeError):
        skills = []

    is_stale = _is_stale(run, stale_after_seconds)
    return {
        "run_uid": run.run_uid,
        "project_id": run.project_id,
        "project_path": run.project_path or "",
        "mr_iid": run.mr_iid,
        "mr_title": run.mr_title or "",
        "trigger": run.trigger,
        "review_mode": run.review_mode,
        "review_skills": skills,
        "retry_of_uid": run.retry_of_uid or "",
        "retry_scope": run.retry_scope or "",
        "original_retry_available": _original_input(run) is not None,
        "status": run.status,
        "skip_reason": run.skip_reason or "",
        "phase": run.phase or "",
        "files_total": run.files_total,
        "files_done": run.files_done,
        "degradations": degradations,
        "error_kind": run.error_kind or "",
        "error_message": run.error_message or "",
        "started_at": run.started_at.isoformat() if run.started_at else "",
        "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else "",
        "finished_at": run.finished_at.isoformat() if run.finished_at else "",
        "is_stale": is_stale,
    }


def list_active_runs(stale_after_seconds: int = 600) -> List[dict]:
    """
    进行中的 ReviewRun。

    Stale 只标注不改写 —— 多进程下本进程无法断言其他进程的 run 已死，
    它也可能只是卡在一次特别慢的模型调用上。
    """
    with session_scope() as session:
        runs = session.scalars(
            select(ReviewRun)
            .where(ReviewRun.status == RUN_RUNNING)
            .order_by(ReviewRun.started_at.desc())
        ).all()
        return [_run_to_dict(r, stale_after_seconds) for r in runs]


def list_recent_runs(limit: int = 50, status: str = "", stale_after_seconds: int = 600) -> List[dict]:
    """最近的 ReviewRun，按开始时间倒序。status 为空表示不过滤。"""
    with session_scope() as session:
        stmt = select(ReviewRun).order_by(ReviewRun.started_at.desc()).limit(max(int(limit), 1))
        if status:
            stmt = stmt.where(ReviewRun.status == status)
        runs = session.scalars(stmt).all()
        return [_run_to_dict(r, stale_after_seconds) for r in runs]


def get_run_detail(run_uid: str, stale_after_seconds: int = 600) -> Optional[dict]:
    """单次 ReviewRun 及其全部 Finding；run_uid 不存在时返回 None。"""
    with session_scope() as session:
        run = session.scalar(select(ReviewRun).where(ReviewRun.run_uid == run_uid))
        if run is None:
            return None
        findings = session.scalars(
            select(Finding).where(Finding.run_id == run.id).order_by(Finding.id.asc())
        ).all()
        detail = _run_to_dict(run, stale_after_seconds)
        detail["findings"] = [_finding_to_dict(f) for f in findings]
        return detail



def _finding_to_dict(finding: Finding) -> dict:
    """将审查发现转换为接口数据，正文的访问控制由后台 API 负责。"""
    return {
        "id": finding.id, "file_path": finding.file_path or "", "line": finding.line,
        "severity": finding.severity, "delivery": finding.delivery,
        "discussion_id": finding.discussion_id or "", "verdict": finding.verdict,
        "verdict_reason": finding.verdict_reason or "", "body": finding.body or "",
    }


def get_change_history(
    run_uid: str, limit: int = 20, offset: int = 0,
    severity: str = "", verdict: str = "", stale_after_seconds: int = 600,
) -> Optional[dict]:
    """查询指定运行所属合并请求的历史批次及发现；分页以批次为单位，找不到运行返回 None。"""
    with session_scope() as session:
        anchor = session.scalar(select(ReviewRun).where(ReviewRun.run_uid == run_uid))
        if anchor is None:
            return None
        same_change = (ReviewRun.project_id == anchor.project_id, ReviewRun.mr_iid == anchor.mr_iid)
        total = session.scalar(select(func.count()).select_from(ReviewRun).where(*same_change))
        runs = session.scalars(
            select(ReviewRun).where(*same_change)
            .order_by(ReviewRun.started_at.desc(), ReviewRun.id.desc())
            .offset(max(offset, 0)).limit(max(1, min(limit, 100)))
        ).all()
        groups = {run.id: {**_run_to_dict(run, stale_after_seconds), "findings": []} for run in runs}
        if groups:
            stmt = select(Finding).where(Finding.run_id.in_(groups)).order_by(Finding.id.asc())
            if severity:
                stmt = stmt.where(Finding.severity == severity)
            if verdict:
                stmt = stmt.where(Finding.verdict == verdict)
            # 保留零发现批次：失败、跳过和筛选后无匹配都不能被误读为从未运行。
            for finding in session.scalars(stmt):
                groups[finding.run_id]["findings"].append(_finding_to_dict(finding))
        return {"items": list(groups.values()), "total": total}

def _window_start(days: int) -> datetime:
    """统计时间窗的起点。"""
    return utcnow() - timedelta(days=max(int(days), 1))


def error_stats(days: int = 7) -> dict:
    """
    错误统计。failed / degraded / skipped 分开报，不混为一谈。

    行内投递失败有 fallback 兜底、内容最终仍送达用户，属于 degraded 而非 failed。
    """
    since = _window_start(days)
    with session_scope() as session:
        by_status = dict(
            session.execute(
                select(ReviewRun.status, func.count())
                .where(ReviewRun.started_at >= since)
                .group_by(ReviewRun.status)
            ).all()
        )
        by_error_kind = dict(
            session.execute(
                select(ReviewRun.error_kind, func.count())
                .where(ReviewRun.started_at >= since, ReviewRun.error_kind.isnot(None))
                .group_by(ReviewRun.error_kind)
            ).all()
        )
        degraded_rows = session.scalars(
            select(ReviewRun.degradations).where(
                ReviewRun.started_at >= since, ReviewRun.degradations.isnot(None)
            )
        ).all()

    by_degradation: Dict[str, int] = {}
    degraded_runs = 0
    for raw in degraded_rows:
        try:
            items = json.loads(raw) or []
        except (ValueError, TypeError):
            continue
        if not isinstance(items, list) or not items:
            continue
        degraded_runs += 1
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "unknown")
            by_degradation[kind] = by_degradation.get(kind, 0) + int(item.get("count", 0) or 0)

    return {
        "window_days": days,
        "total_runs": sum(by_status.values()),
        "succeeded": by_status.get(RUN_SUCCEEDED, 0),
        "failed": by_status.get(RUN_FAILED, 0),
        "running": by_status.get(RUN_RUNNING, 0),
        "skipped": by_status.get(RUN_SKIPPED, 0),
        "degraded_runs": degraded_runs,
        "by_error_kind": by_error_kind,
        "by_degradation": by_degradation,
    }


def verdict_stats(days: int = 30) -> dict:
    """
    采纳统计。

    采纳率与 Coverage 必须成对呈现：只报采纳率会让人误以为分母是全部产出，
    而实际上 overall 整体评论里的 Finding 与降级成普通评论的 Finding 都不可追踪。
    """
    since = _window_start(days)
    with session_scope() as session:
        by_verdict = dict(
            session.execute(
                select(Finding.verdict, func.count())
                .where(Finding.created_at >= since)
                .group_by(Finding.verdict)
            ).all()
        )
        by_delivery = dict(
            session.execute(
                select(Finding.delivery, func.count())
                .where(Finding.created_at >= since)
                .group_by(Finding.delivery)
            ).all()
        )
        by_severity = dict(
            session.execute(
                select(Finding.severity, func.count())
                .where(Finding.created_at >= since)
                .group_by(Finding.severity)
            ).all()
        )

    total = sum(by_verdict.values())
    trackable = total - by_verdict.get(VERDICT_UNTRACKABLE, 0)
    settled = sum(by_verdict.get(v, 0) for v in SETTLED_VERDICTS)
    accepted = by_verdict.get(VERDICT_ACCEPTED, 0)

    return {
        "window_days": days,
        "total_findings": total,
        "trackable_findings": trackable,
        "settled_findings": settled,
        "accepted": accepted,
        # 分母是已结算的 Finding，未结算的（MR 还开着）不该稀释比率
        "acceptance_rate": round(accepted / settled, 4) if settled else None,
        # Coverage：可追踪的占全部产出的比例
        "coverage_rate": round(trackable / total, 4) if total else None,
        "by_verdict": by_verdict,
        "by_delivery": by_delivery,
        "by_severity": by_severity,
        "summary_only_findings": by_delivery.get(DELIVERY_SUMMARY_ONLY, 0),
    }


# --------------------------------------------------------------------------
# 维护
# --------------------------------------------------------------------------

def purge_old_runs(retention_days: int = 90) -> int:
    """删除超过保留期的 ReviewRun（Finding 由外键级联删除）。"""
    cutoff = _window_start(retention_days)
    with session_scope() as session:
        run_ids = session.scalars(
            select(ReviewRun.id).where(ReviewRun.started_at < cutoff)
        ).all()
        if not run_ids:
            return 0
        # SQLite 的 ON DELETE CASCADE 依赖 PRAGMA foreign_keys=ON，这里显式删以防万一
        session.execute(delete(Finding).where(Finding.run_id.in_(run_ids)))
        session.execute(delete(ReviewRun).where(ReviewRun.id.in_(run_ids)))
        return len(run_ids)


def acquire_lease(name: str, holder: str, ttl_seconds: int) -> bool:
    """
    抢占式租约：多 worker 下只让一个进程跑 reconciler。

    为一个每 5 分钟一次的清扫任务引入 redis/celery 不成比例，一行 UPDATE 足够。
    """
    now = utcnow()
    expires_at = now + timedelta(seconds=max(int(ttl_seconds), 1))
    with session_scope() as session:
        lease = session.get(LeaderLease, name)
        if lease is None:
            session.add(LeaderLease(name=name, holder=holder, expires_at=expires_at))
            return True
        if lease.holder == holder or lease.expires_at <= now:
            lease.holder = holder
            lease.expires_at = expires_at
            return True
        return False


def try_acquire_lease(name: str, holder: str, ttl_seconds: int) -> bool:
    """
    acquire_lease 的不抛异常版本：两个进程同时首次抢同一个租约时，后插入的那个会撞主键，按"没抢到"处理。
    """
    try:
        return acquire_lease(name, holder, ttl_seconds)
    except Exception:
        # 主键冲突说明别人刚抢到；其他数据库错误同样按没抢到处理，后果只是这一次不执行
        logger.warning("Acquiring lease failed: name=%s", name, exc_info=True)
        return False


def release_lease(name: str, holder: str) -> None:
    """释放自己持有的租约。别人已经接手（我们的租约过期后被抢走）时不动它。"""
    with session_scope() as session:
        session.execute(delete(LeaderLease).where(LeaderLease.name == name, LeaderLease.holder == holder))


def lease_active(name: str) -> bool:
    """这个租约当前是否被人持有且未过期。"""
    with session_scope() as session:
        lease = session.get(LeaderLease, name)
        return lease is not None and lease.expires_at > utcnow()


# --------------------------------------------------------------------------
# 运行期设置
# --------------------------------------------------------------------------

def get_setting(key: str) -> Optional[str]:
    """读取一个运行期设置；不存在返回 None（与"存在但为空串"区分开）。"""
    with session_scope() as session:
        row = session.get(AppSetting, key)
        return None if row is None else row.value


def set_setting(key: str, value: str) -> None:
    """写入一个运行期设置。"""
    with session_scope() as session:
        row = session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=value, updated_at=utcnow()))
        else:
            row.value = value
            row.updated_at = utcnow()


def set_setting_if_absent(key: str, value: str) -> str:
    """
    不存在时才写入，返回最终生效的值。

    多个 worker 会同时初始化 session secret_key，谁先写入就以谁为准；
    后来者读回已存在的值，而不是覆盖它——否则先登录的人会被踢掉。
    """
    from sqlalchemy.exc import IntegrityError

    try:
        with session_scope() as session:
            existing = session.get(AppSetting, key)
            if existing is not None:
                return existing.value or value
            session.add(AppSetting(key=key, value=value, updated_at=utcnow()))
    except IntegrityError:
        pass

    current = get_setting(key)
    return current if current is not None else value


# --------------------------------------------------------------------------
# 跨 ReviewRun 的 Finding 查询
# --------------------------------------------------------------------------

def list_findings(
    limit: int = 50,
    offset: int = 0,
    project_id: Optional[int] = None,
    verdict: str = "",
    severity: str = "",
    days: int = 30,
    include_body: bool = True,
) -> dict:
    """
    跨 ReviewRun 列出 Finding，用于"这个项目所有被忽略的严重问题"这类提问。

    include_body=False 时**不返回正文**：Guest 只能看到状态与聚合，
    正文的剔除必须发生在服务端，前端隐藏不是安全边界（见 ADR-0002）。
    """
    since = _window_start(days)
    with session_scope() as session:
        stmt = select(Finding).where(Finding.created_at >= since)
        if project_id is not None:
            stmt = stmt.where(Finding.project_id == int(project_id))
        if verdict:
            stmt = stmt.where(Finding.verdict == verdict)
        if severity:
            stmt = stmt.where(Finding.severity == severity)

        total = session.scalar(
            select(func.count()).select_from(stmt.subquery())
        ) or 0
        rows = session.scalars(
            stmt.order_by(Finding.created_at.desc())
            .offset(max(int(offset), 0))
            .limit(max(int(limit), 1))
        ).all()

        run_uids = {}
        if rows:
            for run_id, run_uid in session.execute(
                select(ReviewRun.id, ReviewRun.run_uid).where(
                    ReviewRun.id.in_({r.run_id for r in rows})
                )
            ).all():
                run_uids[run_id] = run_uid

        items = []
        for f in rows:
            item = {
                "id": f.id,
                "run_uid": run_uids.get(f.run_id, ""),
                "project_id": f.project_id,
                "mr_iid": f.mr_iid,
                "file_path": f.file_path or "",
                "line": f.line,
                "severity": f.severity,
                "delivery": f.delivery,
                "verdict": f.verdict,
                "verdict_reason": f.verdict_reason or "",
                "created_at": f.created_at.isoformat() if f.created_at else "",
            }
            if include_body:
                item["body"] = f.body or ""
            items.append(item)

    return {"total": total, "items": items, "body_included": include_body}


def list_projects(days: int = 90) -> List[dict]:
    """出现过审查记录的项目列表，供前端做筛选下拉。"""
    since = _window_start(days)
    with session_scope() as session:
        rows = session.execute(
            select(
                ReviewRun.project_id,
                func.max(ReviewRun.project_path),
                func.count(),
            )
            .where(ReviewRun.started_at >= since)
            .group_by(ReviewRun.project_id)
            .order_by(func.count().desc())
        ).all()
    return [
        {"project_id": r[0], "project_path": r[1] or "", "run_count": r[2]}
        for r in rows
    ]


def daily_run_trend(days: int = 30) -> List[dict]:
    """按天统计运行数与失败数，供控制台画趋势图。"""
    since = _window_start(days)
    with session_scope() as session:
        rows = session.execute(
            select(
                func.date(ReviewRun.started_at),
                ReviewRun.status,
                func.count(),
            )
            .where(ReviewRun.started_at >= since)
            .group_by(func.date(ReviewRun.started_at), ReviewRun.status)
        ).all()

    buckets: Dict[str, Dict[str, int]] = {}
    for day, status, count in rows:
        bucket = buckets.setdefault(str(day), {})
        bucket[status] = bucket.get(status, 0) + int(count)

    return [
        {
            "date": day,
            "succeeded": counts.get(RUN_SUCCEEDED, 0),
            "failed": counts.get(RUN_FAILED, 0),
            "skipped": counts.get(RUN_SKIPPED, 0),
            "running": counts.get(RUN_RUNNING, 0),
        }
        for day, counts in sorted(buckets.items())
    ]


def skill_hit_counts(days: int = 30) -> Dict[str, int]:
    """
    统计各 skill 在近期审查中的命中次数。

    review_skills 存的是 JSON 数组字符串，这里在 Python 侧展开计数 ——
    数据量是"每次审查一行"，没有必要为它引入 SQL 侧的 JSON 函数
    （SQLite 的 json1 扩展并非所有构建都启用）。
    """
    since = _window_start(days)
    with session_scope() as session:
        rows = session.scalars(
            select(ReviewRun.review_skills).where(
                ReviewRun.started_at >= since,
                ReviewRun.review_skills.isnot(None),
            )
        ).all()

    counts: Dict[str, int] = {}
    for raw in rows:
        try:
            names = json.loads(raw) or []
        except (ValueError, TypeError):
            continue
        if not isinstance(names, list):
            continue
        for name in names:
            key = str(name).strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


# ==========================================================================
# 定期巡检（Survey / SurveyRun）
#
# 与上面的 ReviewRun 完全分开：两者唯一的共性是都产出"问题"，
# 但巡检的产出不依附 MR discussion，不参与 Verdict 与 Coverage 的任何统计。
# ==========================================================================

def _unique_survey_slug(session, base_slug: str) -> str:
    """
    生成不冲突的工作区 slug。

    冲突是真实存在的：`我的巡检 A` 和 `另一个巡检 A` 去掉中文后 slug 一样。
    直接让唯一索引报错等于把一个可自动处理的情况丢给用户。
    """
    from .models import Survey as _Survey

    candidate = base_slug or "survey"
    suffix = 2
    while session.scalar(select(_Survey.id).where(_Survey.slug == candidate)) is not None:
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
    return candidate


def _survey_to_dict(survey: "Survey") -> dict:
    """把 Survey 转成接口数据。"""
    return {
        "survey_uid": survey.survey_uid,
        "name": survey.name,
        "slug": survey.slug,
        "enabled": bool(survey.enabled),
        "schedule_kind": survey.schedule_kind,
        "schedule_expr": survey.schedule_expr,
        "timezone": survey.timezone,
        "excluded_skills": json.loads(survey.excluded_skills) if survey.excluded_skills else [],
        "delete_workspace_after": bool(survey.delete_workspace_after),
        "budget_wall_clock_minutes": survey.budget_wall_clock_minutes,
        "budget_l1_max_chars": survey.budget_l1_max_chars,
        "budget_l2_max_focus": survey.budget_l2_max_focus,
        "budget_index_timeout_seconds": survey.budget_index_timeout_seconds,
        "retention_runs": survey.retention_runs,
        "fix_mode": survey.fix_mode,
        "fix_min_severity": survey.fix_min_severity,
        "last_run_at": survey.last_run_at.isoformat() if survey.last_run_at else "",
        "next_run_at": survey.next_run_at.isoformat() if survey.next_run_at else "",
        "created_at": survey.created_at.isoformat() if survey.created_at else "",
        "sources": [
            {
                "id": s.id,
                "kind": s.kind,
                "url": s.url,
                "branch": s.branch or "",
                "exclude_patterns": json.loads(s.exclude_patterns) if s.exclude_patterns else [],
            }
            for s in sorted(survey.sources, key=lambda x: x.id)
        ],
        "bindings": [_survey_binding_to_dict(b) for b in sorted(survey.bindings, key=lambda x: x.id)],
    }


def _survey_binding_to_dict(binding: "SurveyBinding") -> dict:
    """Binding 的接口数据。target 含表格 ID 这类访问入口，Guest 视角由路由层剔除。"""
    return {
        "id": binding.id,
        "destination": binding.destination,
        "target": _json_object(binding.target),
        "last_succeeded_push_at": (
            binding.last_succeeded_push_at.isoformat() if binding.last_succeeded_push_at else ""
        ),
    }


def _json_object(raw: Optional[str]) -> dict:
    """解析 JSON 对象字段；脏数据退化为空对象。"""
    try:
        value = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _binding_identity(destination: str, target: dict) -> tuple:
    """判定"是不是同一个 Binding"的依据：实例名 + 规整后的目标位置。"""
    return (destination, json.dumps(target or {}, sort_keys=True, ensure_ascii=False))


def _sync_survey_bindings(session, survey: "Survey", bindings: List[dict]) -> None:
    """
    按"实例名 + 目标位置"增量同步 Binding：相同的保留，多出的新建，缺少的删除。

    **不能整体删掉重建**（来源清单就是这么做的）：Binding 上记着最近一次成功推送的时刻，
    它决定"用户在表里删掉的行要不要补回"。整体重建会让每次保存巡检配置都把这个时刻清空，
    下一次推送就会把用户删掉的行统统写回去。
    """
    from .models import SurveyBinding

    wanted = []
    seen = set()
    for item in bindings or []:
        identity = _binding_identity(item["destination"], item.get("target") or {})
        if identity not in seen:
            seen.add(identity)
            wanted.append((identity, item))

    current = {_binding_identity(b.destination, _json_object(b.target)): b for b in survey.bindings}
    for identity, binding in current.items():
        if identity not in seen:
            survey.bindings.remove(binding)
    for identity, item in wanted:
        if identity not in current:
            survey.bindings.append(
                SurveyBinding(
                    destination=item["destination"],
                    target=identity[1],
                )
            )
    session.flush()


def create_survey(
    name: str,
    slug: str,
    schedule_kind: str,
    schedule_expr: str,
    timezone_name: str,
    sources: List[dict],
    next_run_at: Optional[datetime] = None,
    bindings: Optional[List[dict]] = None,
    **options,
) -> dict:
    """新建一个巡检配置，返回它的完整数据。bindings 为 [{destination, target}]，target 须已规整。"""
    from .models import FIX_MODE_MANUAL, Survey, SurveySource

    survey_uid = str(uuid.uuid4())
    with session_scope() as session:
        survey = Survey(
            survey_uid=survey_uid,
            name=name,
            slug=_unique_survey_slug(session, slug),
            schedule_kind=schedule_kind,
            schedule_expr=schedule_expr,
            timezone=timezone_name,
            next_run_at=next_run_at,
            enabled=1 if options.get("enabled", True) else 0,
            excluded_skills=json.dumps(options.get("excluded_skills") or [], ensure_ascii=False),
            delete_workspace_after=1 if options.get("delete_workspace_after") else 0,
            budget_wall_clock_minutes=options.get("budget_wall_clock_minutes"),
            budget_l1_max_chars=options.get("budget_l1_max_chars"),
            budget_l2_max_focus=options.get("budget_l2_max_focus"),
            budget_index_timeout_seconds=options.get("budget_index_timeout_seconds"),
            retention_runs=int(options.get("retention_runs") or 20),
            fix_mode=options.get("fix_mode") or FIX_MODE_MANUAL,
            fix_min_severity=options.get("fix_min_severity") or SEVERITY_WARNING,
        )
        session.add(survey)
        session.flush()
        for item in sources or []:
            session.add(
                SurveySource(
                    survey_id=survey.id,
                    kind=item.get("kind") or "repo",
                    url=item.get("url") or "",
                    branch=(item.get("branch") or "").strip() or None,
                    exclude_patterns=json.dumps(item.get("exclude_patterns") or [], ensure_ascii=False),
                )
            )
        session.flush()
        if bindings:
            _sync_survey_bindings(session, survey, bindings)
        session.refresh(survey)
        return _survey_to_dict(survey)


def update_survey(
    survey_uid: str,
    fields: dict,
    sources: Optional[List[dict]] = None,
    bindings: Optional[List[dict]] = None,
) -> Optional[dict]:
    """
    更新巡检配置。sources 传 None 表示不动来源清单，传列表表示整体替换；
    bindings 同理，但按实例名 + 目标位置增量同步（见 _sync_survey_bindings）。

    **slug 永不随改名变化** —— 改个名字就要搬几十 GB 代码，不划算，
    而且搬运过程中断会留下一个谁也说不清状态的工作区。
    """
    from .models import Survey, SurveySource

    with session_scope() as session:
        survey = session.scalar(select(Survey).where(Survey.survey_uid == survey_uid))
        if survey is None:
            return None

        for key, value in (fields or {}).items():
            if key in {"slug", "survey_uid", "id"}:
                continue
            if key == "excluded_skills":
                survey.excluded_skills = json.dumps(value or [], ensure_ascii=False)
            elif key in {"enabled", "delete_workspace_after"}:
                setattr(survey, key, 1 if value else 0)
            elif hasattr(survey, key):
                setattr(survey, key, value)
        survey.updated_at = utcnow()

        if sources is not None:
            session.execute(delete(SurveySource).where(SurveySource.survey_id == survey.id))
            for item in sources:
                session.add(
                    SurveySource(
                        survey_id=survey.id,
                        kind=item.get("kind") or "repo",
                        url=item.get("url") or "",
                        branch=(item.get("branch") or "").strip() or None,
                        exclude_patterns=json.dumps(item.get("exclude_patterns") or [], ensure_ascii=False),
                    )
                )
        if bindings is not None:
            _sync_survey_bindings(session, survey, bindings)
        session.flush()
        session.refresh(survey)
        return _survey_to_dict(survey)


def delete_survey(survey_uid: str) -> Optional[str]:
    """
    删除巡检配置及其全部运行记录，返回它的 slug（供调用方决定是否清理工作区）。

    **不连带删工作区** —— 误删一个巡检顺手把几十 GB 代码删掉是不可逆的，
    工作区清理是界面上单独的动作。
    """
    from .models import Survey

    with session_scope() as session:
        survey = session.scalar(select(Survey).where(Survey.survey_uid == survey_uid))
        if survey is None:
            return None
        slug = survey.slug
        session.delete(survey)
        return slug


def get_survey(survey_uid: str) -> Optional[dict]:
    """按 uid 读取巡检配置。"""
    from .models import Survey

    with session_scope() as session:
        survey = session.scalar(select(Survey).where(Survey.survey_uid == survey_uid))
        return None if survey is None else _survey_to_dict(survey)


def list_surveys() -> List[dict]:
    """全部巡检配置，按创建时间倒序。"""
    from .models import Survey

    with session_scope() as session:
        rows = session.scalars(select(Survey).order_by(Survey.created_at.desc())).all()
        return [_survey_to_dict(s) for s in rows]


def list_due_surveys(now: Optional[datetime] = None) -> List[dict]:
    """到期待执行的巡检。调度线程每轮调用一次，走 ix_survey_next_run 索引。"""
    from .models import Survey

    moment = now or utcnow()
    with session_scope() as session:
        rows = session.scalars(
            select(Survey).where(
                Survey.enabled == 1,
                Survey.next_run_at.isnot(None),
                Survey.next_run_at <= moment,
            )
        ).all()
        return [_survey_to_dict(s) for s in rows]


def set_survey_next_run(survey_uid: str, next_run_at: Optional[datetime], mark_ran: bool = False) -> None:
    """更新下次触发时间；mark_ran=True 时同时记录本次执行时刻。"""
    from .models import Survey

    values: Dict[str, object] = {"next_run_at": next_run_at}
    if mark_ran:
        values["last_run_at"] = utcnow()
    with session_scope() as session:
        session.execute(update(Survey).where(Survey.survey_uid == survey_uid).values(**values))


def has_running_survey_run(survey_uid: str) -> bool:
    """
    该巡检是否已有运行中的实例。

    上一次还没跑完、下一次时间点又到了时跳过而不是排队 ——
    一个每周任务积压两轮全量分析，除了烧钱没有意义。
    """
    from .models import Survey, SurveyRun

    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return False
        found = session.scalar(
            select(SurveyRun.id).where(
                SurveyRun.survey_id == survey_id, SurveyRun.status == RUN_RUNNING
            ).limit(1)
        )
        return found is not None


def start_survey_run(survey_uid: str, trigger: str) -> Optional[str]:
    """登记一次 SurveyRun，返回 run_uid；巡检不存在返回 None。"""
    from .models import Survey, SurveyRun, SURVEY_PHASE_FETCHING

    run_uid = str(uuid.uuid4())
    now = utcnow()
    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return None
        session.add(
            SurveyRun(
                run_uid=run_uid,
                survey_id=survey_id,
                trigger=trigger,
                status=RUN_RUNNING,
                phase=SURVEY_PHASE_FETCHING,
                started_at=now,
                heartbeat_at=now,
                # 一开始就写空数组：在取证之前失败或仍在进行中的运行，报告不能被当成
                # 早于该字段的旧运行去走朴素差集，把上一轮的发现全列进"已消失"
                inspected_files="[]",
            )
        )
    return run_uid


def update_survey_progress(
    run_uid: str,
    phase: str = "",
    repos_total: Optional[int] = None,
    repos_done: Optional[int] = None,
    matched_skills: Optional[List[str]] = None,
) -> None:
    """推进阶段与仓库计数，并顺带刷新心跳。已结束（含已确认中断）的运行不再改写。"""
    from .models import SurveyRun

    values: Dict[str, object] = {"heartbeat_at": utcnow()}
    if phase:
        values["phase"] = phase
    if repos_total is not None:
        values["repos_total"] = int(repos_total)
    if repos_done is not None:
        values["repos_done"] = int(repos_done)
    if matched_skills is not None:
        values["matched_skills"] = json.dumps(sorted(set(matched_skills)), ensure_ascii=False)

    with session_scope() as session:
        session.execute(
            update(SurveyRun)
            .where(SurveyRun.run_uid == run_uid, SurveyRun.status == RUN_RUNNING)
            .values(**values)
        )


def survey_heartbeat(run_uid: str) -> None:
    """仅刷新心跳。L1/L2 的单次模型调用可能很久，靠它保活。"""
    from .models import SurveyRun

    with session_scope() as session:
        session.execute(
            update(SurveyRun)
            .where(SurveyRun.run_uid == run_uid, SurveyRun.status == RUN_RUNNING)
            .values(heartbeat_at=utcnow())
        )


def add_survey_degradation(run_uid: str, kind: str, count: int = 1) -> None:
    """累加一条巡检降级记录。与 status 正交：成功的运行也可以带多条降级。"""
    from .models import SurveyRun

    with session_scope() as session:
        run = session.scalar(select(SurveyRun).where(SurveyRun.run_uid == run_uid))
        if run is None:
            return
        try:
            items = json.loads(run.degradations) if run.degradations else []
        except (ValueError, TypeError):
            items = []
        if not isinstance(items, list):
            items = []
        for item in items:
            if isinstance(item, dict) and item.get("kind") == kind:
                item["count"] = int(item.get("count", 0)) + int(count)
                break
        else:
            items.append({"kind": kind, "count": int(count)})
        run.degradations = json.dumps(items, ensure_ascii=False)
        # 降级是真实发生过的事，照记；心跳只给进行中的运行续，已确认中断的不能被它"救活"
        if run.status == RUN_RUNNING:
            run.heartbeat_at = utcnow()


def record_survey_repo(
    run_uid: str,
    repo_slug: str,
    url: str,
    branch: str = "",
    commit_sha: str = "",
    status: str = "ok",
    profile_kind: str = "",
    file_count: int = 0,
    error_message: str = "",
    index: Optional[dict] = None,
    route_count: Optional[int] = None,
    type_count: Optional[int] = None,
) -> None:
    """
    记录一次运行里单个仓库的处理结果。

    index 是画像里的建索引结果（{mode, elapsed_ms, error}，这里只取前两项，失败原因走 error_message），
    未启用 codegraph 时为 None；
    route_count / type_count 只在画像确实来自 codegraph 时传入，其余情况留 NULL。
    """
    from .models import SurveyRun, SurveyRunRepo

    with session_scope() as session:
        run = session.scalar(select(SurveyRun).where(SurveyRun.run_uid == run_uid))
        if run is None:
            return
        session.add(
            SurveyRunRepo(
                run_id=run.id,
                repo_slug=repo_slug,
                url=url,
                branch=branch or None,
                commit_sha=commit_sha or None,
                status=status,
                profile_kind=profile_kind or None,
                file_count=int(file_count or 0),
                error_message=(error_message or "")[:2000] or None,
                index_mode=index.get("mode") if index else None,
                index_ms=index.get("elapsed_ms") if index else None,
                route_count=route_count,
                type_count=type_count,
            )
        )


def set_survey_codegraph_stats(run_uid: str, stats: dict) -> None:
    """写入（覆盖）一次运行的 codegraph 快照，见 survey.clues.summarize_codegraph。"""
    from .models import SurveyRun

    with session_scope() as session:
        run = session.scalar(select(SurveyRun).where(SurveyRun.run_uid == run_uid))
        if run is None:
            # 运行记录被并发清理掉了；快照只是观测数据，记一笔便于排查"为什么这轮没有 codegraph 记录"
            logger.warning("Survey run not found when recording codegraph stats: %s", run_uid)
            return
        run.codegraph_stats = json.dumps(stats, ensure_ascii=False)


def previous_survey_fingerprints(survey_id: int, before_run_id: int) -> set:
    """
    上一次 SurveyRun 产出的指纹集合，用于判定本轮哪些是新增。

    只看**紧邻的上一次**而不是历史全集：一条问题被修好、几周后又被引入，
    它对读报告的人来说就是新问题，不该因为半年前出现过就被标成"仍存在"。
    """
    from .models import SurveyFinding, SurveyRun

    with session_scope() as session:
        prev_run_id = session.scalar(
            select(SurveyRun.id)
            .where(SurveyRun.survey_id == survey_id, SurveyRun.id < before_run_id,
                   SurveyRun.status == RUN_SUCCEEDED)
            .order_by(SurveyRun.id.desc())
            .limit(1)
        )
        if prev_run_id is None:
            return set()
        rows = session.scalars(
            select(SurveyFinding.fingerprint).where(SurveyFinding.run_id == prev_run_id)
        ).all()
        return set(rows)


def record_survey_findings(run_uid: str, findings: List[dict]) -> Dict[str, int]:
    """
    批量落库巡检产出，并标注每条是新增还是仍存在。

    模型认出是某个已忽略问题的（item["ignore_id"]）照常入库并关联，报告与统计在读取时隐藏它们：
    丢弃的话，模型认错了也没人能发现 —— 一个新问题会被静默吞掉。关联只接受本巡检自己的已忽略问题。
    上一次运行里有、或台账里仍为"存在"的记为仍存在：复核按名额轮转，一个文件可能隔几轮才被看一次，
    只比对上一次的话，中间没轮到的那几轮会让老问题在再次被看到时变成"新增"。
    台账里是本轮未发现的仍算新增，与 previous_survey_fingerprints 的口径一致。
    必须在本轮更新台账之前调用，读到的才是上一轮结束时的台账。
    返回 {"new": n, "persisted": m, "ignored": k}，ignored 是关联了已忽略问题的条数，不计入前两项。
    """
    from .models import (
        FINDING_STATE_NEW,
        FINDING_STATE_PERSISTED,
        LEDGER_PRESENT,
        SurveyFinding,
        SurveyIgnore,
        SurveyLedgerEntry,
        SurveyRun,
    )

    counters = {"new": 0, "persisted": 0, "ignored": 0}
    if not findings:
        return counters

    with session_scope() as session:
        run = session.scalar(select(SurveyRun).where(SurveyRun.run_uid == run_uid))
        if run is None:
            return counters
        survey_id = run.survey_id
        own_ignores = set(
            session.scalars(select(SurveyIgnore.id).where(SurveyIgnore.survey_id == survey_id)).all()
        )
        still_present = set(
            session.scalars(
                select(SurveyLedgerEntry.fingerprint).where(
                    SurveyLedgerEntry.survey_id == survey_id, SurveyLedgerEntry.state == LEDGER_PRESENT
                )
            ).all()
        )
        run_id = run.id

    previous = previous_survey_fingerprints(survey_id, run_id) | still_present

    with session_scope() as session:
        for item in findings:
            fingerprint = item["fingerprint"]
            ignore_id = item.get("ignore_id")
            ignore_id = ignore_id if ignore_id in own_ignores else None
            state = FINDING_STATE_PERSISTED if fingerprint in previous else FINDING_STATE_NEW
            if ignore_id:
                counters["ignored"] += 1
            else:
                counters["new" if state == FINDING_STATE_NEW else "persisted"] += 1
            session.add(
                SurveyFinding(
                    run_id=run_id,
                    survey_id=survey_id,
                    repo_slug=item.get("repo_slug", "")[:128],
                    file_path=(item.get("file_path") or "")[:1024] or None,
                    line=max(int(item.get("line") or 0), 0),
                    category=item["category"],
                    severity=item.get("severity") or SEVERITY_UNKNOWN,
                    title=(item.get("title") or "")[:512] or None,
                    body=(item.get("body") or "")[:8000] or None,
                    fingerprint=fingerprint,
                    state=state,
                    clue_source=item.get("clue_source") or None,
                    ignore_id=ignore_id,
                )
            )
    return counters


def _file_key(item: dict) -> tuple:
    """取证记录里的文件身份 (repo_slug, file_path)。"""
    return (str(item.get("repo_slug") or ""), str(item.get("file_path") or ""))


def record_survey_inspected_files(run_uid: str, files: List[dict]) -> None:
    """
    记下本轮交给 L2 取证过的文件（每项 {"repo_slug", "file_path", "conclusive"}），整体覆盖写入。

    同一文件出现多次时合并，有一次得出结论即算有结论。
    """
    from .models import SurveyRun

    merged: Dict[tuple, bool] = {}
    for item in files or []:
        key = _file_key(item)
        if not all(key):
            continue
        merged[key] = merged.get(key, False) or bool(item.get("conclusive"))
    items = [{"repo_slug": k[0], "file_path": k[1], "conclusive": v} for k, v in merged.items()]

    with session_scope() as session:
        session.execute(
            update(SurveyRun)
            # 已确认中断的运行不再写：取证清单只在成功的运行上被台账读取，写进去也只会改写已关闭的记录
            .where(SurveyRun.run_uid == run_uid, SurveyRun.status == RUN_RUNNING)
            .values(inspected_files=json.dumps(items, ensure_ascii=False), heartbeat_at=utcnow())
        )


def _parse_inspected_files(raw: Optional[str], conclusive_only: bool = True) -> Optional[set]:
    """
    把 inspected_files 解析成 {(repo_slug, file_path)}；conclusive_only 为 False 时包含没得出结论的文件。

    NULL 返回 None（早于该字段的运行），与"一个文件都没看"的空集区分开。
    内容损坏时返回空集：宁可这一轮什么都不判，也不要按旧口径把没看过的文件判成没出现。
    """
    if raw is None:
        return None
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return set()
    if not isinstance(items, list):
        return set()
    return {
        _file_key(i) for i in items
        if isinstance(i, dict) and (i.get("conclusive") or not conclusive_only)
    }


def record_survey_repo_reach(run_uid: str, repo_slug: str, reach: dict) -> None:
    """把一个仓库的 Reach 统计写到本轮的仓库记录上。仓库记录不存在时什么也不做。"""
    from .models import SurveyRun, SurveyRunRepo

    with session_scope() as session:
        run_id = session.scalar(select(SurveyRun.id).where(SurveyRun.run_uid == run_uid))
        if run_id is None:
            return
        session.execute(
            update(SurveyRunRepo)
            .where(SurveyRunRepo.run_id == run_id, SurveyRunRepo.repo_slug == repo_slug)
            .values(reach=json.dumps(reach, ensure_ascii=False))
        )


# Reach 里列出具体文件与目录的字段。它们描述的是代码库的结构，与发现正文同一档待遇，
# Guest 只拿得到计数（见 docs/adr/0002-guest-read-scope.md）
_REACH_DETAIL_KEYS = ("hidden_dirs", "heavy_hidden_files")


def _reach_to_dict(raw: Optional[str], include_body: bool) -> Optional[dict]:
    """解析 Reach 统计；为空或损坏时返回 None，界面显示为"无记录"而不是让详情页打不开。"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not include_body:
        for key in _REACH_DETAIL_KEYS:
            data.pop(key, None)
    return data


def finish_survey_run(
    run_uid: str,
    status: str,
    summary: str = "",
    error_kind: str = "",
    error_message: str = "",
) -> bool:
    """
    收尾一次 SurveyRun，返回是否由本次调用收尾。

    与 finish_run 同理只收尾仍在进行中的运行。返回 False 时调用方不能再更新 Ledger、
    不能推送：被确认中断后，同一 Survey 可能已经有更新的一轮写过台账，迟到的旧结果会把它盖掉。
    """
    from .models import SURVEY_PHASE_DONE, SurveyRun

    now = utcnow()
    values: Dict[str, object] = {
        "status": status,
        "phase": SURVEY_PHASE_DONE,
        "heartbeat_at": now,
        "finished_at": now,
    }
    if summary:
        values["summary"] = summary
    if error_kind:
        values["error_kind"] = error_kind
    if error_message:
        values["error_message"] = str(error_message)[:4000]

    with session_scope() as session:
        result = session.execute(
            update(SurveyRun)
            .where(SurveyRun.run_uid == run_uid, SurveyRun.status == RUN_RUNNING)
            .values(**values)
        )
        return result.rowcount > 0


def confirm_survey_run_interrupted(run_uid: str, stale_after_seconds: int) -> str:
    """确认一个 Stale 的 SurveyRun 已中断。返回值见 ``_confirm_interrupted``。"""
    from .models import SurveyRun

    return _confirm_interrupted(SurveyRun, run_uid, stale_after_seconds)


def _survey_run_to_dict(run: "SurveyRun", stale_after_seconds: int = 600) -> dict:
    """把 SurveyRun 转成接口数据。JSON 字段解析失败退化为空列表，不让面板打不开。"""
    try:
        degradations = json.loads(run.degradations) if run.degradations else []
    except (ValueError, TypeError):
        degradations = []
    try:
        skills = json.loads(run.matched_skills) if run.matched_skills else []
    except (ValueError, TypeError):
        skills = []
    try:
        # 功能上线前的旧运行没有快照，返回 None 让界面显示"无记录"而不是一排 0
        codegraph_stats = json.loads(run.codegraph_stats) if run.codegraph_stats else None
    except (ValueError, TypeError):
        codegraph_stats = None

    is_stale = _is_stale(run, stale_after_seconds)
    return {
        "run_uid": run.run_uid,
        "trigger": run.trigger,
        "status": run.status,
        "phase": run.phase or "",
        "repos_total": run.repos_total,
        "repos_done": run.repos_done,
        "matched_skills": skills,
        "degradations": degradations,
        "error_kind": run.error_kind or "",
        "error_message": run.error_message or "",
        "started_at": run.started_at.isoformat() if run.started_at else "",
        "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else "",
        "finished_at": run.finished_at.isoformat() if run.finished_at else "",
        "is_stale": is_stale,
        "codegraph_stats": codegraph_stats,
    }


def _survey_finding_to_dict(finding: "SurveyFinding", include_body: bool) -> dict:
    """
    巡检发现的接口数据。

    include_body=False 时不返回正文与标题：巡检 Finding 描述的是整个代码库的
    架构与弱点，正文密度比 MR 审查更高，Guest 边界只会更严格，不会更松。
    """
    item = {
        "id": finding.id,
        "repo_slug": finding.repo_slug,
        "file_path": finding.file_path or "",
        "line": finding.line,
        "category": finding.category,
        "severity": finding.severity,
        "state": finding.state,
        "fingerprint": finding.fingerprint,
        # 线索来源是标签不是正文，Guest 同样可见；旧数据为空串
        "clue_source": finding.clue_source or "",
        "created_at": finding.created_at.isoformat() if finding.created_at else "",
    }
    if include_body:
        item["title"] = finding.title or ""
        item["body"] = finding.body or ""
    return item


def _survey_finding_not_ignored():
    """
    SurveyFinding 的查询条件：没有被已忽略问题隐藏。

    被忽略的发现不删除，只在读取时剔除（取消忽略即恢复）；
    按条数汇总 SurveyFinding 给人看的地方都要带上它，否则数字会和报告列表对不上。
    """
    from .models import SurveyFinding

    return SurveyFinding.ignore_id.is_(None)


def _survey_clue_counts(session, run_ids: List[int]) -> Dict[int, Dict[str, int]]:
    """
    按运行统计已落库 Finding 的线索来源分布：{run_id: {CLUE_SOURCES 的每个键, unknown}}。

    从 survey_finding 现算而不是读快照：被忽略的条目不入库，快照里的数字会比列表多。
    同理，入库之后才被忽略的条目也不计入 —— 报告列表在读取时把它们藏掉了。
    unknown 是功能上线前的旧数据，单列出来，不并进任何一类。
    """
    from .models import CLUE_SOURCES, CLUE_UNKNOWN, SurveyFinding

    result: Dict[int, Dict[str, int]] = {
        run_id: {**{source: 0 for source in CLUE_SOURCES}, CLUE_UNKNOWN: 0} for run_id in run_ids
    }
    if not run_ids:
        return result
    rows = session.execute(
        select(SurveyFinding.run_id, SurveyFinding.clue_source, func.count())
        .where(SurveyFinding.run_id.in_(run_ids), _survey_finding_not_ignored())
        .group_by(SurveyFinding.run_id, SurveyFinding.clue_source)
    ).all()
    for run_id, source, count in rows:
        key = source if source in CLUE_SOURCES else CLUE_UNKNOWN
        result[run_id][key] += int(count)
    return result


def list_survey_runs(survey_uid: str = "", limit: int = 50, stale_after_seconds: int = 600) -> List[dict]:
    """巡检运行列表，按开始时间倒序。survey_uid 为空表示不限巡检。"""
    from .models import Survey, SurveyRun

    with session_scope() as session:
        stmt = select(SurveyRun).order_by(SurveyRun.started_at.desc()).limit(max(int(limit), 1))
        if survey_uid:
            survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
            if survey_id is None:
                return []
            stmt = stmt.where(SurveyRun.survey_id == survey_id)
        runs = session.scalars(stmt).all()
        survey_names = dict(session.execute(select(Survey.id, Survey.name)).all())
        clue_counts = _survey_clue_counts(session, [r.id for r in runs])
        return [
            {
                **_survey_run_to_dict(r, stale_after_seconds),
                "survey_name": survey_names.get(r.survey_id, ""),
                "clue_counts": clue_counts[r.id],
            }
            for r in runs
        ]


def get_survey_run_detail(
    run_uid: str, include_body: bool = True, stale_after_seconds: int = 600
) -> Optional[dict]:
    """
    单次 SurveyRun 的完整报告：仓库清单、逐条发现、以及与上一次相比的差异。

    "已消失"不从库里读 —— 它没有对应的行（见 models 里 FINDING_STATE 的注释），
    是拿上一次的指纹集减去本次算出来的。差集里落在本轮没取证过的文件上的，
    单独列为"本轮未复查"（unchecked_findings），不算已消失；其中所在仓库本轮没参与的
    （超出上限、拉取失败）带上 repo_status 说明原因。
    所在仓库本轮因为在忽略清单里而跳过的，再单独列为 ignored_repo_findings：它们不是没轮到检查，
    而是用户说过不再看这个仓库，混在"本轮未复查"里会虚增它的条数，也会让人以为该去催一次复查。

    被已忽略问题隐藏的发现不出现在任何一栏，也不计入 counts；隐藏的条数单独给出（counts.ignored）。
    """
    from .models import Survey, SurveyFinding, SurveyRun, SurveyRunRepo

    with session_scope() as session:
        run = session.scalar(select(SurveyRun).where(SurveyRun.run_uid == run_uid))
        if run is None:
            return None
        survey = session.get(Survey, run.survey_id)
        # 被忽略的发现在读取时过滤，而不是删掉：删掉就回不来了，取消忽略后历史报告应当原样恢复；
        # 也不改写 state，它记录的是当时的比对结论
        all_findings = session.scalars(
            select(SurveyFinding).where(SurveyFinding.run_id == run.id).order_by(SurveyFinding.id.asc())
        ).all()
        findings = [f for f in all_findings if f.ignore_id is None]
        repos = session.scalars(
            select(SurveyRunRepo).where(SurveyRunRepo.run_id == run.id).order_by(SurveyRunRepo.id.asc())
        ).all()

        detail = _survey_run_to_dict(run, stale_after_seconds)
        detail["survey_uid"] = survey.survey_uid if survey else ""
        detail["survey_name"] = survey.name if survey else ""
        # 时间字段是 naive UTC，导出报告要按巡检自己的时区换算后才给人看
        detail["survey_timezone"] = (survey.timezone if survey else "") or "UTC"
        # 整合叙述也是模型对私有代码的描述，与正文同一档待遇
        detail["summary"] = (run.summary or "") if include_body else ""
        detail["body_included"] = include_body
        detail["repos"] = [
            {
                "repo_slug": r.repo_slug,
                "url": r.url,
                "branch": r.branch or "",
                "commit_sha": (r.commit_sha or "")[:12],
                "status": r.status,
                "profile_kind": r.profile_kind or "",
                "file_count": r.file_count,
                # 拉取与建索引的原始报错（git / codegraph 的输出）可能带出路径与代码片段，
                # 与推送记录的错误信息同等对待：Guest 只看状态，不看原文
                "error_message": (r.error_message or "") if include_body else "",
                "reach": _reach_to_dict(r.reach, include_body),
                "index_mode": r.index_mode or "",
                "index_ms": r.index_ms,
                "route_count": r.route_count,
                "type_count": r.type_count,
            }
            for r in repos
        ]
        detail["findings"] = [_survey_finding_to_dict(f, include_body) for f in findings]
        detail["clue_counts"] = _survey_clue_counts(session, [run.id])[run.id]

        # 用全部发现而不是可见的：这一轮被忽略隐藏的指纹仍然出现了，上一轮同指纹的条目不能借"已消失"冒出来
        current_fps = {f.fingerprint for f in all_findings}
        prev_run_id = session.scalar(
            select(SurveyRun.id)
            .where(SurveyRun.survey_id == run.survey_id, SurveyRun.id < run.id,
                   SurveyRun.status == RUN_SUCCEEDED)
            .order_by(SurveyRun.id.desc())
            .limit(1)
        )
        # None 是早于该字段的运行：没有取证记录可查，只能沿用朴素差集
        inspected = _parse_inspected_files(run.inspected_files)
        resolved = []
        unchecked = []
        ignored_repo = []
        # 取本轮的仓库状态快照而不是当前的忽略清单：报告说的是这一轮发生了什么，之后取消忽略不该改写它
        repo_status = {r.repo_slug: r.status for r in repos}
        if prev_run_id is not None:
            prev_findings = session.scalars(
                select(SurveyFinding).where(SurveyFinding.run_id == prev_run_id)
            ).all()
            for f in prev_findings:
                if f.fingerprint in current_fps or f.ignore_id is not None:
                    continue
                item = _survey_finding_to_dict(f, include_body)
                status = repo_status.get(f.repo_slug, "")
                if status == REPO_IGNORED:
                    ignored_repo.append(item)
                elif inspected is None or (f.repo_slug, f.file_path or "") in inspected:
                    resolved.append(item)
                else:
                    # 只标"整个仓库没参与"的两种状态，与 survey/report.py 的 UNCHECKED_REPO_LABELS 对应
                    # （storage 不反向依赖 survey 层，所以在这里各列一遍）
                    item["repo_status"] = status if status in (REPO_TRUNCATED, REPO_FETCH_FAILED) else ""
                    unchecked.append(item)
        detail["resolved_findings"] = resolved
        detail["unchecked_findings"] = unchecked
        detail["ignored_repo_findings"] = ignored_repo
        detail["counts"] = {
            "new": sum(1 for f in findings if f.state == "new"),
            "persisted": sum(1 for f in findings if f.state == "persisted"),
            "resolved": len(resolved),
            "unchecked": len(unchecked),
            "ignored_repo": len(ignored_repo),
            "total": len(findings),
            "ignored": len(all_findings) - len(findings),
        }
        return detail


# 忽略理由的长度上限。前端校验用同一个数（web/src/common/constants/opencr.ts 的 IGNORE_NOTE_MAX）
SURVEY_IGNORE_NOTE_MAX = 2000


def _clean_ignore_note(note: str) -> Optional[str]:
    """规整忽略理由：去掉首尾空白并截断；空理由存 NULL，界面上才会显示"未填写"而不是一段空白。"""
    return (note or "").strip()[:SURVEY_IGNORE_NOTE_MAX] or None


def _ignore_is_active(title) -> bool:
    """
    已忽略问题是否生效：有标题才能交给模型比对。

    没有标题的是旧版本迁移过来、找不到原问题的条目（见迁移 b7e4c1a9d2f0），它不再参与匹配，
    只留在忽略清单里等管理员删除。巡检链路与管理页都按这一个判定，改条件时只改这里。
    """
    return bool((title or "").strip())


def _ignore_snapshot_title(finding: "SurveyFinding") -> str:
    """
    已忽略问题的快照标题：发现的标题，空时退回正文第一行，再不行用位置。

    模型偶尔给出空标题。快照标题是交给模型比对的依据，空着的话这条忽略从创建起就不生效（见 _ignore_is_active）。
    """
    title = (finding.title or "").strip()
    if title:
        return title
    body = (finding.body or "").strip()
    if body:
        return body.splitlines()[0][:120]
    return f"{finding.file_path or ''} 第 {finding.line} 行的问题"


def add_survey_ignore(survey_uid: str, finding_id: int, note: str = "") -> Optional[int]:
    """
    把一条发现标记为已忽略问题（"已知问题、不再提醒"），返回已忽略问题的 id；巡检或发现不存在时返回 None。

    忽略的是这一个问题，不是它的指纹（ADR-0006）。发现已关联过忽略、或同一巡检下已有同指纹同标题的忽略时复用它，
    并把保留的运行里同指纹、标题规整后相同的发现一起关联 —— 它们是同一个问题被原样报了几遍，
    在旧报告里点一次就该在各轮报告里都隐藏；换了说法的不在此列，要等之后的运行由模型认出。
    """
    from ..survey.common import normalize_title
    from .models import LEDGER_IGNORED, Survey, SurveyFinding, SurveyIgnore, SurveyLedgerEntry, SurveyRun

    note = _clean_ignore_note(note)
    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return None
        finding = session.get(SurveyFinding, int(finding_id))
        if finding is None or finding.survey_id != survey_id:
            return None

        title_key = normalize_title(finding.title)
        ignore = session.get(SurveyIgnore, finding.ignore_id) if finding.ignore_id else None
        if ignore is None and title_key:
            ignore = next(
                (
                    candidate for candidate in session.scalars(
                        select(SurveyIgnore).where(
                            SurveyIgnore.survey_id == survey_id, SurveyIgnore.fingerprint == finding.fingerprint
                        )
                    ).all()
                    if normalize_title(candidate.title) == title_key
                ),
                None,
            )
        if ignore is None:
            ignore = SurveyIgnore(
                survey_id=survey_id,
                fingerprint=finding.fingerprint,
                repo_slug=finding.repo_slug,
                file_path=finding.file_path,
                category=finding.category,
                line=finding.line,
                severity=finding.severity,
                title=_ignore_snapshot_title(finding),
                body=finding.body,
                note=note,
            )
            session.add(ignore)
            session.flush()
        elif note and not ignore.note:
            # 再次标记时补上此前没填的理由；已有理由则不覆盖 ——
            # 改理由走编辑接口，这里静默覆盖会丢掉别人写的说明
            ignore.note = note

        finding.ignore_id = ignore.id
        if title_key:
            for item in session.scalars(
                select(SurveyFinding).where(
                    SurveyFinding.survey_id == survey_id,
                    SurveyFinding.fingerprint == finding.fingerprint,
                    SurveyFinding.ignore_id.is_(None),
                )
            ).all():
                if normalize_title(item.title) == title_key:
                    item.ignore_id = ignore.id
        session.flush()

        # 台账一行是一个指纹，可能合并了好几个问题，按它最近一次出现的那轮判断：
        # 那一轮里该指纹下的问题全部被忽略了才立刻转为已忽略，还剩别的问题时它仍然存在
        entry = session.scalar(
            select(SurveyLedgerEntry).where(
                SurveyLedgerEntry.survey_id == survey_id, SurveyLedgerEntry.fingerprint == finding.fingerprint
            )
        )
        last_run_id = session.scalar(
            select(SurveyRun.id).where(SurveyRun.run_uid == entry.last_run_uid)
        ) if entry is not None and entry.last_run_uid else None
        if last_run_id is not None:
            remaining = session.scalar(
                select(func.count()).select_from(SurveyFinding).where(
                    SurveyFinding.run_id == last_run_id,
                    SurveyFinding.fingerprint == finding.fingerprint,
                    SurveyFinding.ignore_id.is_(None),
                )
            )
            if not remaining:
                entry.state = LEDGER_IGNORED
                entry.updated_at = utcnow()
        return ignore.id


def remove_survey_ignore(survey_uid: str, ignore_id: int) -> bool:
    """
    取消忽略：解除它与发现的关联并删除记录，被它隐藏的历史发现恢复显示。

    台账状态不在这里改回：该行会停在已忽略，直到下一轮出现（变回存在）
    或下一轮结论可信地没出现（变为本轮未发现）。凭空把它改成"存在"是没有证据的判断。
    """
    from .models import Survey, SurveyFinding, SurveyIgnore

    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        ignore = session.get(SurveyIgnore, int(ignore_id))
        if survey_id is None or ignore is None or ignore.survey_id != survey_id:
            return False
        # 不依赖外键的 SET NULL：SQLite 只有在连接上打开 foreign_keys 时才执行它
        session.execute(update(SurveyFinding).where(SurveyFinding.ignore_id == ignore.id).values(ignore_id=None))
        session.delete(ignore)
        return True


def update_survey_ignore_note(survey_uid: str, ignore_id: int, note: str) -> bool:
    """修改忽略理由。巡检或已忽略问题不存在时返回 False。"""
    from .models import Survey, SurveyIgnore

    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return False
        result = session.execute(
            update(SurveyIgnore)
            .where(SurveyIgnore.survey_id == survey_id, SurveyIgnore.id == int(ignore_id))
            .values(note=_clean_ignore_note(note))
        )
        return (result.rowcount or 0) > 0


def list_survey_active_ignores(survey_uid: str) -> List[dict]:
    """
    某个巡检里生效的已忽略问题（见 _ignore_is_active），供巡检链路分发到各文件的取证上。

    只取快照字段：巡检链路不需要理由与命中记录，管理页那份联查对它是白付的开销。
    """
    from .models import Survey, SurveyIgnore

    with session_scope() as session:
        rows = session.scalars(
            select(SurveyIgnore)
            .join(Survey, Survey.id == SurveyIgnore.survey_id)
            .where(Survey.survey_uid == survey_uid)
        ).all()
        return [
            {
                "id": r.id,
                "fingerprint": r.fingerprint,
                "repo_slug": r.repo_slug,
                "file_path": r.file_path or "",
                "category": r.category,
                "line": r.line,
                "title": r.title or "",
                "body": r.body or "",
            }
            for r in rows
            if _ignore_is_active(r.title)
        ]


def list_survey_ignores(survey_uid: str) -> List[dict]:
    """
    某个巡检的忽略清单：每条是一个已忽略问题，附带标记时的快照与最近一次命中。

    最近一次命中 = 关联到它的最新一条发现（标记时被点的那条，或之后的运行里模型认出的同一个问题）。
    把它摆出来是为了让人核对模型有没有认错：认错的后果是一个新问题被静默隐藏。
    运行被清理后发现跟着删除，命中记录随之消失，只剩快照。
    """
    from .models import Survey, SurveyFinding, SurveyIgnore, SurveyRun

    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return []
        rows = session.scalars(
            select(SurveyIgnore).where(SurveyIgnore.survey_id == survey_id).order_by(SurveyIgnore.id.desc())
        ).all()
        latest: Dict[int, tuple] = {}
        hits: Dict[int, int] = {}
        if rows:
            # 按 id 升序遍历、后者覆盖前者，得到每条忽略最近的一次命中
            for finding, run_uid in session.execute(
                select(SurveyFinding, SurveyRun.run_uid)
                .join(SurveyRun, SurveyRun.id == SurveyFinding.run_id)
                .where(SurveyFinding.ignore_id.in_([r.id for r in rows]))
                .order_by(SurveyFinding.id)
            ).all():
                latest[finding.ignore_id] = (finding, run_uid)
                hits[finding.ignore_id] = hits.get(finding.ignore_id, 0) + 1

        items = []
        for r in rows:
            match = latest.get(r.id)
            items.append({
                "id": r.id,
                "fingerprint": r.fingerprint,
                "repo_slug": r.repo_slug,
                "file_path": r.file_path or "",
                "category": r.category,
                "line": r.line,
                "severity": r.severity,
                "title": r.title or "",
                "note": r.note or "",
                "created_at": r.created_at.isoformat() if r.created_at else "",
                "active": _ignore_is_active(r.title),
                # 被隐藏的发现条数（仍保留的运行里）
                "hidden_count": hits.get(r.id, 0),
                "last_match": {
                    "run_uid": match[1],
                    "line": match[0].line,
                    "title": match[0].title or "",
                    "created_at": match[0].created_at.isoformat() if match[0].created_at else "",
                } if match else None,
            })
        return items


def list_survey_ignored_repo_slugs(survey_uid: str) -> Set[str]:
    """某个巡检的已忽略仓库的 repo_slug 集合，供来源展开时剔除。"""
    from .models import Survey, SurveyIgnoredRepo

    with session_scope() as session:
        return set(session.scalars(
            select(SurveyIgnoredRepo.repo_slug)
            .join(Survey, Survey.id == SurveyIgnoredRepo.survey_id)
            .where(Survey.survey_uid == survey_uid)
        ).all())


def list_survey_ignored_repos(survey_uid: str) -> List[dict]:
    """某个巡检的已忽略仓库，最近标记的在前；巡检不存在时返回空列表。"""
    from .models import Survey, SurveyIgnoredRepo

    with session_scope() as session:
        rows = session.scalars(
            select(SurveyIgnoredRepo)
            .join(Survey, Survey.id == SurveyIgnoredRepo.survey_id)
            .where(Survey.survey_uid == survey_uid)
            .order_by(SurveyIgnoredRepo.id.desc())
        ).all()
        return [
            {
                "id": r.id,
                "repo_slug": r.repo_slug,
                "url": r.url,
                "note": r.note or "",
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ]


def list_survey_repo_candidates(survey_uid: str) -> dict:
    """
    可供标记为已忽略的仓库：最近一次记录了仓库清单的运行里出现的仓库，去掉已经忽略的。

    返回 {"items": [{"repo_slug", "url"}], "run_uid", "run_started_at"}，后两项说明清单来自哪一轮，
    界面据此提示它可能过时；还没运行过的巡检 items 为空、后两项为空串。
    默认取运行记录而不是实时展开组织：打开管理页不该触发一串 GitLab 请求，而上一轮展开的清单
    就是用户在报告里看到、想要剔除的那一批。代价是配置或组织变了之后清单会过时，
    所以另有手动刷新（survey.sources.live_repo_candidates）按当前配置实时展开。
    """
    from .models import Survey, SurveyIgnoredRepo, SurveyRun, SurveyRunRepo

    empty = {"items": [], "run_uid": "", "run_started_at": ""}
    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return empty
        latest_run_id = session.scalar(
            select(func.max(SurveyRunRepo.run_id))
            .join(SurveyRun, SurveyRun.id == SurveyRunRepo.run_id)
            .where(SurveyRun.survey_id == survey_id)
        )
        if latest_run_id is None:
            return empty
        run = session.get(SurveyRun, latest_run_id)
        ignored = set(session.scalars(
            select(SurveyIgnoredRepo.repo_slug).where(SurveyIgnoredRepo.survey_id == survey_id)
        ).all())
        rows = session.scalars(
            select(SurveyRunRepo).where(SurveyRunRepo.run_id == latest_run_id).order_by(SurveyRunRepo.repo_slug)
        ).all()
        return {
            "items": [
                {"repo_slug": r.repo_slug, "url": r.url}
                for r in rows
                # 组织展开失败的行以组织地址充当 repo_slug（见 runner._prepare_workspaces），它不是仓库：
                # 选中它会存下一个永远命中不了的 slug。拉取失败的真实仓库照常提供，它们正是常被剔除的那类
                if r.repo_slug not in ignored and r.repo_slug != r.url
            ],
            "run_uid": run.run_uid,
            "run_started_at": run.started_at.isoformat() if run.started_at else "",
        }


def add_survey_ignored_repos(survey_uid: str, urls: List[str], note: str = "") -> Optional[List[dict]]:
    """
    把一批仓库标记为已忽略仓库（"不再巡检"），共用一条理由；巡检不存在时返回 None。

    返回每个仓库一项 {"id", "repo_slug", "url", "created"}，按输入顺序、同一仓库只出现一次；
    created 为 False 表示它原本就在清单里。
    url 可以是仓库地址、浏览器里的页面地址，也可以是"组织/仓库"路径，按 parse_repo_reference 换算成仓库身份。
    任何一个不合法时抛 ValueError，列出全部不合法的输入，整批都不写入。
    同一仓库已在清单里时复用原记录，只补上此前没填的理由，与已忽略问题的重复标记口径一致。
    台账里这些仓库仍存在的行立即转为已忽略：之后的运行都不会再拉取它们，不在这里改的话，
    这些行要等到下一轮跑完才变，期间手动重推出去的镜像仍然显示"存在"。这里不主动推送，与已忽略问题一致。
    """
    from ..survey.common import MAX_REPOS_UPPER_BOUND, parse_repo_reference
    from .models import LEDGER_IGNORED, LEDGER_PRESENT, Survey, SurveyIgnoredRepo, SurveyLedgerEntry

    raw = [str(u or "").strip() for u in urls or [] if str(u or "").strip()]
    if not raw:
        raise ValueError("仓库地址不能为空")
    if len(raw) > MAX_REPOS_UPPER_BOUND:
        # 与单次巡检仓库上限的最大值一致：超出它的批量只可能是误操作，一次写这么多行也会长时间占着 SQLite 写锁
        raise ValueError(f"一次最多添加 {MAX_REPOS_UPPER_BOUND} 个仓库")

    # 先全部校验再写入：部分成功的话，管理员很难分清哪些加上了、哪些没有
    parsed: Dict[str, str] = {}
    invalid: List[str] = []
    for entry in raw:
        try:
            url, slug = parse_repo_reference(entry)
        except ValueError:
            invalid.append(entry)
            continue
        # 同一批里换了写法的同一个仓库只算一次，保留第一次出现的写法
        parsed.setdefault(slug, url)
    if invalid:
        # 一批最多上千条，全列出来提示框装不下；列前 10 个与总数，够管理员定位是哪一类写法出了问题
        shown = "、".join(invalid[:10]) + (f" 等 {len(invalid)} 个" if len(invalid) > 10 else "")
        raise ValueError(f"以下地址无法识别为仓库（请填写仓库地址或「组织/仓库」路径）：{shown}")

    note = _clean_ignore_note(note)
    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return None
        # 一个巡检的忽略清单很小，整份读出来比拼一个最多上千项的 IN 更省事
        existing = {
            item.repo_slug: item
            for item in session.scalars(
                select(SurveyIgnoredRepo).where(SurveyIgnoredRepo.survey_id == survey_id)
            ).all()
        }
        results = []
        for slug, url in parsed.items():
            item = existing.get(slug)
            created = item is None
            if created:
                item = SurveyIgnoredRepo(survey_id=survey_id, repo_slug=slug, url=url[:1024], note=note)
                session.add(item)
            elif note and not item.note:
                item.note = note
            results.append((item, created))
        session.flush()

        slugs = list(parsed)
        # 分段：旧版 SQLite 单条语句默认最多 999 个参数，一批最多有 1000 个仓库
        for offset in range(0, len(slugs), 500):
            session.execute(
                update(SurveyLedgerEntry)
                .where(
                    SurveyLedgerEntry.survey_id == survey_id,
                    SurveyLedgerEntry.repo_slug.in_(slugs[offset:offset + 500]),
                    # 只转仍存在的行，与 plan_ledger_update 口径一致（理由见那里）
                    SurveyLedgerEntry.state == LEDGER_PRESENT,
                )
                .values(state=LEDGER_IGNORED, updated_at=utcnow())
            )
        return [
            {"id": item.id, "repo_slug": item.repo_slug, "url": item.url, "created": created}
            for item, created in results
        ]


def remove_survey_ignored_repo(survey_uid: str, item_id: int) -> bool:
    """
    取消忽略一个仓库，下一轮起它重新参与巡检。

    台账状态不在这里改回，理由同 remove_survey_ignore：这个仓库很久没被看过，
    凭空把它的行改回"存在"是没有证据的判断；下一轮复核取证后自然会得出结论。
    """
    from .models import Survey, SurveyIgnoredRepo

    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        item = session.get(SurveyIgnoredRepo, int(item_id))
        if survey_id is None or item is None or item.survey_id != survey_id:
            return False
        session.delete(item)
        return True


def update_survey_ignored_repo_note(survey_uid: str, item_id: int, note: str) -> bool:
    """修改已忽略仓库的理由。巡检或记录不存在时返回 False。"""
    from .models import Survey, SurveyIgnoredRepo

    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return False
        result = session.execute(
            update(SurveyIgnoredRepo)
            .where(SurveyIgnoredRepo.survey_id == survey_id, SurveyIgnoredRepo.id == int(item_id))
            .values(note=_clean_ignore_note(note))
        )
        return (result.rowcount or 0) > 0


def purge_survey_runs(survey_id: int, retention_runs: int) -> int:
    """
    按次数清理巡检运行记录，返回删除条数。

    **永远保留最近一次**，哪怕 retention_runs 被设成 0 或 1 ——
    "新增/仍存在"的比对依赖上一次的记录还在，删掉它之后下一轮报告会把
    所有问题都标成新增。这类故障发生在某个凌晨，且看起来完全正常。
    """
    from .models import SurveyFinding, SurveyPush, SurveyRun

    keep = max(int(retention_runs or 1), 1)
    with session_scope() as session:
        run_ids = session.scalars(
            select(SurveyRun.id)
            .where(SurveyRun.survey_id == int(survey_id))
            .order_by(SurveyRun.started_at.desc())
        ).all()
        stale_ids = list(run_ids[keep:])
        if not stale_ids:
            return 0
        session.execute(delete(SurveyFinding).where(SurveyFinding.run_id.in_(stale_ids)))
        session.execute(delete(SurveyPush).where(SurveyPush.run_id.in_(stale_ids)))
        session.execute(delete(SurveyRun).where(SurveyRun.id.in_(stale_ids)))
        return len(stale_ids)


def purge_all_survey_runs() -> int:
    """对所有巡检按各自的 retention_runs 做一次清理，返回总删除条数。"""
    from .models import Survey

    with session_scope() as session:
        pairs = session.execute(select(Survey.id, Survey.retention_runs)).all()
    return sum(purge_survey_runs(sid, keep) for sid, keep in pairs)


def survey_dashboard_stats(days: int = 90) -> dict:
    """
    巡检的聚合统计。不含任何正文，Guest 开关打开时可直接返回。

    发现的三项分布不计当前忽略清单里的条目，与报告列表口径一致；运行数与忽略无关，照常计。
    """
    from .models import SurveyFinding, SurveyRun

    since = _window_start(days)
    visible = (SurveyFinding.created_at >= since, _survey_finding_not_ignored())
    with session_scope() as session:
        by_status = dict(
            session.execute(
                select(SurveyRun.status, func.count())
                .where(SurveyRun.started_at >= since)
                .group_by(SurveyRun.status)
            ).all()
        )
        by_category = dict(
            session.execute(
                select(SurveyFinding.category, func.count())
                .where(*visible)
                .group_by(SurveyFinding.category)
            ).all()
        )
        by_severity = dict(
            session.execute(
                select(SurveyFinding.severity, func.count())
                .where(*visible)
                .group_by(SurveyFinding.severity)
            ).all()
        )
        by_state = dict(
            session.execute(
                select(SurveyFinding.state, func.count())
                .where(*visible)
                .group_by(SurveyFinding.state)
            ).all()
        )
    return {
        "window_days": days,
        "total_runs": sum(by_status.values()),
        "succeeded": by_status.get(RUN_SUCCEEDED, 0),
        "failed": by_status.get(RUN_FAILED, 0),
        "running": by_status.get(RUN_RUNNING, 0),
        "by_category": by_category,
        "by_severity": by_severity,
        "by_state": by_state,
    }


def purge_survey_runs_by_uid(survey_uid: str) -> int:
    """按 uid 对单个巡检做保留清理，供执行收尾调用。"""
    from .models import Survey

    with session_scope() as session:
        row = session.execute(
            select(Survey.id, Survey.retention_runs).where(Survey.survey_uid == survey_uid)
        ).first()
    if row is None:
        return 0
    return purge_survey_runs(row[0], row[1])


# ==========================================================================
# 巡检输出：Ledger / Binding / Push
#
# Ledger 是系统持有的问题台账，Destination 上的表格只是镜像（ADR-0004）。
# 状态判定在 survey/ledger.py 里是纯函数，这里只做存取。
# ==========================================================================

# 一条推送记录停在进行中超过这个时长，就不再阻止同一 Binding 发起新的推送。
# 推送没有心跳：进程在推送中途被杀掉时记录会永远停在进行中，不设上限的话这个 Binding 就再也推不出去了。
# 取 15 分钟：几千行的台账在限流退避下也能在这个时间内推完。
PUSH_STALE_SECONDS = 900


def get_survey_run_ledger_inputs(run_uid: str) -> Optional[dict]:
    """
    更新 Ledger 所需的一次运行的全部输入：运行状态、逐条发现（含正文）、仓库处理结果、降级、
    本轮被忽略的指纹 ignored、本轮被跳过的已忽略仓库 ignored_repos，以及本轮得出结论的文件 inspected 与交给过 L2 的文件 attempted（都是 {(repo_slug, file_path)}）。

    findings 只含没被忽略的发现：台账一行聚合的是一个指纹下仍要跟进的问题，被忽略的不该进正文与条数。
    ignored 是本轮出现过、但被已忽略问题隐藏的发现的指纹 —— 这些指纹本轮"看到了、是已知问题"，
    台账据此标为已忽略，而不是本轮未发现。
    运行不存在时返回 None。
    """
    from .models import SurveyFinding, SurveyIgnoredRepo, SurveyRun, SurveyRunRepo

    with session_scope() as session:
        run = session.scalar(select(SurveyRun).where(SurveyRun.run_uid == run_uid))
        if run is None:
            return None
        findings = session.scalars(select(SurveyFinding).where(SurveyFinding.run_id == run.id)).all()
        repos = session.scalars(select(SurveyRunRepo).where(SurveyRunRepo.run_id == run.id)).all()
        ignored = {f.fingerprint for f in findings if f.ignore_id is not None}
        findings = [f for f in findings if f.ignore_id is None]
        return {
            "survey_id": run.survey_id,
            "status": run.status,
            "degradations": _survey_run_to_dict(run)["degradations"],
            "findings": [
                {
                    "fingerprint": f.fingerprint,
                    "repo_slug": f.repo_slug,
                    "file_path": f.file_path or "",
                    "line": f.line,
                    "category": f.category,
                    "severity": f.severity,
                    "title": f.title or "",
                    "body": f.body or "",
                }
                for f in findings
            ],
            "repos": [{"repo_slug": r.repo_slug, "status": r.status} for r in repos],
            "ignored": ignored,
            # 本轮被跳过的仓库，加上此刻在忽略清单里的：运行进行中才加入忽略的仓库已经被分析过了，
            # 只看本轮快照的话，这一轮收尾会把标记时刚转为已忽略的行又写回存在
            "ignored_repos": {r.repo_slug for r in repos if r.status == REPO_IGNORED} | set(session.scalars(
                select(SurveyIgnoredRepo.repo_slug).where(SurveyIgnoredRepo.survey_id == run.survey_id)
            ).all()),
            # 台账只为运行结束时更新一次，不会碰到早于该字段的运行；真遇到 NULL 也按"一个都没看"处理
            "inspected": _parse_inspected_files(run.inspected_files) or set(),
            "attempted": _parse_inspected_files(run.inspected_files, conclusive_only=False) or set(),
        }


def _ledger_entry_to_dict(entry: "SurveyLedgerEntry") -> dict:
    """Ledger 条目的数据形态。"""
    try:
        lines = json.loads(entry.lines) if entry.lines else []
    except (ValueError, TypeError):
        lines = []
    return {
        "fingerprint": entry.fingerprint,
        "repo_slug": entry.repo_slug,
        "file_path": entry.file_path or "",
        "category": entry.category,
        "severity": entry.severity,
        "title": entry.title or "",
        "body": entry.body or "",
        "lines": lines if isinstance(lines, list) else [],
        "finding_count": entry.finding_count,
        "state": entry.state,
        "first_seen_at": entry.first_seen_at,
        "last_seen_at": entry.last_seen_at,
        "last_run_uid": entry.last_run_uid or "",
        "last_checked_at": entry.last_checked_at,
    }


def get_survey_ledger_map(survey_id: int) -> Dict[str, dict]:
    """某个 Survey 的全部 Ledger 条目，{指纹: 条目}。"""
    from .models import SurveyLedgerEntry

    with session_scope() as session:
        rows = session.scalars(
            select(SurveyLedgerEntry).where(SurveyLedgerEntry.survey_id == int(survey_id))
        ).all()
        return {r.fingerprint: _ledger_entry_to_dict(r) for r in rows}


def list_survey_ledger(survey_id: int) -> List[dict]:
    """某个 Survey 的全部 Ledger 条目，按首次发现时间排序 —— 新表里的行序就是问题出现的先后。"""
    entries = list(get_survey_ledger_map(survey_id).values())
    return sorted(entries, key=lambda e: (e["first_seen_at"], e["fingerprint"]))


def list_survey_ledger_by_uid(survey_uid: str) -> List[dict]:
    """按 survey_uid 取 Ledger 条目（执行链路手里只有 uid）；巡检不存在时返回空列表。"""
    from .models import Survey

    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
    if survey_id is None:
        return []
    return list_survey_ledger(survey_id)


def earliest_survey_finding_times(survey_id: int, fingerprints: List[str]) -> Dict[str, datetime]:
    """
    若干指纹在库里仍保留的最早出现时刻。

    用于新进入 Ledger 的行的首次发现时间。运行记录按次数清理，
    更早的历史已经不在了，这个时间会偏晚 —— 这是有意接受的偏差，库里没有更好的证据。
    """
    from .models import SurveyFinding

    if not fingerprints:
        return {}
    result: Dict[str, datetime] = {}
    with session_scope() as session:
        # SQLite 单条语句的参数上限是 999，分批查
        for start in range(0, len(fingerprints), 500):
            batch = fingerprints[start:start + 500]
            rows = session.execute(
                select(SurveyFinding.fingerprint, func.min(SurveyFinding.created_at))
                .where(SurveyFinding.survey_id == int(survey_id), SurveyFinding.fingerprint.in_(batch))
                .group_by(SurveyFinding.fingerprint)
            ).all()
            result.update({fp: ts for fp, ts in rows if ts is not None})
    return result


def upsert_survey_ledger(survey_id: int, changes: List[dict]) -> None:
    """按指纹写入 Ledger 变更：已有的只覆盖变更里给出的字段，没有的新建。"""
    from .models import SurveyLedgerEntry

    if not changes:
        return
    now = utcnow()
    columns = {c.key for c in SurveyLedgerEntry.__table__.columns} - {"id", "survey_id", "fingerprint"}
    with session_scope() as session:
        existing = {
            e.fingerprint: e
            for e in session.scalars(
                select(SurveyLedgerEntry).where(SurveyLedgerEntry.survey_id == int(survey_id))
            ).all()
        }
        for change in changes:
            entry = existing.get(change["fingerprint"])
            if entry is None:
                entry = SurveyLedgerEntry(survey_id=int(survey_id), fingerprint=change["fingerprint"])
                session.add(entry)
                existing[change["fingerprint"]] = entry
            for key, value in change.items():
                if key not in columns:
                    continue
                if key == "title" and value:
                    value = str(value)[:512]
                if key == "repo_slug":
                    value = str(value or "")[:128]
                if key == "file_path":
                    value = str(value or "")[:1024] or None
                setattr(entry, key, value)
            entry.updated_at = now


def get_survey_push_context(run_uid: str) -> Optional[dict]:
    """
    判断一次运行能否推送所需的上下文：所属 Survey、运行状态、是否为最近一次成功运行，以及全部 Binding。

    运行不存在时返回 None。
    """
    from .models import Survey, SurveyRun

    with session_scope() as session:
        run = session.scalar(select(SurveyRun).where(SurveyRun.run_uid == run_uid))
        if run is None:
            return None
        survey = session.get(Survey, run.survey_id)
        latest_id = session.scalar(
            select(SurveyRun.id)
            .where(SurveyRun.survey_id == run.survey_id, SurveyRun.status == RUN_SUCCEEDED)
            .order_by(SurveyRun.id.desc())
            .limit(1)
        )
        return {
            "survey": _survey_to_dict(survey),
            "survey_id": run.survey_id,
            "status": run.status,
            "is_latest_succeeded": run.status == RUN_SUCCEEDED and latest_id == run.id,
        }


def start_survey_push(run_uid: str, binding_id: int, trigger: str) -> Optional[int]:
    """
    登记一次推送，返回推送记录 id；该 Binding 已有进行中的推送时返回 None。

    同一 Binding 并发推送会让两边都读到"键不存在"然后各追加一行，
    因此在写锁内检查并登记，挡住跨 worker 的并发（自动推送与手动重推可能落在不同进程）。
    """
    from .models import PUSH_RUNNING, SurveyBinding, SurveyPush, SurveyRun

    with session_scope() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        run_id = session.scalar(select(SurveyRun.id).where(SurveyRun.run_uid == run_uid))
        binding = session.get(SurveyBinding, int(binding_id))
        if run_id is None or binding is None:
            return None
        busy = session.scalar(
            select(SurveyPush.id).where(
                SurveyPush.binding_id == binding.id,
                SurveyPush.status == PUSH_RUNNING,
                SurveyPush.started_at > utcnow() - timedelta(seconds=PUSH_STALE_SECONDS),
            ).limit(1)
        )
        if busy is not None:
            return None
        push = SurveyPush(
            run_id=run_id,
            binding_id=binding.id,
            destination=binding.destination,
            target=binding.target,
            trigger=trigger,
            status=PUSH_RUNNING,
            started_at=utcnow(),
        )
        session.add(push)
        session.flush()
        return push.id


def get_survey_push(push_id: int) -> Optional[dict]:
    """执行一次推送所需的全部信息：推送记录、所属 Survey 与 Binding 当前状态。"""
    from .models import Survey, SurveyBinding, SurveyPush, SurveyRun

    with session_scope() as session:
        push = session.get(SurveyPush, int(push_id))
        if push is None:
            return None
        run = session.get(SurveyRun, push.run_id)
        survey = session.get(Survey, run.survey_id) if run else None
        binding = session.get(SurveyBinding, push.binding_id) if push.binding_id else None
        return {
            "id": push.id,
            "run_uid": run.run_uid if run else "",
            "survey": _survey_to_dict(survey) if survey else None,
            "survey_id": survey.id if survey else None,
            "destination": push.destination,
            "target": _json_object(push.target),
            "first_push": binding is not None and binding.last_succeeded_push_at is None,
        }


def finish_survey_push(
    push_id: int, status: str, stats: Optional[dict] = None, error_message: str = ""
) -> None:
    """收尾一次推送；成功时刷新对应 Binding 的最近成功推送时刻。"""
    from .models import PUSH_SUCCEEDED, SurveyBinding, SurveyPush

    now = utcnow()
    with session_scope() as session:
        push = session.get(SurveyPush, int(push_id))
        if push is None:
            return
        push.status = status
        push.finished_at = now
        if stats is not None:
            push.stats = json.dumps(stats, ensure_ascii=False)
        if error_message:
            push.error_message = str(error_message)[:4000]
        if status == PUSH_SUCCEEDED and push.binding_id:
            binding = session.get(SurveyBinding, push.binding_id)
            # 目标位置在推送期间被改掉时不刷新：刷新的话，新位置会被当成"已经推送过"，
            # 第一次推送时就不会补写本轮未发现与已忽略的行
            if binding is not None and binding.target == push.target:
                binding.last_succeeded_push_at = now


def list_survey_pushes(run_uid: str, include_detail: bool) -> List[dict]:
    """
    一次运行的推送记录，按时间倒序。

    include_detail=False（Guest）时不返回目标位置与错误信息：表格 ID 本身就是访问入口，
    错误信息里也可能带着表格链接或服务账号邮箱。
    """
    from .models import PUSH_RUNNING, SurveyPush, SurveyRun

    with session_scope() as session:
        run_id = session.scalar(select(SurveyRun.id).where(SurveyRun.run_uid == run_uid))
        if run_id is None:
            return []
        rows = session.scalars(
            select(SurveyPush).where(SurveyPush.run_id == run_id).order_by(SurveyPush.id.desc())
        ).all()
        stale_before = utcnow() - timedelta(seconds=PUSH_STALE_SECONDS)
        items = []
        for p in rows:
            item = {
                "id": p.id,
                "binding_id": p.binding_id,
                "destination": p.destination,
                "trigger": p.trigger,
                "status": p.status,
                "is_stale": p.status == PUSH_RUNNING and p.started_at < stale_before,
                "stats": _json_object(p.stats),
                "started_at": p.started_at.isoformat() if p.started_at else "",
                "finished_at": p.finished_at.isoformat() if p.finished_at else "",
            }
            if include_detail:
                item["target"] = _json_object(p.target)
                item["error_message"] = p.error_message or ""
            items.append(item)
        return items


# --------------------------------------------------------------------------
# SurveyFix（修复运行）
# --------------------------------------------------------------------------

# 登记修复运行的结果
FIX_START_OK = "ok"
FIX_START_NOT_FOUND = "not_found"
FIX_START_IGNORED = "ignored"
FIX_START_NO_REPO = "no_repo"
FIX_START_BUSY = "busy"
# 只给从没修复过的问题发起时（一键全部修复、巡检后自动发起），这个问题已经有过修复记录
FIX_START_EXISTS = "exists"

# 严重度从高到低的次序，用于批量修复的门槛与排序。unknown 不在其中：模型没按格式给出严重度的问题，
# 判断不了够不够格，批量时一律不处理，需要的话由人单条发起
_FIX_SEVERITY_RANK = {severity: len(FIX_MIN_SEVERITIES) - i for i, severity in enumerate(FIX_MIN_SEVERITIES)}

# 合并请求的 diff 最多存多少字符。修复只改一个文件，正常远小于此；超出说明模型改动失控，截断后照样能看出问题
SURVEY_FIX_DIFF_MAX = 200_000


def start_survey_fix(finding_id: int, trigger: str = FIX_TRIGGER_MANUAL, only_new: bool = False) -> tuple:
    """
    为一条巡检发现登记一次修复运行，返回 (结果, run_uid)。结果取 FIX_START_* 之一，非 ok 时 run_uid 为空串。

    only_new=True（一键全部修复、巡检后自动发起）时，这个问题有过任何修复记录都不再发起：成功的已经有合并请求，
    失败或未修改的重复发起大概率是同一个结果，只会每轮多烧几次模型调用、多推几个分支。要重试由人单条发起。
    判断放在同一个写锁里：两批同时跑时，先后选出的候选可能重叠，挑候选那一刻的判断挡不住重复。

    同一个问题（同巡检、同指纹、标题规整后相同）已有进行中的修复时拒绝：两次并行会提出两个一样的合并请求。
    这个问题在后续运行里是另一条发现，所以不按 finding_id 判断。疑似中断的也算进行中，与 SurveyRun 的口径一致，
    要先确认中断才能重新发起。检查与登记放在同一个写锁里，挡住跨 worker 的并发点击。
    """
    from ..survey.common import issue_key
    from .models import FIX_PHASE_FETCHING, SurveyFinding, SurveyFix, SurveyRunRepo

    with session_scope() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        finding = session.get(SurveyFinding, int(finding_id))
        if finding is None:
            return FIX_START_NOT_FOUND, ""
        if finding.ignore_id is not None:
            # 用户说过不再提醒的问题，界面上已经隐藏；绕过界面直接发起也不受理
            return FIX_START_IGNORED, ""
        repo_row = session.scalar(
            select(SurveyRunRepo).where(
                SurveyRunRepo.run_id == finding.run_id, SurveyRunRepo.repo_slug == finding.repo_slug
            ).limit(1)
        )
        if repo_row is None or not repo_row.url or not finding.file_path:
            return FIX_START_NO_REPO, ""

        key = issue_key(finding.fingerprint, finding.title)
        previous = [
            item for item in session.scalars(
                select(SurveyFix).where(
                    SurveyFix.survey_id == finding.survey_id, SurveyFix.fingerprint == finding.fingerprint
                )
            ).all()
            # 标题为空时认不出是不是同一个问题，只按 finding_id 判断，免得把同文件同类的另一个问题挡掉
            if item.finding_id == finding.id or (key[1] and issue_key(item.fingerprint, item.title) == key)
        ]
        if any(item.status == RUN_RUNNING for item in previous):
            return FIX_START_BUSY, ""
        if only_new and previous:
            return FIX_START_EXISTS, ""

        run_uid = str(uuid.uuid4())
        now = utcnow()
        session.add(
            SurveyFix(
                run_uid=run_uid,
                survey_id=finding.survey_id,
                finding_id=finding.id,
                fingerprint=finding.fingerprint,
                repo_slug=finding.repo_slug,
                file_path=finding.file_path,
                line=finding.line,
                category=finding.category,
                severity=finding.severity,
                title=finding.title,
                body=finding.body,
                repo_url=repo_row.url,
                target_branch=repo_row.branch or "",
                trigger=trigger,
                status=RUN_RUNNING,
                phase=FIX_PHASE_FETCHING,
                started_at=now,
                heartbeat_at=now,
            )
        )
        return FIX_START_OK, run_uid


def update_survey_fix(run_uid: str, **values) -> bool:
    """改写进行中的修复运行的字段并刷新心跳，返回是否改写了。已结束（含已确认中断）的不再改写。"""
    from .models import SurveyFix

    values["heartbeat_at"] = utcnow()
    if "diff" in values:
        values["diff"] = str(values["diff"] or "")[:SURVEY_FIX_DIFF_MAX]
    if "verification" in values and not isinstance(values["verification"], str):
        values["verification"] = json.dumps(values["verification"], ensure_ascii=False)
    with session_scope() as session:
        result = session.execute(
            update(SurveyFix)
            .where(SurveyFix.run_uid == run_uid, SurveyFix.status == RUN_RUNNING)
            .values(**values)
        )
        return result.rowcount > 0


def finish_survey_fix(run_uid: str, status: str, error_kind: str = "", error_message: str = "") -> bool:
    """
    收尾一次修复运行，返回是否由本次调用收尾。

    与 finish_survey_run 同理只收尾仍在进行中的。返回 False 说明它已被确认中断，
    调用方不能再拿它的结果去触发推送。
    """
    from .models import FIX_PHASE_DONE, SurveyFix

    now = utcnow()
    values: Dict[str, object] = {"status": status, "phase": FIX_PHASE_DONE, "heartbeat_at": now, "finished_at": now}
    if error_kind:
        values["error_kind"] = error_kind
    if error_message:
        values["error_message"] = str(error_message)[:4000]
    with session_scope() as session:
        result = session.execute(
            update(SurveyFix)
            .where(SurveyFix.run_uid == run_uid, SurveyFix.status == RUN_RUNNING)
            .values(**values)
        )
        return result.rowcount > 0


def confirm_survey_fix_interrupted(run_uid: str, stale_after_seconds: int) -> str:
    """确认一个 Stale 的修复运行已中断。返回值见 ``_confirm_interrupted``。"""
    from .models import SurveyFix

    return _confirm_interrupted(SurveyFix, run_uid, stale_after_seconds)


def _survey_fix_to_dict(fix: "SurveyFix", stale_after_seconds: int, include_detail: bool) -> dict:
    """
    修复运行的接口数据。整条都属于正文一侧（标题、作者、合并请求），默认只对 Admin 返回；
    「游客可见修复」打开后 Guest 也能读到，原始作者邮箱与问题正文由路由层剔除（见 ADR-0002 末节）。

    include_detail=False 时省掉 diff、验证过程与原问题正文，用于列表。
    """
    is_stale = _is_stale(fix, stale_after_seconds)
    item = {
        "run_uid": fix.run_uid,
        "finding_id": fix.finding_id,
        "fingerprint": fix.fingerprint,
        "repo_slug": fix.repo_slug,
        "file_path": fix.file_path or "",
        "line": fix.line,
        "category": fix.category,
        "severity": fix.severity,
        "title": fix.title or "",
        "target_branch": fix.target_branch or "",
        "source_branch": fix.source_branch or "",
        "trigger": fix.trigger,
        "status": fix.status,
        "phase": fix.phase or "",
        "is_stale": is_stale,
        "error_kind": fix.error_kind or "",
        "error_message": fix.error_message or "",
        "author_name": fix.author_name or "",
        "author_email": fix.author_email or "",
        "assignee_source": fix.assignee_source or "",
        "assignee_username": fix.assignee_username or "",
        "assignee_name": fix.assignee_name or "",
        "assignee_note": fix.assignee_note or "",
        "mr_iid": fix.mr_iid,
        "mr_url": fix.mr_url or "",
        "started_at": fix.started_at.isoformat() if fix.started_at else "",
        "finished_at": fix.finished_at.isoformat() if fix.finished_at else "",
    }
    if include_detail:
        try:
            verification = json.loads(fix.verification) if fix.verification else []
        except (ValueError, TypeError):
            verification = []
        item.update({
            "body": fix.body or "",
            "base_sha": fix.base_sha or "",
            "summary": fix.summary or "",
            "diff": fix.diff or "",
            "verification": verification if isinstance(verification, list) else [],
            "heartbeat_at": fix.heartbeat_at.isoformat() if fix.heartbeat_at else "",
        })
    return item


def get_survey_fix(run_uid: str, stale_after_seconds: int = 600) -> Optional[dict]:
    """单次修复运行的完整记录，附带所属巡检的标识与时区；不存在返回 None。"""
    from .models import Survey, SurveyFix

    with session_scope() as session:
        fix = session.scalar(select(SurveyFix).where(SurveyFix.run_uid == run_uid))
        if fix is None:
            return None
        survey = session.get(Survey, fix.survey_id)
        item = _survey_fix_to_dict(fix, stale_after_seconds, include_detail=True)
        item["survey_id"] = fix.survey_id
        item["survey_uid"] = survey.survey_uid if survey else ""
        item["survey_name"] = survey.name if survey else ""
        item["repo_url"] = fix.repo_url
        return item


def list_survey_fixes_for_issues(survey_uid: str, issues: List[dict], stale_after_seconds: int = 600) -> List[dict]:
    """
    针对报告里这些问题发起过的全部修复运行，按发起时间倒序。issues 是报告里的发现（含 id、fingerprint、title）。

    认"同一个问题"的口径与 survey.fix.attach_fixes 一致：finding_id 相同，或同指纹且标题规整后相同。
    只按指纹的话，同一文件同一类别里另一个问题（包括已被忽略的问题）的修复也会列进来。
    上一轮发起的修复，这一轮问题仍存在时是另一条发现，按标题才认得出来。
    """
    from ..survey.common import issue_key
    from .models import Survey, SurveyFix

    if not issues:
        return []
    finding_ids = {item.get("id") for item in issues}
    keys = {issue_key(item.get("fingerprint"), item.get("title")) for item in issues}
    keys = {key for key in keys if key[1]}
    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return []
        rows = session.scalars(
            select(SurveyFix)
            .where(
                SurveyFix.survey_id == survey_id,
                SurveyFix.fingerprint.in_({item.get("fingerprint") for item in issues}),
            )
            .order_by(SurveyFix.id.desc())
        ).all()
        return [
            _survey_fix_to_dict(f, stale_after_seconds, include_detail=False)
            for f in rows
            if f.finding_id in finding_ids or issue_key(f.fingerprint, f.title) in keys
        ]


def survey_fix_merge_requests(survey_id: int) -> Dict[str, List[dict]]:
    """
    一个巡检下每个指纹已提出的修复合并请求，按发起时间排列。供台账镜像使用。

    每项是 {"url", "assignee_username", "assignee_name"}，未指派时两个处理人字段为空串。
    链接与处理人在这里一起产出：台账里两列逐行对齐，分开查、分开去重迟早会错位。
    处理人是提出合并请求时指派的人，之后在代码平台上改派不会反映到这里。
    只收已提出合并请求的：失败与跳过的没有链接可附，进行中的还没有结果。
    """
    from .models import SurveyFix

    merge_requests: Dict[str, List[dict]] = {}
    with session_scope() as session:
        rows = session.execute(
            select(SurveyFix.fingerprint, SurveyFix.mr_url, SurveyFix.assignee_username, SurveyFix.assignee_name)
            .where(SurveyFix.survey_id == int(survey_id), SurveyFix.status == RUN_SUCCEEDED)
            .order_by(SurveyFix.id.asc())
        ).all()
    for fingerprint, url, username, name in rows:
        listed = merge_requests.setdefault(fingerprint, [])
        if url and all(mr["url"] != url for mr in listed):
            listed.append({"url": url, "assignee_username": username or "", "assignee_name": name or ""})
    return merge_requests


# 「修复合并请求」列表里代表"未指派"的处理人取值。平台用户名只含字母、数字、点、横线与下划线，不会撞上它
UNASSIGNED_FILTER = "__unassigned__"


def _unassigned(username_column):
    """处理人为空的条件。未指派在库里可能是 NULL 也可能是空串（取决于写入时走的哪条路径），两种都算。"""
    from sqlalchemy import or_

    return or_(username_column.is_(None), username_column == "")


def _like_pattern(text: str) -> str:
    """把关键字转成"包含"匹配的 LIKE 模式。% 与 _ 要转义：文件路径里到处是下划线，不转义会被当成任意字符。"""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _merge_request_fixes_stmt(survey_uid: str):
    """提出了合并请求的修复运行（可限定巡检）的查询，口径与 survey_fix_merge_requests 一致：成功且有链接。"""
    from .models import Survey, SurveyFix

    stmt = select(SurveyFix).where(
        SurveyFix.status == RUN_SUCCEEDED, SurveyFix.mr_url.is_not(None), SurveyFix.mr_url != ""
    )
    if survey_uid:
        stmt = stmt.where(SurveyFix.survey_id == select(Survey.id).where(Survey.survey_uid == survey_uid).scalar_subquery())
    return stmt


def list_fix_merge_requests(
    survey_uid: str = "",
    assignees: Optional[List[str]] = None,
    keyword: str = "",
    limit: int = 50,
    offset: int = 0,
    stale_after_seconds: int = 600,
) -> dict:
    """
    跨巡检列出修复提出的合并请求与处理人，按发起时间倒序。

    assignees 是要看的处理人用户名，可含 UNASSIGNED_FILTER 表示未指派；为空表示不按处理人筛选。
    keyword 在问题标题、仓库与文件路径里匹配。返回 {"items", "total", "assignees", "unassigned"}：
    后两项是只按巡检与关键字筛选时，各处理人的合并请求数与未指派的数量，给筛选框当选项——
    不受处理人筛选影响，否则选了一个人之后其他人就从选项里消失了。
    """
    from sqlalchemy import or_

    from .models import Survey, SurveyFix

    base = _merge_request_fixes_stmt(survey_uid)
    text_filter = str(keyword or "").strip()
    if text_filter:
        pattern = _like_pattern(text_filter)
        base = base.where(or_(
            SurveyFix.title.ilike(pattern, escape="\\"),
            SurveyFix.repo_slug.ilike(pattern, escape="\\"),
            SurveyFix.file_path.ilike(pattern, escape="\\"),
        ))

    wanted = [str(a).strip() for a in (assignees or []) if str(a or "").strip()]
    stmt = base
    if wanted:
        names = [a for a in wanted if a != UNASSIGNED_FILTER]
        conds = [SurveyFix.assignee_username.in_(names)] if names else []
        if UNASSIGNED_FILTER in wanted:
            conds.append(_unassigned(SurveyFix.assignee_username))
        stmt = stmt.where(or_(*conds))

    with session_scope() as session:
        total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = session.scalars(
            stmt.order_by(SurveyFix.id.desc()).offset(max(int(offset), 0)).limit(max(int(limit), 1))
        ).all()
        surveys = {
            s.id: s for s in session.scalars(select(Survey).where(Survey.id.in_({r.survey_id for r in rows}))).all()
        } if rows else {}
        items = []
        for fix in rows:
            item = _survey_fix_to_dict(fix, stale_after_seconds, include_detail=False)
            survey = surveys.get(fix.survey_id)
            item["survey_uid"] = survey.survey_uid if survey else ""
            item["survey_name"] = survey.name if survey else ""
            items.append(item)

        facet = base.subquery()
        counted = session.execute(
            select(facet.c.assignee_username, func.max(facet.c.assignee_name), func.count())
            .where(~_unassigned(facet.c.assignee_username))
            .group_by(facet.c.assignee_username)
        ).all()
        unassigned = session.scalar(
            select(func.count()).select_from(facet).where(_unassigned(facet.c.assignee_username))
        ) or 0
    options = sorted(
        ({"username": username, "name": name or "", "count": count} for username, name, count in counted),
        key=lambda o: (-o["count"], o["username"].lower()),
    )
    return {"items": items, "total": total, "assignees": options, "unassigned": unassigned}


def latest_succeeded_survey_run_uid(survey_id: int) -> str:
    """该巡检最近一次成功运行的 run_uid；没有时返回空串。"""
    from .models import SurveyRun

    with session_scope() as session:
        uid = session.scalar(
            select(SurveyRun.run_uid)
            .where(SurveyRun.survey_id == int(survey_id), SurveyRun.status == RUN_SUCCEEDED)
            .order_by(SurveyRun.id.desc())
            .limit(1)
        )
    return uid or ""


def list_survey_fix_candidates(run_uid: str, min_severity: str) -> List[int]:
    """
    一键全部修复与巡检后自动发起的修复要处理的发现 id，按优先级排好：严重度高的在前，同级时本轮新增的在前。

    只取这一轮可见的发现（被忽略的不算），严重度不低于 min_severity，且这个问题从没有过修复记录；
    同一个问题在这一轮被报了几遍时只取一条。是否有过修复在登记时还会在写锁里再判断一次（only_new），
    这里先剔除是为了让返回的数量对得上实际会发起的数量。
    """
    from ..survey.common import issue_key
    from .models import FINDING_STATE_NEW, SurveyFinding, SurveyFix, SurveyRun

    threshold = _FIX_SEVERITY_RANK.get(str(min_severity or "").strip().lower(), _FIX_SEVERITY_RANK[SEVERITY_WARNING])
    with session_scope() as session:
        run = session.scalar(select(SurveyRun).where(SurveyRun.run_uid == run_uid))
        if run is None:
            return []
        findings = session.scalars(
            select(SurveyFinding).where(SurveyFinding.run_id == run.id, _survey_finding_not_ignored())
        ).all()
        fixes = session.execute(
            select(SurveyFix.finding_id, SurveyFix.fingerprint, SurveyFix.title).where(
                SurveyFix.survey_id == run.survey_id
            )
        ).all()
    fixed_ids = {row.finding_id for row in fixes if row.finding_id is not None}
    fixed_keys = {issue_key(row.fingerprint, row.title) for row in fixes if issue_key(row.fingerprint, row.title)[1]}

    picked: List = []
    seen_keys = set()
    for finding in findings:
        if _FIX_SEVERITY_RANK.get(finding.severity, 0) < threshold or not finding.file_path:
            continue
        key = issue_key(finding.fingerprint, finding.title)
        if finding.id in fixed_ids or (key[1] and (key in fixed_keys or key in seen_keys)):
            continue
        if key[1]:
            seen_keys.add(key)
        picked.append(finding)
    picked.sort(key=lambda f: (-_FIX_SEVERITY_RANK.get(f.severity, 0), f.state != FINDING_STATE_NEW, f.id))
    return [f.id for f in picked]


# --------------------------------------------------------------------------
# RepoOwner（仓库负责人配置）
# --------------------------------------------------------------------------

REPO_OWNER_NOTE_MAX = 2000
# 姓名快照的长度上限，只为挡住异常数据；平台上的姓名远短于此
REPO_OWNER_NAME_MAX = 255
# 按负责人填写时一次最多几个路径：一个人负责的仓库再多也到不了这个数，超了多半是贴错了内容
REPO_OWNER_BATCH_MAX = 200


def normalize_owner_path(raw: str) -> str:
    """
    把填写的项目或组织路径规整成比对用的形式：小写、去掉首尾斜杠与 .git 后缀。

    平台上的路径大小写不敏感；填整条地址的情况由调用方先剥成路径（见 survey.fix.project_path_from_url）。
    """
    text = str(raw or "").strip().strip("/").lower()
    return text[:-4] if text.endswith(".git") else text


def _owner_usernames(owner: "RepoOwner") -> List[str]:
    """一条配置里按优先级排好的用户名；存的内容坏了时当作空列表。"""
    try:
        usernames = json.loads(owner.usernames or "[]")
    except (ValueError, TypeError):
        return []
    return [str(n) for n in usernames] if isinstance(usernames, list) else []


def _owner_names(owner: "RepoOwner") -> Dict[str, str]:
    """
    一条配置里 用户名 → 姓名 的快照；存的内容坏了时当作空。

    值为空串表示在平台上查过、查无此人；不在快照里的人是还没查过。两者要分开：
    查无此人的不再补查，否则他们永远占着每次补查的名额，排在后面的人就一直补不上。
    """
    try:
        names = json.loads(owner.user_names or "{}")
    except (ValueError, TypeError):
        return {}
    return {str(k): str(v or "") for k, v in names.items()} if isinstance(names, dict) else {}


def _merge_owner_names(known: Dict[str, str], looked_up: Optional[Dict[str, str]]) -> Dict[str, str]:
    """把这次查到的结果并进已有快照。这次查无此人不覆盖已有的名字：账号被删前的名字仍比一个裸用户名有用。"""
    merged = dict(known)
    for username, name in (looked_up or {}).items():
        if name or not merged.get(username):
            merged[username] = name
    return merged


def _set_owner_users(owner: "RepoOwner", usernames: List[str], names: Dict[str, str]) -> None:
    """写入负责人列表与姓名快照。快照只留列表里还有的人：移除的人不该在下次加回来时带出一个旧名字。"""
    owner.usernames = json.dumps(usernames, ensure_ascii=False)
    kept = {u: str(names[u])[:REPO_OWNER_NAME_MAX] for u in usernames if u in names}
    owner.user_names = json.dumps(kept, ensure_ascii=False) if kept else None


def _dedupe_usernames(usernames: List[str]) -> List[str]:
    """保持顺序去重，大小写不同的算同一个人、留第一次出现的写法：平台上的用户名不区分大小写。"""
    seen: Set[str] = set()
    result = []
    for name in usernames:
        if name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result


def clean_owner_username(raw) -> str:
    """填写的用户名去掉首尾空白与 @ 前缀。"""
    return str(raw or "").strip().lstrip("@")


def _clean_owner_note(note) -> Optional[str]:
    """备注去掉首尾空白并截到上限，空的存 None。"""
    return str(note or "").strip()[:REPO_OWNER_NOTE_MAX] or None


def _repo_owner_to_dict(owner: "RepoOwner") -> dict:
    """
    仓库负责人配置的接口数据。

    users 与 usernames 同序；保留 usernames 是为了不破坏已有的调用方（处理人追溯只读它）。
    还没查到姓名的人 name 为空串，由调用方决定显示什么。
    """
    usernames = _owner_usernames(owner)
    names = _owner_names(owner)
    return {
        "id": owner.id,
        "path": owner.path,
        "usernames": usernames,
        "users": [{"username": u, "name": names.get(u, "")} for u in usernames],
        "note": owner.note or "",
        "created_at": owner.created_at.isoformat() if owner.created_at else "",
        "updated_at": owner.updated_at.isoformat() if owner.updated_at else "",
    }


def list_repo_owners() -> List[dict]:
    """全部仓库负责人配置，按路径排序：组织与它下面的项目排在一起，方便看出谁覆盖了谁。"""
    from .models import RepoOwner

    with session_scope() as session:
        rows = session.scalars(select(RepoOwner).order_by(RepoOwner.path.asc())).all()
        return [_repo_owner_to_dict(r) for r in rows]


def save_repo_owner(
    path: str,
    usernames: List[str],
    note: str = "",
    owner_id: Optional[int] = None,
    user_names: Optional[Dict[str, str]] = None,
) -> Optional[dict]:
    """
    新增或修改一条仓库负责人配置，返回保存后的数据。

    owner_id 为空时按路径新增，路径已存在则覆盖它的负责人与备注（同一个路径只有一条，不会出现两条互相打架）；
    owner_id 不存在时返回 None。路径与别的配置冲突时抛 ValueError。
    user_names 是这次向平台查询的结果（空串表示查无此人），没查的人沿用这条配置里原有的快照。
    """
    from .models import RepoOwner

    normalized = normalize_owner_path(path)
    names = [clean_owner_username(n) for n in usernames or [] if clean_owner_username(n)]
    if not normalized:
        raise ValueError("路径不能为空")
    if not names:
        raise ValueError("至少填写一个负责人")
    with session_scope() as session:
        same_path = session.scalar(select(RepoOwner).where(RepoOwner.path == normalized))
        if owner_id is None:
            owner = same_path or RepoOwner(path=normalized, created_at=utcnow())
        else:
            owner = session.get(RepoOwner, int(owner_id))
            if owner is None:
                return None
            if same_path is not None and same_path.id != owner.id:
                raise ValueError(f"已有路径为 {normalized} 的配置")
        # 查不到姓名时不能把原有快照抹掉：平台一时不可达不该让列表退回一排用户名
        known = _merge_owner_names(_owner_names(owner), user_names)
        owner.path = normalized
        # 保持填写顺序去重：顺序就是优先级
        _set_owner_users(owner, _dedupe_usernames(names), known)
        owner.note = _clean_owner_note(note)
        owner.updated_at = utcnow()
        session.add(owner)
        session.flush()
        return _repo_owner_to_dict(owner)


def add_repo_owner_paths(username: str, paths: List[str], note: str = "", name: Optional[str] = None) -> dict:
    """
    把一个人加为多个项目或组织的负责人（按负责人填写），返回各路径的处理结果。

    路径已有配置时把这个人追加到末尾：已有的优先级是别人排好的，新加的人不该悄悄插到他们前面；
    已经在列表里的人位置不动。没有配置的路径新建一条，备注只写给新建的，不覆盖已有配置的备注。
    整批在一个事务里：有一条路径不合法就整批不写，免得一半生效、一半要人去对。
    name 是这次向平台查到的姓名：空串表示查无此人，None 表示没查成。
    返回 {"created": [...], "appended": [...], "unchanged": [...]}，元素是规整后的路径。
    """
    from .models import RepoOwner

    who = clean_owner_username(username)
    if not who:
        raise ValueError("请选择负责人")
    normalized = list(dict.fromkeys(normalize_owner_path(p) for p in paths or []))
    if "" in normalized:
        raise ValueError("路径不能为空")
    if not normalized:
        raise ValueError("至少填写一个路径")
    if len(normalized) > REPO_OWNER_BATCH_MAX:
        raise ValueError(f"一次最多填写 {REPO_OWNER_BATCH_MAX} 个路径")
    result: Dict[str, List[str]] = {"created": [], "appended": [], "unchanged": []}
    with session_scope() as session:
        existing = {r.path: r for r in session.scalars(select(RepoOwner).where(RepoOwner.path.in_(normalized))).all()}
        now = utcnow()
        for path in normalized:
            owner = existing.get(path)
            if owner is None:
                owner = RepoOwner(path=path, created_at=now, note=_clean_owner_note(note))
                current: List[str] = []
                result["created"].append(path)
            else:
                current = _owner_usernames(owner)
                if who.lower() in {c.lower() for c in current}:
                    result["unchanged"].append(path)
                    continue
                result["appended"].append(path)
            known = _merge_owner_names(_owner_names(owner), None if name is None else {who: name})
            _set_owner_users(owner, current + [who], known)
            owner.updated_at = now
            session.add(owner)
    return result


def fill_repo_owner_names(user_names: Dict[str, str]) -> int:
    """
    给还没查过姓名的负责人记下查询结果（空串表示查无此人），返回改了几条配置。

    只补没查过的，不改已有的名字，也不动 updated_at：这是展示用的缓存，不是谁改了配置。
    """
    from .models import RepoOwner

    found = dict(user_names or {})
    if not found:
        return 0
    changed = 0
    with session_scope() as session:
        for owner in session.scalars(select(RepoOwner)).all():
            usernames = _owner_usernames(owner)
            names = _owner_names(owner)
            missing = {u: found[u] for u in usernames if u not in names and u in found}
            if missing:
                _set_owner_users(owner, usernames, {**names, **missing})
                changed += 1
    return changed


def repo_owner_usernames_to_look_up() -> List[str]:
    """全部配置里还没查过姓名的负责人，按配置的路径与优先级排序、去重。"""
    from .models import RepoOwner

    with session_scope() as session:
        pending: List[str] = []
        for owner in session.scalars(select(RepoOwner).order_by(RepoOwner.path.asc())).all():
            names = _owner_names(owner)
            pending.extend(u for u in _owner_usernames(owner) if u not in names)
    return list(dict.fromkeys(pending))


def delete_repo_owner(owner_id: int) -> bool:
    """删除一条仓库负责人配置。返回是否真的删掉了。"""
    from .models import RepoOwner

    with session_scope() as session:
        return session.execute(delete(RepoOwner).where(RepoOwner.id == int(owner_id))).rowcount > 0


def find_repo_owner(project_path: str) -> Optional[dict]:
    """
    一个项目适用的仓库负责人配置：路径等于项目路径、或是它的上级组织路径的配置里，路径最长的那条。没有时返回 None。
    """
    from .models import RepoOwner

    target = normalize_owner_path(project_path)
    if not target:
        return None
    parts = target.split("/")
    candidates = ["/".join(parts[:n]) for n in range(len(parts), 0, -1)]
    with session_scope() as session:
        rows = {r.path: r for r in session.scalars(select(RepoOwner).where(RepoOwner.path.in_(candidates))).all()}
        for path in candidates:
            if path in rows:
                return _repo_owner_to_dict(rows[path])
    return None
