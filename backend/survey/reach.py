#!/usr/bin/env python3
"""
Reach（L1 可达范围）：一轮巡检里，L1 能点名的源码文件占仓库源码的多少。

L1 只能从画像文本里出现过完整路径的文件中挑选取证对象，而画像只在路由、类型骨架、
接口调用、依赖清单与根目录文件里带路径 —— 只有函数的文件、配置与脚本，
不管放在多深的目录都进不了 L1 的视野。这个统计让"巡检到底看得到多少代码"有数可查，
而不是靠"全量巡检"四个字想当然。

判定逻辑是纯函数（measure_reach），不碰数据库；文件遍历与 codegraph 索引读取在 collect_reach 里。
"""

import logging
import os
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .common import CODEGRAPH_INDEX_DIRNAME
from .profile import (
    CLIENT_SOURCE_EXTENSIONS,
    MAX_API_CALLS,
    MAX_ROUTES,
    MAX_SCANNED_FILE_BYTES,
    MAX_SCANNED_FILES,
    MAX_TYPES,
    SKIP_DIR_NAMES,
    profile_paths,
    render_profile,
    rendered_paths,
)

logger = logging.getLogger(__name__)

# 比调用侧扫描用的扩展名更宽：这里回答的是"仓库里有多少代码"，不是"哪些文件可能调接口"
SOURCE_EXTENSIONS = set(CLIENT_SOURCE_EXTENSIONS) | {
    ".rs", ".rb", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm", ".scala",
    ".kts", ".lua", ".ex", ".exs", ".erl", ".clj", ".fs", ".sql", ".sh",
}
# 明细只留最有用的前几项：完整清单可能上万行，存进库里只会让运行详情接口变慢
TOP_HIDDEN_DIRS = 8
TOP_HIDDEN_FILES = 10
# 符号数达到这个值的看不到的文件单独计数：它们大概率是核心逻辑，最值得关心
HEAVY_SYMBOLS = 10


def walk_sources(repo_dir: Path) -> List[str]:
    """仓库内全部源码文件的相对路径，跳过规则与画像扫描相同。"""
    result: List[str] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        for name in files:
            if Path(name).suffix.lower() in SOURCE_EXTENSIONS:
                result.append(Path(root, name).relative_to(repo_dir).as_posix())
    return result


def symbol_counts(repo_dir: Path) -> Optional[Dict[str, int]]:
    """从 codegraph 索引统计每个文件的符号数（函数、方法、类型等）；没有索引或读不了时返回 None。"""
    db = repo_dir / CODEGRAPH_INDEX_DIRNAME / "codegraph.db"
    if not db.is_file():
        return None
    try:
        # 只读打开：索引是 codegraph 的产物，我们不写
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            rows = conn.execute(
                "SELECT file_path, COUNT(*) FROM nodes WHERE kind NOT IN ('file', 'route', 'import') GROUP BY file_path"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning("Cannot read codegraph symbols for reach: %s: %s", repo_dir.name, e)
        return None
    counts: Dict[str, int] = {}
    root = os.path.realpath(repo_dir)
    for path, count in rows:
        key = _posix(path, root)
        counts[key] = counts.get(key, 0) + int(count)
    return counts


def _posix(path: str, root: Optional[str] = None) -> str:
    """
    把画像或索引里的路径统一成仓库内的相对 POSIX 路径，才能与源码遍历的结果对上。

    - 画像里的路径是 str(Path.relative_to) 的结果，在 Windows 上是反斜杠；
    - codegraph 可能存绝对路径，也可能带 "./" 前缀；
    - 绝对路径要与 realpath 后的仓库根比较：macOS 上 /var 实际是 /private/var，写法一变就对不上。
    任何一种不统一，对应来源的可达数都会被悄悄算成 0。
    """
    text = str(path or "").replace("\\", "/")
    if root and os.path.isabs(text):
        real = os.path.realpath(text)
        if real == root or real.startswith(root + os.sep):
            text = os.path.relpath(real, root).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _dir_of(path: str) -> str:
    """文件所在目录，根目录记为 "."。"""
    return path.rsplit("/", 1)[0] if "/" in path else "."


def _depth(path: str) -> int:
    """目录深度：根目录下的文件为 0。"""
    return path.count("/")


def measure_reach(
    profile: dict,
    sources: Iterable[str],
    budget_chars: int,
    scan_candidates: int,
    symbols: Optional[Dict[str, int]] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """
    统计一个仓库的 Reach。sources 是仓库全部源码文件的相对路径，scan_candidates 是调用侧扫描
    会计入上限的文件数（见 _scan_candidates），symbols 是每个文件的符号数（没有 codegraph 索引时缺省），
    repo_root 是 realpath 后的仓库根，用于把画像里的绝对路径换算成相对路径。

    返回的字段：
    - source_files / listed_files / reachable_files：源码总数、画像中出现、L1 可达（截断之后仍在）；
    - by_source：画像中出现的源码文件按来源计数（一个文件可同时属于多个来源）；
    - by_depth：[{depth, reachable, total}]；
    - truncated / profile_chars / budget_chars：画像是否被 L1 预算截断；
    - caps：撞上的抽取上限；scan_limited：调用侧扫描是否只扫了一部分文件；
    - hidden_dirs / heavy_hidden_files / heavy_hidden_count / heavy_threshold：看不到的文件最集中的目录、
      符号最多的文件，以及符号数达到阈值的看不到的文件数。
    """
    sources = [_posix(s) for s in sources]
    source_set = set(sources)
    groups = {key: {_posix(p, repo_root) for p in paths} for key, paths in profile_paths(profile).items()}
    listed = set().union(*groups.values()) & source_set
    full_text = render_profile(profile, 0)
    text = render_profile(profile, budget_chars)
    reachable = {_posix(p, repo_root) for p in rendered_paths(profile, text)} & source_set

    total_by_depth = Counter(_depth(s) for s in sources)
    reachable_by_depth = Counter(_depth(s) for s in reachable)

    hidden = [s for s in sources if s not in reachable]
    hidden_by_dir = Counter(_dir_of(s) for s in hidden)
    total_by_dir = Counter(_dir_of(s) for s in sources)

    caps = []
    if len(profile.get("routes") or []) >= MAX_ROUTES:
        caps.append("routes")
    if len(profile.get("types") or []) >= MAX_TYPES:
        caps.append("types")
    if profile.get("api_calls_capped", len(profile.get("api_calls") or []) >= MAX_API_CALLS):
        caps.append("api_calls")

    heavy: List[dict] = []
    heavy_count = 0
    if symbols is not None:
        ranked = sorted(((symbols.get(s, 0), s) for s in hidden if symbols.get(s, 0) > 0), reverse=True)
        heavy = [{"path": s, "symbols": c} for c, s in ranked[:TOP_HIDDEN_FILES]]
        heavy_count = sum(1 for c, _ in ranked if c >= HEAVY_SYMBOLS)

    return {
        "source_files": len(sources),
        "listed_files": len(listed),
        "reachable_files": len(reachable),
        "by_source": {key: len(paths & source_set) for key, paths in groups.items()},
        "by_depth": [
            {"depth": d, "reachable": reachable_by_depth[d], "total": total_by_depth[d]}
            for d in sorted(total_by_depth)
        ],
        "truncated": len(full_text) > len(text),
        "profile_chars": len(full_text),
        "budget_chars": int(budget_chars),
        "caps": caps,
        "scan_limited": scan_candidates > MAX_SCANNED_FILES,
        "hidden_dirs": [
            {"dir": d, "hidden": n, "total": total_by_dir[d]} for d, n in hidden_by_dir.most_common(TOP_HIDDEN_DIRS)
        ],
        "heavy_hidden_files": heavy,
        # 没有 codegraph 索引时无从判断哪些文件"重"，用 None 与"一个都没有"区分开
        "heavy_hidden_count": heavy_count if symbols is not None else None,
        # 阈值随数据一起返回，界面据此显示，不在前端再写一份
        "heavy_threshold": HEAVY_SYMBOLS,
    }


def _scan_candidates(repo_dir: Path, sources: List[str]) -> int:
    """
    调用侧扫描（profile._iter_source_files）会计入上限的文件数。

    它跳过超过 MAX_SCANNED_FILE_BYTES 的文件且不计数，只按扩展名数会把"全扫了"误报成"只扫了一部分"。
    """
    count = 0
    for rel in sources:
        if Path(rel).suffix.lower() not in CLIENT_SOURCE_EXTENSIONS:
            continue
        try:
            if (repo_dir / rel).stat().st_size <= MAX_SCANNED_FILE_BYTES:
                count += 1
        except OSError:
            # 与扫描侧一致：读不到大小的文件扫描时同样跳过
            continue
    return count


def collect_reach(profile: dict, repo_dir: Path, budget_chars: int) -> dict:
    """遍历仓库、读取 codegraph 符号数，再交给 measure_reach。"""
    sources = walk_sources(repo_dir)
    return measure_reach(
        profile, sources, budget_chars, _scan_candidates(repo_dir, sources), symbol_counts(repo_dir),
        repo_root=os.path.realpath(repo_dir),
    )
