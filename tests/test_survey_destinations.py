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

    def test_table_object_is_appended_with_append_cells(self):
        """
        用户把区域转换成了表格对象：values.append 认不出它，会把新行插到工作表顶上把表头挤下去。
        这里表格对象从第 5 行开始（就是被挤下去之后的样子），表头、键列都要跟着它走，追加改用 appendCells。
        """
        table = {"tableId": "t1", "range": {"sheetId": 7, "startRowIndex": 4, "endRowIndex": 9,
                                            "startColumnIndex": 0, "endColumnIndex": 4}}
        destination, session, _ = self._destination([
            _Response(payload={"sheets": [{
                "properties": {"sheetId": 7, "title": "巡检", "gridProperties": {"columnCount": 26}},
                "tables": [table],
            }]}),
            # A=键 B=处理人(人工) C=标题 D=状态；缺少"同类条数"
            _Response(payload={"values": [["键", "处理人", "标题", "状态"]]}),
            _Response(payload={}),  # 补表头
            _Response(payload={}),  # updateTable 扩到 E 列
            _Response(payload={"values": [["s:1"]]}),
            _Response(payload={}),  # 批量更新
            _Response(payload={}),  # appendCells
        ])
        stats = destination.push(self.TARGET, self._columns(), [self._row("s:1"), self._row("s:new", title="=1+1")])
        self.assertEqual(stats.to_dict(), {"updated": 1, "inserted": 1, "skipped": 0})

        self.assertTrue(session.requests[1]["url"].endswith("'巡检'!5:5"))
        self.assertEqual(session.requests[2]["json"]["data"], [{"range": "'巡检'!E5", "values": [["同类条数"]]}])

        update_table = session.requests[3]["json"]["requests"][0]["updateTable"]
        self.assertEqual(update_table["fields"], "range")
        self.assertEqual(update_table["table"]["range"]["endColumnIndex"], 5)

        self.assertTrue(session.requests[4]["url"].endswith("'巡检'!A6:A"))
        ranges = {d["range"] for d in session.requests[5]["json"]["data"]}
        self.assertEqual(ranges, {"'巡检'!A6:A6", "'巡检'!C6:E6"})

        append = session.requests[6]
        self.assertTrue(append["url"].endswith(":batchUpdate"))
        request = append["json"]["requests"][0]["appendCells"]
        self.assertEqual(request["tableId"], "t1")
        self.assertEqual(request["fields"], "userEnteredValue")
        # 人工列写空 CellData；以 "=" 开头的标题写成 stringValue，不会被当成公式
        self.assertEqual(request["rows"], [{"values": [
            {"userEnteredValue": {"stringValue": "s:new"}},
            {},
            {"userEnteredValue": {"stringValue": "=1+1"}},
            {"userEnteredValue": {"stringValue": "存在"}},
            {"userEnteredValue": {"numberValue": 2}},
        ]}])

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
        destination._transport.client_email = "opencr@proj.iam.gserviceaccount.com"
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

    def test_check_reads_headers_from_table_object(self):
        """表格对象不在第 1 行时，测试连通性也要读它的首行，否则会误报"没有任何系统列"。"""
        destination, session, _ = self._destination([
            _Response(payload={"properties": {"title": "质量台账"}, "sheets": [{
                "properties": {"sheetId": 0, "title": "巡检"},
                "tables": [{"tableId": "t1", "range": {"startRowIndex": 4, "endColumnIndex": 4}}],
            }]}),
            _Response(payload={"values": [["键", "标题", "状态", "同类条数"]]}),
            _Response(payload={}),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertTrue(session.requests[1]["url"].endswith("'巡检'!5:5"))
        self.assertIn("系统列齐全", items[2].message)

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
                destination._transport._get_session()
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


# ---------------------------------------------------------------------------
# Google Sheet 插件：gogcli 鉴权方式（假子进程，不依赖真实安装的 gogcli）
# ---------------------------------------------------------------------------

class _FakeGogcli:
    """按顺序回放预设结果；记录每次调用的参数、环境与请求体文件内容。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, args, capture_output=None, text=None, timeout=None, env=None):
        import subprocess

        body = None
        if "--body" in args:
            path = args[args.index("--body") + 1].lstrip("@")
            body = json.loads(Path(path).read_text(encoding="utf-8"))
            self.body_paths = getattr(self, "body_paths", []) + [path]
        self.calls.append({"args": args, "env": env, "timeout": timeout, "body": body})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        code, stdout, stderr = result
        return subprocess.CompletedProcess(args, code, stdout, stderr)


def _ok(payload=None):
    return (0, json.dumps(payload if payload is not None else {}), "")


class GogcliTests(unittest.TestCase):
    TARGET = {"spreadsheet": "x" * 30, "worksheet": "巡检"}
    ACCOUNT = "someone@company.com"

    def _destination(self, results, options=None):
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        runner = _FakeGogcli(results)
        sleeps = []
        destination = GoogleSheetDestination(
            "sheet", {"auth": "gogcli", "account": self.ACCOUNT, "gogcli_bin": "/opt/bin/gog", **(options or {})},
            runner=runner, sleep=sleeps.append,
        )
        return destination, runner, sleeps

    def _columns(self):
        from backend.survey.destinations.base import Column

        return [Column("key", "键"), Column("title", "标题")]

    def _row(self, key):
        from backend.survey.destinations.base import LedgerRow

        return LedgerRow(key=key, values={"key": key, "title": "=IMPORTXML(x)"}, restore_if_missing=True)

    def test_options_require_explicit_account(self):
        from backend.survey.destinations import DestinationError
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        GoogleSheetDestination.validate_options({"credentials_file": "/k.json"})  # 缺省仍是服务账号
        for options in ({"auth": "gogcli"}, {"auth": "gogcli", "account": "auto"}, {"auth": "oauth"}):
            with self.subTest(options=options), self.assertRaises(DestinationError):
                GoogleSheetDestination.validate_options(options)

    def test_dropdown_summary_names_auth_mode_without_secrets(self):
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        self.assertEqual(GoogleSheetDestination.describe_options({"credentials_file": "/secret/k.json"}), "服务账号")
        self.assertEqual(
            GoogleSheetDestination.describe_options({"auth": "gogcli", "account": self.ACCOUNT}),
            f"gogcli · {self.ACCOUNT}",
        )

    def test_push_goes_through_api_call_with_body_file(self):
        """
        全部走 `gog api call`：写操作带 --allow-write --force，读操作不带；
        请求体经临时文件传入而不出现在参数里，调用结束后文件被删除；写入仍是 RAW。
        """
        destination, runner, _ = self._destination([
            _ok({"sheets": [{"properties": {"sheetId": 3, "title": "巡检", "gridProperties": {"columnCount": 26}}}]}),
            _ok({"values": [["键", "标题"]]}),
            _ok({"values": [["other:1"]]}),
            _ok({}),
        ])
        stats = destination.push(self.TARGET, self._columns(), [self._row("s:new")])
        self.assertEqual(stats.inserted, 1)

        methods = [c["args"][5] for c in runner.calls]
        self.assertEqual(methods, [
            "spreadsheets.get", "spreadsheets.values.get", "spreadsheets.values.get", "spreadsheets.values.append",
        ])
        for call in runner.calls:
            args = call["args"]
            self.assertEqual(args[:5], ["/opt/bin/gog", "api", "call", "sheets", "v4"])
            self.assertEqual(args[args.index("--account") + 1], self.ACCOUNT)
            self.assertEqual(args[args.index("--scope") + 1], "https://www.googleapis.com/auth/spreadsheets")
            self.assertIn("--no-input", args)
            self.assertEqual(call["env"]["GOG_KEYRING_LOCK_TIMEOUT"], "30s")
        self.assertNotIn("--allow-write", runner.calls[0]["args"])

        append = runner.calls[-1]
        self.assertIn("--allow-write", append["args"])
        self.assertIn("--force", append["args"])
        params = json.loads(append["args"][append["args"].index("--params") + 1])
        self.assertEqual(params["valueInputOption"], "RAW")
        self.assertEqual(params["range"], "'巡检'!A1")
        self.assertEqual(append["body"]["values"], [["s:new", "=IMPORTXML(x)"]])
        # 正文不出现在命令行参数里
        self.assertFalse(any("IMPORTXML" in a for a in append["args"]))
        self.assertTrue(all(not Path(p).exists() for p in runner.body_paths))

    def test_exit_codes_become_actionable_errors(self):
        from backend.survey.destinations import DestinationError
        from backend.survey.destinations.google_transport import AuthRejected

        cases = [
            ((6, "", "Google API error (403 forbidden): nope"), DestinationError, ["编辑者", self.ACCOUNT]),
            ((5, "", "Google API error (404)"), DestinationError, ["表格不存在"]),
            ((4, "", "refresh token expired or revoked"), AuthRejected, ["gog auth add " + self.ACCOUNT, "Testing"]),
            ((1, "", "macOS Keychain may be waiting for a permission prompt"), AuthRejected, ["始终允许"]),
            ((1, "", "no TTY available for keyring file backend password prompt"), AuthRejected, ["GOG_KEYRING_PASSWORD"]),
            ((2, "", "unknown flag"), DestinationError, ["版本不兼容"]),
            ((10, "", "OAuth client credentials missing (OAuth client ID JSON)."), AuthRejected, ["gog auth credentials set"]),
        ]
        for result, error, phrases in cases:
            with self.subTest(result=result):
                destination, _, _ = self._destination([result])
                with self.assertRaises(error) as ctx:
                    destination.push(self.TARGET, self._columns(), [])
                for phrase in phrases:
                    self.assertIn(phrase, str(ctx.exception))

    def test_missing_binary_points_at_absolute_path(self):
        from backend.survey.destinations import DestinationError

        destination, _, _ = self._destination([FileNotFoundError("gog")])
        with self.assertRaises(DestinationError) as ctx:
            destination.push(self.TARGET, self._columns(), [])
        self.assertIn("gogcli_bin", str(ctx.exception))

    def test_rate_limit_retried_after_gogcli_gives_up_but_not_in_check(self):
        """gogcli 自己已经重试过；推送时再补两次长间隔重试，测试时一次都不补。"""
        destination, runner, sleeps = self._destination([
            (7, "", "rate limited"), (8, "", "timeout"),
            _ok({"sheets": [{"properties": {"sheetId": 1, "title": "巡检", "gridProperties": {"columnCount": 26}}}]}),
            _ok({"values": [["键", "标题"]]}),
            _ok({"values": []}),
        ])
        destination.push(self.TARGET, self._columns(), [])
        self.assertEqual(sleeps, [15, 45])

        destination, runner, sleeps = self._destination([
            _ok(), _ok({"accounts": [{"email": self.ACCOUNT, "services": ["sheets"]}]}),
            (7, "", "rate limited"),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual(sleeps, [])
        self.assertEqual(items[-1].title, "读取表格")
        self.assertIn("限流", items[-1].message)

    def test_check_verifies_binary_and_account_first(self):
        from backend.survey.destinations.base import CHECK_ERROR, CHECK_OK

        missing, _, _ = self._destination([FileNotFoundError("gog")])
        items = missing.check(self.TARGET, self._columns())
        self.assertEqual([(i.title, i.level) for i in items], [("gogcli 可执行文件", CHECK_ERROR)])

        other, _, _ = self._destination([(0, "v0.40.0", ""), _ok({"accounts": [{"email": "a@b.com"}]})])
        items = other.check(self.TARGET, self._columns())
        self.assertEqual(items[-1].level, CHECK_ERROR)
        self.assertIn("a@b.com", items[-1].message)
        self.assertIn("gog auth add " + self.ACCOUNT, items[-1].message)

        no_sheets, _, _ = self._destination([
            (0, "v0.40.0", ""), _ok({"accounts": [{"email": self.ACCOUNT.upper(), "services": ["gmail"]}]}),
        ])
        items = no_sheets.check(self.TARGET, self._columns())
        self.assertEqual(items[-1].level, CHECK_ERROR)
        self.assertIn("没有包含 Sheets", items[-1].message)

        ok, runner, _ = self._destination([
            (0, "v0.40.0", ""),
            _ok({"accounts": [{"email": self.ACCOUNT, "services": ["sheets", "drive"]}]}),
            _ok({"properties": {"title": "质量台账"}, "sheets": [{"properties": {"title": "巡检"}}]}),
            _ok({"values": [["键", "标题"]]}),
            _ok({}),
        ])
        items = ok.check(self.TARGET, self._columns())
        self.assertEqual([i.level for i in items], [CHECK_OK] * 5)
        self.assertEqual(items[0].title, "gogcli 可执行文件")
        self.assertIn("v0.40.0", items[0].message)
        write = runner.calls[-1]
        self.assertEqual(write["args"][5], "spreadsheets.batchUpdate")
        self.assertIn("updateSpreadsheetProperties", json.dumps(write["body"]))

    def test_expired_authorization_is_reported_under_account_item(self):
        """账号在 gogcli 里，但令牌已失效：第一次真实请求才会暴露，结论要归到账号授权那一项。"""
        from backend.survey.destinations.base import CHECK_ERROR

        destination, _, _ = self._destination([
            (0, "v0.40.0", ""),
            _ok({"accounts": [{"email": self.ACCOUNT, "services": ["sheets"]}]}),
            (4, "", "refresh token expired or revoked"),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual([(i.title, i.level) for i in items][-1], ("gogcli 账号授权", CHECK_ERROR))
        self.assertEqual(len(items), 2)



# ---------------------------------------------------------------------------
# 部署脚本用的 gogcli 授权检查（install.sh / docker-entrypoint.sh / setup-gogcli.sh）
# ---------------------------------------------------------------------------

class GogcliStatusTests(unittest.TestCase):
    ACCOUNT = "someone@company.com"

    def _transport(self, results):
        from backend.survey.destinations.google_transport import GogcliTransport

        return GogcliTransport(self.ACCOUNT, "/opt/bin/gog", runner=_FakeGogcli(results))

    def test_only_fixable_problems_ask_for_auth_add(self):
        """
        只有"账号不在 / 没授权 Sheets"才引向 `gog auth add`；
        钥匙串被拒、令牌读取失败重新授权也解决不了，部署脚本不该让人白走一遍浏览器授权。
        """
        cases = [
            (_ok({"accounts": [{"email": "a@b.com"}]}), True),
            (_ok({"accounts": [{"email": self.ACCOUNT, "services": ["gmail"]}]}), True),
            (_ok({"accounts": [{"email": self.ACCOUNT, "services": ["sheets"]}]}), False),
            (_ok({"accounts": [{"email": self.ACCOUNT, "error": "keychain locked"}]}), False),
            ((1, "", "Keychain access denied"), False),
        ]
        for listed, expected in cases:
            with self.subTest(listed=listed):
                self.assertEqual(self._transport([listed]).check_account()[1], expected)

    def _run(self, destinations, results, argv):
        """以假的配置与假子进程跑一次命令行入口，返回 (退出码, 输出, 子进程调用次数)。"""
        import contextlib
        import io

        from backend.survey.destinations import gogcli_status
        from backend.survey.destinations.google_transport import GogcliTransport

        runner = _FakeGogcli(results)

        def transport(options):
            return GogcliTransport(options.get("account", ""), options.get("gogcli_bin") or "gog", runner=runner)

        out = io.StringIO()
        with mock.patch.object(gogcli_status, "load_destinations", return_value=destinations), \
                mock.patch.object(gogcli_status, "_transport", side_effect=transport), \
                contextlib.redirect_stdout(out):
            code = gogcli_status.main(argv)
        return code, out.getvalue(), len(runner.calls)

    def _info(self, name, options, type_name="google_sheet", error=""):
        from backend.survey.destinations.base import DestinationInfo

        return DestinationInfo(name=name, type_name=type_name, options=options, error=error)

    def test_needs_auth_lists_each_account_once_and_ignores_other_instances(self):
        destinations = {
            "a": self._info("a", {"auth": "gogcli", "account": self.ACCOUNT}),
            "b": self._info("b", {"auth": "gogcli", "account": self.ACCOUNT}),
            "sa": self._info("sa", {"credentials_file": "/k.json"}),
            "broken": self._info("broken", {"auth": "gogcli"}, error="缺少 account"),
        }
        code, out, calls = self._run(destinations, [_ok({"accounts": []})], ["--needs-auth"])
        self.assertEqual((code, out, calls), (0, f"{self.ACCOUNT}\n", 1))

    def test_no_gogcli_instance_prints_nothing(self):
        """install.sh 靠空输出判断"没有 gogcli 实例"并跳过整步；输出一个空行会被当成空账号。"""
        destinations = {"sa": self._info("sa", {"credentials_file": "/k.json"})}
        for argv in (["--accounts"], ["--needs-auth"], []):
            with self.subTest(argv=argv):
                self.assertEqual(self._run(destinations, [], argv), (0, "", 0))

    def test_report_fails_on_any_problem_and_checks_shared_account_once(self):
        destinations = {
            "a": self._info("a", {"auth": "gogcli", "account": self.ACCOUNT}),
            "b": self._info("b", {"auth": "gogcli", "account": self.ACCOUNT}),
            "broken": self._info("broken", {"auth": "gogcli"}, error="缺少 account"),
        }
        listed = _ok({"accounts": [{"email": self.ACCOUNT, "services": ["sheets"]}]})
        code, out, calls = self._run(destinations, [(0, "v0.40.0", ""), listed], [])
        self.assertEqual(code, 1)
        self.assertEqual(calls, 2)
        self.assertIn("✗ 配置有误：缺少 account", out)
        self.assertEqual(out.count("✓ gogcli 账号授权"), 2)

        del destinations["broken"]
        code, _, _ = self._run(destinations, [(0, "v0.40.0", ""), listed], [])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
