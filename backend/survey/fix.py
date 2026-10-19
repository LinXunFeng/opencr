#!/usr/bin/env python3
"""
FixRun（修复运行）的唯一执行入口：针对一条巡检 Finding 生成修改、本地验证、提出合并请求并指派处理人。
单条手动发起、一键全部修复、巡检结束后自动发起都走这里（execute_survey_fix / start_fix_batch），不各写一份。

范围刻意收窄到"只改发现所在的那一个文件"：L2 取证就是按单个文件下的结论，修复沿用同一个边界；
跨文件、跨仓库的问题（例如接口两端字段不一致）需要人来决定改哪一边，交给模型会提出一半的修改。

本地验证不在服务端构建、不跑测试，只做不需要目标仓库工具链的检查加一次模型复核，理由见
docs/adr/0007-survey-fix-local-verification.md。构建与测试交给目标仓库自己的 CI。

判定逻辑（apply_edits / static_checks / verdict_passed / project_path_from_url / pick_commit_author / mr_title）是纯函数，不碰 git、网络与数据库。
"""

import difflib
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from ..review.common import ReviewError
from ..review.config import load_gitlab_config
from ..review.gitlab import create_merge_request, get_current_user
from ..storage import repo
from ..storage.models import (
    ERROR_INTERRUPTED,
    ERROR_UNEXPECTED,
    FIX_GIT_FAILED,
    FIX_MODEL_FAILED,
    FIX_NOT_APPLICABLE,
    FIX_PHASE_ASSIGNING,
    FIX_PHASE_GENERATING,
    FIX_PHASE_SUBMITTING,
    FIX_PHASE_VERIFYING,
    FIX_PLATFORM_FAILED,
    FIX_UNSUPPORTED,
    FIX_VERIFY_FAILED,
    PUSH_TRIGGER_AUTO,
    RUN_FAILED,
    RUN_SKIPPED,
    RUN_SUCCEEDED,
)
# 与巡检分析共用同一份模型配置、调用封装与输出容错解析，不另起一套
from .analysis import _call_model, _parse_json_payload
from .assignee import changed_lines, find_original_author, resolve_assignee
from .common import SurveyError, issue_key
from .config import load_fix_mr_title_prefix, load_public_url, load_survey_config, resolve_budget
from .push import PushRejected, begin_pushes, execute_pushes
from .workspace import CommitAuthor, commit_and_push_fix, delete_fix_workspace, prepare_fix_repo

logger = logging.getLogger(__name__)

# 生成 + 验证最多几轮。第二轮会带上第一轮没通过的原因；再多轮通常只是在同一个错误上打转，成本却线性增长
MAX_ATTEMPTS = 2
# 修复分支名前缀。固定前缀让仓库管理员能一眼认出、也能按前缀批量清理
FIX_BRANCH_PREFIX = "opencr/fix-"
# 一次修复最多改多少行。修一个问题改出几百行，基本是模型在重写文件，人工审查它的成本会高过它的价值
MAX_CHANGED_LINES = 200
# 一键全部修复与巡检后自动发起的修复每批最多处理多少个问题。逐个执行时一个要几分钟，20 个已是一两个小时；
# 也是对代码平台的保护：一次巡检往各仓库甩出上百个合并请求，没人看得过来，只会让人把功能关掉。剩下的下一批再处理
MAX_FIXES_PER_BATCH = 20
# 批量修复租约的有效期，每个修复开始前续一次。单个修复（克隆、两三次模型调用、推分支）通常几分钟，
# 半小时留足余量；进程中途退出时，最多这么久之后可以再发起下一批
FIX_BATCH_LEASE_SECONDS = 1800
# 修复成功后重推台账遇到输出目标正忙时，最多再等几次、每次等多久。
# 一次台账推送在限流退避下要几十秒，两分钟足够等它结束；更久的大概率是疑似中断的推送，等下去没有意义
_REPUBLISH_BUSY_RETRIES = 6
_REPUBLISH_BUSY_WAIT_SECONDS = 20
# 平台合并请求标题的长度上限
_MR_TITLE_MAX = 255
# 标题模板里代表问题标题的占位符。只认这一个，模板里其他花括号原样保留
MR_TITLE_PLACEHOLDER = "{title}"
# 修复提交的兜底作者：既没有配置、也查不到令牌账号的名字或邮箱时使用
FALLBACK_COMMIT_AUTHOR = CommitAuthor("OpenCR", "opencr@noreply.invalid")


class FixFailure(Exception):
    """修复运行的可预期失败，携带 error_kind 与收尾状态。"""

    def __init__(self, kind: str, message: str, status: str = RUN_FAILED):
        """kind 取 models 里的 FIX_*；status 默认失败，"模型认为没有可改的"用 RUN_SKIPPED。"""
        super().__init__(message)
        self.kind = kind
        self.status = status


class EditError(ValueError):
    """模型给出的修改无法套用到原文件上。"""


def project_path_from_url(url: str, platform_url: str = "") -> str:
    """
    从仓库地址取出平台上的项目路径（group/sub/project），用作接口里的项目 id。

    平台部署在子路径下时（https://host/gitlab/group/app），先按配置的平台地址剥前缀，
    否则子路径会被当成组织的一部分。
    """
    text = re.split(r"[?#]", str(url or "").strip(), maxsplit=1)[0].rstrip("/")
    text = re.sub(r"\.git$", "", text)
    base = str(platform_url or "").rstrip("/")
    if base and text.lower().startswith(base.lower() + "/"):
        return text[len(base) + 1:].strip("/")
    path = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*", "", text)
    path = re.sub(r"^[^/@]*@[^/:]*:", "", path)
    return path.strip("/")


def pick_commit_author(configured_name: str, configured_email: str, account: dict) -> CommitAuthor:
    """
    修复提交的署名。名字与邮箱各自取值：先取配置，再取令牌账号（account 是平台上令牌所属账号的信息），
    都没有时取 FALLBACK_COMMIT_AUTHOR。
    """
    # 默认跟随令牌账号而不是固定的服务身份：推分支、开合并请求的本来就是这个账号，
    # 提交作者与之一致，平台才能把提交关联到账号上，"提交者邮箱必须属于推送者"的推送规则也才能通过
    name = str(configured_name or "").strip()
    if not name:
        names = (account.get("name"), account.get("username"))
        name = next((str(v).strip() for v in names if str(v or "").strip()), "") or FALLBACK_COMMIT_AUTHOR.name
    email = str(configured_email or "").strip()
    if not email:
        # 提交邮箱优先：账号选了隐藏邮箱时它是平台生成的 noreply 地址，用主邮箱会把对方不想公开的地址写进提交历史
        candidates = (account.get("commit_email"), account.get("email"), account.get("public_email"))
        email = next((str(v).strip() for v in candidates if "@" in str(v or "")), "") or FALLBACK_COMMIT_AUTHOR.email
    return CommitAuthor(name, email)


def resolve_commit_author() -> CommitAuthor:
    """按配置与令牌账号决定修复提交的署名；两项都配置了时不访问平台。"""
    cfg = load_survey_config()
    name, email = cfg.get("fix_commit_name", ""), cfg.get("fix_commit_email", "")
    account: dict = {}
    if not (name and email):
        try:
            account = get_current_user() or {}
        except Exception:
            # 查不到账号只影响提交上署谁的名字，退回兜底身份照常提交；
            # 若目标项目有推送规则卡提交者邮箱，推送那一步会失败并把原因记到修复记录上
            logger.warning("Fetching token account for commit author failed, using fallback", exc_info=True)
    return pick_commit_author(name, email, account)


def mr_title(prefix: str, title: str, template: str = "") -> str:
    """
    修复合并请求的标题，不超过平台上限。

    template 是巡检的标题模板，非空时由它决定整个标题（MR_TITLE_PLACEHOLDER 换成问题标题，不再加 prefix）；
    为空时是 prefix 加问题标题，prefix 为空时只有问题标题。
    """
    title = " ".join(str(title or "").split())
    # 库里的脏值（手改过库）按接口的规则收拾：换行会把标题拆坏
    template = " ".join(str(template or "").split())
    if not template:
        prefix = str(prefix or "").strip()
        return (f"{prefix} {title}" if prefix else title)[:_MR_TITLE_MAX]
    count = template.count(MR_TITLE_PLACEHOLDER)
    if not count:
        return template[:_MR_TITLE_MAX]
    # 超长时只截问题标题，模板里的固定部分一个字不动：它往往是需求链接，截掉半个链接，
    # 依赖它的自动合并与需求关联会静默失效。模板本身有长度上限，固定部分不会把问题标题挤没
    room = max(_MR_TITLE_MAX - (len(template) - count * len(MR_TITLE_PLACEHOLDER)), 0) // count
    # 用 replace 而不是 str.format：链接里的花括号、问题标题里碰巧出现的占位符都应当原样保留
    return template.replace(MR_TITLE_PLACEHOLDER, title[:room])


def attach_fixes(findings: List[dict], fixes: List[dict]) -> None:
    """
    把修复运行挂到报告里对应的发现上（finding["fixes"]），就地修改。

    同一个问题在之后的运行里是另一条发现，所以除了 finding_id，还按"同指纹 + 标题规整后相同"认 ——
    与 repo.add_survey_ignore 认"同一个问题被原样报了几遍"的口径一致。换了说法的不挂，宁可漏挂，
    也不要把 A 问题的合并请求挂到同一文件同类的 B 问题上。
    """
    for finding in findings:
        key = issue_key(finding.get("fingerprint"), finding.get("title"))
        finding["fixes"] = [
            fix for fix in fixes
            if fix.get("fingerprint") == finding.get("fingerprint")
            and (fix.get("finding_id") == finding.get("id") or (key[1] and issue_key(fix.get("fingerprint"), fix.get("title")) == key))
        ]


def apply_edits(text: str, edits) -> str:
    """
    把模型给出的"原文 → 替换文"逐条套用到文件上，返回修改后的全文。

    要求每段原文在当时的文件里恰好出现一次。让模型输出整份新文件更省事，但长文件会被输出上限截断，
    截掉的代码会被静默删除；按原文定位的修改套不上时能当场发现。
    """
    if not isinstance(edits, list) or not edits:
        raise EditError("模型没有给出任何修改")
    current = text
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            raise EditError(f"第 {index} 处修改格式不对")
        search = str(edit.get("search") or "")
        replace = str(edit.get("replace") or "")
        if not search:
            raise EditError(f"第 {index} 处修改没有给出原文")
        count = current.count(search)
        if count != 1:
            raise EditError(f"第 {index} 处修改的原文在文件里出现了 {count} 次，必须恰好一次")
        current = current.replace(search, replace, 1)
    if current == text:
        raise EditError("修改没有带来任何变化")
    return current


def static_checks(file_path: str, old_text: str, new_text: str) -> List[dict]:
    """
    不需要目标仓库工具链就能做的检查，每项 {"name", "passed", "detail"}。

    只覆盖解释器自带解析器的格式（Python、JSON）。其他语言的语法要靠目标仓库的 CI，
    在服务端装一遍各语言工具链不现实（见 ADR-0007）。
    """
    checks: List[dict] = []
    count = len(changed_lines(old_text, new_text))
    checks.append({
        "name": "改动范围",
        "passed": count <= MAX_CHANGED_LINES,
        "detail": f"涉及原文件 {count} 行" + ("" if count <= MAX_CHANGED_LINES else f"，超过上限 {MAX_CHANGED_LINES}"),
    })
    suffix = Path(file_path).suffix.lower()
    if suffix == ".py":
        try:
            compile(new_text, file_path, "exec")
            checks.append({"name": "Python 语法", "passed": True, "detail": ""})
        except SyntaxError as e:
            checks.append({"name": "Python 语法", "passed": False, "detail": f"第 {e.lineno} 行：{e.msg}"})
    elif suffix == ".json":
        try:
            json.loads(new_text)
            checks.append({"name": "JSON 语法", "passed": True, "detail": ""})
        except ValueError as e:
            checks.append({"name": "JSON 语法", "passed": False, "detail": str(e)})
    return checks


def verdict_passed(attempt: dict) -> bool:
    """一轮验证是否通过：全部检查通过，且模型复核认为问题已解决、没有引入新问题。"""
    return all(c.get("passed") for c in attempt.get("checks") or []) and not attempt.get("problems")


def unified_diff(file_path: str, old_text: str, new_text: str) -> str:
    """两份文本之间的 unified diff，用于展示与模型复核。"""
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
    )


def fix_detail_link(public_url: str, run_uid: str) -> str:
    """修复运行详情页的对外链接。与 ledger.run_detail_link 同理，后台前端是 hash 路由，路径里要带 "#/"。"""
    if not public_url or not run_uid:
        return ""
    return f"{public_url.rstrip('/')}/admin/#/surveys/fixes/{run_uid}"


def _issue_block(fix: dict) -> str:
    """交给模型的问题描述。"""
    line = f"第 {fix['line']} 行附近" if int(fix.get("line") or 0) > 0 else "未定位到行"
    return (
        f"仓库：{fix['repo_slug']}\n文件：{fix['file_path']}（{line}）\n"
        f"类别：{fix['category']}　严重度：{fix['severity']}\n"
        f"标题：{fix['title']}\n描述：\n{fix.get('body') or '（无）'}"
    )


def fix_prompt_block(fix_prompt: str) -> str:
    """
    把巡检的修复提示词包成交给模型的一段补充要求；没有配置时返回空串。

    生成与复核用同一段文字：复核看不到这些要求，就会把"按要求改成繁体"当成改了无关代码打回，
    也拦不住没照要求写的修改。
    """
    text = fix_prompt.strip()
    if not text:
        return ""
    # 写明它管不到边界：Admin 写一句"顺便把整个文件统一一下"，模型照做就会改出几百行无关修改
    return (
        "\n这个巡检的管理员对修改另有以下要求。它们约束修改的代码与修改说明（summary）怎么写，"
        "与「只改这一个文件、只改修复问题所必需的代码」冲突时，以后者为准：\n"
        f"<<<\n{text}\n>>>\n"
    )


def propose_fix(fix: dict, source: str, feedback: List[str], fix_prompt: str = "") -> dict:
    """
    让模型针对问题给出修改，返回 {"applicable", "reason", "summary", "edits"}。fix_prompt 是巡检的修复提示词。

    发现可能是几周前的，代码已经变了，所以允许模型说"最新代码里已经没有这个问题"，而不是硬改一处。
    """
    retry_block = ""
    if feedback:
        retry_block = (
            "\n上一次给出的修改没有通过验证，原因如下，请避开这些问题重新给出：\n"
            + "\n".join(f"- {item}" for item in feedback)
            + "\n"
        )
    prompt = f"""你是资深工程师。代码巡检在下面这个文件里报告了一个问题，请给出修复它的最小修改。

{_issue_block(fix)}
{retry_block}{fix_prompt_block(fix_prompt)}
要求：
1. 只改这一个文件，只改修复这个问题所必需的代码；不要顺手重构、改格式或修别的问题。
2. 先确认问题在下面的最新代码里是否仍然存在。已经不存在、或必须改别的文件才能修时，applicable 填 false 并说明原因。
3. 修改用"原文 → 替换文"表示：search 必须是文件里**原样**存在、且只出现一次的连续文本（含缩进），
   replace 是替换后的文本。多处修改就给多项。
4. summary 用一两句话说明改了什么、为什么这样改，会写进合并请求的说明里。
5. 只输出 JSON 对象，形如：
   {{"applicable":true,"reason":"","summary":"...","edits":[{{"search":"...","replace":"..."}}]}}

以下是文件的最新内容：

```
{source}
```
"""
    try:
        payload = _parse_json_payload(_call_model(prompt, max_tokens=8000))
    except SurveyError as e:
        raise FixFailure(FIX_MODEL_FAILED, str(e)) from e
    if not isinstance(payload, dict):
        raise FixFailure(FIX_MODEL_FAILED, "模型给出的修改无法解析")
    return {
        # 没写 applicable 时按"要改"处理：模型给了 edits 却漏了这个字段很常见，后面套用与验证会把关
        "applicable": payload.get("applicable") is not False,
        "reason": str(payload.get("reason") or "").strip()[:1000],
        "summary": str(payload.get("summary") or "").strip()[:2000],
        "edits": payload.get("edits"),
    }


def review_fix(fix: dict, diff: str, new_source: str, fix_prompt: str = "") -> List[str]:
    """
    让模型复核修改，返回发现的问题；空列表表示通过。fix_prompt 是巡检的修复提示词，修改不符合它也算问题。

    复核要回答两件事：原问题解决了没有、有没有引入新问题。输出解析不了也算没通过 ——
    验证没跑成不能当成验证通过。
    """
    prompt = f"""你是严格的代码审查者。下面是针对一个已知问题提出的修改，请复核它。

{_issue_block(fix)}
{fix_prompt_block(fix_prompt)}
修改（unified diff）：
```diff
{diff}
```

修改后的完整文件：
```
{new_source}
```

请判断：
1. 这个修改是否真正解决了上面的问题；
2. 是否引入了新的缺陷（语法错误、逻辑错误、行为变化、遗漏的分支、改了无关代码等）；
3. 上面有管理员的补充要求时，修改的部分是否符合它们。

只输出 JSON 对象，形如：{{"resolved":true,"problems":[]}}
problems 列出每个具体问题，一句话一条；没有问题时为空数组。
"""
    try:
        payload = _parse_json_payload(_call_model(prompt, max_tokens=2000))
    except SurveyError as e:
        raise FixFailure(FIX_MODEL_FAILED, str(e)) from e
    if not isinstance(payload, dict):
        return ["模型复核的输出无法解析"]
    problems = [str(p).strip()[:500] for p in payload.get("problems") or [] if str(p).strip()]
    if payload.get("resolved") is not True:
        problems.insert(0, "模型复核认为原问题没有解决")
    return problems


def _read_source(repo_dir: Path, file_path: str, max_chars: int) -> Tuple[str, bool]:
    """
    读取要修的文件，返回 (统一成 \\n 换行的文本, 原文件是否用 CRLF)。

    按字节读、自己处理换行：按文本模式读写会把 CRLF 文件整个改成 LF，合并请求里每一行都会显示被改过。
    """
    root = repo_dir.resolve()
    if (repo_dir / file_path).is_symlink():
        # 写回会落到链接指向的文件上，提交里却只有链接本身；修改的应当是链接指向的那个文件
        raise FixFailure(FIX_UNSUPPORTED, "文件是符号链接，不做自动修改")
    target = (repo_dir / file_path).resolve()
    if target == root or root not in target.parents:
        raise FixFailure(FIX_UNSUPPORTED, "文件路径越出了仓库")
    if not target.is_file():
        raise FixFailure(FIX_NOT_APPLICABLE, "目标分支上已没有这个文件", status=RUN_SKIPPED)
    try:
        raw = target.read_bytes().decode("utf-8")
    except UnicodeDecodeError as e:
        raise FixFailure(FIX_UNSUPPORTED, "文件不是 UTF-8 编码，不支持自动修改") from e
    if len(raw) > max_chars:
        # 与 L2 取证同一个上限：放不下整份文件，模型就看不全上下文，给出的修改不可信
        raise FixFailure(FIX_UNSUPPORTED, f"文件超过 {max_chars} 个字符，模型看不全，不做自动修改")
    crlf = "\r\n" in raw
    return (raw.replace("\r\n", "\n") if crlf else raw), crlf


def _write_source(repo_dir: Path, file_path: str, text: str, crlf: bool) -> None:
    """把修改写回文件，保持原来的换行风格。"""
    data = text.replace("\n", "\r\n") if crlf else text
    (repo_dir / file_path).write_bytes(data.encode("utf-8"))


def _generate_verified(run_uid: str, fix: dict, source: str, fix_prompt: str) -> Tuple[str, str, List[dict]]:
    """
    生成修改并验证，最多 MAX_ATTEMPTS 轮。返回 (修改后的全文, 修改说明, 每轮的验证记录)。fix_prompt 是巡检的修复提示词。

    没有一轮通过时抛 FixFailure(verify_failed)；模型一开始就认为不需要改时抛 FixFailure(not_applicable)。
    """
    attempts: List[dict] = []
    feedback: List[str] = []
    for index in range(MAX_ATTEMPTS):
        repo.update_survey_fix(run_uid, phase=FIX_PHASE_GENERATING, verification=attempts)
        proposal = propose_fix(fix, source, feedback, fix_prompt)
        if not proposal["applicable"]:
            if index == 0:
                raise FixFailure(
                    FIX_NOT_APPLICABLE, proposal["reason"] or "模型认为最新代码里没有需要修改的地方",
                    status=RUN_SKIPPED,
                )
            # 第二轮才说"不用改"，是在回避上一轮的验证意见，不能当成问题已不存在
            attempts.append({"passed": False, "checks": [], "problems": [f"模型放弃修改：{proposal['reason']}"]})
            break

        attempt = {"passed": False, "checks": [], "problems": []}
        try:
            new_source = apply_edits(source, proposal["edits"])
        except EditError as e:
            attempt["checks"].append({"name": "补丁可应用", "passed": False, "detail": str(e)})
            attempts.append(attempt)
            feedback = [str(e)]
            continue
        attempt["checks"].append({"name": "补丁可应用", "passed": True, "detail": ""})

        repo.update_survey_fix(run_uid, phase=FIX_PHASE_VERIFYING, verification=attempts + [attempt])
        attempt["checks"].extend(static_checks(fix["file_path"], source, new_source))
        if all(c["passed"] for c in attempt["checks"]):
            # 静态检查没过就不必再花一次模型调用复核
            attempt["problems"] = review_fix(
                fix, unified_diff(fix["file_path"], source, new_source), new_source, fix_prompt
            )
        attempt["passed"] = verdict_passed(attempt)
        attempts.append(attempt)
        if attempt["passed"]:
            return new_source, proposal["summary"], attempts
        feedback = [f"{c['name']}未通过：{c['detail']}" for c in attempt["checks"] if not c["passed"]]
        feedback += attempt["problems"]

    repo.update_survey_fix(run_uid, verification=attempts)
    raise FixFailure(FIX_VERIFY_FAILED, f"{len(attempts)} 次生成的修改都没有通过本地验证，未提出合并请求")


def _mr_description(fix: dict, survey_name: str, summary: str, attempts: List[dict], assignee: dict) -> str:
    """合并请求的说明。写清它是机器生成的、验证做到了哪一步、合并之后台账会怎样。"""
    public_url = load_public_url()
    passed = next((a for a in reversed(attempts) if a.get("passed")), {})
    checks = "\n".join(
        f"- {c['name']}：通过" + (f"（{c['detail']}）" if c.get("detail") else "") for c in passed.get("checks") or []
    )
    lines = [
        f"OpenCR 巡检「{survey_name}」发现了下面这个问题，这是自动生成的修改，请审查后再合并。",
        "",
        "### 问题",
        "",
        _issue_block(fix),
        "",
        "### 修改说明",
        "",
        summary or "（模型未给出说明）",
        "",
        "### 本地验证",
        "",
        checks,
        "- 模型复核：认为问题已解决、未引入新问题",
        "",
        "_本地验证不包括构建与测试，请以本仓库的 CI 与人工审查为准。_",
        "",
    ]
    if assignee.get("note"):
        lines += [f"_处理人：{assignee['note']}。_", ""]
    lines.append("_合并不会直接改变巡检台账的状态：后续巡检复核到问题不再出现时才会更新。_")
    if public_url:
        lines.append(f"\n[修复运行详情]({fix_detail_link(public_url, fix['run_uid'])})")
    return "\n".join(lines)


def execute_survey_fix(run_uid: str, republish: bool = True) -> bool:
    """
    执行一次已登记的修复运行，返回是否提出了合并请求。任何错误都收敛为这条记录的失败状态，不向上抛。

    成功提出合并请求后，以该巡检最近一次成功运行的名义重推一次台账：链接要尽快出现在外部表格里，
    不能等到下一轮巡检。批量执行时传 republish=False，由批次结束后统一重推一次。
    """
    fix = repo.get_survey_fix(run_uid)
    if fix is None:
        return False
    succeeded = False
    try:
        _execute(run_uid, fix)
        succeeded = repo.finish_survey_fix(run_uid, RUN_SUCCEEDED)
    except FixFailure as e:
        logger.info("Survey fix ended without MR: run=%s kind=%s: %s", run_uid, e.kind, e)
        repo.finish_survey_fix(run_uid, e.status, error_kind=e.kind, error_message=str(e))
    except SurveyError as e:
        # 走到这里的 SurveyError 只会来自 git：模型调用的已在 propose/review 里转成 FixFailure
        logger.warning("Survey fix git failure: run=%s: %s", run_uid, e)
        repo.finish_survey_fix(run_uid, RUN_FAILED, error_kind=FIX_GIT_FAILED, error_message=str(e))
    except ReviewError as e:
        logger.warning("Survey fix platform failure: run=%s: %s", run_uid, e)
        repo.finish_survey_fix(run_uid, RUN_FAILED, error_kind=FIX_PLATFORM_FAILED, error_message=str(e))
    except Exception as e:
        logger.exception("Survey fix crashed: run=%s", run_uid)
        repo.finish_survey_fix(run_uid, RUN_FAILED, error_kind=ERROR_UNEXPECTED, error_message=str(e))
    finally:
        delete_fix_workspace(run_uid)

    if succeeded and republish:
        republish_ledger(fix["survey_id"])
    return succeeded


def fix_unsupported_reason() -> str:
    """
    当前配置的代码平台为什么不能提修复；能提时返回空串。

    单条发起、一键全部、巡检后自动发起与执行本身都用这一个判断：自动发起漏掉它的话，每轮会登记一批注定失败的修复，
    而有过修复记录的问题不再被批量发起，这些问题就再也轮不到了。
    """
    platform_type = load_gitlab_config().get("type")
    if platform_type != "gitlab":
        # 巡检的仓库来源与组织展开只接了 GitLab，其他平台只做了链接展示
        return f"修复目前只支持 GitLab，当前平台为 {platform_type}"
    return ""


def _execute(run_uid: str, fix: dict) -> None:
    """修复的主流程：拉取 → 生成与验证 → 追溯处理人 → 推送分支并提出合并请求。失败以异常表达。"""
    unsupported = fix_unsupported_reason()
    if unsupported:
        raise FixFailure(FIX_UNSUPPORTED, unsupported)
    platform = load_gitlab_config()
    survey = repo.get_survey(fix["survey_uid"])
    if survey is None:
        raise FixFailure(FIX_UNSUPPORTED, "所属巡检已被删除")
    budget = resolve_budget(survey)
    project_path = project_path_from_url(fix["repo_url"], platform.get("url", ""))
    if not project_path:
        raise FixFailure(FIX_UNSUPPORTED, f"无法从仓库地址解析出项目路径：{fix['repo_url']}")

    repo_dir, branch, sha = prepare_fix_repo(
        run_uid, fix["repo_url"], fix["target_branch"], timeout=budget.get("fetch_timeout_seconds", 600)
    )
    repo.update_survey_fix(run_uid, target_branch=branch, base_sha=sha)
    source, crlf = _read_source(repo_dir, fix["file_path"], budget["l2_max_chars_per_focus"])

    # 修复提示词在执行时读取而不是登记时快照：批量修复排队期间 Admin 改了提示词，排在后面的应当按新的来
    new_source, summary, attempts = _generate_verified(run_uid, fix, source, survey["fix_prompt"])
    _write_source(repo_dir, fix["file_path"], new_source, crlf)
    repo.update_survey_fix(
        run_uid, phase=FIX_PHASE_ASSIGNING, summary=summary, verification=attempts,
        diff=unified_diff(fix["file_path"], source, new_source),
    )

    try:
        author = find_original_author(repo_dir, fix["file_path"], changed_lines(source, new_source))
    except SurveyError as e:
        # 追溯失败只影响指派给谁，修改本身已经通过验证，退回配置的仓库负责人（没有配置时不指派）
        logger.warning("Blame failed, falling back to configured owner or none: run=%s: %s", run_uid, e)
        author = None
    assignee = resolve_assignee(project_path, author)
    repo.update_survey_fix(
        run_uid,
        author_name=(author or {}).get("name") or None,
        author_email=(author or {}).get("email") or None,
        assignee_source=assignee["source"] or None,
        assignee_username=assignee["username"] or None,
        assignee_name=assignee["name"] or None,
        assignee_note=assignee["note"] or None,
    )

    if not repo.update_survey_fix(run_uid, phase=FIX_PHASE_SUBMITTING):
        # 已被确认中断：推分支、提合并请求是对外可见的动作，记录关掉之后不能再做
        raise FixFailure(ERROR_INTERRUPTED, "修复运行已被确认中断，未提出合并请求")
    source_branch = f"{FIX_BRANCH_PREFIX}{run_uid[:8]}"
    title = str(fix.get("title") or "修复巡检发现的问题").strip()
    commit_author = resolve_commit_author()
    logger.info("Survey fix commit author: run=%s author=%s <%s>", run_uid, commit_author.name, commit_author.email)
    commit_sha = commit_and_push_fix(
        repo_dir, fix["file_path"], source_branch,
        f"fix: {title}\n\nGenerated by OpenCR survey fix {run_uid}.",
        commit_author, timeout=budget.get("fetch_timeout_seconds", 600),
    )
    repo.update_survey_fix(run_uid, source_branch=source_branch, commit_sha=commit_sha)
    mr = create_merge_request(
        project_path, source_branch, branch,
        # 标题模板与修复提示词一样在执行时读取：排队期间改了模板，之后提出的合并请求按新的来
        mr_title(load_fix_mr_title_prefix(), title, survey["fix_mr_title_template"]),
        _mr_description(fix, survey.get("name") or "", summary, attempts, assignee),
        assignee_id=assignee["id"],
    )
    repo.update_survey_fix(run_uid, mr_iid=mr.get("iid"), mr_url=str(mr.get("web_url") or ""))
    logger.info(
        "Survey fix MR opened: run=%s mr=%s assignee=%s(%s)",
        run_uid, mr.get("web_url"), assignee["username"], assignee["source"],
    )


def republish_ledger(survey_id: int) -> None:
    """
    以最近一次成功运行的名义重推台账，把合并请求链接的变化（新提出的、已清理的）带到外部表格。
    失败只记日志：链接下次推送时照样会更新。

    某个输出目标正在推送时（例如两个修复先后几秒内完成），那次推送可能是在新链接入库之前读的台账，
    所以等它结束后再补推一次，而不是直接放弃。
    """
    run_uid = repo.latest_succeeded_survey_run_uid(survey_id)
    if not run_uid:
        return
    pending: Optional[List[int]] = None
    for attempt in range(_REPUBLISH_BUSY_RETRIES + 1):
        busy: List[int] = []
        # 第一次推全部输出目标；之后只补推上一次正忙的那几个，已经推过的不重复推
        for binding_id in pending if pending is not None else [None]:
            try:
                started = begin_pushes(run_uid, PUSH_TRIGGER_AUTO, binding_id)
            except PushRejected as e:
                # 没配输出目标、绑定已被删除、或等待期间有了更新的成功运行（它自己的推送会带上链接）：
                # 这里没有需要补的，跳过即可
                logger.info("No push after fix: survey=%s binding=%s: %s", survey_id, binding_id, e)
                continue
            except Exception:
                logger.exception("Registering pushes after fix failed: survey=%s", survey_id)
                return
            execute_pushes(started["started"])
            busy.extend(started["busy"])
        if not busy:
            return
        pending = busy
        if attempt < _REPUBLISH_BUSY_RETRIES:
            time.sleep(_REPUBLISH_BUSY_WAIT_SECONDS)
    logger.warning(
        "Push after fix still busy, links will appear on the next push: survey=%s bindings=%s",
        survey_id, pending,
    )


def execute_survey_fix_in_background(run_uid: str) -> None:
    """把修复丢到后台线程：克隆、两三次模型调用与推送加起来要几分钟，不能占住 HTTP 请求。"""

    def _target():
        """执行体。execute_survey_fix 内部已收敛异常，这里只兜最后一层。"""
        try:
            execute_survey_fix(run_uid)
        except Exception:
            logger.exception("Survey fix thread crashed: %s", run_uid)

    threading.Thread(target=_target, name="opencr-survey-fix", daemon=True).start()


class FixBatchBusy(Exception):
    """同一个巡检已有一批修复正在进行。"""


def fix_batch_lease_name(survey_id: int) -> str:
    """一个巡检的批量修复租约名。"""
    return f"survey-fix-batch:{int(survey_id)}"


def start_fix_batch(run_uid: str, trigger: str) -> List[int]:
    """
    为一次运行里尚未修复过的问题批量发起修复（一键全部修复、巡检后自动发起），返回本批要处理的发现 id。

    只处理不低于该巡检 fix_min_severity 的问题，每批最多 MAX_FIXES_PER_BATCH 个，其余留给下一批。
    在后台线程里**逐个**执行而不是并行：每个修复都要克隆仓库、调几次模型、推一次分支，
    并行几十个会同时撞上模型与平台的限流，失败了还分不清是谁的问题。
    同一个巡检同时只跑一批（按租约，跨进程有效），已有一批在跑时抛 FixBatchBusy：
    批次里的修复轮到时才登记，重复点击或上一轮自动发起的那批还没跑完时，候选看起来一个都没少，不挡的话就成了两批并行。
    """
    context = repo.get_survey_push_context(run_uid)
    if context is None:
        return []
    candidates = repo.list_survey_fix_candidates(run_uid, context["survey"].get("fix_min_severity") or "")
    batch = candidates[:MAX_FIXES_PER_BATCH]
    if not batch:
        return []
    holder = str(uuid.uuid4())
    lease = fix_batch_lease_name(context["survey_id"])
    if not repo.try_acquire_lease(lease, holder, FIX_BATCH_LEASE_SECONDS):
        raise FixBatchBusy("这个巡检已有一批修复正在进行，结束后再试")
    threading.Thread(
        target=_run_batch, args=(context["survey_id"], batch, trigger, holder),
        name="opencr-survey-fix-batch", daemon=True,
    ).start()
    return batch


def _run_batch(survey_id: int, finding_ids: List[int], trigger: str, holder: str = "") -> None:
    """
    逐个登记并执行一批修复，全部结束后重推一次台账。

    每个都在轮到时才登记，而不是一开始全部登记：登记即进入"进行中"，排在后面的会因为心跳一直不动被当成疑似中断。
    登记时只给从没修复过的问题发起（only_new）：另一批或人工单条可能已经先处理了它。
    进程在批次中途退出时，剩下的不会被执行；再点一次一键全部修复（或等下一轮自动发起）即可接着处理，已处理的不会重复。
    """
    lease = fix_batch_lease_name(survey_id)
    try:
        opened = _run_batch_items(lease, holder, finding_ids, trigger)
    finally:
        if holder:
            repo.release_lease(lease, holder)
    logger.info("Survey fix batch finished: survey=%s trigger=%s opened=%s/%s", survey_id, trigger, opened, len(finding_ids))
    if opened:
        republish_ledger(survey_id)


def _run_batch_items(lease: str, holder: str, finding_ids: List[int], trigger: str) -> int:
    """逐个登记并执行，返回提出了合并请求的个数。每轮开始前续租，持有不住（被别人接手）就停下。"""
    opened = 0
    for finding_id in finding_ids:
        if holder and not repo.try_acquire_lease(lease, holder, FIX_BATCH_LEASE_SECONDS):
            # 上一个修复跑得比租约还久，租约过期后被另一批接手了；剩下的交给它，免得两批并行
            logger.warning("Fix batch lease lost, stopping: lease=%s", lease)
            break
        try:
            outcome, run_uid = repo.start_survey_fix(finding_id, trigger=trigger, only_new=True)
        except Exception:
            # 登记失败只影响这一条，其余照常处理
            logger.exception("Registering batch fix failed: finding=%s", finding_id)
            continue
        if outcome != repo.FIX_START_OK:
            logger.info("Batch fix skipped: finding=%s outcome=%s", finding_id, outcome)
            continue
        if execute_survey_fix(run_uid, republish=False):
            opened += 1
    return opened
