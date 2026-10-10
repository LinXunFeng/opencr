"""迁移 b7e4c1a9d2f0：指纹级忽略换算成问题级（ADR-0006）。

换算是一次性的、升级后无法再从数据里还原，因此按真实迁移脚本跑一遍，而不是用 create_all 建表。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class IgnorePerIssueMigrationTests(unittest.TestCase):
    """旧数据按"该指纹最近一次出现的运行里每个不同标题"换算，范围只会更窄。"""

    BEFORE = "d2a3378d6cb3"

    def setUp(self):
        """每个用例一份独立的 SQLite 文件。"""
        self.tmpdir = tempfile.mkdtemp(prefix="opencr-migrate-")
        self.db_path = Path(self.tmpdir, "test.db")
        os.environ["OPENCR_DATABASE_URL"] = f"sqlite:///{self.db_path}"

    def tearDown(self):
        """恢复环境变量并删掉临时库。"""
        os.environ.pop("OPENCR_DATABASE_URL", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _upgrade(self, target):
        """用部署时同一份 alembic 配置升级到指定版本。"""
        from alembic import command
        from alembic.config import Config

        from backend.storage.migrate import BACKEND_ROOT

        cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
        command.upgrade(cfg, target)

    def _seed(self, con):
        """按用户实际撞到的数据形态造数：同一指纹三轮共 5 条，外加一条找不到任何记录的旧忽略。"""
        con.execute(
            "INSERT INTO survey (id, survey_uid, name, slug, enabled, schedule_kind, schedule_expr, timezone,"
            " excluded_skills, delete_workspace_after, retention_runs, created_at, updated_at)"
            " VALUES (1, 'u', 'n', 's', 1, 'weekly', '1 09:00', 'UTC', '[]', 0, 20, '2026-01-01', '2026-01-01')"
        )
        for run_id in (2, 3, 4):
            con.execute(
                "INSERT INTO survey_run (id, run_uid, survey_id, trigger, status, started_at, heartbeat_at,"
                " degradations, matched_skills, repos_total, repos_done)"
                f" VALUES ({run_id}, 'r{run_id}', 1, 'manual', 'succeeded', '2026-01-01', '2026-01-01', '[]', '[]', 0, 0)"
            )
        rows = [(2, 53, "强制解包空返回值"), (3, 48, "强制解包空返回值"), (3, 179, "日志级别未返回"),
                (4, 47, "强制解包空返回值"), (4, 225, "可选参数被强制解包"), (4, 230, " 可选参数被强制解包"),
                (4, 300, None)]
        for finding_id, (run_id, line, title) in enumerate(rows, start=1):
            con.execute(
                "INSERT INTO survey_finding (id, run_id, survey_id, repo_slug, file_path, line, category, severity,"
                " title, body, fingerprint, state, created_at)"
                " VALUES (?, ?, 1, 'app', 'a.dart', ?, 'correctness', 'warning', ?, 'b', 'fp', 'new', '2026-01-01')",
                (finding_id, run_id, line, title),
            )
        con.execute(
            "INSERT INTO survey_ledger (survey_id, fingerprint, repo_slug, file_path, category, severity, title,"
            " body, lines, finding_count, state, first_seen_at, last_seen_at, updated_at)"
            " VALUES (1, 'ledger-only', 'app', 'b.dart', 'security', 'critical', '台账标题', '台账正文', '[12, 30]',"
            " 2, 'ignored', '2026-01-01', '2026-01-01', '2026-01-01')"
        )
        for fingerprint, note in (("fp", "老理由"), ("ledger-only", None), ("gone", None)):
            con.execute(
                "INSERT INTO survey_ignore (survey_id, fingerprint, note, created_at) VALUES (1, ?, ?, '2026-01-01')",
                (fingerprint, note),
            )
        con.commit()

    def test_fingerprint_ignores_become_issue_ignores(self):
        """按最近一次运行里的标题换算并关联各轮同标题的发现；找不到发现退回台账；什么都找不到的保留为失效条目。"""
        self._upgrade(self.BEFORE)
        con = sqlite3.connect(self.db_path)
        self._seed(con)
        con.close()

        self._upgrade("head")
        con = sqlite3.connect(self.db_path)
        ignores = con.execute(
            "SELECT fingerprint, line, title, note, file_path FROM survey_ignore ORDER BY fingerprint, line"
        ).fetchall()
        links = dict(con.execute("SELECT id, ignore_id FROM survey_finding").fetchall())
        con.close()

        self.assertEqual(ignores, [
            # 只换算最近一次出现的运行，标题规整后相同的合成一条
            ("fp", 47, "强制解包空返回值", "老理由", "a.dart"),
            ("fp", 225, "可选参数被强制解包", "老理由", "a.dart"),
            # 原发现没有标题时退回正文开头，否则这条忽略从迁移起就不生效
            ("fp", 300, "b", "老理由", "a.dart"),
            # 什么都找不到的保留原样，作为失效条目留给管理员处理
            ("gone", 0, None, None, None),
            # 找不到发现时退回台账快照
            ("ledger-only", 12, "台账标题", None, "b.dart"),
        ])
        # 最近一次运行的三条被关联，更早运行里同标题的两条一起关联；标题不同的同指纹发现恢复显示
        self.assertEqual([links[i] is not None for i in range(1, 8)], [True, True, False, True, True, True, True])
        self.assertEqual(len({links[1], links[2], links[4]}), 1)
        self.assertEqual(links[5], links[6])
