#!/usr/bin/env python3
"""
Skill 路由与提示词加载
"""

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import openai

from .common import (
    REVIEW_MODE_OVERALL,
    normalize_review_mode,
    normalize_review_skill,
)
from .config import load_openai_config, load_review_config
from .diff import normalize_change_diff

logger = logging.getLogger(__name__)
SKILL_PREVIEW_CHARS = 240


def resolve_review_options(
    data: dict,
    default_skill: str = "",
    manual_mode: str = "",
    manual_skill: str = "",
) -> Tuple[str, str]:
    """解析最终审查模式与 skill"""
    # 参数 data 保留用于兼容旧签名
    _ = data
    final_mode = normalize_review_mode(manual_mode or REVIEW_MODE_OVERALL)
    final_skill = normalize_review_skill(manual_skill or default_skill)
    logger.info(
        "Review options resolved: base_mode=%s, manual_mode=%s -> final_mode=%s; "
        "default_skill=%s, manual_skill=%s -> final_skill=%s",
        REVIEW_MODE_OVERALL,
        manual_mode or "<empty>",
        final_mode,
        default_skill,
        manual_skill or "<empty>",
        final_skill,
    )
    return final_mode, final_skill


def _resolve_skills_dir(skills_dir: str) -> Path:
    configured_path = Path(skills_dir).expanduser()
    if configured_path.is_absolute():
        return configured_path

    project_root = Path(__file__).resolve().parents[2]
    cwd_path = Path.cwd() / configured_path
    if cwd_path.exists():
        return cwd_path
    return project_root / configured_path


def _split_skill_meta_and_body(raw_text: str) -> Tuple[Dict[str, str], str]:
    """
    解析 Markdown 前置 meta（YAML front matter）并返回 (meta, body)。
    仅支持简单 `key: value` 结构；无 meta 时返回 ({}, 原文)。
    """
    text = raw_text or ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()

    end_index = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_index = i
            break
    if end_index < 0:
        return {}, text.strip()

    meta: Dict[str, str] = {}
    for line in lines[1:end_index]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*?)\s*$", stripped)
        if not match:
            continue
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if value:
            meta[key] = value

    body = "\n".join(lines[end_index + 1:]).strip()
    return meta, body


def _build_preview_from_meta(meta: Dict[str, str], max_chars: int) -> str:
    """
    从 meta 字段构建路由预览文本。
    没有可用 meta 信息时返回空字符串。
    """
    if not meta:
        return ""

    keys = ["name", "description", "summary", "scope", "scene", "stack", "language", "tags", "match"]
    parts = []
    for key in keys:
        value = (meta.get(key) or "").strip()
        if value:
            parts.append(f"{key}:{value}")

    if not parts:
        return ""

    cleaned = " ".join(" ".join(parts).split())
    return cleaned[:max_chars].strip()


def load_review_skill_prompt(skill_name: str, skills_dir: str = "") -> str:
    """读取指定审查 skill 提示词；加载失败返回空字符串。"""
    safe_skill_name = normalize_review_skill(skill_name)
    if not safe_skill_name:
        return ""
    review_cfg = load_review_config()
    resolved_dir = _resolve_skills_dir(skills_dir or review_cfg["skills_dir"])

    requested_file = resolved_dir / f"{safe_skill_name}.md"
    if not requested_file.exists() or not requested_file.is_file():
        logger.warning("Skill prompt file not found: skill=%s, file=%s", safe_skill_name, requested_file)
        return ""

    try:
        raw = requested_file.read_text(encoding="utf-8")
        _meta, body = _split_skill_meta_and_body(raw)
        if not body:
            logger.warning("Skill prompt body empty: skill=%s, file=%s", safe_skill_name, requested_file)
            return ""
        return body
    except Exception as e:
        logger.warning(f"Failed to read review skill file {requested_file}: {e}")
        return ""


def load_review_skill_prompts(skill_names: List[str], skills_dir: str = "") -> str:
    """读取多个 skill 提示词并拼接。"""
    unique_names: List[str] = []
    seen = set()
    for name in skill_names or []:
        safe_name = normalize_review_skill(name)
        if safe_name and safe_name not in seen:
            unique_names.append(safe_name)
            seen.add(safe_name)

    if not unique_names:
        return ""

    blocks: List[str] = []
    for name in unique_names:
        prompt = load_review_skill_prompt(name, skills_dir=skills_dir)
        if not prompt.strip():
            logger.warning("Skill prompt skipped (empty): skill=%s", name)
            continue
        blocks.append(f"[{name}]\n{prompt}")

    return "\n\n".join(blocks)


def load_available_review_skills(skills_dir: str = "") -> Dict[str, str]:
    """加载可用审查 skill（name -> prompt_content）"""
    review_cfg = load_review_config()
    resolved_dir = _resolve_skills_dir(skills_dir or review_cfg["skills_dir"])
    skills: Dict[str, str] = {}

    if not resolved_dir.exists() or not resolved_dir.is_dir():
        return skills

    for path in sorted(resolved_dir.glob("*.md")):
        skill_name = normalize_review_skill(path.stem)
        if not skill_name:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            _meta, body = _split_skill_meta_and_body(raw)
            if body:
                skills[skill_name] = body
        except Exception as e:
            logger.warning(f"Failed to read skill file {path}: {e}")

    return skills


def load_review_skill_previews(skills_dir: str = "", max_chars: int = SKILL_PREVIEW_CHARS) -> Dict[str, str]:
    """加载所有 skill 的简要预览（name -> preview）。"""
    review_cfg = load_review_config()
    resolved_dir = _resolve_skills_dir(skills_dir or review_cfg["skills_dir"])
    previews: Dict[str, str] = {}

    if not resolved_dir.exists() or not resolved_dir.is_dir():
        return previews

    skipped_missing_meta = 0
    skipped_empty_preview = 0

    for path in sorted(resolved_dir.glob("*.md")):
        skill_name = normalize_review_skill(path.stem)
        if not skill_name:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to read skill preview {path}: {e}")
            continue

        meta, _body = _split_skill_meta_and_body(raw)
        if not meta:
            skipped_missing_meta += 1
            logger.info("Skill preview skipped (missing meta): %s", path.name)
            continue

        preview = _build_preview_from_meta(meta, max_chars=max_chars)
        if preview:
            previews[skill_name] = preview
            logger.info("SkillMatch preview included: skill=%s, file=%s", skill_name, path.name)
        else:
            skipped_empty_preview += 1
            logger.info("Skill preview skipped (empty meta preview): %s", path.name)

    logger.info(
        "SkillMatch previews summary: included=%s, skipped_missing_meta=%s, skipped_empty_preview=%s, dir=%s",
        len(previews),
        skipped_missing_meta,
        skipped_empty_preview,
        str(resolved_dir),
    )

    return previews


def _load_skill_content_exact(skill_name: str, skills_dir: str = "") -> str:
    """只读取指定 skill 文件内容，不做 fallback。"""
    safe_skill_name = normalize_review_skill(skill_name)
    review_cfg = load_review_config()
    resolved_dir = _resolve_skills_dir(skills_dir or review_cfg["skills_dir"])
    skill_file = resolved_dir / f"{safe_skill_name}.md"
    if not skill_file.exists() or not skill_file.is_file():
        return ""
    try:
        raw = skill_file.read_text(encoding="utf-8")
        _meta, body = _split_skill_meta_and_body(raw)
        return body
    except Exception as e:
        logger.warning(f"Failed to read skill file {skill_file}: {e}")
        return ""


def _build_skill_selection_context(changes: List[dict], max_chars: int = 12000) -> str:
    file_paths: List[str] = []
    diff_snippets: List[str] = []
    current_size = 0

    for change in changes or []:
        file_path = change.get("new_path") or change.get("old_path") or "unknown"
        file_paths.append(file_path)
        normalized_diff = normalize_change_diff(change)
        if not normalized_diff:
            continue

        remaining = max_chars - current_size
        if remaining <= 0:
            break

        snippet = normalized_diff[:remaining]
        diff_snippets.append(snippet)
        current_size += len(snippet)

    path_block = "\n".join(f"- {p}" for p in file_paths[:200]) or "- (no files)"
    diff_block = "\n\n".join(diff_snippets) if diff_snippets else "(no diff content)"

    return f"""### Changed Files
{path_block}

### Diff Excerpts
{diff_block}
"""


def _parse_selected_skill(raw_output: str, allowed_skills: List[str], fallback_skill: str) -> str:
    cleaned = (raw_output or "").strip().lower()
    if cleaned in allowed_skills:
        return cleaned

    match = re.search(r"[a-z0-9_-]+", cleaned)
    if match:
        candidate = match.group(0)
        if candidate in allowed_skills:
            return candidate

    return fallback_skill


def _parse_selected_skills(
    raw_output: str,
    allowed_skills: List[str],
    max_count: Optional[int] = None,
) -> List[str]:
    """解析多选 skill。未命中返回空列表。"""
    cleaned = (raw_output or "").strip().lower()
    if not cleaned:
        return []

    candidates = re.findall(r"[a-z0-9_-]+", cleaned)
    selected: List[str] = []
    seen = set()

    for token in candidates:
        if token in allowed_skills and token not in seen:
            selected.append(token)
            seen.add(token)
        if max_count is not None and len(selected) >= max_count:
            break

    return selected


def auto_select_review_skill(
    changes: List[dict],
    fallback_skill: str = "",
    skills_dir: str = "",
) -> str:
    selected = auto_select_review_skills(
        changes=changes,
        fallback_skill=fallback_skill,
        skills_dir=skills_dir,
        max_count=1,
    )
    return selected[0] if selected else ""


def auto_select_review_skills(
    changes: List[dict],
    fallback_skill: str = "",
    skills_dir: str = "",
    max_count: Optional[int] = None,
) -> List[str]:
    """
    基于 skill 描述与本次变更自动选择多个审查 skill（按优先级）。
    若无法判定或调用失败，返回空列表（表示不参与本轮审查）。
    """
    logger.info(
        "Auto skill selection start: changes=%s, fallback_skill=%s, skills_dir=%s, max_count=%s",
        len(changes or []),
        fallback_skill,
        skills_dir or "<auto>",
        max_count if max_count is not None else "<unlimited>",
    )
    skill_previews = load_review_skill_previews(skills_dir=skills_dir)
    if not skill_previews:
        logger.warning("SkillMatch result: hit=0, reason=no_skill_previews, scope=auto_select")
        return []

    # 只有一个 skill 时无需路由
    if len(skill_previews) == 1:
        only_skill = next(iter(skill_previews.keys()))
        logger.info("SkillMatch result: hit=1, reason=single_skill, selected=%s", only_skill)
        return [only_skill]

    cfg = load_openai_config()
    if not cfg.get("api_key"):
        logger.warning("SkillMatch result: hit=0, reason=missing_api_key, scope=auto_select")
        return []

    skill_options = sorted(skill_previews.keys())
    logger.info(
        "Auto skill selection candidates(preview): count=%s, options=%s",
        len(skill_options),
        ",".join(skill_options),
    )
    preview_desc = "\n\n".join([f"[{name}]\n{skill_previews[name]}" for name in skill_options])
    context = _build_skill_selection_context(changes)

    shortlist_prompt = f"""你是代码审查技能路由器。
请先基于 skill 的“预览摘要”筛选最可能匹配的候选 skill。

要求：
1. 只能从候选 skill 中选择匹配项，可选择多个。
2. 如果没有明确匹配，请输出空字符串。
3. 仅输出 skill 名称，使用英文逗号分隔，不要输出其它文字。

候选 skill：
{", ".join(skill_options)}

技能预览摘要：
{preview_desc}

代码变更：
{context}
"""

    try:
        client = openai.OpenAI(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
        )
        shortlist_resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "user", "content": shortlist_prompt}],
            max_tokens=256,
        )
        shortlist_raw = ""
        if shortlist_resp.choices and shortlist_resp.choices[0].message:
            shortlist_raw = str(shortlist_resp.choices[0].message.content or "").strip()
        logger.info("Auto skill selection shortlist raw output: %s", shortlist_raw or "<empty>")

        shortlisted = _parse_selected_skills(
            shortlist_raw,
            skill_options,
        )
        logger.info("Auto skill selection shortlist parsed: %s", ",".join(shortlisted) or "<empty>")
        if not shortlisted:
            logger.info("SkillMatch result: hit=0, reason=shortlist_empty, scope=auto_select")
            return []

        # 第二阶段：仅加载候选 skill 的完整正文进行最终选择（两阶段按需加载）
        shortlisted_contents: Dict[str, str] = {}
        for skill_name in shortlisted:
            content = _load_skill_content_exact(skill_name, skills_dir=skills_dir)
            if content:
                shortlisted_contents[skill_name] = content

        if not shortlisted_contents:
            logger.warning("SkillMatch result: hit=0, reason=shortlist_content_empty, scope=auto_select")
            return []
        if len(shortlisted_contents) == 1:
            selected = next(iter(shortlisted_contents.keys()))
            logger.info("SkillMatch result: hit=1, reason=single_shortlist, selected=%s", selected)
            return [selected]

        final_options = sorted(shortlisted_contents.keys())
        logger.info("Auto skill selection final candidates(full text): %s", ",".join(final_options))
        final_desc = "\n\n".join([f"[{name}]\n{shortlisted_contents[name]}" for name in final_options])
        final_prompt = f"""你是代码审查技能路由器。
请严格依据“技能描述（完整正文）”判断本次变更最适合哪些 skill（按优先级）。

要求：
1. 只能从候选 skill 中选择匹配项，可选择多个。
2. 如果没有明确匹配，请输出空字符串。
3. 仅输出 skill 名称，使用英文逗号分隔，不要输出其它文字。

候选 skill：
{", ".join(final_options)}

技能完整描述：
{final_desc}

代码变更：
{context}
"""
        final_resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "user", "content": final_prompt}],
            max_tokens=256,
        )
        final_raw = ""
        if final_resp.choices and final_resp.choices[0].message:
            final_raw = str(final_resp.choices[0].message.content or "").strip()
        selected_skills = _parse_selected_skills(
            final_raw,
            final_options,
            max_count=max_count,
        )
        logger.info(
            "SkillMatch result: hit=%s, reason=final_select, selected=%s",
            1 if selected_skills else 0,
            ",".join(selected_skills) or "<empty>",
        )
        return selected_skills
    except Exception as e:
        logger.warning(f"SkillMatch result: hit=0, reason=exception, error={e}")
        return []
