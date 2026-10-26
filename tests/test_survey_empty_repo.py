"""空仓库的测试。

新建后还没推过任何提交的仓库没有代码可巡检，但它不是拉取失败：
记成拉取失败会每轮多一条降级、让人以为要去处理，全部仓库都为空时报错又说不清原因。
拉取用真实的 git 与本地裸仓库，空仓库在哪一步失败取决于 git 自己的行为，mock 掉就测不到了。
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.survey import workspace  # noqa: E402,F401  先导入，mock.patch 才能按路径找到它
from tests.test_survey_destinations import LedgerStorageTestCase, _finding  # noqa: E402


def _git(cwd, *args):
    """在 cwd 里执行 git，不读本机的全局与系统配置。"""
    env = {
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        "GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "t@x.com",
        "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "t@x.com",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


class PrepareEmptyRepoTests(unittest.TestCase):
    """prepare_repo 对空仓库的识别。"""

    def setUp(self):
        """工作区放进临时目录，平台配置不带令牌；远端是一个默认分支为 main 的空裸仓库。"""
        self.root = Path(tempfile.mkdtemp(prefix="opencr-empty-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for target, value in (
            ("backend.survey.workspace.load_gitlab_config", {"token": ""}),
            ("backend.survey.workspace.workspace_root", self.root / "ws"),
        ):
            patch = mock.patch(target, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        self.bare = self.root / "app.git"
        _git(self.root, "init", "-q", "--bare", "--initial-branch=main", str(self.bare))
        self.url = f"file://{self.bare}"

    def _repo_dir(self):
        """prepare_repo 会用到的工作区路径。"""
        from backend.survey.common import repo_slug_from_url
        from backend.survey.workspace import survey_workspace_dir

        return survey_workspace_dir("weekly") / repo_slug_from_url(self.url)

    def _push_content(self):
        """往远端推一个提交，让它不再是空仓库。"""
        src = self.root / "src"
        _git(self.root, "init", "-q", "--initial-branch=main", str(src))
        (src / "main.py").write_text("print(1)\n")
        _git(src, "add", ".")
        _git(src, "commit", "-q", "-m", "init")
        _git(src, "push", "-q", str(self.bare), "main")
        return _git(src, "rev-parse", "HEAD").strip()

    def test_named_branch_on_empty_repo(self):
        """组织展开时 GitLab 给空仓库也填了默认分支，clone --branch 报 Remote branch not found。"""
        from backend.survey.workspace import EmptyRepoError, prepare_repo

        with self.assertRaises(EmptyRepoError):
            prepare_repo("weekly", self.url, "master")
        self.assertFalse(self._repo_dir().exists())

    def test_default_branch_on_empty_repo(self):
        """没指定分支时 clone 会成功，到取默认分支才失败，同样要认出是空仓库，且不留下那份空克隆。"""
        from backend.survey.workspace import EmptyRepoError, prepare_repo

        with self.assertRaises(EmptyRepoError):
            prepare_repo("weekly", self.url)
        self.assertFalse(self._repo_dir().exists())

    def test_leftover_empty_clone_recovers_once_repo_has_content(self):
        """之前的版本会留下一份没有任何提交的克隆；仓库有了内容后直接在它上面拉取也要成功。"""
        from backend.survey.workspace import prepare_repo

        repo_dir = self._repo_dir()
        repo_dir.parent.mkdir(parents=True)
        _git(self.root, "clone", "-q", self.url, str(repo_dir))
        sha = self._push_content()
        _dir, branch, head = prepare_repo("weekly", self.url)
        self.assertEqual((branch, head), ("main", sha))

    def test_missing_branch_on_repo_with_content_is_still_a_fetch_failure(self):
        """有分支、只是指定的分支不存在，是配置写错了，要照常报拉取失败让人去改。"""
        from backend.survey.common import SurveyError
        from backend.survey.workspace import EmptyRepoError, prepare_repo

        self._push_content()
        with self.assertRaises(SurveyError) as ctx:
            prepare_repo("weekly", self.url, "master")
        self.assertNotIsInstance(ctx.exception, EmptyRepoError)
        self.assertIn("master", str(ctx.exception))

    def test_tag_only_repo_is_not_empty(self):
        """只有 tag、没有分支的仓库是有内容的，拉取失败要照常报出来。"""
        from backend.survey.common import SurveyError
        from backend.survey.workspace import EmptyRepoError, prepare_repo

        self._push_content()
        _git(self.bare, "tag", "v1", "main")
        _git(self.bare, "update-ref", "-d", "refs/heads/main")
        with self.assertRaises(SurveyError) as ctx:
            prepare_repo("weekly", self.url)
        self.assertNotIsInstance(ctx.exception, EmptyRepoError)

    def test_unreachable_remote_is_still_a_fetch_failure(self):
        """问远端也失败时不能因为 ls-remote 没输出就当成空仓库。"""
        from backend.survey.common import SurveyError
        from backend.survey.workspace import EmptyRepoError, prepare_repo

        self.url = f"file://{self.root / 'missing.git'}"
        with self.assertRaises(SurveyError) as ctx:
            prepare_repo("weekly", self.url)
        self.assertNotIsInstance(ctx.exception, EmptyRepoError)


class EmptyRepoRunTests(LedgerStorageTestCase):
    """空仓库在运行记录、报告与台账里的样子。"""

    def _prepare(self, side_effect):
        """新开一轮运行（run_uid 存进 self.run_uid），用给定的 prepare_repo 行为跑 _prepare_workspaces，返回成功的仓库列表。"""
        from backend.survey import runner

        self.run_uid = self.repo.start_survey_run(self.survey["survey_uid"], "manual")
        targets = [{"slug": "empty", "url": "https://g.com/a/empty.git", "branch": "master"},
                   {"slug": "app", "url": "https://g.com/a/app.git"}][: len(side_effect)]
        with mock.patch.object(runner, "codegraph_status", return_value="enabled"), \
                mock.patch.object(runner, "prepare_repo", side_effect=side_effect), \
                mock.patch.object(runner, "build_profile", return_value={"kind": "codegraph", "routes": [], "types": []}), \
                mock.patch.object(runner, "save_profile"), \
                mock.patch.object(runner, "count_files", return_value=1), \
                mock.patch.object(runner, "artifacts_dir", return_value=Path("/tmp")):
            return runner._prepare_workspaces(
                self.survey, self.run_uid, targets, {"index_timeout_seconds": 5, "fetch_timeout_seconds": 5}
            )

    def test_empty_repo_is_skipped_without_degradation(self):
        """空仓库记一行 empty、带上配置的分支，其余仓库照常处理，整轮不记降级。"""
        from backend.survey.workspace import EmptyRepoError

        prepared = self._prepare([EmptyRepoError("仓库为空"), (Path("/tmp"), "main", "sha")])
        self.assertEqual([p["slug"] for p in prepared], ["app"])
        detail = self.repo.get_survey_run_detail(self.run_uid)
        self.assertEqual(
            sorted((r["repo_slug"], r["status"], r["branch"]) for r in detail["repos"]),
            [("app", "ok", "main"), ("empty", "empty", "master")],
        )
        self.assertEqual(detail["degradations"], [])

    def test_all_empty_fails_with_reason(self):
        """全部是空仓库时判失败并说清原因；混着拉取失败时也要说出有几个只是空仓库。"""
        from backend.survey.common import SurveyError
        from backend.survey.workspace import EmptyRepoError

        with self.assertRaises(SurveyError) as ctx:
            self._prepare([EmptyRepoError("仓库为空")])
        self.assertIn("全部 1 个仓库都是空仓库", str(ctx.exception))
        with self.assertRaises(SurveyError) as ctx:
            self._prepare([EmptyRepoError("仓库为空"), SurveyError("git clone 失败")])
        self.assertIn("1 个是空仓库，其余拉取失败", str(ctx.exception))

    def test_report_and_ledger(self):
        """空仓库列入未参与的仓库；它名下上一轮的问题算本轮未复查并注明原因，台账行保持存在。"""
        from backend.survey.report import render_run_markdown

        self._complete_run([_finding("lib/a.dart", repo="gone")], repos=(("gone", "ok"), ("app", "ok")))
        run_uid = self._complete_run([], repos=(("gone", "empty"), ("app", "ok")))
        detail = self.repo.get_survey_run_detail(run_uid)
        self.assertEqual([(f["repo_slug"], f["repo_status"]) for f in detail["unchecked_findings"]], [("gone", "empty")])
        self.assertEqual(detail["resolved_findings"], [])
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "present")

        markdown = render_run_markdown(detail)
        self.assertIn("- gone（空仓库，还没有任何提交）", markdown)
        self.assertNotIn("| gone |", markdown)
        self.assertIn("[仓库为空]", markdown)


if __name__ == "__main__":
    unittest.main()
