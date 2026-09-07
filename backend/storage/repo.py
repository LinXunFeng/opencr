#!/usr/bin/env python3
"""
ReviewRun / Finding 的存取与聚合。

上层（review_server、admin）只通过本模块访问数据库，不直接持有 Session。
"""

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import delete, func, select, update

from .db import session_scope
from .models import (
    DELIVERY_SUMMARY_ONLY,
    ERROR_REVIEW,
    ERROR_UNEXPECTED,
    PHASE_DONE,
    REASON_NOT_TRACKABLE,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SKIPPED,
    RUN_SUCCEEDED,
    SETTLED_VERDICTS,
    SEVERITY_UNKNOWN,
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
    """推进阶段/文件计数，并顺带刷新心跳。"""
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
        session.execute(update(ReviewRun).where(ReviewRun.run_uid == run_uid).values(**values))


def heartbeat(run_uid: str) -> None:
    """仅刷新心跳。长时间停留在同一阶段时（例如单次大模型调用）用它保活。"""
    with session_scope() as session:
        session.execute(
            update(ReviewRun).where(ReviewRun.run_uid == run_uid).values(heartbeat_at=utcnow())
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
        run.heartbeat_at = utcnow()


def finish_run(
    run_uid: str,
    status: str,
    error_kind: str = "",
    error_message: str = "",
) -> None:
    """收尾一次 ReviewRun。error_message 截断到 4000 字符，避免超长堆栈撑爆行。"""
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
        session.execute(update(ReviewRun).where(ReviewRun.run_uid == run_uid).values(**values))


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

    is_stale = (
        run.status == RUN_RUNNING
        and run.heartbeat_at is not None
        and (utcnow() - run.heartbeat_at) > timedelta(seconds=stale_after_seconds)
    )
    return {
        "run_uid": run.run_uid,
        "project_id": run.project_id,
        "project_path": run.project_path or "",
        "mr_iid": run.mr_iid,
        "mr_title": run.mr_title or "",
        "trigger": run.trigger,
        "review_mode": run.review_mode,
        "review_skills": skills,
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
