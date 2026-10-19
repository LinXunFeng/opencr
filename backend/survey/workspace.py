#!/usr/bin/env python3
"""
巡检工作区：把仓库拉取到本地并重置到最新。

Workspace 是执行的副产物而不是数据 —— 删掉它不影响任何已产出的 Finding，
只是让下一次执行退回全量 clone。
"""

import base64
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from ..review.config import load_gitlab_config
from .common import CODEGRAPH_INDEX_DIRNAME, SurveyError, repo_slug_from_url, slugify
from .config import load_survey_config

logger = logging.getLogger(__name__)


def workspace_root() -> Path:
    """工作区根目录。"""
    return Path(load_survey_config()["workspace_dir"]).expanduser()


def survey_workspace_dir(survey_slug: str) -> Path:
    """单个 Survey 的工作区目录。slug 已在入库时净化过，这里再兜一次底。"""
    safe = slugify(survey_slug, fallback="survey")
    if not safe:
        raise SurveyError("巡检 slug 非法，无法定位工作区")
    return workspace_root() / safe


def artifacts_dir(survey_slug: str) -> Path:
    """
    L0 画像产物目录。

    必须在**仓库工作区之外** —— 拉取前会对每个仓库跑 `git clean -fdx`，
    放在仓库里的产物每轮都会被删掉。唯一的例外是 codegraph 的索引目录，
    它只能待在仓库内部，因此由 `clean -e` 单独放行。
    """
    return survey_workspace_dir(survey_slug) / ".artifacts"


def _git_env(token: str) -> Dict[str, str]:
    """
    构造 git 子进程环境。

    凭据通过 GIT_CONFIG_KEY_*/VALUE_* 传，不写进 remote URL、也不放进命令行参数：
    - 写进 remote URL 会把 token 落到 .git/config 里，长期留在磁盘上；
    - 放进 `git -c` 的命令行参数会让它出现在 ps 的输出里，同机器上任何用户都能看到。
    环境变量不进 argv，且 /proc/<pid>/environ 只有属主可读，是三者里最不糟的。
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        # 认证失败时必须立刻报错。留着交互提示会让子进程挂在那里等输入，
        # 而巡检是无人值守的，表现出来就是"这个仓库永远卡在拉取中"。
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
    }
    entries: List[Tuple[str, str]] = [
        # 禁用系统credential helper：既不读用户的凭据，也不把本次的凭据存进去
        ("credential.helper", ""),
    ]
    if token:
        basic = base64.b64encode(f"oauth2:{token}".encode("utf-8")).decode("ascii")
        entries.append(("http.extraHeader", f"Authorization: Basic {basic}"))

    env["GIT_CONFIG_COUNT"] = str(len(entries))
    for i, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def _run_git(
    args: List[str], cwd: Optional[Path], token: str, timeout: int, extra_env: Optional[Dict[str, str]] = None
) -> subprocess.CompletedProcess:
    """执行一条 git 命令；失败时抛 SurveyError，错误信息里剥掉可能的凭据。"""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env={**_git_env(token), **(extra_env or {})},
            capture_output=True,
            text=True,
            timeout=max(int(timeout), 1),
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise SurveyError(f"git {args[0]} 超时（{timeout}s）") from e
    except FileNotFoundError as e:
        raise SurveyError("找不到 git 命令，请确认已安装并在 PATH 中") from e

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if token:
            detail = detail.replace(token, "***")
        raise SurveyError(f"git {args[0]} 失败（exit={completed.returncode}）：{detail[:500]}")
    return completed


def _resolve_default_branch(repo_dir: Path, token: str, timeout: int) -> str:
    """
    取远端默认分支。

    不硬写 main —— 老仓库很多还是 master，写死会让它们每轮都拉取失败。
    """
    try:
        out = _run_git(
            ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo_dir, token, timeout
        ).stdout.strip()
        if out.startswith("origin/"):
            return out[len("origin/"):]
    except SurveyError:
        # 浅克隆下这个 ref 可能不存在，退回问远端
        pass

    out = _run_git(["remote", "show", "origin"], repo_dir, token, timeout).stdout
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("HEAD branch:"):
            branch = line.split(":", 1)[1].strip()
            if branch and branch != "(unknown)":
                return branch
    raise SurveyError("无法确定远端默认分支")


def prepare_repo(
    survey_slug: str,
    url: str,
    branch: str = "",
    timeout: int = 600,
) -> Tuple[Path, str, str]:
    """
    把一个仓库准备到"最新内容"状态，返回 (工作区路径, 实际分支, commit sha)。

    已存在则重置本地改动后增量拉取，不存在则浅克隆。
    用 --depth 1 是因为巡检分析的是**当前快照**，历史一行都用不到，
    而全历史 clone 在大仓库上是几十倍的时间与磁盘。
    """
    token = load_gitlab_config().get("token", "")
    repo_dir = survey_workspace_dir(survey_slug) / repo_slug_from_url(url)
    git_dir = repo_dir / ".git"

    if not git_dir.is_dir():
        if repo_dir.exists():
            # 上一轮中途失败可能留下半个目录；重来一次比猜它坏在哪里可靠
            logger.warning("Workspace dir exists without .git, recreating: %s", repo_dir)
            shutil.rmtree(repo_dir, ignore_errors=True)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--depth", "1"]
        if branch:
            args += ["--branch", branch]
        args += [url, str(repo_dir)]
        _run_git(args, repo_dir.parent, token, timeout)
    else:
        # 远端地址可能被改过（换域名、换协议），每轮对齐一次比让它静默拉旧地址好
        _run_git(["remote", "set-url", "origin", url], repo_dir, token, timeout)

    target_branch = branch or _resolve_default_branch(repo_dir, token, timeout)

    # 先丢弃本地改动再拉取。顺序不能反：clean 必须在 reset 之后，
    # 才能清掉 reset 过程中新产生的未跟踪文件。
    #
    # `-e .codegraph` 是为了保住代码索引：它只能落在仓库内部（codegraph 不支持
    # 把索引放到别处，绝对路径会被静默忽略），不排除的话每轮拉取都会把它删掉，
    # 下一轮只能全量重建。保住它之后可以走 `codegraph sync` 增量更新。
    _run_git(["fetch", "--depth", "1", "origin", target_branch], repo_dir, token, timeout)
    _run_git(["reset", "--hard", f"origin/{target_branch}"], repo_dir, token, timeout)
    _run_git(["clean", "-fdx", "-e", CODEGRAPH_INDEX_DIRNAME], repo_dir, token, timeout)

    sha = _run_git(["rev-parse", "HEAD"], repo_dir, token, timeout).stdout.strip()
    logger.info(
        "Repo ready: survey=%s, repo=%s, branch=%s, sha=%s",
        survey_slug, repo_dir.name, target_branch, sha[:12],
    )
    return repo_dir, target_branch, sha


def count_files(repo_dir: Path) -> int:
    """统计工作区内被 git 跟踪的文件数，用于展示与体量判断。"""
    try:
        out = _run_git(["ls-files"], repo_dir, "", 60).stdout
        return sum(1 for line in out.splitlines() if line.strip())
    except SurveyError:
        return 0


def delete_workspace(survey_slug: str) -> bool:
    """
    删除一个 Survey 的整个工作区。返回是否真的删掉了东西。

    只在用户明确要求（配置项或界面动作）时调用 —— 误删一个 Survey 顺手把
    几十 GB 代码删掉是不可逆的，所以删除 Survey 记录时默认**不**连带删目录。
    """
    target = survey_workspace_dir(survey_slug)
    if not target.exists():
        return False
    shutil.rmtree(target, ignore_errors=True)
    logger.info("Workspace removed: %s", target)
    return not target.exists()


def workspace_size_bytes(survey_slug: str) -> int:
    """工作区占用的磁盘字节数；目录不存在返回 0。"""
    target = survey_workspace_dir(survey_slug)
    if not target.exists():
        return 0
    total = 0
    for path in target.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


# ---------------------------------------------------------------------------
# 修复运行（FixRun）的工作区
# ---------------------------------------------------------------------------

class CommitAuthor(NamedTuple):
    """修复提交的署名，同时用作作者与提交者。"""

    name: str
    email: str


# 修复工作区放在工作区根目录下的这个子目录里。以点开头：Survey 的 slug 只含 [a-z0-9-]，不会撞名
FIX_WORKSPACE_DIRNAME = ".fixes"
# 仓库自带的格式化提交清单（git blame 的通行约定），里面的提交在追溯原始作者时一并跳过
BLAME_IGNORE_REVS_FILE = ".git-blame-ignore-revs"
# 最多认多少条 .git-blame-ignore-revs。每条都要一次 rev-parse，几百条已经覆盖绝大多数仓库，
# 再多的清单通常是脚本生成的、混着大量别的分支上的提交
_MAX_IGNORE_REVS = 200


def fix_workspace_dir(run_uid: str) -> Path:
    """
    单次修复运行的工作区目录。

    不复用 Survey 的工作区：那里的仓库是浅克隆、每轮都会被 reset 与 clean，
    巡检与修复同时进行时，一边的改动会被另一边清掉；追溯原始作者也需要历史，浅克隆里没有。
    """
    safe = slugify(run_uid, fallback="fix")
    return workspace_root() / FIX_WORKSPACE_DIRNAME / safe


def prepare_fix_repo(run_uid: str, url: str, branch: str = "", timeout: int = 600) -> Tuple[Path, str, str]:
    """
    为一次修复克隆仓库，返回 (工作区路径, 实际分支, commit sha)。

    用 --filter=blob:none 而不是浅克隆：追溯原始作者要走提交历史，浅克隆只有一个提交；
    全量克隆又要把所有历史文件内容都拉下来。无文件内容的部分克隆只拉提交与目录树，
    blame 用到哪个文件的历史版本才按需取回。
    """
    token = load_gitlab_config().get("token", "")
    repo_dir = fix_workspace_dir(run_uid)
    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    args = ["clone", "--filter=blob:none", "--single-branch", "--no-tags"]
    if branch:
        args += ["--branch", branch]
    args += [url, str(repo_dir)]
    _run_git(args, repo_dir.parent, token, timeout)

    actual_branch = branch or _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], repo_dir, token, timeout
    ).stdout.strip()
    sha = _run_git(["rev-parse", "HEAD"], repo_dir, token, timeout).stdout.strip()
    return repo_dir, actual_branch, sha


def prepare_history_repo(key: str, url: str, branch: str = "", commit_sha: str = "", timeout: int = 600) -> Path:
    """
    为给台账行追溯原始作者克隆一份带历史的仓库，返回工作区路径；用完由调用方 delete_fix_workspace(key) 删除。

    与修复用同一种部分克隆、同一个目录（key 与修复的 run_uid 不会撞上）：Survey 的工作区是浅克隆，blame 不出作者。
    commit_sha 是那一轮分析的提交，检出到它，台账里的行号才对得上。检出不了（分支在这期间被强推、本地没有那个提交）时抛 SurveyError，
    不退回分支最新的提交：行号整体错位时 blame 不会报错，而是安静地归给别人，追溯结果又只写一次，错的人会一直留在台账上。
    """
    repo_dir, _branch, head = prepare_fix_repo(key, url, branch, timeout)
    if commit_sha and commit_sha != head:
        _run_git(["checkout", "-q", "--detach", commit_sha], repo_dir, load_gitlab_config().get("token", ""), timeout)
    return repo_dir


def read_blame_ignore_revs(repo_dir: Path, timeout: int = 60) -> List[str]:
    """
    读取仓库自带的 .git-blame-ignore-revs，只返回本地能解析成提交的条目。

    不直接把文件交给 --ignore-revs-file：里面常有别的分支上的提交，单分支克隆里解析不到，
    git 会因为一条无效条目让整个 blame 失败。
    """
    path = repo_dir / BLAME_IGNORE_REVS_FILE
    # 符号链接可能指向仓库外的任意文件，不跟随
    if path.is_symlink() or not path.is_file():
        return []
    token = load_gitlab_config().get("token", "")
    revs: List[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        candidate = raw.split("#", 1)[0].strip()
        if not candidate:
            continue
        try:
            resolved = _run_git(
                ["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], repo_dir, token, timeout
            ).stdout.strip()
        except SurveyError:
            # 解析不到（多半是别的分支上的提交）就跳过，代价是这个格式化提交若出现在本分支历史里，仍要靠逐行判断认出它
            continue
        if resolved:
            revs.append(resolved)
        if len(revs) >= _MAX_IGNORE_REVS:
            break
    return revs


def parse_blame_porcelain(text: str) -> List[dict]:
    """
    解析 `git blame --line-porcelain` 的输出，每行一项：
    {"sha", "line", "orig_line", "author_name", "author_email", "author_time", "filename", "previous_sha",
    "previous_filename"}。

    line 是这一行在被 blame 的版本里的行号，orig_line 是它在归到的那个提交（sha）的 filename 里的行号；
    previous_* 是这一行在该提交之前所在的提交与文件名，文件在该提交里首次出现时为空串。
    """
    entries: List[dict] = []
    current: Optional[dict] = None
    for raw in text.splitlines():
        if raw.startswith("\t"):
            if current is not None:
                entries.append(current)
            current = None
            continue
        parts = raw.split(" ")
        if current is None:
            if len(parts) >= 3 and len(parts[0]) >= 40:
                current = {
                    "sha": parts[0],
                    "line": int(parts[2]) if parts[2].isdigit() else 0,
                    "orig_line": int(parts[1]) if parts[1].isdigit() else 0,
                    "author_name": "",
                    "author_email": "",
                    "author_time": 0,
                    "filename": "",
                    "previous_sha": "",
                    "previous_filename": "",
                }
            continue
        key, _, value = raw.partition(" ")
        if key == "author":
            current["author_name"] = value.strip()
        elif key == "author-mail":
            current["author_email"] = value.strip().strip("<>").strip()
        elif key == "author-time":
            current["author_time"] = int(value) if value.strip().isdigit() else 0
        elif key == "filename":
            current["filename"] = value
        elif key == "previous":
            prev_sha, _, prev_name = value.partition(" ")
            current["previous_sha"] = prev_sha
            current["previous_filename"] = prev_name
    return entries


def git_blame(
    repo_dir: Path, file_path: str, ranges: List[Tuple[int, int]], ignore_revs: List[str], timeout: int = 300
) -> List[dict]:
    """
    对文件的若干行区间做 blame，返回 parse_blame_porcelain 的结果。

    -w 忽略只改空白的提交；-M 跟随文件内的代码移动 —— 把一段代码挪了个位置的人不是写它的人。
    不加跨文件的 -C：它要把每个提交里其他被改动的文件也读一遍，部分克隆下每读一个历史版本就是一次网络请求，
    历史长的仓库会拖到超时，结果是几乎总退回仓库负责人。代价是从别的文件整段搬来的代码会归给搬运的人。
    """
    if not ranges:
        return []
    token = load_gitlab_config().get("token", "")
    args = ["blame", "--line-porcelain", "-w", "-M"]
    for rev in ignore_revs:
        args += ["--ignore-rev", rev]
    for start, end in ranges:
        args += ["-L", f"{int(start)},{int(end)}"]
    args += ["HEAD", "--", file_path]
    return parse_blame_porcelain(_run_git(args, repo_dir, token, timeout).stdout)


def git_show_file(repo_dir: Path, rev: str, file_path: str, timeout: int = 120) -> Optional[str]:
    """读取某个提交里的文件内容；文件在那个提交里不存在时返回 None。"""
    token = load_gitlab_config().get("token", "")
    try:
        return _run_git(["show", f"{rev}:{file_path}"], repo_dir, token, timeout).stdout
    except SurveyError:
        return None


def commit_and_push_fix(
    repo_dir: Path, file_path: str, source_branch: str, message: str, author: CommitAuthor, timeout: int = 600
) -> str:
    """
    把对单个文件的修改提交到新分支并推送，返回新提交的 sha。

    只 add 这一个文件：修复只允许改发现所在的文件，工作区里别的变化（例如 blame 时生成的东西）一概不进提交。
    """
    token = load_gitlab_config().get("token", "")
    # 走环境变量而不是 git config：优先级高于机器上的 ~/.gitconfig，部署环境里配没配 user.name 都不影响
    identity = {
        "GIT_AUTHOR_NAME": author.name,
        "GIT_AUTHOR_EMAIL": author.email,
        "GIT_COMMITTER_NAME": author.name,
        "GIT_COMMITTER_EMAIL": author.email,
    }
    _run_git(["checkout", "-b", source_branch], repo_dir, token, timeout)
    _run_git(["add", "--", file_path], repo_dir, token, timeout)
    _run_git(["commit", "--no-verify", "-m", message], repo_dir, token, timeout, extra_env=identity)
    _run_git(["push", "origin", f"HEAD:refs/heads/{source_branch}"], repo_dir, token, timeout)
    return _run_git(["rev-parse", "HEAD"], repo_dir, token, timeout).stdout.strip()


def delete_fix_workspace(run_uid: str) -> None:
    """删除一次修复运行的工作区。修复工作区不复用，做完即删，失败只记日志。"""
    target = fix_workspace_dir(run_uid)
    if not target.exists():
        return
    shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        logger.warning("Fix workspace not fully removed: %s", target)
