#!/usr/bin/env python3
"""
巡检配置加载：默认值 + config.yaml + 环境变量覆盖。

沿用 backend/review/config.py 的三层取值顺序，不另起一套 —— 运维只需要记住一套规则。
"""

import logging
import os
from pathlib import Path

from ..review.config import _pick_config_int, _pick_config_value, load_file_config

logger = logging.getLogger(__name__)

# 预算默认值。这几个数字来自 codegraph spike 的实测：
# 三个仓库（约 187k 行）的组合画像约 43 万字符，因此 l1_max_chars 取 40 万，
# 略低于它 —— 宁可截断一次并记降级，也不要让单次调用撞上模型的硬上限后整轮失败。
DEFAULT_WALL_CLOCK_MINUTES = 60
DEFAULT_L1_MAX_CHARS = 400000
DEFAULT_L2_MAX_FOCUS = 20
DEFAULT_L2_MAX_CHARS_PER_FOCUS = 20000
DEFAULT_INDEX_TIMEOUT_SECONDS = 600
DEFAULT_FETCH_TIMEOUT_SECONDS = 600
DEFAULT_SCHEDULER_INTERVAL_SECONDS = 60


def _truthy(value, default: bool) -> bool:
    """空值取默认，其余按常见的假值词判定。"""
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "no", "off", "disabled"}


def _resolve_default_workspace_dir() -> Path:
    """默认工作区根目录：优先安装目录 ~/opencr/workspaces，其次项目根 workspaces/。"""
    installed = Path.home() / "opencr"
    if installed.is_dir():
        return installed / "workspaces"
    # backend/survey/config.py -> backend -> 项目根
    return Path(__file__).resolve().parent.parent.parent / "workspaces"


def load_survey_config() -> dict:
    """读取巡检配置。"""
    config_data = load_file_config()

    enabled_raw = _pick_config_value(config_data, "survey.enabled", "OPENCR_SURVEY_ENABLED")
    workspace_dir = _pick_config_value(config_data, "survey.workspace_dir", "OPENCR_SURVEY_WORKSPACE_DIR")
    scheduler_interval = _pick_config_int(
        config_data,
        DEFAULT_SCHEDULER_INTERVAL_SECONDS,
        "survey.scheduler_interval_seconds",
        "OPENCR_SURVEY_SCHEDULER_INTERVAL_SECONDS",
    )
    codegraph_enabled_raw = _pick_config_value(
        config_data, "survey.codegraph_enabled", "OPENCR_SURVEY_CODEGRAPH_ENABLED"
    )
    codegraph_bin = _pick_config_value(config_data, "survey.codegraph_bin", "OPENCR_SURVEY_CODEGRAPH_BIN")

    wall_clock_minutes = _pick_config_int(
        config_data, DEFAULT_WALL_CLOCK_MINUTES, "survey.budget.wall_clock_minutes"
    )
    l1_max_chars = _pick_config_int(config_data, DEFAULT_L1_MAX_CHARS, "survey.budget.l1_max_chars")
    l2_max_focus = _pick_config_int(config_data, DEFAULT_L2_MAX_FOCUS, "survey.budget.l2_max_focus")
    l2_max_chars_per_focus = _pick_config_int(
        config_data, DEFAULT_L2_MAX_CHARS_PER_FOCUS, "survey.budget.l2_max_chars_per_focus"
    )
    index_timeout = _pick_config_int(
        config_data, DEFAULT_INDEX_TIMEOUT_SECONDS, "survey.budget.index_timeout_seconds"
    )
    fetch_timeout = _pick_config_int(
        config_data, DEFAULT_FETCH_TIMEOUT_SECONDS, "survey.budget.fetch_timeout_seconds"
    )

    env_workspace_dir = os.getenv("OPENCR_SURVEY_WORKSPACE_DIR", "").strip()
    if env_workspace_dir:
        workspace_dir = env_workspace_dir
    env_codegraph_bin = os.getenv("OPENCR_SURVEY_CODEGRAPH_BIN", "").strip()
    if env_codegraph_bin:
        codegraph_bin = env_codegraph_bin

    resolved = {
        "enabled": _truthy(enabled_raw, True),
        "workspace_dir": str(Path(workspace_dir).expanduser()) if workspace_dir else str(_resolve_default_workspace_dir()),
        # 下限 10 秒：调度线程本身极轻（一次带索引的 SELECT），但没有下限的话
        # 一个手滑填的 0 会变成忙等，把 SQLite 的锁竞争推上去。
        "scheduler_interval_seconds": max(scheduler_interval, 10),
        "codegraph_enabled": _truthy(codegraph_enabled_raw, True),
        "codegraph_bin": codegraph_bin or "codegraph",
        "budget": {
            "wall_clock_minutes": max(wall_clock_minutes, 1),
            "l1_max_chars": max(l1_max_chars, 1000),
            "l2_max_focus": max(l2_max_focus, 1),
            "l2_max_chars_per_focus": max(l2_max_chars_per_focus, 500),
            "index_timeout_seconds": max(index_timeout, 10),
            "fetch_timeout_seconds": max(fetch_timeout, 30),
        },
    }
    logger.info(
        "Survey config resolved: enabled=%s, workspace_dir=%s, scheduler_interval=%s, "
        "codegraph_enabled=%s, codegraph_bin=%s, budget=%s",
        resolved["enabled"],
        resolved["workspace_dir"],
        resolved["scheduler_interval_seconds"],
        resolved["codegraph_enabled"],
        resolved["codegraph_bin"],
        resolved["budget"],
    )
    return resolved


def resolve_budget(survey: dict) -> dict:
    """
    合并全局默认与单个 Survey 的覆盖项。

    覆盖项为 None 表示"跟随全局"，而不是 0 —— 三个小仓库的巡检和一个 monorepo
    的巡检合理预算能差一个量级，但绝大多数人不会去调，全局默认必须够用。
    """
    defaults = load_survey_config()["budget"]
    overrides = {
        "wall_clock_minutes": survey.get("budget_wall_clock_minutes"),
        "l1_max_chars": survey.get("budget_l1_max_chars"),
        "l2_max_focus": survey.get("budget_l2_max_focus"),
        "index_timeout_seconds": survey.get("budget_index_timeout_seconds"),
    }
    merged = dict(defaults)
    for key, value in overrides.items():
        if value is not None and int(value) > 0:
            merged[key] = int(value)
    return merged
