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

    def test_build_review_prompt_includes_file_metadata(self):
        prompt = review_server.build_review_prompt(
            "diff --git a/assets/hero.png b/assets/hero.png",
            review_mode="file",
            file_path="assets/hero.png",
            file_metadata='{"path":"assets/hero.png","file_size_bytes":634880}',
            skill_prompt="",
            skill_name="",
        )

        self.assertIn("## 文件元信息（由程序采集）", prompt)
        self.assertIn('"file_size_bytes":634880', prompt)

    def test_enrich_changes_with_file_info_adds_size_fields(self):
        changes = [
            {
                "new_path": "assets/hero.png",
                "old_path": "assets/hero.png",
                "new_file": False,
                "deleted_file": False,
            },
            {
                "new_path": "lib/main.dart",
                "old_path": "lib/main.dart",
                "new_file": False,
                "deleted_file": False,
            },
            {
                "new_path": "assets/deleted.png",
                "old_path": "assets/deleted.png",
                "new_file": False,
                "deleted_file": True,
            },
        ]

        def _build_response(size):
            response = mock.Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"size": size}
            return response

        def _fake_get(url, headers=None, params=None, timeout=None, verify=None):
            if url.endswith("/repository/files/assets%2Fhero.png"):
                return _build_response(620 * 1024)
            if url.endswith("/repository/files/lib%2Fmain.dart"):
                return _build_response(2 * 1024)
            raise AssertionError(f"unexpected url: {url}")

        with mock.patch.dict(
            os.environ,
            self._env(
                CODE_PLATFORM_URL="https://gitlab.example.com",
                CODE_PLATFORM_TOKEN="token-123",
            ),
            clear=True,
        ):
            with mock.patch("requests.get", side_effect=_fake_get):
                enriched = review_server.enrich_changes_with_file_info(
                    project_id=1022,
                    changes=changes,
                    ref="b" * 40,
                )

        self.assertEqual(enriched[0]["new_path"], "assets/hero.png")
        self.assertEqual(enriched[0]["file_size_bytes"], 620 * 1024)
        self.assertEqual(enriched[0]["file_size_kb"], 620.0)
        self.assertEqual(enriched[1]["file_size_bytes"], 2 * 1024)
        self.assertIsNone(enriched[2]["file_size_bytes"])

    def test_process_review_async_passes_enriched_changes_to_ai_review(self):
        changes = [
            {
                "new_path": "assets/hero.png",
                "old_path": "assets/hero.png",
                "new_file": False,
                "deleted_file": False,
            }
        ]
        enriched_changes = [
            {
                "new_path": "assets/hero.png",
                "old_path": "assets/hero.png",
                "new_file": False,
                "deleted_file": False,
                "file_size_bytes": 620 * 1024,
                "file_size_kb": 620.0,
            }
        ]

        with mock.patch.object(
            review_server,
            "load_review_config",
            return_value={"max_diff_size": 50000, "skills_dir": "skills/review"},
        ):
            with mock.patch.object(
                review_server,
                "get_mr_changes_with_refs",
                return_value=(changes, {"head_sha": "c" * 40}),
            ):
                with mock.patch.object(review_server, "get_compare_changes", return_value=changes):
                    with mock.patch.object(
                        review_server,
                        "enrich_changes_with_file_info",
                        return_value=enriched_changes,
                    ) as mock_enrich:
                        with mock.patch.object(review_server, "review_changes_with_inline_notes", return_value=("", [])) as mock_review:
                            with mock.patch.object(review_server, "post_mr_comment") as mock_post:
                                review_server.process_review_async(
                                    project_id=1022,
                                    mr_iid=8,
                                    mr_title="test",
                                    review_mode="file",
                                    review_skill="",
                                    action="update",
                                    update_from_sha="a" * 40,
                                    update_to_sha="b" * 40,
                                )

        self.assertEqual(mock_enrich.call_args.kwargs["project_id"], 1022)
        self.assertEqual(mock_enrich.call_args.kwargs["ref"], "b" * 40)
        self.assertEqual(mock_review.call_args.args[0], enriched_changes)
        mock_post.assert_not_called()

    def test_process_review_async_falls_back_to_mr_comment_when_inline_fails(self):
        changes = [
            {
                "new_path": "assets/images/a.png",
                "old_path": "assets/images/a.png",
                "new_file": False,
                "deleted_file": False,
            }
        ]
        inline_notes = [
            {
                "file_path": "assets/images/a.png",
                "line": 1,
                "body": "#### 问题 : 图片体积过大\n- **建议**: 请压缩图片至 500KB 以内后再提交。",
            }
        ]

        with mock.patch.object(
            review_server,
            "load_review_config",
            return_value={"max_diff_size": 50000, "skills_dir": "skills/review"},
        ):
            with mock.patch.object(
                review_server,
                "get_mr_changes_with_refs",
                return_value=(changes, {"head_sha": "c" * 40}),
            ):
                with mock.patch.object(review_server, "get_compare_changes", return_value=changes):
                    with mock.patch.object(review_server, "enrich_changes_with_file_info", return_value=changes):
                        with mock.patch.object(
                            review_server,
                            "review_changes_with_inline_notes",
                            return_value=("", inline_notes),
                        ):
                            with mock.patch.object(
                                review_server,
                                "_post_inline_comment_with_offset",
                                side_effect=review_server.ReviewError("发布行内评论失败"),
                            ):
                                with mock.patch.object(review_server, "post_mr_comment") as mock_post:
                                    review_server.process_review_async(
                                        project_id=1022,
                                        mr_iid=11,
                                        mr_title="test",
                                        review_mode="file",
                                        review_skill="general",
                                        action="update",
                                        update_from_sha="a" * 40,
                                        update_to_sha="b" * 40,
                                    )

        mock_post.assert_called_once()
        content = mock_post.call_args.args[2]
        self.assertIn("文件级降级评论", content)
        self.assertIn("已降级为普通评论展示", content)
        self.assertIn("- **位置**: `assets/images/a.png`", content)

    def test_build_inline_fallback_comments_by_file(self):
        failed_notes = [
            {"file_path": "assets/images/a.png", "line": 2, "body": "first issue"},
            {"file_path": "assets/images/a.png", "line": 8, "body": "second issue"},
            {"file_path": "assets/images/b.png", "line": 1, "body": "third issue"},
        ]

        comments = review_server._build_inline_fallback_comments_by_file(failed_notes)

        self.assertEqual(len(comments), 2)
        self.assertIn("文件级降级评论｜`assets/images/a.png`", comments[0])
        self.assertIn("assets/images/a.png:2", comments[0])
        self.assertIn("assets/images/a.png:8", comments[0])
        self.assertIn("文件级降级评论｜`assets/images/b.png`", comments[1])
        self.assertIn("assets/images/b.png:1", comments[1])

    def test_is_binary_or_non_text_change(self):
        self.assertTrue(review_server._is_binary_or_non_text_change({"diff": ""}))
        self.assertTrue(
            review_server._is_binary_or_non_text_change(
                {"diff": "Binary files a/assets/a.png and b/assets/a.png differ"}
            )
        )
        self.assertFalse(
            review_server._is_binary_or_non_text_change(
                {"diff": "@@ -1,1 +1,1 @@\n-foo\n+bar\n"}
            )
        )

    def test_process_review_async_fallback_posts_separate_comment_per_file(self):
        changes = [
            {
                "new_path": "assets/images/a.png",
                "old_path": "assets/images/a.png",
                "new_file": False,
                "deleted_file": False,
            },
            {
                "new_path": "assets/images/b.png",
                "old_path": "assets/images/b.png",
                "new_file": False,
                "deleted_file": False,
            },
        ]
        inline_notes = [
            {
                "file_path": "assets/images/a.png",
                "line": 2,
                "body": "issue a",
            },
            {
                "file_path": "assets/images/b.png",
                "line": 3,
                "body": "issue b",
            },
        ]

        with mock.patch.object(
            review_server,
            "load_review_config",
            return_value={"max_diff_size": 50000, "skills_dir": "skills/review"},
        ):
            with mock.patch.object(
                review_server,
                "get_mr_changes_with_refs",
                return_value=(changes, {"head_sha": "c" * 40}),
            ):
                with mock.patch.object(review_server, "get_compare_changes", return_value=changes):
                    with mock.patch.object(review_server, "enrich_changes_with_file_info", return_value=changes):
                        with mock.patch.object(
                            review_server,
                            "review_changes_with_inline_notes",
                            return_value=("", inline_notes),
                        ):
                            with mock.patch.object(
                                review_server,
                                "_post_inline_comment_with_offset",
                                side_effect=review_server.ReviewError("发布行内评论失败"),
                            ):
                                with mock.patch.object(review_server, "post_mr_comment") as mock_post:
                                    review_server.process_review_async(
                                        project_id=1022,
                                        mr_iid=11,
                                        mr_title="test",
                                        review_mode="file",
                                        review_skill="general",
                                        action="update",
                                        update_from_sha="a" * 40,
                                        update_to_sha="b" * 40,
                                    )

        self.assertEqual(mock_post.call_count, 2)
        first_comment = mock_post.call_args_list[0].args[2]
        second_comment = mock_post.call_args_list[1].args[2]
        self.assertIn("文件级降级评论｜`assets/images/a.png`", first_comment)
        self.assertIn("- **位置**: `assets/images/a.png`", first_comment)
        self.assertIn("文件级降级评论｜`assets/images/b.png`", second_comment)
        self.assertIn("- **位置**: `assets/images/b.png`", second_comment)

    def test_process_review_async_skips_inline_for_binary_and_posts_file_level_comment(self):
        changes = [
            {
                "new_path": "assets/images/a.png",
                "old_path": "assets/images/a.png",
                "diff": "",
                "new_file": False,
                "deleted_file": False,
            }
        ]
        inline_notes = [
            {
                "file_path": "assets/images/a.png",
                "line": 1,
                "body": "binary issue",
            }
        ]

        with mock.patch.object(
            review_server,
            "load_review_config",
            return_value={"max_diff_size": 50000, "skills_dir": "skills/review"},
        ):
            with mock.patch.object(
                review_server,
                "get_mr_changes_with_refs",
                return_value=(changes, {"head_sha": "c" * 40}),
            ):
                with mock.patch.object(review_server, "get_compare_changes", return_value=changes):
                    with mock.patch.object(review_server, "enrich_changes_with_file_info", return_value=changes):
                        with mock.patch.object(
                            review_server,
                            "review_changes_with_inline_notes",
                            return_value=("", inline_notes),
                        ):
                            with mock.patch.object(
                                review_server,
                                "_post_inline_comment_with_offset",
                            ) as mock_inline_post:
                                with mock.patch.object(review_server, "post_mr_comment") as mock_post:
                                    review_server.process_review_async(
                                        project_id=1022,
                                        mr_iid=11,
                                        mr_title="test",
                                        review_mode="file",
                                        review_skill="general",
                                        action="update",
                                        update_from_sha="a" * 40,
                                        update_to_sha="b" * 40,
                                    )

        mock_inline_post.assert_not_called()
        mock_post.assert_called_once()
        content = mock_post.call_args.args[2]
        self.assertIn("文件级降级评论｜`assets/images/a.png`", content)
        self.assertIn("- **位置**: `assets/images/a.png`", content)

    def test_file_review_non_text_change_is_not_skipped_when_diff_empty(self):
        non_text_change = {
            "new_path": "assets/images/a.png",
            "old_path": "assets/images/a.png",
            "diff": "",
            "new_file": False,
            "deleted_file": False,
            "file_size_bytes": 620 * 1024,
            "file_size_kb": 620.0,
        }

        with mock.patch("src.review.ai.auto_select_review_skills", return_value=["general"]):
            with mock.patch("src.review.ai.load_review_skill_prompts", return_value="general rules"):
                with mock.patch(
                    "src.review.ai.call_codex_review",
                    return_value=(
                        "#### 问题 : 图片体积过大\n"
                        "- **位置**: `assets/images/a.png:1`\n"
                        "- **级别**: 🟡 警告\n"
                        "- **描述**: 文件体积过大\n"
                        "- **建议**: 请压缩后提交。\n"
                    ),
                ) as mock_call:
                    _summary, inline_notes = review_server.review_changes_with_inline_notes(
                        changes=[non_text_change],
                        review_mode="file",
                        review_skill="",
                        max_diff_size=50000,
                        skills_dir="skills/review",
                    )

        self.assertEqual(len(inline_notes), 1)
        self.assertEqual(inline_notes[0]["file_path"], "assets/images/a.png")
        self.assertEqual(inline_notes[0]["line"], 1)
        called_diff = mock_call.call_args.args[0]
        self.assertIn("Binary or non-text file changed", called_diff)

    def test_file_review_unstructured_issue_falls_back_to_file_level_note(self):
        non_text_change = {
            "new_path": "assets/images/a.png",
            "old_path": "assets/images/a.png",
            "diff": "",
            "new_file": False,
            "deleted_file": False,
            "file_size_bytes": 620 * 1024,
            "file_size_kb": 620.0,
        }

        with mock.patch("src.review.ai.auto_select_review_skills", return_value=["general"]):
            with mock.patch("src.review.ai.load_review_skill_prompts", return_value="general rules"):
                with mock.patch(
                    "src.review.ai.call_codex_review",
                    return_value=(
                        "### 详细审查结果\n"
                        "- 该图片文件体积超过 300KB，建议压缩后提交。\n"
                    ),
                ):
                    _summary, inline_notes = review_server.review_changes_with_inline_notes(
                        changes=[non_text_change],
                        review_mode="file",
                        review_skill="",
                        max_diff_size=50000,
                        skills_dir="skills/review",
                    )

        self.assertEqual(len(inline_notes), 1)
        self.assertEqual(inline_notes[0]["file_path"], "assets/images/a.png")
        self.assertEqual(inline_notes[0]["line"], 0)
        self.assertIn("300KB", inline_notes[0]["body"])

    def test_file_review_explicit_pass_text_does_not_create_file_level_note(self):
        non_text_change = {
            "new_path": "assets/images/a.png",
            "old_path": "assets/images/a.png",
            "diff": "",
            "new_file": False,
            "deleted_file": False,
            "file_size_bytes": 120 * 1024,
            "file_size_kb": 120.0,
        }

        with mock.patch("src.review.ai.auto_select_review_skills", return_value=["general"]):
            with mock.patch("src.review.ai.load_review_skill_prompts", return_value="general rules"):
                with mock.patch(
                    "src.review.ai.call_codex_review",
                    return_value="✅ 代码审查通过，未发现明显问题。",
                ):
                    _summary, inline_notes = review_server.review_changes_with_inline_notes(
                        changes=[non_text_change],
                        review_mode="file",
                        review_skill="",
                        max_diff_size=50000,
                        skills_dir="skills/review",
                    )

        self.assertEqual(inline_notes, [])

    def test_file_review_long_pass_text_without_issue_marker_does_not_create_note(self):
        non_text_change = {
            "new_path": "assets/images/a.png",
            "old_path": "assets/images/a.png",
            "diff": "",
            "new_file": False,
            "deleted_file": False,
            "file_size_bytes": 120 * 1024,
            "file_size_kb": 120.0,
        }

        with mock.patch("src.review.ai.auto_select_review_skills", return_value=["general"]):
            with mock.patch("src.review.ai.load_review_skill_prompts", return_value="general rules"):
                with mock.patch(
                    "src.review.ai.call_codex_review",
                    return_value=(
                        "### 总体评价\n"
                        "本次未发现高置信度问题，代码逻辑清晰，结构完整，未发现明显风险，保持当前实现即可。"
                    ),
                ):
                    _summary, inline_notes = review_server.review_changes_with_inline_notes(
                        changes=[non_text_change],
                        review_mode="file",
                        review_skill="",
                        max_diff_size=50000,
                        skills_dir="skills/review",
                    )

        self.assertEqual(inline_notes, [])

    def test_file_review_pass_text_with_suggestion_still_creates_file_level_note(self):
        non_text_change = {
            "new_path": "assets/images/a.png",
            "old_path": "assets/images/a.png",
            "diff": "",
            "new_file": False,
            "deleted_file": False,
            "file_size_bytes": 620 * 1024,
            "file_size_kb": 620.0,
        }

        with mock.patch("src.review.ai.auto_select_review_skills", return_value=["general"]):
            with mock.patch("src.review.ai.load_review_skill_prompts", return_value="general rules"):
                with mock.patch(
                    "src.review.ai.call_codex_review",
                    return_value=(
                        "本次未发现高置信度问题，但该图片体积偏大，建议压缩后提交。"
                    ),
                ):
                    _summary, inline_notes = review_server.review_changes_with_inline_notes(
                        changes=[non_text_change],
                        review_mode="file",
                        review_skill="",
                        max_diff_size=50000,
                        skills_dir="skills/review",
                    )

        self.assertEqual(len(inline_notes), 1)
        self.assertEqual(inline_notes[0]["file_path"], "assets/images/a.png")
        self.assertEqual(inline_notes[0]["line"], 0)
        self.assertIn("建议压缩", inline_notes[0]["body"])

    def test_process_review_async_posts_file_level_comment_when_line_is_zero(self):
        changes = [
            {
                "new_path": "assets/images/a.png",
                "old_path": "assets/images/a.png",
                "diff": "",
                "new_file": False,
                "deleted_file": False,
            }
        ]
        inline_notes = [
            {
                "file_path": "assets/images/a.png",
                "line": 0,
                "body": "### 详细审查结果\n- 图片过大，请压缩。",
            }
        ]

        with mock.patch.object(
            review_server,
            "load_review_config",
            return_value={"max_diff_size": 50000, "skills_dir": "skills/review"},
        ):
            with mock.patch.object(
                review_server,
                "get_mr_changes_with_refs",
                return_value=(changes, {"head_sha": "c" * 40}),
            ):
                with mock.patch.object(review_server, "get_compare_changes", return_value=changes):
                    with mock.patch.object(review_server, "enrich_changes_with_file_info", return_value=changes):
                        with mock.patch.object(
                            review_server,
                            "review_changes_with_inline_notes",
                            return_value=("", inline_notes),
                        ):
                            with mock.patch.object(
                                review_server,
                                "_post_inline_comment_with_offset",
                            ) as mock_inline_post:
                                with mock.patch.object(review_server, "post_mr_comment") as mock_post:
                                    review_server.process_review_async(
                                        project_id=1022,
                                        mr_iid=11,
                                        mr_title="test",
                                        review_mode="file",
                                        review_skill="general",
                                        action="update",
                                        update_from_sha="a" * 40,
                                        update_to_sha="b" * 40,
                                    )

        mock_inline_post.assert_not_called()
        mock_post.assert_called_once()
        content = mock_post.call_args.args[2]
        self.assertIn("文件级降级评论｜`assets/images/a.png`", content)
        self.assertIn("- **位置**: `assets/images/a.png`", content)

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
                "state": "opened",
                "title": "feat: add api",
                "source_branch": "feature/api",
            }
        }
        should_review, reason, mode = review_server.should_review_mr(payload)
        self.assertTrue(should_review)
        self.assertIn("新提交", reason)
        self.assertEqual(mode, "file")

    def test_should_review_mr_update_merge_commit_is_skipped(self):
        payload = {
            "object_attributes": {
                "action": "update",
                "oldrev": "a" * 40,
                "state": "opened",
                "title": "feat: add api",
                "source_branch": "feature/api",
                "last_commit": {
                    "id": "b" * 40,
                    "message": "Merge branch 'master' into 'feature/api'",
                },
            }
        }
        should_review, reason, mode = review_server.should_review_mr(payload)
        self.assertFalse(should_review)
        self.assertIn("merge commit", reason)
        self.assertEqual(mode, "")

    def test_should_review_mr_update_closed_is_skipped(self):
        payload = {
            "object_attributes": {
                "action": "update",
                "oldrev": "a" * 40,
                "state": "closed",
                "title": "feat: add api",
                "source_branch": "feature/api",
            }
        }
        should_review, reason, mode = review_server.should_review_mr(payload)
        self.assertFalse(should_review)
        self.assertIn("state=closed", reason)
        self.assertEqual(mode, "")

    def test_should_review_mr_update_merged_is_skipped(self):
        payload = {
            "object_attributes": {
                "action": "update",
                "oldrev": "a" * 40,
                "state": "merged",
                "title": "feat: add api",
                "source_branch": "feature/api",
            }
        }
        should_review, reason, mode = review_server.should_review_mr(payload)
        self.assertFalse(should_review)
        self.assertIn("state=merged", reason)
        self.assertEqual(mode, "")

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
