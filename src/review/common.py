#!/usr/bin/env python3
"""
公共常量与异常定义
"""

import re

REVIEW_MODE_OVERALL = "overall"
REVIEW_MODE_FILE = "file"
REVIEW_MODE_HYBRID = "hybrid"
DEFAULT_REVIEW_SKILLS_DIR = "skills/review"


class ReviewError(Exception):
    """审查过程中的错误"""


def normalize_review_mode(mode: str) -> str:
    normalized = (mode or "").strip().lower()
    if normalized in {"hybrid", "both", "all", "overall+file", "overall_file"}:
        return REVIEW_MODE_HYBRID
    if normalized in {"file", "files", "by_file", "by-file", "per_file", "per-file"}:
        return REVIEW_MODE_FILE
    return REVIEW_MODE_OVERALL


def normalize_review_skill(skill_name: str) -> str:
    raw = (skill_name or "").strip().lower()
    if not raw:
        return ""
    # 仅允许简单名称，避免路径穿越
    if not re.fullmatch(r"[a-z0-9_-]+", raw):
        return ""
    return raw
