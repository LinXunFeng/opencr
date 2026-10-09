#!/usr/bin/env python3
"""
巡检的分析三层：L1 整合 → L2 取证 → L3 汇总。

分层的理由是全量代码进不了上下文，而"一次整合分析"又必须让所有仓库同时在场。
折中办法是让**整合发生在画像层**（L1 同时看见全部仓库的结构摘要与跨仓库连接），
真实代码只在 L2 按 L1 点名的位置按需读取。

L2 按符号/行号精确取，而不是整文件读：实测符号平均 13-16 行、文件平均 71-244 行，
按行号取比按文件取省 5-18 倍的上下文。

L2 的取证对象除了 L1 点名的位置，还有台账里仍为"存在"的问题所在的文件（复核）。
L1 每轮点名的文件都不一样，不复核的话，上一轮的问题下一轮只是没被看到，看起来却像消失了。
"""

import json
import logging
import posixpath
import re
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

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
    per_repo = l1_repo_budget(len(profiles), len(cross_text), max_chars)
    blocks = [render_profile(p, per_repo) for p in profiles]
    return cross_text + "\n\n" + "\n\n".join(blocks)


def l1_repo_budget(repo_count: int, cross_chars: int, max_chars: int) -> int:
    """
    L1 输入里每个仓库的画像分到的字符数：扣掉跨仓库连接后按仓库均分。

    单独成函数是为了让 Reach 统计（reach.py）与 L1 实际拿到的输入用同一套分配，
    两边各算一遍迟早会算出不一样的截断位置。
    """
    remaining = max(max_chars - cross_chars, 1000)
    return max(remaining // max(repo_count, 1), 500)


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
        file_path = normalize_file_path(item.get("file_path"))
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


class FocusSource(NamedTuple):
    """关注点要交给 L2 的源码，以及这份源码能否支撑"没看到就是不存在"的结论。"""

    text: str
    # True：整份文件都在 text 里。只有这时"模型没报"才算本轮看过、没发现。
    # 按行号截取的摘录也不算：上方插进几十行代码，问题就被挤出了窗口，
    # 模型看不到它自然不会报 —— 那正是"没去看却判成没有"的老问题。
    # 代价是超长文件里已经修好的问题会一直停在"存在"，这比把没修的问题报成消失更可接受
    complete: bool
    # 摘录模式下每行带了"行号| "前缀，提示词要告诉模型按前缀报行号
    numbered: bool = False
    # 文件已经不在仓库里：问题随文件一起没了，本身就是确定的结论
    missing: bool = False


def _excerpt_around(lines: List[str], anchors: List[int], max_chars: int) -> str:
    """
    按锚点行截取上下文窗口，返回带行号前缀的摘录；一个窗口都放不下时返回空串。

    窗口按锚点顺序加入，放不下就停：只放进一半的窗口会让模型看到一个断掉的函数。
    """
    windows: List[List[int]] = []
    for anchor in sorted(set(anchors)):
        if anchor > len(lines):
            # 文件变短了，原来的行号已经不存在，交给模型看文件尾部也无从比对
            continue
        start = max(anchor - FOCUS_CONTEXT_LINES, 1)
        end = min(anchor + FOCUS_CONTEXT_LINES, len(lines))
        if windows and start <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])

    parts: List[str] = []
    used = 0
    for start, end in windows:
        block = "\n".join(f"{n:>6}| {lines[n - 1]}" for n in range(start, end + 1))
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n   ...\n".join(parts)


def normalize_file_path(raw) -> str:
    """
    把模型给出的文件路径规范成仓库内的相对 POSIX 路径；规范不出来时返回空串。

    指纹与复核都按路径字面比对：同一个文件写成 "./src/a.py" 与 "src/a.py"，
    会被取证两次，产出的指纹也对不上台账里原有的那一行，一个问题就变成了两行。
    """
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return ""
    path = posixpath.normpath(text).lstrip("/")
    return "" if path in ("", ".") else path


def focus_key(focus: dict) -> Tuple[str, str]:
    """
    关注点的文件身份：(仓库, 规整后的路径)。

    合并取证顺序（merge_focuses）与判定"这个文件是不是 L1 点名的"（clues.annotate_focuses）
    必须用同一个身份：两边口径不一，L1 点名的文件就会被误记成台账复核。
    """
    return (focus.get("repo_slug", ""), normalize_file_path(focus.get("file_path", "")))


def _read_focus_source(
    repo_dir: Path, file_path: str, max_chars: int, anchor_lines: Optional[List[int]] = None
) -> Optional[FocusSource]:
    """
    读取关注点对应的源码。

    路径越界或读取出错时返回 None —— 这一轮对这个文件没有结论。
    文件放得下就整份给；放不下且有锚点行（纯复核）就按行号截取上下文，让模型有机会再次报出旧问题，
    否则退回取开头 max_chars 个字符。放不下的两种情况都不算完整结论。
    """
    try:
        root = repo_dir.resolve()
        target = (repo_dir / file_path).resolve()
        # 模型给出的路径是不可信输入，必须确认它没跳出工作区
        if target != root and root not in target.parents:
            logger.warning("Focus path escapes workspace, dropped: %s", file_path)
            return None
        if not target.exists():
            return FocusSource(text="", complete=True, missing=True)
        if not target.is_file():
            return None
        text = target.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    if len(text) <= max_chars:
        return FocusSource(text=text, complete=True)
    if anchor_lines:
        excerpt = _excerpt_around(text.splitlines(), list(anchor_lines), max_chars)
        if excerpt:
            return FocusSource(text=excerpt, complete=False, numbered=True)
    return FocusSource(text=text[:max_chars], complete=False)


def merge_focuses(rechecks: List[dict], planned: List[dict], pending: Dict[Tuple[str, str], dict]) -> List[dict]:
    """
    把复核清单与 L1 点名的关注点合成本轮的取证顺序。

    - 同一个文件只取证一次：L1 点名了复核清单里的文件时合并成一条，怀疑理由一起带上；
    - L1 点名了仍有"存在"问题、但没进复核清单的文件时，把那些问题挂上去（见 plan_rechecks）；
    - 只有纯复核才按旧问题的行号截取源码：L1 怀疑的位置可能在别处，截取会把它截掉，
      这类关注点按原来的方式从文件开头读，读不全就不算有结论；
    - 文件身份按规范化后的路径比对（台账里可能存着旧版本没规范化的写法），取证用台账里的原文，
      产出的指纹才对得上原来那一行；
    - 两边交替排列：预算按墙钟计，撞顶时排在后面的整批落空，
      全放前面会让台账大的巡检永远发现不了新问题，全放后面则复核形同虚设。
    """
    planned_by_file: Dict[Tuple[str, str], dict] = {}
    for focus in planned:
        key = focus_key(focus)
        current = planned_by_file.get(key)
        if current is None:
            planned_by_file[key] = {**focus, "reason": _tagged_reason(focus)}
        else:
            # L1 常对同一文件按不同类别点名多次，只取第一条会让其余怀疑永远进不了 L2，
            # 文件却被判成有结论，漏查的那几类看起来就像"看过、没有问题"
            current["reason"] = f"{current['reason']}\n{_tagged_reason(focus)}"
    pending_by_file = {focus_key(item): item for item in pending.values()}

    recheck_items: List[dict] = []
    recheck_keys = set()
    for item in rechecks:
        key = focus_key(item)
        recheck_keys.add(key)
        twin = planned_by_file.get(key) or {}
        fallback_category = item["previous"][0]["category"] if item["previous"] else ""
        recheck_items.append({
            "repo_slug": item["repo_slug"],
            "file_path": item["file_path"],
            "category": twin.get("category") or fallback_category,
            "reason": twin.get("reason") or "",
            "previous": item["previous"],
            "anchor_lines": [] if twin else item["anchor_lines"],
        })

    planned_items: List[dict] = []
    for key, focus in planned_by_file.items():
        if key in recheck_keys:
            continue
        extra = pending_by_file.get(key)
        planned_items.append({
            **focus,
            # 沿用台账里的路径原文，理由同上
            "file_path": extra["file_path"] if extra else focus["file_path"],
            "previous": extra["previous"] if extra else [],
            "anchor_lines": [],
        })

    merged: List[dict] = []
    for index in range(max(len(recheck_items), len(planned_items))):
        if index < len(recheck_items):
            merged.append(recheck_items[index])
        if index < len(planned_items):
            merged.append(planned_items[index])
    return merged


def _tagged_reason(focus: dict) -> str:
    """带类别前缀的怀疑理由，合并同一文件的多条点名时用来区分。"""
    return f"[{focus.get('category') or '未分类'}] {focus.get('reason') or ''}".strip()


def _previous_block(previous: List[dict]) -> str:
    """复核时交给 L2 的"上一轮报过的问题"清单。"""
    if not previous:
        return ""
    rows = []
    for item in previous:
        lines = ", ".join(str(n) for n in item.get("lines") or []) or "未定位到行"
        rows.append(f"- [{item.get('category')}/{item.get('severity')}] 第 {lines} 行：{item.get('title')}")
    return (
        "\n\n此前的巡检在这个文件里报告过下面这些问题，请逐条复核：\n"
        + "\n".join(rows)
        + "\n仍然存在的照常输出，category 必须沿用上面的原值（同一个问题换了类别会被当成另一个问题）；"
        "已经不存在的不要输出。其他新发现的问题照常输出。\n"
    )


def inspect_focus(
    focus: dict,
    repo_dir: Path,
    skill_prompt: str,
    budget: Budget,
) -> Tuple[List[dict], bool]:
    """
    L2 取证层：读真实代码，产出结构化 Finding。返回 (Finding 列表, 本轮对该文件是否有可信结论)。

    这一层是唯一被允许下结论的地方 —— 它看得到源码。
    "有可信结论"要求源码完整（见 FocusSource.complete）且模型给出了可解析的输出；
    模型输出解析失败时同样返回空列表，但不能算作"看过、没有问题"。
    """
    source = _read_focus_source(
        repo_dir, focus["file_path"], budget.l2_max_chars_per_focus, focus.get("anchor_lines") or None
    )
    if source is None:
        logger.info("Focus skipped (source unavailable): %s/%s", focus["repo_slug"], focus["file_path"])
        return [], False
    if source.missing or not source.text.strip():
        # 文件已删除或是空文件：上面不可能有问题，不必花一次模型调用
        return [], True

    reason = focus.get("reason") or "复核此前巡检报告过的问题"
    numbered_note = (
        "\n注意：文件过长，下面只给出相关片段，每行开头的数字是它在文件里的行号，line 请按这个行号填写。\n"
        if source.numbered else ""
    )
    skill_block = f"\n\n参考以下审查技能：\n{skill_prompt}\n" if skill_prompt.strip() else ""
    prompt = f"""你是资深代码审查者。上一步的架构分析怀疑下面这个位置有问题，请读代码确认。

怀疑理由：{reason}
仓库：{focus['repo_slug']}
文件：{focus['file_path']}
{_previous_block(focus.get("previous") or [])}
要求：
1. **确认不了就返回空数组**。上一步只是怀疑，代码里没有实际问题时不要为了交差编一条。
2. category 只能从这个闭集里选：{", ".join(SURVEY_CATEGORIES)}
3. severity 只能是 critical / warning / advice 之一。
4. line 填问题所在行号（从 1 开始）；只能定位到文件时填 0。
5. 只输出 JSON 数组，每项形如：
   {{"line":12,"category":"...","severity":"...","title":"一句话标题","body":"问题描述与修复方案"}}
{skill_block}{numbered_note}
以下是源码：

```
{source.text}
```
"""
    payload = _parse_json_payload(_call_model(prompt, max_tokens=3000))
    if not isinstance(payload, list):
        logger.info("Focus produced unparseable output: %s/%s", focus["repo_slug"], focus["file_path"])
        return [], False

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
    return findings, source.complete


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
