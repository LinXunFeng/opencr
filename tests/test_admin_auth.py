"""后台鉴权与 Guest 边界测试。

重点覆盖 ADR-0002 的核心约束：Guest 拿不到 Finding 正文，且这个剔除必须发生在
服务端。前端隐藏不是安全边界，因此这里断言的是**响应里没有该字段**。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class PasswordHashingTests(unittest.TestCase):
    """明文密码首次启动被就地替换成哈希。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="opencr-cfg-")
        self.config_path = Path(self.tmpdir, "config.yaml")

    def _write(self, password: str) -> None:
        self.config_path.write_text(
            "admin:\n"
            '  enabled: true\n'
            '  username: "admin"\n'
            f'  password: "{password}"\n'
            "  bind_local_only: false\n",
            encoding="utf-8",
        )
        os.chmod(self.config_path, 0o600)

    def test_plaintext_is_rewritten_as_hash(self):
        from backend.admin import auth

        self._write("plain-secret")
        with mock.patch.object(
            auth, "load_admin_config",
            return_value={"enabled": True, "username": "admin",
                          "password": "plain-secret", "bind_local_only": False},
        ), mock.patch.object(auth, "resolve_active_config_path", return_value=str(self.config_path)):
            hashed = auth.ensure_password_hashed()

        self.assertTrue(auth.is_hashed(hashed))
        content = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn("plain-secret", content)
        self.assertIn("scrypt:", content)
        # 注释被插入，用户知道怎么改密码
        self.assertIn("换回明文并重启", content)
        # 权限没有因为改写而放宽
        self.assertEqual(oct(self.config_path.stat().st_mode)[-3:], "600")
        # 哈希能校验原密码
        self.assertTrue(auth.verify_password("plain-secret", hashed))
        self.assertFalse(auth.verify_password("wrong", hashed))

    def test_existing_hash_is_left_alone(self):
        """已经是哈希就完全不动 —— 改写必须幂等。"""
        from backend.admin import auth

        existing = auth.hash_password("secret")
        self._write(existing)
        before = self.config_path.read_text(encoding="utf-8")

        with mock.patch.object(
            auth, "load_admin_config",
            return_value={"enabled": True, "username": "admin",
                          "password": existing, "bind_local_only": False},
        ), mock.patch.object(auth, "resolve_active_config_path", return_value=str(self.config_path)):
            result = auth.ensure_password_hashed()

        self.assertEqual(result, existing)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_falls_back_to_inplace_write_when_atomic_replace_fails(self):
        """
        Docker 把 config.yaml 挂成单文件 bind mount 时，os.replace 会以
        "Device or resource busy" 失败。必须退回原地写入，否则推荐的部署方式上
        明文密码永远清理不掉。
        """
        from backend.admin import auth

        self._write("plain-secret")
        with mock.patch.object(auth, "_atomic_write", return_value=False) as atomic:
            with mock.patch.object(
                auth, "load_admin_config",
                return_value={"enabled": True, "username": "admin",
                              "password": "plain-secret", "bind_local_only": False},
            ), mock.patch.object(auth, "resolve_active_config_path", return_value=str(self.config_path)):
                hashed = auth.ensure_password_hashed()

        atomic.assert_called_once()
        content = self.config_path.read_text(encoding="utf-8")
        self.assertNotIn("plain-secret", content)
        self.assertIn("scrypt:", content)
        self.assertTrue(auth.verify_password("plain-secret", hashed))

    def test_unwritable_config_degrades_instead_of_crashing(self):
        """
        只读挂载时改写会失败。服务必须继续可用（哈希留在内存里），
        而不是拒绝启动 —— 但调用方能看到明文没被清理。
        """
        from backend.admin import auth

        self._write("plain-secret")
        with mock.patch.object(
            auth, "load_admin_config",
            return_value={"enabled": True, "username": "admin",
                          "password": "plain-secret", "bind_local_only": False},
        ), mock.patch.object(auth, "resolve_active_config_path", return_value=str(self.config_path)), \
             mock.patch.object(auth, "_rewrite_password_line", return_value=False):
            hashed = auth.ensure_password_hashed()

        self.assertTrue(auth.is_hashed(hashed))
        self.assertTrue(auth.verify_password("plain-secret", hashed))

    def test_empty_password_never_authenticates(self):
        """没配密码不等于免密登录。"""
        from backend.admin import auth

        self.assertFalse(auth.verify_password("", auth.hash_password("x")))
        self.assertFalse(auth.verify_password("x", ""))
        self.assertFalse(auth.verify_password("", ""))


class GuestBoundaryTests(unittest.TestCase):
    """Guest 可见范围：状态与聚合可见，Finding 正文不可见。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="opencr-db-")
        os.environ["OPENCR_DATABASE_URL"] = f"sqlite:///{Path(self.tmpdir, 'test.db')}"

        from backend.storage import db

        db.reset_engine_for_tests(os.environ["OPENCR_DATABASE_URL"])
        from backend.storage.models import Base

        Base.metadata.create_all(db.get_engine())

        from backend.storage import repo

        self.repo = repo

    def tearDown(self):
        from backend.storage import db

        db.reset_engine_for_tests()
        os.environ.pop("OPENCR_DATABASE_URL", None)

    def _seed(self) -> str:
        from backend.storage.models import DELIVERY_INLINE, RUN_SUCCEEDED

        run_uid = self.repo.start_run(
            project_id=1, mr_iid=2, trigger="webhook_open", review_mode="hybrid",
            mr_title="t", project_path="g/p",
        )
        self.repo.record_finding(
            run_uid, "a.py", 10, "机密的审查正文", DELIVERY_INLINE,
            severity="critical", discussion_id="d1", note_id=5,
        )
        self.repo.finish_run(run_uid, RUN_SUCCEEDED)
        return run_uid

    def test_list_findings_omits_body_when_not_included(self):
        self._seed()
        as_guest = self.repo.list_findings(include_body=False)
        self.assertFalse(as_guest["body_included"])
        self.assertEqual(len(as_guest["items"]), 1)
        # 字段必须不存在，而不是存在但为空
        self.assertNotIn("body", as_guest["items"][0])
        # 状态类信息仍然可见 —— 否则"看状态"就失去意义了
        self.assertEqual(as_guest["items"][0]["file_path"], "a.py")
        self.assertEqual(as_guest["items"][0]["severity"], "critical")

        as_admin = self.repo.list_findings(include_body=True)
        self.assertTrue(as_admin["body_included"])
        self.assertEqual(as_admin["items"][0]["body"], "机密的审查正文")

    def test_guest_read_setting_defaults_to_enabled(self):
        """默认开启 —— 前提是可见范围已经排除了敏感内容，见 ADR-0002。"""
        from backend.admin import auth

        self.assertTrue(auth.guest_read_enabled())
        self.repo.set_setting("guest_read", "0")
        self.assertFalse(auth.guest_read_enabled())
        self.repo.set_setting("guest_read", "1")
        self.assertTrue(auth.guest_read_enabled())

    def test_secret_key_is_stable_across_calls(self):
        """
        session 密钥必须跨调用稳定，否则每个 worker 各持一份，
        用户会随着请求落到不同 worker 而被反复登出。
        """
        from backend.admin import auth

        first = auth.get_secret_key()
        self.assertTrue(first)
        self.assertEqual(first, auth.get_secret_key())

    def test_set_setting_if_absent_does_not_overwrite(self):
        self.assertEqual(self.repo.set_setting_if_absent("k", "first"), "first")
        self.assertEqual(self.repo.set_setting_if_absent("k", "second"), "first")


class SkillHitTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="opencr-db-")
        os.environ["OPENCR_DATABASE_URL"] = f"sqlite:///{Path(self.tmpdir, 'test.db')}"
        from backend.storage import db

        db.reset_engine_for_tests(os.environ["OPENCR_DATABASE_URL"])
        from backend.storage.models import Base

        Base.metadata.create_all(db.get_engine())
        from backend.storage import repo

        self.repo = repo

    def tearDown(self):
        from backend.storage import db

        db.reset_engine_for_tests()
        os.environ.pop("OPENCR_DATABASE_URL", None)

    def test_hits_are_counted_per_skill(self):
        for skills in (["flutter", "general"], ["flutter"], []):
            uid = self.repo.start_run(project_id=1, mr_iid=1, trigger="manual", review_mode="file")
            self.repo.update_progress(uid, review_skills=skills)
        counts = self.repo.skill_hit_counts(days=30)
        self.assertEqual(counts.get("flutter"), 2)
        self.assertEqual(counts.get("general"), 1)


if __name__ == "__main__":
    unittest.main()
