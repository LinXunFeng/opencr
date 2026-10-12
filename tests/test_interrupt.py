"""确认中断：只关闭 Stale 运行的记录，迟到的收尾与发布不能改写它。"""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from tests.test_storage import StorageTestCase

STALE_AFTER = 600


class InterruptTestCase(StorageTestCase):
    """建库、建巡检与后台蓝图，提供让运行失联的辅助方法。"""

    def setUp(self):
        """在独立数据库上注册后台蓝图，并建一个巡检供 SurveyRun 用例使用。"""
        super().setUp()
        from flask import Flask

        from backend.admin import auth, routes

        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        self.app.register_blueprint(routes.admin_bp)
        self.app.config["START_REVIEW_THREAD"] = mock.Mock()
        self.client = self.app.test_client()
        admin_config = mock.patch.object(
            auth, "load_admin_config", return_value={"enabled": True, "bind_local_only": False}
        )
        admin_config.start()
        self.addCleanup(admin_config.stop)
        storage_config = mock.patch.object(
            routes, "load_storage_config", return_value={"stale_after_seconds": STALE_AFTER}
        )
        storage_config.start()
        self.addCleanup(storage_config.stop)
        self.survey = self.repo.create_survey(
            name="周巡检", slug="weekly",
            schedule_kind="weekly", schedule_expr="1 09:00", timezone_name="Asia/Shanghai",
            sources=[{"kind": "repo", "url": "https://g.com/a/app.git", "branch": "main"}],
            next_run_at=datetime(2026, 9, 14, 1, 0),
        )

    def go_silent(self, model, run_uid, seconds=STALE_AFTER + 60):
        """把运行的心跳拨回过去，模拟进程失联。"""
        from backend.storage import db
        from backend.storage.models import utcnow

        with db.session_scope() as session:
            run = session.query(model).filter_by(run_uid=run_uid).one()
            run.heartbeat_at = utcnow() - timedelta(seconds=seconds)
            return run.heartbeat_at

    def stale_review_run(self, phase="reviewing"):
        """建一个停在某阶段、心跳已超时的 ReviewRun。"""
        from backend.storage.models import ReviewRun

        uid = self.repo.start_run(1, 1, "webhook_update", "file")
        self.repo.update_progress(uid, phase=phase)
        self.go_silent(ReviewRun, uid)
        return uid

    def stale_survey_run(self):
        """建一个心跳已超时的 SurveyRun。"""
        from backend.storage.models import SurveyRun

        uid = self.repo.start_survey_run(self.survey["survey_uid"], "schedule")
        self.go_silent(SurveyRun, uid)
        return uid

    def login(self):
        """把测试客户端设为管理员身份。"""
        with self.client.session_transaction() as session:
            session["identity"] = "admin"


class ConfirmInterruptedTests(InterruptTestCase):
    """repo 层的判定与改写。"""

    def test_stale_review_run_becomes_failed_and_keeps_its_clues(self):
        """记为失败与已确认中断；阶段与最后心跳是排查线索，原样保留。"""
        from backend.storage.models import ReviewRun

        uid = self.repo.start_run(1, 1, "webhook_update", "file")
        self.repo.update_progress(uid, phase="reviewing")
        heartbeat = self.go_silent(ReviewRun, uid)

        self.assertEqual(self.repo.confirm_run_interrupted(uid, STALE_AFTER), "ok")
        detail = self.repo.get_run_detail(uid)
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["error_kind"], "interrupted")
        self.assertIn("没有停止", detail["error_message"])
        self.assertEqual(detail["phase"], "reviewing")
        self.assertEqual(detail["heartbeat_at"], heartbeat.isoformat())
        self.assertTrue(detail["finished_at"])
        self.assertFalse(detail["is_stale"])

    def test_only_stale_runs_can_be_confirmed(self):
        """心跳正常的、已结束的、不存在的都拒绝；同一条不能确认两次。"""
        fresh = self.repo.start_run(1, 1, "manual", "file")
        self.assertEqual(self.repo.confirm_run_interrupted(fresh, STALE_AFTER), "not_stale")
        self.assertEqual(self.repo.get_run_detail(fresh)["status"], "running")

        done = self.repo.start_run(1, 2, "manual", "file")
        self.repo.finish_run(done, "succeeded")
        self.assertEqual(self.repo.confirm_run_interrupted(done, STALE_AFTER), "not_stale")
        self.assertEqual(self.repo.get_run_detail(done)["status"], "succeeded")

        self.assertEqual(self.repo.confirm_run_interrupted("missing", STALE_AFTER), "not_found")

        stale = self.stale_review_run()
        self.assertEqual(self.repo.confirm_run_interrupted(stale, STALE_AFTER), "ok")
        self.assertEqual(self.repo.confirm_run_interrupted(stale, STALE_AFTER), "not_stale")

    def test_late_writes_cannot_reopen_a_confirmed_run(self):
        """原运行其实还活着：迟到的收尾返回 False，进度与心跳也不再改写记录。"""
        uid = self.stale_review_run()
        self.repo.confirm_run_interrupted(uid, STALE_AFTER)
        before = self.repo.get_run_detail(uid)

        self.repo.heartbeat(uid)
        self.repo.update_progress(uid, phase="publishing", files_done=3)
        self.assertFalse(self.repo.finish_run(uid, "succeeded"))
        self.assertFalse(self.repo.finish_run(uid, "failed", "review_error", "late"))

        after = self.repo.get_run_detail(uid)
        for key in ("status", "error_kind", "error_message", "phase", "files_done", "heartbeat_at", "finished_at"):
            self.assertEqual(after[key], before[key], key)

    def test_degradation_is_kept_but_does_not_revive_a_confirmed_run(self):
        """迟到的降级照记，但不能续上心跳；取证清单不再写入已关闭的巡检。"""
        uid = self.stale_review_run()
        self.repo.confirm_run_interrupted(uid, STALE_AFTER)
        heartbeat = self.repo.get_run_detail(uid)["heartbeat_at"]
        self.repo.add_degradation(uid, "diff_truncated")
        detail = self.repo.get_run_detail(uid)
        self.assertEqual(detail["heartbeat_at"], heartbeat)
        self.assertEqual(detail["degradations"], [{"kind": "diff_truncated", "count": 1}])

        survey_uid = self.stale_survey_run()
        self.repo.confirm_survey_run_interrupted(survey_uid, STALE_AFTER)
        heartbeat = self.repo.get_survey_run_detail(survey_uid)["heartbeat_at"]
        self.repo.add_survey_degradation(survey_uid, "repo_fetch_failed")
        self.repo.record_survey_inspected_files(
            survey_uid, [{"repo_slug": "app", "file_path": "a.py", "conclusive": True}]
        )
        self.assertEqual(self.repo.get_survey_run_detail(survey_uid)["heartbeat_at"], heartbeat)
        from backend.storage import db
        from backend.storage.models import SurveyRun
        with db.session_scope() as session:
            self.assertEqual(session.query(SurveyRun).filter_by(run_uid=survey_uid).one().inspected_files, "[]")

    def test_normal_finish_still_reports_success(self):
        """收尾保护不影响正常路径。"""
        uid = self.repo.start_run(1, 1, "manual", "file")
        self.assertTrue(self.repo.finish_run(uid, "succeeded"))
        self.assertEqual(self.repo.get_run_detail(uid)["status"], "succeeded")

    def test_confirmed_survey_run_unblocks_the_survey(self):
        """Stale 的 SurveyRun 挡住同一 Survey 的执行，确认中断后放行，迟到的收尾写不进来。"""
        uid = self.stale_survey_run()
        self.assertTrue(self.repo.has_running_survey_run(self.survey["survey_uid"]))

        self.assertEqual(self.repo.confirm_survey_run_interrupted(uid, STALE_AFTER), "ok")
        self.assertFalse(self.repo.has_running_survey_run(self.survey["survey_uid"]))
        self.repo.survey_heartbeat(uid)
        self.repo.update_survey_progress(uid, phase="summarizing")
        self.assertFalse(self.repo.finish_survey_run(uid, "succeeded", summary="late"))

        detail = self.repo.get_survey_run_detail(uid)
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["error_kind"], "interrupted")
        self.assertEqual(detail["phase"], "fetching")
        self.assertFalse(detail["summary"])

    def test_fresh_survey_run_is_not_confirmed(self):
        """心跳正常的巡检不能被关掉。"""
        uid = self.repo.start_survey_run(self.survey["survey_uid"], "manual")
        self.assertEqual(self.repo.confirm_survey_run_interrupted(uid, STALE_AFTER), "not_stale")
        self.assertEqual(self.repo.confirm_survey_run_interrupted("missing", STALE_AFTER), "not_found")


class InterruptRouteTests(InterruptTestCase):
    """后台接口的权限与响应。"""

    def test_review_run_interrupt_is_admin_only(self):
        """游客即使开了重新触发也不能确认中断。"""
        uid = self.stale_review_run()
        self.repo.set_setting("guest_read", "1")
        self.repo.set_setting("guest_retry", "1")
        self.assertIn(self.client.post(f"/api/admin/runs/{uid}/interrupt").status_code, (401, 403))
        self.assertEqual(self.repo.get_run_detail(uid)["status"], "running")

        self.login()
        response = self.client.post(f"/api/admin/runs/{uid}/interrupt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "failed")

    def test_review_run_interrupt_rejects_non_stale_and_missing(self):
        """心跳正常返回 409，不存在返回 404。"""
        self.login()
        fresh = self.repo.start_run(1, 1, "manual", "file")
        self.assertEqual(self.client.post(f"/api/admin/runs/{fresh}/interrupt").status_code, 409)
        self.assertEqual(self.client.post("/api/admin/runs/missing/interrupt").status_code, 404)

    def test_confirmed_review_run_can_be_retried(self):
        """确认中断后原来被挡住的重新触发可以进行。"""
        self.login()
        uid = self.stale_review_run()
        with mock.patch("backend.review.gitlab.get_mr_state", return_value={"state": "opened"}):
            blocked = self.client.post(f"/api/admin/runs/{uid}/retry", json={"scope": "latest"})
            self.assertEqual(blocked.status_code, 409)
            self.client.post(f"/api/admin/runs/{uid}/interrupt")
            retried = self.client.post(f"/api/admin/runs/{uid}/retry", json={"scope": "latest"})
        self.assertEqual(retried.status_code, 202)

    def test_survey_run_interrupt(self):
        """巡检同样只对 Admin 开放，并区分 409 与 404。"""
        uid = self.stale_survey_run()
        self.assertIn(self.client.post(f"/api/admin/survey-runs/{uid}/interrupt").status_code, (401, 403))
        self.login()
        self.assertEqual(self.client.post(f"/api/admin/survey-runs/{uid}/interrupt").status_code, 200)
        self.assertEqual(self.client.post(f"/api/admin/survey-runs/{uid}/interrupt").status_code, 409)
        self.assertEqual(self.client.post("/api/admin/survey-runs/missing/interrupt").status_code, 404)


class LateRunnerTests(InterruptTestCase):
    """被确认中断后原运行才结束：收尾之后的对外动作必须跳过。"""

    def test_late_review_failure_does_not_comment_on_the_mr(self):
        """迟到的审查失败不再往 MR 发「审查失败」评论。"""
        from backend.review import runner
        from backend.review.ai import ReviewError
        from backend.storage.models import ReviewRun

        uid = self.repo.start_run(1, 1, "webhook_update", "file")

        def _hang_then_fail(*_args, **_kwargs):
            """卡住期间被管理员确认中断，之后才报错。"""
            self.go_silent(ReviewRun, uid)
            self.repo.confirm_run_interrupted(uid, STALE_AFTER)
            raise ReviewError("model timeout")

        with mock.patch.object(runner, "load_review_config", return_value={}), \
                mock.patch.object(runner, "_resolve_changes", side_effect=_hang_then_fail), \
                mock.patch.object(runner, "post_mr_comment") as post:
            runner.execute_review_run(uid, 1, 1, "t", "file", "")
        post.assert_not_called()
        self.assertEqual(self.repo.get_run_detail(uid)["error_kind"], "interrupted")

    def test_late_survey_success_is_not_published(self):
        """迟到的巡检成功不更新台账、不推送，记录保持已确认中断。"""
        from backend.storage.models import SurveyRun
        from backend.survey import runner

        uid_box = {}
        # runner.repo 与 self.repo 是同一个模块，打补丁前先拿住原函数，否则会调回补丁自身
        original_start = self.repo.start_survey_run

        def _start(survey_uid, trigger):
            """登记运行并记下 run_uid，供汇总阶段确认中断。"""
            uid_box["uid"] = original_start(survey_uid, trigger)
            return uid_box["uid"]

        def _summarize(*_args, **_kwargs):
            """汇总阶段卡住期间被确认中断，之后才返回结论。"""
            self.go_silent(SurveyRun, uid_box["uid"])
            self.repo.confirm_survey_run_interrupted(uid_box["uid"], STALE_AFTER)
            return "late summary"

        target = {"slug": "app", "url": "https://g.com/a/app.git", "branch": "main"}
        patches = {
            "start_survey_run": mock.patch.object(runner.repo, "start_survey_run", side_effect=_start),
            "load_survey_config": mock.patch.object(runner, "load_survey_config", return_value={}),
            "resolve_budget": mock.patch.object(runner, "resolve_budget", return_value={
                "wall_clock_minutes": 10, "l1_max_chars": 1000, "l2_max_focus": 1, "l2_max_chars_per_focus": 1000}),
            "load_max_repos": mock.patch.object(runner, "load_max_repos", return_value=10),
            "resolve_sources": mock.patch.object(runner, "resolve_sources", return_value=SimpleNamespace(
                targets=[target], ignored=[], truncated=[])),
            "_prepare_workspaces": mock.patch.object(runner, "_prepare_workspaces", return_value=[
                {"slug": "app", "profile": {}}]),
            "build_cross_repo_map": mock.patch.object(runner, "build_cross_repo_map", return_value={"links": []}),
            "_record_reach": mock.patch.object(runner, "_record_reach"),
            "_record_codegraph_stats": mock.patch.object(runner, "_record_codegraph_stats"),
            "_candidate_skills": mock.patch.object(runner, "_candidate_skills", return_value=[]),
            "match_skills": mock.patch.object(runner, "match_skills", return_value=[]),
            "load_skill_prompt": mock.patch.object(runner, "load_skill_prompt", return_value=""),
            "plan_focus": mock.patch.object(runner, "plan_focus", return_value=[]),
            "plan_rechecks": mock.patch.object(runner, "plan_rechecks", return_value=([], [])),
            "merge_focuses": mock.patch.object(runner, "merge_focuses", return_value=[]),
            "annotate_focuses": mock.patch.object(runner, "annotate_focuses"),
            "_collect_findings": mock.patch.object(runner, "_collect_findings", return_value=([], [])),
            "summarize": mock.patch.object(runner, "summarize", side_effect=_summarize),
            "_cleanup": mock.patch.object(runner, "_cleanup"),
            "_publish": mock.patch.object(runner, "_publish"),
        }
        mocks = {name: p.start() for name, p in patches.items()}
        self.addCleanup(mock.patch.stopall)

        runner.execute_survey_run(self.survey["survey_uid"], "schedule")

        mocks["_publish"].assert_not_called()
        # 确认之后可能已有新一轮在用工作区，迟到的旧运行不能删它
        mocks["_cleanup"].assert_called_once_with(mock.ANY, delete_allowed=False)
        detail = self.repo.get_survey_run_detail(uid_box["uid"])
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["error_kind"], "interrupted")
        self.assertFalse(detail["summary"])

    def test_cleanup_keeps_workspace_for_a_confirmed_run(self):
        """配置了用完即删时，已被确认中断的运行也不删工作区；正常收尾照删。"""
        from backend.survey import runner

        survey = {**self.survey, "delete_workspace_after": True}
        with mock.patch.object(runner, "delete_workspace") as delete:
            runner._cleanup(survey, delete_allowed=False)
            delete.assert_not_called()
            runner._cleanup(survey)
            delete.assert_called_once_with(survey["slug"])


if __name__ == "__main__":
    unittest.main()
