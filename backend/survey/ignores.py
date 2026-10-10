#!/usr/bin/env python3
"""
已忽略问题（IgnoredIssue）在一次 SurveyRun 里的分发与命中判定。

忽略的单位是问题而不是指纹（ADR-0006）。同一个问题每轮的行号与措辞都会变，
没有稳定的键能认出它，所以"是不是同一个问题"交给 L2 的模型判断（见 analysis.inspect_focus），
这里只做两件确定性的事：把已忽略问题挂到要取证的文件上，以及模型漏标时按标题兜底。

全部是纯函数，不碰数据库也不碰网络。
"""

from typing import Dict, Iterable, List, Tuple

from .analysis import focus_key
from .common import normalize_title

# 一个文件最多交给模型多少条已忽略问题。再多会挤占源码的上下文，
# 而且一个文件攒出几十条"不再提醒"本身就说明该整体处理这个文件了
MAX_IGNORED_PER_FOCUS = 20


def attach_ignored(focuses: List[dict], ignores: Iterable[dict]) -> None:
    """
    把已忽略问题挂到对应文件的关注点上（focus["ignored"]），就地修改。

    文件身份用 focus_key：台账、L1 点名与标记时的发现，路径写法可能不一样。
    ignores 应当只含生效的条目（repo.list_survey_active_ignores）；
    超过上限时保留最新标记的，越新越可能还在被模型报出来。
    """
    by_file: Dict[Tuple[str, str], List[dict]] = {}
    for item in sorted(ignores, key=lambda i: i["id"], reverse=True):
        bucket = by_file.setdefault(focus_key(item), [])
        if len(bucket) < MAX_IGNORED_PER_FOCUS:
            bucket.append(item)
    for focus in focuses:
        focus["ignored"] = by_file.get(focus_key(focus), [])


def match_by_title(findings: List[dict], ignores: Iterable[dict]) -> int:
    """
    模型没标 ignore_id 的产出，按"同指纹且标题规整后完全相同"兜底关联，返回兜底命中的条数。

    只认完全相同的标题：相似就算命中的话，同一文件里换了个说法的另一个问题也会被吞掉，
    那正是按指纹忽略的老毛病。认不出来的宁可再报一次，由人再点一次"不再提醒"。
    """
    index: Dict[Tuple[str, str], int] = {}
    for item in sorted(ignores, key=lambda i: i["id"]):
        title = normalize_title(item.get("title"))
        if title:
            index.setdefault((item["fingerprint"], title), item["id"])
    matched = 0
    for finding in findings:
        if finding.get("ignore_id"):
            continue
        ignore_id = index.get((finding.get("fingerprint", ""), normalize_title(finding.get("title"))))
        if ignore_id:
            finding["ignore_id"] = ignore_id
            matched += 1
    return matched
