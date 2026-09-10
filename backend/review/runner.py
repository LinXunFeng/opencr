#!/usr/bin/env python3
"""
ReviewRun 的统一执行入口。

webhook 与手动触发原先各自复制了一份投递逻辑，导致状态机要写两遍且必然漂移。
这里把两条路径收敛成一个 `execute_review_run`，埋点只存在于一处。
"""

import logging
from typing import List, Optional

from ..storage import repo
from ..storage.models import (
    DEGRADE_DIFF_TRUNCATED,
    DEGRADE_INLINE_POST_FAILED,
    DELIVERY_FALLBACK_NOTE,
    DELIVERY_FILE_LEVEL,
    DELIVERY_INLINE,
    DELIVERY_SUMMARY_ONLY,
    ERROR_REVIEW,
    ERROR_UNEXPECTED,
    PHASE_FETCHING,
    PHASE_MATCHING_SKILL,
    PHASE_PUBLISHING,
    PHASE_REVIEWING,
    RUN_FAILED,
    RUN_SUCCEEDED,
)
from .ai import parse_severity, parse_summary_findings, review_changes_with_inline_notes
from .common import (
    REVIEW_MODE_FILE,
    REVIEW_MODE_HYBRID,
    REVIEW_MODE_OVERALL,
    ReviewError,
    normalize_review_mode,
)
from .config import load_review_config
from .diff import normalize_change_diff
from .gitlab import (
    enrich_changes_with_file_info,
    get_compare_changes,
    get_mr_changes_with_refs,
    post_mr_comment,
    post_mr_file_comment,
    post_mr_inline_comment,
)

logger = logging.getLogger(__name__)


def post_inline_comment_with_offset(
    project_id: int,
    mr_iid: int,
    content: str,
    new_path: str,
    old_path: str,
    source_line: int,
    diff_refs: dict,
    log_prefix: str = "",
) -> tuple:
    """
    行内评论优先发布到问题行的下一行，避免评论遮挡目标代码。
    若下一行定位失败，则自动回退到原始行。
    返回 (最终行号, discussion 身份)。
    """
    preferred_line = max(int(source_line) + 1, 1)
    candidate_lines = [preferred_line]
    if source_line not in candidate_lines:
        candidate_lines.append(int(source_line))

    last_error = None
    for target_line in candidate_lines:
        try:
            identity = post_mr_inline_comment(
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
            return target_line, (identity or {})
        except Exception as e:
            last_error = e
            if target_line != source_line:
                logger.warning(
                    "%s Inline note next-line placement failed for MR !%s at %s:%s, "
                    "fallback to source line %s: %s",
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


def build_inline_fallback_comments_by_file(failed_notes: list) -> list:
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


def is_binary_or_non_text_change(change: dict) -> bool:
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


def _resolve_changes(
    project_id: int,
    mr_iid: int,
    run_uid: str,
    normalized_mode: str,
    action: str,
    update_from_sha: str,
    update_to_sha: str,
    original_input: Optional[dict] = None,
    review_skill: str = "",
) -> tuple:
    """拉取本次要审查的变更集。增量场景只取本次新提交区间。"""
    repo.update_progress(run_uid, phase=PHASE_FETCHING)
    if original_input is not None:
        # 原范围不读取当前 MR diff，避免后续推送把 A→B 偷换为 A→C。
        changes = get_compare_changes(project_id, original_input["from_sha"], original_input["to_sha"])
        return changes, original_input["diff_refs"], original_input["to_sha"]
    all_changes, diff_refs = get_mr_changes_with_refs(project_id, mr_iid)
    changes_for_review = all_changes
    file_info_ref = str(diff_refs.get("head_sha", "") or "").strip()

    incremental = action == "update" and normalized_mode == REVIEW_MODE_FILE and bool(update_from_sha)
    target_sha = (update_to_sha or file_info_ref) if incremental else file_info_ref
    # 快照中的投递位置也必须指向被审查的提交，不能携带后续推送的 head。
    snapshot_refs = dict(diff_refs)
    if incremental and target_sha != file_info_ref:
        snapshot_refs = {"base_sha": update_from_sha, "start_sha": update_from_sha, "head_sha": target_sha}
    repo.save_review_input(run_uid, {
        "review_skill": review_skill,
        "from_sha": update_from_sha if incremental else diff_refs.get("base_sha", ""),
        "to_sha": target_sha, "diff_refs": snapshot_refs,
    })

    if action == "update" and normalized_mode == REVIEW_MODE_FILE and update_from_sha:
        target_to_sha = (update_to_sha or diff_refs.get("head_sha", "")).strip()
        if not target_to_sha:
            logger.warning("Incremental review skipped: missing update_to_sha for MR !%s", mr_iid)
            return [], diff_refs, ""
        if update_from_sha == target_to_sha:
            logger.info(
                "Incremental review skipped: from_sha equals to_sha for MR !%s (%s)",
                mr_iid,
                update_from_sha[:8],
            )
            return [], diff_refs, ""

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
            "Incremental changes resolved for MR !%s: from=%s to=%s count=%s",
            mr_iid,
            update_from_sha[:8],
            target_to_sha[:8],
            len(changes_for_review),
        )
        logger.info(
            "Incremental file list for MR !%s: unique_count=%s, files=%s",
            mr_iid,
            len(incremental_paths),
            ", ".join(preview_paths) if preview_paths else "<empty>",
        )

    return changes_for_review, diff_refs, file_info_ref


def _publish_findings(
    project_id: int,
    mr_iid: int,
    run_uid: str,
    inline_notes: List[dict],
    changes_for_review: List[dict],
    diff_refs: dict,
    log_prefix: str,
) -> tuple:
    """投递行内/文件级评论，并把每条 Finding 落库。返回 (成功数, 失败数, 降级评论列表)。"""
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
        severity = parse_severity(body)

        # 二进制/非文本文件，或未提供有效行号的问题，优先尝试文件级 discussion。
        # 若平台不支持 position_type=file，再降级为 MR 普通评论。
        if line <= 0 or is_binary_or_non_text_change(change):
            try:
                identity = post_mr_file_comment(
                    project_id=project_id,
                    mr_iid=mr_iid,
                    content=body,
                    new_path=new_path,
                    old_path=old_path,
                    diff_refs=diff_refs,
                ) or {}
                inline_ok += 1
                repo.record_finding(
                    run_uid=run_uid,
                    file_path=new_path,
                    line=0,
                    body=body,
                    delivery=DELIVERY_FILE_LEVEL,
                    severity=severity,
                    discussion_id=identity.get("discussion_id", ""),
                    note_id=identity.get("note_id"),
                )
                logger.info(
                    "%s Posted file-level discussion on MR !%s: %s (line=%s)",
                    log_prefix,
                    mr_iid,
                    new_path,
                    line,
                )
            except Exception as e:
                inline_fail += 1
                failed_inline_notes.append(
                    {"file_path": new_path, "line": 0, "body": body, "severity": severity}
                )
                logger.warning(
                    "%s File-level discussion unavailable for MR !%s at %s (line=%s), "
                    "fallback to MR note: %s",
                    log_prefix,
                    mr_iid,
                    new_path,
                    line,
                    e,
                )
            continue

        try:
            _, identity = post_inline_comment_with_offset(
                project_id=project_id,
                mr_iid=mr_iid,
                content=body,
                new_path=new_path,
                old_path=old_path,
                diff_refs=diff_refs,
                source_line=line,
                log_prefix=log_prefix,
            )
            inline_ok += 1
            repo.record_finding(
                run_uid=run_uid,
                file_path=new_path,
                line=line,
                body=body,
                delivery=DELIVERY_INLINE,
                severity=severity,
                discussion_id=(identity or {}).get("discussion_id", ""),
                note_id=(identity or {}).get("note_id"),
            )
        except Exception as e:
            inline_fail += 1
            failed_inline_notes.append(
                {"file_path": new_path, "line": line, "body": body, "severity": severity}
            )
            logger.warning(
                "%s Inline comment failed for MR !%s at %s (source_line=%s): %s",
                log_prefix,
                mr_iid,
                new_path,
                line,
                e,
            )

    logger.info(
        "%s Inline comments posted: success=%s, failed=%s, extracted=%s",
        log_prefix,
        inline_ok,
        inline_fail,
        len(inline_notes),
    )

    # 降级投递的 Finding 仍然入库，但永远不可追踪 —— 它们计入 Coverage 分母，不计入采纳率分母
    for item in failed_inline_notes:
        repo.record_finding(
            run_uid=run_uid,
            file_path=str(item.get("file_path") or ""),
            line=int(item.get("line") or 0),
            body=str(item.get("body") or ""),
            delivery=DELIVERY_FALLBACK_NOTE,
            severity=str(item.get("severity") or "unknown"),
        )
    if inline_fail:
        repo.add_degradation(run_uid, DEGRADE_INLINE_POST_FAILED, inline_fail)

    fallback_comments = build_inline_fallback_comments_by_file(failed_inline_notes)
    if fallback_comments:
        logger.info(
            "%s Inline fallback grouped by file: files=%s, comments=%s",
            log_prefix,
            len({str(n.get("file_path") or "unknown") for n in failed_inline_notes}),
            len(fallback_comments),
        )
    return inline_ok, inline_fail, fallback_comments


def execute_review_run(
    run_uid: str,
    project_id: int,
    mr_iid: int,
    mr_title: str,
    review_mode: str,
    review_skill: str,
    action: str = "",
    update_from_sha: str = "",
    update_to_sha: str = "",
    log_prefix: str = "[Run]",
    original_input: Optional[dict] = None,
) -> None:
    """
    执行一次 ReviewRun 的完整流程，并全程更新 ReviewRun 状态。

    webhook 与手动触发都走这里，状态机只有这一份。
    """
    try:
        # 模型或 GitLab 调用失败之前保留原始参数；尚未取到范围时原范围选项保持禁用。
        repo.save_review_input(run_uid, original_input or {"review_skill": review_skill})
        review_cfg = load_review_config()
        normalized_mode = normalize_review_mode(review_mode)

        logger.info("%s Fetching changes for MR !%s", log_prefix, mr_iid)
        changes_for_review, diff_refs, file_info_ref = _resolve_changes(
            project_id, mr_iid, run_uid, normalized_mode, action, update_from_sha, update_to_sha,
            original_input, review_skill
        )

        if not changes_for_review:
            logger.info("%s No changes to review for MR !%s, skip", log_prefix, mr_iid)
            repo.finish_run(run_uid, RUN_SUCCEEDED)
            return

        changes_for_review = enrich_changes_with_file_info(
            project_id=project_id,
            changes=changes_for_review,
            ref=file_info_ref,
        )
        size_known_count = sum(
            1 for c in changes_for_review if c.get("file_size_bytes") is not None
        )
        logger.info(
            "%s File metadata attached: total=%s, size_known=%s, ref=%s",
            log_prefix,
            len(changes_for_review),
            size_known_count,
            file_info_ref[:8] if file_info_ref else "<empty>",
        )

        repo.update_progress(
            run_uid,
            phase=PHASE_MATCHING_SKILL,
            files_total=len(changes_for_review),
            files_done=0,
        )

        # 超出上限的 diff 会被 truncate_diff 截断：流程仍会成功，但审查质量已降级
        total_diff_chars = sum(
            len(normalize_change_diff(c) or "") for c in changes_for_review
        )
        if total_diff_chars > int(review_cfg["max_diff_size"]):
            repo.add_degradation(run_uid, DEGRADE_DIFF_TRUNCATED)

        repo.update_progress(run_uid, phase=PHASE_REVIEWING)
        logger.info("%s Review mode=%s, skill=%s", log_prefix, review_mode, review_skill)

        def _on_progress(done: int, total: int) -> None:
            """逐文件进度回调；同时刷新心跳，避免长审查被误判为 Stale。"""
            repo.update_progress(run_uid, files_done=done, files_total=total)

        matched_skills = set()

        def _on_skills(names: List[str]) -> None:
            """累积本次 ReviewRun 命中的技能并写入统计记录。"""
            # 同一技能可能命中多个文件或 hybrid 的两个分支，每个 ReviewRun 只计一次。
            # 匹配后立即记录，后续模型调用失败也不会丢失已经发生的命中。
            matched_skills.update(names)
            repo.update_progress(run_uid, review_skills=sorted(matched_skills))

        review_result, inline_notes = review_changes_with_inline_notes(
            changes_for_review,
            review_mode=review_mode,
            review_skill=review_skill,
            max_diff_size=review_cfg["max_diff_size"],
            skills_dir=review_cfg["skills_dir"],
            progress_cb=_on_progress,
            skills_cb=_on_skills,
        )

        repo.update_progress(run_uid, phase=PHASE_PUBLISHING)
        inline_ok, inline_fail, fallback_comments = _publish_findings(
            project_id=project_id,
            mr_iid=mr_iid,
            run_uid=run_uid,
            inline_notes=inline_notes,
            changes_for_review=changes_for_review,
            diff_refs=diff_refs,
            log_prefix=log_prefix,
        )

        if normalized_mode in {REVIEW_MODE_OVERALL, REVIEW_MODE_HYBRID}:
            if review_result.strip():
                post_mr_comment(project_id, mr_iid, review_result.strip())
                # 整体评论走的是不可 resolve 的普通 note，其中的 Finding 永远不可追踪。
                # 仍然记账，是为了让 Coverage 的分母反映真实产出量。
                for item in parse_summary_findings(review_result):
                    body = str(item.get("body") or "")
                    repo.record_finding(
                        run_uid=run_uid,
                        file_path=str(item.get("file_path") or ""),
                        line=int(item.get("line") or 0),
                        body=body,
                        delivery=DELIVERY_SUMMARY_ONLY,
                        severity=parse_severity(body),
                    )
            for fallback_comment in fallback_comments:
                post_mr_comment(project_id, mr_iid, fallback_comment)
        elif normalized_mode == REVIEW_MODE_FILE and fallback_comments:
            for fallback_comment in fallback_comments:
                post_mr_comment(project_id, mr_iid, fallback_comment)
        else:
            logger.info(
                "%s Skip MR summary comment: mr_iid=%s, mode=%s, inline_success=%s",
                log_prefix,
                mr_iid,
                normalized_mode,
                inline_ok,
            )

        repo.finish_run(run_uid, RUN_SUCCEEDED)
        logger.info("%s Successfully reviewed MR !%s", log_prefix, mr_iid)

    except ReviewError as e:
        logger.error("%s Review failed for MR !%s: %s", log_prefix, mr_iid, e)
        repo.finish_run(run_uid, RUN_FAILED, ERROR_REVIEW, str(e))
        try:
            post_mr_comment(project_id, mr_iid, f"❌ 代码审查失败\n\n```\n{str(e)}\n```")
        except Exception:
            # GitLab 不可用时 MR 无法收到失败反馈，仍保留已落库的原始审查错误。
            logger.warning("MR !%s 的审查失败反馈发送失败", mr_iid, exc_info=True)
    except Exception as e:
        logger.exception("%s Unexpected error for MR !%s", log_prefix, mr_iid)
        repo.finish_run(run_uid, RUN_FAILED, ERROR_UNEXPECTED, str(e))
