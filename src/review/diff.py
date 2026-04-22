#!/usr/bin/env python3
"""
Diff 处理工具
"""

import re
from typing import List


def truncate_diff(diff: str, max_chars: int = 50000) -> str:
    """智能截断 diff 内容"""
    if len(diff) <= max_chars:
        return diff

    file_pattern = r"diff --git a/(.+?) b/\1"
    files = re.split(file_pattern, diff)

    if len(files) <= 1:
        return diff[:max_chars] + "\n\n... (内容已截断)"

    result = []
    current_size = 0

    for i in range(1, len(files), 2):
        if i >= len(files):
            break

        filename = files[i]
        content = files[i + 1] if i + 1 < len(files) else ""

        file_header = f"diff --git a/{filename} b/{filename}\n"
        file_diff = file_header + content

        if current_size + len(file_diff) > max_chars:
            remaining = max_chars - current_size
            if remaining > 200:
                truncated = file_diff[:remaining] + "\n... (文件内容已截断)\n"
                result.append(truncated)
            break
        else:
            result.append(file_diff)
            current_size += len(file_diff)

    return "".join(result) + "\n\n... (更多文件未显示)"


def normalize_change_diff(change: dict) -> str:
    raw_diff = (change or {}).get("diff", "")
    if not raw_diff:
        return ""

    old_path = change.get("old_path") or change.get("new_path") or "unknown"
    new_path = change.get("new_path") or change.get("old_path") or old_path

    if raw_diff.lstrip().startswith("diff --git"):
        return raw_diff

    return f"diff --git a/{old_path} b/{new_path}\n{raw_diff}"


def build_diff_from_changes(changes: List[dict]) -> str:
    parts: List[str] = []
    for change in changes or []:
        normalized = normalize_change_diff(change)
        if normalized:
            parts.append(normalized)
    return "\n".join(parts)
