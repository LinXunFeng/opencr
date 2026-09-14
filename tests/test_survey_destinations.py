"""巡检输出（Ledger / Binding / Push / Google Sheet）测试。

重点覆盖几类"坏掉了也不会报错"的约束：
1. LedgerState 判定 —— 本轮没出现不等于已修复，结论不可信时必须保持原状态；
2. 镜像的补回规则 —— 用户删掉的已不出现的行不能被写回，新表却必须拿到完整台账；
3. 人工列不被触碰 —— 系统列按表头定位、按连续列段写入，写入必须是 RAW；
4. 推送与运行解耦 —— 推送失败不改写 SurveyRun，Guest 看不到目标位置与错误信息。
"""

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.test_survey import SurveyStorageTestCase  # noqa: E402


def _finding(path, category="security", line=1, severity="warning", title="标题", body="正文", repo="app"):
    """构造一条已带指纹的发现。"""
    from backend.survey.common import finding_fingerprint

    return {
        "repo_slug": repo, "file_path": path, "line": line, "category": category,
        "severity": severity, "title": title, "body": body,
        "fingerprint": finding_fingerprint(repo, path, category),
    }


# ---------------------------------------------------------------------------
# 纯函数：聚合与状态判定
# ---------------------------------------------------------------------------

class AggregateFindingsTests(unittest.TestCase):
    def test_same_fingerprint_merges_into_one_row(self):
        """同一文件同一类别的多条发现共享指纹，按"指纹 + 行号"分行会让行号漂移制造假的新增与消失。"""
        from backend.survey.ledger import aggregate_findings

        items = [
            _finding("lib/a.dart", line=40, severity="advice", title="次要", body="B"),
            _finding("lib/a.dart", line=12, severity="critical", title="严重", body="A"),
            _finding("lib/b.dart", line=3),
        ]
        result = aggregate_findings(items)
        self.assertEqual(len(result), 2)

        merged = result[items[0]["fingerprint"]]
        self.assertEqual(merged["severity"], "critical")
        self.assertEqual(merged["title"], "严重")
        self.assertEqual(merged["lines"], [12, 40])
        self.assertEqual(merged["finding_count"], 2)
        # 正文按严重度排序拼接，每条带行号小标题
        self.assertLess(merged["body"].index("[第 12 行] 严重"), merged["body"].index("[第 40 行] 次要"))

        single = result[items[2]["fingerprint"]]
        self.assertEqual(single["body"], "正文")
        self.assertEqual(single["finding_count"], 1)

    def test_conclusive_repos(self):
        """拉取/索引失败的仓库不可信；预算耗尽整轮不可信；codegraph 未启用不影响。"""
        from backend.survey.ledger import conclusive_repos

        repos = [
            {"repo_slug": "ok", "status": "ok"},
            {"repo_slug": "fetch", "status": "fetch_failed"},
            {"repo_slug": "index", "status": "index_failed"},
        ]
        self.assertEqual(conclusive_repos(repos, [{"kind": "profile_fallback", "count": 1}]), {"ok"})
        self.assertEqual(conclusive_repos(repos, [{"kind": "budget_exhausted", "count": 1}]), set())


class PlanLedgerUpdateTests(unittest.TestCase):
    NOW = datetime(2026, 9, 21, 1, 0)
    EARLIER = datetime(2026, 9, 14, 1, 0)

    def _entry(self, fingerprint, state="present", repo="app"):
        return {"fingerprint": fingerprint, "repo_slug": repo, "state": state, "first_seen_at": self.EARLIER}

    def _plan(self, existing, aggregated=None, reliable=("app",), ignored=(), lookup=None):
        from backend.survey.ledger import plan_ledger_update

        changes = plan_ledger_update(
            existing={e["fingerprint"]: e for e in existing},
            aggregated=aggregated or {},
            reliable_repos=set(reliable),
            ignored=set(ignored),
            first_seen_lookup=lookup or {},
            run_uid="run-2",
            now=self.NOW,
        )
        return {c["fingerprint"]: c for c in changes}

    def test_present_rows_keep_first_seen(self):
        from backend.survey.ledger import aggregate_findings

        item = _finding("lib/a.dart")
        changes = self._plan([self._entry(item["fingerprint"], state="unseen")], aggregate_findings([item]))
        change = changes[item["fingerprint"]]
        self.assertEqual(change["state"], "present")
        self.assertEqual(change["first_seen_at"], self.EARLIER)
        self.assertEqual(change["last_seen_at"], self.NOW)
        self.assertEqual(change["last_run_uid"], "run-2")

    def test_new_rows_take_first_seen_from_retained_history(self):
        from backend.survey.ledger import aggregate_findings

        item = _finding("lib/a.dart")
        changes = self._plan([], aggregate_findings([item]), lookup={item["fingerprint"]: self.EARLIER})
        self.assertEqual(changes[item["fingerprint"]]["first_seen_at"], self.EARLIER)

    def test_absent_rows_are_marked_unseen_only_when_conclusive(self):
        """本轮没看到 ≠ 已修复。仓库不可信时保持原状态，否则一次拉取失败就会把整个仓库的问题清空。"""
        changes = self._plan([self._entry("fp-app"), self._entry("fp-broken", repo="broken")])
        self.assertEqual(changes["fp-app"]["state"], "unseen")
        self.assertNotIn("fp-broken", changes)

    def test_unchanged_state_produces_no_change(self):
        self.assertEqual(self._plan([self._entry("fp", state="unseen")]), {})

    def test_ignored_rows_are_marked_regardless_of_conclusiveness(self):
        changes = self._plan([self._entry("fp", repo="broken")], ignored={"fp"})
        self.assertEqual(changes["fp"]["state"], "ignored")

    def test_unignored_row_waits_for_evidence(self):
        """取消忽略后没有证据就停在已忽略，结论可信的一轮没出现才变为本轮未发现。"""
        inconclusive = self._plan([self._entry("fp", state="ignored")], reliable=())
        self.assertEqual(inconclusive, {})
        conclusive = self._plan([self._entry("fp", state="ignored")])
        self.assertEqual(conclusive["fp"]["state"], "unseen")


class LedgerRowsTests(unittest.TestCase):
    SURVEY = {"slug": "mobile-weekly", "name": "移动端周巡检", "timezone": "Asia/Shanghai"}

    def _entries(self):
        base = {
            "repo_slug": "app", "file_path": "lib/a.dart", "category": "security", "severity": "critical",
            "title": "t", "body": "b", "lines": [3, 9], "finding_count": 2,
            "first_seen_at": datetime(2026, 9, 14, 1, 0), "last_seen_at": datetime(2026, 9, 21, 1, 0),
            "last_run_uid": "run-uid",
        }
        return [
            {**base, "fingerprint": "fp-present", "state": "present"},
            {**base, "fingerprint": "fp-unseen", "state": "unseen"},
            {**base, "fingerprint": "fp-ignored", "state": "ignored"},
        ]

    def test_restore_rules(self):
        """推送过的表只补回仍存在的行；从未推送过的新表拿到完整台账。"""
        from backend.survey.ledger import build_ledger_rows

        later = {r.key: r.restore_if_missing for r in build_ledger_rows(self.SURVEY, self._entries(), "", False)}
        self.assertEqual(later, {
            "mobile-weekly:fp-present": True,
            "mobile-weekly:fp-unseen": False,
            "mobile-weekly:fp-ignored": False,
        })
        first = build_ledger_rows(self.SURVEY, self._entries(), "", True)
        self.assertTrue(all(r.restore_if_missing for r in first))

    def test_values_are_human_readable(self):
        from backend.survey.ledger import build_ledger_rows

        row = build_ledger_rows(self.SURVEY, self._entries(), "https://cr.example.com/", False)[1]
        self.assertEqual(row.values["state"], "本轮未发现")
        self.assertEqual(row.values["severity"], "严重")
        self.assertEqual(row.values["category"], "安全")
        self.assertEqual(row.values["lines"], "3, 9")
        # 存的是 UTC，表里显示巡检所在时区
        self.assertEqual(row.values["first_seen"].strftime("%Y-%m-%d %H:%M"), "2026-09-14 09:00")
        # 后台是 hash 路由
        self.assertEqual(row.values["link"], "https://cr.example.com/admin/#/surveys/runs/run-uid")

    def test_link_column_only_with_public_url(self):
        from backend.survey.ledger import ledger_columns

        self.assertNotIn("link", [c.key for c in ledger_columns(False)])
        self.assertEqual(ledger_columns(True)[-1].key, "link")
        # 键列必须在第一位，插件据此定位行
        self.assertEqual(ledger_columns(False)[0].key, "key")


# ---------------------------------------------------------------------------
# 持久化：Ledger 更新、Binding 同步
# ---------------------------------------------------------------------------

class LedgerStorageTestCase(SurveyStorageTestCase):
    def _complete_run(self, findings, repos=(("app", "ok"),), degradations=(), status="succeeded"):
        """跑一轮完整的运行：登记仓库、落库发现、收尾并更新 Ledger。"""
        run_uid = self.repo.start_survey_run(self.survey["survey_uid"], "schedule")
        for slug, repo_status in repos:
            self.repo.record_survey_repo(run_uid, slug, f"https://g.com/a/{slug}.git", status=repo_status)
        for kind in degradations:
            self.repo.add_survey_degradation(run_uid, kind)
        self.repo.record_survey_findings(run_uid, list(findings))
        self.repo.finish_survey_run(run_uid, status)

        from backend.survey.ledger import update_ledger_for_run

        update_ledger_for_run(run_uid)
        return run_uid

    def _ledger(self):
        return {e["file_path"]: e for e in self.repo.list_survey_ledger(self._survey_id())}

    def _survey_id(self):
        from sqlalchemy import select

        from backend.storage.db import session_scope
        from backend.storage.models import Survey

        with session_scope() as session:
            return session.scalar(select(Survey.id).where(Survey.survey_uid == self.survey["survey_uid"]))


class LedgerStorageTests(LedgerStorageTestCase):
    def test_ledger_follows_runs(self):
        self._complete_run([_finding("lib/a.dart", line=1), _finding("lib/a.dart", line=7), _finding("lib/b.dart")])
        ledger = self._ledger()
        self.assertEqual(ledger["lib/a.dart"]["finding_count"], 2)
        self.assertEqual(ledger["lib/a.dart"]["lines"], [1, 7])

        self._complete_run([_finding("lib/a.dart")])
        ledger = self._ledger()
        self.assertEqual(ledger["lib/a.dart"]["state"], "present")
        self.assertEqual(ledger["lib/b.dart"]["state"], "unseen")

        self._complete_run([_finding("lib/b.dart")])
        self.assertEqual(self._ledger()["lib/b.dart"]["state"], "present")

    def test_budget_exhausted_run_marks_nothing_unseen(self):
        self._complete_run([_finding("lib/a.dart")])
        self._complete_run([], degradations=["budget_exhausted"])
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "present")

    def test_index_failed_repo_marks_nothing_unseen(self):
        self._complete_run([_finding("lib/a.dart")])
        self._complete_run([], repos=[("app", "index_failed")])
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "present")

    def test_failed_run_never_touches_ledger(self):
        """失败的运行产出不完整，拿它更新台账会把一大批问题错标成本轮未发现。"""
        self._complete_run([_finding("lib/a.dart")])
        self._complete_run([], status="failed")
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "present")

    def test_ignore_flips_ledger_row_immediately(self):
        """忽略之后手动重推一次，表里就应该看到变化，而不是等下一轮巡检。"""
        item = _finding("lib/a.dart")
        self._complete_run([item])
        self.repo.add_survey_ignore(self.survey["survey_uid"], item["fingerprint"])
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "ignored")
        # 取消忽略不会凭空改回存在
        self.repo.remove_survey_ignore(self.survey["survey_uid"], item["fingerprint"])
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "ignored")

    def test_ledger_outlives_run_retention(self):
        """Ledger 寿命跟随 Survey：运行记录按次数清理后，首次发现时间仍然是最初那一轮。"""
        self._complete_run([_finding("lib/a.dart")])
        first_seen = self._ledger()["lib/a.dart"]["first_seen_at"]
        self.repo.update_survey(self.survey["survey_uid"], {"retention_runs": 1})
        for _ in range(2):
            self._complete_run([_finding("lib/a.dart")])
            self.repo.purge_survey_runs_by_uid(self.survey["survey_uid"])
        self.assertEqual(len(self.repo.list_survey_runs(self.survey["survey_uid"])), 1)
        self.assertEqual(self._ledger()["lib/a.dart"]["first_seen_at"], first_seen)


class BindingStorageTests(SurveyStorageTestCase):
    SHEET_A = {"destination": "sheet", "target": {"spreadsheet": "a" * 30, "worksheet": "W"}}
    SHEET_B = {"destination": "sheet", "target": {"spreadsheet": "b" * 30, "worksheet": "W"}}

    def test_saving_survey_keeps_push_history_of_unchanged_bindings(self):
        """
        Binding 按实例名 + 目标位置增量同步。整体重建会清空最近成功推送时刻，
        下一次推送就会把用户删掉的行统统写回去。
        """
        from backend.storage.db import session_scope
        from backend.storage.models import SurveyBinding, utcnow

        uid = self.survey["survey_uid"]
        survey = self.repo.update_survey(uid, {}, bindings=[self.SHEET_A])
        binding_id = survey["bindings"][0]["id"]
        with session_scope() as session:
            session.get(SurveyBinding, binding_id).last_succeeded_push_at = utcnow()

        survey = self.repo.update_survey(uid, {"name": "改名"}, bindings=[self.SHEET_A, self.SHEET_B])
        by_target = {b["target"]["spreadsheet"][0]: b for b in survey["bindings"]}
        self.assertEqual(by_target["a"]["id"], binding_id)
        self.assertTrue(by_target["a"]["last_succeeded_push_at"])
        self.assertFalse(by_target["b"]["last_succeeded_push_at"])

        # 不传 bindings 表示不动
        self.assertEqual(len(self.repo.update_survey(uid, {"enabled": True})["bindings"]), 2)
        # 传空数组表示全部移除
        self.assertEqual(self.repo.update_survey(uid, {}, bindings=[])["bindings"], [])


# ---------------------------------------------------------------------------
# 推送编排（假插件）
# ---------------------------------------------------------------------------

class _FakeDestination:
    """记录每次推送收到的行；raise_error 非空时模拟平台故障。"""

    calls = []
    raise_error = ""


def _fake_destination_class():
    from backend.survey.destinations.base import Destination, DestinationError, PushStats, TargetField

    class FakeDestination(Destination):
        type_name = "fake"
        label = "假平台"
        target_fields = (TargetField("table", "表"),)

        def push(self, target, columns, rows):
            if _FakeDestination.raise_error:
                raise DestinationError(_FakeDestination.raise_error)
            _FakeDestination.calls.append({"target": target, "columns": columns, "rows": rows})
            return PushStats(updated=0, inserted=len(rows))

    return FakeDestination


class PushTestCase(LedgerStorageTestCase):
    """注册一个假的 Destination 类型与实例，并给巡检绑定它。"""

    def setUp(self):
        super().setUp()
        from backend.survey import destinations

        _FakeDestination.calls = []
        _FakeDestination.raise_error = ""
        patches = [
            mock.patch.dict(destinations.DESTINATION_TYPES, {"fake": _fake_destination_class()}),
            mock.patch.object(destinations, "load_file_config", return_value={
                "destinations": {"fake-1": {"type": "fake"}},
                "server": {"public_url": ""},
            }),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.survey = self.repo.update_survey(
            self.survey["survey_uid"], {}, bindings=[{"destination": "fake-1", "target": {"table": "T"}}]
        )
        self.binding_id = self.survey["bindings"][0]["id"]

    def _push(self, run_uid, trigger="manual", binding_id=None):
        from backend.survey.push import begin_pushes, execute_pushes

        started = begin_pushes(run_uid, trigger, binding_id)
        execute_pushes(started["started"])
        return started


class PushFlowTests(PushTestCase):
    def test_first_push_writes_full_ledger_then_only_restores_present(self):
        self._complete_run([_finding("lib/a.dart"), _finding("lib/b.dart")])
        run2 = self._complete_run([_finding("lib/a.dart")])

        self._push(run2)
        first = {r.values["file"]: r.restore_if_missing for r in _FakeDestination.calls[-1]["rows"]}
        self.assertEqual(first, {"lib/a.dart": True, "lib/b.dart": True})

        self._push(run2)
        second = {r.values["file"]: r.restore_if_missing for r in _FakeDestination.calls[-1]["rows"]}
        self.assertEqual(second, {"lib/a.dart": True, "lib/b.dart": False})

        pushes = self.repo.list_survey_pushes(run2, include_detail=True)
        self.assertEqual([p["status"] for p in pushes], ["succeeded", "succeeded"])
        self.assertEqual(pushes[0]["stats"], {"updated": 0, "inserted": 2, "skipped": 0})

    def test_only_latest_succeeded_run_is_pushable(self):
        from backend.survey.push import PushRejected

        run1 = self._complete_run([_finding("lib/a.dart")])
        self._complete_run([_finding("lib/a.dart")])
        with self.assertRaises(PushRejected):
            self._push(run1)

        failed = self._complete_run([], status="failed")
        with self.assertRaises(PushRejected):
            self._push(failed)

    def test_busy_binding_is_rejected_until_stale(self):
        """同一 Binding 并发推送会让两边都读到"键不存在"后各追加一行。"""
        from datetime import timedelta

        from backend.storage.db import session_scope
        from backend.storage.models import SurveyPush
        from backend.survey.push import begin_pushes

        run_uid = self._complete_run([_finding("lib/a.dart")])
        first = begin_pushes(run_uid, "manual")
        self.assertEqual(len(first["started"]), 1)
        second = begin_pushes(run_uid, "auto")
        self.assertEqual(second, {"started": [], "busy": [self.binding_id]})

        # 进程在推送中途被杀掉时记录会一直停在进行中，超过上限后不再阻塞
        with session_scope() as session:
            push = session.get(SurveyPush, first["started"][0])
            push.started_at = push.started_at - timedelta(seconds=self.repo.PUSH_STALE_SECONDS + 1)
        self.assertEqual(len(begin_pushes(run_uid, "manual")["started"]), 1)

    def test_push_failure_does_not_touch_run_status(self):
        run_uid = self._complete_run([_finding("lib/a.dart")])
        _FakeDestination.raise_error = "表格被删了"
        self._push(run_uid)

        push = self.repo.list_survey_pushes(run_uid, include_detail=True)[0]
        self.assertEqual(push["status"], "failed")
        self.assertEqual(push["error_message"], "表格被删了")
        self.assertEqual(self.repo.get_survey_run_detail(run_uid)["status"], "succeeded")
        self.assertEqual(self.repo.get_survey_run_detail(run_uid)["degradations"], [])

    def test_failed_push_does_not_count_as_first_push_done(self):
        """推送失败时不能刷新最近成功推送时刻，否则下一次成功推送会漏写本轮未发现的行。"""
        self._complete_run([_finding("lib/a.dart"), _finding("lib/b.dart")])
        run2 = self._complete_run([_finding("lib/a.dart")])
        _FakeDestination.raise_error = "限流"
        self._push(run2)
        _FakeDestination.raise_error = ""
        self._push(run2)
        rows = {r.values["file"]: r.restore_if_missing for r in _FakeDestination.calls[-1]["rows"]}
        self.assertTrue(rows["lib/b.dart"])

    def test_removed_destination_keeps_binding_and_fails_push(self):
        from backend.survey import destinations

        run_uid = self._complete_run([_finding("lib/a.dart")])
        with mock.patch.object(destinations, "load_file_config", return_value={}):
            self._push(run_uid)
        push = self.repo.list_survey_pushes(run_uid, include_detail=True)[0]
        self.assertEqual(push["status"], "failed")
        self.assertIn("输出目标未配置", push["error_message"])
        self.assertEqual(len(self.repo.get_survey(self.survey["survey_uid"])["bindings"]), 1)

    def test_removing_binding_keeps_push_records(self):
        """推送记录是留痕：绑定删掉后记录仍在，只是不能再从这条记录重推。"""
        run_uid = self._complete_run([_finding("lib/a.dart")])
        self._push(run_uid)
        self.repo.update_survey(self.survey["survey_uid"], {}, bindings=[])
        pushes = self.repo.list_survey_pushes(run_uid, include_detail=True)
        self.assertEqual(len(pushes), 1)
        self.assertIsNone(pushes[0]["binding_id"])
        self.assertEqual(pushes[0]["target"], {"table": "T"})

    def test_runner_publishes_after_success(self):
        """运行成功结束后自动更新台账并推送，推送记录的触发方式为自动。"""
        from backend.survey import runner

        run_uid = self.repo.start_survey_run(self.survey["survey_uid"], "schedule")
        self.repo.record_survey_repo(run_uid, "app", "https://g.com/a/app.git", status="ok")
        self.repo.record_survey_findings(run_uid, [_finding("lib/a.dart")])
        self.repo.finish_survey_run(run_uid, "succeeded")

        runner._publish(run_uid)
        pushes = self.repo.list_survey_pushes(run_uid, include_detail=True)
        self.assertEqual([(p["trigger"], p["status"]) for p in pushes], [("auto", "succeeded")])
        self.assertEqual(len(_FakeDestination.calls[-1]["rows"]), 1)


# ---------------------------------------------------------------------------
# 后台接口：Guest 边界与权限
# ---------------------------------------------------------------------------

class DestinationRouteTests(PushTestCase):

    def setUp(self):
        super().setUp()
        from flask import Flask

        from backend.admin import auth, routes

        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        self.app.register_blueprint(routes.admin_bp)
        self.client = self.app.test_client()
        patch = mock.patch.object(auth, "load_admin_config", return_value={"enabled": True, "bind_local_only": False})
        patch.start()
        self.addCleanup(patch.stop)
        self.run_uid = self._complete_run([_finding("lib/a.dart")])
        _FakeDestination.raise_error = "https://docs.google.com/spreadsheets/d/secret"
        self._push(self.run_uid)
        _FakeDestination.raise_error = ""

    def _admin(self):
        with self.client.session_transaction() as session:
            session["identity"] = "admin"

    def test_guest_sees_push_status_but_not_target_or_error(self):
        detail = self.client.get(f"/api/admin/survey-runs/{self.run_uid}").json
        self.assertEqual(detail["pushes"][0]["status"], "failed")
        self.assertNotIn("target", detail["pushes"][0])
        self.assertNotIn("error_message", detail["pushes"][0])
        self.assertNotIn("secret", json.dumps(detail))

        survey = self.client.get(f"/api/admin/surveys/{self.survey['survey_uid']}").json
        self.assertEqual(survey["bindings"][0]["destination"], "fake-1")
        self.assertNotIn("target", survey["bindings"][0])

        self._admin()
        detail = self.client.get(f"/api/admin/survey-runs/{self.run_uid}").json
        self.assertIn("secret", detail["pushes"][0]["error_message"])
        self.assertTrue(detail["pushable"])

    def test_manual_push_is_admin_only_even_with_guest_retry(self):
        self.repo.set_setting("guest_retry", "1")
        self.assertEqual(self.client.post(f"/api/admin/survey-runs/{self.run_uid}/push").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/destinations").status_code, 401)

        self._admin()
        with mock.patch("backend.admin.routes.execute_pushes_in_background") as background:
            response = self.client.post(f"/api/admin/survey-runs/{self.run_uid}/push", json={})
        self.assertEqual(response.status_code, 202)
        background.assert_called_once()

    def test_saving_binding_validates_destination(self):
        self._admin()
        uid = self.survey["survey_uid"]
        bad = self.client.patch(f"/api/admin/surveys/{uid}", json={
            "bindings": [{"destination": "nope", "target": {"table": "T"}}]
        })
        self.assertEqual(bad.status_code, 400)
        missing = self.client.patch(f"/api/admin/surveys/{uid}", json={
            "bindings": [{"destination": "fake-1", "target": {}}]
        })
        self.assertEqual(missing.status_code, 400)

        destinations = self.client.get("/api/admin/destinations").json
        self.assertEqual(destinations["items"][0]["name"], "fake-1")
        self.assertEqual(destinations["items"][0]["target_fields"][0]["key"], "table")

    def test_check_endpoint_returns_items_for_admin_only(self):
        from backend.survey.destinations.base import CHECK_OK, CheckItem

        url = "/api/admin/destinations/fake-1/check"
        self.assertEqual(self.client.post(url, json={"target": {"table": "T"}}).status_code, 401)

        self._admin()
        cls = _fake_destination_class()
        with mock.patch.object(cls, "check", return_value=[CheckItem("读取", CHECK_OK, "可以访问")]), \
                mock.patch.dict("backend.survey.destinations.DESTINATION_TYPES", {"fake": cls}):
            ok = self.client.post(url, json={"target": {"table": " T "}, "survey_name": "巡检"}).json
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["target"], {"table": "T"})
        self.assertEqual(ok["items"], [{"title": "读取", "level": "ok", "message": "可以访问"}])

        # 目标位置不合法、实例不存在：都是测试的正常结论，以检查项返回而不是 4xx
        invalid = self.client.post(url, json={"target": {}}).json
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["items"][0]["title"], "目标位置")
        missing = self.client.post("/api/admin/destinations/nope/check", json={"target": {}}).json
        self.assertFalse(missing["ok"])
        self.assertIn("输出目标未配置", missing["items"][0]["message"])

    def test_check_endpoint_survives_plugin_crash(self):
        self._admin()
        cls = _fake_destination_class()
        with mock.patch.object(cls, "check", side_effect=RuntimeError("boom")), \
                mock.patch.dict("backend.survey.destinations.DESTINATION_TYPES", {"fake": cls}):
            response = self.client.post("/api/admin/destinations/fake-1/check", json={"target": {"table": "T"}})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["ok"])
        self.assertIn("boom", response.json["items"][-1]["message"])

    def test_binding_to_removed_destination_can_be_kept(self):
        """实例从 config.yaml 删掉后，保存巡检的其它改动不能被这个 Binding 挡住。"""
        from backend.survey import destinations

        self._admin()
        uid = self.survey["survey_uid"]
        with mock.patch.object(destinations, "load_file_config", return_value={}):
            response = self.client.patch(f"/api/admin/surveys/{uid}", json={
                "name": "新名字",
                "bindings": [{"destination": "fake-1", "target": {"table": "T"}}],
            })
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["bindings"][0]["available"])


# ---------------------------------------------------------------------------
# Google Sheet 插件（假会话，不连真实接口）
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.content = json.dumps(self._payload).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self):
        return self._payload


class _FakeSession:
    """按顺序回放预设响应，并记录每个请求。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, params=None, json=None, timeout=None):
        from urllib.parse import unquote

        self.requests.append({"method": method, "url": unquote(url), "params": params, "json": json})
        return self.responses.pop(0)


def _sheet_meta(title="巡检", column_count=26, sheet_id=7):
    return _Response(payload={"sheets": [{"properties": {
        "sheetId": sheet_id, "title": title, "gridProperties": {"rowCount": 1000, "columnCount": column_count},
    }}]})


class GoogleSheetTests(unittest.TestCase):
    TARGET = {"spreadsheet": "x" * 30, "worksheet": "巡检"}

    def _destination(self, responses):
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        session = _FakeSession(responses)
        sleeps = []
        destination = GoogleSheetDestination(
            "sheet", {"credentials_file": "/nonexistent.json"}, session=session, sleep=sleeps.append
        )
        return destination, session, sleeps

    def _columns(self):
        from backend.survey.destinations.base import COLUMN_NUMBER, Column

        return [Column("key", "键"), Column("title", "标题"), Column("state", "状态"),
                Column("count", "同类条数", COLUMN_NUMBER)]

    def _row(self, key, restore=True, state="存在", title="t"):
        from backend.survey.destinations.base import LedgerRow

        return LedgerRow(key=key, values={"key": key, "title": title, "state": state, "count": 2},
                         restore_if_missing=restore)

    def test_normalize_target(self):
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        url = "https://docs.google.com/spreadsheets/d/1AbC_defGhijklmnopqrstuvwxyz0123/edit#gid=0"
        target = GoogleSheetDestination.normalize_target({"spreadsheet": url}, "移动端周巡检")
        self.assertEqual(target, {"spreadsheet": "1AbC_defGhijklmnopqrstuvwxyz0123", "worksheet": "移动端周巡检"})
        with self.assertRaises(ValueError):
            GoogleSheetDestination.normalize_target({"spreadsheet": "not an id"}, "x")
        with self.assertRaises(ValueError):
            GoogleSheetDestination.normalize_target({}, "x")

    def test_missing_credentials_file_is_a_config_error(self):
        from backend.survey.destinations import DestinationError
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        with self.assertRaises(DestinationError):
            GoogleSheetDestination.validate_options({})

    def test_column_letters_and_sheet_quoting(self):
        from backend.survey.destinations.google_sheet import column_letter, quote_sheet_title

        self.assertEqual([column_letter(i) for i in (0, 25, 26, 51, 52, 701, 702)],
                         ["A", "Z", "AA", "AZ", "BA", "ZZ", "AAA"])
        self.assertEqual(quote_sheet_title("Bob's 表"), "'Bob''s 表'")

    def test_new_worksheet_gets_headers_and_all_rows(self):
        destination, session, _ = self._destination([
            _Response(payload={"sheets": [{"properties": {"sheetId": 1, "title": "Sheet1",
                                                          "gridProperties": {"columnCount": 26}}}]}),
            _Response(payload={"replies": [{"addSheet": {"properties": {
                "sheetId": 9, "title": "巡检", "gridProperties": {"columnCount": 26}}}}]}),
            _Response(payload={"range": "'巡检'!A1:Z1"}),  # 空表头
            _Response(payload={}),  # 写表头
            _Response(payload={}),  # 追加
        ])
        stats = destination.push(self.TARGET, self._columns(),
                                 [self._row("s:1"), self._row("s:2", restore=False)])
        # 补不补回由核心层决定（新表的首次推送会把所有行都标成可补回），插件只照做
        self.assertEqual(stats.to_dict(), {"updated": 0, "inserted": 1, "skipped": 1})

        methods = [(r["method"], r["url"].rsplit("/", 1)[-1]) for r in session.requests]
        self.assertIn("addSheet", json.dumps(session.requests[1]["json"]))
        # 键列是新补出来的，不应再去读键列
        self.assertEqual(len(methods), 5)

        headers = session.requests[3]["json"]
        self.assertEqual(headers["valueInputOption"], "RAW")
        self.assertEqual([d["values"][0][0] for d in headers["data"]], ["键", "标题", "状态", "同类条数"])

        append = session.requests[4]
        self.assertTrue(append["url"].endswith("'巡检'!A1:append"))
        self.assertEqual(append["params"]["valueInputOption"], "RAW")
        self.assertEqual(append["json"]["values"][0], ["s:1", "t", "存在", 2])

    def test_existing_sheet_updates_only_system_columns(self):
        """
        用户调整过列序、插入了人工列：系统列按表头定位，
        写入按连续列段拆开，夹在中间的人工列一个字都不碰。
        """
        destination, session, _ = self._destination([
            _sheet_meta(),
            # A=键 B=负责人(人工) C=标题 D=状态；缺少"同类条数"
            _Response(payload={"values": [["键", "负责人", "标题", "状态"]]}),
            _Response(payload={}),  # 补表头
            _Response(payload={"values": [["s:1"], [], ["other:9"], ["s:1"], ["s:gone"]]}),
            _Response(payload={}),  # 批量更新
            _Response(payload={}),  # 追加
        ])
        rows = [
            self._row("s:1", state="本轮未发现"),
            self._row("s:new"),
            self._row("s:deleted-by-user", restore=False, state="本轮未发现"),
        ]
        stats = destination.push(self.TARGET, self._columns(), rows)
        self.assertEqual(stats.to_dict(), {"updated": 1, "inserted": 1, "skipped": 1})

        header_write = session.requests[2]["json"]["data"]
        self.assertEqual(header_write, [{"range": "'巡检'!E1", "values": [["同类条数"]]}])

        read_keys = session.requests[3]
        self.assertTrue(read_keys["url"].endswith("'巡检'!A2:A"))

        updates = session.requests[4]["json"]
        self.assertEqual(updates["valueInputOption"], "RAW")
        ranges = {d["range"]: d["values"][0] for d in updates["data"]}
        # 同一个键出现在第 2 行与第 5 行，两行都更新；B 列（负责人）被跳过
        self.assertEqual(ranges, {
            "'巡检'!A2:A2": ["s:1"], "'巡检'!C2:E2": ["t", "本轮未发现", 2],
            "'巡检'!A5:A5": ["s:1"], "'巡检'!C5:E5": ["t", "本轮未发现", 2],
        })

        appended = session.requests[5]["json"]["values"]
        # 人工列位置是 null，追加接口会跳过它而不是写空串
        self.assertEqual(appended, [["s:new", None, "t", "存在", 2]])

    def test_grid_is_widened_before_writing_new_headers(self):
        destination, session, _ = self._destination([
            _sheet_meta(column_count=2, sheet_id=42),
            _Response(payload={"values": [["键", "标题"]]}),
            _Response(payload={}),  # appendDimension
            _Response(payload={}),  # 补表头
            _Response(payload={"values": []}),
        ])
        destination.push(self.TARGET, self._columns(), [])
        dimension = session.requests[2]["json"]["requests"][0]["appendDimension"]
        self.assertEqual(dimension, {"sheetId": 42, "dimension": "COLUMNS", "length": 2})

    def test_rate_limit_is_retried_with_backoff(self):
        destination, session, sleeps = self._destination([
            _Response(status=429, payload={"error": {"message": "quota"}}, headers={"Retry-After": "3"}),
            _Response(status=503, payload={}),
            _sheet_meta(),
            _Response(payload={"values": [["键", "标题", "状态", "同类条数"]]}),
            _Response(payload={"values": []}),
        ])
        destination.push(self.TARGET, self._columns(), [])
        self.assertEqual(sleeps, [3, 2])

    def test_permission_error_names_the_service_account(self):
        from backend.survey.destinations import DestinationError

        destination, _, _ = self._destination([
            _Response(status=403, payload={"error": {"message": "The caller does not have permission"}}),
        ])
        destination._client_email = "opencr@proj.iam.gserviceaccount.com"
        with self.assertRaises(DestinationError) as ctx:
            destination.push(self.TARGET, self._columns(), [])
        self.assertIn("opencr@proj.iam.gserviceaccount.com", str(ctx.exception))
        self.assertIn("编辑者", str(ctx.exception))

    def _check_meta(self, sheets=("巡检",)):
        return _Response(payload={
            "properties": {"title": "质量台账"},
            "sheets": [{"properties": {"sheetId": i, "title": t}} for i, t in enumerate(sheets)],
        })

    def test_check_passes_without_writing_ledger_rows(self):
        """测试不写任何台账行：写权限用"把标题改成它自己"验证，内容与标题都不变。"""
        from backend.survey.destinations.base import CHECK_OK

        destination, session, _ = self._destination([
            self._check_meta(),
            _Response(payload={"values": [["键", "负责人", "标题"]]}),
            _Response(payload={}),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual([i.level for i in items], [CHECK_OK] * 4)
        self.assertIn("2/4", items[2].message)
        self.assertIn("状态、同类条数", items[2].message)

        write = session.requests[-1]
        self.assertTrue(write["url"].endswith(":batchUpdate"))
        self.assertEqual(write["json"]["requests"], [{"updateSpreadsheetProperties": {
            "properties": {"title": "质量台账"}, "fields": "title",
        }}])
        self.assertFalse(any("/values:" in r["url"] or ":append" in r["url"] for r in session.requests))

    def test_check_reports_missing_worksheet_as_warning(self):
        from backend.survey.destinations.base import CHECK_WARN

        destination, session, _ = self._destination([self._check_meta(sheets=("Sheet1",)), _Response(payload={})])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual(items[2].level, CHECK_WARN)
        self.assertIn("自动创建", items[2].message)
        # 测试不创建工作表
        self.assertNotIn("addSheet", json.dumps([r["json"] for r in session.requests]))

    def test_check_stops_at_first_fatal_problem(self):
        from backend.survey.destinations.base import CHECK_ERROR

        destination, session, sleeps = self._destination([
            _Response(status=404, payload={"error": {"message": "Requested entity was not found."}}),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual([i.title for i in items], ["服务账号凭据", "读取表格"])
        self.assertEqual(items[-1].level, CHECK_ERROR)
        self.assertEqual(len(session.requests), 1)

    def test_rejected_credentials_are_not_reported_as_network_failure(self):
        """密钥能解析不代表凭据有效；被 Google 拒绝时不重试，也不能说成"无法连接"。"""
        from backend.survey.destinations.base import CHECK_ERROR

        class RefreshError(Exception):
            pass

        destination, session, sleeps = self._destination([])
        session.request = mock.Mock(side_effect=RefreshError("invalid_grant: account not found"))
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual([(i.title, i.level) for i in items], [("服务账号凭据", CHECK_ERROR)])
        self.assertIn("invalid_grant", items[0].message)
        self.assertNotIn("无法连接", items[0].message)
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_check_reports_read_only_access(self):
        from backend.survey.destinations.base import CHECK_ERROR

        destination, _, _ = self._destination([
            self._check_meta(),
            _Response(payload={"values": [["键", "标题", "状态", "同类条数"]]}),
            _Response(status=403, payload={"error": {"message": "The caller does not have permission"}}),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual((items[-1].title, items[-1].level), ("写入权限", CHECK_ERROR))
        self.assertIn("编辑者", items[-1].message)

    def test_check_does_not_wait_out_long_backoff(self):
        """测试是用户在表单里同步等结果，不能套用推送的五次退避。"""
        destination, session, sleeps = self._destination([
            _Response(status=429, payload={}),
            _Response(status=429, payload={"error": {"message": "quota"}}),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertIn("429", items[-1].message)

    def test_missing_credentials_message_points_at_likely_causes(self):
        """"路径明明是对的"几乎总是行内注释被读进了值里，或者服务跑在容器里。"""
        from backend.survey.destinations import DestinationError
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        raw = '"/Users/lxf/path/to/accout.json"  # 服务账号'
        destination = GoogleSheetDestination("sheet", {"credentials_file": raw})
        with mock.patch.dict(sys.modules, {"google.auth.transport.requests": mock.Mock(),
                                           "google.oauth2": mock.Mock()}):
            with self.assertRaises(DestinationError) as ctx:
                destination._get_session()
        message = str(ctx.exception)
        self.assertIn(repr(raw), message)
        self.assertIn("行内注释", message)

    def test_long_cells_are_truncated_under_the_hard_limit(self):
        """超过五万字符会让整批写入失败，而不是只截断那一格。"""
        from backend.survey.destinations.base import COLUMN_LONG_TEXT, Column
        from backend.survey.destinations.google_sheet import CELL_CHAR_LIMIT, _cell_value

        value = _cell_value(Column("body", "正文", COLUMN_LONG_TEXT), "x" * (CELL_CHAR_LIMIT + 10))
        self.assertEqual(len(value), CELL_CHAR_LIMIT)
        self.assertIn("已截断", value)
        self.assertEqual(_cell_value(Column("t", "t"), None), "")

    def test_large_updates_are_split_into_batches(self):
        from backend.survey.destinations.google_sheet import _chunks_by_size

        items = [{"range": f"A{i}", "values": [["x" * 100]]} for i in range(50)]
        batches = _chunks_by_size(items, max_bytes=1000)
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(len(b) for b in batches), 50)


if __name__ == "__main__":
    unittest.main()
