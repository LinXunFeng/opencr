#!/usr/bin/env python3
"""
Push（推送）：把 Survey 的 Ledger 镜像到它绑定的各个 Destination。

自动推送（运行成功结束后）与手动重推都走这里，不各写一份。
推送只做镜像：内容永远是 Ledger 的当前状态，SurveyRun 只是"以哪一轮的名义推送"。
"""

import logging
import threading
from typing import List, Optional

from ..storage import repo
from ..storage.models import PUSH_FAILED, PUSH_SUCCEEDED
from .config import load_public_url
from .destinations import DestinationError, build_destination
from .ledger import build_ledger_rows, ledger_columns

logger = logging.getLogger(__name__)


class PushRejected(ValueError):
    """推送请求被拒绝，携带建议的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 409):
        """构造可直接展示给 Admin 的拒绝原因。"""
        super().__init__(message)
        self.status = status


def begin_pushes(run_uid: str, trigger: str, binding_id: Optional[int] = None) -> dict:
    """
    为一次运行登记推送记录，返回 {"started": [push_id...], "busy": [binding_id...]}。

    只允许推送该 Survey **最近一次成功**的运行：推送内容是 Ledger 的当前状态，
    以一轮旧运行的名义推送不会让台账倒退，但会让推送记录挂在一轮已经过时的报告下面，
    读记录的人会以为表里是那一轮的结果。
    """
    context = repo.get_survey_push_context(run_uid)
    if context is None:
        raise PushRejected("运行记录不存在", 404)
    if not context["is_latest_succeeded"]:
        raise PushRejected("只能推送该巡检最近一次成功的运行")

    bindings = context["survey"]["bindings"]
    if binding_id is not None:
        bindings = [b for b in bindings if b["id"] == int(binding_id)]
        if not bindings:
            raise PushRejected("该输出目标已不在巡检配置中", 404)
    if not bindings:
        raise PushRejected("该巡检没有配置输出目标", 400)

    started: List[int] = []
    busy: List[int] = []
    for binding in bindings:
        push_id = repo.start_survey_push(run_uid, binding["id"], trigger)
        if push_id is None:
            busy.append(binding["id"])
        else:
            started.append(push_id)
    return {"started": started, "busy": busy}


def execute_push(push_id: int) -> None:
    """
    执行一次已登记的推送。任何错误都收敛为推送记录的失败状态，不向上抛。

    推送失败不改变 SurveyRun 的状态、也不记降级：分析产出本身没有受损，
    把外部平台的故障写回运行记录，会让人以为这一轮的报告不可信。
    """
    push = repo.get_survey_push(push_id)
    if push is None or push["survey"] is None:
        return

    try:
        destination = build_destination(push["destination"])
        target = push["target"]
        public_url = load_public_url()
        rows = build_ledger_rows(
            push["survey"],
            repo.list_survey_ledger(push["survey_id"]),
            public_url,
            first_push=push["first_push"],
        )
        stats = destination.push(target, ledger_columns(include_link=bool(public_url)), rows)
    except DestinationError as e:
        logger.warning("Push failed: push=%s destination=%s: %s", push_id, push["destination"], e)
        repo.finish_survey_push(push_id, PUSH_FAILED, error_message=str(e))
        return
    except Exception as e:
        logger.exception("Push crashed: push=%s destination=%s", push_id, push["destination"])
        repo.finish_survey_push(push_id, PUSH_FAILED, error_message=f"未预期的错误：{e}")
        return

    repo.finish_survey_push(push_id, PUSH_SUCCEEDED, stats=stats.to_dict())
    logger.info(
        "Push succeeded: push=%s destination=%s stats=%s", push_id, push["destination"], stats.to_dict()
    )


def execute_pushes(push_ids: List[int]) -> None:
    """依次执行若干推送。串行而不是并发：多个 Binding 往往指向同一个平台账号，并发只会更快撞上限流。"""
    for push_id in push_ids:
        execute_push(push_id)


def execute_pushes_in_background(push_ids: List[int]) -> None:
    """把推送丢到后台线程。几千行的台账在限流退避下可能要几十秒，不能占住 HTTP 请求。"""
    if not push_ids:
        return

    def _target():
        """执行体。execute_push 内部已收敛异常，这里只兜最后一层。"""
        try:
            execute_pushes(push_ids)
        except Exception:
            logger.exception("Push thread crashed: %s", push_ids)

    threading.Thread(target=_target, name="opencr-survey-push", daemon=True).start()
