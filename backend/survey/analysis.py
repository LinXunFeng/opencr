#!/usr/bin/env python3
"""
巡检的分析三层：L1 整合 → L2 取证 → L3 汇总。

分层的理由是全量代码进不了上下文，而"一次整合分析"又必须让所有仓库同时在场。
折中办法是让**整合发生在画像层**（L1 同时看见全部仓库的结构摘要与跨仓库连接），
真实代码只在 L2 按 L1 点名的位置按需读取。

L2 按符号/行号精确取，而不是整文件读：实测符号平均 13-16 行、文件平均 71-244 行，
按行号取比按文件取省 5-18 倍的上下文。
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import openai

from ..review.config import load_openai_config
from ..review.skills import load_review_skill_previews, load_review_skill_prompts
from ..storage.models import (
    SEVERITY_ADVICE,
    SEVERITY_CRITICAL,
    SEVERITY_UNKNOWN,
    SEVERITY_WARNING,
    SURVEY_CATEGORIES,
)
from .common import SurveyError, normalize_category
from .crossrepo import render_cross_repo_map
from .profile import render_profile

logger = logging.getLogger(__name__)

_VALID_SEVERITIES = {SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_ADVICE}
# L2 读取代码时，在目标行上下各取多少行
FOCUS_CONTEXT_LINES = 60


class Budget:
    """
    一次 SurveyRun 的硬预算。

    没有它的话，一个每周自动跑的东西可以从 2 美元浮动到 200 美元，
    然后你第二周就会把它关掉。撞顶不是失败，是降级：已产出的照常保留。
    """

    def __init__(self, wall_clock_minutes: int, l1_max_chars: int, l2_max_focus: int,
                 l2_max_chars_per_focus: int):
        self.deadline = time.monotonic() + max(int(wall_clock_minutes), 1) * 60
        self.l1_max_chars = max(int(l1_max_chars), 1000)
        self.l2_max_focus = max(int(l2_max_focus), 1)
        self.l2_max_chars_per_focus = max(int(l2_max_chars_per_focus), 500)
        self.exhausted = False

    def time_left(self) -> float:
        """剩余墙钟秒数。"""
        return self.deadline - time.monotonic()

    def check(self) -> bool:
        """还有预算返回 True；一旦撞顶就永久置位，不会因为某次判断恰好通过而复活。"""
        if self.exhausted:
            return False
        if self.time_left() <= 0:
            self.exhausted = True
            logger.warning("Survey budget exhausted: wall clock deadline reached")
        return not self.exhausted


def _call_model(prompt: str, max_tokens: int = 4000) -> str:
    """
    调一次模型并返回文本。

    失败直接抛 SurveyError：吞掉调用错误会让"零发现"和"根本没跑起来"
    在界面上长得一模一样，那是最难排查的一类故障。
    """
    cfg = load_openai_config()
    if not cfg.get("api_key"):
        raise SurveyError("未配置 API Key，无法执行巡检分析")

    try:
        client = openai.OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
        kwargs = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        if cfg.get("reasoning_effort") and any(x in cfg["model"].lower() for x in ["o3", "o4"]):
            kwargs["reasoning_effort"] = cfg["reasoning_effort"]
        response = client.chat.completions.create(**kwargs)
    except Exception as e:
        raise SurveyError(f"模型调用失败：{e}") from e

    if not response.choices:
        raise SurveyError("模型返回空 choices")
    content = response.choices[0].message.content
    return str(content or "").strip()


def _parse_json_payload(text: str):
    """
    从模型输出里取出 JSON。

    模型时常把 JSON 包在 ```json 围栏里或前后加一句解释，这里做容错切分；
    实在解析不出来返回 None，由调用方决定是当作"零产出"还是报错。
    """
    raw = str(text or "").strip()
    if not raw:
        return None

    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, flags=re.DOTALL)
    if fence:
        raw = fence.group(1).strip()

    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass

    # 退而求其次：截取第一个 [ 或 { 到最后一个配对括号
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = raw.find(opener), raw.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(raw[start:end + 1])
            except (ValueError, TypeError):
                continue

    logger.warning("Failed to parse JSON from model output: %s", raw[:200])
    return None


def match_skills(profiles: List[dict], candidate_skills: List[str], skills_dir: str = "") -> List[str]:
    """
    按仓库画像挑选参与分析的 skill。

    全量巡检没有 diff 可看，匹配依据换成语言构成与依赖清单 ——
    一个纯 Dart 仓库不该被 ts skill 审一遍，那产出的是模型硬凑的假问题。
    候选池由 Survey 的勾选决定；这里只在池内挑，不会把取消勾选的捞回来。
    """
    candidates = sorted({str(s).strip() for s in candidate_skills if str(s).strip()})
    if not candidates:
        return []
    if len(candidates) == 1:
        return candidates

    previews = load_review_skill_previews(skills_dir=skills_dir)
    available = [name for name in candidates if name in previews]
    if not available:
        logger.warning("No skill previews for candidates=%s, skipping skill matching", candidates)
        return []

    digest_lines = []
    for profile in profiles:
        languages = ", ".join(f"{k}×{v}" for k, v in (profile.get("languages") or {}).items())
        manifests = ", ".join((profile.get("manifests") or {}).keys())
        digest_lines.append(
            f"- {profile.get('repo_slug')}：语言[{languages or '未知'}] 依赖清单[{manifests or '无'}]"
        )

    preview_text = "\n\n".join(f"[{name}]\n{previews[name]}" for name in available)
    prompt = f"""你是代码审查技能路由器。下面是本次巡检覆盖的仓库画像，请判断哪些 skill 适用。

要求：
1. 只能从候选 skill 中选择，可多选。
2. 没有明确匹配就输出空数组。
3. 只输出 JSON 数组，例如 ["flutter","ts"]，不要输出其它文字。

候选 skill：{", ".join(available)}

技能预览：
{preview_text}

仓库画像：
{chr(10).join(digest_lines)}
"""
    payload = _parse_json_payload(_call_model(prompt, max_tokens=256))
    if not isinstance(payload, list):
        logger.warning("Skill matching returned non-list, treating as no match")
        return []

    selected = [str(x).strip() for x in payload if str(x).strip() in available]
    logger.info("Survey skill matching: candidates=%s -> selected=%s", available, selected)
    return selected


def _build_l1_context(profiles: List[dict], cross_map: dict, max_chars: int) -> str:
    """
    拼装 L1 的输入：所有仓库的画像 + 跨仓库连接事实。

    按仓库均分预算而不是先到先得 —— 否则第一个大仓库会把额度吃光，
    后面的仓库在"整合分析"里根本不在场，那就不叫整合了。
    """
    cross_text = render_cross_repo_map(cross_map)
    remaining = max(max_chars - len(cross_text), 1000)
    per_repo = max(remaining // max(len(profiles), 1), 500)
    blocks = [render_profile(p, per_repo) for p in profiles]
    return cross_text + "\n\n" + "\n\n".join(blocks)


def plan_focus(
    profiles: List[dict],
    cross_map: dict,
    skill_prompt: str,
    budget: Budget,
) -> List[dict]:
    """
    L1 整合层：所有仓库同时在场，产出"需要深入看的位置"清单。

    这一层**不产出 Finding** —— 它看的是画像不是代码，让它直接下结论就是
    在鼓励它编造。它的职责是点名，取证交给 L2。
    """
    context = _build_l1_context(profiles, cross_map, budget.l1_max_chars)
    skill_block = f"\n\n参考以下审查技能：\n{skill_prompt}\n" if skill_prompt.strip() else ""

    prompt = f"""你是资深架构审查者。下面是一组仓库的结构画像与它们之间的接口连接事实。

请基于这些信息，找出**值得深入查看源码**的位置，输出关注点清单。

要求：
1. 你看到的是结构摘要，不是源码。**不要下结论**，只指出"这里可能有问题、需要看代码确认"。
2. 优先跨仓库问题（接口字段不一致、重复实现、依赖版本冲突）——这是单仓库审查看不到的部分。
3. 每个关注点必须给出具体的仓库与文件路径，文件路径必须来自上面出现过的路径。
4. category 只能从这个闭集里选：{", ".join(SURVEY_CATEGORIES)}
5. 最多输出 {budget.l2_max_focus} 条。
6. 只输出 JSON 数组，每项形如：
   {{"repo_slug":"...","file_path":"...","category":"...","reason":"为什么怀疑这里"}}
{skill_block}
以下是画像与连接事实：

{context}
"""
    payload = _parse_json_payload(_call_model(prompt, max_tokens=4000))
    if not isinstance(payload, list):
        logger.warning("L1 returned non-list payload, no focus produced")
        return []

    known_repos = {p.get("repo_slug") for p in profiles}
    focuses: List[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        repo_slug = str(item.get("repo_slug") or "").strip()
        file_path = str(item.get("file_path") or "").strip()
        if repo_slug not in known_repos or not file_path:
            # 模型偶尔会编出不存在的仓库或路径，这类条目直接丢掉而不是去猜它想说谁
            logger.info("Focus dropped (unknown repo or empty path): %s", item)
            continue
        focuses.append(
            {
                "repo_slug": repo_slug,
                "file_path": file_path,
                "category": normalize_category(item.get("category")),
                "reason": str(item.get("reason") or "").strip()[:1000],
            }
        )
        if len(focuses) >= budget.l2_max_focus:
            break

    logger.info("L1 produced %s focus items", len(focuses))
    return focuses


def _read_focus_source(repo_dir: Path, file_path: str, max_chars: int) -> str:
    """读取关注点对应的源码；路径不存在或越界时返回空串。"""
    try:
        target = (repo_dir / file_path).resolve()
        # 模型给出的路径是不可信输入，必须确认它没跳出工作区
        if not str(target).startswith(str(repo_dir.resolve())):
            logger.warning("Focus path escapes workspace, dropped: %s", file_path)
            return ""
        if not target.is_file():
            return ""
        text = target.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return text[:max_chars]


def inspect_focus(
    focus: dict,
    repo_dir: Path,
    skill_prompt: str,
    budget: Budget,
) -> List[dict]:
    """
    L2 取证层：读真实代码，产出结构化 Finding。

    这一层是唯一被允许下结论的地方 —— 它看得到源码。
    """
    source = _read_focus_source(repo_dir, focus["file_path"], budget.l2_max_chars_per_focus)
    if not source.strip():
        logger.info("Focus skipped (source unavailable): %s/%s", focus["repo_slug"], focus["file_path"])
        return []

    skill_block = f"\n\n参考以下审查技能：\n{skill_prompt}\n" if skill_prompt.strip() else ""
    prompt = f"""你是资深代码审查者。上一步的架构分析怀疑下面这个位置有问题，请读代码确认。

怀疑理由：{focus['reason']}
仓库：{focus['repo_slug']}
文件：{focus['file_path']}

要求：
1. **确认不了就返回空数组**。上一步只是怀疑，代码里没有实际问题时不要为了交差编一条。
2. category 只能从这个闭集里选：{", ".join(SURVEY_CATEGORIES)}
3. severity 只能是 critical / warning / advice 之一。
4. line 填问题所在行号（从 1 开始）；只能定位到文件时填 0。
5. 只输出 JSON 数组，每项形如：
   {{"line":12,"category":"...","severity":"...","title":"一句话标题","body":"问题描述与修复方案"}}
{skill_block}
以下是源码：

```
{source}
```
"""
    payload = _parse_json_payload(_call_model(prompt, max_tokens=3000))
    if not isinstance(payload, list):
        return []

    findings: List[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "").strip().lower()
        try:
            line = max(int(item.get("line") or 0), 0)
        except (TypeError, ValueError):
            line = 0
        findings.append(
            {
                "repo_slug": focus["repo_slug"],
                "file_path": focus["file_path"],
                "line": line,
                "category": normalize_category(item.get("category")),
                # 不做推断兜底：宁可如实显示 unknown，也不要编一个看起来合理的级别
                "severity": severity if severity in _VALID_SEVERITIES else SEVERITY_UNKNOWN,
                "title": str(item.get("title") or "").strip()[:500],
                "body": str(item.get("body") or "").strip()[:8000],
            }
        )
    return findings


def summarize(findings: List[dict], cross_map: dict, budget: Budget) -> str:
    """
    L3 汇总层：把逐条 Finding 收敛成一段跨仓库叙述。

    它吃的是 Finding 列表而不是原始 diff，量小得多，因此不需要单独的预算控制。
    汇总失败不抛错 —— 逐条 Finding 已经落库，缺一段叙述不该让整轮判失败。
    """
    if not findings:
        return ""
    if not budget.check():
        return ""

    digest = "\n".join(
        f"- [{f['category']}/{f['severity']}] {f['repo_slug']}/{f['file_path']}:{f['line']} {f['title']}"
        for f in findings[:200]
    )
    link_count = len(cross_map.get("links") or [])
    prompt = f"""下面是一次跨仓库巡检产出的全部问题条目，本次巡检共发现 {link_count} 处跨仓库接口连接。

请写一段简明的整体结论（Markdown，不超过 600 字），说明：
1. 这组仓库当前最值得优先处理的是什么
2. 是否存在跨仓库的系统性问题（而不只是单个仓库的局部缺陷）

不要逐条复述下面的列表，读者能直接看到它。

{digest}
"""
    try:
        return _call_model(prompt, max_tokens=1500)
    except SurveyError as e:
        logger.warning("L3 summary failed, continuing without it: %s", e)
        return ""


def load_skill_prompt(skill_names: List[str], skills_dir: str = "") -> str:
    """加载命中 skill 的提示词正文。巡检不执行 skill scripts —— 见 runner 的注释。"""
    if not skill_names:
        return ""
    return load_review_skill_prompts(skill_names, skills_dir=skills_dir, scripts_enabled=False)
