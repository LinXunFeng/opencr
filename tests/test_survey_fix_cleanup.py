"""清理修复合并请求，以及没提修复的台账行也有处理人。

重点覆盖几类"坏掉了也不会报错"的约束：
1. 清理不能删掉别人的工作 —— 分支上有不是 OpenCR 推的提交时整条拒绝，合并请求也不关；
2. 清理过的问题仍算修复过 —— 批量修复不能再给它发起，否则自动修复会把清理掉的合并请求原样提回来；
3. 已清理的合并请求不再出现在台账里；
4. 台账行的处理人与修复同一套规则，原始作者只追溯一次，处理人每轮重新确定，读不到平台时保留上一次的结果。
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
