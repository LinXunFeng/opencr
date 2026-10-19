"""拷贝巡检测试。

拷贝 = 前端用源巡检的配置预填新建表单，再加上服务端复制忽略清单。重点覆盖：
1. 忽略清单原样带过去，且在新巡检里照样生效；之后两边互不影响；
2. 运行记录、台账、修复记录不跟着走，新巡检从零开始；
3. 源巡检不存在时整个创建回滚，不留下一个没有忽略清单的副本。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.test_survey_destinations import _finding  # noqa: E402
from tests.test_survey_fix import FixRouteTestCase  # noqa: E402


class SurveyCopyTests(FixRouteTestCase):
    """POST /api/admin/surveys 带 copy_ignores_from。"""

    def _prepare_source(self):
        """源巡检：一轮运行、一条已忽略问题、一个已忽略仓库，外加一条旧版本迁移来的失效忽略。"""
        from backend.storage.db import session_scope
        from backend.storage.models import SurveyIgnore

        uid = self.survey["survey_uid"]
        run_uid = self._complete_run([_finding("lib/a.dart", title="空指针", body="可能为 None")])
        finding_id = self.repo.get_survey_run_detail(run_uid)["findings"][0]["id"]
        self.repo.add_survey_ignore(uid, finding_id, note="已知问题，下季度重构")
        self.repo.add_survey_ignored_repos(uid, ["https://g.com/a/legacy.git"], note="不再维护")
        with session_scope() as session:
            # 没有快照标题的旧条目不生效（见 repo._ignore_is_active）
            session.add(SurveyIgnore(survey_id=self._survey_id(), fingerprint="old", title=None))
        return uid

    def _copy(self, **extra):
        """以源巡检为底稿新建一个巡检，返回响应。"""
        return self.client.post("/api/admin/surveys", json={
            "name": "11 月巡检", "sources": [{"kind": "repo", "url": "https://g.com/a/app.git"}], **extra,
        })

    def test_copies_ignore_list_without_history(self):
        """只复制人的判断：理由与标记时间原样保留；命中记录、运行、台账都属于源巡检。"""
        self._login()
        uid = self._prepare_source()
        created = self._copy(copy_ignores_from=uid)
        self.assertEqual(created.status_code, 201, created.json)
        new_uid = created.json["survey_uid"]

        source_ignores = self.repo.list_survey_ignores(uid)
        copied = self.repo.list_survey_ignores(new_uid)
        self.assertEqual(len(source_ignores), 2)
        self.assertEqual(len(copied), 1, "失效的旧条目不该复制")
        active = next(item for item in source_ignores if item["active"])
        for key in ("fingerprint", "repo_slug", "file_path", "category", "title", "note", "created_at"):
            self.assertEqual(copied[0][key], active[key], key)
        self.assertTrue(copied[0]["active"])
        # 巡检链路取证时读的就是这一份：副本交给模型比对的快照与源巡检一致
        strip = lambda items: [{k: v for k, v in i.items() if k != "id"} for i in items]  # noqa: E731
        self.assertEqual(strip(self.repo.list_survey_active_ignores(new_uid)), strip(self.repo.list_survey_active_ignores(uid)))
        self.assertEqual((copied[0]["hidden_count"], copied[0]["last_match"]), (0, None))

        repos = self.repo.list_survey_ignored_repos(new_uid)
        self.assertEqual([(r["repo_slug"], r["note"]) for r in repos],
                         [(r["repo_slug"], r["note"]) for r in self.repo.list_survey_ignored_repos(uid)])
        self.assertEqual(self.repo.list_survey_ignored_repo_slugs(new_uid), {"a-legacy"})

        self.assertEqual(self.repo.list_survey_runs(new_uid), [])
        self.assertEqual(self.repo.list_survey_ledger_by_uid(new_uid), [])
        self.assertTrue(self.repo.list_survey_ledger_by_uid(uid))

    def test_lists_diverge_after_copy(self):
        """拷贝之后两边各管各的：在副本里取消忽略，源巡检的清单不变。"""
        self._login()
        uid = self._prepare_source()
        new_uid = self._copy(copy_ignores_from=uid).json["survey_uid"]
        self.assertTrue(self.repo.remove_survey_ignore(new_uid, self.repo.list_survey_ignores(new_uid)[0]["id"]))
        self.assertTrue(self.repo.remove_survey_ignored_repo(new_uid, self.repo.list_survey_ignored_repos(new_uid)[0]["id"]))
        self.assertEqual(len(self.repo.list_survey_ignores(uid)), 2)
        self.assertEqual(len(self.repo.list_survey_ignored_repos(uid)), 1)

    def test_without_flag_copies_nothing(self):
        """不勾选时与普通新建完全一样。"""
        self._login()
        self._prepare_source()
        new_uid = self._copy().json["survey_uid"]
        self.assertEqual(self.repo.list_survey_ignores(new_uid), [])
        self.assertEqual(self.repo.list_survey_ignored_repos(new_uid), [])

    def test_missing_source_rolls_back(self):
        """源巡检被删了：返回 400，且不留下一个没有忽略清单的副本。"""
        self._login()
        before = len(self.repo.list_surveys())
        response = self._copy(copy_ignores_from="not-a-survey")
        self.assertEqual(response.status_code, 400)
        self.assertIn("不存在", response.json["error"])
        self.assertEqual(len(self.repo.list_surveys()), before)

    def test_guest_cannot_copy(self):
        """新建（含拷贝）只对 Admin 开放。"""
        uid = self._prepare_source()
        self.assertEqual(self._copy(copy_ignores_from=uid).status_code, 401)


class FollowsSurveyNameFieldTests(unittest.TestCase):
    """前端拷贝时靠这个标记清空"留空即取巡检名称"的字段，标记丢了副本就会写进源巡检的工作表。"""

    def test_google_sheet_worksheet_follows_survey_name(self):
        """工作表留空取巡检名称，因此带标记；表格不带。"""
        from backend.survey.destinations import list_destination_types

        fields = {
            f["key"]: f["follows_survey_name"]
            for t in list_destination_types() if t["type"] == "google_sheet"
            for f in t["target_fields"]
        }
        self.assertEqual(fields, {"spreadsheet": False, "worksheet": True})


if __name__ == "__main__":
    unittest.main()
