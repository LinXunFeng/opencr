#!/usr/bin/env python3
"""
把 Survey 配置的来源展开成本轮要处理的具体仓库清单。

组织（GitLab group）**每次执行时实时展开**，不在配置阶段固化：
组织下的项目列表会变，固化就等于"往组织里加仓库不会被巡检看到"，
那和手填一堆链接就没有区别了。
"""

import logging
from typing import Dict, List
from urllib.parse import urlparse

from ..review.gitlab import list_group_projects, require_gitlab_config
from ..storage.models import SOURCE_ORG, SOURCE_REPO
from .common import (
    MAX_REPOS_PER_SURVEY,
    SurveyError,
    matches_any_pattern,
    parse_json_list,
    repo_slug_from_url,
)

logger = logging.getLogger(__name__)

# 单个组织最多翻多少页（每页 100）。上限存在的意义是别让一个指错的顶层 group
# 把整个实例的项目都拉回来 —— 真正的裁剪交给 MAX_REPOS_PER_SURVEY 和排除清单。
MAX_GROUP_PAGES = 10


def normalize_group_path(raw: str) -> str:
    """
    把用户填的组织地址规整成 GitLab 的 group full path。

    界面上填整条 URL（从浏览器地址栏复制）和填裸路径同样常见，两种都要认。
    """
    text = str(raw or "").strip().strip("/")
    if not text:
        raise SurveyError("组织地址为空")

    if "://" in text:
        parsed = urlparse(text)
        text = parsed.path.strip("/")
        # GitLab 的 group 页面路径可能带 /groups/ 前缀
        if text.startswith("groups/"):
            text = text[len("groups/"):]

    if not text:
        raise SurveyError(f"无法从地址中解析出组织路径：{raw}")
    return text


def expand_group(group_path: str, exclude_patterns: List[str]) -> List[Dict[str, str]]:
    """
    列出组织（含子组）下的全部非归档项目。

    翻页上限的意义是别让一个指错的顶层 group 把整个实例的项目都拉回来；
    真正的裁剪交给 MAX_REPOS_PER_SURVEY 与排除清单。
    """
    normalized = normalize_group_path(group_path)
    repos: List[Dict[str, str]] = []

    for page in range(1, MAX_GROUP_PAGES + 1):
        batch = list_group_projects(normalized, page=page)
        if not batch:
            break

        for item in batch:
            if not isinstance(item, dict):
                continue
            full_path = str(item.get("path_with_namespace") or "").strip()
            url = str(item.get("http_url_to_repo") or "").strip()
            if not url:
                continue
            if matches_any_pattern(full_path, exclude_patterns):
                logger.info("Repo excluded by pattern: %s", full_path)
                continue
            repos.append(
                {
                    "url": url,
                    # 用各仓库自己的默认分支，不硬写 main —— 老仓库很多还是 master
                    "branch": str(item.get("default_branch") or "").strip(),
                    "path": full_path,
                }
            )

        if len(batch) < 100:
            break

    return repos


def resolve_sources(sources: List[dict]) -> List[Dict[str, str]]:
    """
    把 Survey 的来源清单展开成去重后的仓库列表。

    单个来源展开失败不会让整轮直接失败 —— 由调用方按"部分失败记降级、
    全部失败才判失败"处理，所以这里把错误随条目一起返回而不是抛出去。
    """
    resolved: List[Dict[str, str]] = []
    seen_slugs = set()

    for source in sources or []:
        kind = str(source.get("kind") or SOURCE_REPO).strip().lower()
        url = str(source.get("url") or "").strip()
        if not url:
            continue

        if kind == SOURCE_ORG:
            patterns = parse_json_list(source.get("exclude_patterns"))
            try:
                expanded = expand_group(url, patterns)
                logger.info("Group expanded: %s -> %s repos", url, len(expanded))
            except Exception as e:
                logger.warning("Group expansion failed: %s: %s", url, e)
                resolved.append({"url": url, "branch": "", "path": url, "error": str(e)[:300]})
                continue
        else:
            expanded = [{"url": url, "branch": str(source.get("branch") or "").strip(), "path": url}]

        for item in expanded:
            slug = repo_slug_from_url(item["url"])
            if slug in seen_slugs:
                # 同一个仓库既被手填、又落在某个组织里是很常见的配置，去重而不是报错
                logger.info("Duplicate repo skipped: %s", item["url"])
                continue
            seen_slugs.add(slug)
            resolved.append({**item, "slug": slug})

    if len(resolved) > MAX_REPOS_PER_SURVEY:
        logger.warning(
            "Repo count %s exceeds limit %s, truncating", len(resolved), MAX_REPOS_PER_SURVEY
        )
        resolved = resolved[:MAX_REPOS_PER_SURVEY]

    return resolved


def check_platform_ready() -> None:
    """组织展开依赖 GitLab API，配置不完整时给出明确错误而不是等 HTTP 报错。"""
    require_gitlab_config()
