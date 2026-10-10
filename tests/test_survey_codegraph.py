"""codegraph 执行情况与线索来源的测试。

重点覆盖三类"错了也不会报错、只会让页面撒谎"的约束：
1. 线索来源宁可低估不可高估 —— 不靠 codegraph 也看得到的位置不能记成它的功劳；
2. NULL 与 0 必须分开 —— "没建索引"和"建了但抽出 0 条"是两回事，旧运行没有数据不是"没起作用"；
3. Finding 的线索分布按落库结果现算 —— 被忽略的条目不入库，不能算进去。
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


def _profile(**overrides):
    """一个结构图画像：后端路由与类型来自 codegraph，调用侧与清单是基础画像。"""
    profile = {
        "repo_slug": "api",
        "kind": "codegraph",
        "manifests": {"go.mod": "module api"},
        "tree": ["handlers/ (order)", "main.go", "README.md"],
        "routes": [
            {"method": "POST", "path": "/api/order", "file": "main.go",
             "handler": "CreateOrder", "handler_file": "handlers/order.go"},
        ],
        "types": [{"kind": "struct", "name": "Order", "file": "model/order.go"}],
        "api_calls": [{"path": "/api/pay", "file": "client/pay.go", "line": 3}],
        "codegraph_status": "enabled",
        "index": {"mode": "init", "elapsed_ms": 1200, "error": ""},
        "dropped_calls": [
            {"path": "/api/order", "file": "main.go", "line": 9},
            {"path": "/api/admin", "file": "routes/admin.go", "line": 4},
        ],
    }
    profile.update(overrides)
    return profile


class ClueClassificationTests(unittest.TestCase):
    def test_codegraph_only_paths_are_credited_to_codegraph(self):
        from backend.survey.clues import classify_clue

        self.assertEqual(classify_clue(_profile(), "handlers/order.go"), "codegraph")
        self.assertEqual(classify_clue(_profile(), "model/order.go"), "codegraph")

    def test_paths_visible_without_codegraph_are_baseline(self):
        """路由注册在顶层 main.go，而 main.go 本来就在目录摘要里——不能记成 codegraph 的功劳。"""
        from backend.survey.clues import classify_clue

        self.assertEqual(classify_clue(_profile(), "main.go"), "baseline")
        self.assertEqual(classify_clue(_profile(), "client/pay.go"), "baseline")
        self.assertEqual(classify_clue(_profile(), "go.mod"), "baseline")

    def test_route_files_seen_by_the_call_scan_stay_baseline(self):
        """
        不开 codegraph 时，路由注册那一行本来就会被调用侧扫描到；codegraph 认出它是路由后才被剔除。
        不把剔除的条目算回基础画像，后端路由文件会全被记成 codegraph 的功劳。
        """
        from backend.survey.clues import classify_clue

        profile = _profile(routes=[{"method": "GET", "path": "/api/admin", "file": "routes/admin.go",
                                    "handler": "Admin", "handler_file": "routes/admin.go"}])
        self.assertEqual(classify_clue(profile, "routes/admin.go"), "baseline")
        profile["dropped_calls"] = []
        self.assertEqual(classify_clue(profile, "routes/admin.go"), "codegraph")

    def test_unknown_paths_and_directories_are_unlisted(self):
        from backend.survey.clues import classify_clue

        self.assertEqual(classify_clue(_profile(), "service/guess.go"), "unlisted")
        # 目录摘要里的目录名不算指向了某个文件
        self.assertEqual(classify_clue(_profile(), "handlers"), "unlisted")
        self.assertEqual(classify_clue(None, "main.go"), "unlisted")

    def test_model_path_prefixes_are_normalised(self):
        from backend.survey.clues import classify_clue

        self.assertEqual(classify_clue(_profile(), "./handlers/order.go"), "codegraph")
        self.assertEqual(classify_clue(_profile(), "/model/order.go"), "codegraph")

    def test_rechecks_not_named_by_l1_are_credited_to_the_ledger(self):
        """
        台账复核的文件是"上次发现过问题"才来的，不是画像指出来的。按路径判定的话，
        恰好也在 codegraph 路由里的复核文件会被记成 codegraph 的功劳。
        """
        from backend.survey.analysis import focus_key
        from backend.survey.clues import annotate_focuses

        l1 = {"repo_slug": "api", "file_path": "model/order.go"}
        focuses = [
            {"repo_slug": "api", "file_path": "handlers/order.go"},   # 纯复核，路径恰好是 codegraph 文件
            {"repo_slug": "api", "file_path": "./model/order.go"},    # 复核且 L1 也点名了
        ]
        annotate_focuses(focuses, [_profile()], named_by_l1={focus_key(l1)})
        self.assertEqual([f["clue_source"] for f in focuses], ["recheck", "codegraph"])

    def test_focuses_are_annotated_per_repo(self):
        from backend.survey.clues import annotate_focuses

        focuses = [
            {"repo_slug": "api", "file_path": "handlers/order.go"},
            {"repo_slug": "web", "file_path": "handlers/order.go"},
        ]
        annotate_focuses(focuses, [_profile()])
        # 同一路径在另一个仓库里没有任何画像依据
        self.assertEqual([f["clue_source"] for f in focuses], ["codegraph", "unlisted"])


class CodegraphSummaryTests(unittest.TestCase):
    def test_summary_counts_structure_and_cross_repo_facts(self):
        from backend.survey.clues import summarize_codegraph

        cross_map = {"links": [1, 2], "method_mismatch": [1], "orphan_calls": [], "unused_routes": [1]}
        focuses = [{"clue_source": "codegraph"}, {"clue_source": "baseline"}, {"clue_source": "codegraph"}]
        stats = summarize_codegraph([_profile()], cross_map, focuses)

        self.assertEqual(stats["status"], "enabled")
        self.assertEqual((stats["routes"], stats["types"]), (1, 1))
        self.assertEqual(stats["dropped_registrations"], 2)
        self.assertEqual(stats["cross_repo"], {"links": 2, "method_mismatch": 1, "orphan_calls": 0, "unused_routes": 1})
        self.assertEqual(stats["focus"], {"codegraph": 2, "baseline": 1, "unlisted": 0, "recheck": 0})

    def test_focus_is_none_until_l1_has_run(self):
        """"没有关注点"和"还没到这一步"在页面上要能区分。"""
        from backend.survey.clues import summarize_codegraph

        self.assertIsNone(summarize_codegraph([_profile()], {})["focus"])
        self.assertEqual(summarize_codegraph([_profile()], {}, [])["focus"],
                         {"codegraph": 0, "baseline": 0, "unlisted": 0, "recheck": 0})

    def test_unannotated_focus_is_counted_as_unknown(self):
        """标注缺失说明调用顺序被改坏了：记为无记录让它显形，而不是悄悄抬高某一类。"""
        from backend.survey.clues import summarize_codegraph

        stats = summarize_codegraph([_profile()], {}, [{"clue_source": "codegraph"}, {}])
        self.assertEqual(stats["focus"]["unknown"], 1)
        self.assertEqual(stats["focus"]["unlisted"], 0)

    def test_empty_extraction_is_counted_separately(self):
        """索引建成却一条都没抽出来，效果等同未启用，但画像类型显示的是结构图。"""
        from backend.survey.clues import summarize_codegraph

        stats = summarize_codegraph([_profile(routes=[], types=[]), _profile(repo_slug="b")], {})
        self.assertEqual((stats["repos_structured"], stats["repos_empty"]), (2, 1))

    def test_availability_comes_from_the_profiles(self):
        from backend.survey.clues import summarize_codegraph

        degraded = _profile(kind="manifest", routes=[], types=[], index=None, dropped_calls=[],
                            codegraph_status="disabled")
        stats = summarize_codegraph([degraded], {})
        self.assertEqual(stats["status"], "disabled")
        self.assertEqual((stats["repos_structured"], stats["repos_empty"]), (0, 0))
        # 索引失败的仓库画像也退化成清单级，但 codegraph 本身是启用的
        failed = _profile(kind="manifest", routes=[], types=[], index={"mode": "failed", "elapsed_ms": 5})
        self.assertEqual(summarize_codegraph([failed], {})["status"], "enabled")

    def test_missing_binary_is_told_apart_from_switched_off(self):
        """对照实验关掉 codegraph 与部署装坏了都会退化，但只有前者能拿来比。"""
        from backend.survey.clues import summarize_codegraph

        missing = _profile(kind="manifest", routes=[], types=[], index=None, codegraph_status="missing")
        stats = summarize_codegraph([missing], {})
        self.assertEqual(stats["status"], "missing")

    def test_profile_status_reflects_config_and_binary(self):
        from backend.survey import profile

        cases = [
            ({"codegraph_enabled": False, "codegraph_bin": "x"}, "/bin/x", "disabled"),
            ({"codegraph_enabled": True, "codegraph_bin": "x"}, None, "missing"),
            ({"codegraph_enabled": True, "codegraph_bin": "x"}, "/bin/x", "enabled"),
        ]
        for cfg, which, expected in cases:
            with self.subTest(expected=expected), \
                    mock.patch.object(profile, "load_survey_config", return_value=cfg), \
                    mock.patch.object(profile.shutil, "which", return_value=which):
                self.assertEqual(profile.codegraph_status(), expected)


class IndexOutcomeTests(unittest.TestCase):
    """建索引要如实报告走的是哪条路：增量、全量还是失败，以及失败原因。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="opencr-index-")
        self.repo_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self):
        from backend.survey.common import CODEGRAPH_INDEX_DIRNAME

        index_dir = self.repo_dir / CODEGRAPH_INDEX_DIRNAME
        index_dir.mkdir(exist_ok=True)
        (index_dir / "codegraph.db").write_text("db", encoding="utf-8")

    def _run(self, results, reusable=False):
        """按顺序返回 results 里的 _run_codegraph 结果；init 成功时顺带造出库文件。"""
        from backend.survey import profile

        queue = list(results)

        def fake(args, repo_dir, timeout):
            result = queue.pop(0)
            if args[0] == "init" and result is not None and result.returncode == 0:
                self._db()
            return result

        with mock.patch.object(profile, "_index_is_reusable", return_value=reusable), \
                mock.patch.object(profile, "_run_codegraph", side_effect=fake):
            return profile.build_index(self.repo_dir, timeout=5)

    def test_fresh_repository_is_built_from_scratch(self):
        outcome = self._run([mock.Mock(returncode=0)])
        self.assertEqual(outcome.mode, "init")
        self.assertIsNotNone(outcome.db_path)
        self.assertGreaterEqual(outcome.elapsed_ms, 0)

    def test_reusable_index_is_synced(self):
        self._db()
        outcome = self._run([mock.Mock(returncode=0)], reusable=True)
        self.assertEqual(outcome.mode, "sync")

    def test_failed_sync_is_reported_as_the_rebuild_it_fell_back_to(self):
        self._db()
        outcome = self._run([mock.Mock(returncode=1, stderr="boom", stdout=""), mock.Mock(returncode=0)],
                            reusable=True)
        self.assertEqual(outcome.mode, "init")
        self.assertIsNotNone(outcome.db_path)

    def test_failures_carry_a_reason(self):
        outcome = self._run([mock.Mock(returncode=2, stderr="unsupported", stdout="")])
        self.assertEqual(outcome.mode, "failed")
        self.assertIsNone(outcome.db_path)
        self.assertIn("unsupported", outcome.error)

        multiline = self._run([mock.Mock(returncode=1, stderr="line one\n  line two\n", stdout="")])
        # 多行 stderr 原样落库会撑破 Markdown 导出里的表格行
        self.assertEqual(multiline.error, "exit=1 line one line two")

        timeout = self._run([None])
        self.assertEqual(timeout.mode, "failed")
        self.assertIn("超时", timeout.error)


class FindingClueInheritanceTests(unittest.TestCase):
    def test_findings_inherit_the_clue_of_their_focus(self):
        from backend.survey import runner

        focuses = [
            {"repo_slug": "api", "file_path": "handlers/order.go", "clue_source": "codegraph"},
            {"repo_slug": "api", "file_path": "main.go", "clue_source": "baseline"},
        ]

        def fake_inspect(focus, repo_dir, skill_prompt, budget):
            return [{"repo_slug": focus["repo_slug"], "file_path": focus["file_path"]}], True

        budget = mock.Mock(check=mock.Mock(return_value=True))
        with mock.patch.object(runner, "inspect_focus", side_effect=fake_inspect), \
                mock.patch.object(runner.repo, "survey_heartbeat"):
            findings, _ = runner._collect_findings(focuses, [{"slug": "api", "dir": Path("/tmp")}], "", budget, "uid")
        self.assertEqual([f["clue_source"] for f in findings], ["codegraph", "baseline"])


class RunWideCodegraphStatusTests(unittest.TestCase):
    def test_every_repo_profile_gets_the_status_checked_once_per_run(self):
        """逐仓库各查一次的话，运行中途环境一变，同一轮里快照、降级与仓库状态就会互相矛盾。"""
        from backend.survey import runner

        seen = []

        def fake_build_profile(slug, repo_dir, index_timeout=600, status=None):
            seen.append(status)
            return _profile(repo_slug=slug, codegraph_status=status)

        targets = [{"slug": "a", "url": "u1"}, {"slug": "b", "url": "u2"}, {"slug": "c", "url": "u3"}]
        status_probe = mock.Mock(side_effect=["missing", "enabled", "enabled"])
        with mock.patch.object(runner, "codegraph_status", status_probe), \
                mock.patch.object(runner, "prepare_repo", return_value=(Path("/tmp"), "main", "sha")), \
                mock.patch.object(runner, "build_profile", side_effect=fake_build_profile), \
                mock.patch.object(runner, "save_profile"), \
                mock.patch.object(runner, "count_files", return_value=1), \
                mock.patch.object(runner, "artifacts_dir", return_value=Path("/tmp")), \
                mock.patch.object(runner, "repo") as fake_repo:
            prepared = runner._prepare_workspaces(
                {"slug": "s"}, "uid", targets, {"index_timeout_seconds": 5, "fetch_timeout_seconds": 5}
            )

        self.assertEqual(status_probe.call_count, 1)
        self.assertEqual(seen, ["missing", "missing", "missing"])
        self.assertEqual(len(prepared), 3)
        # 不可用时只记一次整轮降级，不会再把每个仓库标成 index_failed
        kinds = [c.args[1] for c in fake_repo.add_survey_degradation.call_args_list]
        self.assertEqual(kinds, ["profile_fallback"])


class SurveyConfigEnvOverrideTests(unittest.TestCase):
    """
    巡检配置的环境变量必须能覆盖 config.yaml。

    这几个开关曾经被当成配置文件路径传给 _pick_config_value，环境变量从来没被读过——
    对照实验"关掉 codegraph 跑一轮"因此静默失效，页面上看到的永远是启用状态。
    """

    def _load(self, file_config: dict, env: dict) -> dict:
        from backend.survey import config

        with mock.patch.object(config, "load_file_config", return_value=file_config), \
                mock.patch.dict(os.environ, env):
            return config.load_survey_config()

    def test_env_overrides_file_for_every_survey_switch(self):
        file_config = {"survey": {
            "enabled": True, "codegraph_enabled": True, "scheduler_interval_seconds": 60,
            "workspace_dir": "/from/file", "codegraph_bin": "/file/codegraph",
        }}
        resolved = self._load(file_config, {
            "OPENCR_SURVEY_ENABLED": "false",
            "OPENCR_SURVEY_CODEGRAPH_ENABLED": "false",
            "OPENCR_SURVEY_SCHEDULER_INTERVAL_SECONDS": "120",
            "OPENCR_SURVEY_WORKSPACE_DIR": "/from/env",
            "OPENCR_SURVEY_CODEGRAPH_BIN": "/env/codegraph",
        })
        self.assertFalse(resolved["enabled"])
        self.assertFalse(resolved["codegraph_enabled"])
        self.assertEqual(resolved["scheduler_interval_seconds"], 120)
        self.assertEqual(resolved["workspace_dir"], "/from/env")
        self.assertEqual(resolved["codegraph_bin"], "/env/codegraph")

    def test_file_values_apply_when_env_is_unset_or_blank(self):
        file_config = {"survey": {"codegraph_enabled": False, "scheduler_interval_seconds": 90}}
        env = {"OPENCR_SURVEY_CODEGRAPH_ENABLED": "  ", "OPENCR_SURVEY_SCHEDULER_INTERVAL_SECONDS": ""}
        resolved = self._load(file_config, env)
        self.assertFalse(resolved["codegraph_enabled"])
        self.assertEqual(resolved["scheduler_interval_seconds"], 90)

    def test_invalid_interval_falls_back_to_default_with_a_warning(self):
        """写错的值不能让服务起不来，但也不能悄无声息：要留日志。"""
        from backend.survey.config import DEFAULT_SCHEDULER_INTERVAL_SECONDS

        with self.assertLogs("backend.survey.config", level="WARNING") as logs:
            resolved = self._load({}, {"OPENCR_SURVEY_SCHEDULER_INTERVAL_SECONDS": "5m"})
        self.assertEqual(resolved["scheduler_interval_seconds"], DEFAULT_SCHEDULER_INTERVAL_SECONDS)
        self.assertTrue(any("5m" in line for line in logs.output))

    def test_disabled_codegraph_via_env_is_not_available(self):
        from backend.survey import config, profile

        with mock.patch.object(config, "load_file_config", return_value={"survey": {"codegraph_enabled": True}}), \
                mock.patch.dict(os.environ, {"OPENCR_SURVEY_CODEGRAPH_ENABLED": "false"}):
            self.assertFalse(profile.codegraph_available())


class CodegraphStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="opencr-codegraph-db-")
        os.environ["OPENCR_DATABASE_URL"] = f"sqlite:///{Path(self.tmpdir, 'test.db')}"

        from backend.storage import db

        db.reset_engine_for_tests(os.environ["OPENCR_DATABASE_URL"])

        from backend.storage.models import Base

        Base.metadata.create_all(db.get_engine())

        from backend.storage import repo

        self.repo = repo
        self.survey = repo.create_survey(
            name="周巡检", slug="weekly", schedule_kind="daily", schedule_expr="09:00",
            timezone_name="UTC", sources=[],
        )

    def tearDown(self):
        from backend.storage import db

        db.reset_engine_for_tests()
        os.environ.pop("OPENCR_DATABASE_URL", None)

    def _finding(self, path, clue):
        from backend.survey.common import finding_fingerprint

        return {
            "repo_slug": "api", "file_path": path, "line": 1, "category": "security",
            "severity": "warning", "title": "t", "body": "b", "clue_source": clue,
            "fingerprint": finding_fingerprint("api", path, "security"),
        }

    def _start(self):
        return self.repo.start_survey_run(self.survey["survey_uid"], "manual")

    def test_repo_rows_keep_null_apart_from_zero(self):
        run_uid = self._start()
        self.repo.record_survey_repo(
            run_uid, "api", "u", status="ok", profile_kind="codegraph",
            index={"mode": "sync", "elapsed_ms": 830}, route_count=0, type_count=12,
        )
        self.repo.record_survey_repo(run_uid, "web", "u2", status="ok", profile_kind="manifest")

        repos = self.repo.get_survey_run_detail(run_uid)["repos"]
        self.assertEqual(
            {k: repos[0][k] for k in ("index_mode", "index_ms", "route_count", "type_count")},
            {"index_mode": "sync", "index_ms": 830, "route_count": 0, "type_count": 12},
        )
        self.assertEqual(
            {k: repos[1][k] for k in ("index_mode", "index_ms", "route_count", "type_count")},
            {"index_mode": "", "index_ms": None, "route_count": None, "type_count": None},
        )

    def test_stats_snapshot_round_trips_and_old_runs_have_none(self):
        old_run = self._start()
        self.assertIsNone(self.repo.get_survey_run_detail(old_run)["codegraph_stats"])

        from backend.storage.models import RUN_SUCCEEDED

        self.repo.finish_survey_run(old_run, RUN_SUCCEEDED)
        run_uid = self._start()
        self.repo.set_survey_codegraph_stats(run_uid, {"status": "enabled", "focus": None})
        self.assertEqual(self.repo.get_survey_run_detail(run_uid)["codegraph_stats"],
                         {"status": "enabled", "focus": None})

    def test_clue_counts_exclude_ignored_findings_and_separate_old_rows(self):
        run_uid = self._start()
        ignored = self._finding("a.go", "codegraph")
        self.repo.add_survey_ignore(self.survey["survey_uid"], ignored["fingerprint"])
        self.repo.record_survey_findings(run_uid, [
            ignored,
            self._finding("b.go", "codegraph"),
            self._finding("c.go", "baseline"),
            self._finding("d.go", ""),
        ])

        detail = self.repo.get_survey_run_detail(run_uid, include_body=False)
        expected = {"codegraph": 1, "baseline": 1, "unlisted": 0, "recheck": 0, "unknown": 1}
        self.assertEqual(detail["clue_counts"], expected)
        # 线索来源是标签不是正文，Guest 视角同样可见
        self.assertEqual([f["clue_source"] for f in detail["findings"]], ["codegraph", "baseline", ""])

        listed = self.repo.list_survey_runs(self.survey["survey_uid"])
        self.assertEqual(listed[0]["clue_counts"], expected)

        # 入库之后才被忽略的条目同样不计入，与报告列表在读取时隐藏它们保持一致
        self.repo.add_survey_ignore(self.survey["survey_uid"], self._finding("b.go", "codegraph")["fingerprint"])
        expected["codegraph"] = 0
        self.assertEqual(self.repo.get_survey_run_detail(run_uid)["clue_counts"], expected)
        self.assertEqual(self.repo.list_survey_runs(self.survey["survey_uid"])[0]["clue_counts"], expected)

    def test_guest_does_not_see_raw_repo_errors(self):
        """codegraph / git 的原始输出可能带出路径与代码片段，与推送错误同等对待。"""
        run_uid = self._start()
        self.repo.record_survey_repo(
            run_uid, "api", "u", status="index_failed", profile_kind="manifest",
            error_message="exit=1 parse error in src/secret.ts", index={"mode": "failed", "elapsed_ms": 3},
        )
        self.assertIn("secret", self.repo.get_survey_run_detail(run_uid)["repos"][0]["error_message"])
        guest = self.repo.get_survey_run_detail(run_uid, include_body=False)["repos"][0]
        self.assertEqual(guest["error_message"], "")
        self.assertEqual(guest["index_mode"], "failed")

    def test_markdown_report_has_codegraph_section_only_with_a_snapshot(self):
        from backend.survey.report import render_run_markdown

        run_uid = self._start()
        self.assertNotIn("## codegraph", render_run_markdown(self.repo.get_survey_run_detail(run_uid)))

        self.repo.record_survey_repo(
            run_uid, "api", "u", status="ok", profile_kind="codegraph",
            index={"mode": "init", "elapsed_ms": 1500}, route_count=3, type_count=4,
        )
        self.repo.record_survey_repo(
            run_uid, "web", "u2", status="index_failed", profile_kind="manifest",
            error_message="exit=2 boom", index={"mode": "failed", "elapsed_ms": 10},
        )
        self.repo.set_survey_codegraph_stats(run_uid, {
            "status": "enabled", "repos_total": 2, "repos_structured": 1,
            "repos_empty": 0, "routes": 3, "types": 4, "dropped_registrations": 1,
            "cross_repo": {"links": 2, "method_mismatch": 0, "orphan_calls": 5, "unused_routes": 1},
            "focus": {"codegraph": 3, "baseline": 1, "unlisted": 0},
        })
        markdown = render_run_markdown(self.repo.get_survey_run_detail(run_uid))
        self.assertIn("## codegraph", markdown)
        self.assertIn("结构图画像的仓库 1 / 2，接口 3 个，类型 4 个", markdown)
        self.assertIn("| api | 全量重建 | 1.5s | 3 | 4 | - |", markdown)
        self.assertIn("| web | 失败 | 0.0s | - | - | exit=2 boom |", markdown)
        self.assertIn("接口连接 2 处", markdown)
        self.assertIn("范围内无人提供的调用 5 处", markdown)
        self.assertIn("关注点线索来源：codegraph 3、基础画像 1；codegraph 占 L1 点名的 75%", markdown)
        # 导出与界面同源：Guest 导出同样看不到原始报错
        guest = render_run_markdown(self.repo.get_survey_run_detail(run_uid, include_body=False))
        self.assertNotIn("boom", guest)
        self.assertIn("| web | 失败 | 0.0s | - | - | 建索引失败 |", guest)

    def test_markdown_findings_carry_their_clue_source(self):
        """导出与页面同源：页面发现列表有「线索来源」列，导出的每条发现也要有。"""
        from backend.survey.report import render_run_markdown

        run_uid = self._start()
        self.repo.record_survey_findings(run_uid, [self._finding("a.go", "codegraph"), self._finding("b.go", "")])
        for include_body in (True, False):
            markdown = render_run_markdown(self.repo.get_survey_run_detail(run_uid, include_body=include_body))
            self.assertIn("[安全·线索：codegraph] `api/a.go`", markdown)
            self.assertIn("[安全·线索：无记录] `api/b.go`", markdown)

    def test_markdown_tells_missing_binary_apart_from_switched_off(self):
        from backend.survey.report import render_run_markdown

        run_uid = self._start()
        base = {"cross_repo": {}, "focus": None}
        self.repo.set_survey_codegraph_stats(run_uid, {**base, "status": "missing"})
        missing = render_run_markdown(self.repo.get_survey_run_detail(run_uid))
        self.assertIn("找不到可执行文件", missing)
        # focus 为 None（L1 没跑到）时写明原因，与页面一致
        self.assertIn("关注点线索来源：本轮未走到整合分析这一步", missing)
        self.assertNotIn("配置关闭", missing)
        self.repo.set_survey_codegraph_stats(run_uid, {**base, "status": "disabled"})
        disabled = render_run_markdown(self.repo.get_survey_run_detail(run_uid))
        self.assertIn("未启用 codegraph（配置关闭）", disabled)
        self.assertNotIn("找不到可执行文件", disabled)

    def test_markdown_share_excludes_rechecks(self):
        """与页面同口径：台账复核不进分母，否则台账越大 codegraph 占比越被稀释。"""
        from backend.survey.report import render_run_markdown

        run_uid = self._start()
        self.repo.set_survey_codegraph_stats(run_uid, {
            "status": "enabled", "cross_repo": {},
            "focus": {"codegraph": 1, "baseline": 1, "unlisted": 0, "recheck": 8},
        })
        markdown = render_run_markdown(self.repo.get_survey_run_detail(run_uid))
        self.assertIn("台账复核 8；codegraph 占 L1 点名的 50%", markdown)
        # 没有 L1 点名的发现时不写占比，而不是写成 0%
        self.assertNotIn("发现线索来源：（无）；", markdown)

    def test_markdown_mentions_empty_extraction_only_when_there_is_some(self):
        """与报告页一致：为 0 时不提，否则未启用的运行也会读到一句多余的"其中抽取为空 0 个"。"""
        from backend.survey.report import render_run_markdown

        run_uid = self._start()
        base = {"status": "enabled", "repos_total": 2, "repos_structured": 2, "cross_repo": {}, "focus": None}
        self.repo.set_survey_codegraph_stats(run_uid, {**base, "repos_empty": 0})
        self.assertNotIn("抽取为空", render_run_markdown(self.repo.get_survey_run_detail(run_uid)))
        self.repo.set_survey_codegraph_stats(run_uid, {**base, "repos_empty": 1})
        self.assertIn("2 / 2（其中抽取为空 1 个）", render_run_markdown(self.repo.get_survey_run_detail(run_uid)))


if __name__ == "__main__":
    unittest.main()
