#!/usr/bin/env python3
"""
GitLab API 交互
"""

import logging
from datetime import datetime
from typing import List, Optional
from urllib.parse import quote

from .common import ReviewError
from .config import load_gitlab_config, load_openai_config

logger = logging.getLogger(__name__)


def require_gitlab_config() -> dict:
    """确保 GitLab 基础配置完整"""
    cfg = load_gitlab_config()

    if not cfg["url"] or not cfg["token"]:
        logger.error(
            f"GitLab config missing: url={'SET' if cfg['url'] else 'EMPTY'}, "
            f"token={'SET' if cfg['token'] else 'EMPTY'}"
        )
        raise ReviewError(f"GitLab 配置不完整: url={bool(cfg['url'])}, token={bool(cfg['token'])}")

    return cfg


def get_mr_changes(project_id: int, mr_iid: int) -> List[dict]:
    """从 GitLab API 获取 MR 变更列表"""
    changes, _ = get_mr_changes_with_refs(project_id, mr_iid)
    return changes


def get_mr_changes_with_refs(project_id: int, mr_iid: int) -> tuple:
    """从 GitLab API 获取 MR 变更列表与 diff refs"""
    cfg = require_gitlab_config()
    gitlab_url = cfg["url"]
    token = cfg["token"]

    url = f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes"

    headers = {
        "PRIVATE-TOKEN": token,
        "Content-Type": "application/json",
    }

    try:
        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.get(url, headers=headers, timeout=30, verify=False)
        response.raise_for_status()

        data = response.json()
        changes = data.get("changes", [])
        diff_refs = data.get("diff_refs", {}) or {}
        if not changes:
            return [], diff_refs
        return changes, diff_refs

    except Exception as e:
        logger.error(f"Failed to fetch MR diff: {e}")
        raise ReviewError(f"获取 MR diff 失败: {str(e)}")


def get_compare_changes(project_id: int, from_sha: str, to_sha: str) -> List[dict]:
    """从 GitLab compare API 获取两个提交区间的增量变更列表"""
    cfg = require_gitlab_config()
    gitlab_url = cfg["url"]
    token = cfg["token"]

    url = f"{gitlab_url}/api/v4/projects/{project_id}/repository/compare"
    headers = {
        "PRIVATE-TOKEN": token,
        "Content-Type": "application/json",
    }
    params = {
        "from": from_sha,
        "to": to_sha,
    }

    try:
        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.get(url, headers=headers, params=params, timeout=30, verify=False)
        response.raise_for_status()

        data = response.json() or {}
        diffs = data.get("diffs", []) or []
        return diffs
    except Exception as e:
        logger.error("Failed to fetch compare changes from %s to %s: %s", from_sha, to_sha, e)
        raise ReviewError(f"获取提交区间 diff 失败: {str(e)}")


def get_repository_file_info(project_id: int, file_path: str, ref: str) -> dict:
    """读取仓库文件信息（包含 size）。"""
    cfg = require_gitlab_config()
    gitlab_url = cfg["url"]
    token = cfg["token"]

    encoded_path = quote((file_path or "").strip(), safe="")
    url = f"{gitlab_url}/api/v4/projects/{project_id}/repository/files/{encoded_path}"
    headers = {
        "PRIVATE-TOKEN": token,
        "Content-Type": "application/json",
    }
    params = {"ref": ref}

    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    response = requests.get(url, headers=headers, params=params, timeout=30, verify=False)
    response.raise_for_status()
    return response.json() or {}


def _parse_file_size_bytes(file_info: dict) -> Optional[int]:
    """从文件详情响应中解析 size 字段，失败时返回 None。"""
    size_raw = (file_info or {}).get("size")
    try:
        return int(size_raw)
    except (TypeError, ValueError):
        return None


def enrich_changes_with_file_info(
    project_id: int,
    changes: List[dict],
    ref: str,
) -> List[dict]:
    """
    为变更补充文件元信息，供 AI/skill 决策使用。
    补充字段：
    - file_size_bytes: Optional[int]
    - file_size_kb: Optional[float]
    """
    safe_ref = (ref or "").strip()
    cache: dict = {}
    enriched: List[dict] = []

    for change in changes or []:
        item = dict(change or {})
        path = str(item.get("new_path") or item.get("old_path") or "").strip()
        deleted = bool(item.get("deleted_file"))
        item["file_size_bytes"] = None
        item["file_size_kb"] = None

        if not path or deleted or not safe_ref:
            enriched.append(item)
            continue

        if path not in cache:
            try:
                file_info = get_repository_file_info(project_id, path, safe_ref)
                cache[path] = _parse_file_size_bytes(file_info)
            except Exception as e:
                logger.warning("Skip file size lookup for %s: %s", path, e)
                cache[path] = None

        size_bytes = cache[path]
        if size_bytes is not None:
            item["file_size_bytes"] = size_bytes
            item["file_size_kb"] = round(size_bytes / 1024.0, 1)

        enriched.append(item)

    return enriched


def get_mr_diff(project_id: int, mr_iid: int) -> str:
    """从 GitLab API 获取 MR diff（兼容旧逻辑）"""
    changes = get_mr_changes(project_id, mr_iid)
    return "\n".join([c.get("diff", "") for c in changes])


def post_mr_comment(project_id: int, mr_iid: int, content: str) -> None:
    """在 MR 中发表评论"""
    cfg = require_gitlab_config()
    gitlab_url = cfg["url"]
    token = cfg["token"]

    url = f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes"
    headers = {
        "PRIVATE-TOKEN": token,
        "Content-Type": "application/json",
    }

    ai_cfg = load_openai_config()
    full_content = f"""## 🤖 AI 代码审查报告

**审查时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**审查模型**: {ai_cfg['model']}

---

{content}

"""

    try:
        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.post(url, headers=headers, json={"body": full_content}, timeout=30, verify=False)
        response.raise_for_status()
        logger.info(f"Posted comment to MR !{mr_iid}")
    except Exception as e:
        logger.error(f"Failed to post comment: {e}")
        raise ReviewError(f"发表评论失败: {str(e)}")


def post_mr_inline_comment(
    project_id: int,
    mr_iid: int,
    content: str,
    new_path: str,
    new_line: int,
    diff_refs: dict,
    old_path: str = "",
) -> dict:
    """在 MR 的文件行位置发表评论（discussion）。返回 discussion 身份。"""
    cfg = require_gitlab_config()
    gitlab_url = cfg["url"]
    token = cfg["token"]

    base_sha = diff_refs.get("base_sha", "")
    start_sha = diff_refs.get("start_sha", "")
    head_sha = diff_refs.get("head_sha", "")
    if not (base_sha and start_sha and head_sha):
        raise ReviewError("缺少 diff_refs，无法发布行内评论")

    url = f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions"
    headers = {
        "PRIVATE-TOKEN": token,
        "Content-Type": "application/json",
    }

    payload = {
        "body": content,
        "position": {
            "position_type": "text",
            "base_sha": base_sha,
            "start_sha": start_sha,
            "head_sha": head_sha,
            "new_path": new_path,
            "old_path": old_path or new_path,
            "new_line": int(new_line),
        },
    }

    try:
        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.post(url, headers=headers, json=payload, timeout=30, verify=False)
        response.raise_for_status()
        logger.info("Posted inline comment to MR !%s at %s:%s", mr_iid, new_path, new_line)
        return _extract_discussion_identity(response)
    except ReviewError:
        raise
    except Exception as e:
        logger.error("Failed to post inline comment: %s", e)
        raise ReviewError(f"发布行内评论失败: {str(e)}")


def post_mr_file_comment(
    project_id: int,
    mr_iid: int,
    content: str,
    new_path: str,
    diff_refs: dict,
    old_path: str = "",
) -> dict:
    """在 MR 文件级位置发表评论（discussion，position_type=file）。返回 discussion 身份。"""
    cfg = require_gitlab_config()
    gitlab_url = cfg["url"]
    token = cfg["token"]

    base_sha = diff_refs.get("base_sha", "")
    start_sha = diff_refs.get("start_sha", "")
    head_sha = diff_refs.get("head_sha", "")
    if not (base_sha and start_sha and head_sha):
        raise ReviewError("缺少 diff_refs，无法发布文件级评论")

    url = f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions"
    headers = {
        "PRIVATE-TOKEN": token,
        "Content-Type": "application/json",
    }

    payload = {
        "body": content,
        "position": {
            "position_type": "file",
            "base_sha": base_sha,
            "start_sha": start_sha,
            "head_sha": head_sha,
            "new_path": new_path,
            "old_path": old_path or new_path,
        },
    }

    try:
        import requests
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.post(url, headers=headers, json=payload, timeout=30, verify=False)
        response.raise_for_status()
        logger.info("Posted file-level comment to MR !%s at %s", mr_iid, new_path)
        return _extract_discussion_identity(response)
    except ReviewError:
        raise
    except Exception as e:
        logger.error("Failed to post file-level comment: %s", e)
        raise ReviewError(f"发布文件级评论失败: {str(e)}")


def _extract_discussion_identity(response) -> dict:
    """
    从 discussions 接口的响应里取出 discussion_id 与首条 note_id。

    这是 Finding 能否被追踪的唯一依据 —— 拿不到就意味着这条建议永远无法结算，
    因此解析失败时返回空值而不是抛错：评论本身已经发出去了，不该因为解析失败而回滚。
    """
    try:
        payload = response.json()
    except Exception:
        logger.warning("Discussion response is not JSON, finding will be untrackable")
        return {"discussion_id": "", "note_id": None}

    if not isinstance(payload, dict):
        return {"discussion_id": "", "note_id": None}

    discussion_id = str(payload.get("id") or "").strip()
    note_id = None
    notes = payload.get("notes")
    if isinstance(notes, list) and notes:
        first = notes[0]
        if isinstance(first, dict):
            try:
                note_id = int(first.get("id"))
            except (TypeError, ValueError):
                note_id = None

    if not discussion_id:
        logger.warning("Discussion response missing id, finding will be untrackable")
    return {"discussion_id": discussion_id, "note_id": note_id}


def _gitlab_get(path: str, params: dict = None, timeout: int = 30):
    """GitLab GET 的统一封装。"""
    import requests
    import urllib3

    cfg = require_gitlab_config()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = f"{cfg['url']}/api/v4{path}"
    response = requests.get(
        url,
        headers={"PRIVATE-TOKEN": cfg["token"]},
        params=params or {},
        timeout=timeout,
        verify=False,
    )
    response.raise_for_status()
    return response


def list_group_projects(group_path: str, page: int = 1, per_page: int = 100) -> List[dict]:
    """
    列出某个组织（含子组）下的项目，一页一页取。

    归档项目由 API 侧过滤掉：它们按定义已经不再维护，对它们提问题没有收件人。
    调用方负责翻页与去重——这里只做一次 HTTP，不替它决定翻几页。
    """
    encoded = quote(str(group_path or "").strip().strip("/"), safe="")
    response = _gitlab_get(
        f"/groups/{encoded}/projects",
        params={
            "per_page": per_page,
            "page": page,
            "include_subgroups": "true",
            "archived": "false",
            "simple": "true",
        },
    )
    data = response.json() or []
    return data if isinstance(data, list) else []


def get_mr_state(project_id: int, mr_iid: int) -> dict:
    """读取 MR 当前状态，用于判断是否到了结算时机。"""
    response = _gitlab_get(f"/projects/{project_id}/merge_requests/{mr_iid}")
    data = response.json() or {}
    return {
        "state": str(data.get("state") or "").strip().lower(),
        "merged_at": data.get("merged_at") or "",
        "closed_at": data.get("closed_at") or "",
        "title": data.get("title") or "",
    }


def get_mr_discussions(project_id: int, mr_iid: int, max_pages: int = 10) -> List[dict]:
    """分页拉取 MR 的全部 discussion。"""
    discussions: List[dict] = []
    for page in range(1, max(int(max_pages), 1) + 1):
        response = _gitlab_get(
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions",
            params={"per_page": 100, "page": page},
        )
        batch = response.json() or []
        if not isinstance(batch, list) or not batch:
            break
        discussions.extend(batch)
        if len(batch) < 100:
            break
    return discussions


def get_note_award_emoji(project_id: int, mr_iid: int, note_id: int) -> List[str]:
    """
    读取某条 note 上的表态 emoji。

    GitLab 的 discussions 接口不返回 emoji，只能逐条查 —— 一个有 20 条建议的 MR
    会产生 20 次调用。因此只在结算时刻打一次，绝不对进行中的 MR 轮询。
    单条失败按"无表态"降级，不让整个结算失败。
    """
    try:
        response = _gitlab_get(
            f"/projects/{project_id}/merge_requests/{mr_iid}/notes/{note_id}/award_emoji",
            params={"per_page": 100},
            timeout=15,
        )
        payload = response.json() or []
    except Exception as e:
        logger.warning("Failed to read award emoji for note %s: %s", note_id, e)
        return []

    if not isinstance(payload, list):
        return []
    return [str(item.get("name") or "").strip() for item in payload if isinstance(item, dict)]
