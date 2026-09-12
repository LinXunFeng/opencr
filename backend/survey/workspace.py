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
from typing import Dict, List, Optional, Tuple

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


def _run_git(args: List[str], cwd: Optional[Path], token: str, timeout: int) -> subprocess.CompletedProcess:
    """执行一条 git 命令；失败时抛 SurveyError，错误信息里剥掉可能的凭据。"""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env=_git_env(token),
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
