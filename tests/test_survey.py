"""定期巡检测试。

重点覆盖三类容易在半年后被"顺手优化"掉的约束：
1. 调度语义 —— 时区、错过窗口、非法表达式必须在保存时就被挡住；
2. 跨轮次比对 —— 指纹不含正文、清理永不删最近一次，两者任一失效都会让报告静默失真；
3. 跨仓库连接 —— 只连路由字面量，绝不使用 codegraph 的跨仓库边（见 ADR-0003）。
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

class SlugAndFingerprintTests(unittest.TestCase):
    def test_slugify_strips_path_traversal_and_spaces(self):
        """slug 会成为文件系统路径的一部分，必须挡住穿越与空格。"""
        from backend.survey.common import slugify

        self.assertEqual(slugify("../../etc/passwd"), "etc-passwd")
        self.assertEqual(slugify("My Survey #1"), "my-survey-1")
        self.assertNotIn("/", slugify("a/b/c"))
        self.assertNotIn("..", slugify("...."))

    def test_slugify_falls_back_to_hash_for_non_ascii(self):
        """纯中文名去掉后会是空串，必须退回哈希而不是报错——用中文命名很正常。"""
        from backend.survey.common import slugify

        slug = slugify("巡检")
        self.assertTrue(slug.startswith("repo-"))
        self.assertEqual(slug, slugify("巡检"))
        self.assertNotEqual(slug, slugify("另一个"))

    def test_repo_slug_is_stable_across_url_forms(self):
        """同一个仓库的 https / ssh / 带 .git 写法必须落到同一个目录。"""
        from backend.survey.common import repo_slug_from_url

        expected = "mobile-flutter-app"
        for url in (
            "https://gitlab.company.com/mobile/flutter-app.git",
            "https://gitlab.company.com/mobile/flutter-app",
            "git@gitlab.company.com:mobile/flutter-app.git",
        ):
            self.assertEqual(repo_slug_from_url(url), expected)

    def test_fingerprint_ignores_body_wording(self):
        """指纹不含正文：模型两次措辞不同，含正文会让每轮产出全是"新增"。"""
        from backend.survey.common import finding_fingerprint

        a = finding_fingerprint("app", "lib/a.dart", "security")
        b = finding_fingerprint("app", "lib/a.dart", "security")
        self.assertEqual(a, b)
        self.assertNotEqual(a, finding_fingerprint("app", "lib/a.dart", "performance"))
        self.assertNotEqual(a, finding_fingerprint("web", "lib/a.dart", "security"))

    def test_unknown_category_falls_back_without_guessing(self):
        """类别是闭集。模型给出集外值时落到默认档，不做模糊匹配。"""
        from backend.survey.common import normalize_category

        self.assertEqual(normalize_category("security"), "security")
        self.assertEqual(normalize_category("cross-repo"), "cross_repo")
        self.assertEqual(normalize_category("我编的类别"), "correctness")

    def test_exclude_pattern_falls_back_to_substring(self):
        """用户填的多半是朴素关键字而不是正则，非法正则不该让整条排除失效。"""
        from backend.survey.common import matches_any_pattern

        self.assertTrue(matches_any_pattern("group/legacy-tools", ["legacy"]))
        self.assertFalse(matches_any_pattern("group/app", ["legacy"]))
        self.assertTrue(matches_any_pattern("group/a[b", ["a[b"]))


class ScheduleTests(unittest.TestCase):
    BASE = datetime(2026, 9, 9, 1, 0)  # naive UTC，周三

    def test_presets_convert_to_cron(self):
        from backend.survey.schedule import to_cron

        self.assertEqual(to_cron("daily", "09:00"), "0 9 * * *")
        self.assertEqual(to_cron("weekly", "1 09:00"), "0 9 * * 1")
        self.assertEqual(to_cron("weekly", "7 09:00"), "0 9 * * 0")  # ISO 周日 -> cron 0
        self.assertEqual(to_cron("monthly", "15 09:00"), "0 9 15 * *")

    def test_timezone_is_honoured(self):
        """容器默认 UTC，不按时区折算会让"每周一 9 点"变成别的时间。"""
        from backend.survey.schedule import next_fire_time

        shanghai = next_fire_time("daily", "09:00", "Asia/Shanghai", after=self.BASE)
        utc = next_fire_time("daily", "09:00", "UTC", after=self.BASE)
        self.assertEqual(utc.hour, 9)
        self.assertEqual(shanghai.hour, 1)  # 09:00 CST == 01:00 UTC

    def test_weekly_lands_on_the_right_weekday(self):
        from backend.survey.schedule import next_fire_time

        moment = next_fire_time("weekly", "1 09:00", "Asia/Shanghai", after=self.BASE)
        self.assertEqual(moment, datetime(2026, 9, 14, 1, 0))

    def test_invalid_schedules_are_rejected_at_save_time(self):
        """非法周期必须在保存时报错，而不是某个周一早上没触发才由人去翻日志。"""
        from backend.survey.common import SurveyError
        from backend.survey.schedule import next_fire_time

        for kind, expr in (
            ("cron", "bad"),
            ("cron", "0 9 30 2 *"),   # 2月30日，永不触发
            ("weekly", "9 09:00"),
            ("daily", "25:00"),
            ("unknown", "09:00"),
        ):
            with self.subTest(kind=kind, expr=expr):
                with self.assertRaises(SurveyError):
                    next_fire_time(kind, expr, "UTC", after=self.BASE)

    def test_invalid_timezone_falls_back_to_utc(self):
        """时区写错不该让巡检起不来，但要能算出时间。"""
        from backend.survey.schedule import next_fire_time

        moment = next_fire_time("daily", "09:00", "Nowhere/Nothing", after=self.BASE)
        self.assertEqual(moment.hour, 9)


class CrossRepoTests(unittest.TestCase):
    def test_path_params_are_normalised(self):
        from backend.survey.crossrepo import normalize_path

        for raw in ("/api/order/:id", "/api/order/{id}", "/api/order/<id>",
                    "/api/order/${id}", "/api/order/$id"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_path(raw), "/api/order/*")
        self.assertEqual(normalize_path("/api/order/list/"), "/api/order/list")

    def test_case_is_preserved(self):
        """HTTP 路径大小写敏感，折叠会把两个不同接口误判成同一个。"""
        from backend.survey.crossrepo import normalize_path

        self.assertNotEqual(normalize_path("/api/User"), normalize_path("/api/user"))

    def _profiles(self):
        return [
            {
                "repo_slug": "api",
                "routes": [
                    {"method": "POST", "path": "/api/order/create", "file": "main.go",
                     "handler": "CreateOrder", "handler_file": "handlers/order.go"},
                    {"method": "GET", "path": "/api/unused", "file": "main.go",
                     "handler": "Unused", "handler_file": "handlers/x.go"},
                ],
                "api_calls": [],
            },
            {
                "repo_slug": "web",
                "routes": [],
                "api_calls": [
                    {"method": "GET", "path": "/api/order/create", "file": "src/api.ts", "line": 7},
                    {"method": "GET", "path": "/api/third-party", "file": "src/api.ts", "line": 20},
                ],
            },
        ]

    def test_links_connect_consumer_to_provider(self):
        from backend.survey.crossrepo import build_cross_repo_map

        result = build_cross_repo_map(self._profiles())
        self.assertEqual(len(result["links"]), 1)
        link = result["links"][0]
        self.assertEqual(link["from_repo"], "web")
        self.assertEqual(link["to_repo"], "api")
        self.assertEqual(link["handler"], "CreateOrder")

    def test_method_mismatch_and_orphans_are_reported(self):
        from backend.survey.crossrepo import build_cross_repo_map

        result = build_cross_repo_map(self._profiles())
        self.assertEqual(len(result["method_mismatch"]), 1)
        self.assertEqual([o["path"] for o in result["orphan_calls"]], ["/api/third-party"])
        self.assertEqual([u["path"] for u in result["unused_routes"]], ["/api/unused"])

    def test_missing_call_method_does_not_produce_false_mismatch(self):
        """`fetch(url, { method: "POST" })` 的方法在下一行，抽不到时宁可漏报也不要造假问题。"""
        from backend.survey.crossrepo import build_cross_repo_map

        profiles = self._profiles()
        profiles[1]["api_calls"][0]["method"] = ""
        result = build_cross_repo_map(profiles)
        self.assertEqual(result["method_mismatch"], [])
        self.assertEqual(len(result["links"]), 1)


class ProfileExtractionTests(unittest.TestCase):
    def test_route_registration_lines_are_not_counted_as_calls(self):
        """否则每个后端仓库都会显示成"自己调用了自己的接口"。"""
        from backend.survey.profile import _drop_route_registrations

        calls = [
            {"path": "/api/a", "file": "main.go", "line": 10},
            {"path": "/api/a", "file": "client.go", "line": 3},
        ]
        routes = [{"path": "/api/a", "file": "main.go"}]
        kept = _drop_route_registrations(calls, routes)
        self.assertEqual([c["file"] for c in kept], ["client.go"])

    def test_api_calls_are_extracted_from_source(self):
        """codegraph 完全不捕获调用侧的 URL 字面量，这一半必须我们自己抽（ADR-0003）。"""
        from backend.survey.profile import extract_api_calls

        with tempfile.TemporaryDirectory(prefix="opencr-profile-") as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "api.ts").write_text(
                'export function f() {\n'
                '  return fetch("/api/order/create", { method: "POST" });\n'
                '}\n',
                encoding="utf-8",
            )
            calls = extract_api_calls(root)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["path"], "/api/order/create")
        self.assertEqual(calls[0]["file"], "src/api.ts")


class JsonParsingTests(unittest.TestCase):
    def test_fenced_and_prefixed_json_are_both_parsed(self):
        from backend.survey.analysis import _parse_json_payload

        self.assertEqual(_parse_json_payload('```json\n[{"a": 1}]\n```'), [{"a": 1}])
        self.assertEqual(_parse_json_payload('结果如下：[{"a": 2}] 以上'), [{"a": 2}])
        self.assertIsNone(_parse_json_payload("完全不是 JSON"))


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

class SurveyStorageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="opencr-survey-db-")
        os.environ["OPENCR_DATABASE_URL"] = f"sqlite:///{Path(self.tmpdir, 'test.db')}"

        from backend.storage import db

        db.reset_engine_for_tests(os.environ["OPENCR_DATABASE_URL"])

        from backend.storage.models import Base

        Base.metadata.create_all(db.get_engine())

        from backend.storage import repo

        self.repo = repo
        self.survey = repo.create_survey(
            name="移动端周巡检", slug="mobile-weekly",
            schedule_kind="weekly", schedule_expr="1 09:00", timezone_name="Asia/Shanghai",
            sources=[{"kind": "repo", "url": "https://g.com/a/app.git", "branch": "main"}],
            next_run_at=datetime(2026, 9, 14, 1, 0),
        )

    def tearDown(self):
        from backend.storage import db

        db.reset_engine_for_tests()
        os.environ.pop("OPENCR_DATABASE_URL", None)

    def _run(self, entries, trigger="schedule", inspected=None):
        """
        跑一轮并落库；entries 是 (file_path, category) 列表。

        inspected 为 None 时不额外记录已取证文件（运行开始时写入的是空数组）。
        """
        from backend.storage.models import RUN_SUCCEEDED
        from backend.survey.common import finding_fingerprint

        run_uid = self.repo.start_survey_run(self.survey["survey_uid"], trigger)
        findings = [
            {
                "repo_slug": "app", "file_path": path, "line": 1, "category": category,
                "severity": "warning", "title": f"问题 {path}", "body": "详情",
                "fingerprint": finding_fingerprint("app", path, category),
            }
            for path, category in entries
        ]
        counters = self.repo.record_survey_findings(run_uid, findings)
        if inspected is not None:
            self.repo.record_survey_inspected_files(
                run_uid, [{"repo_slug": "app", "file_path": path, "conclusive": True} for path in inspected]
            )
        self.repo.finish_survey_run(run_uid, RUN_SUCCEEDED, summary="整体结论")
        return run_uid, counters


class SurveyConfigTests(SurveyStorageTestCase):
    def test_slug_collision_is_resolved(self):
        """两个中文名去掉非 ASCII 后可能撞 slug，不该把唯一索引错误抛给用户。"""
        other = self.repo.create_survey(
            name="另一个", slug="mobile-weekly", schedule_kind="daily",
            schedule_expr="09:00", timezone_name="UTC", sources=[],
        )
        self.assertNotEqual(other["slug"], self.survey["slug"])

    def test_rename_does_not_move_workspace(self):
        """改名就搬几十 GB 代码不划算，且搬运中断会留下说不清状态的工作区。"""
        updated = self.repo.update_survey(self.survey["survey_uid"], {"name": "新名字"})
        self.assertEqual(updated["name"], "新名字")
        self.assertEqual(updated["slug"], self.survey["slug"])

    def test_due_surveys_respect_enabled_and_time(self):
        due = self.repo.list_due_surveys(datetime(2026, 9, 14, 2, 0))
        self.assertEqual([s["slug"] for s in due], ["mobile-weekly"])

        self.assertEqual(self.repo.list_due_surveys(datetime(2026, 9, 13, 0, 0)), [])

        self.repo.update_survey(self.survey["survey_uid"], {"enabled": False})
        self.assertEqual(self.repo.list_due_surveys(datetime(2026, 9, 14, 2, 0)), [])

    def test_delete_survey_keeps_workspace(self):
        """误删一个巡检顺手把几十 GB 代码删掉是不可逆的。"""
        slug = self.repo.delete_survey(self.survey["survey_uid"])
        self.assertEqual(slug, "mobile-weekly")
        self.assertIsNone(self.repo.get_survey(self.survey["survey_uid"]))

    def test_max_repos_defaults_to_200_and_clamps_dirty_values(self):
        """未设置时取默认 200；库里的脏值不能让巡检变成零仓库或没有上限。"""
        from backend.survey.config import SETTING_MAX_REPOS, load_max_repos

        self.assertEqual(load_max_repos(), 200)
        self.repo.set_setting(SETTING_MAX_REPOS, "350")
        self.assertEqual(load_max_repos(), 350)
        self.repo.set_setting(SETTING_MAX_REPOS, "0")
        self.assertEqual(load_max_repos(), 1)
        self.repo.set_setting(SETTING_MAX_REPOS, "99999")
        self.assertEqual(load_max_repos(), 1000)
        self.repo.set_setting(SETTING_MAX_REPOS, "abc")
        self.assertEqual(load_max_repos(), 200)

    def test_resolve_sources_reports_truncated_count(self):
        """截断数要交给调用方记降级，只写日志会让被截掉的仓库悄无声息地退出巡检。"""
        from backend.survey.sources import resolve_sources

        sources = [{"kind": "repo", "url": f"https://g.com/a/r{i}.git"} for i in range(5)]
        targets, truncated = resolve_sources(sources, 3)
        self.assertEqual(len(targets), 3)
        self.assertEqual(truncated, 2)

        targets, truncated = resolve_sources(sources, 5)
        self.assertEqual((len(targets), truncated), (5, 0))


class SurveyFindingDiffTests(SurveyStorageTestCase):
    def test_new_and_persisted_are_labelled(self):
        _, first = self._run([("lib/a.dart", "security"), ("lib/b.dart", "performance")])
        self.assertEqual(first, {"new": 2, "persisted": 0, "ignored": 0})

        run2, second = self._run(
            [("lib/a.dart", "security"), ("lib/c.dart", "architecture")],
            inspected=["lib/a.dart", "lib/b.dart", "lib/c.dart"],
        )
        self.assertEqual(second, {"new": 1, "persisted": 1, "ignored": 0})

        detail = self.repo.get_survey_run_detail(run2)
        self.assertEqual(detail["counts"], {"new": 1, "persisted": 1, "resolved": 1, "unchecked": 0, "total": 2})
        self.assertEqual([f["file_path"] for f in detail["resolved_findings"]], ["lib/b.dart"])

    def test_run_that_failed_before_inspection_resolves_nothing(self):
        """取证前就失败的运行不能被当成早于该字段的旧运行，否则上一轮的发现会全部列进已消失。"""
        from backend.storage.models import RUN_FAILED

        self._run([("lib/a.dart", "security")], inspected=["lib/a.dart"])
        run_uid = self.repo.start_survey_run(self.survey["survey_uid"], "manual")
        self.repo.finish_survey_run(run_uid, RUN_FAILED, error_message="模型调用失败")
        detail = self.repo.get_survey_run_detail(run_uid)
        self.assertEqual(detail["resolved_findings"], [])
        self.assertEqual([f["file_path"] for f in detail["unchecked_findings"]], ["lib/a.dart"])

    def test_inconclusive_files_are_unchecked(self):
        self._run([("lib/a.dart", "security")], inspected=["lib/a.dart"])
        run_uid = self.repo.start_survey_run(self.survey["survey_uid"], "manual")
        self.repo.record_survey_inspected_files(
            run_uid, [{"repo_slug": "app", "file_path": "lib/a.dart", "conclusive": False}]
        )
        from backend.storage.models import RUN_SUCCEEDED

        self.repo.finish_survey_run(run_uid, RUN_SUCCEEDED)
        self.assertEqual(self.repo.get_survey_run_detail(run_uid)["counts"]["unchecked"], 1)

    def test_uninspected_files_are_unchecked_not_resolved(self):
        """上一轮的问题这一轮没再出现：所在文件取证过才算已消失，没取证过的只能说没复查。"""
        self._run([("lib/a.dart", "security"), ("lib/b.dart", "performance")], inspected=["lib/a.dart", "lib/b.dart"])
        run2, _ = self._run([], inspected=["lib/a.dart"])
        detail = self.repo.get_survey_run_detail(run2)
        self.assertEqual([f["file_path"] for f in detail["resolved_findings"]], ["lib/a.dart"])
        self.assertEqual([f["file_path"] for f in detail["unchecked_findings"]], ["lib/b.dart"])
        self.assertEqual((detail["counts"]["resolved"], detail["counts"]["unchecked"]), (1, 1))

    def test_resolved_findings_have_no_rows_of_their_own(self):
        """"已消失"是比对算出来的，凭空造行会让列表出现没有对应代码的幽灵条目。"""
        self._run([("lib/a.dart", "security")])
        run2 = self._run([], inspected=["lib/a.dart"])[0]
        detail = self.repo.get_survey_run_detail(run2)
        self.assertEqual(detail["findings"], [])
        self.assertEqual(len(detail["resolved_findings"]), 1)

    def test_legacy_runs_keep_the_naive_diff(self):
        """升级前的运行没有取证记录，全部归为未复查会让历史报告的已消失一夜清零。"""
        from sqlalchemy import update

        from backend.storage.db import session_scope
        from backend.storage.models import SurveyRun

        self._run([("lib/a.dart", "security")])
        run2 = self._run([])[0]
        with session_scope() as session:
            session.execute(update(SurveyRun).where(SurveyRun.run_uid == run2).values(inspected_files=None))
        detail = self.repo.get_survey_run_detail(run2)
        self.assertEqual(len(detail["resolved_findings"]), 1)
        self.assertEqual(detail["unchecked_findings"], [])

    def test_still_present_ledger_rows_are_persisted_not_new(self):
        """复核按名额轮转，隔几轮才被看到的老问题不能因为上一次没轮到它就变成"新增"。"""
        from backend.survey.common import finding_fingerprint

        fingerprint = finding_fingerprint("app", "lib/a.dart", "security")
        self.repo.upsert_survey_ledger(self._survey_id(), [{
            "fingerprint": fingerprint, "repo_slug": "app", "file_path": "lib/a.dart", "category": "security",
            "state": "present",
        }])
        self._run([])
        _, counters = self._run([("lib/a.dart", "security")])
        self.assertEqual(counters, {"new": 0, "persisted": 1, "ignored": 0})

        self.repo.upsert_survey_ledger(self._survey_id(), [{"fingerprint": fingerprint, "state": "unseen"}])
        self._run([])
        _, counters = self._run([("lib/a.dart", "security")])
        # 本轮未发现之后又出现，对读报告的人来说就是新问题
        self.assertEqual(counters["new"], 1)

    def _survey_id(self):
        from sqlalchemy import select

        from backend.storage.db import session_scope
        from backend.storage.models import Survey

        with session_scope() as session:
            return session.scalar(select(Survey.id).where(Survey.survey_uid == self.survey["survey_uid"]))

    def test_ignored_findings_are_never_stored(self):
        """入库再过滤的话，"本次 80 条"会一直包含用户说过不想再看的条目。"""
        from backend.survey.common import finding_fingerprint

        self.repo.add_survey_ignore(
            self.survey["survey_uid"], finding_fingerprint("app", "lib/a.dart", "security"), "已知"
        )
        run_uid, counters = self._run([("lib/a.dart", "security"), ("lib/d.dart", "dependency")])
        self.assertEqual(counters, {"new": 1, "persisted": 0, "ignored": 1})
        detail = self.repo.get_survey_run_detail(run_uid)
        self.assertEqual([f["file_path"] for f in detail["findings"]], ["lib/d.dart"])

    def test_removing_ignore_lets_finding_return(self):
        from backend.survey.common import finding_fingerprint

        fingerprint = finding_fingerprint("app", "lib/a.dart", "security")
        self.repo.add_survey_ignore(self.survey["survey_uid"], fingerprint)
        self.assertTrue(self.repo.remove_survey_ignore(self.survey["survey_uid"], fingerprint))
        _, counters = self._run([("lib/a.dart", "security")])
        self.assertEqual(counters["ignored"], 0)


class SurveyRetentionTests(SurveyStorageTestCase):
    def test_purge_never_deletes_the_latest_run(self):
        """删掉最近一次，下一轮报告会把所有问题标成新增——且看起来完全正常。"""
        for _ in range(3):
            self._run([("lib/a.dart", "security")])

        deleted = self.repo.purge_survey_runs_by_uid(self.survey["survey_uid"])
        self.assertEqual(deleted, 0)  # 默认保留 20 次

        self.repo.update_survey(self.survey["survey_uid"], {"retention_runs": 1})
        self.assertEqual(self.repo.purge_survey_runs_by_uid(self.survey["survey_uid"]), 2)
        self.assertEqual(len(self.repo.list_survey_runs(self.survey["survey_uid"])), 1)

    def test_retention_zero_still_keeps_one(self):
        for _ in range(2):
            self._run([("lib/a.dart", "security")])
        from backend.storage.db import session_scope
        from backend.storage.models import Survey
        from sqlalchemy import select

        with session_scope() as session:
            survey_id = session.scalar(select(Survey.id).where(Survey.slug == "mobile-weekly"))
        self.repo.purge_survey_runs(survey_id, 0)
        self.assertEqual(len(self.repo.list_survey_runs(self.survey["survey_uid"])), 1)


class SurveyGuestScopeTests(SurveyStorageTestCase):
    def test_guest_gets_no_body_title_or_summary(self):
        """巡检正文描述的是整个代码库的架构与弱点，边界只会更严格（ADR-0002）。"""
        run_uid, _ = self._run([("lib/a.dart", "security")])

        admin_view = self.repo.get_survey_run_detail(run_uid, include_body=True)
        self.assertIn("body", admin_view["findings"][0])
        self.assertIn("title", admin_view["findings"][0])
        self.assertEqual(admin_view["summary"], "整体结论")

        guest_view = self.repo.get_survey_run_detail(run_uid, include_body=False)
        self.assertNotIn("body", guest_view["findings"][0])
        self.assertNotIn("title", guest_view["findings"][0])
        self.assertEqual(guest_view["summary"], "")
        # 状态与聚合仍然可见——否则这个开关就没有存在价值
        self.assertEqual(guest_view["counts"]["total"], 1)
        self.assertEqual(guest_view["findings"][0]["severity"], "warning")

    def test_survey_guest_read_defaults_to_enabled(self):
        from backend.admin.auth import survey_guest_read_enabled

        self.assertTrue(survey_guest_read_enabled())
        self.repo.set_setting("survey_guest_read", "0")
        self.assertFalse(survey_guest_read_enabled())


class SurveyReportTests(SurveyStorageTestCase):
    def test_markdown_report_separates_three_states(self):
        from backend.survey.report import render_run_markdown

        self._run([("lib/a.dart", "security"), ("lib/b.dart", "performance")])
        run2, _ = self._run(
            [("lib/a.dart", "security"), ("lib/c.dart", "architecture")],
            inspected=["lib/a.dart", "lib/b.dart", "lib/c.dart"],
        )
        markdown = render_run_markdown(self.repo.get_survey_run_detail(run2))

        self.assertIn("## 新增", markdown)
        self.assertIn("## 仍存在", markdown)
        self.assertIn("## 较上次已消失", markdown)
        self.assertIn("lib/c.dart", markdown)
        # 与界面的页签一致，没有条目时也出这一节
        self.assertIn("## 本轮未复查", markdown)

    def test_markdown_report_lists_unchecked_separately(self):
        from backend.survey.report import render_run_markdown

        self._run([("lib/b.dart", "performance")], inspected=["lib/b.dart"])
        run2, _ = self._run([], inspected=[])
        markdown = render_run_markdown(self.repo.get_survey_run_detail(run2))
        section = markdown.split("## 本轮未复查", 1)[1]
        self.assertIn("lib/b.dart", section)
        self.assertNotIn("lib/b.dart", markdown.split("## 较上次已消失", 1)[1].split("## 本轮未复查", 1)[0])

    def test_guest_export_contains_no_body(self):
        """导出与界面同源，否则导出就成了绕过可见范围的后门。"""
        from backend.survey.report import render_run_markdown

        run_uid, _ = self._run([("lib/a.dart", "security")])
        markdown = render_run_markdown(self.repo.get_survey_run_detail(run_uid, include_body=False))
        self.assertNotIn("详情", markdown)
        self.assertIn("不含发现正文", markdown)


class ReachTests(unittest.TestCase):
    """Reach：L1 能点名的文件只限画像里带完整路径的那些，与目录深度无关。"""

    PROFILE = {
        "repo_slug": "app",
        "kind": "codegraph",
        "tree": ["main.go", "internal/ (order, user)"],
        "manifests": {"go.mod": "module app"},
        "routes": [{"method": "POST", "path": "/api/order", "handler": "Create", "file": "internal/order/handler.go"}],
        "types": [{"kind": "struct", "name": "Order", "file": "internal/order/deep/x/model.go"}],
        "api_calls": [],
        "languages": {},
    }
    SOURCES = [
        "main.go",
        "internal/order/handler.go",
        "internal/order/deep/x/model.go",
        "internal/order/service.go",
        "internal/user/service.go",
    ]

    def test_only_paths_in_profile_are_reachable_regardless_of_depth(self):
        """可达与否取决于文件有没有以完整路径出现在画像里，与目录深度无关。"""
        from backend.survey.reach import measure_reach

        reach = measure_reach(self.PROFILE, self.SOURCES, 100000, 0, {"internal/order/service.go": 30})
        self.assertEqual((reach["source_files"], reach["reachable_files"]), (5, 3))
        # 深层文件只要定义了类型就可达；只有函数的 service.go 不管多浅都不可达
        self.assertIn({"depth": 4, "reachable": 1, "total": 1}, reach["by_depth"])
        self.assertEqual(reach["by_source"]["types"], 1)
        self.assertEqual(reach["heavy_hidden_files"], [{"path": "internal/order/service.go", "symbols": 30}])
        self.assertEqual(reach["heavy_hidden_count"], 1)
        self.assertFalse(reach["truncated"])

    def test_truncation_removes_reach(self):
        """画像超出 L1 预算被截断时，截掉部分里的文件同样看不到。"""
        from backend.survey.reach import measure_reach

        reach = measure_reach(self.PROFILE, self.SOURCES, 120, 0)
        self.assertTrue(reach["truncated"])
        self.assertLess(reach["reachable_files"], reach["listed_files"])
        # 没有 codegraph 符号数时与"一个都没有"区分开
        self.assertIsNone(reach["heavy_hidden_count"])

    def test_rendered_paths_follows_render_profile(self):
        """反查与渲染格式必须一致，否则统计会悄悄算错。"""
        from backend.survey.profile import render_profile, rendered_paths

        text = render_profile(self.PROFILE, 0)
        self.assertEqual(
            rendered_paths(self.PROFILE, text),
            {"main.go", "go.mod", "internal/order/handler.go", "internal/order/deep/x/model.go"},
        )

    def test_scan_limit_ignores_files_the_scanner_skips(self):
        """调用侧扫描跳过的大文件不计入上限，按扩展名数会把"全扫了"误报成"只扫了一部分"。"""
        from backend.survey import reach

        root = Path(tempfile.mkdtemp(prefix="opencr-reach-scan-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "small.py").write_text("x", encoding="utf-8")
        (root / "huge.py").write_text("x" * (reach.MAX_SCANNED_FILE_BYTES + 1), encoding="utf-8")
        (root / "notes.sql").write_text("x", encoding="utf-8")
        sources = reach.walk_sources(root)
        self.assertEqual(len(sources), 3)
        # 只有 small.py 会被调用侧扫描计数：huge.py 超过大小上限，.sql 不在调用侧扩展名里
        self.assertEqual(reach._scan_candidates(root, sources), 1)

        many = [f"f{i}.py" for i in range(3001)]
        self.assertTrue(reach.measure_reach(self.PROFILE, many, 100000, 3001)["scan_limited"])
        self.assertFalse(reach.measure_reach(self.PROFILE, many, 100000, 2999)["scan_limited"])

    def test_backslash_paths_in_profile_still_match(self):
        """画像里的路径来自 str(Path)，在 Windows 上是反斜杠，统计时要与遍历结果对得上。"""
        from backend.survey.reach import measure_reach

        profile = {**self.PROFILE, "types": [{"kind": "struct", "name": "Order", "file": "internal\\order\\deep\\x\\model.go"}]}
        reach = measure_reach(profile, self.SOURCES, 100000, 0)
        self.assertEqual(reach["by_source"]["types"], 1)
        self.assertEqual(reach["reachable_files"], 3)

    def test_absolute_and_dotted_paths_in_profile_still_match(self):
        """codegraph 可能存绝对路径或带 "./"，不统一的话对应来源的可达数会被悄悄算成 0。"""
        from backend.survey.reach import measure_reach

        root = os.path.realpath(tempfile.mkdtemp(prefix="opencr-reach-root-"))
        self.addCleanup(shutil.rmtree, root, True)
        profile = {
            **self.PROFILE,
            "routes": [{"method": "POST", "path": "/api/order", "handler": "Create",
                        "file": os.path.join(root, "internal/order/handler.go")}],
            "types": [{"kind": "struct", "name": "Order", "file": "./internal/order/deep/x/model.go"}],
        }
        reach = measure_reach(profile, self.SOURCES, 100000, 0, repo_root=root)
        self.assertEqual((reach["by_source"]["routes"], reach["by_source"]["types"]), (1, 1))
        self.assertEqual(reach["reachable_files"], 3)

    def test_api_call_cap_is_judged_before_dropping_registrations(self):
        """剔除路由注册后条数会低于上限，事后再数会把撞了上限报成没撞。"""
        from unittest import mock

        from backend.survey import profile as profile_mod
        from backend.survey.reach import measure_reach

        calls = [{"path": f"/api/x/{i}", "method": "GET", "file": "srv.go", "line": i}
                 for i in range(profile_mod.MAX_API_CALLS)]
        # 第一条调用其实是路由注册那一行，codegraph 模式下会被剔除
        routes = [{"method": "GET", "path": "/api/x/0", "handler": "", "file": "srv.go"}]
        root = Path(tempfile.mkdtemp(prefix="opencr-reach-cap-"))
        self.addCleanup(shutil.rmtree, root, True)
        with mock.patch.object(profile_mod, "extract_api_calls", return_value=calls), \
                mock.patch.object(profile_mod, "codegraph_status", return_value="enabled"), \
                mock.patch.object(profile_mod, "build_index",
                                  return_value=profile_mod.IndexOutcome(root / "codegraph.db", "init", 0)), \
                mock.patch.object(profile_mod, "_extract_routes", return_value=routes), \
                mock.patch.object(profile_mod, "_extract_types", return_value=[]), \
                mock.patch.object(profile_mod, "_extract_languages", return_value={}):
            built = profile_mod.build_profile("app", root)
        self.assertLess(len(built["api_calls"]), profile_mod.MAX_API_CALLS)
        self.assertTrue(built["api_calls_capped"])
        self.assertIn("api_calls", measure_reach(built, ["srv.go"], 100000, 0)["caps"])

    def test_walk_skips_dependency_dirs(self):
        """依赖与产物目录不算仓库的源码，算进去会把可达比例压得失真。"""
        from backend.survey.reach import walk_sources

        root = Path(tempfile.mkdtemp(prefix="opencr-reach-"))
        self.addCleanup(shutil.rmtree, root, True)
        for rel in ("a.py", "pkg/b.py", "node_modules/x.js", ".git/hooks/y.sh", "README.md"):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        self.assertEqual(sorted(walk_sources(root)), ["a.py", "pkg/b.py"])


class ReachStorageTests(SurveyStorageTestCase):
    """Reach 的落库、游客剔除与报告展示。"""

    def test_reach_round_trips_and_guest_gets_counts_only(self):
        """具体目录与文件描述的是代码库结构，与正文同一档待遇。"""
        run_uid = self.repo.start_survey_run(self.survey["survey_uid"], "manual")
        self.repo.record_survey_repo(run_uid, "app", "https://g.com/a/app.git")
        self.repo.record_survey_repo_reach(run_uid, "app", {
            "source_files": 10, "reachable_files": 4,
            "hidden_dirs": [{"dir": "lib", "hidden": 6, "total": 8}],
            "heavy_hidden_files": [{"path": "lib/core.dart", "symbols": 40}],
        })
        admin = self.repo.get_survey_run_detail(run_uid)["repos"][0]["reach"]
        self.assertEqual(admin["hidden_dirs"][0]["dir"], "lib")
        guest = self.repo.get_survey_run_detail(run_uid, include_body=False)["repos"][0]["reach"]
        self.assertEqual(guest["reachable_files"], 4)
        self.assertNotIn("hidden_dirs", guest)
        self.assertNotIn("heavy_hidden_files", guest)

    def test_missing_reach_is_none(self):
        """没有统计记录时返回 None，界面显示为无记录。"""
        run_uid = self.repo.start_survey_run(self.survey["survey_uid"], "manual")
        self.repo.record_survey_repo(run_uid, "app", "https://g.com/a/app.git")
        self.assertIsNone(self.repo.get_survey_run_detail(run_uid)["repos"][0]["reach"])

    def test_report_shows_reach_cell(self):
        """Markdown 报告的仓库表带上可达比例，画像被截断时注明。"""
        from backend.survey.report import render_run_markdown

        run_uid = self.repo.start_survey_run(self.survey["survey_uid"], "manual")
        self.repo.record_survey_repo(run_uid, "app", "https://g.com/a/app.git")
        self.repo.record_survey_repo_reach(run_uid, "app", {"source_files": 8, "reachable_files": 2, "truncated": True})
        markdown = render_run_markdown(self.repo.get_survey_run_detail(run_uid))
        self.assertIn("2/8（25%），画像被截断", markdown)

    def test_reach_failure_does_not_break_the_run(self):
        """统计只是排查辅助，失败只记日志。"""
        from unittest import mock

        from backend.survey import runner
        from backend.survey.analysis import Budget

        run_uid = self.repo.start_survey_run(self.survey["survey_uid"], "manual")
        self.repo.record_survey_repo(run_uid, "app", "https://g.com/a/app.git")
        prepared = [{"slug": "app", "dir": Path("/nonexistent"), "profile": ReachTests.PROFILE}]
        with mock.patch.object(runner, "collect_reach", side_effect=RuntimeError("boom")):
            runner._record_reach(run_uid, prepared, {"links": []}, Budget(10, 10000, 5, 10000))
        with mock.patch.object(runner, "render_cross_repo_map", side_effect=RuntimeError("boom")):
            runner._record_reach(run_uid, prepared, {"links": []}, Budget(10, 10000, 5, 10000))
        # 没抛出就是巡检没被拖垮；统计失败的仓库落成"无记录"
        self.assertIsNone(self.repo.get_survey_run_detail(run_uid)["repos"][0]["reach"])


if __name__ == "__main__":
    unittest.main()


class IndexReuseTests(unittest.TestCase):
    """
    索引跨轮次复用的两条约束。

    两条都属于"坏掉了也不会报错"的类型：索引被误删只会让每轮退回全量（慢，但结果对），
    而复用了一份陈旧索引会让画像悄悄退回旧版本的水平——产出看起来完全正常。
    """

    def test_git_clean_preserves_the_index_directory(self):
        import subprocess

        from backend.survey.common import CODEGRAPH_INDEX_DIRNAME

        with tempfile.TemporaryDirectory(prefix="opencr-clean-") as tmp:
            repo_dir = Path(tmp, "repo")
            repo_dir.mkdir()
            run = lambda *a: subprocess.run(["git", "-C", str(repo_dir), *a], check=True, capture_output=True)
            run("init", "-q", "-b", "main")
            Path(repo_dir, "tracked.txt").write_text("x", encoding="utf-8")
            run("add", "-A")
            run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")

            index_dir = repo_dir / CODEGRAPH_INDEX_DIRNAME
            index_dir.mkdir()
            Path(index_dir, "codegraph.db").write_text("db", encoding="utf-8")
            Path(repo_dir, "junk.tmp").write_text("junk", encoding="utf-8")

            run("clean", "-fdx", "-e", CODEGRAPH_INDEX_DIRNAME)

            self.assertFalse(Path(repo_dir, "junk.tmp").exists(), "未跟踪文件应被清除")
            self.assertTrue(Path(index_dir, "codegraph.db").exists(), "索引目录必须被保留")

    def test_stale_index_is_not_reused(self):
        """任何拿不准的情况都判为不可复用——宁可多花几百毫秒重建。"""
        import json as _json
        from unittest import mock

        from backend.survey import profile

        healthy = {
            "initialized": True,
            "index": {"state": "complete", "reindexRecommended": False,
                      "builtWithExtractionVersion": 25, "currentExtractionVersion": 25},
        }
        cases = {
            "健康": (healthy, True),
            "抽取版本变化": ({**healthy, "index": {**healthy["index"],
                                                "builtWithExtractionVersion": 24}}, False),
            "自报需要重建": ({**healthy, "index": {**healthy["index"],
                                                "reindexRecommended": True}}, False),
            "索引不完整": ({**healthy, "index": {**healthy["index"], "state": "partial"}}, False),
            "未初始化": ({**healthy, "initialized": False}, False),
        }
        for name, (status, expected) in cases.items():
            with self.subTest(case=name):
                completed = mock.Mock(returncode=0, stdout=_json.dumps(status))
                with mock.patch.object(profile, "_run_codegraph", return_value=completed):
                    self.assertEqual(profile._index_is_reusable(Path("/tmp/x"), 60), expected)

        # status 本身跑不起来或输出不是 JSON，同样判为不可复用
        with mock.patch.object(profile, "_run_codegraph", return_value=None):
            self.assertFalse(profile._index_is_reusable(Path("/tmp/x"), 60))
        with mock.patch.object(profile, "_run_codegraph",
                               return_value=mock.Mock(returncode=0, stdout="not json")):
            self.assertFalse(profile._index_is_reusable(Path("/tmp/x"), 60))
