#!/usr/bin/env python3
"""
Verdict 结算：在 MR 合并/关闭时，为该 MR 下的 Finding 判定采纳结论。

判定口径见 docs/adr/0001-suggestion-acceptance-via-discussion-state.md。
核心分类逻辑做成纯函数，不碰网络与数据库，便于逐分支单测。
"""

import logging
from typing import Dict, List, Optional, Tuple

from ..storage import repo
from ..storage.models import (
    REASON_DISCUSSION_MISSING,
    REASON_HUMAN_REPLIED,
    REASON_MERGED_UNRESOLVED,
    REASON_RESOLVED,
    REASON_THUMBS_DOWN,
    REASON_THUMBS_UP,
    VERDICT_ACCEPTED,
    VERDICT_IGNORED,
    VERDICT_REJECTED,
    VERDICT_UNDECIDED,
)
from .gitlab import get_mr_discussions, get_mr_state, get_note_award_emoji

logger = logging.getLogger(__name__)

# GitLab 上表示赞成/反对的 emoji 名
THUMBS_UP_NAMES = {"thumbsup", "+1"}
THUMBS_DOWN_NAMES = {"thumbsdown", "-1"}

# 到达终态的 MR 状态
SETTLEABLE_STATES = {"merged", "closed"}


def is_discussion_resolved(discussion: dict) -> bool:
    """
    discussion 是否已 resolve。

    GitLab 把 resolved 放在每条 note 上而非 discussion 上，只有全部可 resolve 的
    note 都被 resolve，这条 discussion 才算解决。
    """
    notes = discussion.get("notes")
    if not isinstance(notes, list) or not notes:
        return False
    resolvable = [n for n in notes if isinstance(n, dict) and n.get("resolvable")]
    if not resolvable:
        return False
    return all(bool(n.get("resolved")) for n in resolvable)


def has_human_reply(discussion: dict) -> bool:
    """
    是否有人回复过。

    首条 note 是 OpenCR 自己发的，因此只要 note 数大于 1 就说明有人接话了。
    """
    notes = discussion.get("notes")
    if not isinstance(notes, list):
        return False
    return len(notes) > 1


def classify_finding(
    discussion: Optional[dict],
    emoji_names: List[str],
    mr_state: str,
) -> Tuple[str, str]:
    """
    判定单条 Finding 的 Verdict，返回 (verdict, reason)。

    优先级：显式表态 > resolved > 人类回复 > MR 终态兜底。
    显式表态排在最前，是因为它是唯一语义无歧义的信号。

    注意：v1 没有代码验证，`accepted` 的口径是「有 👍，或 discussion 已 resolved」，
    因此 `dismissed`（resolved 但代码未改）在本版本中不会产生 —— 它是为
    引入代码验证后预留的状态，见 ADR-0001。
    """
    names = {str(n or "").strip().lower() for n in (emoji_names or [])}

    if names & THUMBS_DOWN_NAMES:
        return VERDICT_REJECTED, REASON_THUMBS_DOWN
    if names & THUMBS_UP_NAMES:
        return VERDICT_ACCEPTED, REASON_THUMBS_UP

    if discussion is None:
        # discussion 被删除：无从判定，按未处理计
        return VERDICT_IGNORED, REASON_DISCUSSION_MISSING

    if is_discussion_resolved(discussion):
        return VERDICT_ACCEPTED, REASON_RESOLVED

    if has_human_reply(discussion):
        return VERDICT_REJECTED, REASON_HUMAN_REPLIED

    if (mr_state or "").strip().lower() in SETTLEABLE_STATES:
        return VERDICT_IGNORED, REASON_MERGED_UNRESOLVED

    # MR 还开着，尚无结论
    return VERDICT_UNDECIDED, ""


def settle_mr(project_id: int, mr_iid: int, mr_state: str = "") -> dict:
    """
    结算一个 MR 下所有待判定的 Finding。

    只在 MR 合并/关闭时调用一次，不对进行中的 MR 轮询。
    """
    pending = repo.list_undecided_findings(project_id, mr_iid)
    if not pending:
        if mr_state in SETTLEABLE_STATES:
            repo.mark_mr_settled(project_id, mr_iid, mr_state)
        return {"settled": 0, "mr_state": mr_state, "reason": "no pending findings"}

    state = (mr_state or "").strip().lower()
    if state not in SETTLEABLE_STATES:
        state = get_mr_state(project_id, mr_iid).get("state", "")
    if state not in SETTLEABLE_STATES:
        return {"settled": 0, "mr_state": state, "reason": "MR not in a settleable state"}

    discussions = get_mr_discussions(project_id, mr_iid)
    by_id = {
        str(d.get("id") or ""): d
        for d in discussions
        if isinstance(d, dict) and d.get("id")
    }

    verdicts: Dict[int, tuple] = {}
    for item in pending:
        discussion = by_id.get(str(item["discussion_id"]))
        emoji: List[str] = []
        note_id = item.get("note_id")
        if note_id:
            emoji = get_note_award_emoji(project_id, mr_iid, int(note_id))

        verdict, reason = classify_finding(discussion, emoji, state)
        if verdict == VERDICT_UNDECIDED:
            continue
        verdicts[item["id"]] = (verdict, reason)

    updated = repo.apply_verdicts(verdicts)
    repo.mark_mr_settled(project_id, mr_iid, state)
    logger.info(
        "Settled MR !%s (project=%s, state=%s): pending=%s, updated=%s",
        mr_iid,
        project_id,
        state,
        len(pending),
        updated,
    )
    return {"settled": updated, "mr_state": state, "pending": len(pending)}
