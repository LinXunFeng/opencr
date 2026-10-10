#!/usr/bin/env python3
"""
把 Survey 配置的来源展开成本轮要处理的具体仓库清单。

组织（GitLab group）**每次执行时实时展开**，不在配置阶段固化：
组织下的项目列表会变，固化就等于"往组织里加仓库不会被巡检看到"，
那和手填一堆链接就没有区别了。
"""

import logging
from typing import Dict, List, NamedTuple, Optional, Set, Tuple
from urllib.parse import urlparse

from ..review.config import load_gitlab_config
from ..review.gitlab import get_group_full_path, list_group_projects, require_gitlab_config
from ..storage.models import SOURCE_ORG, SOURCE_REPO
from .common import (
    SurveyError,
    matches_any_pattern,
    parse_json_list,
    repo_slug_from_url,
)

logger = logging.getLogger(__name__)

# 单个组织最多翻多少页（每页 100）。上限存在的意义是别让一个指错的顶层 group
# 把整个实例的项目都拉回来 —— 真正的裁剪交给单次巡检的仓库上限和排除清单。
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
        # 从组织的子页面复制的地址带 /-/shared 这类页面后缀，不剥掉的话会被当成组织路径的一部分
        text = text.split("/-/", 1)[0].strip("/")

    if not text:
        raise SurveyError(f"无法从地址中解析出组织路径：{raw}")
    return text


def expand_group(group_path: str, exclude_patterns: List[str]) -> List[Dict[str, str]]:
    """
    列出组织（含子组）下的全部非归档项目，不含其他组织共享进来的项目。

    翻页上限的意义是别让一个指错的顶层 group 把整个实例的项目都拉回来；
    真正的裁剪交给单次巡检的仓库上限与排除清单。
    """
    normalized = normalize_group_path(group_path)
    # 除了请求时关掉 with_shared，再按路径前缀兜一遍：巡检范围以配置里写明的组织为准，
    # 不能押在某个 GitLab 版本一定支持这个参数上。
    # 前缀取组织当前的 full_path 而不是用户填的：组织改名或转移后旧路径仍能访问，
    # 拿填写值比较会把组织自己的项目全当成共享的剔掉，巡检静默变成零个仓库。
    # 路径大小写不敏感，统一小写比较
    full_path = get_group_full_path(normalized)
    if not full_path:
        raise SurveyError(f"无法读取组织信息：{normalized}")
    own_prefix = full_path.lower() + "/"
    repos: List[Dict[str, str]] = []

    for page in range(1, MAX_GROUP_PAGES + 1):
        batch = list_group_projects(normalized, page=page)
        if not batch:
            break

        for item in batch:
            if not isinstance(item, dict):
                continue
            project_path = str(item.get("path_with_namespace") or "").strip()
            url = str(item.get("http_url_to_repo") or "").strip()
            if not url:
                continue
            if not project_path.lower().startswith(own_prefix):
                logger.info("Shared project outside group skipped: %s (group %s)", project_path, full_path)
                continue
            if matches_any_pattern(project_path, exclude_patterns):
                logger.info("Repo excluded by pattern: %s", project_path)
                continue
            repos.append(
                {
                    "url": url,
                    # 用各仓库自己的默认分支，不硬写 main —— 老仓库很多还是 master
                    "branch": str(item.get("default_branch") or "").strip(),
                    "path": project_path,
                }
            )

        if len(batch) < 100:
            break

    return repos


class ResolvedSources(NamedTuple):
    """
    来源展开的结果：本轮要处理的仓库，以及没参与的两类仓库。

    后两类都要交给调用方记到运行记录上 —— 只写日志的话，它们会悄无声息地退出巡检，报告看起来完全正常。
    截掉的另记降级；被忽略的是用户的选择，不是降级。
    """

    targets: List[Dict[str, str]]
    truncated: List[Dict[str, str]]
    ignored: List[Dict[str, str]]


def resolve_sources(
    sources: List[dict], max_repos: int, ignored_slugs: Optional[Set[str]] = None
) -> ResolvedSources:
    """
    把 Survey 的来源清单展开成去重后的仓库列表，剔除已忽略仓库，超出 max_repos 的部分截掉。

    单个来源展开失败不会让整轮直接失败 —— 由调用方按"部分失败记降级、
    全部失败才判失败"处理，所以这里把错误随条目一起返回而不是抛出去。
    """
    resolved: List[Dict[str, str]] = []
    ignored: List[Dict[str, str]] = []
    ignored_slugs = ignored_slugs or set()
    seen_slugs = set()

    for source in sources or []:
        kind = str(source.get("kind") or SOURCE_REPO).strip().lower()
        url = str(source.get("url") or "").strip()
        if not url:
            continue

        if kind == SOURCE_ORG:
            patterns = parse_json_list(source.get("exclude_patterns"))
            try:
                check_platform_ready()
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
            if slug in ignored_slugs:
                # 在截断之前剔除：被忽略的仓库占着上限名额，会把本该巡检的仓库挤出去
                logger.info("Ignored repo skipped: %s", item["url"])
                ignored.append({**item, "slug": slug})
                continue
            resolved.append({**item, "slug": slug})

    truncated = resolved[max_repos:]
    if truncated:
        logger.warning("Repo count %s exceeds limit %s, truncating", len(resolved), max_repos)
        resolved = resolved[:max_repos]

    return ResolvedSources(resolved, truncated, ignored)


def live_repo_candidates(
    sources: List[dict], max_repos: int, ignored_slugs: Set[str]
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """
    按当前配置实时展开来源，返回 (可供标记为已忽略的仓库, 展开失败的来源)。

    走的是巡检执行时同一个 resolve_sources，所以清单与下一轮实际会处理的仓库一致：
    超出上限会被截掉的仓库照样列出（仓库太多时最想剔除的就是它们），已忽略的不列。
    展开失败的组织单独返回而不是混进候选 —— 它不是仓库，选中它会存下一个永远命中不了的 slug；
    也不能直接丢掉，否则界面上只是少了一批候选，看不出是某个组织没展开。
    """
    resolved = resolve_sources(sources, max_repos, ignored_slugs)
    candidates: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []
    for target in resolved.targets + resolved.truncated:
        if target.get("error"):
            errors.append({"source": target["url"], "error": target["error"]})
        else:
            candidates.append({"repo_slug": target["slug"], "url": target["url"]})
    return sorted(candidates, key=lambda c: c["repo_slug"]), errors


def check_platform_ready() -> None:
    """
    组织展开目前只实现了 GitLab 的 group，平台不是 GitLab、或配置不完整时给出明确错误。

    不拦的话，按 GitLab 路由把请求发到别的平台，用户只会看到一个 404 或"无法读取组织信息"，
    看不出原因是平台不支持。
    """
    platform = str(load_gitlab_config().get("type") or "gitlab").strip().lower()
    if platform != "gitlab":
        raise SurveyError(f"组织展开目前只支持 GitLab 的 group，当前代码平台为 {platform}；请逐个填写仓库地址")
    require_gitlab_config()
