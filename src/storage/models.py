#!/usr/bin/env python3
"""
持久化模型定义（SQLAlchemy 2.x declarative）。

术语以 CONTEXT.md 为准：ReviewRun / Finding / Verdict / Degradation。
所有时间统一存 naive UTC —— SQLite 不保留时区，混用 aware/naive 会在比较时炸。
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """当前 naive UTC 时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """所有模型的基类。Alembic 从它的 metadata 推导迁移脚本。"""


# --- ReviewRun.status ---------------------------------------------------
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_SKIPPED = "skipped"

# --- ReviewRun.trigger --------------------------------------------------
TRIGGER_WEBHOOK_OPEN = "webhook_open"
TRIGGER_WEBHOOK_UPDATE = "webhook_update"
TRIGGER_MANUAL = "manual"

# --- ReviewRun.phase ----------------------------------------------------
PHASE_FETCHING = "fetching"
PHASE_MATCHING_SKILL = "matching_skill"
PHASE_REVIEWING = "reviewing"
PHASE_PUBLISHING = "publishing"
PHASE_DONE = "done"

# --- ReviewRun.error_kind -----------------------------------------------
ERROR_REVIEW = "review_error"
ERROR_UNEXPECTED = "unexpected"

# --- Degradation.kind ---------------------------------------------------
DEGRADE_INLINE_POST_FAILED = "inline_post_failed"
DEGRADE_DIFF_TRUNCATED = "diff_truncated"
DEGRADE_SKILL_SCRIPT_FAILED = "skill_script_failed"

# --- Finding.delivery ---------------------------------------------------
DELIVERY_INLINE = "inline"
DELIVERY_FILE_LEVEL = "file_level"
DELIVERY_FALLBACK_NOTE = "fallback_note"
DELIVERY_SUMMARY_ONLY = "summary_only"

TRACKABLE_DELIVERIES = {DELIVERY_INLINE, DELIVERY_FILE_LEVEL}

# --- Finding.severity ---------------------------------------------------
SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_ADVICE = "advice"
SEVERITY_UNKNOWN = "unknown"

# --- Finding.verdict ----------------------------------------------------
VERDICT_UNDECIDED = "undecided"
VERDICT_ACCEPTED = "accepted"
VERDICT_REJECTED = "rejected"
VERDICT_DISMISSED = "dismissed"
VERDICT_IGNORED = "ignored"
VERDICT_UNTRACKABLE = "untrackable"

# 进入采纳率分母的 Verdict。undecided（MR 尚未结算）与 untrackable 都不计入。
SETTLED_VERDICTS = (VERDICT_ACCEPTED, VERDICT_REJECTED, VERDICT_DISMISSED, VERDICT_IGNORED)

# --- Finding.verdict_reason ---------------------------------------------
REASON_THUMBS_UP = "thumbs_up"
REASON_THUMBS_DOWN = "thumbs_down"
REASON_RESOLVED = "resolved"
REASON_HUMAN_REPLIED = "human_replied"
REASON_MERGED_UNRESOLVED = "merged_unresolved"
REASON_NOT_TRACKABLE = "not_trackable"
REASON_DISCUSSION_MISSING = "discussion_missing"


class ReviewRun(Base):
    """一次审查执行。同一个 MR 会有多条 ReviewRun。"""

    __tablename__ = "review_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 对外只暴露 run_uid，不暴露自增 id
    run_uid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)

    project_id: Mapped[int] = mapped_column(Integer, nullable=False)
    project_path: Mapped[Optional[str]] = mapped_column(String(512))
    mr_iid: Mapped[int] = mapped_column(Integer, nullable=False)
    mr_title: Mapped[Optional[str]] = mapped_column(Text)

    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    review_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    # 实际命中的 skill，JSON 数组字符串
    review_skills: Mapped[Optional[str]] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RUN_RUNNING)
    # should_review_mr 的跳过理由，只在 status=skipped 时有值
    skip_reason: Mapped[Optional[str]] = mapped_column(Text)
    phase: Mapped[Optional[str]] = mapped_column(String(32))

    files_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Degradation 与 status 正交：一次 run 可以既 succeeded 又带多条降级。
    # JSON 数组：[{"kind": "inline_post_failed", "count": 3}, ...]
    degradations: Mapped[Optional[str]] = mapped_column(Text)

    error_kind: Mapped[Optional[str]] = mapped_column(String(32))
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    # 多进程下无法断言他人的 run 已死，Stale 只能由心跳超时推断
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    findings: Mapped[list["Finding"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_review_run_status_heartbeat", "status", "heartbeat_at"),
        Index("ix_review_run_started_at", "started_at"),
        Index("ix_review_run_mr", "project_id", "mr_iid"),
    )


class Finding(Base):
    """一条可定位的审查产出。"""

    __tablename__ = "finding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("review_run.id", ondelete="CASCADE"), nullable=False
    )

    project_id: Mapped[int] = mapped_column(Integer, nullable=False)
    mr_iid: Mapped[int] = mapped_column(Integer, nullable=False)

    # NULL = 不可追踪（Trackable 的唯一依据）
    discussion_id: Mapped[Optional[str]] = mapped_column(String(128))
    note_id: Mapped[Optional[int]] = mapped_column(Integer)

    file_path: Mapped[Optional[str]] = mapped_column(String(1024))
    line: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0 = 文件级
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default=SEVERITY_UNKNOWN)
    body: Mapped[Optional[str]] = mapped_column(Text)

    delivery: Mapped[str] = mapped_column(String(32), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, default=VERDICT_UNDECIDED)
    verdict_reason: Mapped[Optional[str]] = mapped_column(String(32))
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    run: Mapped["ReviewRun"] = relationship(back_populates="findings")

    __table_args__ = (
        # 部分唯一索引：只约束有 discussion_id 的行，untrackable 的 NULL 不参与
        Index(
            "ux_finding_discussion",
            "project_id",
            "discussion_id",
            unique=True,
            sqlite_where=discussion_id.isnot(None),
        ),
        Index("ix_finding_run", "run_id"),
        Index("ix_finding_mr_verdict", "project_id", "mr_iid", "verdict"),
    )


class MrSettlement(Base):
    """MR 结算记录，用于避免对同一 MR 重复结算。"""

    __tablename__ = "mr_settlement"

    project_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mr_iid: Mapped[int] = mapped_column(Integer, primary_key=True)
    mr_state: Mapped[str] = mapped_column(String(16), nullable=False)
    settled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class LeaderLease(Base):
    """单行租约表：多 worker 下只让一个进程跑 reconciler。"""

    __tablename__ = "leader_lease"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    holder: Mapped[Optional[str]] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
