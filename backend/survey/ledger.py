#!/usr/bin/env python3
"""
Ledger（问题台账）：状态判定与镜像行的生成。

Ledger 由系统持有、每个成功的 SurveyRun 结束后更新一次；Destination 上的表格只是它的镜像。
为什么库里和表里各存一份，见 docs/adr/0004-survey-ledger-mirrored-to-destinations.md。

判定逻辑全部是纯函数（plan_ledger_update / plan_rechecks / aggregate_findings / build_ledger_rows），
不碰数据库也不碰网络，便于单测；update_ledger_for_run 只负责把它们和 repo 串起来。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..storage import repo
from ..storage.models import (
    LEDGER_IGNORED,
    LEDGER_PRESENT,
    LEDGER_UNSEEN,
    RUN_SUCCEEDED,
    SEVERITY_ADVICE,
    SEVERITY_CRITICAL,
    SEVERITY_UNKNOWN,
    SEVERITY_WARNING,
    utcnow,
)
from .destinations.base import (
    COLUMN_DATETIME,
    COLUMN_LONG_TEXT,
    COLUMN_NUMBER,
    COLUMN_SINGLE_SELECT,
    COLUMN_TEXT,
    COLUMN_URL,
    Column,
    LedgerRow,
)
from .report import CATEGORY_LABELS, SEVERITY_LABELS
from .schedule import resolve_timezone

logger = logging.getLogger(__name__)

LEDGER_STATE_LABELS: Dict[str, str] = {
    LEDGER_PRESENT: "存在",
    LEDGER_UNSEEN: "本轮未发现",
    LEDGER_IGNORED: "已忽略",
}

_SEVERITY_RANK = {SEVERITY_CRITICAL: 3, SEVERITY_WARNING: 2, SEVERITY_ADVICE: 1, SEVERITY_UNKNOWN: 0}

# 系统列。**顺序只决定新表的初始列序**，已有的表按表头名称定位，用户可以随意调整。
# 表头文案一旦发布就不要改：改了之后，已有表格里的旧列会被当成人工列，推送时再补一个新列。
KEY_COLUMN = Column("key", "键")
LEDGER_COLUMNS = (
    KEY_COLUMN,
    Column("survey", "巡检"),
    Column("repo", "仓库"),
    Column("file", "文件"),
    Column("lines", "行号"),
    Column("category", "问题类别", COLUMN_SINGLE_SELECT, tuple(CATEGORY_LABELS.values())),
    Column("severity", "严重度", COLUMN_SINGLE_SELECT, tuple(SEVERITY_LABELS.values())),
    Column("title", "标题"),
    Column("body", "正文", COLUMN_LONG_TEXT),
    Column("state", "状态", COLUMN_SINGLE_SELECT, tuple(LEDGER_STATE_LABELS.values())),
    Column("first_seen", "首次发现", COLUMN_DATETIME),
    Column("last_seen", "最近发现", COLUMN_DATETIME),
    Column("count", "同类条数", COLUMN_NUMBER),
)
LINK_COLUMN = Column("link", "运行详情", COLUMN_URL)


def ledger_key(survey_slug: str, fingerprint: str) -> str:
    """
    镜像里的行键。

    用 slug 而不是 survey_uid 或名称：名称会改，改名后整张台账会被当成全新的行；
    uid 对人不可读，而多个 Survey 共用一张表时，人需要一眼看出这行属于谁。slug 永不变化。
    """
    return f"{survey_slug}:{fingerprint}"


def aggregate_findings(findings: Iterable[dict]) -> Dict[str, dict]:
    """
    把一轮产出按指纹聚合，返回 {指纹: 聚合结果}。

    指纹是 `仓库 + 文件路径 + 类别`，同一文件同一类别下不同行号的多条 Finding 共享指纹。
    合并规则：严重度取最高，标题取最严重那条，行号全列，正文按条拼接并带上行号小标题。
    """
    groups: Dict[str, List[dict]] = {}
    for item in findings:
        groups.setdefault(item["fingerprint"], []).append(item)

    result: Dict[str, dict] = {}
    for fingerprint, items in groups.items():
        items = sorted(
            items,
            key=lambda f: (-_SEVERITY_RANK.get(f.get("severity") or SEVERITY_UNKNOWN, 0), int(f.get("line") or 0)),
        )
        top = items[0]
        lines = sorted({int(f.get("line") or 0) for f in items if int(f.get("line") or 0) > 0})
        if len(items) == 1:
            body = (top.get("body") or "").strip()
        else:
            blocks = []
            for f in items:
                line = int(f.get("line") or 0)
                heading = f"[第 {line} 行] " if line > 0 else "[未定位到行] "
                blocks.append(f"{heading}{(f.get('title') or '').strip()}\n{(f.get('body') or '').strip()}".strip())
            body = "\n\n".join(blocks)
        result[fingerprint] = {
            "fingerprint": fingerprint,
            "repo_slug": top.get("repo_slug") or "",
            "file_path": top.get("file_path") or "",
            "category": top.get("category") or "",
            "severity": top.get("severity") or SEVERITY_UNKNOWN,
            "title": (top.get("title") or "").strip(),
            "body": body,
            "lines": lines,
            "finding_count": len(items),
        }
    return result


def plan_ledger_update(
    existing: Dict[str, dict],
    aggregated: Dict[str, dict],
    inspected: Set[Tuple[str, str]],
    ignored: set,
    first_seen_lookup: Dict[str, datetime],
    run_uid: str,
    now: datetime,
    attempted: Optional[Set[Tuple[str, str]]] = None,
) -> List[dict]:
    """
    算出本轮要写入 Ledger 的变更，返回待 upsert 的条目列表（只含发生变化的字段）。

    inspected 是本轮得出可信结论的文件集合 {(repo_slug, file_path)}；attempted 是交给过 L2 的文件
    （含没得出结论的），落在其中的行刷新 last_checked_at，供复核轮转。

    - 本轮出现：状态置为存在，刷新聚合内容与最近发现；新指纹的首次发现取库里仍保留的最早记录。
    - 本轮没出现、但出现过被已忽略问题隐藏的发现（ignored，见 get_survey_run_ledger_inputs）：已忽略。
    - 其余没出现的：所在文件本轮被取证过时标为本轮未发现，否则保持原状态不动 ——
      包括"已忽略后又取消忽略"的行，它会停在已忽略，直到某一轮取证过它的文件（Q25 的推导，不做特殊处理）。
    """
    attempted = set(attempted or ()) | set(inspected)
    changes: List[dict] = []

    for fingerprint, item in aggregated.items():
        current = existing.get(fingerprint)
        first_seen = (
            current["first_seen_at"] if current
            else first_seen_lookup.get(fingerprint) or now
        )
        changes.append({
            **item,
            "lines": json.dumps(item["lines"]),
            "state": LEDGER_PRESENT,
            "first_seen_at": first_seen,
            "last_seen_at": now,
            "last_run_uid": run_uid,
            "last_checked_at": now,
        })

    for fingerprint, current in existing.items():
        if fingerprint in aggregated:
            continue
        file_key = (current.get("repo_slug") or "", current.get("file_path") or "")
        change: dict = {}
        if fingerprint in ignored:
            target = LEDGER_IGNORED
        elif file_key in inspected:
            # 按文件而不是按仓库判定：仓库拉取成功只说明文件"可以被看"，
            # L1 每轮点名的文件都不一样，没被点名的文件本来就不可能出现
            target = LEDGER_UNSEEN
        else:
            target = None
        if target is not None and current.get("state") != target:
            change["state"] = target
        if file_key in attempted:
            change["last_checked_at"] = now
        if change:
            changes.append({"fingerprint": fingerprint, **change})

    return changes


def plan_rechecks(
    entries: Iterable[dict], max_files: int, ignored: Optional[set] = None
) -> Tuple[List[dict], Dict[Tuple[str, str], dict]]:
    """
    从 Ledger 里挑出本轮要复核的文件。

    返回 (复核清单, 全部待复核文件的索引)：
    - 复核清单：最多 max_files 个文件，每项 {"repo_slug", "file_path", "previous", "anchor_lines"}；
    - 索引：{(repo_slug, file_path): 同形态的项}，覆盖所有仍为"存在"的文件。L1 恰好点名了
      没进复核清单的文件时，靠它把上一轮的问题一并交给 L2，否则那一轮取证会在模型不知情的
      情况下把这些问题判成本轮未发现。

    只复核状态为"存在"的行，以及取消了忽略、状态还停在已忽略的行（ignored 是仍有已忽略问题的指纹）：
    后者一旦所在文件被取证就会被判定，不交给 L2 复核的话，就是在模型没看它的情况下判它本轮未发现。
    本轮未发现的行已经被取证过一次，再反复复核只是在烧预算；仍在忽略清单里的行用户明确说过不想再看。
    排序按上次复核时间（last_checked_at）从早到晚：交给过 L2 的文件不论有没有结论都会刷新、排到队尾，
    超出名额的文件下一轮自然轮到。
    """
    ignored = ignored or set()
    by_file: Dict[Tuple[str, str], dict] = {}
    for entry in entries:
        state = entry.get("state")
        unignored = state == LEDGER_IGNORED and entry.get("fingerprint") not in ignored
        if (state != LEDGER_PRESENT and not unignored) or not entry.get("file_path"):
            continue
        # 沿用台账里的路径原文，不做 normalize_file_path：复核产出的指纹要和原来那一行对上，
        # 规范化之后旧行会被判成本轮未发现、同时冒出一行新的
        key = (entry.get("repo_slug") or "", entry["file_path"])
        target = by_file.setdefault(key, {
            "repo_slug": key[0], "file_path": key[1], "previous": [], "anchor_lines": [],
            "_checked": [], "_seen": [],
        })
        lines = _positive_lines(entry.get("lines"))
        target["previous"].append({
            "category": entry.get("category") or "",
            "severity": entry.get("severity") or SEVERITY_UNKNOWN,
            "title": entry.get("title") or "",
            "lines": lines,
        })
        target["anchor_lines"] = sorted(set(target["anchor_lines"]) | set(lines))
        target["_checked"].append(entry.get("last_checked_at") or datetime.min)
        target["_seen"].append(entry.get("last_seen_at") or datetime.min)

    def order(item: dict) -> tuple:
        """从没复核过的优先，其次是最久没复核、最久没再发现的。"""
        return (min(item["_checked"]), min(item["_seen"]), item["repo_slug"], item["file_path"])

    ordered = sorted(by_file.values(), key=order)
    index = {}
    for item in ordered:
        item.pop("_checked")
        item.pop("_seen")
        index[(item["repo_slug"], item["file_path"])] = item
    return ordered[:max(int(max_files), 0)], index


def _positive_lines(raw) -> List[int]:
    """台账里的行号列表转成正整数列表；脏数据跳过而不是抛错，一条坏行号不该让整轮巡检失败。"""
    result = []
    for value in raw or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            result.append(number)
    return sorted(set(result))


def update_ledger_for_run(run_uid: str) -> Optional[dict]:
    """
    用一次成功的 SurveyRun 更新其 Survey 的 Ledger，返回各状态的变更计数。

    运行不存在或未成功时返回 None 并不做任何修改：失败的运行产出不完整，
    拿它更新台账会把一大批问题错标成本轮未发现。
    """
    inputs = repo.get_survey_run_ledger_inputs(run_uid)
    if inputs is None or inputs["status"] != RUN_SUCCEEDED:
        return None

    aggregated = aggregate_findings(inputs["findings"])
    existing = repo.get_survey_ledger_map(inputs["survey_id"])
    new_fingerprints = [fp for fp in aggregated if fp not in existing]
    changes = plan_ledger_update(
        existing=existing,
        aggregated=aggregated,
        inspected=inputs["inspected"],
        attempted=inputs["attempted"],
        ignored=inputs["ignored"],
        first_seen_lookup=repo.earliest_survey_finding_times(inputs["survey_id"], new_fingerprints),
        run_uid=run_uid,
        now=utcnow(),
    )
    repo.upsert_survey_ledger(inputs["survey_id"], changes)

    counts = {LEDGER_PRESENT: 0, LEDGER_UNSEEN: 0, LEDGER_IGNORED: 0}
    for change in changes:
        # 只刷新复核时间的变更不带状态
        if "state" in change:
            counts[change["state"]] = counts.get(change["state"], 0) + 1
    logger.info("Ledger updated: run=%s changes=%s", run_uid, counts)
    return counts


def run_detail_link(public_url: str, run_uid: str) -> str:
    """
    运行详情页的对外链接。

    后台前端用的是 hash 路由（web/.env 的 VITE_ROUTER_HISTORY），因此路径里要带 "#/"。
    """
    if not public_url or not run_uid:
        return ""
    return f"{public_url.rstrip('/')}/admin/#/surveys/runs/{run_uid}"


def ledger_columns(include_link: bool) -> List[Column]:
    """本次推送的系统列。没有配置 public_url 时不输出链接列，而不是写一列打不开的相对路径。"""
    columns = list(LEDGER_COLUMNS)
    if include_link:
        columns.append(LINK_COLUMN)
    return columns


def _to_zone(value: Optional[datetime], zone) -> Optional[datetime]:
    """库里的 naive UTC 转成巡检所在时区。表格是给人看的，UTC 时间会让每个人都在心里换算一遍。"""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(zone)


def build_ledger_rows(
    survey: dict, entries: List[dict], public_url: str, first_push: bool
) -> List[LedgerRow]:
    """
    把 Ledger 条目转成镜像行。

    first_push：该 Binding 是否从未成功推送过。
    - 从未推送过 = 这是一张新表，所有行都要写进去（包括本轮未发现与已忽略）；
    - 推送过 = 平台上缺失的行是用户删掉的，只补回状态仍为存在的行 ——
      仍然存在的问题被删了应当重新出现，否则就被悄悄吞掉了；
      已不出现的问题被删了，用户的意思就是"清掉它"。
    """
    zone = resolve_timezone(survey.get("timezone") or "UTC")
    rows: List[LedgerRow] = []
    for entry in entries:
        lines = entry.get("lines") or []
        values = {
            "key": ledger_key(survey["slug"], entry["fingerprint"]),
            "survey": survey.get("name") or "",
            "repo": entry.get("repo_slug") or "",
            "file": entry.get("file_path") or "",
            "lines": ", ".join(str(n) for n in lines),
            "category": CATEGORY_LABELS.get(entry.get("category") or "", entry.get("category") or ""),
            "severity": SEVERITY_LABELS.get(entry.get("severity") or "", entry.get("severity") or ""),
            "title": entry.get("title") or "",
            "body": entry.get("body") or "",
            "state": LEDGER_STATE_LABELS.get(entry.get("state") or "", entry.get("state") or ""),
            "first_seen": _to_zone(entry.get("first_seen_at"), zone),
            "last_seen": _to_zone(entry.get("last_seen_at"), zone),
            "count": int(entry.get("finding_count") or 0),
            "link": run_detail_link(public_url, entry.get("last_run_uid") or ""),
        }
        rows.append(LedgerRow(
            key=values["key"],
            values=values,
            restore_if_missing=first_push or entry.get("state") == LEDGER_PRESENT,
        ))
    return rows
