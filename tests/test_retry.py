"""失败运行重试的权限、范围与跨进程防重测试。"""

import multiprocessing
import unittest
from datetime import timedelta
from unittest import mock

from tests.test_storage import StorageTestCase


INPUT = {"review_skill": "original-choice", "from_sha": "A", "to_sha": "B",
         "diff_refs": {"base_sha": "A", "start_sha": "A", "head_sha": "B"}}


def _retry_worker(database_url, source, barrier, results):
    """独立进程在同一时刻请求重试，返回成功标识或拒绝状态。"""
    from backend.storage import db, repo
    db.reset_engine_for_tests(database_url)
    barrier.wait(timeout=10)
    try:
        results.put(repo.start_retry_run(source, "original", "overall", "")["run_uid"])
    except repo.RetryRejected:
        results.put("rejected")
    finally:
        db.reset_engine_for_tests()


class RetryTests(StorageTestCase):
    """覆盖重试的持久化、执行和 HTTP 行为。"""

    def setUp(self):
        """建立独立数据库与后台蓝图，禁止实际发起审查。"""
        super().setUp()
        from flask import Flask
        from backend.admin import auth, routes
        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        self.app.register_blueprint(routes.admin_bp)
        self.start = mock.Mock()
        self.app.config["START_REVIEW_THREAD"] = self.start
        self.client = self.app.test_client()
        self.admin_config = mock.patch.object(auth, "load_admin_config", return_value={
            "enabled": True, "bind_local_only": False})
        self.admin_config.start()
        self.addCleanup(self.admin_config.stop)
        self.state = mock.patch("backend.review.gitlab.get_mr_state", return_value={"state": "opened"})
        self.state_mock = self.state.start()
        self.addCleanup(self.state.stop)

    def source(self, snapshot=True, status="failed", mr_iid=1):
        """建立可选范围快照的来源运行。"""
        uid = self.repo.start_run(1, mr_iid, "webhook_update", "file")
        if snapshot:
            self.repo.save_review_input(uid, INPUT)
        self.repo.finish_run(uid, status)
        return uid

    def admin(self):
        """将测试客户端设为管理员身份。"""
        with self.client.session_transaction() as session:
            session["identity"] = "admin"

    def post(self, uid, scope="original"):
        """调用后台重试接口。"""
        return self.client.post(f"/api/admin/runs/{uid}/retry", json={"scope": scope})

    def test_guest_requires_both_switches_and_admin_does_not(self):
        """游客须双开关放行，管理员不受游客开关限制。"""
        uid = self.source()
        self.assertEqual(self.post(uid).status_code, 403)
        self.repo.set_setting("guest_retry", "1")
        self.repo.set_setting("guest_read", "0")
        self.assertEqual(self.post(uid).status_code, 401)
        self.repo.set_setting("guest_read", "1")
        response = self.post(uid)
        self.assertEqual(response.status_code, 202)
        self.repo.finish_run(response.json["run_uid"], "failed")
        self.repo.set_setting("guest_retry", "0")
        self.repo.set_setting("guest_read", "0")
        self.admin()
        self.assertEqual(self.post(uid).status_code, 202)

    def test_settings_default_off_and_reject_non_boolean(self):
        """游客不能改权限，布尔开关不接受字符串的隐式真值。"""
        from backend.admin.auth import guest_retry_enabled
        self.assertFalse(guest_retry_enabled())
        self.assertEqual(self.client.patch("/api/admin/settings", json={"guest_retry": True}).status_code, 401)
        self.admin()
        response = self.client.patch("/api/admin/settings", json={"guest_retry": True})
        self.assertTrue(response.json["writable"]["guest_retry"])
        self.assertEqual(self.client.patch("/api/admin/settings", json={"guest_retry": "false"}).status_code, 400)
        self.assertTrue(guest_retry_enabled())

    def test_original_preserves_inputs_and_source(self):
        """原范围保存关联与模式参数，历史记录不被重置。"""
        self.admin()
        uid = self.source()
        response = self.post(uid)
        self.assertEqual(response.status_code, 202)
        params = self.start.call_args.kwargs
        self.assertEqual(params["original_input"], INPUT)
        self.assertEqual(params["review_mode"], "file")
        self.assertEqual(params["review_skill"], "original-choice")
        detail = self.repo.get_run_detail(response.json["run_uid"])
        self.assertEqual(detail["retry_of_uid"], uid)
        self.assertEqual(detail["retry_scope"], "original")
        self.assertEqual(self.repo.get_run_detail(uid)["status"], "failed")

    def test_legacy_only_allows_latest_and_uses_current_defaults(self):
        """旧记录拒绝原范围，最新全量使用当前默认参数。"""
        self.admin()
        uid = self.source(snapshot=False)
        self.assertFalse(self.client.get(f"/api/admin/runs/{uid}").json["original_retry_available"])
        self.assertEqual(self.post(uid).status_code, 409)
        self.assertEqual(self.post(uid, "latest").status_code, 202)
        self.assertIsNone(self.start.call_args.kwargs["original_input"])
        self.assertEqual(self.start.call_args.kwargs["review_mode"], "overall")
        self.assertEqual(self.start.call_args.kwargs["review_skill"], "")

    def test_status_and_payload_guards(self):
        """非失败来源、无效范围、额外参数和不存在记录不能启动。"""
        self.admin()
        for status in ("running", "succeeded", "skipped"):
            uid = self.source(status=status)
            self.assertEqual(self.post(uid).status_code, 409)
        self.assertEqual(self.post("missing").status_code, 404)
        for payload in (None, [], {"scope": []}, {"scope": "bad"}, {"scope": "latest", "project_id": 999}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/admin/runs/missing/retry", json=payload).status_code, 400)
        self.start.assert_not_called()

    def test_closed_merged_and_unavailable_mr_do_not_start(self):
        """不能确认 MR 仍打开时拒绝启动。"""
        self.admin()
        uid = self.source()
        for state in ("closed", "merged", ""):
            self.state_mock.return_value = {"state": state}
            self.assertEqual(self.post(uid).status_code, 409)
        self.state_mock.side_effect = RuntimeError("不可用")
        self.assertEqual(self.post(uid).status_code, 502)
        self.start.assert_not_called()
        self.assertEqual(len(self.repo.list_recent_runs()), 1)

    def test_stale_blocks_retry_with_active_link(self):
        """疑似中断不等于终止，拒绝时给出阻塞运行。"""
        from backend.storage import db
        from backend.storage.models import ReviewRun, utcnow
        self.admin()
        uid = self.source()
        active = self.repo.start_run(1, 1, "manual", "file")
        with db.session_scope() as session:
            session.query(ReviewRun).filter_by(run_uid=active).one().heartbeat_at = utcnow() - timedelta(hours=1)
        response = self.post(uid)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["active_run_uid"], active)
        self.start.assert_not_called()

    def test_thread_start_failure_is_recorded(self):
        """线程启动失败不能留下永久 running 的记录。"""
        self.admin()
        uid = self.source()
        self.start.side_effect = RuntimeError("无法启动")
        response = self.post(uid)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.repo.get_run_detail(response.json["run_uid"])["status"], "failed")

    def test_two_processes_create_only_one_retry(self):
        """真实独立进程同时插入，SQLite 事务只允许一个成功。"""
        import os
        uid = self.source()
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = [context.Process(target=_retry_worker, args=(os.environ["OPENCR_DATABASE_URL"], uid, barrier, results)) for _ in range(2)]
        for worker in workers:
            worker.start()
        try:
            answers = [results.get(timeout=20) for _ in workers]
            self.assertEqual(answers.count("rejected"), 1)
            self.assertEqual(len(self.repo.list_active_runs()), 1)
        finally:
            for worker in workers:
                worker.join(timeout=5)
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
            results.close()

    def test_guest_detail_has_capability_but_no_private_snapshot(self):
        """重试能力不扩大游客可见的正文与原始参数范围。"""
        uid = self.source()
        self.repo.record_finding(uid, "a.py", 1, "机密正文", "summary_only")
        response = self.client.get(f"/api/admin/runs/{uid}").json
        self.assertFalse(response["can_retry"])
        self.assertNotIn("review_input", response)
        self.assertNotIn("body", response["findings"][0])
        self.repo.set_setting("guest_retry", "1")
        self.assertTrue(self.client.get(f"/api/admin/runs/{uid}").json["can_retry"])

    def test_original_execution_never_fetches_latest_diff(self):
        """原范围始终取 A→B，即使当前 MR 已推进，也不读取最新 diff。"""
        from backend.review import runner
        uid = self.source()
        params = self.repo.start_retry_run(uid, "original", "overall", "")
        with mock.patch.object(runner, "get_compare_changes", return_value=[]) as compare, mock.patch.object(runner, "get_mr_changes_with_refs") as latest:
            runner.execute_review_run(**params)
        compare.assert_called_once_with(1, "A", "B")
        latest.assert_not_called()
        self.assertEqual(self.repo.get_run_detail(params["run_uid"])["status"], "succeeded")

    def test_original_unavailable_fails_without_switching_scope(self):
        """历史提交拉取失败只标记失败，不自动审查当前版本。"""
        from backend.review import runner
        from backend.review.common import ReviewError
        uid = self.source()
        params = self.repo.start_retry_run(uid, "original", "overall", "")
        with mock.patch.object(runner, "get_compare_changes", side_effect=ReviewError("原提交不可获取")), mock.patch.object(runner, "get_mr_changes_with_refs") as latest, mock.patch.object(runner, "post_mr_comment"):
            runner.execute_review_run(**params)
        latest.assert_not_called()
        self.assertEqual(self.repo.get_run_detail(params["run_uid"])["status"], "failed")

    def test_failed_model_preserves_resolved_range(self):
        """模型失败前保存提交区间，以便原范围重试。"""
        from backend.review import runner
        from backend.review.common import ReviewError
        uid = self.repo.start_run(1, 1, "manual", "overall")
        change = {"new_path": "a.py", "diff": "@@ -1 +1 @@\n-old\n+new"}
        with mock.patch.object(runner, "get_mr_changes_with_refs", return_value=([change], INPUT["diff_refs"])), mock.patch.object(runner, "enrich_changes_with_file_info", return_value=[change]), mock.patch.object(runner, "review_changes_with_inline_notes", side_effect=ReviewError("模型失败")), mock.patch.object(runner, "post_mr_comment"):
            runner.execute_review_run(uid, 1, 1, "", "overall", "selected")
        self.assertTrue(self.repo.get_run_detail(uid)["original_retry_available"])
        params = self.repo.start_retry_run(uid, "original", "file", "")
        self.assertEqual(params["original_input"]["from_sha"], "A")
        self.assertEqual(params["original_input"]["to_sha"], "B")
        self.assertEqual(params["review_skill"], "selected")

    def test_incremental_failure_keeps_event_interval_after_new_push(self):
        """新推送推进当前 MR 后，失败增量仍保留原 A→B 区间。"""
        from backend.review import runner
        from backend.review.common import ReviewError
        uid = self.repo.start_run(1, 1, "webhook_update", "file")
        current_refs = {"base_sha": "base", "start_sha": "base", "head_sha": "C"}
        with mock.patch.object(runner, "get_mr_changes_with_refs", return_value=([], current_refs)), mock.patch.object(runner, "get_compare_changes", side_effect=ReviewError("拉取失败")), mock.patch.object(runner, "post_mr_comment"):
            runner.execute_review_run(uid, 1, 1, "", "file", "selected", action="update", update_from_sha="A", update_to_sha="B")
        params = self.repo.start_retry_run(uid, "original", "overall", "")
        self.assertEqual(params["original_input"]["from_sha"], "A")
        self.assertEqual(params["original_input"]["to_sha"], "B")
        self.assertEqual(params["original_input"]["diff_refs"]["head_sha"], "B")

    def test_incomplete_inputs_disable_original_retry(self):
        """缺少技能参数或 diff refs 的快照不应被当作可恢复记录。"""
        uid = self.source()
        for value in ({"from_sha": "A", "to_sha": "B"}, {**INPUT, "diff_refs": {}}, {**INPUT, "review_skill": None}):
            self.repo.save_review_input(uid, value)
            self.assertFalse(self.repo.get_run_detail(uid)["original_retry_available"])
            with self.assertRaises(self.repo.RetryRejected):
                self.repo.start_retry_run(uid, "original", "overall", "")


if __name__ == "__main__":
    unittest.main()
