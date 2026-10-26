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


def get_group_full_path(group_path: str) -> str:
    """
    读取组织当前的完整路径（full_path）。

    组织改名或转移后，旧路径仍会被 GitLab 重定向到新组织，但返回的项目路径都是新路径；
    填数字 id 时也只有这样才能拿到路径。拿不到时返回空串，由调用方决定如何处理。
    """
    encoded = quote(str(group_path or "").strip().strip("/"), safe="")
    response = _gitlab_get(f"/groups/{encoded}", params={"with_projects": "false"})
    data = response.json() or {}
    return str(data.get("full_path") or "").strip() if isinstance(data, dict) else ""


def list_group_projects(group_path: str, page: int = 1, per_page: int = 100) -> List[dict]:
    """
    列出某个组织（含子组）下的项目，一页一页取。

    归档项目由 API 侧过滤掉：它们按定义已经不再维护，对它们提问题没有收件人。
    共享进来的项目同样由 API 侧过滤：with_shared 默认是 true，不显式关掉的话，
    其他组织通过 "Share with group" 共享给它的项目也会被当成组织下的项目巡检。
    调用方负责翻页与去重——这里只做一次 HTTP，不替它决定翻几页。
    """
    encoded = quote(str(group_path or "").strip().strip("/"), safe="")
    response = _gitlab_get(
        f"/groups/{encoded}/projects",
        params={
            "per_page": per_page,
            "page": page,
            "include_subgroups": "true",
            "with_shared": "false",
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


# ---------------------------------------------------------------------------
# 巡检修复（FixRun）用到的接口
# ---------------------------------------------------------------------------

# 成员接口最多翻几页（每页 100）。超大的组织继承成员可能上千，找原始作者与负责人不需要看全
MAX_MEMBER_PAGES = 10


def _gitlab_send(method: str, path: str, payload: Optional[dict] = None, timeout: int = 30, allow_404: bool = False):
    """
    GitLab 写操作（以及需要区分 404 的读操作）的统一封装。

    失败时抛 ReviewError，消息里带上平台返回的原因（例如权限不足、分支已有合并请求）。
    allow_404=True 时 404 不算失败、返回 None：清理时"分支已经不在了"是正常情况，不是错误。
    """
    import requests
    import urllib3

    cfg = require_gitlab_config()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        response = requests.request(
            method,
            f"{cfg['url']}/api/v4{path}",
            headers={"PRIVATE-TOKEN": cfg["token"]},
            json=payload,
            timeout=timeout,
            verify=False,
        )
    except requests.RequestException as e:
        raise ReviewError(f"请求 GitLab 失败：{e}") from e
    if allow_404 and response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise ReviewError(f"GitLab 返回 {response.status_code}：{response.text[:500]}")
    return response


def _gitlab_post(path: str, payload: dict, timeout: int = 30):
    """GitLab POST 的统一封装，见 _gitlab_send。"""
    return _gitlab_send("POST", path, payload, timeout)


def encode_project_path(project_path: str) -> str:
    """把 group/sub/project 编码成接口路径里的项目 id。"""
    return quote(str(project_path or "").strip().strip("/"), safe="")


def get_project(project_path: str) -> dict:
    """读取项目信息（含 web_url、默认分支）。"""
    response = _gitlab_get(f"/projects/{encode_project_path(project_path)}")
    data = response.json() or {}
    return data if isinstance(data, dict) else {}


def list_project_members(project_path: str) -> List[dict]:
    """
    列出项目成员，包括从上级组织继承来的（members/all）。

    要用含继承的：多数人是在组织上被授权的，只看直属成员会把他们都当成"已不在项目里"。
    """
    members: List[dict] = []
    for page in range(1, MAX_MEMBER_PAGES + 1):
        response = _gitlab_get(
            f"/projects/{encode_project_path(project_path)}/members/all",
            params={"per_page": 100, "page": page},
        )
        batch = response.json() or []
        if not isinstance(batch, list) or not batch:
            break
        members.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
    return members


def search_users(query: str) -> List[dict]:
    """
    按关键字搜索用户。

    管理员令牌能按任意邮箱搜到人，普通令牌只能搜到公开了邮箱的人 —— 搜不到不代表这个人不存在，
    调用方要有别的匹配方式兜底。
    """
    response = _gitlab_get("/users", params={"search": str(query or "").strip(), "per_page": 20})
    data = response.json() or []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def get_user_by_username(username: str, timeout: int = 30) -> dict:
    """
    按用户名精确查找用户，找不到时返回空字典。

    不用 search_users：模糊搜索按用户名、姓名、邮箱一起匹配，用户名是工号一类的数字时，
    别人的邮箱或姓名里恰好含这串数字也会被搜出来。
    """
    response = _gitlab_get("/users", params={"username": str(username or "").strip()}, timeout=timeout)
    data = response.json() or []
    users = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    return users[0] if users else {}


def get_current_user() -> dict:
    """令牌所属的账号。"""
    response = _gitlab_get("/user")
    data = response.json() or {}
    return data if isinstance(data, dict) else {}


def create_merge_request(
    project_path: str,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    assignee_id: Optional[int] = None,
) -> dict:
    """创建合并请求，返回平台的响应（含 iid 与 web_url）。合并后删除源分支：那是机器建的一次性分支。"""
    payload = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "description": description,
        "remove_source_branch": True,
    }
    if assignee_id:
        payload["assignee_id"] = int(assignee_id)
    response = _gitlab_post(f"/projects/{encode_project_path(project_path)}/merge_requests", payload)
    data = response.json() or {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 清理修复合并请求用到的接口
# ---------------------------------------------------------------------------

def _json_object_or_none(response) -> Optional[dict]:
    """把 allow_404 请求的响应解析成对象：404（response 为 None）返回 None，响应体不是对象时返回空字典。"""
    if response is None:
        return None
    data = response.json() or {}
    return data if isinstance(data, dict) else {}


def get_branch(project_path: str, branch: str) -> Optional[dict]:
    """读取分支信息（含分支头提交 commit.id 与 commit.parent_ids）；分支不存在时返回 None。"""
    response = _gitlab_send(
        "GET", f"/projects/{encode_project_path(project_path)}/repository/branches/{quote(branch, safe='')}",
        allow_404=True,
    )
    return _json_object_or_none(response)


def get_merge_request(project_path: str, mr_iid: int) -> Optional[dict]:
    """读取合并请求（含 state：opened / closed / merged / locked）；不存在（已被删除）时返回 None。"""
    response = _gitlab_send(
        "GET", f"/projects/{encode_project_path(project_path)}/merge_requests/{int(mr_iid)}", allow_404=True
    )
    return _json_object_or_none(response)


def get_latest_merge_request_note(project_path: str, mr_iid: int) -> Optional[dict]:
    """读取合并请求上最新的一条评论（含系统记录，如改派、改标题），没有评论时返回 None。"""
    response = _gitlab_send(
        "GET",
        f"/projects/{encode_project_path(project_path)}/merge_requests/{int(mr_iid)}/notes"
        "?sort=desc&order_by=created_at&per_page=1",
    )
    data = response.json()
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None


def close_merge_request(project_path: str, mr_iid: int) -> None:
    """关闭合并请求。关闭而不是删除：删除要 Owner 权限，还会把上面的讨论一起抹掉，关闭之后需要时仍能重新打开。"""
    _gitlab_send(
        "PUT", f"/projects/{encode_project_path(project_path)}/merge_requests/{int(mr_iid)}",
        {"state_event": "close"},
    )


def delete_branch(project_path: str, branch: str) -> bool:
    """删除分支，返回是否删掉了；分支本来就不存在时返回 False。"""
    response = _gitlab_send(
        "DELETE", f"/projects/{encode_project_path(project_path)}/repository/branches/{quote(branch, safe='')}",
        allow_404=True,
    )
    return response is not None
