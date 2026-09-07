"""结算状态机测试：逐条覆盖 classify_finding 的每个分支。

判定口径见 docs/adr/0001-suggestion-acceptance-via-discussion-state.md。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.review.settlement import (  # noqa: E402
    classify_finding,
    has_human_reply,
    is_discussion_resolved,
)
from backend.storage.models import (  # noqa: E402
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


def discussion(*, resolved=False, resolvable=True, replies=0):
    notes = [{"id": 1, "resolvable": resolvable, "resolved": resolved}]
    for i in range(replies):
        notes.append({"id": 100 + i, "resolvable": resolvable, "resolved": resolved})
    return {"id": "abc", "notes": notes}


class ResolvedDetectionTests(unittest.TestCase):
    def test_all_resolvable_notes_must_be_resolved(self):
        self.assertTrue(is_discussion_resolved(discussion(resolved=True)))
        self.assertFalse(is_discussion_resolved(discussion(resolved=False)))

    def test_partially_resolved_thread_is_not_resolved(self):
        d = {"id": "x", "notes": [
            {"resolvable": True, "resolved": True},
            {"resolvable": True, "resolved": False},
        ]}
        self.assertFalse(is_discussion_resolved(d))

    def test_non_resolvable_discussion_is_never_resolved(self):
        """普通 note 不可 resolve —— 这正是整体评论无法结算的原因。"""
        self.assertFalse(is_discussion_resolved(discussion(resolvable=False, resolved=True)))

    def test_empty_notes(self):
        self.assertFalse(is_discussion_resolved({"id": "x", "notes": []}))
        self.assertFalse(is_discussion_resolved({"id": "x"}))


class HumanReplyDetectionTests(unittest.TestCase):
    def test_first_note_is_ours_so_a_single_note_is_no_reply(self):
        self.assertFalse(has_human_reply(discussion()))

    def test_second_note_means_someone_replied(self):
        self.assertTrue(has_human_reply(discussion(replies=1)))


class ClassifyFindingTests(unittest.TestCase):
    def test_thumbs_down_wins_over_everything(self):
        """显式表态是唯一语义无歧义的信号，压过 resolved。"""
        verdict, reason = classify_finding(discussion(resolved=True), ["thumbsdown"], "merged")
        self.assertEqual((verdict, reason), (VERDICT_REJECTED, REASON_THUMBS_DOWN))

    def test_thumbs_up_wins_over_unresolved(self):
        verdict, reason = classify_finding(discussion(resolved=False), ["thumbsup"], "merged")
        self.assertEqual((verdict, reason), (VERDICT_ACCEPTED, REASON_THUMBS_UP))

    def test_thumbs_down_beats_thumbs_up(self):
        """同时有赞成和反对时按反对计：宁可低估采纳率，也不要高估。"""
        verdict, _ = classify_finding(discussion(), ["thumbsup", "thumbsdown"], "merged")
        self.assertEqual(verdict, VERDICT_REJECTED)

    def test_plus_one_alias_is_recognized(self):
        verdict, _ = classify_finding(discussion(), ["+1"], "merged")
        self.assertEqual(verdict, VERDICT_ACCEPTED)
        verdict, _ = classify_finding(discussion(), ["-1"], "merged")
        self.assertEqual(verdict, VERDICT_REJECTED)

    def test_unrelated_emoji_is_ignored(self):
        verdict, reason = classify_finding(discussion(resolved=True), ["rocket", "eyes"], "merged")
        self.assertEqual((verdict, reason), (VERDICT_ACCEPTED, REASON_RESOLVED))

    def test_resolved_counts_as_accepted_in_this_version(self):
        """
        v1 没有代码改动验证，accepted 的口径退化为「有 👍 或已 resolved」。
        dismissed 是为引入代码验证后预留的状态，本版本不会产生。
        """
        verdict, reason = classify_finding(discussion(resolved=True), [], "merged")
        self.assertEqual((verdict, reason), (VERDICT_ACCEPTED, REASON_RESOLVED))

    def test_human_replied_and_left_unresolved_is_rejected(self):
        verdict, reason = classify_finding(discussion(resolved=False, replies=1), [], "merged")
        self.assertEqual((verdict, reason), (VERDICT_REJECTED, REASON_HUMAN_REPLIED))

    def test_replied_then_resolved_is_accepted(self):
        """讨论过再 resolve，算采纳 —— resolved 的判定排在人类回复之前。"""
        verdict, reason = classify_finding(discussion(resolved=True, replies=2), [], "merged")
        self.assertEqual((verdict, reason), (VERDICT_ACCEPTED, REASON_RESOLVED))

    def test_untouched_on_merged_mr_is_ignored(self):
        verdict, reason = classify_finding(discussion(), [], "merged")
        self.assertEqual((verdict, reason), (VERDICT_IGNORED, REASON_MERGED_UNRESOLVED))

    def test_untouched_on_closed_mr_is_ignored(self):
        verdict, _ = classify_finding(discussion(), [], "closed")
        self.assertEqual(verdict, VERDICT_IGNORED)

    def test_open_mr_stays_undecided(self):
        """MR 还开着就没有结论，绝不提前下判断。"""
        verdict, reason = classify_finding(discussion(), [], "opened")
        self.assertEqual((verdict, reason), (VERDICT_UNDECIDED, ""))

    def test_deleted_discussion_is_ignored(self):
        verdict, reason = classify_finding(None, [], "merged")
        self.assertEqual((verdict, reason), (VERDICT_IGNORED, REASON_DISCUSSION_MISSING))

    def test_deleted_discussion_still_respects_emoji(self):
        verdict, _ = classify_finding(None, ["thumbsup"], "merged")
        self.assertEqual(verdict, VERDICT_ACCEPTED)


class SettleMrTests(unittest.TestCase):
    def test_open_mr_is_not_settled(self):
        from backend.review import settlement

        with mock.patch.object(settlement.repo, "list_undecided_findings",
                               return_value=[{"id": 1, "discussion_id": "d", "note_id": 2,
                                              "file_path": "a.py", "line": 1}]):
            with mock.patch.object(settlement, "get_mr_state", return_value={"state": "opened"}):
                with mock.patch.object(settlement, "get_mr_discussions") as fetch:
                    result = settlement.settle_mr(1, 1)

        self.assertEqual(result["settled"], 0)
        # 进行中的 MR 连 discussion 都不该去拉
        fetch.assert_not_called()

    def test_emoji_failure_degrades_instead_of_aborting_settlement(self):
        """
        逐条查 emoji 的调用会失败（网络/权限）。单条失败按"无表态"降级，
        不能让整个 MR 的结算跟着挂掉。
        """
        from backend.review import settlement

        pending = [{"id": 7, "discussion_id": "abc", "note_id": 5, "file_path": "a.py", "line": 1}]
        applied = {}

        with mock.patch.object(settlement.repo, "list_undecided_findings", return_value=pending), \
             mock.patch.object(settlement.repo, "apply_verdicts",
                               side_effect=lambda v: applied.update(v) or len(v)), \
             mock.patch.object(settlement.repo, "mark_mr_settled"), \
             mock.patch.object(settlement, "get_mr_discussions",
                               return_value=[discussion(resolved=True)]), \
             mock.patch.object(settlement, "get_note_award_emoji", return_value=[]):
            result = settlement.settle_mr(1, 1, "merged")

        self.assertEqual(result["settled"], 1)
        self.assertEqual(applied[7], (VERDICT_ACCEPTED, REASON_RESOLVED))


if __name__ == "__main__":
    unittest.main()
