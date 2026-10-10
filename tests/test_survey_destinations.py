"""巡检输出（Ledger / Binding / Push / Google Sheet）测试。

重点覆盖几类"坏掉了也不会报错"的约束：
1. LedgerState 判定 —— 本轮没出现不等于已修复，所在文件本轮没被取证过时必须保持原状态；
2. 镜像的补回规则 —— 用户删掉的已不出现的行不能被写回，新表却必须拿到完整台账；
3. 人工列不被触碰 —— 系统列按表头定位、按连续列段写入，写入必须是 RAW；
4. 推送与运行解耦 —— 推送失败不改写 SurveyRun，Guest 看不到目标位置与错误信息。
"""

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.test_survey import SurveyStorageTestCase  # noqa: E402


def _finding(path, category="security", line=1, severity="warning", title="标题", body="正文", repo="app"):
    """构造一条已带指纹的发现。"""
    from backend.survey.common import finding_fingerprint

    return {
        "repo_slug": repo, "file_path": path, "line": line, "category": category,
        "severity": severity, "title": title, "body": body,
        "fingerprint": finding_fingerprint(repo, path, category),
    }


# ---------------------------------------------------------------------------
# 纯函数：聚合与状态判定
# ---------------------------------------------------------------------------

class AggregateFindingsTests(unittest.TestCase):
    def test_same_fingerprint_merges_into_one_row(self):
        """同一文件同一类别的多条发现共享指纹，按"指纹 + 行号"分行会让行号漂移制造假的新增与消失。"""
        from backend.survey.ledger import aggregate_findings

        items = [
            _finding("lib/a.dart", line=40, severity="advice", title="次要", body="B"),
            _finding("lib/a.dart", line=12, severity="critical", title="严重", body="A"),
            _finding("lib/b.dart", line=3),
        ]
        result = aggregate_findings(items)
        self.assertEqual(len(result), 2)

        merged = result[items[0]["fingerprint"]]
        self.assertEqual(merged["severity"], "critical")
        self.assertEqual(merged["title"], "严重")
        self.assertEqual(merged["lines"], [12, 40])
        self.assertEqual(merged["finding_count"], 2)
        # 正文按严重度排序拼接，每条带行号小标题
        self.assertLess(merged["body"].index("[第 12 行] 严重"), merged["body"].index("[第 40 行] 次要"))

        single = result[items[2]["fingerprint"]]
        self.assertEqual(single["body"], "正文")
        self.assertEqual(single["finding_count"], 1)



class PlanLedgerUpdateTests(unittest.TestCase):
    NOW = datetime(2026, 9, 21, 1, 0)
    EARLIER = datetime(2026, 9, 14, 1, 0)

    def _entry(self, fingerprint, state="present", repo="app", path="lib/x.dart"):
        return {
            "fingerprint": fingerprint, "repo_slug": repo, "file_path": path,
            "state": state, "first_seen_at": self.EARLIER,
        }

    def _plan(self, existing, aggregated=None, inspected=(("app", "lib/x.dart"),), ignored=(), lookup=None):
        from backend.survey.ledger import plan_ledger_update

        changes = plan_ledger_update(
            existing={e["fingerprint"]: e for e in existing},
            aggregated=aggregated or {},
            inspected=set(inspected),
            ignored=set(ignored),
            first_seen_lookup=lookup or {},
            run_uid="run-2",
            now=self.NOW,
        )
        return {c["fingerprint"]: c for c in changes}

    def test_rows_of_ignored_repo_become_ignored(self):
        """仓库在忽略清单里、本轮没拉取：它的行转为已忽略，不能停在"存在"冒充仍在跟进。"""
        from backend.survey.ledger import plan_ledger_update

        changes = plan_ledger_update(
            existing={"f1": self._entry("f1", repo="old"), "f2": self._entry("f2", state="unseen", repo="old"),
                      "f3": self._entry("f3")},
            aggregated={}, inspected=set(), ignored=set(), first_seen_lookup={},
            run_uid="run-2", now=self.NOW, ignored_repos={"old"},
        )
        # 本轮未发现的行保持原状：转成已忽略的话，取消忽略后会被当成"取消了忽略"拉去复核
        self.assertEqual({c["fingerprint"]: c["state"] for c in changes}, {"f1": "ignored"})

    def test_present_rows_keep_first_seen(self):
        from backend.survey.ledger import aggregate_findings

        item = _finding("lib/a.dart")
        changes = self._plan([self._entry(item["fingerprint"], state="unseen")], aggregate_findings([item]))
        change = changes[item["fingerprint"]]
        self.assertEqual(change["state"], "present")
        self.assertEqual(change["first_seen_at"], self.EARLIER)
        self.assertEqual(change["last_seen_at"], self.NOW)
        self.assertEqual(change["last_run_uid"], "run-2")

    def test_new_rows_take_first_seen_from_retained_history(self):
        from backend.survey.ledger import aggregate_findings

        item = _finding("lib/a.dart")
        changes = self._plan([], aggregate_findings([item]), lookup={item["fingerprint"]: self.EARLIER})
        self.assertEqual(changes[item["fingerprint"]]["first_seen_at"], self.EARLIER)

    def test_absent_rows_are_marked_unseen_only_when_their_file_was_inspected(self):
        """
        本轮没看到 ≠ 已修复。仓库拉取成功也不够：L1 每轮点名的文件都不一样，
        没被取证的文件本来就不可能出现，按仓库判定会把"没去看"说成"看了没有"。
        """
        changes = self._plan([
            self._entry("fp-inspected"),
            self._entry("fp-same-repo-other-file", path="lib/y.dart"),
            self._entry("fp-other-repo-same-path", repo="other"),
        ])
        self.assertEqual(changes["fp-inspected"]["state"], "unseen")
        self.assertNotIn("fp-same-repo-other-file", changes)
        self.assertNotIn("fp-other-repo-same-path", changes)

    def test_unchanged_state_only_refreshes_check_time(self):
        changes = self._plan([self._entry("fp", state="unseen")])
        self.assertEqual(changes, {"fp": {"fingerprint": "fp", "last_checked_at": self.NOW}})
        self.assertEqual(self._plan([self._entry("fp", state="unseen")], inspected=()), {})

    def test_inconclusive_file_refreshes_check_time_but_keeps_state(self):
        """没结论的复核不改状态，但要刷新复核时间，否则下一轮它还排在队首。"""
        from backend.survey.ledger import plan_ledger_update

        changes = plan_ledger_update(
            existing={"fp": self._entry("fp")}, aggregated={}, inspected=set(), ignored=set(),
            first_seen_lookup={}, run_uid="run-2", now=self.NOW, attempted={("app", "lib/x.dart")},
        )
        self.assertEqual(changes, [{"fingerprint": "fp", "last_checked_at": self.NOW}])

    def test_ignored_rows_are_marked_regardless_of_inspection(self):
        changes = self._plan([self._entry("fp")], inspected=(), ignored={"fp"})
        self.assertEqual(changes["fp"]["state"], "ignored")

    def test_unignored_row_waits_for_evidence(self):
        """取消忽略后没有证据就停在已忽略，取证过所在文件的一轮没出现才变为本轮未发现。"""
        uninspected = self._plan([self._entry("fp", state="ignored")], inspected=())
        self.assertEqual(uninspected, {})
        inspected = self._plan([self._entry("fp", state="ignored")])
        self.assertEqual(inspected["fp"]["state"], "unseen")


class PlanRechecksTests(unittest.TestCase):
    def _entry(self, path, category="security", state="present", seen_day=14, lines=(3,), repo="app"):
        return {
            "fingerprint": f"{repo}:{path}:{category}", "repo_slug": repo, "file_path": path,
            "category": category, "severity": "warning", "title": f"{path} {category}",
            "lines": list(lines), "state": state, "last_seen_at": datetime(2026, 9, seen_day, 1, 0),
        }

    def test_only_present_rows_are_rechecked_grouped_by_file(self):
        """本轮未发现的行已经取证过一次，已忽略的行用户说过不想再看，反复复核只是烧预算。"""
        from backend.survey.ledger import plan_rechecks

        rechecks, index = plan_rechecks([
            self._entry("lib/a.dart", "security", lines=(3,)),
            self._entry("lib/a.dart", "performance", lines=(40, 3)),
            self._entry("lib/b.dart", state="unseen"),
            self._entry("lib/c.dart", state="ignored"),
        ], max_files=10, ignored={"app:lib/c.dart:security"})
        self.assertEqual([r["file_path"] for r in rechecks], ["lib/a.dart"])
        self.assertEqual(rechecks[0]["anchor_lines"], [3, 40])
        self.assertEqual({p["category"] for p in rechecks[0]["previous"]}, {"security", "performance"})
        self.assertEqual(set(index), {("app", "lib/a.dart")})

    def test_least_recently_seen_files_go_first_and_overflow_stays_in_index(self):
        """超出名额的文件下一轮轮到；但 L1 点名了它们时仍要能把旧问题带上，所以索引里要有。"""
        from backend.survey.ledger import plan_rechecks

        rechecks, index = plan_rechecks([
            self._entry("lib/new.dart", seen_day=20),
            self._entry("lib/old.dart", seen_day=7),
            self._entry("lib/mid.dart", seen_day=14),
        ], max_files=2)
        self.assertEqual([r["file_path"] for r in rechecks], ["lib/old.dart", "lib/mid.dart"])
        self.assertIn(("app", "lib/new.dart"), index)

    def test_recently_checked_files_yield_even_if_never_seen_again(self):
        """复核总是没结论的文件不会刷新最近发现时间，按它排序会永远占着名额。"""
        from backend.survey.ledger import plan_rechecks

        stuck = {**self._entry("lib/stuck.dart", seen_day=1), "last_checked_at": datetime(2026, 9, 21)}
        rechecks, _ = plan_rechecks([stuck, self._entry("lib/waiting.dart", seen_day=14)], max_files=1)
        self.assertEqual([r["file_path"] for r in rechecks], ["lib/waiting.dart"])

    def test_unignored_rows_are_rechecked_but_still_ignored_rows_are_not(self):
        """取消忽略后停在已忽略的行，所在文件一被取证就会被判定，不交给 L2 就是没看就判。"""
        from backend.survey.ledger import plan_rechecks

        unignored = self._entry("lib/a.dart", state="ignored")
        still = self._entry("lib/b.dart", state="ignored")
        rechecks, _ = plan_rechecks([unignored, still], max_files=10, ignored={still["fingerprint"]})
        self.assertEqual([r["file_path"] for r in rechecks], ["lib/a.dart"])

    def test_dirty_line_numbers_are_skipped(self):
        from backend.survey.ledger import plan_rechecks

        rechecks, _ = plan_rechecks([self._entry("lib/a.dart", lines=(3, "x", None, -1))], max_files=1)
        self.assertEqual(rechecks[0]["anchor_lines"], [3])


class MergeFocusesTests(unittest.TestCase):
    def _recheck(self, path):
        return {"repo_slug": "app", "file_path": path, "previous": [{"category": "security", "severity": "warning",
                "title": "t", "lines": [3]}], "anchor_lines": [3]}

    def _planned(self, path, reason="L1 怀疑"):
        return {"repo_slug": "app", "file_path": path, "category": "performance", "reason": reason}

    def test_same_file_is_inspected_once_and_lists_alternate(self):
        from backend.survey.analysis import merge_focuses

        merged = merge_focuses(
            [self._recheck("lib/a.dart"), self._recheck("lib/b.dart"), self._recheck("lib/c.dart")],
            [self._planned("lib/b.dart"), self._planned("lib/x.dart")],
            {},
        )
        self.assertEqual([m["file_path"] for m in merged], ["lib/a.dart", "lib/x.dart", "lib/b.dart", "lib/c.dart"])
        twin = merged[2]
        self.assertEqual(twin["reason"], "[performance] L1 怀疑")
        # L1 怀疑的位置可能在旧问题行号之外，按行号截取会把它截掉
        self.assertEqual(twin["anchor_lines"], [])
        self.assertEqual(merged[0]["anchor_lines"], [3])

    def test_several_l1_picks_on_one_file_keep_every_reason(self):
        """只留第一条的话，其余几类怀疑进不了 L2，文件却被判成有结论。"""
        from backend.survey.analysis import merge_focuses

        second = {**self._planned("lib/a.dart", reason="接口字段不一致"), "category": "cross_repo"}
        merged = merge_focuses([], [self._planned("lib/a.dart"), second], {})
        self.assertEqual(len(merged), 1)
        self.assertIn("[performance] L1 怀疑", merged[0]["reason"])
        self.assertIn("[cross_repo] 接口字段不一致", merged[0]["reason"])

    def test_planned_file_with_pending_issues_carries_them(self):
        """L1 点名了没进复核名额的文件：不带上旧问题的话，这一轮取证会在模型不知情时把它们判成本轮未发现。"""
        from backend.survey.analysis import merge_focuses

        merged = merge_focuses([], [self._planned("lib/z.dart")], {("app", "lib/z.dart"): self._recheck("lib/z.dart")})
        self.assertEqual(merged[0]["previous"][0]["category"], "security")
        self.assertEqual(merged[0]["anchor_lines"], [])

    def test_legacy_unnormalized_ledger_path_still_merges(self):
        """台账里存着旧版本没规范化的路径：按原文比对会取证两次，产出的指纹也对不上原来那一行。"""
        from backend.survey.analysis import merge_focuses

        legacy = self._recheck("./lib/a.dart")
        merged = merge_focuses([legacy], [self._planned("lib/a.dart")], {("app", "./lib/a.dart"): legacy})
        self.assertEqual([m["file_path"] for m in merged], ["./lib/a.dart"])

        merged = merge_focuses([], [self._planned("lib/a.dart")], {("app", "./lib/a.dart"): legacy})
        self.assertEqual(merged[0]["file_path"], "./lib/a.dart")
        self.assertEqual(len(merged[0]["previous"]), 1)


class NormalizeFilePathTests(unittest.TestCase):
    def test_equivalent_spellings_collapse(self):
        """同一个文件的不同写法会被取证两次，指纹也对不上台账里原有的那一行。"""
        from backend.survey.analysis import normalize_file_path

        for raw in ("src/a.py", "./src/a.py", "src//a.py", "/src/a.py", "src\\a.py", " src/./a.py "):
            self.assertEqual(normalize_file_path(raw), "src/a.py", raw)
        self.assertEqual(normalize_file_path("."), "")
        self.assertEqual(normalize_file_path(""), "")


class FocusSourceTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.repo_dir = Path(tempfile.mkdtemp(prefix="opencr-focus-"))
        (self.repo_dir / "lib").mkdir()
        self.long_file = self.repo_dir / "lib" / "long.dart"
        self.long_file.write_text("\n".join(f"line {n}" for n in range(1, 2001)), encoding="utf-8")

    def _read(self, path, max_chars=1000, anchors=None):
        from backend.survey.analysis import _read_focus_source

        return _read_focus_source(self.repo_dir, path, max_chars, anchors)

    def test_short_file_is_complete(self):
        (self.repo_dir / "lib" / "short.dart").write_text("x", encoding="utf-8")
        source = self._read("lib/short.dart")
        self.assertTrue(source.complete)
        self.assertFalse(source.numbered)

    def test_truncated_file_without_anchors_is_not_conclusive(self):
        """只读了开头：问题可能恰好在被截掉的部分，模型没报不能算没有。"""
        self.assertFalse(self._read("lib/long.dart").complete)

    def test_anchor_windows_carry_line_numbers_but_are_not_conclusive(self):
        """
        摘录只用来让模型有机会再次报出旧问题。上方插进几十行代码，问题就被挤出窗口，
        据此判本轮未发现正是"没去看却判成没有"的老问题。
        """
        source = self._read("lib/long.dart", max_chars=3000, anchors=[1500])
        self.assertTrue(source.numbered)
        self.assertIn("  1500| line 1500", source.text)
        self.assertNotIn("| line 1\n", source.text)
        self.assertFalse(source.complete)

    def test_windows_that_do_not_fit_are_dropped_whole(self):
        source = self._read("lib/long.dart", max_chars=3000, anchors=[100, 1500])
        self.assertIn("   100| line 100", source.text)
        self.assertNotIn("1500| line 1500", source.text)

    def test_missing_file_is_conclusive_and_escape_is_not(self):
        missing = self._read("lib/deleted.dart")
        self.assertTrue(missing.missing)
        self.assertTrue(missing.complete)
        self.assertIsNone(self._read("../outside.dart"))
        sibling = Path(str(self.repo_dir) + "-evil")
        sibling.mkdir()
        (sibling / "a.dart").write_text("x", encoding="utf-8")
        # 前缀相同的兄弟目录也算越界
        self.assertIsNone(self._read(f"../{sibling.name}/a.dart"))


class InspectFocusTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.repo_dir = Path(tempfile.mkdtemp(prefix="opencr-inspect-"))
        (self.repo_dir / "a.dart").write_text("void main() {}\n", encoding="utf-8")

    def _inspect(self, reply, focus=None):
        from backend.survey import analysis

        budget = analysis.Budget(10, 10000, 5, 10000)
        focus = focus or {"repo_slug": "app", "file_path": "a.dart", "category": "security", "reason": "怀疑"}
        with mock.patch.object(analysis, "_call_model", return_value=reply) as call:
            result = analysis.inspect_focus(focus, self.repo_dir, "", budget)
        return result, call

    def test_unparseable_output_is_not_conclusive(self):
        """模型输出解析不了时同样是零条，但不能当成"看过、没有问题"。"""
        (findings, conclusive), _ = self._inspect("抱歉，我无法确定")
        self.assertEqual(findings, [])
        self.assertFalse(conclusive)

    def test_empty_array_is_conclusive(self):
        (findings, conclusive), _ = self._inspect("[]")
        self.assertEqual(findings, [])
        self.assertTrue(conclusive)

    def test_previous_issues_are_listed_in_prompt(self):
        focus = {"repo_slug": "app", "file_path": "a.dart", "category": "security", "reason": "",
                 "previous": [{"category": "security", "severity": "warning", "title": "硬编码密钥", "lines": [1]}],
                 "anchor_lines": [1]}
        _, call = self._inspect("[]", focus)
        prompt = call.call_args[0][0]
        self.assertIn("硬编码密钥", prompt)
        self.assertIn("沿用", prompt)

    def test_ignored_issues_are_offered_and_only_offered_refs_are_accepted(self):
        """模型偶尔会编一个编号；接受它就等于让模型去隐藏任意一个问题。"""
        focus = {"repo_slug": "app", "file_path": "a.dart", "category": "correctness", "reason": "",
                 "ignored": [{"id": 7, "category": "correctness", "line": 1, "title": "强制解包", "body": "回调里 x!"}]}
        reply = (
            '[{"line":1,"category":"correctness","severity":"warning","title":"同一个","body":"b","ignore_ref":"I7"},'
            '{"line":1,"category":"correctness","severity":"warning","title":"裸数字","body":"b","ignore_ref":"7"},'
            '{"line":1,"category":"correctness","severity":"warning","title":"编出来的","body":"b","ignore_ref":"I8"},'
            '{"line":1,"category":"correctness","severity":"warning","title":"新问题","body":"b"}]'
        )
        (findings, _), call = self._inspect(reply, focus)
        prompt = call.call_args[0][0]
        self.assertIn("I7", prompt)
        self.assertIn("强制解包", prompt)
        self.assertEqual({f["title"]: f["ignore_id"] for f in findings},
                         {"同一个": 7, "裸数字": 7, "编出来的": None, "新问题": None})

    def test_prompt_has_no_ignored_block_without_ignores(self):
        """没有已忽略问题时不该多出一段让模型困惑的说明。"""
        _, call = self._inspect("[]")
        self.assertNotIn("ignore_ref 填成", call.call_args[0][0])

    def test_deleted_file_needs_no_model_call(self):
        focus = {"repo_slug": "app", "file_path": "gone.dart", "category": "security", "reason": ""}
        (findings, conclusive), call = self._inspect("[]", focus)
        self.assertEqual((findings, conclusive), ([], True))
        call.assert_not_called()


class IgnoreMatchingTests(unittest.TestCase):
    """已忽略问题的分发与按标题兜底（survey/ignores.py）。"""

    def _ignore(self, id_, path="lib/a.dart", title="强制解包", category="correctness"):
        """构造一条生效的已忽略问题。"""
        from backend.survey.common import finding_fingerprint

        return {"id": id_, "repo_slug": "app", "file_path": path, "category": category, "line": 1,
                "title": title, "body": "", "fingerprint": finding_fingerprint("app", path, category)}

    def test_attach_matches_files_by_normalized_path_and_caps_count(self):
        """路径写法不同也要挂到同一个文件上；一个文件交给模型的条数有上限。"""
        from backend.survey.ignores import MAX_IGNORED_PER_FOCUS, attach_ignored

        focuses = [{"repo_slug": "app", "file_path": "./lib/a.dart"}, {"repo_slug": "app", "file_path": "lib/b.dart"}]
        ignores = [self._ignore(i) for i in range(1, MAX_IGNORED_PER_FOCUS + 3)]
        attach_ignored(focuses, ignores)
        ids = [i["id"] for i in focuses[0]["ignored"]]
        # 超出上限时保留最新标记的
        self.assertEqual(len(ids), MAX_IGNORED_PER_FOCUS)
        self.assertEqual(ids[0], MAX_IGNORED_PER_FOCUS + 2)
        self.assertEqual(focuses[1]["ignored"], [])

    def test_title_fallback_needs_same_fingerprint_and_same_title(self):
        """相似就算命中的话，同一文件里换了个说法的另一个问题也会被吞掉。"""
        from backend.survey.common import finding_fingerprint
        from backend.survey.ignores import match_by_title

        fp = finding_fingerprint("app", "lib/a.dart", "correctness")
        findings = [
            {"fingerprint": fp, "title": " 强制解包 "},
            {"fingerprint": fp, "title": "强制解包空返回值"},
            {"fingerprint": "other", "title": "强制解包"},
            {"fingerprint": fp, "title": "强制解包", "ignore_id": 5},
        ]
        self.assertEqual(match_by_title(findings, [self._ignore(3)]), 1)
        self.assertEqual([f.get("ignore_id") for f in findings], [3, None, None, 5])


class CollectFindingsTests(unittest.TestCase):
    def test_only_conclusive_focuses_count_as_inspected(self):
        """模型调用失败、结论不完整的文件都不进已取证集合，名下的台账行因此保持原状态。"""
        from backend.survey import runner
        from backend.survey.analysis import Budget
        from backend.survey.common import SurveyError

        focuses = [{"repo_slug": "app", "file_path": p, "category": "security", "reason": ""}
                   for p in ("ok.dart", "partial.dart", "boom.dart", "unknown-repo.dart")]
        focuses[3]["repo_slug"] = "ghost"
        replies = {
            "ok.dart": ([{"file_path": "ok.dart"}], True),
            "partial.dart": ([], False),
            "boom.dart": SurveyError("模型调用失败"),
        }

        def fake_inspect(focus, *_args):
            reply = replies[focus["file_path"]]
            if isinstance(reply, Exception):
                raise reply
            return reply

        with mock.patch.object(runner, "inspect_focus", side_effect=fake_inspect), \
                mock.patch.object(runner.repo, "survey_heartbeat"):
            findings, inspected = runner._collect_findings(
                focuses, [{"slug": "app", "dir": Path(".")}], "", Budget(10, 10000, 5, 10000), "run-1"
            )
        # clue_source 继承自关注点，这里的关注点没有标注，所以是空串
        self.assertEqual(findings, [{"file_path": "ok.dart", "clue_source": ""}])
        # 没结论的也要记下来：复核按"上次交给 L2 的时刻"轮转，否则它们永远排在队首
        self.assertEqual(inspected, [
            {"repo_slug": "app", "file_path": "ok.dart", "conclusive": True},
            {"repo_slug": "app", "file_path": "partial.dart", "conclusive": False},
            {"repo_slug": "app", "file_path": "boom.dart", "conclusive": False},
        ])


class LedgerRowsTests(unittest.TestCase):
    SURVEY = {"slug": "mobile-weekly", "name": "移动端周巡检", "timezone": "Asia/Shanghai"}

    def _entries(self):
        base = {
            "repo_slug": "app", "file_path": "lib/a.dart", "category": "security", "severity": "critical",
            "title": "t", "body": "b", "lines": [3, 9], "finding_count": 2,
            "first_seen_at": datetime(2026, 9, 14, 1, 0), "last_seen_at": datetime(2026, 9, 21, 1, 0),
            "last_run_uid": "run-uid",
        }
        return [
            {**base, "fingerprint": "fp-present", "state": "present"},
            {**base, "fingerprint": "fp-unseen", "state": "unseen"},
            {**base, "fingerprint": "fp-ignored", "state": "ignored"},
        ]

    def test_restore_rules(self):
        """推送过的表只补回仍存在的行；从未推送过的新表拿到完整台账。"""
        from backend.survey.ledger import build_ledger_rows

        later = {r.key: r.restore_if_missing for r in build_ledger_rows(self.SURVEY, self._entries(), "", False)}
        self.assertEqual(later, {
            "mobile-weekly:fp-present": True,
            "mobile-weekly:fp-unseen": False,
            "mobile-weekly:fp-ignored": False,
        })
        first = build_ledger_rows(self.SURVEY, self._entries(), "", True)
        self.assertTrue(all(r.restore_if_missing for r in first))

    def test_values_are_human_readable(self):
        from backend.survey.ledger import build_ledger_rows

        row = build_ledger_rows(self.SURVEY, self._entries(), "https://cr.example.com/", False)[1]
        self.assertEqual(row.values["state"], "本轮未发现")
        self.assertEqual(row.values["severity"], "严重")
        self.assertEqual(row.values["category"], "安全")
        self.assertEqual(row.values["lines"], "3, 9")
        # 存的是 UTC，表里显示巡检所在时区
        self.assertEqual(row.values["first_seen"].strftime("%Y-%m-%d %H:%M"), "2026-09-14 09:00")
        # 后台是 hash 路由
        self.assertEqual(row.values["link"], "https://cr.example.com/admin/#/surveys/runs/run-uid")

    def test_link_column_only_with_public_url(self):
        from backend.survey.ledger import ledger_columns

        self.assertNotIn("link", [c.key for c in ledger_columns(False)])
        self.assertEqual(ledger_columns(True)[-1].key, "link")
        # 键列必须在第一位，插件据此定位行
        self.assertEqual(ledger_columns(False)[0].key, "key")


# ---------------------------------------------------------------------------
# 持久化：Ledger 更新、Binding 同步
# ---------------------------------------------------------------------------

class LedgerStorageTestCase(SurveyStorageTestCase):
    def _complete_run(self, findings, repos=(("app", "ok"),), degradations=(), status="succeeded", inspected=()):
        """
        跑一轮完整的运行：登记仓库、落库发现、记录已取证文件、收尾并更新 Ledger。

        产出所在的文件自动算作取证过；inspected 额外列出取证过但没有产出的文件路径（仓库为 app）。
        """
        run_uid = self.repo.start_survey_run(self.survey["survey_uid"], "schedule")
        for slug, repo_status in repos:
            self.repo.record_survey_repo(run_uid, slug, f"https://g.com/a/{slug}.git", status=repo_status)
        for kind in degradations:
            self.repo.add_survey_degradation(run_uid, kind)
        self.repo.record_survey_findings(run_uid, list(findings))
        self.repo.record_survey_inspected_files(
            run_uid,
            [{"repo_slug": f["repo_slug"], "file_path": f["file_path"], "conclusive": True} for f in findings]
            + [{"repo_slug": "app", "file_path": path, "conclusive": True} for path in inspected],
        )
        self.repo.finish_survey_run(run_uid, status)

        from backend.survey.ledger import update_ledger_for_run

        update_ledger_for_run(run_uid)
        return run_uid

    def _ledger(self):
        return {e["file_path"]: e for e in self.repo.list_survey_ledger(self._survey_id())}

    def _survey_id(self):
        from sqlalchemy import select

        from backend.storage.db import session_scope
        from backend.storage.models import Survey

        with session_scope() as session:
            return session.scalar(select(Survey.id).where(Survey.survey_uid == self.survey["survey_uid"]))


class LedgerStorageTests(LedgerStorageTestCase):
    def test_ledger_follows_runs(self):
        self._complete_run([_finding("lib/a.dart", line=1), _finding("lib/a.dart", line=7), _finding("lib/b.dart")])
        ledger = self._ledger()
        self.assertEqual(ledger["lib/a.dart"]["finding_count"], 2)
        self.assertEqual(ledger["lib/a.dart"]["lines"], [1, 7])

        self._complete_run([_finding("lib/a.dart")], inspected=["lib/b.dart"])
        ledger = self._ledger()
        self.assertEqual(ledger["lib/a.dart"]["state"], "present")
        self.assertEqual(ledger["lib/b.dart"]["state"], "unseen")

        self._complete_run([_finding("lib/b.dart")])
        self.assertEqual(self._ledger()["lib/b.dart"]["state"], "present")

    def test_uninspected_file_keeps_its_state(self):
        """这正是用户撞到的那次：仓库好好的，只是第二轮 L1 没再点名这个文件。"""
        self._complete_run([_finding("lib/a.dart")])
        self._complete_run([_finding("lib/other.dart")])
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "present")

    def test_budget_exhausted_run_still_judges_files_it_finished(self):
        """预算耗尽只影响没轮到的文件；已经取证完的文件结论照样可信。"""
        self._complete_run([_finding("lib/a.dart"), _finding("lib/b.dart")])
        self._complete_run([], degradations=["budget_exhausted"], inspected=["lib/a.dart"])
        ledger = self._ledger()
        self.assertEqual(ledger["lib/a.dart"]["state"], "unseen")
        self.assertEqual(ledger["lib/b.dart"]["state"], "present")

    def test_failed_run_never_touches_ledger(self):
        """失败的运行产出不完整，拿它更新台账会把一大批问题错标成本轮未发现。"""
        self._complete_run([_finding("lib/a.dart")])
        self._complete_run([], status="failed")
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "present")

    def _finding_ids(self, run_uid):
        """某次运行里可见发现的 {标题: id}。"""
        return {f["title"]: f["id"] for f in self.repo.get_survey_run_detail(run_uid)["findings"]}

    def test_ignore_flips_ledger_row_once_every_issue_of_it_is_ignored(self):
        """
        台账一行是一个指纹，可能合并了好几个问题：还剩没被忽略的，这一行就仍然存在。
        全部忽略后立刻转为已忽略 —— 手动重推一次，表里就应该看到变化，而不是等下一轮巡检。
        """
        uid = self.survey["survey_uid"]
        run_uid = self._complete_run([_finding("lib/a.dart", line=1, title="A"), _finding("lib/a.dart", line=9, title="B")])
        ids = self._finding_ids(run_uid)
        self.repo.add_survey_ignore(uid, ids["A"])
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "present")
        ignore_b = self.repo.add_survey_ignore(uid, ids["B"])
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "ignored")
        # 取消忽略不会凭空改回存在
        self.repo.remove_survey_ignore(uid, ignore_b)
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "ignored")

    def test_ledger_state_follows_the_rows_latest_run(self):
        """
        台账行按它最近一次出现的那轮判断：在旧报告里点，原样报过的同一个问题一起隐藏、行随之转为已忽略；
        最近一轮换了说法、模型还没认出来的，行仍然存在。
        """
        first = self._complete_run([_finding("lib/a.dart", title="A")])
        self._complete_run([_finding("lib/a.dart", title="A")])
        self.repo.add_survey_ignore(self.survey["survey_uid"], self._finding_ids(first)["A"])
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "ignored")

        second = self._complete_run([_finding("lib/b.dart", title="B")])
        self._complete_run([_finding("lib/b.dart", title="B 换了说法")])
        self.repo.add_survey_ignore(self.survey["survey_uid"], self._finding_ids(second)["B"])
        self.assertEqual(self._ledger()["lib/b.dart"]["state"], "present")

    def test_ledger_skips_ignored_issues_and_marks_fully_ignored_rows(self):
        """被认出的已知问题不进台账正文与条数；一行里只剩已知问题时是已忽略，而不是本轮未发现。"""
        uid = self.survey["survey_uid"]
        first = self._complete_run([_finding("lib/a.dart", title="A")])
        ignore_id = self.repo.add_survey_ignore(uid, self._finding_ids(first)["A"])

        known = {**_finding("lib/a.dart", line=4, title="A 换了说法"), "ignore_id": ignore_id}
        self._complete_run([known, _finding("lib/a.dart", line=20, title="新问题")])
        row = self._ledger()["lib/a.dart"]
        self.assertEqual((row["state"], row["finding_count"], row["title"]), ("present", 1, "新问题"))

        self._complete_run([{**known}])
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "ignored")

    def test_ledger_outlives_run_retention(self):
        """Ledger 寿命跟随 Survey：运行记录按次数清理后，首次发现时间仍然是最初那一轮。"""
        self._complete_run([_finding("lib/a.dart")])
        first_seen = self._ledger()["lib/a.dart"]["first_seen_at"]
        self.repo.update_survey(self.survey["survey_uid"], {"retention_runs": 1})
        for _ in range(2):
            self._complete_run([_finding("lib/a.dart")])
            self.repo.purge_survey_runs_by_uid(self.survey["survey_uid"])
        self.assertEqual(len(self.repo.list_survey_runs(self.survey["survey_uid"])), 1)
        self.assertEqual(self._ledger()["lib/a.dart"]["first_seen_at"], first_seen)


class IgnoredRepoTests(LedgerStorageTestCase):
    """已忽略仓库：组织里不维护、不重要的仓库点名剔除，台账与运行记录都要看得出它被跳过了。"""

    def test_ignoring_a_repo_flips_its_ledger_rows_at_once(self):
        """不立刻改的话，下一轮跑完之前推送出去的镜像仍然显示这些行"存在"。"""
        uid = self.survey["survey_uid"]
        self._complete_run([_finding("lib/a.dart", repo="group-old"), _finding("lib/b.dart")],
                           repos=(("group-old", "ok"), ("app", "ok")))
        # 仓库地址按 repo_slug_from_url 换算成仓库身份，与组织展开出来的仓库一致
        self.repo.add_survey_ignored_repo(uid, "https://g.com/top/group/old.git", "没人维护了")
        self.assertEqual(self.repo.list_survey_ignored_repo_slugs(uid), {"group-old"})
        ledger = self._ledger()
        self.assertEqual(ledger["lib/a.dart"]["state"], "ignored")
        self.assertEqual(ledger["lib/b.dart"]["state"], "present")

    def test_ledger_rows_of_ignored_repo_are_marked_and_stay_ignored(self):
        self._complete_run([_finding("lib/a.dart", repo="old"), _finding("lib/b.dart")],
                           repos=(("old", "ok"), ("app", "ok")))
        self._complete_run([_finding("lib/b.dart")], repos=(("old", "ignored"), ("app", "ok")))
        ledger = self._ledger()
        self.assertEqual(ledger["lib/a.dart"]["state"], "ignored")
        self.assertEqual(ledger["lib/b.dart"]["state"], "present")

        # 取消忽略后的第一轮没取证到它，行停在已忽略，不凭空改回存在
        self._complete_run([_finding("lib/b.dart")], repos=(("old", "ok"), ("app", "ok")))
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "ignored")

    def test_add_marks_existing_rows_and_keeps_first_note(self):
        uid = self.survey["survey_uid"]
        self._complete_run([_finding("lib/a.dart", repo="g-old")], repos=(("g-old", "ok"),))
        self._complete_run([], repos=(("g-old", "ok"),), inspected=[])
        first, slug = self.repo.add_survey_ignored_repo(uid, "g/old")
        self.assertEqual(slug, "g-old")
        self.assertEqual(self._ledger()["lib/a.dart"]["state"], "ignored")

        again = self.repo.add_survey_ignored_repo(uid, "https://x.com/g/old.git", "迁移到新仓库了")
        self.assertEqual(again, (first, "g-old"))
        self.repo.add_survey_ignored_repo(uid, "g/old", "另一个理由")
        items = self.repo.list_survey_ignored_repos(uid)
        self.assertEqual([(i["repo_slug"], i["note"]) for i in items], [("g-old", "迁移到新仓库了")])

        self.assertTrue(self.repo.update_survey_ignored_repo_note(uid, first, "  "))
        self.assertEqual(self.repo.list_survey_ignored_repos(uid)[0]["note"], "")
        self.assertFalse(self.repo.remove_survey_ignored_repo("missing", first))
        self.assertTrue(self.repo.remove_survey_ignored_repo(uid, first))
        self.assertEqual(self.repo.list_survey_ignored_repos(uid), [])

    def test_bare_project_name_is_rejected(self):
        """只填项目名算出的 slug 少了组织一段，这条忽略永远不会命中，宁可当场拒绝。"""
        for bad in ("", "  ", "old", "/old/"):
            with self.assertRaises(ValueError):
                self.repo.add_survey_ignored_repo(self.survey["survey_uid"], bad)
        self.assertIsNone(self.repo.add_survey_ignored_repo("missing", "g/old"))

    def test_candidates_come_from_latest_run_minus_ignored(self):
        uid = self.survey["survey_uid"]
        self.assertEqual(self.repo.list_survey_repo_candidates(uid), {"items": [], "run_uid": "", "run_started_at": ""})
        self._complete_run([], repos=(("g-a", "ok"),))
        self._complete_run([], repos=(("g-b", "ok"), ("g-c", "fetch_failed"), ("g-a", "ignored")))
        # 组织展开失败的行以组织地址充当 repo_slug，它不是仓库，不能当候选
        latest = self.repo.list_survey_runs(uid)[0]["run_uid"]
        self.repo.record_survey_repo(latest, "grp/sub", "grp/sub", status="fetch_failed", error_message="404")
        self.repo.add_survey_ignored_repo(uid, "g/a")
        self.repo.add_survey_ignored_repo(uid, "g/c")
        candidates = self.repo.list_survey_repo_candidates(uid)
        self.assertEqual([c["repo_slug"] for c in candidates["items"]], ["g-b"])
        # 清单来自哪一轮要带上，界面据此提示它可能过时
        self.assertEqual(candidates["run_uid"], latest)
        self.assertTrue(candidates["run_started_at"])

    def test_run_skips_ignored_repos_and_records_them(self):
        """全部仓库都被忽略时判失败并说清原因；被跳过的仓库留在运行记录里，而不是悄无声息地少了一个。"""
        from backend.survey import runner

        uid = self.survey["survey_uid"]
        self.repo.add_survey_ignored_repo(uid, "https://g.com/a/app.git", "")
        with mock.patch.object(runner, "_prepare_workspaces") as prepare, \
                mock.patch.object(runner, "_cleanup"):
            run_uid = runner.execute_survey_run(uid, "manual")
        prepare.assert_not_called()
        detail = self.repo.get_survey_run_detail(run_uid)
        self.assertEqual(detail["status"], "failed")
        self.assertIn("忽略清单", detail["error_message"])
        self.assertEqual([(r["repo_slug"], r["status"]) for r in detail["repos"]], [("a-app", "ignored")])


class IgnoredRepoRunTests(LedgerStorageTestCase):
    """执行链路与报告：被跳过的仓库要看得出来，运行中途加入忽略也不能被那一轮写回。"""

    def test_parse_repo_reference(self):
        from backend.survey.common import parse_repo_reference

        self.assertEqual(parse_repo_reference(" https://g.com/top/grp/proj/-/tree/main?x=1 "),
                         ("https://g.com/top/grp/proj", "grp-proj"))
        self.assertEqual(parse_repo_reference("git@g.com:grp/proj.git")[1], "grp-proj")
        self.assertEqual(parse_repo_reference("grp/proj/")[1], "grp-proj")
        for bad in ("", "proj", "https://g.com", "https://g.com/", "https://g.com/proj/-/tree", "git@g.com:proj"):
            with self.assertRaises(ValueError, msg=bad):
                parse_repo_reference(bad)

    def test_truncated_and_ignored_repos_are_recorded_and_offered(self):
        """仓库太多时最想剔除的就是被截掉的那批：它们要记进运行记录，才能出现在候选里。"""
        from backend.survey import runner

        uid = self.survey["survey_uid"]
        self.repo.update_survey(uid, {}, sources=[
            {"kind": "repo", "url": f"https://g.com/grp/r{i}.git"} for i in range(4)
        ])
        self.repo.add_survey_ignored_repo(uid, "grp/r0")
        with mock.patch.object(runner, "load_max_repos", return_value=2), \
                mock.patch.object(runner, "_prepare_workspaces", return_value=[]), \
                mock.patch.object(runner, "_cleanup"):
            run_uid = runner.execute_survey_run(uid, "manual")
        detail = self.repo.get_survey_run_detail(run_uid)
        self.assertEqual(
            sorted((r["repo_slug"], r["status"]) for r in detail["repos"]),
            [("grp-r0", "ignored"), ("grp-r3", "truncated")],
        )
        self.assertEqual([c["repo_slug"] for c in self.repo.list_survey_repo_candidates(uid)["items"]], ["grp-r3"])

        from backend.survey.report import render_run_markdown

        markdown = render_run_markdown(detail)
        self.assertIn("## 未参与的仓库", markdown)
        self.assertIn("grp-r3（超出单次巡检的仓库上限）", markdown)
        self.assertNotIn("| grp-r0 |", markdown)

    def test_ignoring_during_a_run_is_not_undone_by_it(self):
        """运行进行中才加入忽略的仓库已经被分析过了，那一轮收尾不能把刚转为已忽略的行写回存在。"""
        uid = self.survey["survey_uid"]
        self._complete_run([_finding("lib/a.dart", repo="grp-old")], repos=(("grp-old", "ok"),))
        run_uid = self.repo.start_survey_run(uid, "schedule")
        self.repo.record_survey_repo(run_uid, "grp-old", "https://g.com/grp/old.git", status="ok")
        self.repo.record_survey_findings(run_uid, [_finding("lib/a.dart", repo="grp-old", title="新说法")])
        self.repo.add_survey_ignored_repo(uid, "grp/old")
        self.repo.finish_survey_run(run_uid, "succeeded")

        from backend.survey.ledger import update_ledger_for_run

        update_ledger_for_run(run_uid)
        row = self._ledger()["lib/a.dart"]
        self.assertEqual((row["state"], row["title"]), ("ignored", "新说法"))


class IgnoredRepoRouteTests(LedgerStorageTestCase):
    def setUp(self):
        super().setUp()
        from flask import Flask

        from backend.admin import auth, routes

        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        self.app.register_blueprint(routes.admin_bp)
        self.client = self.app.test_client()
        patch = mock.patch.object(auth, "load_admin_config", return_value={"enabled": True, "bind_local_only": False})
        patch.start()
        self.addCleanup(patch.stop)
        self.base = f"/api/admin/surveys/{self.survey['survey_uid']}/ignored-repos"

    def test_live_candidates_follow_current_config(self):
        """
        手动刷新按当前配置实时展开：与下一轮巡检会处理的仓库一致，截掉的照列、已忽略的不列，
        展开失败的组织单独报出来而不是混进候选；结果不落库。
        """
        from backend.survey import sources

        uid = self.survey["survey_uid"]
        self.repo.update_survey(uid, {}, sources=[
            {"kind": "repo", "url": "https://g.com/grp/kept.git"},
            {"kind": "repo", "url": "https://g.com/grp/ignored.git"},
            {"kind": "org", "url": "grp/broken"},
            {"kind": "org", "url": "grp/team"},
        ])
        self.repo.add_survey_ignored_repo(uid, "grp/ignored")

        def expand(path, patterns):
            """grp/broken 展开失败，grp/team 下有两个仓库。"""
            if path == "grp/broken":
                raise RuntimeError("404 Group Not Found")
            return [{"url": f"https://g.com/team/{n}.git", "branch": "", "path": f"team/{n}"} for n in ("b", "a")]

        with self.client.session_transaction() as session:
            session["identity"] = "admin"
        with mock.patch.object(sources, "expand_group", side_effect=expand), \
                mock.patch.object(sources, "check_platform_ready"), \
                mock.patch("backend.admin.routes.load_max_repos", return_value=2):
            result = self.client.get(f"{self.base}/live-candidates").json
        self.assertEqual([c["repo_slug"] for c in result["items"]], ["grp-kept", "team-a", "team-b"])
        self.assertEqual(result["errors"], [{"source": "grp/broken", "error": "404 Group Not Found"}])
        self.assertEqual(self.repo.list_survey_runs(uid), [])

        with mock.patch("backend.admin.routes.live_repo_candidates", side_effect=RuntimeError("boom")):
            self.assertEqual(self.client.get(f"{self.base}/live-candidates").status_code, 502)
        self.assertEqual(self.client.get("/api/admin/surveys/missing/ignored-repos/live-candidates").status_code, 404)

    def test_platform_name_follows_config(self):
        """项目不只对接 GitLab：刷新按钮与说明里的平台名按 code_platform.type 显示，未知平台用通用叫法。"""
        with self.client.session_transaction() as session:
            session["identity"] = "admin"
        for platform, expected in (("gitlab", "GitLab"), ("github", "GitHub"), ("gitea", "代码平台")):
            with mock.patch("backend.admin.routes.load_gitlab_config", return_value={"type": platform}):
                self.assertEqual(self.client.get(self.base).json["platform_name"], expected, platform)

    def test_org_sources_on_other_platforms_fail_clearly(self):
        """组织展开只实现了 GitLab：别的平台要明说不支持，而不是按 GitLab 路由发请求、只报一个 404。"""
        from backend.survey import sources

        with mock.patch.object(sources, "load_gitlab_config", return_value={"type": "github"}), \
                mock.patch.object(sources, "expand_group") as expand:
            result = sources.resolve_sources(
                [{"kind": "org", "url": "team"}, {"kind": "repo", "url": "https://github.com/team/app.git"}], 10
            )
        expand.assert_not_called()
        self.assertIn("只支持 GitLab", result.targets[0]["error"])
        self.assertEqual(result.targets[1]["slug"], "team-app")

    def test_live_candidates_are_admin_only(self):
        """实时展开会访问 GitLab 并列出组织下的全部仓库，与忽略清单同一档，Guest 不可调用。"""
        self.assertEqual(self.client.get(f"{self.base}/live-candidates").status_code, 401)

    def test_admin_only_and_round_trip(self):
        self.repo.set_setting("guest_retry", "1")
        self.assertEqual(self.client.get(self.base).status_code, 401)
        self.assertEqual(self.client.post(self.base, json={"url": "g/a"}).status_code, 401)

        with self.client.session_transaction() as session:
            session["identity"] = "admin"
        self._complete_run([], repos=(("g-a", "ok"), ("g-b", "ok")))
        self.assertEqual(self.client.post(self.base, json={"url": "old"}).status_code, 400)
        created = self.client.post(self.base, json={"url": "g/a", "note": "停更"})
        self.assertEqual(created.status_code, 201)
        item_id = created.json["id"]
        self.assertEqual(created.json["repo_slug"], "g-a")

        listed = self.client.get(self.base).json
        self.assertEqual([i["repo_slug"] for i in listed["items"]], ["g-a"])
        self.assertEqual([c["repo_slug"] for c in listed["candidates"]], ["g-b"])
        self.assertTrue(listed["candidates_run_uid"])
        self.assertTrue(listed["candidates_run_started_at"])

        self.assertEqual(self.client.patch(f"{self.base}/{item_id}", json={"note": "x"}).status_code, 200)
        self.assertEqual(self.client.patch(f"{self.base}/999", json={"note": "x"}).status_code, 404)
        self.assertTrue(self.client.delete(f"{self.base}/{item_id}").json["removed"])
        self.assertEqual(self.client.get("/api/admin/surveys/missing/ignored-repos").status_code, 404)
        self.assertEqual(self.client.post("/api/admin/surveys/missing/ignored-repos", json={"url": "g/a"}).status_code, 404)


class BindingStorageTests(SurveyStorageTestCase):
    SHEET_A = {"destination": "sheet", "target": {"spreadsheet": "a" * 30, "worksheet": "W"}}
    SHEET_B = {"destination": "sheet", "target": {"spreadsheet": "b" * 30, "worksheet": "W"}}

    def test_saving_survey_keeps_push_history_of_unchanged_bindings(self):
        """
        Binding 按实例名 + 目标位置增量同步。整体重建会清空最近成功推送时刻，
        下一次推送就会把用户删掉的行统统写回去。
        """
        from backend.storage.db import session_scope
        from backend.storage.models import SurveyBinding, utcnow

        uid = self.survey["survey_uid"]
        survey = self.repo.update_survey(uid, {}, bindings=[self.SHEET_A])
        binding_id = survey["bindings"][0]["id"]
        with session_scope() as session:
            session.get(SurveyBinding, binding_id).last_succeeded_push_at = utcnow()

        survey = self.repo.update_survey(uid, {"name": "改名"}, bindings=[self.SHEET_A, self.SHEET_B])
        by_target = {b["target"]["spreadsheet"][0]: b for b in survey["bindings"]}
        self.assertEqual(by_target["a"]["id"], binding_id)
        self.assertTrue(by_target["a"]["last_succeeded_push_at"])
        self.assertFalse(by_target["b"]["last_succeeded_push_at"])

        # 不传 bindings 表示不动
        self.assertEqual(len(self.repo.update_survey(uid, {"enabled": True})["bindings"]), 2)
        # 传空数组表示全部移除
        self.assertEqual(self.repo.update_survey(uid, {}, bindings=[])["bindings"], [])


# ---------------------------------------------------------------------------
# 推送编排（假插件）
# ---------------------------------------------------------------------------

class _FakeDestination:
    """记录每次推送收到的行；raise_error 非空时模拟平台故障。"""

    calls = []
    raise_error = ""


def _fake_destination_class():
    from backend.survey.destinations.base import Destination, DestinationError, PushStats, TargetField

    class FakeDestination(Destination):
        type_name = "fake"
        label = "假平台"
        target_fields = (TargetField("table", "表"),)

        def push(self, target, columns, rows):
            if _FakeDestination.raise_error:
                raise DestinationError(_FakeDestination.raise_error)
            _FakeDestination.calls.append({"target": target, "columns": columns, "rows": rows})
            return PushStats(updated=0, inserted=len(rows))

    return FakeDestination


class PushTestCase(LedgerStorageTestCase):
    """注册一个假的 Destination 类型与实例，并给巡检绑定它。"""

    def setUp(self):
        super().setUp()
        from backend.survey import destinations

        _FakeDestination.calls = []
        _FakeDestination.raise_error = ""
        patches = [
            mock.patch.dict(destinations.DESTINATION_TYPES, {"fake": _fake_destination_class()}),
            mock.patch.object(destinations, "load_file_config", return_value={
                "destinations": {"fake-1": {"type": "fake"}},
                "server": {"public_url": ""},
            }),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.survey = self.repo.update_survey(
            self.survey["survey_uid"], {}, bindings=[{"destination": "fake-1", "target": {"table": "T"}}]
        )
        self.binding_id = self.survey["bindings"][0]["id"]

    def _push(self, run_uid, trigger="manual", binding_id=None):
        from backend.survey.push import begin_pushes, execute_pushes

        started = begin_pushes(run_uid, trigger, binding_id)
        execute_pushes(started["started"])
        return started


class PushFlowTests(PushTestCase):
    def test_first_push_writes_full_ledger_then_only_restores_present(self):
        self._complete_run([_finding("lib/a.dart"), _finding("lib/b.dart")])
        run2 = self._complete_run([_finding("lib/a.dart")], inspected=["lib/b.dart"])

        self._push(run2)
        first = {r.values["file"]: r.restore_if_missing for r in _FakeDestination.calls[-1]["rows"]}
        self.assertEqual(first, {"lib/a.dart": True, "lib/b.dart": True})

        self._push(run2)
        second = {r.values["file"]: r.restore_if_missing for r in _FakeDestination.calls[-1]["rows"]}
        self.assertEqual(second, {"lib/a.dart": True, "lib/b.dart": False})

        pushes = self.repo.list_survey_pushes(run2, include_detail=True)
        self.assertEqual([p["status"] for p in pushes], ["succeeded", "succeeded"])
        self.assertEqual(pushes[0]["stats"], {"updated": 0, "inserted": 2, "skipped": 0})

    def test_only_latest_succeeded_run_is_pushable(self):
        from backend.survey.push import PushRejected

        run1 = self._complete_run([_finding("lib/a.dart")])
        self._complete_run([_finding("lib/a.dart")])
        with self.assertRaises(PushRejected):
            self._push(run1)

        failed = self._complete_run([], status="failed")
        with self.assertRaises(PushRejected):
            self._push(failed)

    def test_busy_binding_is_rejected_until_stale(self):
        """同一 Binding 并发推送会让两边都读到"键不存在"后各追加一行。"""
        from datetime import timedelta

        from backend.storage.db import session_scope
        from backend.storage.models import SurveyPush
        from backend.survey.push import begin_pushes

        run_uid = self._complete_run([_finding("lib/a.dart")])
        first = begin_pushes(run_uid, "manual")
        self.assertEqual(len(first["started"]), 1)
        second = begin_pushes(run_uid, "auto")
        self.assertEqual(second, {"started": [], "busy": [self.binding_id]})

        # 进程在推送中途被杀掉时记录会一直停在进行中，超过上限后不再阻塞
        with session_scope() as session:
            push = session.get(SurveyPush, first["started"][0])
            push.started_at = push.started_at - timedelta(seconds=self.repo.PUSH_STALE_SECONDS + 1)
        self.assertEqual(len(begin_pushes(run_uid, "manual")["started"]), 1)

    def test_push_failure_does_not_touch_run_status(self):
        run_uid = self._complete_run([_finding("lib/a.dart")])
        _FakeDestination.raise_error = "表格被删了"
        self._push(run_uid)

        push = self.repo.list_survey_pushes(run_uid, include_detail=True)[0]
        self.assertEqual(push["status"], "failed")
        self.assertEqual(push["error_message"], "表格被删了")
        self.assertEqual(self.repo.get_survey_run_detail(run_uid)["status"], "succeeded")
        self.assertEqual(self.repo.get_survey_run_detail(run_uid)["degradations"], [])

    def test_failed_push_does_not_count_as_first_push_done(self):
        """推送失败时不能刷新最近成功推送时刻，否则下一次成功推送会漏写本轮未发现的行。"""
        self._complete_run([_finding("lib/a.dart"), _finding("lib/b.dart")])
        run2 = self._complete_run([_finding("lib/a.dart")])
        _FakeDestination.raise_error = "限流"
        self._push(run2)
        _FakeDestination.raise_error = ""
        self._push(run2)
        rows = {r.values["file"]: r.restore_if_missing for r in _FakeDestination.calls[-1]["rows"]}
        self.assertTrue(rows["lib/b.dart"])

    def test_removed_destination_keeps_binding_and_fails_push(self):
        from backend.survey import destinations

        run_uid = self._complete_run([_finding("lib/a.dart")])
        with mock.patch.object(destinations, "load_file_config", return_value={}):
            self._push(run_uid)
        push = self.repo.list_survey_pushes(run_uid, include_detail=True)[0]
        self.assertEqual(push["status"], "failed")
        self.assertIn("输出目标未配置", push["error_message"])
        self.assertEqual(len(self.repo.get_survey(self.survey["survey_uid"])["bindings"]), 1)

    def test_removing_binding_keeps_push_records(self):
        """推送记录是留痕：绑定删掉后记录仍在，只是不能再从这条记录重推。"""
        run_uid = self._complete_run([_finding("lib/a.dart")])
        self._push(run_uid)
        self.repo.update_survey(self.survey["survey_uid"], {}, bindings=[])
        pushes = self.repo.list_survey_pushes(run_uid, include_detail=True)
        self.assertEqual(len(pushes), 1)
        self.assertIsNone(pushes[0]["binding_id"])
        self.assertEqual(pushes[0]["target"], {"table": "T"})

    def test_runner_publishes_after_success(self):
        """运行成功结束后自动更新台账并推送，推送记录的触发方式为自动。"""
        from backend.survey import runner

        run_uid = self.repo.start_survey_run(self.survey["survey_uid"], "schedule")
        self.repo.record_survey_repo(run_uid, "app", "https://g.com/a/app.git", status="ok")
        self.repo.record_survey_findings(run_uid, [_finding("lib/a.dart")])
        self.repo.finish_survey_run(run_uid, "succeeded")

        runner._publish(run_uid)
        pushes = self.repo.list_survey_pushes(run_uid, include_detail=True)
        self.assertEqual([(p["trigger"], p["status"]) for p in pushes], [("auto", "succeeded")])
        self.assertEqual(len(_FakeDestination.calls[-1]["rows"]), 1)


# ---------------------------------------------------------------------------
# 后台接口：Guest 边界与权限
# ---------------------------------------------------------------------------

class DestinationRouteTests(PushTestCase):

    def setUp(self):
        super().setUp()
        from flask import Flask

        from backend.admin import auth, routes

        self.app = Flask(__name__)
        self.app.secret_key = "test-only"
        self.app.register_blueprint(routes.admin_bp)
        self.client = self.app.test_client()
        patch = mock.patch.object(auth, "load_admin_config", return_value={"enabled": True, "bind_local_only": False})
        patch.start()
        self.addCleanup(patch.stop)
        self.run_uid = self._complete_run([_finding("lib/a.dart")])
        _FakeDestination.raise_error = "https://docs.google.com/spreadsheets/d/secret"
        self._push(self.run_uid)
        _FakeDestination.raise_error = ""

    def _admin(self):
        with self.client.session_transaction() as session:
            session["identity"] = "admin"

    def test_guest_sees_push_status_but_not_target_or_error(self):
        detail = self.client.get(f"/api/admin/survey-runs/{self.run_uid}").json
        self.assertEqual(detail["pushes"][0]["status"], "failed")
        self.assertNotIn("target", detail["pushes"][0])
        self.assertNotIn("error_message", detail["pushes"][0])
        self.assertNotIn("secret", json.dumps(detail))

        survey = self.client.get(f"/api/admin/surveys/{self.survey['survey_uid']}").json
        self.assertEqual(survey["bindings"][0]["destination"], "fake-1")
        self.assertNotIn("target", survey["bindings"][0])

        self._admin()
        detail = self.client.get(f"/api/admin/survey-runs/{self.run_uid}").json
        self.assertIn("secret", detail["pushes"][0]["error_message"])
        self.assertTrue(detail["pushable"])

    def test_manual_push_is_admin_only_even_with_guest_retry(self):
        self.repo.set_setting("guest_retry", "1")
        self.assertEqual(self.client.post(f"/api/admin/survey-runs/{self.run_uid}/push").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/destinations").status_code, 401)

        self._admin()
        with mock.patch("backend.admin.routes.execute_pushes_in_background") as background:
            response = self.client.post(f"/api/admin/survey-runs/{self.run_uid}/push", json={})
        self.assertEqual(response.status_code, 202)
        background.assert_called_once()

    def test_saving_binding_validates_destination(self):
        self._admin()
        uid = self.survey["survey_uid"]
        bad = self.client.patch(f"/api/admin/surveys/{uid}", json={
            "bindings": [{"destination": "nope", "target": {"table": "T"}}]
        })
        self.assertEqual(bad.status_code, 400)
        missing = self.client.patch(f"/api/admin/surveys/{uid}", json={
            "bindings": [{"destination": "fake-1", "target": {}}]
        })
        self.assertEqual(missing.status_code, 400)

        destinations = self.client.get("/api/admin/destinations").json
        self.assertEqual(destinations["items"][0]["name"], "fake-1")
        self.assertEqual(destinations["items"][0]["target_fields"][0]["key"], "table")

    def test_check_endpoint_returns_items_for_admin_only(self):
        from backend.survey.destinations.base import CHECK_OK, CheckItem

        url = "/api/admin/destinations/fake-1/check"
        self.assertEqual(self.client.post(url, json={"target": {"table": "T"}}).status_code, 401)

        self._admin()
        cls = _fake_destination_class()
        with mock.patch.object(cls, "check", return_value=[CheckItem("读取", CHECK_OK, "可以访问")]), \
                mock.patch.dict("backend.survey.destinations.DESTINATION_TYPES", {"fake": cls}):
            ok = self.client.post(url, json={"target": {"table": " T "}, "survey_name": "巡检"}).json
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["target"], {"table": "T"})
        self.assertEqual(ok["items"], [{"title": "读取", "level": "ok", "message": "可以访问"}])

        # 目标位置不合法、实例不存在：都是测试的正常结论，以检查项返回而不是 4xx
        invalid = self.client.post(url, json={"target": {}}).json
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["items"][0]["title"], "目标位置")
        missing = self.client.post("/api/admin/destinations/nope/check", json={"target": {}}).json
        self.assertFalse(missing["ok"])
        self.assertIn("输出目标未配置", missing["items"][0]["message"])

    def test_check_endpoint_survives_plugin_crash(self):
        self._admin()
        cls = _fake_destination_class()
        with mock.patch.object(cls, "check", side_effect=RuntimeError("boom")), \
                mock.patch.dict("backend.survey.destinations.DESTINATION_TYPES", {"fake": cls}):
            response = self.client.post("/api/admin/destinations/fake-1/check", json={"target": {"table": "T"}})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["ok"])
        self.assertIn("boom", response.json["items"][-1]["message"])

    def test_binding_to_removed_destination_can_be_kept(self):
        """实例从 config.yaml 删掉后，保存巡检的其它改动不能被这个 Binding 挡住。"""
        from backend.survey import destinations

        self._admin()
        uid = self.survey["survey_uid"]
        with mock.patch.object(destinations, "load_file_config", return_value={}):
            response = self.client.patch(f"/api/admin/surveys/{uid}", json={
                "name": "新名字",
                "bindings": [{"destination": "fake-1", "target": {"table": "T"}}],
            })
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["bindings"][0]["available"])


# ---------------------------------------------------------------------------
# Google Sheet 插件（假会话，不连真实接口）
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.content = json.dumps(self._payload).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self):
        return self._payload


class _FakeSession:
    """按顺序回放预设响应，并记录每个请求。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, params=None, json=None, timeout=None):
        from urllib.parse import unquote

        self.requests.append({"method": method, "url": unquote(url), "params": params, "json": json})
        return self.responses.pop(0)


def _sheet_meta(title="巡检", column_count=26, sheet_id=7):
    return _Response(payload={"sheets": [{"properties": {
        "sheetId": sheet_id, "title": title, "gridProperties": {"rowCount": 1000, "columnCount": column_count},
    }}]})


class GoogleSheetTests(unittest.TestCase):
    TARGET = {"spreadsheet": "x" * 30, "worksheet": "巡检"}

    def _destination(self, responses):
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        session = _FakeSession(responses)
        sleeps = []
        destination = GoogleSheetDestination(
            "sheet", {"credentials_file": "/nonexistent.json"}, session=session, sleep=sleeps.append
        )
        return destination, session, sleeps

    def _columns(self):
        from backend.survey.destinations.base import COLUMN_NUMBER, Column

        return [Column("key", "键"), Column("title", "标题"), Column("state", "状态"),
                Column("count", "同类条数", COLUMN_NUMBER)]

    def _row(self, key, restore=True, state="存在", title="t"):
        from backend.survey.destinations.base import LedgerRow

        return LedgerRow(key=key, values={"key": key, "title": title, "state": state, "count": 2},
                         restore_if_missing=restore)

    def test_normalize_target(self):
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        url = "https://docs.google.com/spreadsheets/d/1AbC_defGhijklmnopqrstuvwxyz0123/edit#gid=0"
        target = GoogleSheetDestination.normalize_target({"spreadsheet": url}, "移动端周巡检")
        self.assertEqual(target, {"spreadsheet": "1AbC_defGhijklmnopqrstuvwxyz0123", "worksheet": "移动端周巡检"})
        with self.assertRaises(ValueError):
            GoogleSheetDestination.normalize_target({"spreadsheet": "not an id"}, "x")
        with self.assertRaises(ValueError):
            GoogleSheetDestination.normalize_target({}, "x")

    def test_missing_credentials_file_is_a_config_error(self):
        from backend.survey.destinations import DestinationError
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        with self.assertRaises(DestinationError):
            GoogleSheetDestination.validate_options({})

    def test_column_letters_and_sheet_quoting(self):
        from backend.survey.destinations.google_sheet import column_letter, quote_sheet_title

        self.assertEqual([column_letter(i) for i in (0, 25, 26, 51, 52, 701, 702)],
                         ["A", "Z", "AA", "AZ", "BA", "ZZ", "AAA"])
        self.assertEqual(quote_sheet_title("Bob's 表"), "'Bob''s 表'")

    def test_new_worksheet_gets_headers_and_all_rows(self):
        destination, session, _ = self._destination([
            _Response(payload={"sheets": [{"properties": {"sheetId": 1, "title": "Sheet1",
                                                          "gridProperties": {"columnCount": 26}}}]}),
            _Response(payload={"replies": [{"addSheet": {"properties": {
                "sheetId": 9, "title": "巡检", "gridProperties": {"columnCount": 26}}}}]}),
            _Response(payload={"range": "'巡检'!A1:Z1"}),  # 空表头
            _Response(payload={}),  # 写表头
            _Response(payload={}),  # 追加
        ])
        stats = destination.push(self.TARGET, self._columns(),
                                 [self._row("s:1"), self._row("s:2", restore=False)])
        # 补不补回由核心层决定（新表的首次推送会把所有行都标成可补回），插件只照做
        self.assertEqual(stats.to_dict(), {"updated": 0, "inserted": 1, "skipped": 1})

        methods = [(r["method"], r["url"].rsplit("/", 1)[-1]) for r in session.requests]
        self.assertIn("addSheet", json.dumps(session.requests[1]["json"]))
        # 键列是新补出来的，不应再去读键列
        self.assertEqual(len(methods), 5)

        headers = session.requests[3]["json"]
        self.assertEqual(headers["valueInputOption"], "RAW")
        self.assertEqual([d["values"][0][0] for d in headers["data"]], ["键", "标题", "状态", "同类条数"])

        append = session.requests[4]
        self.assertTrue(append["url"].endswith("'巡检'!A1:append"))
        self.assertEqual(append["params"]["valueInputOption"], "RAW")
        self.assertEqual(append["json"]["values"][0], ["s:1", "t", "存在", 2])

    def test_existing_sheet_updates_only_system_columns(self):
        """
        用户调整过列序、插入了人工列：系统列按表头定位，
        写入按连续列段拆开，夹在中间的人工列一个字都不碰。
        """
        destination, session, _ = self._destination([
            _sheet_meta(),
            # A=键 B=负责人(人工) C=标题 D=状态；缺少"同类条数"
            _Response(payload={"values": [["键", "负责人", "标题", "状态"]]}),
            _Response(payload={}),  # 补表头
            _Response(payload={"values": [["s:1"], [], ["other:9"], ["s:1"], ["s:gone"]]}),
            _Response(payload={}),  # 批量更新
            _Response(payload={}),  # 追加
        ])
        rows = [
            self._row("s:1", state="本轮未发现"),
            self._row("s:new"),
            self._row("s:deleted-by-user", restore=False, state="本轮未发现"),
        ]
        stats = destination.push(self.TARGET, self._columns(), rows)
        self.assertEqual(stats.to_dict(), {"updated": 1, "inserted": 1, "skipped": 1})

        header_write = session.requests[2]["json"]["data"]
        self.assertEqual(header_write, [{"range": "'巡检'!E1", "values": [["同类条数"]]}])

        read_keys = session.requests[3]
        self.assertTrue(read_keys["url"].endswith("'巡检'!A2:A"))

        updates = session.requests[4]["json"]
        self.assertEqual(updates["valueInputOption"], "RAW")
        ranges = {d["range"]: d["values"][0] for d in updates["data"]}
        # 同一个键出现在第 2 行与第 5 行，两行都更新；B 列（负责人）被跳过
        self.assertEqual(ranges, {
            "'巡检'!A2:A2": ["s:1"], "'巡检'!C2:E2": ["t", "本轮未发现", 2],
            "'巡检'!A5:A5": ["s:1"], "'巡检'!C5:E5": ["t", "本轮未发现", 2],
        })

        appended = session.requests[5]["json"]["values"]
        # 人工列位置是 null，追加接口会跳过它而不是写空串
        self.assertEqual(appended, [["s:new", None, "t", "存在", 2]])

    def test_grid_is_widened_before_writing_new_headers(self):
        destination, session, _ = self._destination([
            _sheet_meta(column_count=2, sheet_id=42),
            _Response(payload={"values": [["键", "标题"]]}),
            _Response(payload={}),  # appendDimension
            _Response(payload={}),  # 补表头
            _Response(payload={"values": []}),
        ])
        destination.push(self.TARGET, self._columns(), [])
        dimension = session.requests[2]["json"]["requests"][0]["appendDimension"]
        self.assertEqual(dimension, {"sheetId": 42, "dimension": "COLUMNS", "length": 2})

    def test_table_object_is_appended_with_append_cells(self):
        """
        用户把区域转换成了表格对象：values.append 认不出它，会把新行插到工作表顶上把表头挤下去。
        这里表格对象从第 5 行开始（就是被挤下去之后的样子），表头、键列都要跟着它走，追加改用 appendCells。
        """
        table = {"tableId": "t1", "range": {"sheetId": 7, "startRowIndex": 4, "endRowIndex": 9,
                                            "startColumnIndex": 0, "endColumnIndex": 4}}
        destination, session, _ = self._destination([
            _Response(payload={"sheets": [{
                "properties": {"sheetId": 7, "title": "巡检", "gridProperties": {"columnCount": 26}},
                "tables": [table],
            }]}),
            # A=键 B=处理人(人工) C=标题 D=状态；缺少"同类条数"
            _Response(payload={"values": [["键", "处理人", "标题", "状态"]]}),
            _Response(payload={}),  # 补表头
            _Response(payload={}),  # updateTable 扩到 E 列
            _Response(payload={"values": [["s:1"]]}),
            _Response(payload={}),  # 批量更新
            _Response(payload={}),  # appendCells
        ])
        stats = destination.push(self.TARGET, self._columns(), [self._row("s:1"), self._row("s:new", title="=1+1")])
        self.assertEqual(stats.to_dict(), {"updated": 1, "inserted": 1, "skipped": 0})

        self.assertTrue(session.requests[1]["url"].endswith("'巡检'!5:5"))
        self.assertEqual(session.requests[2]["json"]["data"], [{"range": "'巡检'!E5", "values": [["同类条数"]]}])

        update_table = session.requests[3]["json"]["requests"][0]["updateTable"]
        self.assertEqual(update_table["fields"], "range")
        self.assertEqual(update_table["table"]["range"]["endColumnIndex"], 5)

        self.assertTrue(session.requests[4]["url"].endswith("'巡检'!A6:A"))
        ranges = {d["range"] for d in session.requests[5]["json"]["data"]}
        self.assertEqual(ranges, {"'巡检'!A6:A6", "'巡检'!C6:E6"})

        append = session.requests[6]
        self.assertTrue(append["url"].endswith(":batchUpdate"))
        request = append["json"]["requests"][0]["appendCells"]
        self.assertEqual(request["tableId"], "t1")
        # 省略 sheetId 时接口按 0 校验，非 gid=0 的工作表会被拒绝
        self.assertEqual(request["sheetId"], 7)
        self.assertEqual(request["fields"], "userEnteredValue")
        # 人工列写空 CellData；以 "=" 开头的标题写成 stringValue，不会被当成公式
        self.assertEqual(request["rows"], [{"values": [
            {"userEnteredValue": {"stringValue": "s:new"}},
            {},
            {"userEnteredValue": {"stringValue": "=1+1"}},
            {"userEnteredValue": {"stringValue": "存在"}},
            {"userEnteredValue": {"numberValue": 2}},
        ]}])

    def test_rate_limit_is_retried_with_backoff(self):
        destination, session, sleeps = self._destination([
            _Response(status=429, payload={"error": {"message": "quota"}}, headers={"Retry-After": "3"}),
            _Response(status=503, payload={}),
            _sheet_meta(),
            _Response(payload={"values": [["键", "标题", "状态", "同类条数"]]}),
            _Response(payload={"values": []}),
        ])
        destination.push(self.TARGET, self._columns(), [])
        self.assertEqual(sleeps, [3, 2])

    def test_permission_error_names_the_service_account(self):
        from backend.survey.destinations import DestinationError

        destination, _, _ = self._destination([
            _Response(status=403, payload={"error": {"message": "The caller does not have permission"}}),
        ])
        destination._transport.client_email = "opencr@proj.iam.gserviceaccount.com"
        with self.assertRaises(DestinationError) as ctx:
            destination.push(self.TARGET, self._columns(), [])
        self.assertIn("opencr@proj.iam.gserviceaccount.com", str(ctx.exception))
        self.assertIn("编辑者", str(ctx.exception))

    def _check_meta(self, sheets=("巡检",)):
        return _Response(payload={
            "properties": {"title": "质量台账"},
            "sheets": [{"properties": {"sheetId": i, "title": t}} for i, t in enumerate(sheets)],
        })

    def test_check_passes_without_writing_ledger_rows(self):
        """测试不写任何台账行：写权限用"把标题改成它自己"验证，内容与标题都不变。"""
        from backend.survey.destinations.base import CHECK_OK

        destination, session, _ = self._destination([
            self._check_meta(),
            _Response(payload={"values": [["键", "负责人", "标题"]]}),
            _Response(payload={}),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual([i.level for i in items], [CHECK_OK] * 4)
        self.assertIn("2/4", items[2].message)
        self.assertIn("状态、同类条数", items[2].message)

        write = session.requests[-1]
        self.assertTrue(write["url"].endswith(":batchUpdate"))
        self.assertEqual(write["json"]["requests"], [{"updateSpreadsheetProperties": {
            "properties": {"title": "质量台账"}, "fields": "title",
        }}])
        self.assertFalse(any("/values:" in r["url"] or ":append" in r["url"] for r in session.requests))

    def test_check_reads_headers_from_table_object(self):
        """表格对象不在第 1 行时，测试连通性也要读它的首行，否则会误报"没有任何系统列"。"""
        destination, session, _ = self._destination([
            _Response(payload={"properties": {"title": "质量台账"}, "sheets": [{
                "properties": {"sheetId": 0, "title": "巡检"},
                "tables": [{"tableId": "t1", "range": {"startRowIndex": 4, "endColumnIndex": 4}}],
            }]}),
            _Response(payload={"values": [["键", "标题", "状态", "同类条数"]]}),
            _Response(payload={}),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertTrue(session.requests[1]["url"].endswith("'巡检'!5:5"))
        self.assertIn("系统列齐全", items[2].message)

    def test_check_reports_missing_worksheet_as_warning(self):
        from backend.survey.destinations.base import CHECK_WARN

        destination, session, _ = self._destination([self._check_meta(sheets=("Sheet1",)), _Response(payload={})])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual(items[2].level, CHECK_WARN)
        self.assertIn("自动创建", items[2].message)
        # 测试不创建工作表
        self.assertNotIn("addSheet", json.dumps([r["json"] for r in session.requests]))

    def test_check_stops_at_first_fatal_problem(self):
        from backend.survey.destinations.base import CHECK_ERROR

        destination, session, sleeps = self._destination([
            _Response(status=404, payload={"error": {"message": "Requested entity was not found."}}),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual([i.title for i in items], ["服务账号凭据", "读取表格"])
        self.assertEqual(items[-1].level, CHECK_ERROR)
        self.assertEqual(len(session.requests), 1)

    def test_rejected_credentials_are_not_reported_as_network_failure(self):
        """密钥能解析不代表凭据有效；被 Google 拒绝时不重试，也不能说成"无法连接"。"""
        from backend.survey.destinations.base import CHECK_ERROR

        class RefreshError(Exception):
            pass

        destination, session, sleeps = self._destination([])
        session.request = mock.Mock(side_effect=RefreshError("invalid_grant: account not found"))
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual([(i.title, i.level) for i in items], [("服务账号凭据", CHECK_ERROR)])
        self.assertIn("invalid_grant", items[0].message)
        self.assertNotIn("无法连接", items[0].message)
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_check_reports_read_only_access(self):
        from backend.survey.destinations.base import CHECK_ERROR

        destination, _, _ = self._destination([
            self._check_meta(),
            _Response(payload={"values": [["键", "标题", "状态", "同类条数"]]}),
            _Response(status=403, payload={"error": {"message": "The caller does not have permission"}}),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual((items[-1].title, items[-1].level), ("写入权限", CHECK_ERROR))
        self.assertIn("编辑者", items[-1].message)

    def test_check_does_not_wait_out_long_backoff(self):
        """测试是用户在表单里同步等结果，不能套用推送的五次退避。"""
        destination, session, sleeps = self._destination([
            _Response(status=429, payload={}),
            _Response(status=429, payload={"error": {"message": "quota"}}),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertIn("429", items[-1].message)

    def test_missing_credentials_message_points_at_likely_causes(self):
        """"路径明明是对的"几乎总是行内注释被读进了值里，或者服务跑在容器里。"""
        from backend.survey.destinations import DestinationError
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        raw = '"/Users/lxf/path/to/accout.json"  # 服务账号'
        destination = GoogleSheetDestination("sheet", {"credentials_file": raw})
        with mock.patch.dict(sys.modules, {"google.auth.transport.requests": mock.Mock(),
                                           "google.oauth2": mock.Mock()}):
            with self.assertRaises(DestinationError) as ctx:
                destination._transport._get_session()
        message = str(ctx.exception)
        self.assertIn(repr(raw), message)
        self.assertIn("行内注释", message)

    def test_long_cells_are_truncated_under_the_hard_limit(self):
        """超过五万字符会让整批写入失败，而不是只截断那一格。"""
        from backend.survey.destinations.base import COLUMN_LONG_TEXT, Column
        from backend.survey.destinations.google_sheet import CELL_CHAR_LIMIT, _cell_value

        value = _cell_value(Column("body", "正文", COLUMN_LONG_TEXT), "x" * (CELL_CHAR_LIMIT + 10))
        self.assertEqual(len(value), CELL_CHAR_LIMIT)
        self.assertIn("已截断", value)
        self.assertEqual(_cell_value(Column("t", "t"), None), "")

    def test_large_updates_are_split_into_batches(self):
        from backend.survey.destinations.google_sheet import _chunks_by_size

        items = [{"range": f"A{i}", "values": [["x" * 100]]} for i in range(50)]
        batches = _chunks_by_size(items, max_bytes=1000)
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(len(b) for b in batches), 50)


# ---------------------------------------------------------------------------
# Google Sheet 插件：gogcli 鉴权方式（假子进程，不依赖真实安装的 gogcli）
# ---------------------------------------------------------------------------

class _FakeGogcli:
    """按顺序回放预设结果；记录每次调用的参数、环境与请求体文件内容。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, args, capture_output=None, text=None, timeout=None, env=None):
        import subprocess

        body = None
        if "--body" in args:
            path = args[args.index("--body") + 1].lstrip("@")
            body = json.loads(Path(path).read_text(encoding="utf-8"))
            self.body_paths = getattr(self, "body_paths", []) + [path]
        self.calls.append({"args": args, "env": env, "timeout": timeout, "body": body})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        code, stdout, stderr = result
        return subprocess.CompletedProcess(args, code, stdout, stderr)


def _ok(payload=None):
    return (0, json.dumps(payload if payload is not None else {}), "")


class GogcliTests(unittest.TestCase):
    TARGET = {"spreadsheet": "x" * 30, "worksheet": "巡检"}
    ACCOUNT = "someone@company.com"

    def _destination(self, results, options=None):
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        runner = _FakeGogcli(results)
        sleeps = []
        destination = GoogleSheetDestination(
            "sheet", {"auth": "gogcli", "account": self.ACCOUNT, "gogcli_bin": "/opt/bin/gog", **(options or {})},
            runner=runner, sleep=sleeps.append,
        )
        return destination, runner, sleeps

    def _columns(self):
        from backend.survey.destinations.base import Column

        return [Column("key", "键"), Column("title", "标题")]

    def _row(self, key):
        from backend.survey.destinations.base import LedgerRow

        return LedgerRow(key=key, values={"key": key, "title": "=IMPORTXML(x)"}, restore_if_missing=True)

    def test_options_require_explicit_account(self):
        from backend.survey.destinations import DestinationError
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        GoogleSheetDestination.validate_options({"credentials_file": "/k.json"})  # 缺省仍是服务账号
        for options in ({"auth": "gogcli"}, {"auth": "gogcli", "account": "auto"}, {"auth": "oauth"}):
            with self.subTest(options=options), self.assertRaises(DestinationError):
                GoogleSheetDestination.validate_options(options)

    def test_dropdown_summary_names_auth_mode_without_secrets(self):
        from backend.survey.destinations.google_sheet import GoogleSheetDestination

        self.assertEqual(GoogleSheetDestination.describe_options({"credentials_file": "/secret/k.json"}), "服务账号")
        self.assertEqual(
            GoogleSheetDestination.describe_options({"auth": "gogcli", "account": self.ACCOUNT}),
            f"gogcli · {self.ACCOUNT}",
        )

    def test_push_goes_through_api_call_with_body_file(self):
        """
        全部走 `gog api call`：写操作带 --allow-write --force，读操作不带；
        请求体经临时文件传入而不出现在参数里，调用结束后文件被删除；写入仍是 RAW。
        """
        destination, runner, _ = self._destination([
            _ok({"sheets": [{"properties": {"sheetId": 3, "title": "巡检", "gridProperties": {"columnCount": 26}}}]}),
            _ok({"values": [["键", "标题"]]}),
            _ok({"values": [["other:1"]]}),
            _ok({}),
        ])
        stats = destination.push(self.TARGET, self._columns(), [self._row("s:new")])
        self.assertEqual(stats.inserted, 1)

        methods = [c["args"][5] for c in runner.calls]
        self.assertEqual(methods, [
            "spreadsheets.get", "spreadsheets.values.get", "spreadsheets.values.get", "spreadsheets.values.append",
        ])
        for call in runner.calls:
            args = call["args"]
            self.assertEqual(args[:5], ["/opt/bin/gog", "api", "call", "sheets", "v4"])
            self.assertEqual(args[args.index("--account") + 1], self.ACCOUNT)
            self.assertEqual(args[args.index("--scope") + 1], "https://www.googleapis.com/auth/spreadsheets")
            self.assertIn("--no-input", args)
            self.assertEqual(call["env"]["GOG_KEYRING_LOCK_TIMEOUT"], "30s")
        self.assertNotIn("--allow-write", runner.calls[0]["args"])

        append = runner.calls[-1]
        self.assertIn("--allow-write", append["args"])
        self.assertIn("--force", append["args"])
        params = json.loads(append["args"][append["args"].index("--params") + 1])
        self.assertEqual(params["valueInputOption"], "RAW")
        self.assertEqual(params["range"], "'巡检'!A1")
        self.assertEqual(append["body"]["values"], [["s:new", "=IMPORTXML(x)"]])
        # 正文不出现在命令行参数里
        self.assertFalse(any("IMPORTXML" in a for a in append["args"]))
        self.assertTrue(all(not Path(p).exists() for p in runner.body_paths))

    def test_exit_codes_become_actionable_errors(self):
        from backend.survey.destinations import DestinationError
        from backend.survey.destinations.google_transport import AuthRejected

        cases = [
            ((6, "", "Google API error (403 forbidden): nope"), DestinationError, ["编辑者", self.ACCOUNT]),
            ((5, "", "Google API error (404)"), DestinationError, ["表格不存在"]),
            ((4, "", "refresh token expired or revoked"), AuthRejected, ["gog auth add " + self.ACCOUNT, "Testing"]),
            ((1, "", "macOS Keychain may be waiting for a permission prompt"), AuthRejected, ["始终允许"]),
            ((1, "", "no TTY available for keyring file backend password prompt"), AuthRejected, ["GOG_KEYRING_PASSWORD"]),
            ((2, "", "unknown flag"), DestinationError, ["版本不兼容"]),
            ((10, "", "OAuth client credentials missing (OAuth client ID JSON)."), AuthRejected, ["gog auth credentials set"]),
        ]
        for result, error, phrases in cases:
            with self.subTest(result=result):
                destination, _, _ = self._destination([result])
                with self.assertRaises(error) as ctx:
                    destination.push(self.TARGET, self._columns(), [])
                for phrase in phrases:
                    self.assertIn(phrase, str(ctx.exception))

    def test_missing_binary_points_at_absolute_path(self):
        from backend.survey.destinations import DestinationError

        destination, _, _ = self._destination([FileNotFoundError("gog")])
        with self.assertRaises(DestinationError) as ctx:
            destination.push(self.TARGET, self._columns(), [])
        self.assertIn("gogcli_bin", str(ctx.exception))

    def test_rate_limit_retried_after_gogcli_gives_up_but_not_in_check(self):
        """gogcli 自己已经重试过；推送时再补两次长间隔重试，测试时一次都不补。"""
        destination, runner, sleeps = self._destination([
            (7, "", "rate limited"), (8, "", "timeout"),
            _ok({"sheets": [{"properties": {"sheetId": 1, "title": "巡检", "gridProperties": {"columnCount": 26}}}]}),
            _ok({"values": [["键", "标题"]]}),
            _ok({"values": []}),
        ])
        destination.push(self.TARGET, self._columns(), [])
        self.assertEqual(sleeps, [15, 45])

        destination, runner, sleeps = self._destination([
            _ok(), _ok({"accounts": [{"email": self.ACCOUNT, "services": ["sheets"]}]}),
            (7, "", "rate limited"),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual(sleeps, [])
        self.assertEqual(items[-1].title, "读取表格")
        self.assertIn("限流", items[-1].message)

    def test_check_verifies_binary_and_account_first(self):
        from backend.survey.destinations.base import CHECK_ERROR, CHECK_OK

        missing, _, _ = self._destination([FileNotFoundError("gog")])
        items = missing.check(self.TARGET, self._columns())
        self.assertEqual([(i.title, i.level) for i in items], [("gogcli 可执行文件", CHECK_ERROR)])

        other, _, _ = self._destination([(0, "v0.40.0", ""), _ok({"accounts": [{"email": "a@b.com"}]})])
        items = other.check(self.TARGET, self._columns())
        self.assertEqual(items[-1].level, CHECK_ERROR)
        self.assertIn("a@b.com", items[-1].message)
        self.assertIn("gog auth add " + self.ACCOUNT, items[-1].message)

        no_sheets, _, _ = self._destination([
            (0, "v0.40.0", ""), _ok({"accounts": [{"email": self.ACCOUNT.upper(), "services": ["gmail"]}]}),
        ])
        items = no_sheets.check(self.TARGET, self._columns())
        self.assertEqual(items[-1].level, CHECK_ERROR)
        self.assertIn("没有包含 Sheets", items[-1].message)

        ok, runner, _ = self._destination([
            (0, "v0.40.0", ""),
            _ok({"accounts": [{"email": self.ACCOUNT, "services": ["sheets", "drive"]}]}),
            _ok({"properties": {"title": "质量台账"}, "sheets": [{"properties": {"title": "巡检"}}]}),
            _ok({"values": [["键", "标题"]]}),
            _ok({}),
        ])
        items = ok.check(self.TARGET, self._columns())
        self.assertEqual([i.level for i in items], [CHECK_OK] * 5)
        self.assertEqual(items[0].title, "gogcli 可执行文件")
        self.assertIn("v0.40.0", items[0].message)
        write = runner.calls[-1]
        self.assertEqual(write["args"][5], "spreadsheets.batchUpdate")
        self.assertIn("updateSpreadsheetProperties", json.dumps(write["body"]))

    def test_expired_authorization_is_reported_under_account_item(self):
        """账号在 gogcli 里，但令牌已失效：第一次真实请求才会暴露，结论要归到账号授权那一项。"""
        from backend.survey.destinations.base import CHECK_ERROR

        destination, _, _ = self._destination([
            (0, "v0.40.0", ""),
            _ok({"accounts": [{"email": self.ACCOUNT, "services": ["sheets"]}]}),
            (4, "", "refresh token expired or revoked"),
        ])
        items = destination.check(self.TARGET, self._columns())
        self.assertEqual([(i.title, i.level) for i in items][-1], ("gogcli 账号授权", CHECK_ERROR))
        self.assertEqual(len(items), 2)



# ---------------------------------------------------------------------------
# 部署脚本用的 gogcli 授权检查（install.sh / docker-entrypoint.sh / setup-gogcli.sh）
# ---------------------------------------------------------------------------

class GogcliStatusTests(unittest.TestCase):
    ACCOUNT = "someone@company.com"

    def _transport(self, results):
        from backend.survey.destinations.google_transport import GogcliTransport

        return GogcliTransport(self.ACCOUNT, "/opt/bin/gog", runner=_FakeGogcli(results))

    def test_only_fixable_problems_ask_for_auth_add(self):
        """
        只有"账号不在 / 没授权 Sheets"才引向 `gog auth add`；
        钥匙串被拒、令牌读取失败重新授权也解决不了，部署脚本不该让人白走一遍浏览器授权。
        """
        cases = [
            (_ok({"accounts": [{"email": "a@b.com"}]}), True),
            (_ok({"accounts": [{"email": self.ACCOUNT, "services": ["gmail"]}]}), True),
            (_ok({"accounts": [{"email": self.ACCOUNT, "services": ["sheets"]}]}), False),
            (_ok({"accounts": [{"email": self.ACCOUNT, "error": "keychain locked"}]}), False),
            ((1, "", "Keychain access denied"), False),
        ]
        for listed, expected in cases:
            with self.subTest(listed=listed):
                self.assertEqual(self._transport([listed]).check_account()[1], expected)

    def _run(self, destinations, results, argv):
        """以假的配置与假子进程跑一次命令行入口，返回 (退出码, 输出, 子进程调用次数)。"""
        import contextlib
        import io

        from backend.survey.destinations import gogcli_status
        from backend.survey.destinations.google_transport import GogcliTransport

        runner = _FakeGogcli(results)

        def transport(options):
            return GogcliTransport(options.get("account", ""), options.get("gogcli_bin") or "gog", runner=runner)

        out = io.StringIO()
        with mock.patch.object(gogcli_status, "load_destinations", return_value=destinations), \
                mock.patch.object(gogcli_status, "_transport", side_effect=transport), \
                contextlib.redirect_stdout(out):
            code = gogcli_status.main(argv)
        return code, out.getvalue(), len(runner.calls)

    def _info(self, name, options, type_name="google_sheet", error=""):
        from backend.survey.destinations.base import DestinationInfo

        return DestinationInfo(name=name, type_name=type_name, options=options, error=error)

    def test_needs_auth_lists_each_account_once_and_ignores_other_instances(self):
        destinations = {
            "a": self._info("a", {"auth": "gogcli", "account": self.ACCOUNT}),
            "b": self._info("b", {"auth": "gogcli", "account": self.ACCOUNT}),
            "sa": self._info("sa", {"credentials_file": "/k.json"}),
            "broken": self._info("broken", {"auth": "gogcli"}, error="缺少 account"),
        }
        code, out, calls = self._run(destinations, [_ok({"accounts": []})], ["--needs-auth"])
        self.assertEqual((code, out, calls), (0, f"{self.ACCOUNT}\n", 1))

    def test_no_gogcli_instance_prints_nothing(self):
        """install.sh 靠空输出判断"没有 gogcli 实例"并跳过整步；输出一个空行会被当成空账号。"""
        destinations = {"sa": self._info("sa", {"credentials_file": "/k.json"})}
        for argv in (["--accounts"], ["--needs-auth"], []):
            with self.subTest(argv=argv):
                self.assertEqual(self._run(destinations, [], argv), (0, "", 0))

    def test_report_fails_on_any_problem_and_checks_shared_account_once(self):
        destinations = {
            "a": self._info("a", {"auth": "gogcli", "account": self.ACCOUNT}),
            "b": self._info("b", {"auth": "gogcli", "account": self.ACCOUNT}),
            "broken": self._info("broken", {"auth": "gogcli"}, error="缺少 account"),
        }
        listed = _ok({"accounts": [{"email": self.ACCOUNT, "services": ["sheets"]}]})
        code, out, calls = self._run(destinations, [(0, "v0.40.0", ""), listed], [])
        self.assertEqual(code, 1)
        self.assertEqual(calls, 2)
        self.assertIn("✗ 配置有误：缺少 account", out)
        self.assertEqual(out.count("✓ gogcli 账号授权"), 2)

        del destinations["broken"]
        code, _, _ = self._run(destinations, [(0, "v0.40.0", ""), listed], [])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
