#!/usr/bin/env python3
"""
把一次 SurveyRun 渲染成 Markdown 报告。

存储侧是结构化的（逐条 Finding 入表），Markdown 是**渲染产物**而不是真相：
反过来存全文的话，"新增 / 仍存在 / 已消失"的比对就没法做 ——
从两篇 Markdown 里算不出可靠的差集。
"""

from typing import Dict, List

from ..storage.models import (
    CATEGORY_ARCHITECTURE,
    CATEGORY_CONVENTION,
    CATEGORY_CORRECTNESS,
    CATEGORY_CROSS_REPO,
    CATEGORY_DEPENDENCY,
    CATEGORY_MAINTAINABILITY,
    CATEGORY_PERFORMANCE,
    CATEGORY_SECURITY,
    SEVERITY_ADVICE,
    SEVERITY_CRITICAL,
    SEVERITY_UNKNOWN,
    SEVERITY_WARNING,
)

CATEGORY_LABELS: Dict[str, str] = {
    CATEGORY_CORRECTNESS: "正确性",
    CATEGORY_SECURITY: "安全",
    CATEGORY_CROSS_REPO: "跨仓库不一致",
    CATEGORY_ARCHITECTURE: "架构与耦合",
    CATEGORY_PERFORMANCE: "性能",
    CATEGORY_MAINTAINABILITY: "可维护性",
    CATEGORY_DEPENDENCY: "依赖",
    CATEGORY_CONVENTION: "规范与风格",
}

SEVERITY_LABELS: Dict[str, str] = {
    SEVERITY_CRITICAL: "严重",
    SEVERITY_WARNING: "警告",
    SEVERITY_ADVICE: "建议",
    SEVERITY_UNKNOWN: "未知",
}

DEGRADATION_LABELS: Dict[str, str] = {
    "repo_fetch_failed": "仓库拉取失败（该仓库未参与本次分析）",
    "index_failed": "代码索引失败（该仓库的画像退化为依赖清单级）",
    "budget_exhausted": "预算耗尽，提前收工（部分关注点未取证）",
    "profile_fallback": "codegraph 不可用，全部画像退化为依赖清单级",
}

_SEVERITY_ORDER = [SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_ADVICE, SEVERITY_UNKNOWN]


def _render_findings(findings: List[dict], include_body: bool) -> str:
    """按严重度分组渲染发现列表。"""
    if not findings:
        return "_（无）_"

    grouped: Dict[str, List[dict]] = {}
    for item in findings:
        grouped.setdefault(item.get("severity", SEVERITY_UNKNOWN), []).append(item)

    blocks: List[str] = []
    for severity in _SEVERITY_ORDER:
        bucket = grouped.get(severity)
        if not bucket:
            continue
        blocks.append(f"**{SEVERITY_LABELS.get(severity, severity)}（{len(bucket)}）**")
        for item in bucket:
            location = f"`{item.get('repo_slug','')}/{item.get('file_path','')}`"
            line = item.get("line") or 0
            if line:
                location += f":{line}"
            category = CATEGORY_LABELS.get(item.get("category", ""), item.get("category", ""))
            title = item.get("title") or ""
            if include_body and title:
                blocks.append(f"- [{category}] {location} —— {title}")
                body = (item.get("body") or "").strip()
                if body:
                    indented = "\n".join(f"  > {line}" for line in body.splitlines())
                    blocks.append(indented)
            else:
                # Guest 视角：位置与分类是聚合信息，正文才是 ADR-0002 要挡的东西
                blocks.append(f"- [{category}] {location}")
    return "\n".join(blocks)


def render_run_markdown(detail: dict) -> str:
    """把 get_survey_run_detail 的结果渲染成一篇完整报告。"""
    include_body = bool(detail.get("body_included", True))
    counts = detail.get("counts") or {}
    lines: List[str] = [
        f"# 巡检报告：{detail.get('survey_name') or '未命名'}",
        "",
        f"- 运行编号：`{detail.get('run_uid','')}`",
        f"- 触发方式：{'手动' if detail.get('trigger') == 'manual' else '定时'}",
        f"- 状态：{detail.get('status','')}",
        f"- 开始时间：{detail.get('started_at','')}",
        f"- 结束时间：{detail.get('finished_at','') or '（未结束）'}",
        f"- 命中技能：{', '.join(detail.get('matched_skills') or []) or '（无）'}",
        "",
        f"本次共 **{counts.get('total', 0)}** 条发现："
        f"新增 **{counts.get('new', 0)}**、"
        f"仍存在 **{counts.get('persisted', 0)}**、"
        f"较上次已消失 **{counts.get('resolved', 0)}**。",
        "",
    ]

    degradations = detail.get("degradations") or []
    if degradations:
        lines.append("## 本次运行的降级")
        lines.append("")
        lines.append("以下情况**不代表运行失败**，但产出质量受到了影响：")
        lines.append("")
        for item in degradations:
            kind = item.get("kind", "")
            label = DEGRADATION_LABELS.get(kind, kind)
            lines.append(f"- {label} ×{item.get('count', 0)}")
        lines.append("")

    repos = detail.get("repos") or []
    if repos:
        lines.append("## 覆盖的仓库")
        lines.append("")
        lines.append("| 仓库 | 分支 | 提交 | 状态 | 画像 | 文件数 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for r in repos:
            lines.append(
                f"| {r.get('repo_slug','')} | {r.get('branch','') or '默认'} | "
                f"`{r.get('commit_sha','') or '-'}` | {r.get('status','')} | "
                f"{r.get('profile_kind','') or '-'} | {r.get('file_count', 0)} |"
            )
        lines.append("")

    summary = (detail.get("summary") or "").strip()
    if summary:
        lines.extend(["## 整体结论", "", summary, ""])

    findings = detail.get("findings") or []
    new_items = [f for f in findings if f.get("state") == "new"]
    persisted_items = [f for f in findings if f.get("state") == "persisted"]
    resolved_items = detail.get("resolved_findings") or []

    lines.extend(["## 新增", "", _render_findings(new_items, include_body), ""])
    lines.extend(["## 仍存在", "", _render_findings(persisted_items, include_body), ""])
    lines.extend([
        "## 较上次已消失",
        "",
        "_以下问题在上一次巡检中出现过，本次未再检出。_",
        "",
        _render_findings(resolved_items, include_body),
        "",
    ])

    if not include_body:
        lines.extend([
            "---",
            "",
            "_当前身份为游客，报告中不含发现正文与整体结论。_",
        ])

    return "\n".join(lines)
