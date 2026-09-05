"""持久化层测试：ReviewRun 生命周期、Finding 记账、统计口径与保留期清理。"""

import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="opencr-db-")
        os.environ["OPENCR_DATABASE_URL"] = f"sqlite:///{Path(self.tmpdir, 'test.db')}"

        from src.storage import db

        db.reset_engine_for_tests(os.environ["OPENCR_DATABASE_URL"])

        from src.storage.models import Base

        Base.metadata.create_all(db.get_engine())

        from src.storage import repo

        self.repo = repo

    def tearDown(self):
        from src.storage import db

        db.reset_engine_for_tests()
        os.environ.pop("OPENCR_DATABASE_URL", None)


class ReviewRunLifecycleTests(StorageTestCase):
    def test_run_progresses_and_finishes(self):
        from src.storage.models import PHASE_REVIEWING, RUN_SUCCEEDED

        run_uid = self.repo.start_run(
            project_id=7, mr_iid=42, trigger="webhook_open", review_mode="hybrid",
            mr_title="add feature", project_path="group/app",
        )
        self.repo.update_progress(run_uid, phase=PHASE_REVIEWING, files_total=5, files_done=2,
                                  review_skills=["flutter", "general"])
        self.repo.finish_run(run_uid, RUN_SUCCEEDED)

        detail = self.repo.get_run_detail(run_uid)
        self.assertEqual(detail["status"], RUN_SUCCEEDED)
        self.assertEqual(detail["files_done"], 2)
        self.assertEqual(detail["files_total"], 5)
        self.assertEqual(detail["review_skills"], ["flutter", "general"])
        self.assertFalse(detail["is_stale"])

    def test_degradations_accumulate_without_changing_status(self):
        """降级与失败正交：一次 run 可以既成功又带多条降级。"""
        from src.storage.models import DEGRADE_DIFF_TRUNCATED, DEGRADE_INLINE_POST_FAILED, RUN_SUCCEEDED

        run_uid = self.repo.start_run(project_id=1, mr_iid=1, trigger="manual", review_mode="file")
        self.repo.add_degradation(run_uid, DEGRADE_INLINE_POST_FAILED, 2)
        self.repo.add_degradation(run_uid, DEGRADE_INLINE_POST_FAILED, 1)
        self.repo.add_degradation(run_uid, DEGRADE_DIFF_TRUNCATED)
        self.repo.finish_run(run_uid, RUN_SUCCEEDED)

        detail = self.repo.get_run_detail(run_uid)
        self.assertEqual(detail["status"], RUN_SUCCEEDED)
        by_kind = {d["kind"]: d["count"] for d in detail["degradations"]}
        self.assertEqual(by_kind[DEGRADE_INLINE_POST_FAILED], 3)
        self.assertEqual(by_kind[DEGRADE_DIFF_TRUNCATED], 1)

    def test_stale_is_inferred_from_heartbeat_not_rewritten(self):
        """
        心跳超时只标注、不改写状态：多进程下本进程无法断言别的进程的 run 已死。
        """
        from src.storage import db
        from src.storage.models import ReviewRun, RUN_RUNNING, utcnow

        run_uid = self.repo.start_run(project_id=1, mr_iid=1, trigger="manual", review_mode="file")
        with db.session_scope() as session:
            run = session.query(ReviewRun).filter_by(run_uid=run_uid).one()
            run.heartbeat_at = utcnow() - timedelta(seconds=1200)

        active = self.repo.list_active_runs(stale_after_seconds=600)
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]["is_stale"])
        # 状态本身没有被改写
        self.assertEqual(active[0]["status"], RUN_RUNNING)

    def test_skipped_run_keeps_reason(self):
        from src.storage.models import RUN_SKIPPED

        run_uid = self.repo.record_skipped_run(
            project_id=3, mr_iid=9, trigger="webhook_update", skip_reason="标题包含跳过标记: wip",
        )
        detail = self.repo.get_run_detail(run_uid)
        self.assertEqual(detail["status"], RUN_SKIPPED)
        self.assertIn("wip", detail["skip_reason"])


class FindingAccountingTests(StorageTestCase):
    def _run(self):
        return self.repo.start_run(project_id=5, mr_iid=11, trigger="webhook_open", review_mode="hybrid")

    def test_trackable_finding_starts_undecided(self):
        from src.storage.models import DELIVERY_INLINE, VERDICT_UNDECIDED

        run_uid = self._run()
        self.repo.record_finding(run_uid, "a.py", 10, "问题", DELIVERY_INLINE,
                                 severity="critical", discussion_id="d1", note_id=99)
        pending = self.repo.list_undecided_findings(5, 11)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["discussion_id"], "d1")
        self.assertEqual(pending[0]["note_id"], 99)
        self.assertEqual(self.repo.get_run_detail(run_uid)["findings"][0]["verdict"], VERDICT_UNDECIDED)

    def test_summary_and_fallback_findings_are_untrackable(self):
        """
        整体评论与降级评论走的是不可 resolve 的普通 note，永远无法结算，
        但仍然入库 —— 它们要计入 Coverage 的分母。
        """
        from src.storage.models import DELIVERY_FALLBACK_NOTE, DELIVERY_SUMMARY_ONLY, VERDICT_UNTRACKABLE

        run_uid = self._run()
        self.repo.record_finding(run_uid, "b.py", 0, "整体评论问题", DELIVERY_SUMMARY_ONLY)
        self.repo.record_finding(run_uid, "c.py", 3, "降级问题", DELIVERY_FALLBACK_NOTE)

        verdicts = [f["verdict"] for f in self.repo.get_run_detail(run_uid)["findings"]]
        self.assertEqual(verdicts, [VERDICT_UNTRACKABLE, VERDICT_UNTRACKABLE])
        # 不可追踪的不会进入待结算队列
        self.assertEqual(self.repo.list_undecided_findings(5, 11), [])

    def test_inline_finding_without_discussion_id_is_untrackable(self):
        """拿不到 discussion_id 就没有身份，即使投递方式是 inline 也无法结算。"""
        from src.storage.models import DELIVERY_INLINE, VERDICT_UNTRACKABLE

        run_uid = self._run()
        self.repo.record_finding(run_uid, "d.py", 5, "问题", DELIVERY_INLINE, discussion_id="")
        self.assertEqual(self.repo.get_run_detail(run_uid)["findings"][0]["verdict"], VERDICT_UNTRACKABLE)


class StatsTests(StorageTestCase):
    def test_acceptance_denominator_excludes_untrackable_and_undecided(self):
        from src.storage.models import (
            DELIVERY_INLINE,
            DELIVERY_SUMMARY_ONLY,
            VERDICT_ACCEPTED,
            VERDICT_IGNORED,
            VERDICT_REJECTED,
        )

        run_uid = self.repo.start_run(project_id=5, mr_iid=11, trigger="manual", review_mode="hybrid")
        ids = []
        for i in range(4):
            self.repo.record_finding(run_uid, "a.py", i + 1, "x", DELIVERY_INLINE, discussion_id=f"d{i}")
        # 2 条整体评论产出：不可追踪
        self.repo.record_finding(run_uid, "b.py", 0, "y", DELIVERY_SUMMARY_ONLY)
        self.repo.record_finding(run_uid, "b.py", 0, "z", DELIVERY_SUMMARY_ONLY)

        pending = self.repo.list_undecided_findings(5, 11)
        ids = [p["id"] for p in pending]
        self.repo.apply_verdicts({
            ids[0]: (VERDICT_ACCEPTED, "resolved"),
            ids[1]: (VERDICT_ACCEPTED, "thumbs_up"),
            ids[2]: (VERDICT_REJECTED, "thumbs_down"),
            # ids[3] 留作 undecided：MR 还开着
        })
        del VERDICT_IGNORED

        stats = self.repo.verdict_stats(days=30)
        self.assertEqual(stats["total_findings"], 6)
        self.assertEqual(stats["trackable_findings"], 4)
        self.assertEqual(stats["settled_findings"], 3)
        self.assertEqual(stats["accepted"], 2)
        # 采纳率分母是已结算的 3 条，不是全部 6 条
        self.assertAlmostEqual(stats["acceptance_rate"], round(2 / 3, 4))
        # 覆盖率 = 可追踪 4 / 全部 6
        self.assertAlmostEqual(stats["coverage_rate"], round(4 / 6, 4))
        self.assertEqual(stats["summary_only_findings"], 2)

    def test_rates_are_none_when_no_data(self):
        """没有样本时返回 None 而不是 0 —— 0% 会被误读成"一条都没被采纳"。"""
        stats = self.repo.verdict_stats(days=30)
        self.assertIsNone(stats["acceptance_rate"])
        self.assertIsNone(stats["coverage_rate"])

    def test_error_stats_separates_failed_degraded_skipped(self):
        from src.storage.models import (
            DEGRADE_INLINE_POST_FAILED,
            ERROR_REVIEW,
            RUN_FAILED,
            RUN_SUCCEEDED,
        )

        ok = self.repo.start_run(project_id=1, mr_iid=1, trigger="manual", review_mode="file")
        self.repo.finish_run(ok, RUN_SUCCEEDED)

        degraded = self.repo.start_run(project_id=1, mr_iid=2, trigger="manual", review_mode="file")
        self.repo.add_degradation(degraded, DEGRADE_INLINE_POST_FAILED, 3)
        self.repo.finish_run(degraded, RUN_SUCCEEDED)

        bad = self.repo.start_run(project_id=1, mr_iid=3, trigger="manual", review_mode="file")
        self.repo.finish_run(bad, RUN_FAILED, ERROR_REVIEW, "boom")

        self.repo.record_skipped_run(project_id=1, mr_iid=4, trigger="webhook_update", skip_reason="wip")

        stats = self.repo.error_stats(days=7)
        self.assertEqual(stats["total_runs"], 4)
        self.assertEqual(stats["succeeded"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["skipped"], 1)
        # 带降级的那次仍然算成功，只是额外计入 degraded_runs
        self.assertEqual(stats["degraded_runs"], 1)
        self.assertEqual(stats["by_degradation"][DEGRADE_INLINE_POST_FAILED], 3)
        self.assertEqual(stats["by_error_kind"][ERROR_REVIEW], 1)


class MaintenanceTests(StorageTestCase):
    def test_purge_removes_old_runs_and_their_findings(self):
        from src.storage import db
        from src.storage.models import DELIVERY_INLINE, Finding, ReviewRun, utcnow

        old = self.repo.start_run(project_id=1, mr_iid=1, trigger="manual", review_mode="file")
        self.repo.record_finding(old, "a.py", 1, "x", DELIVERY_INLINE, discussion_id="d1")
        fresh = self.repo.start_run(project_id=1, mr_iid=2, trigger="manual", review_mode="file")

        with db.session_scope() as session:
            run = session.query(ReviewRun).filter_by(run_uid=old).one()
            run.started_at = utcnow() - timedelta(days=120)

        purged = self.repo.purge_old_runs(retention_days=90)
        self.assertEqual(purged, 1)
        self.assertIsNone(self.repo.get_run_detail(old))
        self.assertIsNotNone(self.repo.get_run_detail(fresh))
        with db.session_scope() as session:
            self.assertEqual(session.query(Finding).count(), 0)

    def test_lease_is_exclusive_until_it_expires(self):
        self.assertTrue(self.repo.acquire_lease("reconciler", "pid-1", ttl_seconds=60))
        # 别的进程抢不到
        self.assertFalse(self.repo.acquire_lease("reconciler", "pid-2", ttl_seconds=60))
        # 持有者可以续租
        self.assertTrue(self.repo.acquire_lease("reconciler", "pid-1", ttl_seconds=60))

        # 过期后其他进程可以接管
        self.assertTrue(self.repo.acquire_lease("reconciler", "pid-1", ttl_seconds=1))
        from src.storage import db
        from src.storage.models import LeaderLease, utcnow

        with db.session_scope() as session:
            lease = session.get(LeaderLease, "reconciler")
            lease.expires_at = utcnow() - timedelta(seconds=1)
        self.assertTrue(self.repo.acquire_lease("reconciler", "pid-2", ttl_seconds=60))

    def test_pending_settlement_skips_already_settled_mrs(self):
        from src.storage.models import DELIVERY_INLINE

        run_uid = self.repo.start_run(project_id=9, mr_iid=1, trigger="manual", review_mode="file")
        self.repo.record_finding(run_uid, "a.py", 1, "x", DELIVERY_INLINE, discussion_id="d1")
        other = self.repo.start_run(project_id=9, mr_iid=2, trigger="manual", review_mode="file")
        self.repo.record_finding(other, "b.py", 1, "y", DELIVERY_INLINE, discussion_id="d2")

        self.assertEqual(len(self.repo.list_mrs_pending_settlement()), 2)
        self.repo.mark_mr_settled(9, 1, "merged")
        pending = self.repo.list_mrs_pending_settlement()
        self.assertEqual(pending, [{"project_id": 9, "mr_iid": 2}])


if __name__ == "__main__":
    unittest.main()
