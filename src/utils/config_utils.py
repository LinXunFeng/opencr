#!/usr/bin/env python3
"""
通用配置解析工具。

该模块提供“轻量且无第三方依赖”的配置读取能力，主要用于：
1. 在无 PyYAML 的环境中做基础 YAML 解析；
2. 通过多路径兜底读取配置值，并统一为字符串/整数类型。
"""

import re
from typing import Any, Dict


def parse_yaml_scalar(value: str) -> Any:
    """
    解析 YAML 标量（简化版）。

    支持的类型：
    - 带引号字符串（单/双引号）
    - 布尔值 true/false
    - 整数（含负数）
    其余内容按原始字符串返回。
    """
    raw = value.strip()
    if not raw:
        return ""

    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]

    lower = raw.lower()
    if lower in {"true", "false"}:
        return lower == "true"

    if re.fullmatch(r"-?\d+", raw):
        try:
            return int(raw)
        except ValueError:
            return raw

    return raw


def parse_basic_yaml(content: str) -> Dict[str, Any]:
    """
    简化 YAML 解析（仅支持 key/value 与对象嵌套）。
    当运行环境没有 PyYAML 时作为后备解析器。

    说明：
    - 仅处理最常见的“缩进字典”结构；
    - 不支持数组、复杂多行结构、锚点等高级 YAML 语法。
    """
    root: Dict[str, Any] = {}
    # 栈元素： (当前层级缩进, 当前层级对象)
    stack = [(-1, root)]

    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        match = re.match(r"^(\s*)([A-Za-z0-9_.-]+)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue

        indent = len(match.group(1))
        key = match.group(2)
        raw_value = match.group(3)

        # 当前缩进小于等于栈顶时，回退到正确父层级。
        while stack and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1] if stack else root
        if not isinstance(parent, dict):
            continue

        if raw_value == "":
            nested: Dict[str, Any] = {}
            parent[key] = nested
            stack.append((indent, nested))
        else:
            # 处理行内注释：foo: bar # comment
            # 若值本身是引号字符串，则不切分，避免误伤内容里的 #。
            if " #" in raw_value and not raw_value.strip().startswith(("'", '"')):
                raw_value = raw_value.split(" #", 1)[0].rstrip()
            parent[key] = parse_yaml_scalar(raw_value)

    return root


def pick_config_value(config_data: dict, *paths: str) -> str:
    """
    按优先级顺序读取配置值。

    `paths` 支持点路径（如 `openai.model`）；命中首个非空值即返回。
    返回值统一转为字符串，便于后续环境变量/配置合并逻辑复用。
    """
    for path in paths:
        current = config_data
        found = True

        for key in path.split("."):
            if not isinstance(current, dict) or key not in current:
                found = False
                break
            current = current.get(key)

        if not found or current is None:
            continue

        if isinstance(current, str):
            value = current.strip()
            if value:
                return value
            continue

        # YAML 中可能为数字/布尔类型，统一转为字符串
        return str(current)

    return ""


def pick_config_int(config_data: dict, default: int, *paths: str) -> int:
    """读取整型配置；缺失或转换失败时返回 `default`。"""
    raw = pick_config_value(config_data, *paths)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default
