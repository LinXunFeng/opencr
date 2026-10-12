#!/usr/bin/env python3
"""
修复合并请求的处理人：追溯问题代码的原始作者，作者不可用时退回仓库负责人（先看人工配置，没有再按项目权限推断）。

原始作者不是"最后一次碰过这几行的人"。批量格式化、改缩进、换引号的提交会把整个文件的 blame 都归到
执行格式化的人名下，指派给他等于把问题丢给一个从没写过这段逻辑的人。所以追溯时跳过只改格式的提交，
一直追到最后一次实质改动这几行的提交。

判定逻辑（formatting_only_lines / changed_lines / pick_author / match_member / unavailable_reason / pick_owner /
pick_configured_owner）
都是纯函数，不碰 git 与网络；find_original_author 与 resolve_assignee 负责把它们和 git、平台接口串起来。
"""

import bisect
import difflib
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..review.gitlab import get_current_user, list_project_members, search_users
from ..storage import repo
from ..storage.models import ASSIGNEE_AUTHOR, ASSIGNEE_CONFIGURED, ASSIGNEE_OWNER
from .workspace import git_blame, git_show_file, read_blame_ignore_revs

logger = logging.getLogger(__name__)

# 处理人至少要有 Developer 权限：低于它的人推不了分支、改不了代码，指派过去只能干看着
MIN_ASSIGNEE_ACCESS = 30
# 仓库负责人取 Maintainer 及以上
MIN_OWNER_ACCESS = 40
# 跳过格式化提交最多重做几轮 blame。每轮都可能揭出更早的一次格式化，层层嵌套超过这个数的仓库很少见，
# 撞到上限时剩下的格式化提交不再计入作者统计（见 pick_author 的调用处）
MAX_FORMAT_SKIP_ROUNDS = 5
# 每轮最多检查几个提交是否只改格式。每检查一个要取回两个历史版本的文件内容，部分克隆下是两次网络请求
MAX_COMMITS_CHECKED_PER_ROUND = 20
# 相隔不超过这么多行没变的行的改动块，放进同一个区域里做字符级对齐。
# 折行常被逐行比对拆成几块（中间夹着一行恰好没变的 ")"），分开对齐会对不上
_FORMAT_REGION_GAP = 3
# 两端同步之后剩下的中间段，去掉格式后超过这么多字符就不做逐字符对齐：对齐的耗时随长度平方增长。
# 超过时这一段按"不是只改格式"处理，代价是那几行仍归给这次提交的作者
_FORMAT_ALIGN_MAX_CHARS = 20_000
# 平台为项目/组织访问令牌建的机器人账号，用户名形如 project_123_bot_xxx
_BOT_USERNAME = re.compile(r"^(project|group)_\d+_bot", re.IGNORECASE)


def _line_shape(line: str) -> str:
    """
    一行代码去掉格式之后剩下的形状：去掉全部空白与逗号、统一引号。

    这几类正是格式化工具最常改动的（换行折行、引号风格、尾逗号）。逗号整个去掉而不是只去尾逗号：
    折行会把行内的分隔逗号变成行尾逗号，两边只去一部分就对不上了。
    它只用来判断"一行是不是只被改了格式"，判错的代价是把一个人的提交多跳过或少跳过一次，
    不会改动任何代码，所以宁可宽一点。
    """
    text = str(line or "").replace("'", '"').replace(",", "")
    return re.sub(r"\s+", "", text)


def _change_regions(old_lines: List[str], new_lines: List[str]) -> Tuple[List[Tuple[int, int, int, int]], set]:
    """
    逐行比对出的改动块，相隔不超过 _FORMAT_REGION_GAP 行的合成一个区域。

    返回 ([(i1, i2, j1, j2)], 没有改动的新行号集合)：区域里夹着的没改动的行要从结果里剔除。
    """
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    regions: List[List[int]] = []
    unchanged: set = set()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            unchanged.update(range(j1 + 1, j2 + 1))
            continue
        if regions and j1 - regions[-1][3] <= _FORMAT_REGION_GAP and i1 - regions[-1][1] <= _FORMAT_REGION_GAP:
            regions[-1][1], regions[-1][3] = i2, j2
        else:
            regions.append([i1, i2, j1, j2])
    return [tuple(r) for r in regions], unchanged


def _sync_prefix(old_shapes: List[str], new_shapes: List[str]) -> Tuple[int, int]:
    """
    从头同步两边的行：不断补行，直到两边累计的形状相等就切一刀。返回最后一刀的位置 (旧行数, 新行数)。

    切出来的每一段两边形状相同，即只改了格式。遇到实质改动（两边谁也不是谁的前缀）就停下。
    """
    i = j = 0
    cut = (0, 0)
    left = right = ""
    while True:
        if left == right:
            cut = (i, j)
            left = right = ""
            if i >= len(old_shapes) or j >= len(new_shapes):
                return cut
            left += old_shapes[i]
            i += 1
        elif len(left) < len(right):
            if not right.startswith(left) or i >= len(old_shapes):
                return cut
            left += old_shapes[i]
            i += 1
        else:
            if not left.startswith(right) or j >= len(new_shapes):
                return cut
            right += new_shapes[j]
            j += 1


def _align_middle(old_shapes: List[str], new_shapes: List[str]) -> set:
    """
    逐字符对齐两段代码，返回新代码里只改了格式的行（下标从 0 开始，相对于 new_shapes）。

    一行算只改格式，要求它的每个字符都能在旧代码里对上，且对应的那段旧字符中间与两侧没有被删掉的内容——
    删掉一部分代码同样是实质改动。
    """
    old_shape = "".join(old_shapes)
    new_shape = "".join(new_shapes)
    if max(len(old_shape), len(new_shape)) > _FORMAT_ALIGN_MAX_CHARS:
        return set()
    to_old: Dict[int, int] = {}
    for start_old, start_new, size in difflib.SequenceMatcher(a=old_shape, b=new_shape, autojunk=False).get_matching_blocks():
        for k in range(size):
            to_old[start_new + k] = start_old + k
    matched = sorted(to_old.values())
    matched_set = set(matched)
    result = set()
    offset = 0
    for index, shape in enumerate(new_shapes):
        start, end = offset, offset + len(shape)
        offset = end
        if start == end:
            # 空行、只有逗号的行：没有内容可言
            result.add(index)
            continue
        if any(pos not in to_old for pos in range(start, end)):
            continue
        first, last = to_old[start], to_old[end - 1]
        if not all(pos in matched_set for pos in range(first, last + 1)):
            continue
        # 紧挨着这一行两侧也不能有没对上的旧字符：那说明这一行两头的代码被删掉了
        prev_index = bisect.bisect_left(matched, first) - 1
        next_index = bisect.bisect_right(matched, last)
        before_end = matched[prev_index] + 1 if prev_index >= 0 else 0
        after_end = matched[next_index] if next_index < len(matched) else len(old_shape)
        if before_end == first and after_end == last + 1:
            result.add(index)
    return result


def formatting_only_lines(old_text: str, new_text: str) -> set:
    """
    new_text 里只被改了格式的行号（从 1 开始）。没有改动的行不在其中。

    按行判断而不是按整个文件、整个改动块：格式化提交里顺手改了一处逻辑很常见，而且逻辑改动常紧挨着被格式化的代码，
    整块比较会让那几十行格式化一起归给执行格式化的人。
    每个改动区域先从两端按形状同步，能对齐的段落就是只改格式；剩下中间对不上的那一段才逐字符对齐。
    逐字符对齐的耗时随长度平方增长，一次格式化加一处逻辑改动时中间段很短，不会拖慢追溯。
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    result: set = set()
    regions, unchanged = _change_regions(old_lines, new_lines)
    for i1, i2, j1, j2 in regions:
        old_shapes = [_line_shape(line) for line in old_lines[i1:i2]]
        new_shapes = [_line_shape(line) for line in new_lines[j1:j2]]
        head_old, head_new = _sync_prefix(old_shapes, new_shapes)
        tail_old, tail_new = _sync_prefix(
            [shape[::-1] for shape in reversed(old_shapes[head_old:])],
            [shape[::-1] for shape in reversed(new_shapes[head_new:])],
        )
        middle_old = old_shapes[head_old:len(old_shapes) - tail_old]
        middle_new = new_shapes[head_new:len(new_shapes) - tail_new]
        formatted = set(range(head_new)) | set(range(len(new_shapes) - tail_new, len(new_shapes)))
        formatted |= {head_new + index for index in _align_middle(middle_old, middle_new)}
        result.update(j1 + 1 + index for index in formatted)
    return result - unchanged


def changed_lines(old_text: str, new_text: str) -> List[int]:
    """
    修改涉及的原文件行号（从 1 开始）。

    改掉或删掉的行直接取；纯插入没有原有行，取插入点前一行（在文件开头插入时取第一行）——
    插进来的代码修的是它周围那段逻辑，写那段逻辑的人才是该看这个合并请求的人。
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    result = set()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            result.update(range(i1 + 1, i2 + 1))
        elif tag == "insert" and old_lines:
            result.add(max(min(i1, len(old_lines)), 1))
    return sorted(result)


def to_ranges(lines: List[int]) -> List[Tuple[int, int]]:
    """把行号列表合并成连续区间，交给 git blame 的 -L。"""
    ranges: List[Tuple[int, int]] = []
    for line in sorted(set(n for n in lines if n > 0)):
        if ranges and line == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], line)
        else:
            ranges.append((line, line))
    return ranges


def _author_key(entry: dict) -> str:
    """作者身份：优先邮箱（同一个人在不同机器上的名字写法常不一样），没有邮箱时用名字。"""
    email = str(entry.get("author_email") or "").strip().lower()
    return email or str(entry.get("author_name") or "").strip().lower()


def pick_author(entries: List[dict]) -> Optional[dict]:
    """
    从 blame 结果里选出原始作者：贡献行数最多的人，行数相同时取最近改过的。

    返回 {"name", "email", "lines"}；没有可用条目时返回 None。
    """
    stats: Dict[str, dict] = {}
    for entry in entries:
        key = _author_key(entry)
        if not key:
            continue
        item = stats.setdefault(
            key, {"name": entry.get("author_name") or "", "email": entry.get("author_email") or "",
                  "lines": 0, "latest": 0}
        )
        item["lines"] += 1
        item["latest"] = max(item["latest"], int(entry.get("author_time") or 0))
    if not stats:
        return None
    best = max(stats.values(), key=lambda item: (item["lines"], item["latest"]))
    return {"name": best["name"], "email": best["email"], "lines": best["lines"]}


def _commit_is_formatting_only(repo_dir: Path, entries: List[dict]) -> bool:
    """
    blame 归到同一个提交的这几行，在那个提交里是否都只是被改了格式。文件在该提交里首次出现时不算。

    只要其中一行是实质改动就整个不算：git blame 只能按提交忽略，忽略它会把那一行实质改动也归给更早的人。
    """
    first = entries[0]
    if not first.get("previous_sha"):
        return False
    old = git_show_file(repo_dir, first["previous_sha"], first.get("previous_filename") or first["filename"])
    new = git_show_file(repo_dir, first["sha"], first.get("filename") or "")
    if old is None or new is None:
        return False
    formatted = formatting_only_lines(old, new)
    return all(entry.get("orig_line") in formatted for entry in entries)


def find_original_author(repo_dir: Path, file_path: str, lines: List[int]) -> Optional[dict]:
    """
    追溯若干行的原始作者，返回 {"name", "email", "lines"}；追溯不出时返回 None。

    每轮 blame 之后检查归到的提交是不是只改了格式，是就加进忽略清单重做 blame，直到不再出现新的格式化提交。
    仓库自带的 .git-blame-ignore-revs 一开始就加进去。
    """
    ranges = to_ranges(lines)
    if not ranges:
        return None
    ignore = read_blame_ignore_revs(repo_dir)
    formatting: set = set()
    checked: set = set()
    entries: List[dict] = []
    for _ in range(MAX_FORMAT_SKIP_ROUNDS + 1):
        entries = git_blame(repo_dir, file_path, ranges, ignore)
        by_sha: Dict[str, List[dict]] = {}
        for entry in entries:
            by_sha.setdefault(entry["sha"], []).append(entry)
        # 行数多的提交先查：名额有限时，先排除最可能左右结果的那几个
        pending = sorted((sha for sha in by_sha if sha not in checked), key=lambda sha: -len(by_sha[sha]))
        newly = []
        for sha in pending[:MAX_COMMITS_CHECKED_PER_ROUND]:
            checked.add(sha)
            if _commit_is_formatting_only(repo_dir, by_sha[sha]):
                newly.append(sha)
        if not newly:
            break
        formatting.update(newly)
        ignore = ignore + newly
    # 撞到轮数上限时，最后一轮里可能还有认出来的格式化提交，统计时剔除；全被剔除就退回不剔除
    substantive = [entry for entry in entries if entry["sha"] not in formatting]
    return pick_author(substantive or entries)


def _email_local_part(email: str) -> str:
    """邮箱 @ 之前的部分，小写。"""
    return str(email or "").split("@", 1)[0].strip().lower()


def match_member(author: dict, members: List[dict], searched: List[dict]) -> Tuple[Optional[dict], bool]:
    """
    把提交作者对应到平台账号，返回 (账号, 是否是项目成员)。对应不上返回 (None, False)。

    按可靠程度依次尝试：
    1. 按邮箱搜到的账号（searched）里邮箱完全一致的；
    2. 邮箱前缀等于成员用户名的（公司邮箱多用用户名做前缀）；
    3. 名字与成员显示名完全一致、且只有一个人叫这个名字的。
    同名的人不止一个时宁可对应不上、退回仓库负责人，也不要指派给一个同名的陌生人。
    """
    members_by_id = {m.get("id"): m for m in members if m.get("id") is not None}
    email = str(author.get("email") or "").strip().lower()
    if email:
        for user in searched:
            emails = {str(user.get(k) or "").strip().lower() for k in ("email", "public_email")}
            if email in emails:
                member = members_by_id.get(user.get("id"))
                return (member, True) if member else (user, False)

    local = _email_local_part(email)
    if local:
        for member in members:
            if str(member.get("username") or "").strip().lower() == local:
                return member, True

    name = str(author.get("name") or "").strip()
    if name:
        same_name = [m for m in members if str(m.get("name") or "").strip() == name]
        if len(same_name) == 1:
            return same_name[0], True
    return None, False


def _is_bot(account: dict) -> bool:
    """平台的机器人账号：访问令牌建的、或平台标记为 bot 的。"""
    return bool(account.get("bot")) or bool(_BOT_USERNAME.match(str(account.get("username") or "")))


def unavailable_reason(account: Optional[dict], is_member: bool, self_id: Optional[int]) -> str:
    """原始作者的账号为什么不能当处理人；能当时返回空串。"""
    if account is None:
        return "无法对应到平台账号"
    if self_id is not None and account.get("id") == self_id:
        return "是 OpenCR 自己的账号"
    state = str(account.get("state") or "active").strip().lower()
    if state != "active":
        return f"账号不可用（{state}）"
    if not is_member:
        return "已不在项目成员中"
    if not _membership_active(account):
        return "成员资格尚未生效"
    if int(account.get("access_level") or 0) < MIN_ASSIGNEE_ACCESS:
        return "项目权限低于 Developer"
    if _is_bot(account):
        return "是机器人账号"
    return ""


def _membership_active(member: dict) -> bool:
    """成员资格是否已生效。等待审批的成员（awaiting）还不能被指派。没有这个字段的旧版本平台视为已生效。"""
    return str(member.get("membership_state") or "active").strip().lower() == "active"


def pick_owner(members: List[dict], self_id: Optional[int]) -> Optional[dict]:
    """
    从一组成员里挑仓库负责人：权限最高（Owner 优先于 Maintainer）的可用成员，同级取最早加入平台的（id 最小）。

    平台的合并请求在免费版只能指派一个人，所以只挑一个；挑法要确定，同一个仓库每次都落到同一个人身上。
    先从项目直属成员里挑、挑不出再看继承来的，由调用方决定（见 resolve_assignee）。
    """
    candidates = [
        m for m in members
        if int(m.get("access_level") or 0) >= MIN_OWNER_ACCESS
        and str(m.get("state") or "active").strip().lower() == "active"
        and _membership_active(m)
        and not _is_bot(m)
        and (self_id is None or m.get("id") != self_id)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda m: (-int(m.get("access_level") or 0), int(m.get("id") or 0)))[0]


def _direct_members(project_path: str) -> List[dict]:
    """
    项目直属成员；读取失败时返回空列表，由调用方退回含继承的成员。

    负责人先从直属成员里挑：继承来的成员里，顶层组织的 Owner 往往是平台管理员，
    按权限排序会排在这个项目真正的 Maintainer 前面。
    """
    try:
        return list_project_members(project_path, inherited=False)
    except Exception as e:
        # 后果只是负责人可能落到继承来的高权限成员身上
        logger.warning("Listing direct project members failed: project=%s: %s", project_path, e)
        return []


def resolve_assignee(project_path: str, author: Optional[dict]) -> dict:
    """
    决定修复合并请求的处理人。

    依次尝试：原始作者 → 仓库负责人配置里第一个可用的人 → 按项目权限推断的仓库负责人。
    返回 {"source", "id", "username", "name", "note"}：source 为 ASSIGNEE_AUTHOR / ASSIGNEE_CONFIGURED / ASSIGNEE_OWNER，
    都找不到时为空串、id 为 None（合并请求照常提出，只是不指派）。note 说明为什么没指派给原始作者。
    平台接口出错不抛出：处理人只是锦上添花，为它让整个修复失败不划算。
    """
    result = {"source": "", "id": None, "username": "", "name": "", "note": ""}
    try:
        members = list_project_members(project_path)
    except Exception as e:
        logger.warning("Listing project members failed: project=%s: %s", project_path, e)
        # 不把报错原文写进 note：它会进合并请求说明，项目里所有人都看得到，可能带出内网地址与报错细节
        result["note"] = "读取项目成员失败，未指派处理人"
        return result
    try:
        self_id = get_current_user().get("id")
    except Exception as e:
        # 读不到自己是谁的后果只是少了一条排除规则，OpenCR 的账号通常也不是 Maintainer
        logger.warning("Reading current user failed: %s", e)
        self_id = None

    reason = "没有追溯到原始作者"
    if author:
        searched: List[dict] = []
        if author.get("email"):
            try:
                searched = search_users(author["email"])
            except Exception as e:
                # 搜不了就靠用户名与显示名匹配，见 match_member
                logger.warning("Searching user by email failed: %s", e)
        account, is_member = match_member(author, members, searched)
        reason = unavailable_reason(account, is_member, self_id)
        if not reason:
            result.update(
                source=ASSIGNEE_AUTHOR, id=account.get("id"),
                username=account.get("username") or "", name=account.get("name") or "",
            )
            return result
        reason = f"原始作者 {author.get('name') or author.get('email')} {reason}"

    configured, config_note = _configured_owner(project_path, members, self_id)
    if configured is not None:
        result.update(
            source=ASSIGNEE_CONFIGURED, id=configured.get("id"),
            username=configured.get("username") or "", name=configured.get("name") or "",
            note=f"{reason}，改为指派给配置的仓库负责人{config_note}",
        )
        return result
    if config_note:
        reason = f"{reason}；{config_note}"

    owner = pick_owner(_direct_members(project_path), self_id) or pick_owner(members, self_id)
    if owner is None:
        result["note"] = f"{reason}，且项目里没有可用的 Maintainer，未指派处理人"
        return result
    result.update(
        source=ASSIGNEE_OWNER, id=owner.get("id"),
        username=owner.get("username") or "", name=owner.get("name") or "",
        note=f"{reason}，改为按项目权限指派仓库负责人",
    )
    return result


def pick_configured_owner(usernames: List[str], members: List[dict], self_id: Optional[int]) -> Tuple[Optional[dict], List[str]]:
    """
    从配置的负责人里按顺序挑第一个可用的，返回 (成员, 每个被跳过的人与原因)。

    配置的人同样要满足处理人的条件（账号可用、是项目成员、权限不低于 Developer）：
    配置可能是半年前写的，人早就离开了项目，指派给他等于没人处理。
    """
    by_username = {str(m.get("username") or "").strip().lower(): m for m in members}
    skipped: List[str] = []
    for username in usernames:
        member = by_username.get(str(username or "").strip().lstrip("@").lower())
        reason = "不在项目成员中" if member is None else unavailable_reason(member, True, self_id)
        if not reason:
            return member, skipped
        skipped.append(f"{username} {reason}")
    return None, skipped


def _configured_owner(project_path: str, members: List[dict], self_id: Optional[int]) -> Tuple[Optional[dict], str]:
    """
    按仓库负责人配置找处理人，返回 (成员, 说明)。

    找到时说明是"（命中的配置路径）"；配置了但没有可用的人时成员为 None、说明列出每个人被跳过的原因；
    没有适用的配置时两者都为空，调用方按项目权限推断。
    """
    try:
        entry = repo.find_repo_owner(project_path)
    except Exception as e:
        # 后果是这一次跳过配置、按项目权限推断负责人
        logger.warning("Reading repo owner config failed: project=%s: %s", project_path, e)
        return None, ""
    if entry is None:
        return None, ""
    member, skipped = pick_configured_owner(entry["usernames"], members, self_id)
    if member is not None:
        return member, f"（按 {entry['path']} 的配置）"
    return None, f"{entry['path']} 配置的负责人都不可用（{'、'.join(skipped)}）"
