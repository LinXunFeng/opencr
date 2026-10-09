#!/usr/bin/env python3
"""
线索来源：判定一个关注点的位置是画像里的哪一部分指给模型的，并汇总 codegraph 的本轮产出。

它回答的是"codegraph 在这一轮起了什么作用"，因此只做**可观察的出处判定**，不做因果归因：
一个线索来自 codegraph 的关注点，在没有 codegraph 时模型也可能凭目录名猜到同一个文件。
真要量因果只能做对照实验（同一批提交分别开关 codegraph 各跑几轮），这里不假装能算出来。

本模块是纯函数，不碰数据库与网络。
"""

from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..storage.models import (
    CLUE_BASELINE,
    CLUE_CODEGRAPH,
    CLUE_SOURCES,
    CLUE_UNLISTED,
    CODEGRAPH_DISABLED,
    CODEGRAPH_ENABLED,
    CODEGRAPH_MISSING,
    PROFILE_CODEGRAPH,
)


def normalize_file_path(path: str) -> str:
    """
    把文件路径规整成仓库内的相对形式再比较。

    模型给出的路径偶尔带 `./` 或前导斜杠，codegraph 存的是不带前缀的相对路径
    （实测 1.6.0：`src/api/server.ts`）。不规整的话同一个文件会被判成"画像外"。
    """
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _paths(values: Iterable[str]) -> Set[str]:
    """规整一组路径并去掉空值。"""
    return {p for p in (normalize_file_path(v) for v in values) if p}


def codegraph_files(profile: dict) -> Set[str]:
    """画像里只有 codegraph 才能提供的文件：路由注册处、路由处理函数、类型定义。"""
    files: List[str] = []
    for route in profile.get("routes") or []:
        files.extend([route.get("file", ""), route.get("handler_file", "")])
    files.extend(t.get("file", "") for t in profile.get("types") or [])
    return _paths(files)


def baseline_files(profile: dict) -> Set[str]:
    """
    没有 codegraph 也会出现在画像里的文件：调用侧、依赖清单、仓库顶层文件。

    调用侧要连同被剔除的路由注册一起算：不开 codegraph 时那些行原本就在调用侧里，
    只因为 codegraph 认出了它们是路由才被剔掉 —— 漏算的话，后端的路由文件会全被记成 codegraph 的功劳。
    目录结构摘要里只有目录名与顶层文件名，目录不算"指向了某个文件"。
    """
    calls = list(profile.get("api_calls") or []) + list(profile.get("dropped_calls") or [])
    files: List[str] = [c.get("file", "") for c in calls]
    files.extend((profile.get("manifests") or {}).keys())
    for entry in profile.get("tree") or []:
        # 目录条目形如 "lib/" 或 "lib/ (a, b)"，一定含斜杠；顶层文件名不可能含斜杠
        name = str(entry)
        if "/" not in name:
            files.append(name)
    return _paths(files)


def _clue_index(profile: Optional[dict]) -> Tuple[Set[str], Set[str]]:
    """一个仓库画像的 (基础画像文件集, codegraph 文件集)，供逐个关注点判定时复用。"""
    if not profile:
        return set(), set()
    return baseline_files(profile), codegraph_files(profile)


def _classify(index: Tuple[Set[str], Set[str]], file_path: str) -> str:
    """
    按预先算好的文件集判定线索来源。

    两边都出现时判为 baseline 而不是 codegraph：既然不靠 codegraph 也看得到，
    就不能把它记成 codegraph 的功劳 —— 宁可低估收益，也不要高估。
    """
    baseline_set, codegraph_set = index
    target = normalize_file_path(file_path)
    if target in baseline_set:
        return CLUE_BASELINE
    if target in codegraph_set:
        return CLUE_CODEGRAPH
    return CLUE_UNLISTED


def classify_clue(profile: Optional[dict], file_path: str) -> str:
    """判定一个位置在给定仓库画像里的线索来源；没有画像时为画像外。"""
    return _classify(_clue_index(profile), file_path)


def annotate_focuses(focuses: List[dict], profiles: List[dict]) -> List[dict]:
    """给每个关注点标上线索来源（原地修改并返回同一列表）。"""
    # 文件集按仓库算一次：画像可能有上千个类型，逐个关注点重算是 O(关注点 × 画像)
    indexes = {p.get("repo_slug"): _clue_index(p) for p in profiles or []}
    for focus in focuses or []:
        index = indexes.get(focus.get("repo_slug")) or (set(), set())
        focus["clue_source"] = _classify(index, focus.get("file_path", ""))
    return focuses


def _empty_counts() -> Dict[str, int]:
    """线索来源计数的初始形状，保证三个键始终存在，前端不必判空。"""
    return {source: 0 for source in CLUE_SOURCES}


def _run_status(profiles: List[dict]) -> str:
    """
    本轮 codegraph 的状态，从画像上的记录推出，而不是再查一次 codegraph_status()：
    要描述的是本轮建画像那一刻的事实，部署环境在运行中途变了也不该改写它。
    """
    statuses = {p.get("codegraph_status") for p in profiles or []}
    if CODEGRAPH_ENABLED in statuses:
        return CODEGRAPH_ENABLED
    if CODEGRAPH_MISSING in statuses:
        return CODEGRAPH_MISSING
    return CODEGRAPH_DISABLED


def summarize_codegraph(
    profiles: List[dict],
    cross_map: dict,
    focuses: Optional[List[dict]] = None,
) -> dict:
    """
    汇总 codegraph 本轮的产出与去向，作为运行级快照落库。

    focuses 为 None 表示 L1 还没跑（或跑挂了），此时 focus 字段为 None 而不是全 0 ——
    "没有关注点"和"还没到这一步"在页面上要能区分。
    """
    structured = [p for p in profiles or [] if p.get("kind") == PROFILE_CODEGRAPH]
    focus_counts: Optional[Dict[str, int]] = None
    if focuses is not None:
        focus_counts = _empty_counts()
        for focus in focuses:
            source = focus.get("clue_source") or CLUE_UNLISTED
            focus_counts[source] = focus_counts.get(source, 0) + 1

    status = _run_status(profiles)
    cross_map = cross_map or {}
    return {
        "status": status,
        "available": status == CODEGRAPH_ENABLED,
        "repos_total": len(profiles or []),
        "repos_structured": len(structured),
        # 索引建成了却一条路由、一个类型都没抽出来：可能是 codegraph 不支持该仓库的语言，
        # 也可能仓库里本来就没有。画像类型照样显示"结构图"，不单独点出来就会被误读成一切正常。
        "repos_empty": sum(1 for p in structured if not p.get("routes") and not p.get("types")),
        "routes": sum(len(p.get("routes") or []) for p in structured),
        "types": sum(len(p.get("types") or []) for p in structured),
        "dropped_registrations": sum(len(p.get("dropped_calls") or []) for p in profiles or []),
        "cross_repo": {
            key: len(cross_map.get(key) or [])
            for key in ("links", "method_mismatch", "orphan_calls", "unused_routes")
        },
        "focus": focus_counts,
    }
