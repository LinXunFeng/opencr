#!/usr/bin/env python3
"""
OpenCR - 自动代码审查服务（入口与路由）
优先读取 config.yaml，支持环境变量覆盖，兼容 ~/.codex 回退
"""

import logging
import os
import re
import sys
import threading
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, request

def _load_review_dependencies() -> dict:
    """
    统一加载 review 依赖。
    脚本模式（cd src && python3 review_server.py）下先补齐项目根路径。
    """
    if __package__ in {None, ""}:
        from pathlib import Path

        project_root = str(Path(__file__).resolve().parent.parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

    from src.review.ai import build_review_prompt, call_codex_review, review_changes, review_changes_with_inline_notes
    from src.review.common import (
        DEFAULT_REVIEW_SKILLS_DIR,
        REVIEW_MODE_FILE,
        REVIEW_MODE_HYBRID,
        REVIEW_MODE_OVERALL,
        ReviewError,
        normalize_review_mode,
        normalize_review_skill,
    )
    from src.review.config import (
        _pick_config_value,
        get_app_version,
        load_file_config,
        load_gitlab_config,
        load_openai_config,
        load_review_config,
    )
    from src.review.diff import build_diff_from_changes, normalize_change_diff, truncate_diff
    from src.review.gitlab import (
        enrich_changes_with_file_info,
        get_compare_changes,
        get_mr_changes,
        get_mr_changes_with_refs,
        get_mr_diff,
        post_mr_comment,
        post_mr_file_comment,
        post_mr_inline_comment,
        require_gitlab_config,
    )
    from src.review.skills import (
        _parse_selected_skill,
        auto_select_review_skill,
        auto_select_review_skills,
        load_available_review_skills,
        load_review_skill_prompt,
        load_review_skill_prompts,
        resolve_review_options,
    )

    return {
        "build_review_prompt": build_review_prompt,
        "call_codex_review": call_codex_review,
        "review_changes": review_changes,
        "review_changes_with_inline_notes": review_changes_with_inline_notes,
        "DEFAULT_REVIEW_SKILLS_DIR": DEFAULT_REVIEW_SKILLS_DIR,
        "REVIEW_MODE_FILE": REVIEW_MODE_FILE,
        "REVIEW_MODE_HYBRID": REVIEW_MODE_HYBRID,
        "REVIEW_MODE_OVERALL": REVIEW_MODE_OVERALL,
        "ReviewError": ReviewError,
        "normalize_review_mode": normalize_review_mode,
        "normalize_review_skill": normalize_review_skill,
        "_pick_config_value": _pick_config_value,
        "get_app_version": get_app_version,
        "load_file_config": load_file_config,
        "load_gitlab_config": load_gitlab_config,
        "load_openai_config": load_openai_config,
        "load_review_config": load_review_config,
        "build_diff_from_changes": build_diff_from_changes,
        "normalize_change_diff": normalize_change_diff,
        "truncate_diff": truncate_diff,
        "enrich_changes_with_file_info": enrich_changes_with_file_info,
        "get_mr_changes": get_mr_changes,
        "get_mr_changes_with_refs": get_mr_changes_with_refs,
        "get_compare_changes": get_compare_changes,
        "get_mr_diff": get_mr_diff,
        "post_mr_comment": post_mr_comment,
        "post_mr_file_comment": post_mr_file_comment,
        "post_mr_inline_comment": post_mr_inline_comment,
        "require_gitlab_config": require_gitlab_config,
        "_parse_selected_skill": _parse_selected_skill,
        "auto_select_review_skill": auto_select_review_skill,
        "auto_select_review_skills": auto_select_review_skills,
        "load_available_review_skills": load_available_review_skills,
        "load_review_skill_prompt": load_review_skill_prompt,
        "load_review_skill_prompts": load_review_skill_prompts,
        "resolve_review_options": resolve_review_options,
    }


globals().update(_load_review_dependencies())

# 兼容旧私有函数名（供历史调用/测试）
# 旧测试/历史调用可能仍引用该私有别名，保留以避免破坏兼容性。
_normalize_review_mode = normalize_review_mode
_normalize_review_skill = normalize_review_skill
_normalize_change_diff = normalize_change_diff

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.expanduser("~/opencr/logs/server.log")),
    ],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def validate_webhook_token(f):
    """Webhook 安全验证装饰器"""

    @wraps(f)
    def decorated(*args, **kwargs):
        secret_token = load_gitlab_config()["webhook_secret"]
        if secret_token:
            header_token = request.headers.get("X-Gitlab-Token")
            if header_token != secret_token:
                logger.warning("Invalid webhook token")
                return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated


def should_review_mr(data: dict) -> tuple:
    """判断是否应该审查此 MR，并返回建议审查模式。"""
    attrs = data.get("object_attributes", {})

    action = (attrs.get("action") or "").strip().lower()
    if action == "open":
        target_mode = REVIEW_MODE_HYBRID
        action_reason = "MR 创建，触发整体+文件级审查"
    elif action == "reopen":
        return False, "忽略 action=reopen（关闭后重新开启不触发审查）", ""
    elif action == "update":
        oldrev = str(attrs.get("oldrev") or "").strip().lower()
        is_commit_update = bool(re.fullmatch(r"[0-9a-f]{40}", oldrev) and oldrev != ("0" * 40))
        if not is_commit_update:
            return False, f"忽略 action=update（非新提交触发）", ""
        target_mode = REVIEW_MODE_FILE
        action_reason = f"检测到 MR 新提交 oldrev={oldrev[:8]}，触发文件级审查"
    else:
        return False, f"忽略 action={action or '<empty>'} 的 MR", ""

    title = attrs.get("title", "").lower()
    skip_keywords = ["wip", "draft", "skip-review", "[skip ci]"]
    for kw in skip_keywords:
        if kw in title:
            return False, f"标题包含跳过标记: {kw}", ""

    source_branch = attrs.get("source_branch", "")
    if source_branch.startswith("dependabot/"):
        return False, "跳过依赖更新 MR", ""

    return True, action_reason, target_mode


@app.route("/health", methods=["GET"])
def health_check():
    """健康检查端点"""
    cfg = load_openai_config()
    return jsonify(
        {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "version": get_app_version(),
            "model": cfg.get("model", "unknown"),
            "base_url": cfg.get("base_url", "unknown"),
        }
    )


def _post_inline_comment_with_offset(
    project_id: int,
    mr_iid: int,
    content: str,
    new_path: str,
    old_path: str,
    source_line: int,
    diff_refs: dict,
    log_prefix: str = "",
) -> int:
    """
    行内评论优先发布到问题行的下一行，避免评论遮挡目标代码。
    若下一行定位失败，则自动回退到原始行。
    返回最终成功发布的行号。
    """
    preferred_line = max(int(source_line) + 1, 1)
    candidate_lines = [preferred_line]
    if source_line not in candidate_lines:
        candidate_lines.append(int(source_line))

    last_error = None
    for target_line in candidate_lines:
        try:
            post_mr_inline_comment(
                project_id=project_id,
                mr_iid=mr_iid,
                content=content,
                new_path=new_path,
                old_path=old_path,
                new_line=target_line,
                diff_refs=diff_refs,
            )
            if target_line != source_line:
                logger.info(
                    "%s Inline note line shifted for MR !%s: %s source_line=%s -> target_line=%s",
                    log_prefix,
                    mr_iid,
                    new_path,
                    source_line,
                    target_line,
                )
            return target_line
        except Exception as e:
            last_error = e
            if target_line != source_line:
                logger.warning(
                    "%s Inline note next-line placement failed for MR !%s at %s:%s, fallback to source line %s: %s",
                    log_prefix,
                    mr_iid,
                    new_path,
                    target_line,
                    source_line,
                    e,
                )
                continue
            raise

    if last_error:
        raise last_error
    raise ReviewError("Inline comment 发布失败（未知错误）")


def _build_inline_fallback_comments_by_file(failed_notes: list) -> list:
    """将行内评论失败的问题按文件拆分为多条 MR 普通评论内容。"""
    if not failed_notes:
        return []

    grouped_notes = {}
    ordered_files = []
    for item in failed_notes:
        file_path = str(item.get("file_path") or "unknown")
        if file_path not in grouped_notes:
            grouped_notes[file_path] = []
            ordered_files.append(file_path)
        grouped_notes[file_path].append(
            {
                "line": int(item.get("line") or 0),
                "body": str(item.get("body") or "").strip(),
            }
        )

    comments = []
    for file_path in ordered_files:
        lines = [
            f"### 文件级降级评论｜`{file_path}`",
            "",
            "> 行内评论发布失败，以下问题已降级为普通评论展示。",
            "",
        ]
        for note in grouped_notes[file_path]:
            line = note["line"]
            body = note["body"]
            location = f"{file_path}:{line}" if line > 0 else file_path
            lines.append(f"- **位置**: `{location}`")
            if body:
                lines.append(body)
            lines.append("")
        comments.append("\n".join(lines).strip())

    return comments


def _is_binary_or_non_text_change(change: dict) -> bool:
    """
    判断文件变更是否缺少可定位的文本 diff（通常为二进制/非文本文件）。
    这类文件不应尝试发布行内评论，应直接降级为文件级评论。
    """
    normalized_diff = normalize_change_diff(change or {})
    if not normalized_diff.strip():
        return True

    diff_lower = normalized_diff.lower()
    if "binary files" in diff_lower and "differ" in diff_lower:
        return True
    if "binary or non-text file changed" in diff_lower:
        return True
    return False


def process_review_async(
    project_id,
    mr_iid,
    mr_title,
    review_mode,
    review_skill,
    action="",
    update_from_sha="",
    update_to_sha="",
):
    """后台线程处理审查"""
    try:
        review_cfg = load_review_config()
        logger.info(f"[Async] Fetching changes for MR !{mr_iid}")
        all_changes, diff_refs = get_mr_changes_with_refs(project_id, mr_iid)
        changes_for_review = all_changes
        file_info_ref = str(diff_refs.get("head_sha", "") or "").strip()

        normalized_mode = normalize_review_mode(review_mode)
        if (
            action == "update"
            and normalized_mode == REVIEW_MODE_FILE
            and update_from_sha
        ):
            target_to_sha = (update_to_sha or diff_refs.get("head_sha", "")).strip()
            if not target_to_sha:
                logger.warning("[Async] Incremental review skipped: missing update_to_sha for MR !%s", mr_iid)
                return
            if update_from_sha == target_to_sha:
                logger.info(
                    "[Async] Incremental review skipped: from_sha equals to_sha for MR !%s (%s)",
                    mr_iid,
                    update_from_sha[:8],
                )
                return
            changes_for_review = get_compare_changes(project_id, update_from_sha, target_to_sha)
            file_info_ref = target_to_sha
            incremental_paths = []
            seen_paths = set()
            for change in changes_for_review:
                path = (
                    str(change.get("new_path") or "").strip()
                    or str(change.get("old_path") or "").strip()
                    or "unknown"
                )
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                incremental_paths.append(path)

            preview_limit = 30
            preview_paths = incremental_paths[:preview_limit]
            if len(incremental_paths) > preview_limit:
                preview_paths.append(f"...(+{len(incremental_paths) - preview_limit} more)")
            logger.info(
                "[Async] Incremental changes resolved for MR !%s: from=%s to=%s count=%s",
                mr_iid,
                update_from_sha[:8],
                target_to_sha[:8],
                len(changes_for_review),
            )
            logger.info(
                "[Async] Incremental file list for MR !%s: unique_count=%s, files=%s",
                mr_iid,
                len(incremental_paths),
                ", ".join(preview_paths) if preview_paths else "<empty>",
            )

        if not changes_for_review:
            logger.info("[Async] No changes to review for MR !%s, skip", mr_iid)
            return

        changes_for_review = enrich_changes_with_file_info(
            project_id=project_id,
            changes=changes_for_review,
            ref=file_info_ref,
        )
        size_known_count = sum(1 for c in changes_for_review if c.get("file_size_bytes") is not None)
        logger.info(
            "[Async] File metadata attached: total=%s, size_known=%s, ref=%s",
            len(changes_for_review),
            size_known_count,
            file_info_ref[:8] if file_info_ref else "<empty>",
        )

        logger.info(f"[Async] Review mode={review_mode}, skill={review_skill}")
        review_result, inline_notes = review_changes_with_inline_notes(
            changes_for_review,
            review_mode=review_mode,
            review_skill=review_skill,
            max_diff_size=review_cfg["max_diff_size"],
            skills_dir=review_cfg["skills_dir"],
        )

        path_to_change = {}
        for change in changes_for_review:
            np = change.get("new_path")
            op = change.get("old_path")
            if np:
                path_to_change[np] = change
            if op and op not in path_to_change:
                path_to_change[op] = change

        inline_ok = 0
        inline_fail = 0
        failed_inline_notes = []
        for note in inline_notes:
            file_path = str(note.get("file_path", "")).strip()
            line = int(note.get("line", 0))
            body = str(note.get("body", "")).strip()
            if not file_path or not body:
                continue

            change = path_to_change.get(file_path, {})
            new_path = change.get("new_path") or file_path
            old_path = change.get("old_path") or new_path

            # 二进制/非文本文件，或未提供有效行号的问题，优先尝试文件级 discussion。
            # 若平台不支持 position_type=file，再降级为 MR 普通评论。
            if line <= 0 or _is_binary_or_non_text_change(change):
                try:
                    post_mr_file_comment(
                        project_id=project_id,
                        mr_iid=mr_iid,
                        content=body,
                        new_path=new_path,
                        old_path=old_path,
                        diff_refs=diff_refs,
                    )
                    inline_ok += 1
                    logger.info(
                        "[Async] Posted file-level discussion on MR !%s: %s (line=%s)",
                        mr_iid,
                        new_path,
                        line,
                    )
                except Exception as e:
                    inline_fail += 1
                    failed_inline_notes.append(
                        {
                            "file_path": new_path,
                            "line": 0,
                            "body": body,
                        }
                    )
                    logger.warning(
                        "[Async] File-level discussion unavailable for MR !%s at %s (line=%s), fallback to MR note: %s",
                        mr_iid,
                        new_path,
                        line,
                        e,
                    )
                continue

            try:
                _post_inline_comment_with_offset(
                    project_id=project_id,
                    mr_iid=mr_iid,
                    content=body,
                    new_path=new_path,
                    old_path=old_path,
                    diff_refs=diff_refs,
                    source_line=line,
                    log_prefix="[Async]",
                )
                inline_ok += 1
            except Exception as e:
                inline_fail += 1
                failed_inline_notes.append(
                    {
                        "file_path": new_path,
                        "line": line,
                        "body": body,
                    }
                )
                logger.warning(
                    "Inline comment failed for MR !%s at %s (source_line=%s): %s",
                    mr_iid,
                    new_path,
                    line,
                    e,
                )

        logger.info(
            "[Async] Inline comments posted: success=%s, failed=%s, extracted=%s",
            inline_ok,
            inline_fail,
            len(inline_notes),
        )
        fallback_comments = _build_inline_fallback_comments_by_file(failed_inline_notes)
        if fallback_comments:
            logger.info(
                "[Async] Inline fallback grouped by file: files=%s, comments=%s",
                len({str(n.get('file_path') or 'unknown') for n in failed_inline_notes}),
                len(fallback_comments),
            )

        if normalized_mode in {REVIEW_MODE_OVERALL, REVIEW_MODE_HYBRID}:
            if review_result.strip():
                post_mr_comment(project_id, mr_iid, review_result.strip())
            for fallback_comment in fallback_comments:
                post_mr_comment(project_id, mr_iid, fallback_comment)
        elif normalized_mode == REVIEW_MODE_FILE and fallback_comments:
            for fallback_comment in fallback_comments:
                post_mr_comment(project_id, mr_iid, fallback_comment)
        else:
            logger.info(
                "[Async] Skip MR summary comment: mr_iid=%s, mode=%s, inline_success=%s",
                mr_iid,
                normalized_mode,
                inline_ok,
            )

        logger.info(f"[Async] Successfully reviewed MR !{mr_iid}")

    except ReviewError as e:
        logger.error(f"[Async] Review failed for MR !{mr_iid}: {e}")
        try:
            post_mr_comment(project_id, mr_iid, f"❌ 代码审查失败\n\n```\n{str(e)}\n```")
        except Exception:
            pass
    except Exception:
        logger.exception(f"[Async] Unexpected error for MR !{mr_iid}")


@app.route("/webhook", methods=["POST"])
@validate_webhook_token
def handle_webhook():
    """处理 GitLab Webhook - 立即返回，后台处理"""
    data = request.json

    if not data:
        return jsonify({"error": "No JSON payload"}), 400

    event_type = data.get("object_kind")
    if event_type != "merge_request":
        return jsonify({"message": f"Ignored event type: {event_type}"}), 200

    attrs = data.get("object_attributes", {})
    project = data.get("project", {})

    project_id = project.get("id")
    mr_iid = attrs.get("iid")
    mr_title = attrs.get("title")

    logger.info(
        "Received MR webhook: project_id=%s, mr_iid=%s, title=%s, action=%s",
        project_id,
        mr_iid,
        mr_title,
        attrs.get("action"),
    )

    should_review, reason, trigger_mode = should_review_mr(data)
    if not should_review:
        logger.info(f"Skipping MR !{mr_iid}: {reason}")
        return jsonify({"message": f"Skipped: {reason}"}), 200

    review_cfg = load_review_config()
    review_mode, review_skill = resolve_review_options(
        data,
        default_skill="",
        manual_mode=trigger_mode,
    )
    action = (attrs.get("action") or "").strip().lower()
    update_from_sha = ""
    update_to_sha = ""
    if action == "update":
        update_from_sha = str(attrs.get("oldrev") or "").strip()
        last_commit = attrs.get("last_commit")
        if isinstance(last_commit, dict):
            update_to_sha = str(last_commit.get("id") or last_commit.get("sha") or "").strip()
        elif isinstance(last_commit, str):
            update_to_sha = last_commit.strip()
        if not update_to_sha:
            update_to_sha = str(attrs.get("newrev") or "").strip()
    logger.info(
        "Webhook resolved review options: action=%s, trigger_mode=%s, final_mode=%s, skill=%s, "
        "reason=%s, update_from=%s, update_to=%s",
        action,
        trigger_mode,
        review_mode,
        review_skill,
        reason,
        update_from_sha[:8] if update_from_sha else "<empty>",
        update_to_sha[:8] if update_to_sha else "<empty>",
    )

    thread = threading.Thread(
        target=process_review_async,
        args=(
            project_id,
            mr_iid,
            mr_title,
            review_mode,
            review_skill,
            action,
            update_from_sha,
            update_to_sha,
        ),
    )
    thread.daemon = True
    thread.start()

    logger.info(f"Started async review for MR !{mr_iid}")
    return (
        jsonify(
            {
                "message": "Review started",
                "mr_iid": mr_iid,
                "status": "processing",
                "review_mode": review_mode,
                "review_skill": review_skill,
            }
        ),
        202,
    )


@app.route("/manual-review", methods=["POST"])
def manual_review():
    """手动触发审查"""
    data = request.json or {}
    project_id = data.get("project_id")
    mr_iid = data.get("mr_iid")

    if not project_id or not mr_iid:
        return jsonify({"error": "Missing project_id or mr_iid"}), 400

    logger.info(
        "Manual review requested: project_id=%s, mr_iid=%s, body_review_mode=%s, body_review_skill=%s",
        project_id,
        mr_iid,
        data.get("review_mode", ""),
        data.get("review_skill", ""),
    )

    try:
        review_cfg = load_review_config()
        review_mode, review_skill = resolve_review_options(
            {},
            default_skill="",
            manual_mode=str(data.get("review_mode", "")),
            manual_skill=str(data.get("review_skill", "")),
        )
        logger.info(
            "Manual review resolved options: mode=%s, skill=%s, default_skill=%s",
            review_mode,
            review_skill,
            "<empty>",
        )

        changes, diff_refs = get_mr_changes_with_refs(project_id, mr_iid)
        file_info_ref = str(diff_refs.get("head_sha", "") or "").strip()
        changes = enrich_changes_with_file_info(
            project_id=project_id,
            changes=changes,
            ref=file_info_ref,
        )
        size_known_count = sum(1 for c in changes if c.get("file_size_bytes") is not None)
        logger.info(
            "Manual file metadata attached: total=%s, size_known=%s, ref=%s",
            len(changes),
            size_known_count,
            file_info_ref[:8] if file_info_ref else "<empty>",
        )
        review_result, inline_notes = review_changes_with_inline_notes(
            changes,
            review_mode=review_mode,
            review_skill=review_skill,
            max_diff_size=review_cfg["max_diff_size"],
            skills_dir=review_cfg["skills_dir"],
        )

        path_to_change = {}
        for change in changes:
            np = change.get("new_path")
            op = change.get("old_path")
            if np:
                path_to_change[np] = change
            if op and op not in path_to_change:
                path_to_change[op] = change

        inline_ok = 0
        inline_fail = 0
        failed_inline_notes = []
        for note in inline_notes:
            file_path = str(note.get("file_path", "")).strip()
            line = int(note.get("line", 0))
            body = str(note.get("body", "")).strip()
            if not file_path or not body:
                continue

            change = path_to_change.get(file_path, {})
            new_path = change.get("new_path") or file_path
            old_path = change.get("old_path") or new_path

            # 二进制/非文本文件，或未提供有效行号的问题，优先尝试文件级 discussion。
            # 若平台不支持 position_type=file，再降级为 MR 普通评论。
            if line <= 0 or _is_binary_or_non_text_change(change):
                try:
                    post_mr_file_comment(
                        project_id=project_id,
                        mr_iid=mr_iid,
                        content=body,
                        new_path=new_path,
                        old_path=old_path,
                        diff_refs=diff_refs,
                    )
                    inline_ok += 1
                    logger.info(
                        "Manual posted file-level discussion on MR !%s: %s (line=%s)",
                        mr_iid,
                        new_path,
                        line,
                    )
                except Exception as e:
                    inline_fail += 1
                    failed_inline_notes.append(
                        {
                            "file_path": new_path,
                            "line": 0,
                            "body": body,
                        }
                    )
                    logger.warning(
                        "Manual file-level discussion unavailable for MR !%s at %s (line=%s), fallback to MR note: %s",
                        mr_iid,
                        new_path,
                        line,
                        e,
                    )
                continue
            try:
                _post_inline_comment_with_offset(
                    project_id=project_id,
                    mr_iid=mr_iid,
                    content=body,
                    new_path=new_path,
                    old_path=old_path,
                    diff_refs=diff_refs,
                    source_line=line,
                    log_prefix="[Manual]",
                )
                inline_ok += 1
            except Exception as e:
                inline_fail += 1
                failed_inline_notes.append(
                    {
                        "file_path": new_path,
                        "line": line,
                        "body": body,
                    }
                )
                logger.warning(
                    "Manual inline comment failed for MR !%s at %s (source_line=%s): %s",
                    mr_iid,
                    new_path,
                    line,
                    e,
                )

        logger.info(
            "Manual inline comments posted: success=%s, failed=%s, extracted=%s",
            inline_ok,
            inline_fail,
            len(inline_notes),
        )
        normalized_mode = normalize_review_mode(review_mode)
        fallback_comments = _build_inline_fallback_comments_by_file(failed_inline_notes)
        if fallback_comments:
            logger.info(
                "Manual inline fallback grouped by file: files=%s, comments=%s",
                len({str(n.get('file_path') or 'unknown') for n in failed_inline_notes}),
                len(fallback_comments),
            )
        if normalized_mode in {REVIEW_MODE_OVERALL, REVIEW_MODE_HYBRID}:
            if review_result.strip():
                post_mr_comment(project_id, mr_iid, review_result.strip())
            for fallback_comment in fallback_comments:
                post_mr_comment(project_id, mr_iid, fallback_comment)
        elif normalized_mode == REVIEW_MODE_FILE and fallback_comments:
            for fallback_comment in fallback_comments:
                post_mr_comment(project_id, mr_iid, fallback_comment)
        else:
            logger.info(
                "Manual review skip MR summary comment: mr_iid=%s, mode=%s, inline_success=%s",
                mr_iid,
                normalized_mode,
                inline_ok,
            )
        return (
            jsonify(
                {
                    "message": "Manual review completed",
                    "review_mode": review_mode,
                    "review_skill": review_skill,
                }
            ),
            200,
        )

    except Exception as e:
        logger.exception("Manual review failed")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    config_data = load_file_config()
    app_version = get_app_version()

    host = os.getenv("REVIEW_SERVER_HOST") or _pick_config_value(config_data, "server.host", "REVIEW_SERVER_HOST") or "0.0.0.0"
    port_value = os.getenv("REVIEW_SERVER_PORT") or _pick_config_value(config_data, "server.port", "REVIEW_SERVER_PORT") or "5000"
    try:
        port = int(port_value)
    except ValueError:
        logger.warning(f"Invalid REVIEW_SERVER_PORT={port_value}, fallback to 5000")
        port = 5000

    cfg = load_openai_config()
    logger.info(f"Starting OpenCR v{app_version} on {host}:{port}")
    logger.info(f"Model: {cfg['model']}, Base URL: {cfg['base_url']}")

    gitlab_cfg = load_gitlab_config()
    gitlab_url = gitlab_cfg["url"]
    gitlab_token = gitlab_cfg["token"]
    logger.info(f"GitLab URL: {gitlab_url[:30] if gitlab_url else 'NOT SET'}...")
    logger.info(f"GitLab Token: {'SET' if gitlab_token else 'NOT SET'}")

    app.run(host=host, port=port, debug=False)
