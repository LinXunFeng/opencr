#!/usr/bin/env python3
"""
跨仓库接口连接：把各仓库画像里的「提供的接口」与「调用的接口」对起来。

这一层**必须由我们自己写**，不能用 codegraph 的跨仓库边 —— 见
docs/adr/0003-per-repo-codegraph-index.md：它的跨仓库边是按符号名连的，
实测 100% 是假边。这里连的是 HTTP 路径字面量，是确定性的字符串匹配，
跑一万次结果一样，且**不产生任何模型判断** —— 判断留给 L1。
"""

import logging
import re
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# 路径参数的各种写法：:id（gin/express）、{id}（spring/openapi）、<id>（flask）、
# ${id} 与 $id（前端模板串）。统一折算成 * 才能对上。
_PARAM_PATTERNS = (
    re.compile(r"\$\{[^}]*\}"),
    re.compile(r"\{[^}]*\}"),
    re.compile(r"<[^>]*>"),
    re.compile(r"(?<=/):[^/]+"),
    re.compile(r"(?<=/)\$[A-Za-z_][A-Za-z0-9_]*"),
)


def normalize_path(path: str) -> str:
    """
    把接口路径规整成可比较的形式。

    只做参数占位与尾斜杠的归一，不做大小写折叠 —— HTTP 路径是大小写敏感的，
    折叠会把 /api/User 和 /api/user 误判成同一个接口。
    """
    text = str(path or "").strip()
    if not text:
        return ""
    for pattern in _PARAM_PATTERNS:
        text = pattern.sub("*", text)
    text = re.sub(r"/{2,}", "/", text)
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def build_cross_repo_map(profiles: List[dict]) -> dict:
    """
    汇总所有仓库的接口供需关系。

    返回四类事实（都是确定性的，没有任何推断）：
    - links：一个仓库调用了另一个仓库提供的接口
    - method_mismatch：路径对上了但 HTTP 方法对不上
    - orphan_calls：调用了本次巡检范围内没有任何仓库提供的接口
    - unused_routes：提供了但本次巡检范围内没有任何仓库调用的接口
    """
    providers: Dict[str, List[dict]] = {}
    consumers: Dict[str, List[dict]] = {}

    for profile in profiles or []:
        repo = profile.get("repo_slug", "?")
        for route in profile.get("routes") or []:
            key = normalize_path(route.get("path", ""))
            if not key:
                continue
            providers.setdefault(key, []).append({**route, "repo": repo})
        for call in profile.get("api_calls") or []:
            key = normalize_path(call.get("path", ""))
            if not key:
                continue
            consumers.setdefault(key, []).append({**call, "repo": repo})

    links: List[dict] = []
    method_mismatch: List[dict] = []
    orphan_calls: List[dict] = []

    for key, calls in sorted(consumers.items()):
        routes = providers.get(key)
        if not routes:
            for call in calls:
                orphan_calls.append({"path": key, **call})
            continue

        for call in calls:
            for route in routes:
                if route["repo"] == call["repo"]:
                    # 同仓库内的自调用不是跨仓库信息，L1 不需要它
                    continue
                link = {
                    "path": key,
                    "from_repo": call["repo"],
                    "from_file": call.get("file", ""),
                    "from_line": call.get("line", 0),
                    "to_repo": route["repo"],
                    "to_file": route.get("file", ""),
                    "handler": route.get("handler", ""),
                    "handler_file": route.get("handler_file", ""),
                    "call_method": call.get("method", ""),
                    "route_method": route.get("method", ""),
                }
                links.append(link)
                # 调用侧的方法常常抽不到（`fetch(url, { method: "POST" })` 里
                # 方法在下一行），抽不到时不报不匹配，宁可漏报也不要制造假问题
                if link["call_method"] and link["route_method"] and link["call_method"] != link["route_method"]:
                    method_mismatch.append(link)

    linked_route_keys = {link["path"] for link in links}
    unused_routes = [
        {"path": key, **route}
        for key, routes in sorted(providers.items())
        if key not in linked_route_keys
        for route in routes
    ]

    summary = {
        "links": links,
        "method_mismatch": method_mismatch,
        "orphan_calls": orphan_calls,
        "unused_routes": unused_routes,
    }
    logger.info(
        "Cross-repo map: links=%s, method_mismatch=%s, orphan_calls=%s, unused_routes=%s",
        len(links), len(method_mismatch), len(orphan_calls), len(unused_routes),
    )
    return summary


def render_cross_repo_map(cross_map: dict, max_items: int = 60) -> str:
    """把跨仓库事实渲染成 L1 的输入文本。"""
    parts: List[str] = ["### 跨仓库接口连接（确定性匹配，非模型推断）"]

    links = cross_map.get("links") or []
    if links:
        lines = [
            f"- `{l['path']}`：{l['from_repo']}（{l['from_file']}:{l['from_line']}）"
            f" → {l['to_repo']} 的 {l['handler'] or '?'}（{l['handler_file'] or l['to_file']}）"
            for l in links[:max_items]
        ]
        parts.append(f"已连接（{len(links)}）：\n" + "\n".join(lines))

    mismatches = cross_map.get("method_mismatch") or []
    if mismatches:
        lines = [
            f"- `{m['path']}`：{m['from_repo']} 用 {m['call_method']}，"
            f"{m['to_repo']} 注册的是 {m['route_method']}"
            for m in mismatches[:max_items]
        ]
        parts.append(f"HTTP 方法不一致（{len(mismatches)}）：\n" + "\n".join(lines))

    orphans = cross_map.get("orphan_calls") or []
    if orphans:
        lines = [
            f"- `{o['path']}`：{o['repo']}（{o.get('file','')}:{o.get('line',0)}）"
            for o in orphans[:max_items]
        ]
        parts.append(
            f"调用了本次巡检范围内无人提供的接口（{len(orphans)}）"
            f"——注意可能是第三方接口或不在本巡检内的服务，不要据此直接断言缺陷：\n"
            + "\n".join(lines)
        )

    unused = cross_map.get("unused_routes") or []
    if unused:
        lines = [
            f"- `{u['path']}`：{u['repo']}（{u.get('file','')}）"
            for u in unused[:max_items]
        ]
        parts.append(
            f"提供了但本次范围内无人调用的接口（{len(unused)}）"
            f"——注意调用方可能不在本巡检内，不要据此直接断言是死接口：\n"
            + "\n".join(lines)
        )

    if len(parts) == 1:
        parts.append("（未发现跨仓库的接口连接）")
    return "\n\n".join(parts)
