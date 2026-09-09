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

from sqlalchemy import delete, func, select, text, update

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
            raise RetryRejected("该 MR 已有审查正在运行（含疑似中断），请查看运行记录", active_run_uid=active)
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
    }


def create_survey(
    name: str,
    slug: str,
    schedule_kind: str,
    schedule_expr: str,
    timezone_name: str,
    sources: List[dict],
    next_run_at: Optional[datetime] = None,
    **options,
) -> dict:
    """新建一个巡检配置，返回它的完整数据。"""
    from .models import Survey, SurveySource

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
        session.refresh(survey)
        return _survey_to_dict(survey)


def update_survey(survey_uid: str, fields: dict, sources: Optional[List[dict]] = None) -> Optional[dict]:
    """
    更新巡检配置。sources 传 None 表示不动来源清单，传列表表示整体替换。

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
    """推进阶段与仓库计数，并顺带刷新心跳。"""
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
        session.execute(update(SurveyRun).where(SurveyRun.run_uid == run_uid).values(**values))


def survey_heartbeat(run_uid: str) -> None:
    """仅刷新心跳。L1/L2 的单次模型调用可能很久，靠它保活。"""
    from .models import SurveyRun

    with session_scope() as session:
        session.execute(
            update(SurveyRun).where(SurveyRun.run_uid == run_uid).values(heartbeat_at=utcnow())
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
) -> None:
    """记录一次运行里单个仓库的处理结果。"""
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
            )
        )


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

    命中忽略清单的直接丢弃、不入库 —— 入库再在查询时过滤的话，
    "本次发现 80 条"这个数字会一直包含用户明确说过不想再看的条目。
    返回 {"new": n, "persisted": m, "ignored": k}。
    """
    from .models import (
        FINDING_STATE_NEW,
        FINDING_STATE_PERSISTED,
        SurveyFinding,
        SurveyIgnore,
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
        ignored = set(
            session.scalars(
                select(SurveyIgnore.fingerprint).where(SurveyIgnore.survey_id == survey_id)
            ).all()
        )
        run_id = run.id

    previous = previous_survey_fingerprints(survey_id, run_id)

    with session_scope() as session:
        for item in findings:
            fingerprint = item["fingerprint"]
            if fingerprint in ignored:
                counters["ignored"] += 1
                continue
            state = FINDING_STATE_PERSISTED if fingerprint in previous else FINDING_STATE_NEW
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
                )
            )
    return counters


def finish_survey_run(
    run_uid: str,
    status: str,
    summary: str = "",
    error_kind: str = "",
    error_message: str = "",
) -> None:
    """收尾一次 SurveyRun。"""
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
        session.execute(update(SurveyRun).where(SurveyRun.run_uid == run_uid).values(**values))


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

    is_stale = (
        run.status == RUN_RUNNING
        and run.heartbeat_at is not None
        and (utcnow() - run.heartbeat_at) > timedelta(seconds=stale_after_seconds)
    )
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
        "created_at": finding.created_at.isoformat() if finding.created_at else "",
    }
    if include_body:
        item["title"] = finding.title or ""
        item["body"] = finding.body or ""
    return item


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
        return [
            {**_survey_run_to_dict(r, stale_after_seconds), "survey_name": survey_names.get(r.survey_id, "")}
            for r in runs
        ]


def get_survey_run_detail(
    run_uid: str, include_body: bool = True, stale_after_seconds: int = 600
) -> Optional[dict]:
    """
    单次 SurveyRun 的完整报告：仓库清单、逐条发现、以及与上一次相比的差异。

    "已消失"不从库里读 —— 它没有对应的行（见 models 里 FINDING_STATE 的注释），
    是拿上一次的指纹集减去本次算出来的。
    """
    from .models import Survey, SurveyFinding, SurveyRun, SurveyRunRepo

    with session_scope() as session:
        run = session.scalar(select(SurveyRun).where(SurveyRun.run_uid == run_uid))
        if run is None:
            return None
        survey = session.get(Survey, run.survey_id)
        findings = session.scalars(
            select(SurveyFinding).where(SurveyFinding.run_id == run.id).order_by(SurveyFinding.id.asc())
        ).all()
        repos = session.scalars(
            select(SurveyRunRepo).where(SurveyRunRepo.run_id == run.id).order_by(SurveyRunRepo.id.asc())
        ).all()

        detail = _survey_run_to_dict(run, stale_after_seconds)
        detail["survey_uid"] = survey.survey_uid if survey else ""
        detail["survey_name"] = survey.name if survey else ""
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
                "error_message": r.error_message or "",
            }
            for r in repos
        ]
        detail["findings"] = [_survey_finding_to_dict(f, include_body) for f in findings]

        current_fps = {f.fingerprint for f in findings}
        prev_run_id = session.scalar(
            select(SurveyRun.id)
            .where(SurveyRun.survey_id == run.survey_id, SurveyRun.id < run.id,
                   SurveyRun.status == RUN_SUCCEEDED)
            .order_by(SurveyRun.id.desc())
            .limit(1)
        )
        resolved = []
        if prev_run_id is not None:
            prev_findings = session.scalars(
                select(SurveyFinding).where(SurveyFinding.run_id == prev_run_id)
            ).all()
            for f in prev_findings:
                if f.fingerprint not in current_fps:
                    resolved.append(_survey_finding_to_dict(f, include_body))
        detail["resolved_findings"] = resolved
        detail["counts"] = {
            "new": sum(1 for f in findings if f.state == "new"),
            "persisted": sum(1 for f in findings if f.state == "persisted"),
            "resolved": len(resolved),
            "total": len(findings),
        }
        return detail


def add_survey_ignore(survey_uid: str, fingerprint: str, note: str = "") -> bool:
    """把一条发现标记为"已知问题、不再提醒"。已存在时视为成功。"""
    from .models import Survey, SurveyIgnore

    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return False
        existing = session.scalar(
            select(SurveyIgnore).where(
                SurveyIgnore.survey_id == survey_id, SurveyIgnore.fingerprint == fingerprint
            )
        )
        if existing is None:
            session.add(
                SurveyIgnore(survey_id=survey_id, fingerprint=fingerprint, note=(note or "")[:2000] or None)
            )
        return True


def remove_survey_ignore(survey_uid: str, fingerprint: str) -> bool:
    """取消忽略。"""
    from .models import Survey, SurveyIgnore

    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return False
        result = session.execute(
            delete(SurveyIgnore).where(
                SurveyIgnore.survey_id == survey_id, SurveyIgnore.fingerprint == fingerprint
            )
        )
        return (result.rowcount or 0) > 0


def list_survey_ignores(survey_uid: str) -> List[dict]:
    """某个巡检的忽略清单。"""
    from .models import Survey, SurveyIgnore

    with session_scope() as session:
        survey_id = session.scalar(select(Survey.id).where(Survey.survey_uid == survey_uid))
        if survey_id is None:
            return []
        rows = session.scalars(
            select(SurveyIgnore).where(SurveyIgnore.survey_id == survey_id).order_by(SurveyIgnore.id.desc())
        ).all()
        return [
            {
                "fingerprint": r.fingerprint,
                "note": r.note or "",
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ]


def purge_survey_runs(survey_id: int, retention_runs: int) -> int:
    """
    按次数清理巡检运行记录，返回删除条数。

    **永远保留最近一次**，哪怕 retention_runs 被设成 0 或 1 ——
    "新增/仍存在"的比对依赖上一次的记录还在，删掉它之后下一轮报告会把
    所有问题都标成新增。这类故障发生在某个凌晨，且看起来完全正常。
    """
    from .models import SurveyFinding, SurveyRun

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
        session.execute(delete(SurveyRun).where(SurveyRun.id.in_(stale_ids)))
        return len(stale_ids)


def purge_all_survey_runs() -> int:
    """对所有巡检按各自的 retention_runs 做一次清理，返回总删除条数。"""
    from .models import Survey

    with session_scope() as session:
        pairs = session.execute(select(Survey.id, Survey.retention_runs)).all()
    return sum(purge_survey_runs(sid, keep) for sid, keep in pairs)


def survey_dashboard_stats(days: int = 90) -> dict:
    """巡检的聚合统计。不含任何正文，Guest 开关打开时可直接返回。"""
    from .models import SurveyFinding, SurveyRun

    since = _window_start(days)
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
                .where(SurveyFinding.created_at >= since)
                .group_by(SurveyFinding.category)
            ).all()
        )
        by_severity = dict(
            session.execute(
                select(SurveyFinding.severity, func.count())
                .where(SurveyFinding.created_at >= since)
                .group_by(SurveyFinding.severity)
            ).all()
        )
        by_state = dict(
            session.execute(
                select(SurveyFinding.state, func.count())
                .where(SurveyFinding.created_at >= since)
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
