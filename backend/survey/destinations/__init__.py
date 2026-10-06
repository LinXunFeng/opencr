#!/usr/bin/env python3
"""
Destination（输出目标）注册表与实例配置解析。

插件内置在代码仓库里、在这里显式注册，不从目录动态加载：
每个平台都要处理鉴权、限流与字段映射，写插件的人本来就要改代码；
动态加载带来的依赖安装与安全边界问题，眼下没有任何人需要。

新增一种平台：实现 base.Destination，然后加进 DESTINATION_TYPES。
"""

import logging
import re
from typing import Dict, List, Type

from ...review.config import load_file_config
from .base import Destination, DestinationError, DestinationInfo
from .google_sheet import GoogleSheetDestination

logger = logging.getLogger(__name__)

DESTINATION_TYPES: Dict[str, Type[Destination]] = {
    cls.type_name: cls for cls in (GoogleSheetDestination,)
}

# 实例名会存进 survey_binding.destination（String(64)），也会出现在后台下拉框里
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def load_destinations() -> Dict[str, DestinationInfo]:
    """
    解析 config.yaml 的 `destinations` 段，返回 {实例名: DestinationInfo}。

    单个实例配置有误时记在 error 里而不是抛出：一个写错的实例不该让其余实例、
    乃至整个后台的巡检配置页面都不可用。
    """
    raw = load_file_config().get("destinations")
    if not isinstance(raw, dict):
        return {}

    result: Dict[str, DestinationInfo] = {}
    for name, item in raw.items():
        name = str(name).strip()
        options = dict(item) if isinstance(item, dict) else {}
        type_name = str(options.pop("type", "") or "").strip()
        info = DestinationInfo(name=name, type_name=type_name, options=options)

        if not _NAME_PATTERN.match(name):
            info.error = "实例名只能包含字母、数字、下划线、点与短横线，且不超过 64 个字符"
        elif type_name not in DESTINATION_TYPES:
            known = "、".join(sorted(DESTINATION_TYPES)) or "（无）"
            info.error = f"未知的输出目标类型：{type_name or '（未填写）'}；可用类型：{known}"
        else:
            try:
                DESTINATION_TYPES[type_name].validate_options(options)
            except DestinationError as e:
                info.error = str(e)

        if info.error:
            logger.warning("Destination %s misconfigured: %s", name, info.error)
        result[name] = info
    return result


def build_destination(name: str) -> Destination:
    """按实例名构造 Destination。未配置或配置有误时抛 DestinationError。"""
    info = load_destinations().get(name)
    if info is None:
        raise DestinationError(f"输出目标未配置：{name}（config.yaml 的 destinations 中没有这一项）")
    if info.error:
        raise DestinationError(f"输出目标 {name} 配置有误：{info.error}")
    return DESTINATION_TYPES[info.type_name](name, info.options)


def destination_type_of(name: str) -> str:
    """实例对应的类型名；未配置时返回空串。"""
    info = load_destinations().get(name)
    return info.type_name if info else ""


def list_destination_types() -> List[dict]:
    """全部已注册的类型，供后台展示。"""
    return [
        {
            "type": cls.type_name,
            "label": cls.label,
            "target_fields": [f.to_dict() for f in cls.target_fields],
        }
        for cls in DESTINATION_TYPES.values()
    ]


__all__ = [
    "DESTINATION_TYPES",
    "Destination",
    "DestinationError",
    "DestinationInfo",
    "build_destination",
    "destination_type_of",
    "list_destination_types",
    "load_destinations",
]
