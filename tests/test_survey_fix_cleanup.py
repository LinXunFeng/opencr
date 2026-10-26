"""清理修复合并请求，以及没提修复的台账行也有处理人。

重点覆盖几类"坏掉了也不会报错"的约束：
1. 清理不能删掉别人的工作 —— 分支上有不是 OpenCR 推的提交时整条拒绝，合并请求也不关；
2. 清理过的问题仍算修复过 —— 批量修复不能再给它发起，否则自动修复会把清理掉的合并请求原样提回来；
3. 已清理的合并请求不再出现在台账里；
4. 台账行的处理人与修复同一套规则，原始作者只追溯一次，处理人每轮重新确定，读不到平台时保留上一次的结果；
5. 自动清理默认关闭，只动结束已久的修复，合并请求还有人在动的不关，被拒绝的不能每轮都占着名额。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.test_survey_fix import PLATFORM, FixRouteTestCase, FixTestCase, _git  # noqa: E402

SHEET = {"destination": "sheet", "target": {"spreadsheet": "a" * 30, "worksheet": "W"}}
MEMBERS = [
    {"id": 1, "username": "alice", "name": "Alice", "state": "active", "access_level": 30},
    {"id": 2, "username": "bob", "name": "Bob", "state": "active", "access_level": 30},
    {"id": 3, "username": "lead", "name": "Lead", "state": "active", "access_level": 40},
]


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

class CleanupRuleTests(unittest.TestCase):
    """哪些修复能清理、分支是不是还归 OpenCR。"""

    def test_cleanup_rejection(self):
        """进行中的、什么都没推过的、分支名不是修复前缀的都不清理。"""
        from backend.survey.fix_cleanup import cleanup_rejection

        self.assertIn("不存在", cleanup_rejection(None))
        self.assertIn("还在进行", cleanup_rejection({"status": "running", "source_branch": "opencr/fix-1"}))
        self.assertIn("没有可清理", cleanup_rejection({"status": "failed", "source_branch": "", "mr_iid": None}))
        self.assertIn("不是 OpenCR", cleanup_rejection({"status": "succeeded", "source_branch": "main", "mr_iid": 3}))
        self.assertEqual(cleanup_rejection({"status": "succeeded", "source_branch": "opencr/fix-1", "mr_iid": 3}), "")
        # 合并请求没提成、只推了分支的失败记录同样要能清掉那条分支
        self.assertEqual(cleanup_rejection({"status": "failed", "source_branch": "opencr/fix-1", "mr_iid": None}), "")

    def test_branch_owned_by_fix(self):
        """有 commit_sha 时比分支头；旧记录退回看父提交是不是 base_sha；两样都没有时按不是处理。"""
        from backend.survey.fix_cleanup import branch_owned_by_fix

        branch = {"commit": {"id": "c1", "parent_ids": ["b0"]}}
        self.assertTrue(branch_owned_by_fix(branch, "c1", "b0"))
        self.assertFalse(branch_owned_by_fix(branch, "c2", "b0"))
        self.assertTrue(branch_owned_by_fix(branch, "", "b0"))
        self.assertFalse(branch_owned_by_fix({"commit": {"id": "c3", "parent_ids": ["c1"]}}, "", "b0"))
        self.assertFalse(branch_owned_by_fix(branch, "", ""))


class PlanLedgerTracesTests(unittest.TestCase):
    """哪些台账行要追溯原始作者。"""

    def test_plan(self):
        """只追溯存在、没追溯过、本轮重新出现、仓库这一轮拉到了的行；没有行号的不占名额，直接记为追溯过；超出上限的留给下一轮。"""
        from datetime import datetime

        from backend.survey.assignee import plan_ledger_traces

        def entry(fp, repo="app", state="present", lines=(1,), traced=None, run="r2"):
            return {"fingerprint": fp, "repo_slug": repo, "file_path": "a.py", "state": state,
                    "lines": list(lines), "author_traced_at": traced, "last_run_uid": run}

        entries = [
            entry("done", traced=datetime(2026, 1, 1)), entry("unseen", state="unseen"), entry("other", repo="web"),
            # 仍为存在、但本轮没重新出现：行号是旧一轮的，对不上本轮的提交
            entry("stale", run="r1"),
            entry("noline", lines=()), entry("a"), entry("b"), entry("c"),
        ]
        groups, no_lines = plan_ledger_traces(entries, "r2", {"app"}, 2)
        self.assertEqual([e["fingerprint"] for e in groups["app"]], ["a", "b"])
        self.assertEqual(no_lines, ["noline"])


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------

class CleanupTestCase(FixTestCase):
    """登记一条已提出合并请求的修复，平台接口用替身。"""

    def setUp(self):
        """清理模块读取的平台配置也指向 GitLab。"""
        super().setUp()
        patch = mock.patch("backend.survey.fix_cleanup.load_gitlab_config", return_value=PLATFORM)
        patch.start()
        self.addCleanup(patch.stop)

    def _opened_fix(self, finding_id=None, commit_sha="c1"):
        """一条成功提出合并请求 !7 的修复，返回 run_uid。"""
        if finding_id is None:
            _, finding_id = self._run_with_finding()
        _, uid = self.repo.start_survey_fix(finding_id)
        self.repo.update_survey_fix(
            uid, source_branch=f"opencr/fix-{uid[:8]}", base_sha="b0", commit_sha=commit_sha,
            mr_iid=7, mr_url="https://g.com/a/app/-/merge_requests/7", assignee_username="alice", assignee_name="Alice",
        )
        self.repo.finish_survey_fix(uid, "succeeded")
        return uid

    def _cleanup(self, uids, branch=None, mr_state="opened", close_error=None):
        """以给定的平台状态清理，返回 (结果, 平台替身)。branch 为 None 表示分支已不存在。"""
        from backend.review.common import ReviewError
        from backend.survey import fix_cleanup

        platform = mock.Mock()
        platform.get_branch.return_value = branch
        platform.get_merge_request.return_value = {"state": mr_state} if mr_state else None
        platform.close_merge_request.side_effect = ReviewError(close_error) if close_error else None
        with mock.patch.multiple(
            fix_cleanup, get_branch=platform.get_branch, get_merge_request=platform.get_merge_request,
            close_merge_request=platform.close_merge_request, delete_branch=platform.delete_branch,
        ), mock.patch.object(fix_cleanup, "republish_ledger") as republish, \
                mock.patch.object(fix_cleanup.threading, "Thread", side_effect=_run_inline):
            results = fix_cleanup.cleanup_fixes(uids)
        self.republish = republish
        return results, platform


def _run_inline(target, **_kwargs):
    """把后台线程换成当场执行，便于断言重推。"""
    return mock.Mock(start=target)


class CleanupTests(CleanupTestCase):
    def test_closes_mr_deletes_branch_and_drops_from_ledger(self):
        """打开的合并请求被关闭、分支被删除；记录保留并标为已清理，台账不再列出它，重推一次台账。"""
        from backend.survey.ledger import build_ledger_rows

        uid = self._opened_fix()
        self.assertEqual(len(self.repo.survey_fix_merge_requests(self._survey_id())), 1)
        [result], platform = self._cleanup([uid], branch={"commit": {"id": "c1", "parent_ids": ["b0"]}})
        self.assertEqual(result["outcome"], "cleaned", result["message"])
        platform.close_merge_request.assert_called_once_with("a/app", 7)
        platform.delete_branch.assert_called_once_with("a/app", f"opencr/fix-{uid[:8]}")
        self.assertTrue(self.repo.get_survey_fix(uid)["cleaned_at"])
        self.assertEqual(self.repo.survey_fix_merge_requests(self._survey_id()), {})
        row = build_ledger_rows(self.survey, self.repo.list_survey_ledger(self._survey_id()), "", False,
                                fix_merge_requests=self.repo.survey_fix_merge_requests(self._survey_id()))[0]
        self.assertEqual((row.values["fix_links"], row.values["fix_assignees"]), ("", ""))
        self.republish.assert_called_once_with(self._survey_id())

        # 再清理一次视为成功，不再碰平台，也不再重推
        [again], platform = self._cleanup([uid])
        self.assertTrue(again["already_cleaned"])
        platform.get_branch.assert_not_called()
        self.republish.assert_not_called()

    def test_taken_over_branch_is_left_alone(self):
        """分支头不是 OpenCR 推的那个提交：整条拒绝，合并请求不关、分支不删、记录不标。"""
        uid = self._opened_fix()
        [result], platform = self._cleanup([uid], branch={"commit": {"id": "someone-else", "parent_ids": ["c1"]}})
        self.assertEqual(result["outcome"], "rejected")
        self.assertIn("追加了提交", result["message"])
        platform.close_merge_request.assert_not_called()
        platform.delete_branch.assert_not_called()
        self.assertEqual(self.repo.get_survey_fix(uid)["cleaned_at"], "")
        self.republish.assert_not_called()

    def test_merged_mr_with_branch_gone(self):
        """已合并、分支已被平台删掉：什么都不用做，照样标为已清理；链接仍留在台账上，那是修复过并合入的证据。"""
        uid = self._opened_fix()
        [result], platform = self._cleanup([uid], branch=None, mr_state="merged")
        self.assertEqual(result["outcome"], "cleaned")
        self.assertIn("已合并", result["message"])
        platform.close_merge_request.assert_not_called()
        platform.delete_branch.assert_not_called()
        links = self.repo.survey_fix_merge_requests(self._survey_id())
        self.assertEqual([mr["url"] for mrs in links.values() for mr in mrs], ["https://g.com/a/app/-/merge_requests/7"])

    def test_orphan_branch_listed_and_cleaned(self):
        """推了分支、合并请求没提成的失败修复也出现在列表里，清理时只删分支。"""
        _, finding_id = self._run_with_finding()
        _, uid = self.repo.start_survey_fix(finding_id)
        self.repo.update_survey_fix(uid, source_branch=f"opencr/fix-{uid[:8]}", base_sha="b0", commit_sha="c1")
        self.repo.finish_survey_fix(uid, "failed", error_kind="platform_failed", error_message="GitLab 返回 409")
        # 进行中的、没推过分支的失败修复都不在列表里
        _, other = self._run_with_finding(title="另一个")
        self.repo.start_survey_fix(other)
        self.assertEqual([i["run_uid"] for i in self.repo.list_fix_merge_requests()["items"]], [uid])
        [result], platform = self._cleanup([uid], branch={"commit": {"id": "c1"}})
        self.assertEqual(result["outcome"], "cleaned", result["message"])
        platform.get_merge_request.assert_not_called()
        platform.delete_branch.assert_called_once()

    def test_platform_error_is_reported_and_not_marked(self):
        """关闭合并请求失败时记为失败、不标已清理，再清理一次会接着做完。"""
        uid = self._opened_fix()
        [result], _ = self._cleanup([uid], branch={"commit": {"id": "c1"}}, close_error="GitLab 返回 403")
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("403", result["message"])
        self.assertEqual(self.repo.get_survey_fix(uid)["cleaned_at"], "")

    def test_running_fix_rejected(self):
        """进行中的修复不清理：它可能正要推分支。"""
        _, finding_id = self._run_with_finding()
        _, uid = self.repo.start_survey_fix(finding_id)
        [result], platform = self._cleanup([uid])
        self.assertEqual(result["outcome"], "rejected")
        platform.get_branch.assert_not_called()

    def test_manual_rejection_recorded_without_touching_rotation(self):
        """手动清理没清掉的也记下原因，页面上不会挂着过时的旧原因；但不记看过的时刻，那只管自动清理的轮换。"""
        from backend.storage import db
        from backend.storage.models import SurveyFix

        uid = self._opened_fix()
        self.repo.record_survey_fix_cleanup_checks({uid: "上一次自动清理的旧原因"}, checked=True)
        self._cleanup([uid], branch={"commit": {"id": "someone-else"}})
        self.assertIn("追加了提交", self.repo.get_survey_fix(uid)["cleanup_note"])
        with db.session_scope() as session:
            checked = session.query(SurveyFix).filter_by(run_uid=uid).one().cleanup_checked_at
        self.repo.record_survey_fix_cleanup_checks({uid: "又一次"})
        with db.session_scope() as session:
            self.assertEqual(session.query(SurveyFix).filter_by(run_uid=uid).one().cleanup_checked_at, checked)

    def test_cleaned_issue_still_counts_as_fixed(self):
        """清理过的问题不会被批量修复再次发起，否则下一轮自动修复会把它原样提回来。"""
        run_uid, finding_id = self._run_with_finding()
        uid = self._opened_fix(finding_id)
        self._cleanup([uid], branch=None)
        self.assertEqual(self.repo.list_survey_fix_candidates(run_uid, "advice"), [])
        self.assertEqual(self.repo.start_survey_fix(finding_id, only_new=True)[0], self.repo.FIX_START_EXISTS)

    def test_list_filters_by_cleaned(self):
        """合并请求列表可以只看未清理或已清理的，处理人选项跟着同一口径。"""
        _, finding_id = self._run_with_finding()
        cleaned = self._opened_fix(finding_id)
        kept = self._opened_fix(self._run_with_finding(title="另一个问题")[1])
        self._cleanup([cleaned], branch=None)
        self.assertEqual([i["run_uid"] for i in self.repo.list_fix_merge_requests(cleaned="no")["items"]], [kept])
        self.assertEqual([i["run_uid"] for i in self.repo.list_fix_merge_requests(cleaned="yes")["items"]], [cleaned])
        self.assertEqual(self.repo.list_fix_merge_requests()["total"], 2)
        self.assertEqual(self.repo.list_fix_merge_requests(cleaned="yes")["assignees"][0]["count"], 1)

    def test_markdown_marks_cleaned(self):
        """报告是留档：已清理的链接照样列出，注明已清理。"""
        from backend.survey.fix import attach_fixes
        from backend.survey.report import render_run_markdown

        run_uid, finding_id = self._run_with_finding()
        uid = self._opened_fix(finding_id)
        self._cleanup([uid], branch=None)
        detail = self.repo.get_survey_run_detail(run_uid)
        detail["fixes"] = self.repo.list_survey_fixes_for_issues(self.survey["survey_uid"], detail["findings"])
        attach_fixes(detail["findings"], detail["fixes"])
        self.assertIn("merge_requests/7（已清理）", render_run_markdown(detail))


class CleanupRouteTests(FixRouteTestCase):
    """清理接口只对 Admin，校验入参并逐条返回结果。"""

    def test_admin_only_and_validates(self):
        """未登录 401；空数组与超出上限 400；登录后逐条返回结果。"""
        from backend.survey import fix_cleanup

        self.assertEqual(self.client.post("/api/admin/survey-fixes/cleanup", json={"run_uids": ["x"]}).status_code, 401)
        self._login()
        self.assertEqual(self.client.post("/api/admin/survey-fixes/cleanup", json={"run_uids": []}).status_code, 400)
        too_many = ["x"] * (fix_cleanup.MAX_CLEANUP_PER_REQUEST + 1)
        self.assertEqual(self.client.post("/api/admin/survey-fixes/cleanup", json={"run_uids": too_many}).status_code, 400)
        response = self.client.post("/api/admin/survey-fixes/cleanup", json={"run_uids": ["missing"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["results"][0]["outcome"], "rejected")


# ---------------------------------------------------------------------------
# 自动清理
# ---------------------------------------------------------------------------

class MrActiveSinceTests(unittest.TestCase):
    """合并请求在期限之后有没有动静；读不出时间时按有动静处理。"""

    def test_mr_active_since(self):
        """看最新一条评论的时刻，没有评论看提出时刻；带时区的时间与 naive UTC 的期限比较；缺失或格式不对时按有动静处理。"""
        from datetime import datetime

        from backend.survey.fix_cleanup import mr_active_since

        cutoff = datetime(2026, 10, 1, 12, 0)
        # updated_at 再新也不算：目标分支一有提交平台就会刷新它
        mr = {"created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-10-20T00:00:00Z"}
        self.assertFalse(mr_active_since(mr, None, cutoff))
        self.assertFalse(mr_active_since(mr, {"created_at": "2026-10-01T11:59:00.000Z"}, cutoff))
        self.assertTrue(mr_active_since(mr, {"created_at": "2026-10-01T12:01:00.000Z"}, cutoff))
        # +08:00 的 19:30 是 UTC 11:30，在期限之前
        self.assertFalse(mr_active_since(mr, {"created_at": "2026-10-01T19:30:00+08:00"}, cutoff))
        self.assertTrue(mr_active_since(mr, {"created_at": "2026-10-01T20:30:00+08:00"}, cutoff))
        self.assertTrue(mr_active_since({"created_at": "2026-10-02T00:00:00Z"}, None, cutoff))
        for note in ({}, {"created_at": ""}, {"created_at": "昨天"}):
            self.assertTrue(mr_active_since(mr, note or {"created_at": None}, cutoff), note)
        self.assertTrue(mr_active_since({}, None, cutoff))


class LatestNoteApiTests(unittest.TestCase):
    """读取合并请求最新一条评论的接口封装。"""

    def test_reads_newest_note_or_none(self):
        """按创建时间倒序只取一条；没有评论时返回 None。"""
        from backend.review import gitlab

        response = mock.Mock()
        response.json.return_value = [{"id": 9, "created_at": "2026-10-01T00:00:00Z"}]
        with mock.patch.object(gitlab, "_gitlab_send", return_value=response) as send:
            self.assertEqual(gitlab.get_latest_merge_request_note("a/app", 7)["id"], 9)
        method, path = send.call_args[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/projects/a%2Fapp/merge_requests/7/notes?sort=desc&order_by=created_at&per_page=1")
        response.json.return_value = []
        with mock.patch.object(gitlab, "_gitlab_send", return_value=response):
            self.assertIsNone(gitlab.get_latest_merge_request_note("a/app", 7))


class AutoCleanupTests(CleanupTestCase):
    """巡检结束后按自动清理期限清理结束已久的修复。"""

    DAYS = 14

    def _age(self, uid, days):
        """把修复的结束时刻拨回 days 天前。"""
        from datetime import timedelta

        from backend.storage import db
        from backend.storage.models import SurveyFix, utcnow

        with db.session_scope() as session:
            session.query(SurveyFix).filter_by(run_uid=uid).one().finished_at = utcnow() - timedelta(days=days)

    def _enable(self, days=DAYS):
        """打开这个巡检的自动清理。"""
        self.repo.update_survey(self.survey["survey_uid"], {"fix_cleanup_after_days": days})

    def _auto(self, note_days_ago=30, mr_state="opened", branch="owned"):
        """以给定的平台状态跑一次自动清理，返回 (结果, 平台替身)。最新一条评论以几天前计，None 表示没有评论。"""
        from datetime import timedelta

        from backend.storage.models import utcnow
        from backend.survey import fix_cleanup

        def ago(days):
            """几天前的平台时间。"""
            return (utcnow() - timedelta(days=days)).isoformat() + "Z"

        platform = mock.Mock()
        platform.get_branch.side_effect = lambda _p, name: (
            {"commit": {"id": "c1", "parent_ids": ["b0"]}} if branch == "owned" else None
        )
        # updated_at 永远是刚刚：活跃仓库里目标分支一有提交它就会刷新，不能拿它当动静
        platform.get_merge_request.return_value = {"state": mr_state, "created_at": ago(60), "updated_at": ago(0)}
        platform.get_latest_merge_request_note.return_value = (
            None if note_days_ago is None else {"created_at": ago(note_days_ago)}
        )
        with mock.patch.multiple(
            fix_cleanup, get_branch=platform.get_branch, get_merge_request=platform.get_merge_request,
            close_merge_request=platform.close_merge_request, delete_branch=platform.delete_branch,
            get_latest_merge_request_note=platform.get_latest_merge_request_note,
        ), mock.patch.object(fix_cleanup, "republish_ledger") as republish, \
                mock.patch.object(fix_cleanup.threading, "Thread", side_effect=_run_inline):
            results = fix_cleanup.auto_cleanup_fixes(self.repo.get_survey(self.survey["survey_uid"]))
        self.republish = republish
        return results, platform

    def test_off_by_default(self):
        """没配置期限时什么都不做，连候选都不查：自动清理会关闭别人名下的合并请求，必须由人明确打开。"""
        uid = self._opened_fix()
        self._age(uid, 365)
        self.assertIsNone(self.repo.get_survey(self.survey["survey_uid"])["fix_cleanup_after_days"])
        results, platform = self._auto()
        self.assertEqual(results, [])
        platform.get_branch.assert_not_called()
        self.assertEqual(self.repo.get_survey_fix(uid)["cleaned_at"], "")

    def test_cleans_old_idle_fix_and_leaves_young_one(self):
        """结束超过期限、合并请求也久无动静的被清理；还没到期限的不碰。"""
        old = self._opened_fix()
        young = self._opened_fix(self._run_with_finding(title="另一个问题")[1])
        self._age(old, self.DAYS + 1)
        self._age(young, self.DAYS - 1)
        self._enable()
        results, platform = self._auto(note_days_ago=None)
        self.assertEqual([(r["run_uid"], r["outcome"]) for r in results], [(old, "cleaned")])
        platform.close_merge_request.assert_called_once_with("a/app", 7)
        self.assertTrue(self.repo.get_survey_fix(old)["cleaned_at"])
        self.assertEqual(self.repo.get_survey_fix(young)["cleaned_at"], "")
        self.republish.assert_called_once_with(self._survey_id())

    def test_recently_active_mr_is_left_open(self):
        """合并请求还开着、期限内有过评论：不关、不删分支、不标已清理，原因记在修复记录上；之后清理掉了原因随之清空。"""
        uid = self._opened_fix()
        self._age(uid, self.DAYS + 30)
        self._enable()
        [result], platform = self._auto(note_days_ago=1)
        self.assertEqual(result["outcome"], "rejected")
        self.assertIn("动静", result["message"])
        platform.close_merge_request.assert_not_called()
        platform.delete_branch.assert_not_called()
        fix = self.repo.get_survey_fix(uid)
        self.assertEqual(fix["cleaned_at"], "")
        self.assertIn("动静", fix["cleanup_note"])
        self.assertIn("动静", self.repo.list_fix_merge_requests()["items"][0]["cleanup_note"])
        [result], _ = self._auto(note_days_ago=self.DAYS + 1)
        self.assertEqual(result["outcome"], "cleaned", result["message"])
        self.assertEqual(self.repo.get_survey_fix(uid)["cleanup_note"], "")

    def test_merged_mr_wrapped_up_regardless_of_activity(self):
        """已合并的不看动静：合并本身就会刷新 updated_at，看动静的话它永远收不了尾。"""
        uid = self._opened_fix()
        self._age(uid, self.DAYS + 1)
        self._enable()
        [result], platform = self._auto(note_days_ago=0, mr_state="merged")
        self.assertEqual(result["outcome"], "cleaned", result["message"])
        platform.close_merge_request.assert_not_called()
        platform.delete_branch.assert_called_once()

    def test_candidates_exclude_running_cleaned_and_empty(self):
        """进行中的、已清理的、什么都没推过的都不进候选，免得白占名额。"""
        from datetime import timedelta

        from backend.storage.models import utcnow

        cleaned = self._opened_fix()
        self.repo.mark_survey_fix_cleaned(cleaned, "closed")
        _, running = self.repo.start_survey_fix(self._run_with_finding(title="进行中")[1])
        _, empty = self.repo.start_survey_fix(self._run_with_finding(title="没推过")[1])
        self.repo.finish_survey_fix(empty, "skipped")
        wanted = self._opened_fix(self._run_with_finding(title="该清理")[1])
        for uid in (cleaned, running, empty, wanted):
            self._age(uid, 100)
        cutoff = utcnow() - timedelta(days=self.DAYS)
        self.assertEqual(self.repo.list_survey_fix_cleanup_candidates(self.survey["survey_uid"], cutoff, 10), [wanted])
        # 别的巡检的修复不会混进来
        self.assertEqual(self.repo.list_survey_fix_cleanup_candidates("no-such-survey", cutoff, 10), [])

    def test_rejected_ones_rotate_to_the_back(self):
        """被拒绝的记下看过的时刻、排到后面：否则每轮名额都被同几条吃光，其余的永远轮不到。"""
        from datetime import timedelta

        from backend.storage.models import utcnow
        from backend.survey import fix_cleanup

        first = self._opened_fix()
        second = self._opened_fix(self._run_with_finding(title="另一个问题")[1])
        self._age(first, self.DAYS + 10)
        self._age(second, self.DAYS + 5)
        self._enable()
        cutoff = utcnow() - timedelta(days=self.DAYS)
        with mock.patch.object(fix_cleanup, "MAX_AUTO_CLEANUP_PER_RUN", 1):
            [result], _ = self._auto(note_days_ago=1)
            self.assertEqual((result["run_uid"], result["outcome"]), (first, "rejected"))
            self.assertEqual(self.repo.list_survey_fix_cleanup_candidates(self.survey["survey_uid"], cutoff, 2), [second, first])
            [result], _ = self._auto()
        self.assertEqual((result["run_uid"], result["outcome"]), (second, "cleaned"))

    def test_runner_cleans_even_when_run_fails(self):
        """巡检失败的一轮也清理：清理不依赖这一轮的产出；读的是最新的巡检配置。"""
        from backend.survey import runner

        uid = self.survey["survey_uid"]
        self.repo.add_survey_ignored_repos(uid, ["https://g.com/a/app.git"], "")
        self._enable()
        with mock.patch.object(runner, "_cleanup"), mock.patch.object(runner, "auto_cleanup_fixes") as auto:
            run_uid = runner.execute_survey_run(uid, "manual")
        self.assertEqual(self.repo.get_survey_run_detail(run_uid)["status"], "failed")
        auto.assert_called_once()
        self.assertEqual(auto.call_args[0][0]["fix_cleanup_after_days"], self.DAYS)

    def test_runner_survives_cleanup_crash(self):
        """自动清理出错只记日志，不影响这一轮的结果。"""
        from backend.survey import runner

        uid = self.survey["survey_uid"]
        self.repo.add_survey_ignored_repos(uid, ["https://g.com/a/app.git"], "")
        with mock.patch.object(runner, "_cleanup"), \
                mock.patch.object(runner, "auto_cleanup_fixes", side_effect=RuntimeError("boom")):
            run_uid = runner.execute_survey_run(uid, "manual")
        self.assertEqual(run_uid, self.repo.list_survey_runs(uid)[0]["run_uid"])


class AutoCleanupRouteTests(FixRouteTestCase):
    """巡检配置里的自动清理期限。"""

    def test_saved_and_validated(self):
        """正整数照存；空与 0 都表示不自动清理；非整数、负数与超出上限的返回 400 且不改动。"""
        self._login()
        uid = self.survey["survey_uid"]
        url = f"/api/admin/surveys/{uid}"
        self.assertIsNone(self.client.get(url).json["fix_cleanup_after_days"])
        self.assertEqual(self.client.patch(url, json={"fix_cleanup_after_days": 30}).json["fix_cleanup_after_days"], 30)
        for bad in ("abc", -1, 3651, True, False, 1.5, 0.0):
            self.assertEqual(self.client.patch(url, json={"fix_cleanup_after_days": bad}).status_code, 400, bad)
        self.assertEqual(self.repo.get_survey(uid)["fix_cleanup_after_days"], 30)
        for off in (0, None, ""):
            self.client.patch(url, json={"fix_cleanup_after_days": 30})
            self.assertIsNone(self.client.patch(url, json={"fix_cleanup_after_days": off}).json["fix_cleanup_after_days"], off)

    def test_saved_on_create(self):
        """新建时同样能带上（拷贝巡检走的就是新建）。"""
        self._login()
        created = self.client.post("/api/admin/surveys", json={
            "name": "带自动清理", "sources": [{"kind": "repo", "url": "https://g.com/a/b.git"}],
            "fix_cleanup_after_days": 7,
        })
        self.assertEqual(created.status_code, 201, created.json)
        self.assertEqual(self.repo.get_survey(created.json["survey_uid"])["fix_cleanup_after_days"], 7)


# ---------------------------------------------------------------------------
# 台账行的处理人
# ---------------------------------------------------------------------------

class LedgerAssigneeTests(FixTestCase):
    """没提修复的台账行，按处理人规则定下处理人，写进「修复处理人」一格。"""

    def setUp(self):
        """处理人模块读取的平台配置也指向 GitLab；巡检绑定一个输出目标。"""
        super().setUp()
        patch = mock.patch("backend.survey.assignee.load_gitlab_config", return_value=PLATFORM)
        patch.start()
        self.addCleanup(patch.stop)
        self.survey = self.repo.update_survey(self.survey["survey_uid"], {}, bindings=[SHEET])

    def _resolve(self, run_uid, members=MEMBERS, members_error=None):
        """以给定的项目成员执行一次处理人判定，返回成员接口的替身。"""
        from backend.survey import assignee

        list_members = mock.Mock(return_value=members, side_effect=members_error)
        with mock.patch.object(assignee, "list_project_members", list_members), \
                mock.patch.object(assignee, "get_current_user", return_value={"id": 100}), \
                mock.patch.object(assignee, "search_users", return_value=[]):
            assignee.resolve_ledger_assignees(run_uid)
        return list_members

    def _row(self):
        """台账唯一一行的镜像值。"""
        from backend.survey.ledger import build_ledger_rows

        survey_id = self._survey_id()
        return build_ledger_rows(self.survey, self.repo.list_survey_ledger(survey_id), "", False,
                                 fix_merge_requests=self.repo.survey_fix_merge_requests(survey_id))[0].values

    def test_traces_original_author_once_and_reresolves_each_run(self):
        """没提修复也追溯到原始作者 Alice（跳过只做格式化的 Bob）；之后不再克隆，但每轮按最新的成员重新确定处理人。"""
        from backend.survey import assignee

        run_uid, _ = self._run_with_finding()
        self._point_repo_to(self._remote())
        self._resolve(run_uid)
        entry = self.repo.list_survey_ledger(self._survey_id())[0]
        self.assertEqual((entry["author_name"], entry["assignee_username"], entry["assignee_source"]),
                         ("Alice", "alice", "author"))
        self.assertEqual(self._row()["fix_assignees"], "Alice")
        # 追溯用的临时克隆用完即删
        self.assertEqual(list((self.root / "ws" / ".fixes").iterdir()), [])

        # 本地远端的项目路径就是它在磁盘上的路径
        self.repo.save_repo_owner(str(self.root / "remote").lstrip("/"), ["lead"], "")
        blocked = [dict(MEMBERS[0], state="blocked")] + MEMBERS[1:]
        with mock.patch.object(assignee, "prepare_history_repo") as prepare:
            self._resolve(run_uid, members=blocked)
        prepare.assert_not_called()
        entry = self.repo.list_survey_ledger(self._survey_id())[0]
        self.assertEqual((entry["assignee_username"], entry["assignee_source"]), ("lead", "configured"))
        self.assertIn("Alice", entry["assignee_note"])

    def test_blame_failure_marks_row_traced(self):
        """行号超出文件长度、blame 必然失败的行记为追溯过、没有原始作者，不再每轮重试；处理人退回仓库负责人。"""
        from tests.test_survey_destinations import _finding

        run_uid = self._complete_run([_finding("calc.py", category="correctness", line=999)])
        self._point_repo_to(self._remote())
        self.repo.save_repo_owner(str(self.root / "remote").lstrip("/"), ["lead"], "")
        self._resolve(run_uid)
        entry = self.repo.list_survey_ledger(self._survey_id())[0]
        self.assertIsNotNone(entry["author_traced_at"])
        self.assertEqual((entry["author_name"], entry["assignee_username"]), ("", "lead"))

    def test_lookup_failure_keeps_previous_assignee(self):
        """读不到项目成员时这一轮没能判定，保留上一次的处理人，而不是把人清空。"""
        run_uid, _ = self._run_with_finding()
        self._point_repo_to(self._remote())
        self._resolve(run_uid)
        self._resolve(run_uid, members_error=RuntimeError("502"))
        self.assertEqual(self.repo.list_survey_ledger(self._survey_id())[0]["assignee_username"], "alice")

    def test_members_read_once_per_project(self):
        """同一个项目的多行共用一次成员查询。"""
        from tests.test_survey_destinations import _finding

        run_uid = self._complete_run([
            _finding("calc.py", category="correctness", line=2), _finding("calc.py", category="security", line=3),
        ])
        self._point_repo_to(self._remote())
        list_members = self._resolve(run_uid)
        self.assertEqual(list_members.call_count, 1)
        self.assertEqual({e["assignee_username"] for e in self.repo.list_survey_ledger(self._survey_id())}, {"alice"})

    def test_skipped_without_bindings(self):
        """没有绑定输出目标时不追溯：处理人只出现在台账镜像里。"""
        from backend.survey import assignee

        self.survey = self.repo.update_survey(self.survey["survey_uid"], {}, bindings=[])
        run_uid, _ = self._run_with_finding()
        with mock.patch.object(assignee, "prepare_history_repo") as prepare:
            list_members = self._resolve(run_uid)
        prepare.assert_not_called()
        list_members.assert_not_called()

    def test_clone_failure_leaves_row_untraced(self):
        """克隆失败时这一行保持未追溯，下一轮再试；处理人暂时按没有原始作者确定，退回仓库负责人。"""
        run_uid, _ = self._run_with_finding()
        self._point_repo_to(f"file://{self.root / 'missing.git'}")
        self.repo.save_repo_owner(str(self.root / "missing").lstrip("/"), ["lead"], "")
        self._resolve(run_uid)
        entry = self.repo.list_survey_ledger(self._survey_id())[0]
        self.assertIsNone(entry["author_traced_at"])
        self.assertEqual((entry["assignee_username"], entry["assignee_source"]), ("lead", "configured"))
        self.assertIn("没有追溯到原始作者", entry["assignee_note"])

    def test_merge_request_assignee_wins(self):
        """提过、且没清理的修复合并请求有处理人时，写那边实际指派的人；没有时写这一行定下的人。"""
        run_uid, finding_id = self._run_with_finding()
        self._point_repo_to(self._remote())
        self._resolve(run_uid)
        _, uid = self.repo.start_survey_fix(finding_id)
        self.repo.update_survey_fix(uid, mr_url="https://g.com/a/app/-/merge_requests/7", mr_iid=7,
                                    assignee_username="bob", assignee_name="Bob")
        self.repo.finish_survey_fix(uid, "succeeded")
        self.assertEqual(self._row()["fix_assignees"], "Bob")
        self.repo.mark_survey_fix_cleaned(uid, "closed")
        self.assertEqual(self._row()["fix_assignees"], "Alice")

    def test_unassigned_merge_request_is_not_backfilled(self):
        """合并请求没指派时这一格留空：写上这一行定下的人，读表的人会以为合并请求已经指给了他。"""
        run_uid, finding_id = self._run_with_finding()
        self._point_repo_to(self._remote())
        self._resolve(run_uid)
        _, uid = self.repo.start_survey_fix(finding_id)
        self.repo.update_survey_fix(uid, mr_url="https://g.com/a/app/-/merge_requests/7", mr_iid=7)
        self.repo.finish_survey_fix(uid, "succeeded")
        self.assertEqual(self._row()["fix_assignees"], "")


class LedgerAssigneeHistoryTests(FixTestCase):
    """追溯用的克隆检出到那一轮分析的提交，台账里的行号才对得上。"""

    def test_checks_out_analyzed_commit(self):
        """分支在分析之后又有新提交时，blame 的仍是分析时的那一版。"""
        from backend.survey.workspace import delete_fix_workspace, prepare_history_repo

        url = self._remote()
        analyzed = _git(self.root / "remote.git", "rev-parse", "main").strip()
        work = self.root / "more"
        _git(self.root, "clone", "-q", url, str(work))
        (work / "calc.py").write_text("# moved\n" + (work / "calc.py").read_text())
        _git(work, "commit", "-q", "-am", "shift lines")
        _git(work, "push", "-q", "origin", "main")
        repo_dir = prepare_history_repo("ledger-test", url, "main", analyzed)
        try:
            self.assertEqual(_git(repo_dir, "rev-parse", "HEAD").strip(), analyzed)
        finally:
            delete_fix_workspace("ledger-test")

    def test_missing_analyzed_commit_raises(self):
        """检出不了分析时的提交就报错，不退回分支最新的提交：行号错位时 blame 会安静地归给别人。"""
        from backend.survey.common import SurveyError
        from backend.survey.workspace import delete_fix_workspace, prepare_history_repo

        url = self._remote()
        try:
            with self.assertRaises(SurveyError):
                prepare_history_repo("ledger-test", url, "main", "0" * 40)
        finally:
            delete_fix_workspace("ledger-test")


if __name__ == "__main__":
    unittest.main()
