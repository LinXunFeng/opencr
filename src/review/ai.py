#!/usr/bin/env python3
"""
AI 审查调用与聚合
"""

import json
import logging
import re
from typing import Dict, List, Tuple

import openai

from .common import (
    REVIEW_MODE_FILE,
    REVIEW_MODE_HYBRID,
    REVIEW_MODE_OVERALL,
    ReviewError,
    normalize_review_mode,
)
from .config import load_openai_config
from .diff import build_diff_from_changes, normalize_change_diff, truncate_diff
from .skills import auto_select_review_skills, load_review_skill_prompts

logger = logging.getLogger(__name__)


def build_review_prompt(
    diff: str,
    review_mode: str = REVIEW_MODE_OVERALL,
    file_path: str = "",
    skill_prompt: str = "",
    skill_name: str = "",
) -> str:
    """构建审查提示词"""
    normalized_mode = normalize_review_mode(review_mode)
    mode_text = "按文件审查" if normalized_mode == REVIEW_MODE_FILE else "整 MR 审查"
    file_context = f"- 当前文件：`{file_path}`\n" if file_path else ""
    use_skill_rules = bool(skill_prompt.strip())

    if use_skill_rules:
        review_rules_block = f"""
## 场景 Skill（{skill_name}）

请严格按以下 Skill 规则进行审查，不要追加项目默认审查规则：

{skill_prompt}
"""
    else:
        review_rules_block = """
## 审查重点

1. **Bug 与逻辑错误**
   - 空指针/未定义变量
   - 逻辑条件错误
   - 资源泄漏

2. **安全问题**
   - SQL 注入、XSS
   - 敏感信息硬编码
   - 不安全的文件操作

3. **性能问题**
   - 重复计算
   - 不必要的循环
   - 内存泄漏风险

4. **代码质量**
   - 可读性
   - 可维护性
"""

    return f"""你是一位经验丰富的代码审查专家。请对以下代码变更进行审查。

## 审查上下文

- 当前审核模式：{mode_text}
{file_context}

{review_rules_block}

## 输出格式

请按以下 Markdown 格式输出：

### 总体评价
简要评价本次变更的质量

### 详细审查结果
如有问题，按以下格式列出：

#### 问题 : [标题]
- **位置**: `文件路径:行号`
- **级别**: 🔴 严重 / 🟡 警告 / 🟢 建议
- **描述**: 具体问题
- **建议**: 修复方案

### 如无问题
回复："✅ 代码审查通过，未发现明显问题。"

---

```diff
{diff}
```
"""


def call_codex_review(
    diff: str,
    review_mode: str = REVIEW_MODE_OVERALL,
    file_path: str = "",
    skill_prompt: str = "",
    skill_name: str = "",
) -> str:
    """调用 Codex/OpenAI API 进行代码审查"""
    cfg = load_openai_config()

    if not cfg["api_key"]:
        raise ReviewError("未配置 API Key")

    logger.info(f"Calling API: model={cfg['model']}, base_url={cfg['base_url']}")
    logger.info(
        "Prompt strategy: %s, skill_name=%s",
        "skill_only" if skill_prompt.strip() else "default_rules",
        skill_name or "<empty>",
    )

    try:
        client = openai.OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])

        messages = [{
            "role": "user",
            "content": build_review_prompt(
                diff,
                review_mode=review_mode,
                file_path=file_path,
                skill_prompt=skill_prompt,
                skill_name=skill_name,
            ),
        }]

        kwargs = {
            "model": cfg["model"],
            "messages": messages,
            "max_tokens": 4000,
        }

        # o-series 模型支持 reasoning_effort
        if cfg["reasoning_effort"] and any(x in cfg["model"].lower() for x in ["o3", "o4"]):
            kwargs["reasoning_effort"] = cfg["reasoning_effort"]

        response = client.chat.completions.create(**kwargs)
        logger.debug(f"API raw response: {response}")

        if not response.choices or len(response.choices) == 0:
            raise ReviewError("API 返回空 choices")

        message = response.choices[0].message
        content = message.content

        if content is None:
            if hasattr(message, "reasoning_content") and message.reasoning_content:
                content = message.reasoning_content
            elif hasattr(message, "text") and message.text:
                content = message.text
            else:
                content = (
                    "API 返回格式异常，原始响应: "
                    f"{json.dumps(response.model_dump(), ensure_ascii=False, default=str)[:2000]}"
                )

        logger.info(f"API response: {len(str(content))} characters")
        return content

    except Exception as e:
        logger.exception("API call failed")
        raise ReviewError(f"API 调用失败: {str(e)}")


def _parse_file_review_issues(review_text: str, default_file_path: str) -> List[Dict[str, object]]:
    """
    从单文件审查文本中提取问题块，输出结构化行内评论候选。
    仅提取能识别到行号的问题。
    """
    if not review_text or "问题" not in review_text:
        return []

    header_pattern = re.compile(r"(?m)^####\s*问题(?:\s*\d+)?\s*[:：]\s*.*$")
    headers = list(header_pattern.finditer(review_text))
    if not headers:
        return []

    findings: List[Dict[str, object]] = []
    for idx, header in enumerate(headers):
        start = header.start()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(review_text)
        block = review_text[start:end].strip()
        if not block:
            continue

        # 行内评论中统一去掉问题序号：`问题 4:` -> `问题 :`
        block = re.sub(
            r"(?m)^(\s*####\s*问题)(?:\s*\d+)?\s*[:：]\s*",
            r"\1 : ",
            block,
            count=1,
        )

        # 兼容常见位置格式：
        # - **位置**: `path/to/file.dart:123`
        # - **位置**: path/to/file.dart:123
        # - **位置**: `file.dart:L123`
        pos_match = re.search(
            r"位置\*\*\s*:\s*`?([^\n:`]+(?:/[^\n:`]+)*)\s*:\s*L?(\d+)`?",
            block,
            flags=re.IGNORECASE,
        )
        if pos_match:
            file_path = pos_match.group(1).strip()
            line = int(pos_match.group(2))
        else:
            # 若未显式给出文件名，则退回当前文件并尝试抓行号
            line_match = re.search(r"位置\*\*\s*:\s*[^\n]*?L?(\d+)", block, flags=re.IGNORECASE)
            if not line_match:
                continue
            file_path = default_file_path
            line = int(line_match.group(1))

        if line <= 0:
            continue

        findings.append(
            {
                "file_path": file_path or default_file_path,
                "line": line,
                "body": block,
            }
        )

    return findings


def review_changes_with_inline_notes(
    changes: List[dict],
    review_mode: str,
    review_skill: str,
    max_diff_size: int,
    skills_dir: str = "",
) -> Tuple[str, List[Dict[str, object]]]:
    """按审查模式执行审查，并返回可用于行内评论的结构化问题列表。"""
    normalized_mode = normalize_review_mode(review_mode)
    total_changes = len(changes or [])
    logger.info(
        "Review execution start: input_mode=%s, normalized_mode=%s, total_changes=%s, requested_skill=%s",
        review_mode,
        normalized_mode,
        total_changes,
        review_skill,
    )
    sections: List[str] = []
    inline_notes: List[Dict[str, object]] = []

    run_overall = normalized_mode in {REVIEW_MODE_OVERALL, REVIEW_MODE_HYBRID}
    run_file = normalized_mode in {REVIEW_MODE_FILE, REVIEW_MODE_HYBRID}

    if run_overall:
        overall_skills = auto_select_review_skills(
            changes,
            fallback_skill=review_skill,
            skills_dir=skills_dir,
        )
        logger.info(
            "SkillMatch scope=overall hit=%s selected=%s",
            1 if overall_skills else 0,
            ",".join(overall_skills) or "<empty>",
        )
        if not overall_skills:
            logger.info("Review execution OVERALL skipped: no matched skills")
        else:
            overall_skill_prompt = load_review_skill_prompts(overall_skills, skills_dir=skills_dir)
            if not overall_skill_prompt.strip():
                logger.info("Review execution OVERALL skipped: matched skills but prompt content empty")
                overall_skill_prompt = ""
                continue_overall = False
            else:
                continue_overall = True
            combined_diff = build_diff_from_changes(changes)
            if continue_overall and combined_diff:
                truncated = truncate_diff(combined_diff, max_chars=max_diff_size)
                logger.info(
                    "Review execution OVERALL: combined_diff_chars=%s, truncated_chars=%s",
                    len(combined_diff),
                    len(truncated),
                )
                overall_result = call_codex_review(
                    truncated,
                    review_mode=REVIEW_MODE_OVERALL,
                    skill_prompt=overall_skill_prompt,
                    skill_name=",".join(overall_skills),
                )
                sections.append(overall_result)
            elif continue_overall:
                logger.warning("Review execution OVERALL: combined diff empty")

    if run_file:
        logger.info("Review execution FILE: will review per changed file with auto-matched skills")
        reviewed_files = 0
        file_total = 0
        file_hit = 0
        file_miss = 0
        for index, change in enumerate(changes, start=1):
            normalized_diff = normalize_change_diff(change)
            if not normalized_diff:
                continue

            file_total += 1
            file_path = change.get("new_path") or change.get("old_path") or f"file-{index}"
            file_skills = auto_select_review_skills(
                [change],
                fallback_skill=review_skill,
                skills_dir=skills_dir,
            )
            logger.info(
                "SkillMatch scope=file file=%s hit=%s selected=%s",
                file_path,
                1 if file_skills else 0,
                ",".join(file_skills) or "<empty>",
            )
            if not file_skills:
                file_miss += 1
                logger.info("Review execution FILE skipped for %s: no matched skills", file_path)
                continue
            file_skill_prompt = load_review_skill_prompts(file_skills, skills_dir=skills_dir)
            if not file_skill_prompt.strip():
                logger.info("Review execution FILE skipped for %s: matched skills but prompt content empty", file_path)
                file_miss += 1
                continue
            reviewed_files += 1
            file_hit += 1
            truncated = truncate_diff(normalized_diff, max_chars=max_diff_size)
            result = call_codex_review(
                truncated,
                review_mode=REVIEW_MODE_FILE,
                file_path=file_path,
                skill_prompt=file_skill_prompt,
                skill_name=",".join(file_skills),
            )
            logger.info(
                "Review execution FILE progress: index=%s, file=%s, skills=%s",
                index,
                file_path,
                ",".join(file_skills),
            )

            inline_notes.extend(_parse_file_review_issues(result, default_file_path=file_path))

        if reviewed_files:
            logger.info(
                "SkillMatch summary scope=file total=%s hit=%s miss=%s reviewed_files=%s",
                file_total,
                file_hit,
                file_miss,
                reviewed_files,
            )
        else:
            logger.warning(
                "SkillMatch summary scope=file total=%s hit=%s miss=%s reviewed_files=%s",
                file_total,
                file_hit,
                file_miss,
                reviewed_files,
            )

    # 去重（同文件+同行+同正文）
    deduped: List[Dict[str, object]] = []
    seen = set()
    for item in inline_notes:
        key = (str(item.get("file_path")), int(item.get("line", 0)), str(item.get("body")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    logger.info("Review execution inline notes extracted: count=%s", len(deduped))
    if not sections:
        logger.info("Review execution completed without MR summary section")
        return "", deduped

    return "\n\n---\n\n".join(sections), deduped


def review_changes(
    changes: List[dict],
    review_mode: str,
    review_skill: str,
    max_diff_size: int,
    skills_dir: str = "",
) -> str:
    """兼容接口：仅返回审查文本。"""
    content, _inline_notes = review_changes_with_inline_notes(
        changes=changes,
        review_mode=review_mode,
        review_skill=review_skill,
        max_diff_size=max_diff_size,
        skills_dir=skills_dir,
    )
    return content
