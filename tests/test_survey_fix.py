"""巡检修复（FixRun）测试。

重点覆盖几类"坏掉了也不会报错"的约束：
1. 修改套用 —— 原文必须恰好出现一次，套不上要当场发现，不能静默删代码；CRLF 文件不能被整个改成 LF；
2. 原始作者 —— 只改格式的提交要被跳过，指派给真正写那段逻辑的人；作者不可用时退回仓库负责人；
3. 验证不过不提合并请求 —— 第二轮才说"不用改"不能当成问题已不存在；
4. 合并请求不改变台账状态，但链接要出现在台账镜像、报告与 Markdown 导出里；Guest 看不到修复记录。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.test_survey_destinations import LedgerStorageTestCase, _finding  # noqa: E402

PLATFORM = {"type": "gitlab", "url": "https://g.com", "token": ""}


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

class ApplyEditsTests(unittest.TestCase):
    """模型给出的"原文 → 替换文"如何套用到文件上。"""

    def test_applies_unique_edits_in_order(self):
        """逐条套用，后一条看到的是前一条改过的文本。"""
        from backend.survey.fix import apply_edits

        text = "a = 1\nb = 2\n"
        edits = [{"search": "a = 1", "replace": "a = 10"}, {"search": "a = 10\nb", "replace": "a = 10\nc"}]
        self.assertEqual(apply_edits(text, edits), "a = 10\nc = 2\n")

    def test_rejects_ambiguous_missing_and_noop(self):
        """原文出现多次或找不到都要失败：猜一个位置改，等于让模型的笔误落进代码里。"""
        from backend.survey.fix import EditError, apply_edits

        with self.assertRaisesRegex(EditError, "出现了 2 次"):
            apply_edits("x\nx\n", [{"search": "x", "replace": "y"}])
        with self.assertRaisesRegex(EditError, "出现了 0 次"):
            apply_edits("x\n", [{"search": "z", "replace": "y"}])
        with self.assertRaisesRegex(EditError, "没有带来任何变化"):
            apply_edits("x\n", [{"search": "x", "replace": "x"}])
        with self.assertRaises(EditError):
            apply_edits("x\n", [])
        with self.assertRaises(EditError):
            apply_edits("x\n", [{"search": "", "replace": "y"}])


class StaticChecksTests(unittest.TestCase):
    """不需要目标仓库工具链的检查。"""

    def test_python_and_json_syntax(self):
        """Python 与 JSON 用解释器自带的解析器检查语法。"""
        from backend.survey.fix import static_checks

        broken = {c["name"]: c for c in static_checks("a.py", "x = 1\n", "x = (\n")}
        self.assertFalse(broken["Python 语法"]["passed"])
        ok = {c["name"]: c for c in static_checks("a.json", "{}", '{"a": 1}')}
        self.assertTrue(ok["JSON 语法"]["passed"])
        # 没有自带解析器的语言只检查改动范围
        self.assertEqual([c["name"] for c in static_checks("a.dart", "a", "b")], ["改动范围"])

    def test_change_size_cap(self):
        """改动超过上限不通过：修一个问题改几百行，基本是模型在重写文件。"""
        from backend.survey.fix import MAX_CHANGED_LINES, static_checks

        old = "".join(f"line{i}\n" for i in range(MAX_CHANGED_LINES + 10))
        new = old.replace("line", "row")
        self.assertFalse(static_checks("a.txt", old, new)[0]["passed"])

    def test_verdict_needs_checks_and_review(self):
        """检查全部通过且模型复核没有问题才算通过。"""
        from backend.survey.fix import verdict_passed

        self.assertTrue(verdict_passed({"checks": [{"passed": True}], "problems": []}))
        self.assertFalse(verdict_passed({"checks": [{"passed": False}], "problems": []}))
        self.assertFalse(verdict_passed({"checks": [{"passed": True}], "problems": ["引入了空指针"]}))


class ProjectPathTests(unittest.TestCase):
    """从仓库地址取平台上的项目路径。"""

    def test_forms(self):
        """https、scp 风格与部署在子路径下的平台。"""
        from backend.survey.fix import project_path_from_url

        self.assertEqual(project_path_from_url("https://g.com/grp/sub/app.git"), "grp/sub/app")
        self.assertEqual(project_path_from_url("git@g.com:grp/app.git"), "grp/app")
        # 平台部署在子路径下：子路径不能被当成组织
        self.assertEqual(project_path_from_url("https://h.com/gitlab/grp/app.git", "https://h.com/gitlab"), "grp/app")


# 令牌所属账号在平台上的信息，修复提交默认署这个账号
TOKEN_ACCOUNT = {"id": 100, "name": "Fix Bot", "username": "fixbot", "email": "bot@corp.com",
                 "commit_email": "100-fixbot@users.noreply.g.com", "public_email": "public@corp.com"}


class MrTitleTests(unittest.TestCase):
    """合并请求标题的前缀。"""

    def test_prefix_and_truncation(self):
        """有前缀时以空格隔开；空前缀只留问题标题；超出平台上限的部分截掉。"""
        from backend.survey.fix import mr_title

        self.assertEqual(mr_title("[OpenCR]", "空指针"), "[OpenCR] 空指针")
        self.assertEqual(mr_title("  ", "空指针"), "空指针")
        self.assertEqual(len(mr_title("[Bot]", "长" * 400)), 255)


class CommitAuthorTests(unittest.TestCase):
    """修复提交的作者：配置优先，其次令牌账号，最后兜底身份。"""

    def test_follows_token_account_by_default(self):
        """没配置时跟随令牌账号，邮箱优先取提交邮箱。"""
        from backend.survey.fix import pick_commit_author

        self.assertEqual(pick_commit_author("", "", TOKEN_ACCOUNT), ("Fix Bot", "100-fixbot@users.noreply.g.com"))

    def test_configured_fields_win_independently(self):
        """名字与邮箱各自取值：只配了一项时另一项仍跟随令牌账号。"""
        from backend.survey.fix import pick_commit_author

        self.assertEqual(pick_commit_author(" Bot ", "", TOKEN_ACCOUNT), ("Bot", "100-fixbot@users.noreply.g.com"))
        self.assertEqual(pick_commit_author("", "q@corp.com", TOKEN_ACCOUNT), ("Fix Bot", "q@corp.com"))

    def test_account_field_fallbacks(self):
        """账号缺名字时用用户名；提交邮箱缺失或不像邮箱时依次退到主邮箱、公开邮箱，都没有时用兜底身份。"""
        from backend.survey.fix import FALLBACK_COMMIT_AUTHOR, pick_commit_author

        account = {"name": "  ", "username": "fixbot", "commit_email": "", "email": "not-an-email", "public_email": "p@corp.com"}
        self.assertEqual(pick_commit_author("", "", account), ("fixbot", "p@corp.com"))
        self.assertEqual(pick_commit_author("", "", {}), FALLBACK_COMMIT_AUTHOR)

    def test_resolve_reads_config_and_skips_platform_when_fully_configured(self):
        """两项都配置了时不访问平台；查账号失败时退回兜底身份而不是让修复失败。"""
        from backend.survey import fix

        configured = {"fix_commit_name": "Bot", "fix_commit_email": "bot@corp.com"}
        with mock.patch.object(fix, "load_survey_config", return_value=configured), \
                mock.patch.object(fix, "get_current_user") as current:
            self.assertEqual(fix.resolve_commit_author(), ("Bot", "bot@corp.com"))
        current.assert_not_called()
        with mock.patch.object(fix, "load_survey_config", return_value={"fix_commit_name": "", "fix_commit_email": ""}), \
                mock.patch.object(fix, "get_current_user", side_effect=RuntimeError("401")):
            self.assertEqual(fix.resolve_commit_author(), fix.FALLBACK_COMMIT_AUTHOR)

    def test_config_keys_and_env_override(self):
        """配置项从 survey 段读取，环境变量覆盖，首尾空白去掉。"""
        from backend.survey import config

        file_config = {"survey": {"fix_commit_name": " Bot ", "fix_commit_email": "bot@corp.com"}}
        with mock.patch.object(config, "load_file_config", return_value=file_config), \
                mock.patch.dict(os.environ, {"OPENCR_SURVEY_FIX_COMMIT_EMAIL": "env@corp.com"}):
            os.environ.pop("OPENCR_SURVEY_FIX_COMMIT_NAME", None)
            cfg = config.load_survey_config()
        self.assertEqual((cfg["fix_commit_name"], cfg["fix_commit_email"]), ("Bot", "env@corp.com"))


class AuthorshipTests(unittest.TestCase):
    """原始作者追溯里的纯函数。"""

    def test_formatting_only_lines(self):
        """只改空白、折行、引号、尾逗号的改动块算只改格式；同一次提交里的实质改动不算。"""
        from backend.survey.assignee import formatting_only_lines

        self.assertEqual(formatting_only_lines("f(a,b)\n", "f(\n  a,\n  b,\n)\n"), {1, 2, 3, 4})
        self.assertEqual(formatting_only_lines("x = 'a'\n", 'x = "a"\n'), {1})
        self.assertEqual(formatting_only_lines("x = a + b\n", "x = a - b\n"), set())
        # 删掉一部分代码也是实质改动
        self.assertEqual(formatting_only_lines("x = a + b\n", "x = a\n"), set())
        # 第 1 行只改格式、第 3 行改了逻辑：只有第 1 行算
        old = "f(a,b)\nkeep\ny = 1\n"
        new = "f(a, b)\nkeep\ny = 2\n"
        self.assertEqual(formatting_only_lines(old, new), {1})

    def test_split_reformat_is_merged(self):
        """折行被逐行比对拆成几块时，合并相邻的块再比。"""
        from backend.survey.assignee import formatting_only_lines

        old = "call(a, b)\nx = 1\n)\n"
        new = "call(\n    a,\n    b,\n)\nx = 1\n)\n"
        self.assertTrue({1, 2, 3, 4} <= formatting_only_lines(old, new))

    def test_formatting_next_to_logic_change(self):
        """格式化紧挨着一处逻辑改动：只有改了逻辑的那一行不算。"""
        from backend.survey.assignee import formatting_only_lines

        old = "    return sum(i['price'] for i in items)\nLIMIT = 1\n"
        new = '    return sum(\n        i["price"] for i in items\n    )\nLIMIT = 2\n'
        self.assertEqual(formatting_only_lines(old, new), {1, 2, 3})

    def test_changed_lines(self):
        """修改涉及的原文件行号。"""
        from backend.survey.assignee import changed_lines, to_ranges

        old = "a\nb\nc\nd\n"
        self.assertEqual(changed_lines(old, "a\nB\nc\nd\n"), [2])
        self.assertEqual(changed_lines(old, "a\nc\nd\n"), [2])
        # 纯插入取插入点前一行：插进来的代码修的是它上面那段逻辑
        self.assertEqual(changed_lines(old, "a\nb\nnew\nc\nd\n"), [2])
        self.assertEqual(changed_lines(old, "new\na\nb\nc\nd\n"), [1])
        self.assertEqual(to_ranges([5, 1, 2, 3, 7, 6]), [(1, 3), (5, 7)])

    def test_pick_author_by_lines_then_recency(self):
        """贡献行数最多的人；行数相同取最近改过的。邮箱大小写不同算同一个人。"""
        from backend.survey.assignee import pick_author

        entries = [
            {"author_name": "A", "author_email": "a@x.com", "author_time": 1},
            {"author_name": "A", "author_email": "A@x.com", "author_time": 2},
            {"author_name": "B", "author_email": "b@x.com", "author_time": 9},
        ]
        self.assertEqual(pick_author(entries)["name"], "A")
        tie = [entries[0], {"author_name": "B", "author_email": "b@x.com", "author_time": 9}]
        self.assertEqual(pick_author(tie)["name"], "B")
        self.assertIsNone(pick_author([]))


class AssigneeSelectionTests(unittest.TestCase):
    """把原始作者对应到平台账号，不可用且没有配置仓库负责人时不指派。"""

    def setUp(self):
        """这一组不建库：没有仓库负责人配置。不 mock 的话会去读开发机上真实的数据库。"""
        patch = mock.patch("backend.survey.assignee.repo.find_repo_owner", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    MEMBERS = [
        {"id": 1, "username": "alice", "name": "Alice", "state": "active", "access_level": 30},
        {"id": 2, "username": "bob", "name": "Bob", "state": "blocked", "access_level": 40},
        {"id": 3, "username": "carol", "name": "Carol", "state": "active", "access_level": 40},
        {"id": 4, "username": "dave", "name": "Dave", "state": "active", "access_level": 50},
        {"id": 5, "username": "project_9_bot_x", "name": "bot", "state": "active", "access_level": 50},
        {"id": 6, "username": "guest", "name": "Same", "state": "active", "access_level": 10},
        {"id": 7, "username": "same1", "name": "Same", "state": "active", "access_level": 30},
        {"id": 8, "username": "newbie", "name": "Newbie", "state": "active", "access_level": 30,
         "membership_state": "awaiting"},
    ]

    def test_match_member(self):
        """按邮箱搜到的账号、邮箱前缀、唯一的显示名依次尝试；同名多人时宁可对应不上。"""
        from backend.survey.assignee import match_member

        # 按邮箱搜到的账号优先
        account, member = match_member(
            {"name": "x", "email": "Alice.W@corp.com"}, self.MEMBERS, [{"id": 1, "email": "alice.w@corp.com"}]
        )
        self.assertEqual((account["id"], member), (1, True))
        # 搜到了账号、但不在项目成员里
        account, member = match_member({"name": "x", "email": "z@corp.com"}, self.MEMBERS, [{"id": 99, "public_email": "z@corp.com"}])
        self.assertEqual((account["id"], member), (99, False))
        # 邮箱前缀等于用户名
        self.assertEqual(match_member({"name": "?", "email": "carol@corp.com"}, self.MEMBERS, [])[0]["id"], 3)
        # 显示名唯一时才认
        self.assertEqual(match_member({"name": "Dave", "email": ""}, self.MEMBERS, [])[0]["id"], 4)
        self.assertEqual(match_member({"name": "Same", "email": ""}, self.MEMBERS, []), (None, False))

    def test_unavailable_reasons(self):
        """封禁、不在项目里、权限不足、成员资格未生效、是 OpenCR 自己，都不能当处理人。"""
        from backend.survey.assignee import unavailable_reason

        self.assertEqual(unavailable_reason(self.MEMBERS[0], True, None), "")
        self.assertIn("blocked", unavailable_reason(self.MEMBERS[1], True, None))
        self.assertIn("不在项目成员中", unavailable_reason({"id": 9, "state": "active"}, False, None))
        self.assertIn("Developer", unavailable_reason(self.MEMBERS[5], True, None))
        self.assertIn("OpenCR", unavailable_reason(self.MEMBERS[0], True, 1))
        self.assertIn("无法对应", unavailable_reason(None, False, None))
        self.assertIn("尚未生效", unavailable_reason(self.MEMBERS[7], True, None))

    def test_resolve_leaves_unassigned_without_config(self):
        """原始作者可用就指派给他；被封禁或追溯不到、又没有配置仓库负责人时不指派，
        即使项目里有可用的 Maintainer 与 Owner 也不按权限猜一个人，并写明原因。"""
        from backend.storage.models import ASSIGNEE_AUTHOR
        from backend.survey import assignee

        with mock.patch.object(assignee, "list_project_members", return_value=self.MEMBERS), \
                mock.patch.object(assignee, "get_current_user", return_value={"id": 100}), \
                mock.patch.object(assignee, "search_users", return_value=[]):
            ok = assignee.resolve_assignee("grp/app", {"name": "Alice", "email": "alice@corp.com"})
            blocked = assignee.resolve_assignee("grp/app", {"name": "Bob", "email": "bob@corp.com"})
            unknown = assignee.resolve_assignee("grp/app", None)
        self.assertEqual((ok["source"], ok["username"]), (ASSIGNEE_AUTHOR, "alice"))
        self.assertEqual((blocked["source"], blocked["id"], blocked["username"]), ("", None, ""))
        self.assertIn("Bob", blocked["note"])
        self.assertIn("blocked", blocked["note"])
        self.assertIn("没有为这个仓库配置负责人，未指派处理人", blocked["note"])
        self.assertEqual((unknown["source"], unknown["id"]), ("", None))
        self.assertIn("没有追溯到原始作者", unknown["note"])

    def test_members_failure_leaves_unassigned(self):
        """读不到成员时不指派，但不抛：处理人不该让整个修复失败。报错原文不进 note（它会进合并请求说明）。"""
        from backend.survey import assignee

        with mock.patch.object(assignee, "list_project_members", side_effect=RuntimeError("403 internal-host")):
            result = assignee.resolve_assignee("grp/app", {"name": "A", "email": "a@x.com"})
        self.assertEqual((result["source"], result["id"]), ("", None))
        self.assertNotIn("internal-host", result["note"])


class AttachFixesTests(unittest.TestCase):
    """修复挂到报告里的哪条发现上。"""

    def test_matches_same_issue_across_runs(self):
        """按 finding_id 或"同指纹 + 同标题"挂；同指纹、换了标题的另一个问题不挂。"""
        from backend.survey.fix import attach_fixes

        fixes = [
            {"run_uid": "f1", "finding_id": 1, "fingerprint": "fp", "title": "空指针"},
            {"run_uid": "f2", "finding_id": 99, "fingerprint": "fp", "title": " 空指针 "},
        ]
        findings = [
            {"id": 1, "fingerprint": "fp", "title": "空指针"},
            {"id": 2, "fingerprint": "fp", "title": "另一个问题"},
            {"id": 3, "fingerprint": "other", "title": "空指针"},
        ]
        attach_fixes(findings, fixes)
        self.assertEqual([f["run_uid"] for f in findings[0]["fixes"]], ["f1", "f2"])
        self.assertEqual(findings[1]["fixes"], [])
        self.assertEqual(findings[2]["fixes"], [])


# ---------------------------------------------------------------------------
# git：跳过只改格式的提交
# ---------------------------------------------------------------------------

def _git(cwd, *args, author=("Tester", "t@x.com")):
    """在 cwd 里以指定作者身份执行 git。"""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": author[0], "GIT_COMMITTER_EMAIL": author[1],
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


ALICE = ("Alice", "alice@corp.com")
BOB = ("Bob", "bob@corp.com")
ORIGINAL = "def total(items):\n    return sum(i['price'] for i in items)\n"
REFORMATTED = 'def total(items):\n    return sum(\n        i["price"] for i in items\n    )\n'


def _make_history(root: Path) -> Path:
    """建一个仓库：Alice 写了 calc.py，Bob 只把它格式化了一遍。返回仓库目录。"""
    repo_dir = root / "src"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q", "-b", "main")
    (repo_dir / "calc.py").write_text(ORIGINAL)
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-q", "-m", "add total", author=ALICE)
    (repo_dir / "calc.py").write_text(REFORMATTED)
    _git(repo_dir, "commit", "-q", "-am", "format", author=BOB)
    return repo_dir


class FindOriginalAuthorTests(unittest.TestCase):
    """在真实的 git 历史上追溯原始作者。"""

    def setUp(self):
        """临时目录；git 命令不带平台令牌。"""
        self.root = Path(tempfile.mkdtemp(prefix="opencr-fix-git-"))
        patch = mock.patch("backend.survey.workspace.load_gitlab_config", return_value=PLATFORM)
        patch.start()
        self.addCleanup(patch.stop)

    def test_skips_formatting_commit(self):
        """Bob 只做了格式化，blame 却会把每一行都归给他；原始作者应当是 Alice。"""
        from backend.survey.assignee import find_original_author
        from backend.survey.workspace import git_blame

        repo_dir = _make_history(self.root)
        naive = {e["author_name"] for e in git_blame(repo_dir, "calc.py", [(2, 4)], [])}
        self.assertEqual(naive, {"Bob"})
        self.assertEqual(find_original_author(repo_dir, "calc.py", [2, 3, 4])["name"], "Alice")

    def test_substantive_change_is_kept(self):
        """格式化之后又有人改了逻辑，那一行归给改逻辑的人。"""
        from backend.survey.assignee import find_original_author

        repo_dir = _make_history(self.root)
        (repo_dir / "calc.py").write_text(REFORMATTED.replace("sum(", "max(", 1))
        _git(repo_dir, "commit", "-q", "-am", "use max", author=BOB)
        self.assertEqual(find_original_author(repo_dir, "calc.py", [2])["name"], "Bob")

    def test_mixed_formatting_commit(self):
        """格式化提交里顺手改了别处的逻辑：被格式化的那几行仍归给原作者。"""
        from backend.survey.assignee import find_original_author

        repo_dir = self.root / "mixed"
        repo_dir.mkdir()
        _git(repo_dir, "init", "-q", "-b", "main")
        (repo_dir / "calc.py").write_text(ORIGINAL + "LIMIT = 1\n")
        _git(repo_dir, "add", ".")
        _git(repo_dir, "commit", "-q", "-m", "add", author=ALICE)
        (repo_dir / "calc.py").write_text(REFORMATTED + "LIMIT = 2\n")
        _git(repo_dir, "commit", "-q", "-am", "format and bump", author=BOB)
        self.assertEqual(find_original_author(repo_dir, "calc.py", [3])["name"], "Alice")
        self.assertEqual(find_original_author(repo_dir, "calc.py", [5])["name"], "Bob")

    def test_blame_ignore_revs_file(self):
        """仓库自带的 .git-blame-ignore-revs 生效，里面解析不到的条目不让 blame 失败。"""
        from backend.survey.workspace import read_blame_ignore_revs

        repo_dir = _make_history(self.root)
        head = _git(repo_dir, "rev-parse", "HEAD").strip()
        (repo_dir / ".git-blame-ignore-revs").write_text(f"# formatting\n{head}\n{'f' * 40}\n")
        self.assertEqual(read_blame_ignore_revs(repo_dir), [head])


# ---------------------------------------------------------------------------
# 持久化与执行
# ---------------------------------------------------------------------------

class FixTestCase(LedgerStorageTestCase):
    """建一轮带发现的运行，并准备一个可推送的本地远端仓库。"""

    def setUp(self):
        """平台配置指向 GitLab、不带令牌；工作区放进临时目录。"""
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="opencr-fix-"))
        for target in ("backend.survey.workspace.load_gitlab_config", "backend.survey.fix.load_gitlab_config",
                       "backend.admin.routes.load_gitlab_config"):
            patch = mock.patch(target, return_value=PLATFORM)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch("backend.survey.workspace.workspace_root", return_value=self.root / "ws")
        patch.start()
        self.addCleanup(patch.stop)
        # 巡检配置不读本机的 config.yaml 与环境变量：那里配的提交作者会让断言随机器而变
        patch = mock.patch("backend.survey.config.load_file_config", return_value={})
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ("OPENCR_SURVEY_FIX_COMMIT_NAME", "OPENCR_SURVEY_FIX_COMMIT_EMAIL"):
            os.environ.pop(name, None)

    def _run_with_finding(self, title="求和没有处理空价格", path="calc.py"):
        """一轮成功运行，含一条 app 仓库的发现，返回 (run_uid, finding_id)。"""
        run_uid = self._complete_run([_finding(path, category="correctness", line=2, title=title, body="price 可能为 None")])
        detail = self.repo.get_survey_run_detail(run_uid)
        return run_uid, detail["findings"][0]["id"]

    def _remote(self) -> str:
        """一个带历史的裸仓库，作为修复要推送的远端。把发现所在运行的仓库地址指向它。"""
        src = _make_history(self.root)
        bare = self.root / "remote.git"
        _git(self.root, "clone", "-q", "--bare", str(src), str(bare))
        # 部分克隆要求远端允许按需取对象
        _git(bare, "config", "uploadpack.allowFilter", "true")
        _git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
        return f"file://{bare}"

    def _point_repo_to(self, url):
        """把全部运行的仓库记录指向给定地址与 main 分支。"""
        from sqlalchemy import update

        from backend.storage.db import session_scope
        from backend.storage.models import SurveyRunRepo

        with session_scope() as session:
            session.execute(update(SurveyRunRepo).values(url=url, branch="main"))


class StartFixTests(FixTestCase):
    """登记修复运行与报告、台账里的修复链接。"""

    def _fixes_for(self, run_uid):
        """一次运行报告里可见发现相关的修复。"""
        detail = self.repo.get_survey_run_detail(run_uid)
        return self.repo.list_survey_fixes_for_issues(self.survey["survey_uid"], detail["findings"])

    def test_outcomes(self):
        """不存在、进行中、结束后再发起；下一轮同一个问题仍算同一个。"""
        repo = self.repo
        _, finding_id = self._run_with_finding()
        outcome, uid = repo.start_survey_fix(finding_id)
        self.assertEqual(outcome, repo.FIX_START_OK)
        # 同一个问题已有进行中的修复
        self.assertEqual(repo.start_survey_fix(finding_id), (repo.FIX_START_BUSY, ""))
        self.assertEqual(repo.start_survey_fix(999999)[0], repo.FIX_START_NOT_FOUND)

        # 下一轮同一个问题是另一条发现，仍算同一个问题
        _, next_id = self._run_with_finding()
        self.assertEqual(repo.start_survey_fix(next_id)[0], repo.FIX_START_BUSY)
        # 结束后可以再发起
        self.assertTrue(repo.finish_survey_fix(uid, "failed", error_kind="git_failed"))
        self.assertFalse(repo.finish_survey_fix(uid, "succeeded"))
        self.assertEqual(repo.start_survey_fix(next_id)[0], repo.FIX_START_OK)

    def test_empty_titles_are_not_the_same_issue(self):
        """标题为空的两个问题认不出是不是同一个，不能互相挡住。"""
        _, first = self._run_with_finding(title="")
        _, second = self._run_with_finding(title="")
        self.assertEqual(self.repo.start_survey_fix(first)[0], self.repo.FIX_START_OK)
        self.assertEqual(self.repo.start_survey_fix(second)[0], self.repo.FIX_START_OK)
        self.assertEqual(self.repo.start_survey_fix(first)[0], self.repo.FIX_START_BUSY)

    def test_ignored_finding_rejected(self):
        """已忽略的问题不受理修复。"""
        _, finding_id = self._run_with_finding()
        self.repo.add_survey_ignore(self.survey["survey_uid"], finding_id)
        self.assertEqual(self.repo.start_survey_fix(finding_id)[0], self.repo.FIX_START_IGNORED)

    def test_links_follow_fingerprint_across_runs(self):
        """上一轮发起的修复，这一轮报告里仍列出；只有提出了合并请求的进台账链接。"""
        _, finding_id = self._run_with_finding()
        _, uid = self.repo.start_survey_fix(finding_id)
        self.repo.update_survey_fix(uid, mr_url="https://g.com/a/app/-/merge_requests/7", mr_iid=7)
        self.repo.finish_survey_fix(uid, "succeeded")
        _, failed_uid = self.repo.start_survey_fix(finding_id)
        self.repo.finish_survey_fix(failed_uid, "failed")

        # 同一文件同一类别里另一个问题的修复不算这一轮的
        _, other_id = self._run_with_finding(title="另一个问题")
        _, other_uid = self.repo.start_survey_fix(other_id)
        self.repo.finish_survey_fix(other_uid, "failed")

        latest, _ = self._run_with_finding()
        listed = self._fixes_for(latest)
        self.assertEqual([f["run_uid"] for f in listed], [failed_uid, uid])
        self.assertNotIn("diff", listed[0])
        links = self.repo.survey_fix_links(self._survey_id())
        self.assertEqual(list(links.values()), [["https://g.com/a/app/-/merge_requests/7"]])

        # 台账状态不受影响，链接只是带上
        from backend.survey.ledger import build_ledger_rows

        entries = self.repo.list_survey_ledger(self._survey_id())
        self.assertEqual(entries[0]["state"], "present")
        row = build_ledger_rows(self.survey, entries, "", False, fix_links=links)[0]
        self.assertEqual(row.values["fix_links"], "https://g.com/a/app/-/merge_requests/7")
        self.assertEqual(row.values["state"], "存在")

    def test_markdown_lists_fix_links(self):
        """Markdown 导出在问题下方与单独一节都列出合并请求链接与处理人。"""
        from backend.survey.fix import attach_fixes
        from backend.survey.report import render_run_markdown

        run_uid, finding_id = self._run_with_finding()
        _, uid = self.repo.start_survey_fix(finding_id)
        self.repo.update_survey_fix(
            uid, mr_url="https://g.com/a/app/-/merge_requests/7", mr_iid=7,
            assignee_username="alice", assignee_source="author",
        )
        self.repo.finish_survey_fix(uid, "succeeded")
        detail = self.repo.get_survey_run_detail(run_uid)
        detail["fixes"] = self._fixes_for(run_uid)
        attach_fixes(detail["findings"], detail["fixes"])
        markdown = render_run_markdown(detail)
        self.assertIn("修复合并请求：https://g.com/a/app/-/merge_requests/7，处理人 @alice", markdown)
        self.assertIn("## 修复合并请求", markdown)
        self.assertIn("@alice（原始作者）", markdown)


class ExecuteFixTests(FixTestCase):
    """修复的完整执行：模型与平台接口用替身，git 走真实的本地远端仓库。"""

    MEMBERS = [
        {"id": 1, "username": "alice", "name": "Alice", "state": "active", "access_level": 30},
        {"id": 2, "username": "bob", "name": "Bob", "state": "active", "access_level": 30},
        {"id": 3, "username": "lead", "name": "Lead", "state": "active", "access_level": 40},
    ]
    FIXED = "def total(items):\n    return sum(\n        i[\"price\"] or 0 for i in items\n    )\n"

    def _execute(self, replies, members=None):
        """以给定的模型回复序列执行一次修复，返回 (修复记录, create_merge_request 的 mock)。"""
        from backend.survey import assignee, fix

        _, finding_id = self._run_with_finding()
        self._point_repo_to(self._remote())
        _, uid = self.repo.start_survey_fix(finding_id)
        create = mock.Mock(return_value={"iid": 7, "web_url": "https://g.com/a/app/-/merge_requests/7"})
        with mock.patch.object(fix, "_call_model", side_effect=replies), \
                mock.patch.object(fix, "create_merge_request", create), \
                mock.patch.object(assignee, "list_project_members", return_value=members or self.MEMBERS), \
                mock.patch.object(assignee, "get_current_user", return_value={"id": 100}), \
                mock.patch.object(assignee, "search_users", return_value=[]), \
                mock.patch.object(fix, "get_current_user", return_value=TOKEN_ACCOUNT), \
                mock.patch.object(fix, "_republish") as republish:
            fix.execute_survey_fix(uid)
        self.republish = republish
        return self.repo.get_survey_fix(uid), create

    @staticmethod
    def _proposal(search='i["price"] for', replace='i["price"] or 0 for'):
        """一条模型给出的修改回复。"""
        return '{"applicable":true,"summary":"空价格按 0 计","edits":[{"search":%s,"replace":%s}]}' % (
            __import__("json").dumps(search), __import__("json").dumps(replace))

    def test_opens_mr_assigned_to_original_author(self):
        """验证通过后推分支、提合并请求，指派给原始作者 Alice 而不是只做了格式化的 Bob。"""
        result, create = self._execute([self._proposal(), '{"resolved":true,"problems":[]}'])
        self.assertEqual(result["status"], "succeeded", result["error_message"])
        self.assertEqual((result["author_name"], result["assignee_username"], result["assignee_source"]),
                         ("Alice", "alice", "author"))
        self.assertEqual(result["mr_url"], "https://g.com/a/app/-/merge_requests/7")
        self.assertIn('+        i["price"] or 0 for i in items', result["diff"])
        args = create.call_args
        # 项目路径去掉了 .git 后缀
        self.assertEqual(args.args[0], str(self.root / "remote").lstrip("/"))
        self.assertEqual(args.args[2], "main")
        self.assertEqual(args.args[3], "[OpenCR] 求和没有处理空价格")
        self.assertEqual(args.kwargs["assignee_id"], 1)
        self.assertIn("合并不会直接改变巡检台账的状态", args.args[4])
        # 分支真的推到了远端，提交只含这一个文件
        branch = result["source_branch"]
        shown = _git(self.root / "remote.git", "show", "--name-only", "--format=%an|%ae|%cn|%ce", branch)
        # 没有配置提交作者时跟随令牌账号，邮箱取账号的提交邮箱
        self.assertEqual(shown.split("\n")[:3], [
            "Fix Bot|100-fixbot@users.noreply.g.com|Fix Bot|100-fixbot@users.noreply.g.com", "", "calc.py"])
        self.republish.assert_called_once()
        # 工作区做完即删
        self.assertFalse((self.root / "ws" / ".fixes").exists() and any((self.root / "ws" / ".fixes").iterdir()))

    def test_mr_title_prefix_follows_setting(self):
        """标题前缀按后台设置；设成空串时标题只有问题本身。"""
        from backend.survey.config import SETTING_FIX_MR_TITLE_PREFIX

        self.repo.set_setting(SETTING_FIX_MR_TITLE_PREFIX, "")
        _, create = self._execute([self._proposal(), '{"resolved":true,"problems":[]}'])
        self.assertEqual(create.call_args.args[3], "求和没有处理空价格")

    def test_configured_commit_author_wins(self):
        """配置了提交作者时用配置的，只配了名字时邮箱仍跟随令牌账号。"""
        with mock.patch.dict(os.environ, {"OPENCR_SURVEY_FIX_COMMIT_NAME": "Quality Bot"}):
            result, _ = self._execute([self._proposal(), '{"resolved":true,"problems":[]}'])
        shown = _git(self.root / "remote.git", "log", "-1", "--format=%an|%ae", result["source_branch"])
        self.assertEqual(shown.strip(), "Quality Bot|100-fixbot@users.noreply.g.com")

    def test_blocked_author_goes_to_configured_owner(self):
        """原始作者被封禁时改派配置的仓库负责人，原因写进记录。"""
        self.repo.save_repo_owner(str(self.root / "remote").lstrip("/"), ["lead"])
        members = [{**self.MEMBERS[0], "state": "blocked"}, *self.MEMBERS[1:]]
        result, create = self._execute([self._proposal(), '{"resolved":true,"problems":[]}'], members=members)
        self.assertEqual((result["assignee_username"], result["assignee_source"]), ("lead", "configured"))
        self.assertIn("blocked", result["assignee_note"])
        self.assertEqual(create.call_args.kwargs["assignee_id"], 3)

    def test_blocked_author_without_config_leaves_mr_unassigned(self):
        """原始作者被封禁、又没有配置仓库负责人时，合并请求照常提出但不指派，原因写进记录。"""
        members = [{**self.MEMBERS[0], "state": "blocked"}, *self.MEMBERS[1:]]
        result, create = self._execute([self._proposal(), '{"resolved":true,"problems":[]}'], members=members)
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["assignee_username"] or result["assignee_source"])
        self.assertIn("未指派处理人", result["assignee_note"])
        self.assertIsNone(create.call_args.kwargs["assignee_id"])

    def test_retry_after_failed_verification(self):
        """第一轮套不上，第二轮带着原因重来并通过。"""
        result, create = self._execute([
            self._proposal(search="不存在的原文"), self._proposal(), '{"resolved":true,"problems":[]}',
        ])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([a["passed"] for a in result["verification"]], [False, True])
        create.assert_called_once()

    def test_no_mr_when_verification_keeps_failing(self):
        """每轮都没通过验证就不提合并请求，也不重推台账。"""
        result, create = self._execute([
            self._proposal(), '{"resolved":false,"problems":["没有处理负数"]}',
            self._proposal(), '{"resolved":true,"problems":["引入了新问题"]}',
        ])
        self.assertEqual((result["status"], result["error_kind"]), ("failed", "verify_failed"))
        create.assert_not_called()
        self.republish.assert_not_called()

    def test_not_applicable_is_skipped(self):
        """模型一开始就认为最新代码里没有这个问题：记为未修改，不提合并请求。"""
        result, create = self._execute(['{"applicable":false,"reason":"已经处理过空价格"}'])
        self.assertEqual((result["status"], result["error_kind"]), ("skipped", "not_applicable"))
        self.assertIn("已经处理过", result["error_message"])
        create.assert_not_called()

    def test_giving_up_on_retry_is_not_skipped(self):
        """第二轮才说"不用改"是在回避验证意见，不能当成问题已不存在。"""
        result, _ = self._execute([
            self._proposal(), '{"resolved":false,"problems":["没解决"]}', '{"applicable":false,"reason":"算了"}',
        ])
        self.assertEqual((result["status"], result["error_kind"]), ("failed", "verify_failed"))

    def test_crlf_preserved(self):
        """CRLF 文件改完仍是 CRLF，不能整个文件都显示被改过。"""
        from backend.survey.fix import _read_source, _write_source

        repo_dir = self.root / "crlf"
        repo_dir.mkdir()
        (repo_dir / "a.txt").write_bytes(b"x = 1\r\ny = 2\r\n")
        text, crlf = _read_source(repo_dir, "a.txt", 1000)
        self.assertEqual((text, crlf), ("x = 1\ny = 2\n", True))
        _write_source(repo_dir, "a.txt", text.replace("1", "3"), crlf)
        self.assertEqual((repo_dir / "a.txt").read_bytes(), b"x = 3\r\ny = 2\r\n")


class RepublishTests(unittest.TestCase):
    """修复成功后重推台账。"""

    def test_waits_for_busy_binding(self):
        """输出目标正忙时等它结束，只补推正忙的那个，已经推过的不重复推。"""
        from backend.survey import fix

        begin = mock.Mock(side_effect=[
            {"started": [11], "busy": [2]},
            {"started": [], "busy": [2]},
            {"started": [12], "busy": []},
        ])
        with mock.patch.object(fix.repo, "latest_succeeded_survey_run_uid", return_value="run"), \
                mock.patch.object(fix, "begin_pushes", begin), \
                mock.patch.object(fix, "execute_pushes") as execute, \
                mock.patch.object(fix.time, "sleep"):
            fix._republish(1)
        self.assertEqual([c.args for c in begin.call_args_list], [("run", "auto", None), ("run", "auto", 2), ("run", "auto", 2)])
        self.assertEqual([c.args[0] for c in execute.call_args_list], [[11], [], [12]])


class ReadSourceTests(unittest.TestCase):
    """读取要修的文件时的边界。"""

    def test_symlink_rejected(self):
        """符号链接不改：写回会落到链接指向的文件，提交里却只有链接本身。"""
        from backend.survey.fix import FixFailure, _read_source

        root = Path(tempfile.mkdtemp(prefix="opencr-fix-link-"))
        (root / "real.py").write_text("x = 1\n")
        (root / "link.py").symlink_to(root / "real.py")
        with self.assertRaisesRegex(FixFailure, "符号链接"):
            _read_source(root, "link.py", 1000)
        with self.assertRaisesRegex(FixFailure, "越出"):
            _read_source(root, "../etc/passwd", 1000)


class FixRouteTestCase(FixTestCase):
    """注册了后台蓝图的修复测试基类。"""

    def setUp(self):
        """在独立数据库上注册后台蓝图。"""
        super().setUp()
        from flask import Flask

        from backend.admin import auth, routes

        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        self.app.register_blueprint(routes.admin_bp)
        self.client = self.app.test_client()
        for target, value in ((auth, "load_admin_config"), (routes, "load_storage_config")):
            patch = mock.patch.object(
                target, value,
                return_value={"enabled": True, "bind_local_only": False, "stale_after_seconds": 600},
            )
            patch.start()
            self.addCleanup(patch.stop)

    def _login(self):
        """把测试客户端设为管理员身份。"""
        with self.client.session_transaction() as session:
            session["identity"] = "admin"


class FixRouteTests(FixRouteTestCase):
    """后台接口：只对 Admin 开放，Guest 看不到修复记录。"""

    def test_admin_only_and_starts_in_background(self):
        """登记后在后台执行；同一个问题进行中时拒绝；报告带上修复；心跳正常时不能确认中断。"""
        run_uid, finding_id = self._run_with_finding()
        self.assertEqual(self.client.post(f"/api/admin/survey-findings/{finding_id}/fix").status_code, 401)
        self._login()
        with mock.patch("backend.admin.routes.execute_survey_fix_in_background") as start:
            response = self.client.post(f"/api/admin/survey-findings/{finding_id}/fix")
            self.assertEqual(response.status_code, 202)
            start.assert_called_once_with(response.json["run_uid"])
            self.assertEqual(self.client.post(f"/api/admin/survey-findings/{finding_id}/fix").status_code, 409)

        detail = self.client.get(f"/api/admin/survey-runs/{run_uid}").json
        self.assertEqual(len(detail["fixes"]), 1)
        self.assertEqual(detail["findings"][0]["fixes"][0]["run_uid"], response.json["run_uid"])
        fix = self.client.get(f"/api/admin/survey-fixes/{response.json['run_uid']}").json
        self.assertEqual(fix["status"], "running")
        # 心跳正常的修复不能被确认中断
        self.assertEqual(self.client.post(f"/api/admin/survey-fixes/{fix['run_uid']}/interrupt").status_code, 409)

    def test_resolved_section_keeps_fix_links(self):
        """合并请求修掉问题后，下一轮它落进「已消失」，那里仍要看得到对应的合并请求。"""
        _, finding_id = self._run_with_finding()
        _, uid = self.repo.start_survey_fix(finding_id)
        self.repo.update_survey_fix(uid, mr_url="https://g.com/a/app/-/merge_requests/7", mr_iid=7)
        self.repo.finish_survey_fix(uid, "succeeded")
        later = self._complete_run([], inspected=("calc.py",))
        self._login()
        detail = self.client.get(f"/api/admin/survey-runs/{later}").json
        self.assertEqual(len(detail["resolved_findings"]), 1)
        self.assertEqual(detail["resolved_findings"][0]["fixes"][0]["run_uid"], uid)
        self.assertEqual([f["run_uid"] for f in detail["fixes"]], [uid])

    def test_guest_sees_no_fixes(self):
        """Guest 的报告与导出里没有修复记录：它们带着问题标题、作者与合并请求。"""
        from backend.admin import routes

        run_uid, finding_id = self._run_with_finding()
        self.repo.start_survey_fix(finding_id)
        with mock.patch.object(routes, "is_admin", return_value=False), \
                mock.patch("backend.admin.auth.guest_read_enabled", return_value=True), \
                mock.patch("backend.admin.auth.survey_guest_read_enabled", return_value=True):
            detail = self.client.get(f"/api/admin/survey-runs/{run_uid}").json
            markdown = self.client.get(f"/api/admin/survey-runs/{run_uid}/export").get_data(as_text=True)
        self.assertNotIn("fixes", detail)
        self.assertNotIn("fixes", detail["findings"][0])
        self.assertNotIn("修复合并请求", markdown)

    def test_other_platforms_rejected(self):
        """目前只支持 GitLab。"""
        _, finding_id = self._run_with_finding()
        self._login()
        with mock.patch("backend.survey.fix.load_gitlab_config", return_value={**PLATFORM, "type": "github"}):
            self.assertEqual(self.client.post(f"/api/admin/survey-findings/{finding_id}/fix").status_code, 400)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# 触发方式：一键全部修复、巡检后自动发起
# ---------------------------------------------------------------------------

class FixCandidatesTests(FixTestCase):
    """一键全部修复与巡检后自动发起的修复挑哪些问题。"""

    def _run(self, findings):
        """一轮成功运行，返回 (run_uid, {标题: finding_id})。"""
        run_uid = self._complete_run(findings)
        detail = self.repo.get_survey_run_detail(run_uid)
        return run_uid, {f["title"]: f["id"] for f in detail["findings"]}

    def test_threshold_order_and_dedupe(self):
        """不低于门槛、严重的在前；同一个问题只取一条；严重度未知的不处理。"""
        run_uid, ids = self._run([
            _finding("a.py", severity="advice", title="建议档"),
            _finding("b.py", severity="warning", title="警告"),
            _finding("c.py", severity="critical", title="严重"),
            _finding("c.py", severity="critical", title="严重"),
            _finding("d.py", severity="unknown", title="未知"),
        ])
        # 「严重」被报了两遍，ids 里记的是后一条；候选取的是前一条
        severe = ids["严重"] - 1
        self.assertEqual(self.repo.list_survey_fix_candidates(run_uid, "warning"), [severe, ids["警告"]])
        self.assertEqual(len(self.repo.list_survey_fix_candidates(run_uid, "advice")), 3)
        self.assertEqual(self.repo.list_survey_fix_candidates(run_uid, "critical"), [severe])

    def test_skips_issues_with_any_fix_and_ignored(self):
        """修复过的（不论成败、包括上一轮发起的同一个问题）与已忽略的都不再批量发起。"""
        _, first = self._run([_finding("a.py", title="已修过")])
        _, uid = self.repo.start_survey_fix(first["已修过"])
        self.repo.finish_survey_fix(uid, "failed")

        run_uid, ids = self._run([
            _finding("a.py", title="已修过"), _finding("b.py", title="被忽略"), _finding("c.py", title="新问题"),
        ])
        self.repo.add_survey_ignore(self.survey["survey_uid"], ids["被忽略"])
        self.assertEqual(self.repo.list_survey_fix_candidates(run_uid, "warning"), [ids["新问题"]])
        # 登记时同样不给修复过的问题发起
        self.assertEqual(self.repo.start_survey_fix(ids["已修过"], only_new=True)[0], self.repo.FIX_START_EXISTS)
        # 单条手动发起不受限制
        self.assertEqual(self.repo.start_survey_fix(ids["已修过"])[0], self.repo.FIX_START_OK)


class FixBatchTests(FixTestCase):
    """批量执行。"""

    def test_runs_one_by_one_and_republishes_once(self):
        """逐个登记、逐个执行，全部结束后只重推一次；执行期间已被别人修过的跳过。"""
        from backend.storage.models import FIX_TRIGGER_BATCH
        from backend.survey import fix

        run_uid = self._complete_run([_finding("a.py", title="一"), _finding("b.py", title="二")])
        ids = [f["id"] for f in self.repo.get_survey_run_detail(run_uid)["findings"]]
        _, taken = self.repo.start_survey_fix(ids[1])
        self.repo.finish_survey_fix(taken, "succeeded")

        executed = []

        def execute(uid, republish=True):
            """记录执行顺序，并确认批量执行时不各自重推。"""
            self.assertFalse(republish)
            executed.append(self.repo.get_survey_fix(uid))
            self.repo.finish_survey_fix(uid, "succeeded")
            return True

        with mock.patch.object(fix, "execute_survey_fix", side_effect=execute), \
                mock.patch.object(fix, "_republish") as republish:
            fix._run_batch(self._survey_id(), ids, FIX_TRIGGER_BATCH)
        self.assertEqual([(f["finding_id"], f["trigger"]) for f in executed], [(ids[0], "batch")])
        republish.assert_called_once_with(self._survey_id())

    def test_start_caps_batch(self):
        """每批有上限，剩下的留给下一批。"""
        from backend.survey import fix

        run_uid = self._complete_run([_finding(f"f{i}.py", title=f"问题{i}") for i in range(5)])
        with mock.patch.object(fix, "MAX_FIXES_PER_BATCH", 2), mock.patch.object(fix.threading, "Thread") as thread:
            batch = fix.start_fix_batch(run_uid, "batch")
        self.assertEqual(len(batch), 2)
        thread.assert_called_once()

    def test_one_batch_at_a_time(self):
        """同一个巡检同时只跑一批：重复点击或上一轮自动发起的那批没跑完时拒绝，批次结束后放开。"""
        from backend.survey import fix

        run_uid = self._complete_run([_finding("a.py", title="一")])
        with mock.patch.object(fix.threading, "Thread") as thread:
            fix.start_fix_batch(run_uid, "batch")
            with self.assertRaises(fix.FixBatchBusy):
                fix.start_fix_batch(run_uid, "auto")
        args = thread.call_args.kwargs["args"]
        self.assertTrue(self.repo.lease_active(fix.fix_batch_lease_name(self._survey_id())))
        with mock.patch.object(fix, "execute_survey_fix", return_value=False):
            fix._run_batch(*args)
        self.assertFalse(self.repo.lease_active(fix.fix_batch_lease_name(self._survey_id())))

    def test_lost_lease_stops_batch(self):
        """租约被另一批接手后停下，剩下的交给它。"""
        from backend.survey import fix

        run_uid = self._complete_run([_finding("a.py", title="一"), _finding("b.py", title="二")])
        ids = [f["id"] for f in self.repo.get_survey_run_detail(run_uid)["findings"]]
        lease = fix.fix_batch_lease_name(self._survey_id())
        self.repo.acquire_lease(lease, "mine", 60)

        def execute(uid, republish=True):
            """第一个修复执行期间，租约被别人抢走。"""
            self.repo.release_lease(lease, "mine")
            self.repo.acquire_lease(lease, "other", 60)
            return False

        with mock.patch.object(fix, "execute_survey_fix", side_effect=execute) as executed:
            fix._run_batch(self._survey_id(), ids, "batch", "mine")
        self.assertEqual(executed.call_count, 1)
        # 不能释放别人的租约
        self.assertTrue(self.repo.lease_active(lease))

    def test_auto_fix_skipped_on_unsupported_platform(self):
        """平台不支持时自动发起直接跳过，不登记一批注定失败、之后再也轮不到的修复。"""
        from backend.survey import runner

        with mock.patch.object(runner, "fix_unsupported_reason", return_value="只支持 GitLab"), \
                mock.patch.object(runner, "start_fix_batch") as start:
            runner._auto_fix({**self.survey, "fix_mode": "auto"}, "run")
        start.assert_not_called()

    def test_auto_fix_only_when_configured(self):
        """修复触发方式为巡检后自动发起时，运行成功结束后才自动发起。"""
        from backend.survey import runner

        with mock.patch.object(runner, "start_fix_batch", return_value=[]) as start:
            runner._auto_fix({**self.survey, "fix_mode": "manual"}, "run")
            start.assert_not_called()
            runner._auto_fix({**self.survey, "fix_mode": "auto"}, "run")
            start.assert_called_once_with("run", "auto")
        # 上一批还在跑时跳过，不抛
        with mock.patch.object(runner, "start_fix_batch", side_effect=runner.FixBatchBusy("busy")):
            runner._auto_fix({**self.survey, "fix_mode": "auto"}, "run")


# ---------------------------------------------------------------------------
# 仓库负责人配置
# ---------------------------------------------------------------------------

class RepoOwnerStorageTests(FixTestCase):
    """仓库负责人配置的存取与匹配。"""

    def test_most_specific_path_wins(self):
        """组织路径对下面的项目生效，同一个项目命中多条时取最具体的；路径大小写不敏感。"""
        self.repo.save_repo_owner("Team", ["lead"])
        self.repo.save_repo_owner("team/mobile/app.git", ["@alice", "bob", "alice"])
        self.assertEqual(self.repo.find_repo_owner("team/mobile/App")["usernames"], ["alice", "bob"])
        self.assertEqual(self.repo.find_repo_owner("team/web/site")["path"], "team")
        self.assertIsNone(self.repo.find_repo_owner("other/app"))
        # 前缀必须落在路径分隔处：teammate 不是 team 下面的项目
        self.assertIsNone(self.repo.find_repo_owner("teammate/app"))

    def test_save_overwrites_same_path_and_rejects_conflicts(self):
        """同一路径只有一条；编辑成别的已有路径时拒绝。"""
        first = self.repo.save_repo_owner("team/app", ["a"])
        again = self.repo.save_repo_owner("team/app", ["b"], note="换人")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.repo.list_repo_owners()), 1)
        other = self.repo.save_repo_owner("team/web", ["c"])
        with self.assertRaises(ValueError):
            self.repo.save_repo_owner("team/app", ["c"], owner_id=other["id"])
        with self.assertRaises(ValueError):
            self.repo.save_repo_owner("team/x", [])
        self.assertIsNone(self.repo.save_repo_owner("team/x", ["a"], owner_id=999))
        self.assertTrue(self.repo.delete_repo_owner(other["id"]))


class ConfiguredOwnerTests(FixTestCase):
    """原始作者不可用时找配置的负责人，找不到就不指派。"""

    MEMBERS = [
        {"id": 1, "username": "alice", "name": "Alice", "state": "blocked", "access_level": 30},
        {"id": 2, "username": "gone", "name": "Gone", "state": "blocked", "access_level": 40},
        {"id": 3, "username": "carol", "name": "Carol", "state": "active", "access_level": 30},
        {"id": 4, "username": "dave", "name": "Dave", "state": "active", "access_level": 50},
    ]

    def _resolve(self):
        """原始作者 Alice 被封禁时的处理人。"""
        from backend.survey import assignee

        with mock.patch.object(assignee, "list_project_members", return_value=self.MEMBERS), \
                mock.patch.object(assignee, "get_current_user", return_value={"id": 100}), \
                mock.patch.object(assignee, "search_users", return_value=[]):
            return assignee.resolve_assignee("team/app", {"name": "Alice", "email": "alice@corp.com"})

    def test_first_available_configured_owner(self):
        """按顺序跳过不可用的人（被封禁、不在项目里），指派给第一个可用的。"""
        self.repo.save_repo_owner("team", ["gone", "nobody", "carol"])
        result = self._resolve()
        self.assertEqual((result["source"], result["username"]), ("configured", "carol"))
        self.assertIn("按 team 的配置", result["note"])

    def test_unassigned_when_configured_unavailable(self):
        """配置的人都不可用时不指派（不按项目权限推断，项目里的 Owner dave 也不选），并写明配置的人为什么被跳过。"""
        self.repo.save_repo_owner("team/app", ["gone", "nobody"])
        result = self._resolve()
        self.assertEqual((result["source"], result["id"]), ("", None))
        self.assertIn("未指派处理人", result["note"])
        self.assertIn("gone 账号不可用（blocked）", result["note"])
        self.assertIn("nobody 不在项目成员中", result["note"])

    def test_no_config_leaves_unassigned(self):
        """没有配置时不指派。"""
        result = self._resolve()
        self.assertEqual((result["source"], result["id"]), ("", None))
        self.assertIn("没有为这个仓库配置负责人", result["note"])


class FixTriggerRouteTests(FixRouteTestCase):
    """一键全部修复、巡检的修复设置与仓库负责人配置的接口。"""

    def test_fix_all_only_on_latest_succeeded_run(self):
        """只对最近一次成功的运行开放；报告里带上候选数。"""
        old_run, _ = self._run_with_finding()
        latest, _ = self._run_with_finding()
        self._login()
        self.assertEqual(self.client.post(f"/api/admin/survey-runs/{old_run}/fix-all").status_code, 409)
        detail = self.client.get(f"/api/admin/survey-runs/{latest}").json
        self.assertEqual((detail["fix_all_available"], detail["fix_all_candidates"]), (True, 1))
        self.assertFalse(self.client.get(f"/api/admin/survey-runs/{old_run}").json["fix_all_available"])
        with mock.patch("backend.admin.routes.start_fix_batch", return_value=[1]) as start:
            response = self.client.post(f"/api/admin/survey-runs/{latest}/fix-all")
        self.assertEqual((response.status_code, response.json), (202, {"queued": 1, "total": 1}))
        start.assert_called_once_with(latest, "batch")
        self.assertFalse(self.client.get(f"/api/admin/survey-runs/{latest}").json["fix_batch_running"])
        from backend.survey.fix import FixBatchBusy

        with mock.patch("backend.admin.routes.start_fix_batch", side_effect=FixBatchBusy("已有一批")):
            self.assertEqual(self.client.post(f"/api/admin/survey-runs/{latest}/fix-all").status_code, 409)

    def test_survey_fix_settings_validated(self):
        """修复触发方式与门槛只接受固定取值，默认手动、警告及以上。"""
        self._login()
        uid = self.survey["survey_uid"]
        self.assertEqual(self.client.get(f"/api/admin/surveys/{uid}").json["fix_mode"], "manual")
        self.assertEqual(self.client.patch(f"/api/admin/surveys/{uid}", json={"fix_mode": "always"}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/admin/surveys/{uid}", json={"fix_min_severity": "unknown"}).status_code, 400)
        updated = self.client.patch(f"/api/admin/surveys/{uid}", json={"fix_mode": "auto", "fix_min_severity": "critical"}).json
        self.assertEqual((updated["fix_mode"], updated["fix_min_severity"]), ("auto", "critical"))

    def test_repo_owner_crud(self):
        """只对 Admin 开放；路径可以填地址，用户名可以是逗号分隔的字符串。"""
        self.assertEqual(self.client.get("/api/admin/repo-owners").status_code, 401)
        self._login()
        created = self.client.post(
            "/api/admin/repo-owners", json={"path": "https://g.com/Team/App.git", "usernames": "alice，bob"}
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual((created.json["path"], created.json["usernames"]), ("team/app", ["alice", "bob"]))
        self.assertEqual(self.client.post("/api/admin/repo-owners", json={"path": "", "usernames": ["a"]}).status_code, 400)
        owner_id = created.json["id"]
        patched = self.client.patch(f"/api/admin/repo-owners/{owner_id}", json={"path": "team", "usernames": ["carol"]})
        self.assertEqual(patched.json["path"], "team")
        self.assertEqual(self.client.patch("/api/admin/repo-owners/999", json={"path": "x", "usernames": ["a"]}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/admin/repo-owners/{owner_id}").json, {"removed": True})
        self.assertEqual(self.client.get("/api/admin/repo-owners").json["items"], [])


class FixMergeRequestListTests(FixRouteTestCase):
    """「修复合并请求」列表：只列提出了合并请求的修复，按巡检、处理人（含未指派）与关键字筛选。"""

    def _fix(self, title, assignee="", status="succeeded", mr=True):
        """一条修复记录：给定处理人（空串表示未指派）与结果，返回 run_uid。"""
        _, finding_id = self._run_with_finding(title=title)
        _, uid = self.repo.start_survey_fix(finding_id)
        fields = {"assignee_username": assignee or None, "assignee_name": assignee.title() or None,
                  "assignee_source": "author" if assignee else None}
        if mr:
            fields.update(mr_iid=len(title), mr_url=f"https://g.com/a/app/-/merge_requests/{len(title)}")
        self.repo.update_survey_fix(uid, **fields)
        self.repo.finish_survey_fix(uid, status)
        return uid

    def _seed(self):
        """alice 两条、bob 一条、未指派一条（空串与 NULL 各一种写法都要算），外加失败与没有合并请求的各一条。"""
        self._fix("空价格 A", "alice")
        self._fix("空价格 B", "alice")
        self._fix("越界", "bob")
        self._fix("未指派的问题")
        uid = self._fix("另一个未指派")
        self.repo.update_survey_fix(uid, assignee_username="")
        self._fix("失败的修复", "alice", status="failed", mr=False)
        self._fix("跳过的修复", "alice", status="skipped", mr=False)

    def test_lists_only_opened_merge_requests_with_facets(self):
        """失败与跳过的不列；选项里各处理人的数量与未指派的数量对得上。"""
        from backend.storage.repo import list_fix_merge_requests

        self._seed()
        result = list_fix_merge_requests()
        self.assertEqual(result["total"], 5)
        self.assertEqual([(o["username"], o["count"]) for o in result["assignees"]], [("alice", 2), ("bob", 1)])
        self.assertEqual(result["unassigned"], 2)
        self.assertEqual(result["items"][0]["survey_name"], self.survey["name"])

    def test_filters_by_assignees_unassigned_and_keyword(self):
        """多选处理人取并集，可与未指派同时选；选项不受处理人筛选影响；关键字匹配标题。"""
        from backend.storage.repo import UNASSIGNED_FILTER, list_fix_merge_requests

        self._seed()
        titles = lambda r: sorted(i["title"] for i in r["items"])  # noqa: E731
        self.assertEqual(titles(list_fix_merge_requests(assignees=["bob"])), ["越界"])
        both = list_fix_merge_requests(assignees=["bob", UNASSIGNED_FILTER])
        self.assertEqual(titles(both), ["另一个未指派", "未指派的问题", "越界"])
        self.assertEqual(len(both["assignees"]), 2)
        self.assertEqual(titles(list_fix_merge_requests(assignees=[UNASSIGNED_FILTER])), ["另一个未指派", "未指派的问题"])
        self.assertEqual(titles(list_fix_merge_requests(keyword="空价格")), ["空价格 A", "空价格 B"])
        # % 与 _ 按字面匹配，不当通配符
        self.assertEqual(list_fix_merge_requests(keyword="空_格")["total"], 0)
        self.assertEqual(list_fix_merge_requests(keyword="%")["total"], 0)
        self.assertEqual(list_fix_merge_requests(keyword="空价格", assignees=["bob"])["total"], 0)
        self.assertEqual(list_fix_merge_requests(survey_uid="nope")["total"], 0)
        paged = list_fix_merge_requests(limit=2, offset=4)
        self.assertEqual((paged["total"], len(paged["items"])), (5, 1))

    def test_route_admin_only_and_parses_filters(self):
        """只对 Admin；处理人以逗号分隔传入。"""
        self._seed()
        self.assertIn(self.client.get("/api/admin/survey-fixes").status_code, (401, 403))
        self._login()
        response = self.client.get("/api/admin/survey-fixes?assignees=alice,__unassigned__&limit=10")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["total"], 4)
        uid = self.survey["survey_uid"]
        self.assertEqual(self.client.get(f"/api/admin/survey-fixes?survey_uid={uid}&q=越界").json["total"], 1)


class FixTitlePrefixSettingTests(FixRouteTestCase):
    """后台设置里的修复合并请求标题前缀。"""

    def test_default_update_and_validation(self):
        """默认 [OpenCR]；可以改、可以设成空串（不加前缀）；超长、带换行、不是文本的拒绝。"""
        self._login()
        key = "survey_fix_mr_title_prefix"
        self.assertEqual(self.client.get("/api/admin/settings").json["writable"][key], "[OpenCR]")
        response = self.client.patch("/api/admin/settings", json={key: " [Bot] "})
        self.assertEqual(response.json["writable"][key], "[Bot]")
        self.assertEqual(self.client.patch("/api/admin/settings", json={key: ""}).json["writable"][key], "")
        for bad in ("x" * 41, "a\nb", "a\tb", 1, None, True):
            self.assertEqual(self.client.patch("/api/admin/settings", json={key: bad}).status_code, 400, bad)
        self.assertEqual(self.client.get("/api/admin/settings").json["writable"][key], "")


class PlatformUserSearchTests(FixRouteTestCase):
    """仓库负责人配置的候选项：搜索代码平台用户。"""

    def test_admin_only_and_maps_fields(self):
        """只对 Admin；只回用户名、姓名、状态与是否机器人，不回邮箱；没有用户名的条目丢掉。"""
        self.assertIn(self.client.get("/api/admin/platform-users?q=al").status_code, (401, 403))
        self._login()
        users = [
            {"id": 1, "username": "alice", "name": "Alice", "state": "active", "email": "alice@corp.com"},
            {"id": 2, "username": "old", "name": "Old", "state": "blocked"},
            {"id": 3, "username": "ci", "name": "CI", "bot": True},
            {"id": 4, "name": "无用户名"},
        ]
        with mock.patch("backend.admin.routes.search_users", return_value=users) as search:
            response = self.client.get("/api/admin/platform-users?q=%20al%20")
        search.assert_called_once_with("al")
        self.assertEqual(response.json["items"], [
            {"username": "alice", "name": "Alice", "state": "active", "bot": False},
            {"username": "old", "name": "Old", "state": "blocked", "bot": False},
            {"username": "ci", "name": "CI", "state": "active", "bot": True},
        ])

    def test_empty_query_and_failures(self):
        """空关键字不访问平台；平台出错回 502 且不带报错原文；非 GitLab 平台回 409。"""
        self._login()
        with mock.patch("backend.admin.routes.search_users") as search:
            self.assertEqual(self.client.get("/api/admin/platform-users?q=").json["items"], [])
        search.assert_not_called()
        with mock.patch("backend.admin.routes.search_users", side_effect=RuntimeError("10.0.0.5 refused")):
            response = self.client.get("/api/admin/platform-users?q=al")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("10.0.0.5", response.json["error"])
        with mock.patch("backend.admin.routes.fix_unsupported_reason", return_value="修复目前只支持 GitLab"):
            self.assertEqual(self.client.get("/api/admin/platform-users?q=al").status_code, 409)
