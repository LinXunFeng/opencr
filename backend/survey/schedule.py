#!/usr/bin/env python3
"""
巡检的调度语义：把界面上的周期设定折算成下一次触发时刻。

自己实现 cron 而不是引入 croniter：需要的只是"标准五段表达式的下一个触发点"，
而 `_next_fire` 就是按分钟向前搜索到命中为止。为这一个函数增加一项部署依赖
（Docker 镜像、install.sh、离线内网的 pip 源都要跟着改）不成比例。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..storage.models import (
    SCHEDULE_CRON,
    SCHEDULE_DAILY,
    SCHEDULE_MONTHLY,
    SCHEDULE_WEEKLY,
)
from .common import SurveyError

logger = logging.getLogger(__name__)

# 向前搜索的上限。闰年 + 一天余量：合法表达式最迟也会在一年内命中，
# 搜不到就说明是 "2 30 * * *"（2月30日）这类永不触发的表达式，应当报错而不是空转。
_MAX_SEARCH_MINUTES = 367 * 24 * 60


def resolve_timezone(name: str) -> ZoneInfo:
    """
    解析时区名；无效时回退 UTC 并告警。

    时区错了不该让巡检起不来，但一定要在日志里留下痕迹 ——
    容器默认 UTC，"每周一早上 9 点"悄悄变成下午 5 点是很难自查的问题。
    """
    raw = str(name or "").strip() or "UTC"
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("Invalid timezone %r, falling back to UTC", raw)
        return ZoneInfo("UTC")


def _parse_field(field: str, low: int, high: int, name: str) -> Set[int]:
    """解析 cron 单个字段，返回命中值集合。支持 `*`、`a-b`、`*/n`、`a,b,c` 及其组合。"""
    values: Set[int] = set()
    for part in str(field or "").split(","):
        token = part.strip()
        if not token:
            raise SurveyError(f"cron 表达式的 {name} 字段为空")

        step = 1
        if "/" in token:
            token, _, step_raw = token.partition("/")
            try:
                step = int(step_raw)
            except ValueError as e:
                raise SurveyError(f"cron 表达式的 {name} 步长非法：{step_raw}") from e
            if step <= 0:
                raise SurveyError(f"cron 表达式的 {name} 步长必须为正数")
            token = token.strip() or "*"

        if token == "*":
            start, end = low, high
        elif "-" in token.lstrip("-"):
            start_raw, _, end_raw = token.partition("-")
            try:
                start, end = int(start_raw), int(end_raw)
            except ValueError as e:
                raise SurveyError(f"cron 表达式的 {name} 区间非法：{token}") from e
        else:
            try:
                start = end = int(token)
            except ValueError as e:
                raise SurveyError(f"cron 表达式的 {name} 取值非法：{token}") from e

        if start > end or start < low or end > high:
            raise SurveyError(f"cron 表达式的 {name} 超出范围 [{low},{high}]：{token}")
        values.update(range(start, end + 1, step))

    if not values:
        raise SurveyError(f"cron 表达式的 {name} 字段无有效取值")
    return values


def parse_cron(expression: str) -> Tuple[Set[int], Set[int], Set[int], Set[int], Set[int]]:
    """把五段 cron 拆成 (分, 时, 日, 月, 周) 五个命中集合。表达式非法时抛 SurveyError。"""
    fields = str(expression or "").split()
    if len(fields) != 5:
        raise SurveyError(f"cron 表达式必须是五段（分 时 日 月 周），实际收到 {len(fields)} 段")

    minutes = _parse_field(fields[0], 0, 59, "分钟")
    hours = _parse_field(fields[1], 0, 23, "小时")
    doms = _parse_field(fields[2], 1, 31, "日")
    months = _parse_field(fields[3], 1, 12, "月")
    dows = _parse_field(fields[4], 0, 7, "星期")
    # cron 里 0 与 7 都表示周日，统一折算成 Python 的 weekday 语义前先归一
    if 7 in dows:
        dows = (dows - {7}) | {0}
    return minutes, hours, doms, months, dows


def to_cron(schedule_kind: str, schedule_expr: str) -> str:
    """
    把预设周期折算成 cron。

    预设与 cron 共用同一个求值器，而不是各写一套 —— 两套"下次什么时候跑"的
    实现必然会在夏令时、月末这些边界上漂移，而这种漂移只在生产才看得见。
    """
    kind = str(schedule_kind or "").strip().lower()
    expr = str(schedule_expr or "").strip()

    if kind == SCHEDULE_CRON:
        parse_cron(expr)
        return expr

    def _hhmm(text: str) -> Tuple[int, int]:
        hh, _, mm = text.partition(":")
        try:
            hour, minute = int(hh), int(mm)
        except ValueError as e:
            raise SurveyError(f"时间格式应为 HH:MM，实际收到：{text}") from e
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise SurveyError(f"时间超出范围：{text}")
        return hour, minute

    if kind == SCHEDULE_DAILY:
        hour, minute = _hhmm(expr)
        return f"{minute} {hour} * * *"

    if kind == SCHEDULE_WEEKLY:
        dow_raw, _, time_raw = expr.partition(" ")
        try:
            dow = int(dow_raw)
        except ValueError as e:
            raise SurveyError(f"每周设定应为「星期 HH:MM」，实际收到：{expr}") from e
        if not 1 <= dow <= 7:
            raise SurveyError(f"星期取值应在 1(周一) 到 7(周日) 之间：{dow_raw}")
        hour, minute = _hhmm(time_raw.strip())
        # ISO 的 7=周日 对应 cron 的 0
        return f"{minute} {hour} * * {0 if dow == 7 else dow}"

    if kind == SCHEDULE_MONTHLY:
        dom_raw, _, time_raw = expr.partition(" ")
        try:
            dom = int(dom_raw)
        except ValueError as e:
            raise SurveyError(f"每月设定应为「日 HH:MM」，实际收到：{expr}") from e
        if not 1 <= dom <= 31:
            raise SurveyError(f"日期取值应在 1 到 31 之间：{dom_raw}")
        hour, minute = _hhmm(time_raw.strip())
        return f"{minute} {hour} {dom} * *"

    raise SurveyError(f"未知的周期类型：{schedule_kind}")


def next_fire_time(
    schedule_kind: str,
    schedule_expr: str,
    tz_name: str,
    after: Optional[datetime] = None,
) -> datetime:
    """
    计算下一次触发时刻，返回 **naive UTC**（与库里其它时间字段一致）。

    入参 after 也按 naive UTC 处理。搜索在本地时区上进行，因为用户设定的
    "每周一 9 点"是本地语义；夏令时切换时用 fold=0 取先出现的那次，
    不为一年两次的边界额外造一套规则。
    """
    cron = to_cron(schedule_kind, schedule_expr)
    minutes, hours, doms, months, dows = parse_cron(cron)
    tz = resolve_timezone(tz_name)

    base_utc = (after or datetime.now(timezone.utc).replace(tzinfo=None)).replace(
        second=0, microsecond=0
    )
    cursor = base_utc.replace(tzinfo=timezone.utc).astimezone(tz) + timedelta(minutes=1)

    for _ in range(_MAX_SEARCH_MINUTES):
        # cron 的 0=周日，Python 的 weekday() 是 0=周一，isoweekday()%7 正好对上
        if (
            cursor.minute in minutes
            and cursor.hour in hours
            and cursor.month in months
            and cursor.day in doms
            and (cursor.isoweekday() % 7) in dows
        ):
            return cursor.astimezone(timezone.utc).replace(tzinfo=None)
        cursor += timedelta(minutes=1)

    raise SurveyError(f"该周期设定在一年内不会触发，请检查：{cron}")


def describe_schedule(schedule_kind: str, schedule_expr: str, tz_name: str) -> str:
    """给界面用的一句话描述。折算失败时原样回显，不要在展示层抛异常。"""
    try:
        cron = to_cron(schedule_kind, schedule_expr)
    except SurveyError as e:
        return f"（周期设定有误：{e}）"
    return f"{cron} [{str(tz_name or 'UTC').strip() or 'UTC'}]"


def weekday_names() -> List[str]:
    """界面上「每周」选择器的选项文案，索引 0 对应星期一。"""
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
