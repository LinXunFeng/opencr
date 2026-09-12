#!/usr/bin/env python3
"""
L0 仓库画像：把一个仓库的全量代码确定性地降维成结构摘要。

存在的唯一理由是全量代码进不了模型上下文（实测三个中等仓库合计 569 万字符，
而组合画像约 43 万字符，13 倍压缩），必须先降维才能做跨仓库整合分析。
这一层**不调模型**，产出完全可复现。

关于 codegraph 的两条实测结论，改动前请先读 docs/adr/0003-per-repo-codegraph-index.md：
1. 必须逐仓库建索引，绝不能把多个仓库放在同一个父目录下建一张图；
2. 它只抽路由的**生产侧**，前端的调用侧要我们自己用正则抽。
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from ..storage.models import PROFILE_CODEGRAPH, PROFILE_MANIFEST
from .common import CODEGRAPH_INDEX_DIRNAME, SurveyError
from .config import load_survey_config

logger = logging.getLogger(__name__)

# 依赖清单：语言与框架构成的最可靠来源，且体积极小
MANIFEST_FILENAMES = (
    "package.json",
    "pubspec.yaml",
    "requirements.txt",
    "pyproject.toml",
    "go.mod",
    "composer.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "Cargo.toml",
)
MAX_MANIFEST_CHARS = 4000

# 消费侧扫描的范围。只扫这些扩展名是为了别把 node_modules 之外的资源文件也读一遍。
CLIENT_SOURCE_EXTENSIONS = {
    ".dart", ".ts", ".tsx", ".js", ".jsx", ".vue",
    ".kt", ".swift", ".java", ".php", ".go", ".py",
}
MAX_SCANNED_FILES = 3000
MAX_SCANNED_FILE_BYTES = 200_000
MAX_API_CALLS = 400
MAX_TYPES = 1500
MAX_ROUTES = 500

SKIP_DIR_NAMES = {
    ".git", ".codegraph", "node_modules", "build", "dist", "out",
    "vendor", ".dart_tool", ".gradle", "Pods", "__pycache__", ".venv", "venv",
}

# 形如 "/api/order/create" 的路径字面量。要求至少两段，避免把 "/" 和 "/x" 这类
# 正则片段、格式串误当成接口路径。
_PATH_LITERAL_RE = re.compile(r"""['"`](/[A-Za-z0-9_\-./{}:$%]*?/[A-Za-z0-9_\-./{}:$%]*)['"`]""")
_HTTP_METHOD_RE = re.compile(r"\b(get|post|put|patch|delete|head|options)\b", re.IGNORECASE)


def codegraph_available() -> bool:
    """codegraph 是否可用。不可用不是错误 —— 它是可选依赖，缺了就退化。"""
    cfg = load_survey_config()
    if not cfg["codegraph_enabled"]:
        return False
    return shutil.which(cfg["codegraph_bin"]) is not None


def _codegraph_env() -> Dict[str, str]:
    """codegraph 子进程环境。"""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        # 遥测的权威关闭动作在部署脚本里（codegraph telemetry off），
        # 这里再挡一道，成本为零。
        "CODEGRAPH_TELEMETRY": "0",
        "DO_NOT_TRACK": "1",
    }


def _run_codegraph(args: List[str], repo_dir: Path, timeout: int) -> Optional[subprocess.CompletedProcess]:
    """执行一条 codegraph 命令；启动失败或超时返回 None。"""
    cfg = load_survey_config()
    try:
        return subprocess.run(
            [cfg["codegraph_bin"], *args],
            cwd=str(repo_dir),
            env=_codegraph_env(),
            capture_output=True,
            text=True,
            timeout=max(int(timeout), 10),
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("codegraph %s timed out: repo=%s timeout=%ss", args[0], repo_dir.name, timeout)
    except Exception as e:
        logger.warning("codegraph %s failed to start: repo=%s error=%s", args[0], repo_dir.name, e)
    return None


def _index_is_reusable(repo_dir: Path, timeout: int) -> bool:
    """
    已有索引能否直接增量更新。

    codegraph 升级后抽取逻辑可能变化，沿用旧索引会让画像悄悄退化到旧版本的水平 ——
    产出看起来一切正常，只是少了新版本才能抽出来的东西。因此这里以它自报的
    `reindexRecommended` 与抽取版本号为准；**任何拿不准的情况都判为不可复用**，
    宁可多花几百毫秒重建，也不要基于一份说不清的索引出报告。
    """
    completed = _run_codegraph(["status", "-j", str(repo_dir)], repo_dir, timeout=60)
    if completed is None or completed.returncode != 0:
        return False
    try:
        status = json.loads(completed.stdout or "{}")
    except (ValueError, TypeError):
        return False

    index = status.get("index") or {}
    if not status.get("initialized") or index.get("state") != "complete":
        return False
    if index.get("reindexRecommended"):
        logger.info("codegraph recommends reindex: repo=%s", repo_dir.name)
        return False
    if index.get("builtWithExtractionVersion") != index.get("currentExtractionVersion"):
        logger.info(
            "codegraph extraction version changed (%s -> %s), rebuilding: repo=%s",
            index.get("builtWithExtractionVersion"), index.get("currentExtractionVersion"), repo_dir.name,
        )
        return False
    return True


def build_index(repo_dir: Path, timeout: int = 600) -> Optional[Path]:
    """
    为**单个仓库**准备索引，返回 codegraph.db 路径；失败返回 None。

    已有可复用索引时走 `sync` 增量更新，否则全量 `init`。索引之所以能跨轮次留存，
    是因为拉取时的 `git clean` 排除了它（见 workspace.prepare_repo）——
    codegraph 只能把索引放在仓库内部，绝对路径会被静默忽略。

    逐仓库建索引是硬性要求，不是偏好：codegraph 不解析外部依赖，
    未解析的符号会被按名字连到工作区内任意同名节点上，**跨语言也连**。
    实测把 Flutter / NestJS / gin 三个毫无关联的仓库放进同一张图，
    产生了 824 条跨仓库边，其中 100% 是假的（Dart 的 debugPrint 连到了 gin 的 debugPrint）。
    更糟的是这些假边的形状恰好就是"前端调用了后端的函数"——正是巡检想产出的结论。
    """
    db_path = repo_dir / CODEGRAPH_INDEX_DIRNAME / "codegraph.db"

    if db_path.is_file() and _index_is_reusable(repo_dir, timeout):
        completed = _run_codegraph(["sync", str(repo_dir)], repo_dir, timeout)
        if completed is not None and completed.returncode == 0 and db_path.is_file():
            logger.info("codegraph index updated incrementally: repo=%s", repo_dir.name)
            return db_path
        # sync 失败不算致命：删掉这份可疑的索引重建一次即可
        detail = "" if completed is None else (completed.stderr or completed.stdout or "").strip()[:200]
        logger.warning("codegraph sync failed, falling back to full index: repo=%s %s", repo_dir.name, detail)
        shutil.rmtree(repo_dir / CODEGRAPH_INDEX_DIRNAME, ignore_errors=True)

    completed = _run_codegraph(["init", "-y", str(repo_dir)], repo_dir, timeout)
    if completed is None:
        return None
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:300]
        logger.warning("codegraph init failed: repo=%s exit=%s %s", repo_dir.name, completed.returncode, detail)
        return None

    if not db_path.is_file():
        logger.warning("codegraph init reported success but db missing: %s", db_path)
        return None
    logger.info("codegraph index built from scratch: repo=%s", repo_dir.name)
    return db_path


def _query(db_path: Path, sql: str, params: tuple = ()) -> List[tuple]:
    """只读方式查询 codegraph 的库；查询失败退化为空结果而不是让整轮失败。"""
    try:
        # URI 模式的 mode=ro：codegraph 的库是它的产物，我们只读不写
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as e:
        logger.warning("Cannot open codegraph db %s: %s", db_path, e)
        return []
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        logger.warning("codegraph query failed on %s: %s", db_path, e)
        return []
    finally:
        conn.close()


def _extract_routes(db_path: Path) -> List[dict]:
    """
    抽取路由（生产侧）及其处理函数。

    codegraph 把路由建成一等节点（name 形如 "POST /api/order/create"），
    并有一条指向处理函数的 references 边，顺着它能拿到请求结构体。
    """
    rows = _query(
        db_path,
        """
        SELECT sn.name, sn.file_path, sn.language, IFNULL(tn.name, ''), IFNULL(tn.file_path, '')
        FROM nodes sn
        LEFT JOIN edges e ON e.source = sn.id AND e.kind = 'references'
        LEFT JOIN nodes tn ON tn.id = e.target
        WHERE sn.kind = 'route'
        ORDER BY sn.file_path, sn.name
        LIMIT ?
        """,
        (MAX_ROUTES,),
    )
    routes = []
    for name, file_path, language, handler, handler_file in rows:
        raw = str(name or "").strip()
        method, _, path = raw.partition(" ")
        routes.append(
            {
                "method": method.upper() if path else "",
                "path": path or raw,
                "file": file_path or "",
                "language": language or "",
                "handler": handler or "",
                "handler_file": handler_file or "",
            }
        )
    return routes


def _extract_types(db_path: Path) -> List[dict]:
    """抽取类型骨架。它与路由一起构成 L1 唯一能看见"系统里有什么"的来源。"""
    rows = _query(
        db_path,
        """
        SELECT kind, name, file_path, language, IFNULL(signature, '')
        FROM nodes
        WHERE kind IN ('class', 'interface', 'struct', 'enum', 'type_alias')
        ORDER BY file_path, start_line
        LIMIT ?
        """,
        (MAX_TYPES,),
    )
    return [
        {"kind": k, "name": n, "file": f, "language": lang, "signature": (sig or "")[:200]}
        for k, n, f, lang, sig in rows
    ]


def _extract_languages(db_path: Path) -> Dict[str, int]:
    """按语言统计文件数，作为 skill 匹配的依据（全量场景下没有 diff 可看）。"""
    rows = _query(
        db_path,
        "SELECT language, COUNT(*) FROM nodes WHERE kind='file' GROUP BY language ORDER BY 2 DESC",
    )
    return {str(lang): int(count) for lang, count in rows if lang}


def _iter_source_files(repo_dir: Path):
    """遍历仓库内的源码文件，跳过依赖目录与产物目录。"""
    scanned = 0
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        for filename in files:
            if scanned >= MAX_SCANNED_FILES:
                return
            path = Path(root) / filename
            if path.suffix.lower() not in CLIENT_SOURCE_EXTENSIONS:
                continue
            try:
                if path.stat().st_size > MAX_SCANNED_FILE_BYTES:
                    continue
            except OSError:
                continue
            scanned += 1
            yield path


def extract_api_calls(repo_dir: Path) -> List[dict]:
    """
    抽取接口**调用侧**的路径字面量。

    这一段不能省：实测 codegraph 完全不捕获 `fetch("/api/order/create")` 里的
    URL 字面量（`fetch` 只在 unresolved_refs 留下一条 failed，参数丢失），
    全库检索 "/api/order" 只命中后端的 route 节点。
    跨仓库 join 的另一半只能自己抽。
    """
    calls: List[dict] = []
    seen = set()
    for path in _iter_source_files(repo_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if len(calls) >= MAX_API_CALLS:
                return calls
            for match in _PATH_LITERAL_RE.finditer(line):
                literal = match.group(1)
                # import 路径、CSS 选择器、正则片段都会长得像路径，靠这两条粗筛
                if literal.startswith("//") or literal.endswith("/"):
                    continue
                if any(literal.endswith(ext) for ext in (".dart", ".ts", ".js", ".vue", ".css", ".png", ".svg")):
                    continue
                method_match = _HTTP_METHOD_RE.search(line)
                rel = str(path.relative_to(repo_dir))
                key = (literal, rel)
                if key in seen:
                    continue
                seen.add(key)
                calls.append(
                    {
                        "path": literal,
                        "method": (method_match.group(1).upper() if method_match else ""),
                        "file": rel,
                        "line": line_no,
                    }
                )
    return calls


def _drop_route_registrations(calls: List[dict], routes: List[dict]) -> List[dict]:
    """
    剔除把"路由注册"误当成"接口调用"的条目。

    `r.POST("/api/order/create", handler)` 这一行既被 codegraph 抽成 route 节点，
    也会被调用侧的正则命中。不剔掉的话，每个后端仓库都会显示成"自己调用了自己的接口"，
    而真正要找的"前端调了一个后端不存在的接口"会淹没在这些自匹配里。
    只在**同一文件同一路径**时剔除，跨文件的同名路径是真实信息，不能一并抹掉。
    """
    registrations = {(r.get("file", ""), r.get("path", "")) for r in routes or []}
    if not registrations:
        return calls
    return [c for c in calls if (c.get("file", ""), c.get("path", "")) not in registrations]


def _read_manifests(repo_dir: Path) -> Dict[str, str]:
    """读取依赖清单。这一层不依赖 codegraph，是退化模式下唯一的语言线索。"""
    manifests: Dict[str, str] = {}
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        depth = len(Path(root).relative_to(repo_dir).parts)
        if depth > 2:
            dirs[:] = []
            continue
        for filename in files:
            if filename not in MANIFEST_FILENAMES:
                continue
            path = Path(root) / filename
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            manifests[str(path.relative_to(repo_dir))] = content[:MAX_MANIFEST_CHARS]
    return manifests


def _build_tree_summary(repo_dir: Path, max_entries: int = 80) -> List[str]:
    """顶层目录结构摘要：只到二级，够模型判断"这是个什么形状的项目"。"""
    entries: List[str] = []
    for path in sorted(repo_dir.iterdir()):
        if path.name in SKIP_DIR_NAMES or path.name.startswith("."):
            continue
        if path.is_dir():
            children = [
                c.name for c in sorted(path.iterdir())
                if c.is_dir() and c.name not in SKIP_DIR_NAMES and not c.name.startswith(".")
            ][:10]
            entries.append(f"{path.name}/" + (f" ({', '.join(children)})" if children else ""))
        else:
            entries.append(path.name)
        if len(entries) >= max_entries:
            break
    return entries


def build_profile(repo_slug: str, repo_dir: Path, index_timeout: int = 600) -> dict:
    """
    生成一个仓库的画像。

    codegraph 不可用时退化为 manifest 级画像：巡检照常完成，但 L1 是结构盲的
    （只知道有哪些文件，不知道有哪些接口与类型），由调用方记一条 Degradation。
    """
    profile = {
        "repo_slug": repo_slug,
        "kind": PROFILE_MANIFEST,
        "manifests": _read_manifests(repo_dir),
        "tree": _build_tree_summary(repo_dir),
        "routes": [],
        "types": [],
        "languages": {},
        # 调用侧不依赖 codegraph，退化模式下照样抽 —— 它是跨仓库 join 的一半
        "api_calls": extract_api_calls(repo_dir),
    }

    if not codegraph_available():
        logger.info("codegraph unavailable, profile degraded to manifest: repo=%s", repo_slug)
        return profile

    db_path = build_index(repo_dir, timeout=index_timeout)
    if db_path is None:
        return profile

    profile["kind"] = PROFILE_CODEGRAPH
    profile["routes"] = _extract_routes(db_path)
    profile["types"] = _extract_types(db_path)
    profile["languages"] = _extract_languages(db_path)
    profile["api_calls"] = _drop_route_registrations(profile["api_calls"], profile["routes"])
    logger.info(
        "Profile built: repo=%s kind=codegraph routes=%s types=%s api_calls=%s languages=%s",
        repo_slug, len(profile["routes"]), len(profile["types"]),
        len(profile["api_calls"]), ",".join(sorted(profile["languages"])) or "-",
    )
    return profile


def render_profile(profile: dict, max_chars: int) -> str:
    """把画像渲染成 L1 的输入文本，按预算截断。"""
    slug = profile.get("repo_slug", "?")
    parts: List[str] = [f"### 仓库 `{slug}`（画像来源：{profile.get('kind')}）"]

    languages = profile.get("languages") or {}
    if languages:
        parts.append("语言构成：" + ", ".join(f"{k}×{v}" for k, v in languages.items()))

    tree = profile.get("tree") or []
    if tree:
        parts.append("目录结构：\n" + "\n".join(f"- {t}" for t in tree))

    manifests = profile.get("manifests") or {}
    if manifests:
        blocks = [f"#### {name}\n```\n{content}\n```" for name, content in manifests.items()]
        parts.append("依赖清单：\n" + "\n".join(blocks))

    routes = profile.get("routes") or []
    if routes:
        lines = [
            f"- `{r['method']} {r['path']}` → {r['handler'] or '?'} ({r['file']})"
            for r in routes
        ]
        parts.append(f"提供的接口（{len(routes)}）：\n" + "\n".join(lines))

    calls = profile.get("api_calls") or []
    if calls:
        lines = [f"- `{c['method']} {c['path']}` ({c['file']}:{c['line']})" for c in calls]
        parts.append(f"调用的接口路径（{len(calls)}）：\n" + "\n".join(lines))

    types = profile.get("types") or []
    if types:
        lines = [f"- {t['kind']} `{t['name']}` ({t['file']})" for t in types]
        parts.append(f"类型骨架（{len(types)}）：\n" + "\n".join(lines))

    text = "\n\n".join(parts)
    return text[:max_chars] if max_chars > 0 else text


def save_profile(artifacts_root: Path, profile: dict) -> None:
    """把画像落盘，便于排查"这一轮模型到底看到了什么"。"""
    try:
        artifacts_root.mkdir(parents=True, exist_ok=True)
        target = artifacts_root / f"{profile.get('repo_slug', 'repo')}.json"
        target.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        # 画像落盘只是排查便利，失败绝不能影响巡检本身
        logger.warning("Failed to save profile artifact: %s", e)
