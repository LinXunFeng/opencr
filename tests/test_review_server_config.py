import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


TEST_HOME = tempfile.mkdtemp(prefix="opencr-test-home-")
Path(TEST_HOME, "opencr", "logs").mkdir(parents=True, exist_ok=True)
os.environ["HOME"] = TEST_HOME


def _install_stub_modules():
    if "flask" not in sys.modules:
        flask_stub = types.ModuleType("flask")

        class DummyFlask:
            def __init__(self, *args, **kwargs):
                pass

            def route(self, *args, **kwargs):
                def decorator(func):
                    return func

                return decorator

        class DummyRequest:
            headers = {}
            json = None

        def jsonify(payload):
            return payload

        flask_stub.Flask = DummyFlask
        flask_stub.request = DummyRequest()
        flask_stub.jsonify = jsonify
        sys.modules["flask"] = flask_stub

    if "openai" not in sys.modules:
        openai_stub = types.ModuleType("openai")

        class DummyOpenAI:
            def __init__(self, *args, **kwargs):
                pass

        openai_stub.OpenAI = DummyOpenAI
        sys.modules["openai"] = openai_stub

    if "requests" not in sys.modules:
        requests_stub = types.ModuleType("requests")

        def _placeholder(*args, **kwargs):
            raise AssertionError("requests stub should be patched in each test")

        requests_stub.get = _placeholder
        requests_stub.post = _placeholder
        sys.modules["requests"] = requests_stub

    if "urllib3" not in sys.modules:
        urllib3_stub = types.ModuleType("urllib3")

        class _Exceptions:
            InsecureRequestWarning = RuntimeWarning

        def disable_warnings(*args, **kwargs):
            return None

        urllib3_stub.exceptions = _Exceptions()
        urllib3_stub.disable_warnings = disable_warnings
        sys.modules["urllib3"] = urllib3_stub


_install_stub_modules()
opencr_package = importlib.import_module("src")
review_server = importlib.import_module("src.review_server")


class ReviewServerConfigTests(unittest.TestCase):
    def _env(self, **kwargs):
        base = {"HOME": TEST_HOME, "OPENAI_API_KEY": "test-key"}
        base.update(kwargs)
        return base

    def _write_config_yaml(self, content: str) -> str:
        config_dir = tempfile.mkdtemp(prefix="opencr-config-")
        config_path = Path(config_dir, "config.yaml")
        config_path.write_text(content, encoding="utf-8")
        return str(config_path)

    def _write_review_skill(self, root_dir: str, name: str, content: str) -> str:
        skill_dir = Path(root_dir, "skills", "review")
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / f"{name}.md"
        skill_path.write_text(content, encoding="utf-8")
        return str(skill_dir)

    def _write_review_skill_with_meta(self, root_dir: str, name: str, meta: str, body: str) -> str:
        skill_dir = Path(root_dir, "skills", "review")
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / f"{name}.md"
        skill_path.write_text(f"---\n{meta.strip()}\n---\n\n{body.strip()}\n", encoding="utf-8")
        return str(skill_dir)

    def test_get_mr_diff_supports_code_platform_env_names(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"changes": []}

        with mock.patch.dict(
            os.environ,
            self._env(
                CODE_PLATFORM_URL="https://gitlab.example.com",
                CODE_PLATFORM_TOKEN="token-123",
            ),
            clear=True,
        ):
            with mock.patch("requests.get", return_value=response) as mock_get:
                diff = review_server.get_mr_diff(1022, 8)

        self.assertEqual(diff, "")
        self.assertEqual(
            mock_get.call_args.args[0],
            "https://gitlab.example.com/api/v4/projects/1022/merge_requests/8/changes",
        )
        self.assertEqual(
            mock_get.call_args.kwargs["headers"]["PRIVATE-TOKEN"],
            "token-123",
        )

    def test_post_mr_comment_fails_fast_when_url_missing(self):
        config_path = self._write_config_yaml(
            """
code_platform:
  token: "yaml-token"
""".strip()
        )

        with mock.patch.dict(
            os.environ,
            self._env(
                CODE_PLATFORM_TOKEN="token-123",
                OPENAI_MODEL="kimi-2.5",
                OPENCR_CONFIG_PATH=config_path,
            ),
            clear=True,
        ):
            with mock.patch(
                "requests.post",
                side_effect=Exception("Invalid URL '/api/v4/projects/1022/merge_requests/8/notes'"),
            ) as mock_post:
                with self.assertRaises(review_server.ReviewError) as ctx:
                    review_server.post_mr_comment(1022, 8, "hello")

        self.assertIn("GitLab 配置不完整", str(ctx.exception))
        mock_post.assert_not_called()

    def test_load_openai_config_reads_config_yaml(self):
        config_path = self._write_config_yaml(
            """
openai:
  base_url: "https://api.yaml.example/v1"
  api_key: "yaml-key"
  model: "gpt-yaml"
  reasoning_effort: "high"
""".strip()
        )

        with mock.patch.dict(
            os.environ,
            self._env(OPENAI_API_KEY="", OPENCR_CONFIG_PATH=config_path),
            clear=True,
        ):
            cfg = review_server.load_openai_config()

        self.assertEqual(cfg["base_url"], "https://api.yaml.example/v1")
        self.assertEqual(cfg["api_key"], "yaml-key")
        self.assertEqual(cfg["model"], "gpt-yaml")
        self.assertEqual(cfg["reasoning_effort"], "high")

    def test_load_gitlab_config_reads_config_yaml(self):
        config_path = self._write_config_yaml(
            """
code_platform:
  type: "gitlab"
  url: "https://gitlab.yaml.example"
  token: "yaml-token"
  webhook_secret: "yaml-secret"
""".strip()
        )

        with mock.patch.dict(
            os.environ,
            self._env(OPENAI_API_KEY="", OPENCR_CONFIG_PATH=config_path),
            clear=True,
        ):
            cfg = review_server.load_gitlab_config()

        self.assertEqual(cfg["url"], "https://gitlab.yaml.example")
        self.assertEqual(cfg["token"], "yaml-token")
        self.assertEqual(cfg["webhook_secret"], "yaml-secret")

    def test_env_overrides_config_yaml(self):
        config_path = self._write_config_yaml(
            """
openai:
  model: "gpt-yaml"
code_platform:
  token: "yaml-token"
""".strip()
        )

        with mock.patch.dict(
            os.environ,
            self._env(
                OPENCR_CONFIG_PATH=config_path,
                OPENAI_MODEL="gpt-env",
                CODE_PLATFORM_URL="https://gitlab.env.example",
                CODE_PLATFORM_TOKEN="env-token",
            ),
            clear=True,
        ):
            openai_cfg = review_server.load_openai_config()
            gitlab_cfg = review_server.load_gitlab_config()

        self.assertEqual(openai_cfg["model"], "gpt-env")
        self.assertEqual(gitlab_cfg["url"], "https://gitlab.env.example")
        self.assertEqual(gitlab_cfg["token"], "env-token")

    def test_health_check_includes_version(self):
        with mock.patch.dict(
            os.environ,
            self._env(OPENAI_MODEL="gpt-4.1"),
            clear=True,
        ):
            payload = review_server.health_check()

        self.assertIn("version", payload)
        self.assertEqual(payload["version"], opencr_package.__version__)

    def test_health_check_version_not_overridden_by_env(self):
        with mock.patch.dict(
            os.environ,
            self._env(OPENAI_MODEL="gpt-4.1", OPENCR_VERSION="9.9.9"),
            clear=True,
        ):
            payload = review_server.health_check()

        self.assertEqual(payload["version"], opencr_package.__version__)

    def test_resolve_review_options_does_not_depend_on_labels(self):
        payload = {"object_attributes": {"labels": [{"title": "review:file"}, {"title": "review-skill:flutter"}]}}
        mode, skill = review_server.resolve_review_options(
            payload,
            default_skill="general",
            manual_mode="overall",
            manual_skill="ts",
        )
        self.assertEqual(mode, "overall")
        self.assertEqual(skill, "ts")

        mode_from_default, skill_from_default = review_server.resolve_review_options(
            payload,
            default_skill="general",
            manual_mode="",
            manual_skill="",
        )
        self.assertEqual(mode_from_default, "overall")
        self.assertEqual(skill_from_default, "general")

    def test_load_review_skill_prompt_returns_empty_when_missing(self):
        temp_root = tempfile.mkdtemp(prefix="opencr-review-skill-")
        skills_dir = self._write_review_skill(temp_root, "general", "General review checklist")
        self._write_review_skill(temp_root, "flutter", "Flutter review checklist")

        with mock.patch.dict(
            os.environ,
            self._env(),
            clear=True,
        ):
            self.assertEqual(
                review_server.load_review_skill_prompt("flutter", skills_dir),
                "Flutter review checklist",
            )
            self.assertEqual(
                review_server.load_review_skill_prompt("unknown", skills_dir),
                "",
            )

    def test_load_available_review_skills(self):
        temp_root = tempfile.mkdtemp(prefix="opencr-review-skills-all-")
        skills_dir = self._write_review_skill(temp_root, "general", "General review checklist")
        self._write_review_skill(temp_root, "ts", "TS review checklist")

        with mock.patch.dict(os.environ, self._env(), clear=True):
            skills = review_server.load_available_review_skills(skills_dir)

        self.assertEqual(skills["general"], "General review checklist")
        self.assertEqual(skills["ts"], "TS review checklist")

    def test_auto_select_review_skills_returns_empty_when_no_previews(self):
        with mock.patch.dict(os.environ, self._env(), clear=True):
            selected = review_server.auto_select_review_skills(
                changes=[],
                fallback_skill="general",
                skills_dir="/tmp/opencr-skills-not-exists",
                max_count=2,
            )

        self.assertEqual(selected, [])

    def test_auto_select_review_skills_returns_empty_when_skills_have_no_meta(self):
        temp_root = tempfile.mkdtemp(prefix="opencr-review-skill-no-meta-")
        skills_dir = self._write_review_skill(temp_root, "general", "General review checklist")
        self._write_review_skill(temp_root, "flutter", "Flutter review checklist")

        with mock.patch.dict(os.environ, self._env(), clear=True):
            selected = review_server.auto_select_review_skills(
                changes=[],
                fallback_skill="general",
                skills_dir=skills_dir,
                max_count=2,
            )

        self.assertEqual(selected, [])

    def test_load_review_skill_prompt_strips_frontmatter_meta(self):
        temp_root = tempfile.mkdtemp(prefix="opencr-review-skill-meta-")
        skills_dir = self._write_review_skill_with_meta(
            temp_root,
            "flutter",
            'name: flutter\ndescription: flutter skill',
            "Flutter review checklist",
        )
        self._write_review_skill(temp_root, "general", "General review checklist")

        with mock.patch.dict(os.environ, self._env(), clear=True):
            prompt = review_server.load_review_skill_prompt("flutter", skills_dir)

        self.assertEqual(prompt, "Flutter review checklist")

    def test_parse_selected_skill_uses_fallback_when_invalid(self):
        allowed = ["general", "flutter", "ts"]
        self.assertEqual(
            review_server._parse_selected_skill("flutter", allowed, "general"),
            "flutter",
        )
        self.assertEqual(
            review_server._parse_selected_skill("skill: ts", allowed, "general"),
            "general",
        )
        self.assertEqual(
            review_server._parse_selected_skill("unknown", allowed, "general"),
            "general",
        )

    def test_build_review_prompt_includes_mode_and_skill_prompt(self):
        prompt = review_server.build_review_prompt(
            "diff --git a/a.ts b/a.ts\n+const a = 1;",
            review_mode="file",
            file_path="lib/main.dart",
            skill_prompt="Flutter review checklist",
            skill_name="flutter",
        )

        self.assertIn("当前审核模式：按文件审查", prompt)
        self.assertIn("当前文件：`lib/main.dart`", prompt)
        self.assertIn("Skill（flutter）", prompt)
        self.assertIn("Flutter review checklist", prompt)
        self.assertNotIn("1. **Bug 与逻辑错误**", prompt)

    def test_build_review_prompt_uses_default_rules_when_skill_prompt_empty(self):
        prompt = review_server.build_review_prompt(
            "diff --git a/a.ts b/a.ts\n+const a = 1;",
            review_mode="overall",
            file_path="",
            skill_prompt="",
            skill_name="",
        )

        self.assertIn("当前审核模式：整 MR 审查", prompt)
        self.assertIn("1. **Bug 与逻辑错误**", prompt)
        self.assertNotIn("## 场景 Skill（", prompt)

    def test_should_review_mr_open_uses_hybrid_mode(self):
        payload = {
            "object_attributes": {
                "action": "open",
                "title": "feat: add api",
                "source_branch": "feature/api",
            }
        }
        should_review, reason, mode = review_server.should_review_mr(payload)
        self.assertTrue(should_review)
        self.assertIn("MR 创建", reason)
        self.assertEqual(mode, "hybrid")

    def test_should_review_mr_update_with_new_commit_uses_file_mode(self):
        payload = {
            "object_attributes": {
                "action": "update",
                "oldrev": "a" * 40,
                "title": "feat: add api",
                "source_branch": "feature/api",
            }
        }
        should_review, reason, mode = review_server.should_review_mr(payload)
        self.assertTrue(should_review)
        self.assertIn("新提交", reason)
        self.assertEqual(mode, "file")

    def test_should_review_mr_reopen_is_skipped(self):
        payload = {
            "object_attributes": {
                "action": "reopen",
                "title": "feat: reopen mr",
                "source_branch": "feature/api",
            }
        }
        should_review, reason, mode = review_server.should_review_mr(payload)
        self.assertFalse(should_review)
        self.assertIn("reopen", reason)
        self.assertEqual(mode, "")

    def test_should_review_mr_update_without_new_commit_is_skipped(self):
        payload = {
            "object_attributes": {
                "action": "update",
                "oldrev": "",
                "title": "chore: update description",
                "source_branch": "feature/api",
            }
        }
        should_review, reason, mode = review_server.should_review_mr(payload)
        self.assertFalse(should_review)
        self.assertIn("非新提交", reason)
        self.assertEqual(mode, "")


if __name__ == "__main__":
    unittest.main()
