#!/usr/bin/env python3
"""
巡检调度线程。

用**独立的租约**而不是挂在 reconciler 的循环里：reconciler 一轮要做结算加清理，
跑得慢的时候会把巡检的触发时间往后拖；共用一条租约还意味着其中一个卡死、
另一个也跟着停摆。多引入的成本只是一行 UPDATE 和一个线程。
"""

import logging
import os
import socket
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from ..storage import repo
from ..storage.models import SURVEY_TRIGGER_MANUAL, SURVEY_TRIGGER_SCHEDULE, utcnow
from .common import SurveyError
from .config import load_survey_config
from .runner import execute_survey_run
from .schedule import next_fire_time

logger = logging.getLogger(__name__)

SCHEDULER_LEASE = "survey_scheduler"

# 错过窗口的宽限。超过它就跳过本次并重排到下一个触发点，不补跑 ——
# 一个每周任务在服务重启后突然补跑一次全量分析，除了烧钱没有意义。
# 取 15 分钟是为了把"调度线程慢了一拍"和"服务停机跨过了时间点"分开：
# 前者要照常执行，后者要跳过。
MISSED_WINDOW_GRACE_SECONDS = 900

_started = False
_start_lock = threading.Lock()


def _holder_id() -> str:
    """租约持有者标识：主机名 + 进程号，便于在日志里认出是哪个 worker 在跑。"""
    return f"{socket.gethostname()}#{os.getpid()}"


def compute_next_run(survey: dict, after: Optional[datetime] = None) -> Optional[datetime]:
    """算出某个巡检的下次触发时刻；周期设定非法时返回 None 并告警。"""
    try:
        return next_fire_time(
            survey["schedule_kind"], survey["schedule_expr"], survey["timezone"], after=after
        )
    except SurveyError as e:
        logger.warning("Invalid schedule for survey %s: %s", survey.get("slug"), e)
        return None


def ensure_next_run(survey: dict) -> Optional[datetime]:
    """
    为尚未排期的巡检补上 next_run_at。

    新建、启用、以及修改周期后都会走到这里；已排期的不动，
    否则每次调度轮询都会把触发点往后推，形成永远不触发的巡检。
    """
    if survey.get("next_run_at"):
        return None
    moment = compute_next_run(survey)
    if moment is not None:
        repo.set_survey_next_run(survey["survey_uid"], moment)
    return moment


def _run_in_thread(survey_uid: str, trigger: str) -> None:
    """把一次执行丢到后台线程。巡检可能跑几十分钟，绝不能占住调度循环。"""

    def _target():
        """执行体。异常在 execute_survey_run 内部已收敛，这里只兜最后一层。"""
        try:
            execute_survey_run(survey_uid, trigger)
        except Exception:
            logger.exception("Survey execution thread crashed: %s", survey_uid)

    thread = threading.Thread(target=_target, name=f"opencr-survey-{survey_uid[:8]}", daemon=True)
    thread.start()


def trigger_survey_now(survey_uid: str) -> bool:
    """
    立即执行一次巡检（后台页面的「立即执行」）。

    走的是和定时触发**完全相同**的入口，只有 trigger 字段不同 ——
    配置改完要等一周才知道对不对，这个功能就没法用。
    """
    if repo.has_running_survey_run(survey_uid):
        logger.info("Manual trigger skipped, survey already running: %s", survey_uid)
        return False
    _run_in_thread(survey_uid, SURVEY_TRIGGER_MANUAL)
    return True


def _dispatch_due() -> None:
    """扫描一轮到期的巡检并派发。"""
    now = utcnow()
    for survey in repo.list_due_surveys(now):
        survey_uid = survey["survey_uid"]
        next_at = compute_next_run(survey, after=now)

        due_at = survey.get("next_run_at")
        try:
            due_moment = datetime.fromisoformat(due_at) if due_at else None
        except (TypeError, ValueError):
            due_moment = None

        missed_by = (now - due_moment).total_seconds() if due_moment else 0
        if missed_by > MISSED_WINDOW_GRACE_SECONDS:
            logger.warning(
                "Survey %s missed its window by %.0fs (service was likely down), "
                "skipping this run and rescheduling to %s",
                survey["slug"], missed_by, next_at,
            )
            repo.set_survey_next_run(survey_uid, next_at)
            continue

        if repo.has_running_survey_run(survey_uid):
            # 上一次还没跑完就跳过而不是排队：积压两轮全量分析没有意义
            logger.info("Survey %s still running, skipping this tick", survey["slug"])
            repo.set_survey_next_run(survey_uid, next_at)
            continue

        logger.info("Survey %s due, dispatching (next run at %s)", survey["slug"], next_at)
        repo.set_survey_next_run(survey_uid, next_at, mark_ran=True)
        _run_in_thread(survey_uid, SURVEY_TRIGGER_SCHEDULE)


def _scheduler_loop() -> None:
    """
    调度主循环。

    单轮异常不退出循环：调度线程挂掉是静默故障 —— 界面一切正常，
    只是巡检再也不会自己跑了，而这要到下周才有人发现。
    """
    interval = int(load_survey_config()["scheduler_interval_seconds"])
    logger.info("Survey scheduler started: interval=%ss", interval)
    while True:
        try:
            if repo.acquire_lease(SCHEDULER_LEASE, _holder_id(), ttl_seconds=interval * 3):
                _dispatch_due()
        except Exception:
            logger.exception("Survey scheduler tick failed")
        time.sleep(interval)


def start_scheduler() -> None:
    """启动调度线程（每个进程至多一个；多 worker 由租约收敛成一个真正在跑）。"""
    global _started

    cfg = load_survey_config()
    if not cfg["enabled"]:
        logger.info("Survey scheduler disabled by config")
        return

    if _started:
        return
    with _start_lock:
        if _started:
            return
        _started = True

    thread = threading.Thread(target=_scheduler_loop, name="opencr-survey-scheduler", daemon=True)
    thread.start()
