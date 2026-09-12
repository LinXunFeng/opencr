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

# --- ReviewRun.error_kind / SurveyRun.error_kind ------------------------
ERROR_REVIEW = "review_error"
ERROR_SURVEY = "survey_error"
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

    # 只保存重试所需的范围与选择参数，不保存凭据或技能正文。
    review_input: Mapped[Optional[str]] = mapped_column(Text)
    # 不建外键：保留期清理原运行后，新运行仍保留来源标识。
    retry_of_uid: Mapped[Optional[str]] = mapped_column(String(36))
    retry_scope: Mapped[Optional[str]] = mapped_column(String(16))

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


class AppSetting(Base):
    """
    运行期设置的键值存储。

    只放两类东西：一是必须跨重启与跨 worker 保持一致的运行期密钥（session secret_key），
    二是被刻意放在配置文件之外的开关（guest_read / guest_retry，理由见 ADR-0002）。
    **不放任何凭据** —— 密码的唯一真相是 config.yaml。
    """

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


# ==========================================================================
# 定期巡检（Survey / SurveyRun）
#
# 与 ReviewRun 并列而非从属，因此独立建表。合表的代价是 Coverage 会失去意义：
# SurveyRun 的 Finding 不依附任何 MR discussion，永远 untrackable，
# 混进 finding 表就等于往采纳率与 Coverage 的分母里灌一堆恒为 untrackable 的行。
# ==========================================================================

# --- Survey.schedule_kind -----------------------------------------------
SCHEDULE_DAILY = "daily"
SCHEDULE_WEEKLY = "weekly"
SCHEDULE_MONTHLY = "monthly"
SCHEDULE_CRON = "cron"

# --- SurveySource.kind ---------------------------------------------------
SOURCE_REPO = "repo"
SOURCE_ORG = "org"

# --- SurveyRun.trigger ---------------------------------------------------
SURVEY_TRIGGER_SCHEDULE = "schedule"
SURVEY_TRIGGER_MANUAL = "manual"

# --- SurveyRun.phase -----------------------------------------------------
# 与 ReviewRun 的 PHASE_* 刻意分开：两条链路的阶段序列没有任何重叠，
# 共用一套常量只会让"这个阶段属于哪条链路"变成需要查代码才能回答的问题。
SURVEY_PHASE_FETCHING = "fetching"
SURVEY_PHASE_PROFILING = "profiling"
SURVEY_PHASE_MATCHING_SKILL = "matching_skill"
SURVEY_PHASE_INTEGRATING = "integrating"
SURVEY_PHASE_INSPECTING = "inspecting"
SURVEY_PHASE_SUMMARIZING = "summarizing"
SURVEY_PHASE_DONE = "done"

# --- SurveyRunRepo.status ------------------------------------------------
REPO_OK = "ok"
REPO_FETCH_FAILED = "fetch_failed"
REPO_INDEX_FAILED = "index_failed"

# --- SurveyRunRepo.profile_kind ------------------------------------------
PROFILE_CODEGRAPH = "codegraph"
PROFILE_MANIFEST = "manifest"

# --- Degradation.kind（巡检侧）-------------------------------------------
DEGRADE_REPO_FETCH_FAILED = "repo_fetch_failed"
DEGRADE_INDEX_FAILED = "index_failed"
DEGRADE_BUDGET_EXHAUSTED = "budget_exhausted"
DEGRADE_PROFILE_FALLBACK = "profile_fallback"

# --- SurveyFinding.category ----------------------------------------------
# 闭集，不允许模型自由发挥 —— 它是指纹的组成部分，措辞漂移会让跨轮次对比失效。
CATEGORY_CORRECTNESS = "correctness"
CATEGORY_SECURITY = "security"
CATEGORY_CROSS_REPO = "cross_repo"
CATEGORY_ARCHITECTURE = "architecture"
CATEGORY_PERFORMANCE = "performance"
CATEGORY_MAINTAINABILITY = "maintainability"
CATEGORY_DEPENDENCY = "dependency"
CATEGORY_CONVENTION = "convention"

SURVEY_CATEGORIES = (
    CATEGORY_CORRECTNESS,
    CATEGORY_SECURITY,
    CATEGORY_CROSS_REPO,
    CATEGORY_ARCHITECTURE,
    CATEGORY_PERFORMANCE,
    CATEGORY_MAINTAINABILITY,
    CATEGORY_DEPENDENCY,
    CATEGORY_CONVENTION,
)

# --- SurveyFinding.state -------------------------------------------------
# 只有 new / persisted 两种。"已消失"不落行 —— 它是上一轮有、本轮没有，
# 由报告期比对指纹算出来的，凭空造一条"已消失的 Finding"会让列表里出现
# 没有对应代码的幽灵行，也会把 severity/category 的统计口径搞乱。
FINDING_STATE_NEW = "new"
FINDING_STATE_PERSISTED = "persisted"


class Survey(Base):
    """一份定期巡检配置。它是配置，不是执行——执行实例是 SurveyRun。"""

    __tablename__ = "survey"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    survey_uid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 工作区目录名。用 slug 而不是 name：中文、空格、"../" 都会出事。
    # 与 Survey 一一对应且**永不随重命名变化**——改个名字就搬几十 GB 代码不划算。
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)

    schedule_kind: Mapped[str] = mapped_column(String(16), nullable=False, default=SCHEDULE_WEEKLY)
    # daily: "HH:MM" / weekly: "DOW HH:MM" / monthly: "DOM HH:MM" / cron: 五段表达式
    schedule_expr: Mapped[str] = mapped_column(String(128), nullable=False, default="1 09:00")
    # IANA 时区名。容器默认 UTC，不显式存会让"每周一早上 9 点"在部署后变成下午 5 点。
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    # 存**被取消勾选**的 skill，而不是被勾选的。默认空数组 = 全选，
    # 这样新增一个 skill 时它会自动进入所有巡检的候选池；
    # 反过来存勾选集的话，新 skill 会被历史配置永久排除在外，且没人会想起来去补勾。
    excluded_skills: Mapped[Optional[str]] = mapped_column(Text)

    # 巡检完成后是否删除工作区。默认不删 —— 保留下来下次可以增量拉取，
    # 删掉意味着下次全量 clone，耗时会从分钟级涨到小时级。
    delete_workspace_after: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)

    # 预算覆盖项，NULL 表示用 config.yaml 的全局默认。
    budget_wall_clock_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    budget_l1_max_chars: Mapped[Optional[int]] = mapped_column(Integer)
    budget_l2_max_focus: Mapped[Optional[int]] = mapped_column(Integer)
    budget_index_timeout_seconds: Mapped[Optional[int]] = mapped_column(Integer)

    # 保留最近多少次 SurveyRun。清理永远不会删掉最近一次（见 repo.purge_survey_runs）。
    retention_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=20)

    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # 预先算好下次触发时间，调度线程每轮只需一次索引扫描，不用把所有 Survey 拉出来算 cron
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    sources: Mapped[list["SurveySource"]] = relationship(
        back_populates="survey",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_survey_next_run", "enabled", "next_run_at"),
    )


class SurveySource(Base):
    """
    巡检的仓库来源：一条具体仓库链接，或一个仓库组织。

    组织不在这里展开——组织下的项目列表会变，展开必须发生在每次执行时，
    否则"把仓库加进组织就自动纳入巡检"这个语义就不成立了。
    """

    __tablename__ = "survey_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    survey_id: Mapped[int] = mapped_column(
        ForeignKey("survey.id", ondelete="CASCADE"), nullable=False
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=SOURCE_REPO)
    # kind=repo 时是仓库地址；kind=org 时是组织/群组路径
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 留空表示用仓库的默认分支。不硬写 main —— 老仓库很多还是 master。
    branch: Mapped[Optional[str]] = mapped_column(String(255))

    # 仅 kind=org 有意义：排除模式（JSON 数组，逐条按子串或正则匹配仓库路径）。
    # 没有它的话，别人往组织里推一个 500MB 的资源仓库就能让巡检多跑一小时。
    exclude_patterns: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    survey: Mapped["Survey"] = relationship(back_populates="sources")

    __table_args__ = (
        Index("ix_survey_source_survey", "survey_id"),
    )


class SurveyRun(Base):
    """一次巡检执行。"""

    __tablename__ = "survey_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_uid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    survey_id: Mapped[int] = mapped_column(
        ForeignKey("survey.id", ondelete="CASCADE"), nullable=False
    )

    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RUN_RUNNING)
    phase: Mapped[Optional[str]] = mapped_column(String(32))

    repos_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    repos_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 实际命中的 skill，JSON 数组字符串
    matched_skills: Mapped[Optional[str]] = mapped_column(Text)
    # L3 的跨仓库整合叙述（Markdown）。逐条 Finding 另存 survey_finding，
    # 因为"新增/仍存在"的比对必须按条做，两篇 Markdown 算不出可靠的差集。
    summary: Mapped[Optional[str]] = mapped_column(Text)

    degradations: Mapped[Optional[str]] = mapped_column(Text)
    error_kind: Mapped[Optional[str]] = mapped_column(String(32))
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    findings: Mapped[list["SurveyFinding"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    repos: Mapped[list["SurveyRunRepo"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_survey_run_survey_started", "survey_id", "started_at"),
        Index("ix_survey_run_status_heartbeat", "status", "heartbeat_at"),
    )


class SurveyRunRepo(Base):
    """一次 SurveyRun 里单个仓库的处理结果（组织展开后的实际清单也在这里）。"""

    __tablename__ = "survey_run_repo"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("survey_run.id", ondelete="CASCADE"), nullable=False
    )

    repo_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    branch: Mapped[Optional[str]] = mapped_column(String(255))
    commit_sha: Mapped[Optional[str]] = mapped_column(String(64))

    status: Mapped[str] = mapped_column(String(24), nullable=False, default=REPO_OK)
    # codegraph 装不上时退化成 manifest 级画像，巡检照常完成但 L1 是结构盲的
    profile_kind: Mapped[Optional[str]] = mapped_column(String(24))
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    run: Mapped["SurveyRun"] = relationship(back_populates="repos")

    __table_args__ = (
        Index("ix_survey_run_repo_run", "run_id"),
    )


class SurveyFinding(Base):
    """巡检产出的一条 Finding。它永远不 Trackable，因此没有 discussion/verdict 字段。"""

    __tablename__ = "survey_finding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("survey_run.id", ondelete="CASCADE"), nullable=False
    )
    # 冗余存一份 survey_id：跨轮次比对与"这个巡检历史上所有 security 问题"这类查询
    # 都以 Survey 为范围，每次都 JOIN 回 survey_run 只是徒增成本。
    survey_id: Mapped[int] = mapped_column(Integer, nullable=False)

    repo_slug: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    file_path: Mapped[Optional[str]] = mapped_column(String(1024))
    line: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    category: Mapped[str] = mapped_column(String(24), nullable=False, default=CATEGORY_CORRECTNESS)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default=SEVERITY_UNKNOWN)
    title: Mapped[Optional[str]] = mapped_column(String(512))
    body: Mapped[Optional[str]] = mapped_column(Text)

    # 指纹 = hash(repo_slug + file_path + category)。不含正文 ——
    # 模型两次的措辞不会一样，对正文做哈希等于每轮全是"新增"。
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=FINDING_STATE_NEW)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    run: Mapped["SurveyRun"] = relationship(back_populates="findings")

    __table_args__ = (
        Index("ix_survey_finding_run", "run_id"),
        Index("ix_survey_finding_survey_fp", "survey_id", "fingerprint"),
        Index("ix_survey_finding_survey_created", "survey_id", "created_at"),
    )


class SurveyIgnore(Base):
    """人工标记为"已知问题、不再提醒"的指纹。命中的 Finding 不再入库。"""

    __tablename__ = "survey_ignore"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    survey_id: Mapped[int] = mapped_column(
        ForeignKey("survey.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # 留痕用：忽略一条问题的理由，半年后没人记得为什么
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        Index("ux_survey_ignore", "survey_id", "fingerprint", unique=True),
    )
