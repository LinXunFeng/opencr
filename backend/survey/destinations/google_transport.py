#!/usr/bin/env python3
"""
Google Sheets 的请求传输层：同一组 Sheets API 调用，按鉴权方式用不同的途径发出去。

- ServiceAccountTransport：google-auth 服务账号 + 直接调 REST；
- GogcliTransport：调用 gogcli 的 `gog api call` 透传，令牌的存储与刷新完全交给 gogcli。

插件（google_sheet.py）只按 Discovery 方法 ID 描述"要调哪个接口、带什么参数"，
推送、检查、表头定位这些逻辑因此只有一份。为什么 gogcli 方式要走外部命令而不是自管令牌，
见 docs/adr/0005-google-sheet-via-gogcli.md。
"""

import getpass
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from .base import CHECK_ERROR, CHECK_OK, CHECK_WARN, CheckItem, DestinationError

logger = logging.getLogger(__name__)

API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
SPREADSHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# 写入配额是每分钟 60 次（按项目 + 用户），推送一个几百行的台账只需几次请求，
# 撞上 429 基本是多个 Survey 同时推送。退避上限 32 秒、最多 5 次，足够等过一个配额窗口。
MAX_ATTEMPTS = 5
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
REQUEST_TIMEOUT_SECONDS = 60

# 连通性测试是用户在表单里点按钮同步等结果，不能套用推送的退避策略（最坏要等一分多钟）。
# 只重试一次、超时缩短：测试的职责是尽快告诉用户哪里不对，偶发的限流让他再点一次即可。
CHECK_ATTEMPTS = 2
CHECK_TIMEOUT_SECONDS = 15

# Discovery 方法 ID → (HTTP 方法, 路径模板)。路径参数从 params 里取出，其余作为查询参数。
_REST_ROUTES = {
    "spreadsheets.get": ("GET", "{spreadsheetId}"),
    "spreadsheets.batchUpdate": ("POST", "{spreadsheetId}:batchUpdate"),
    "spreadsheets.values.get": ("GET", "{spreadsheetId}/values/{range}"),
    "spreadsheets.values.batchUpdate": ("POST", "{spreadsheetId}/values:batchUpdate"),
    "spreadsheets.values.append": ("POST", "{spreadsheetId}/values/{range}:append"),
}
_PATH_PARAMS = ("spreadsheetId", "range")
_WRITE_METHODS = {"spreadsheets.batchUpdate", "spreadsheets.values.batchUpdate", "spreadsheets.values.append"}


class AuthRejected(DestinationError):
    """
    身份本身不可用：凭据被 Google 拒绝、gogcli 里的授权失效或缺失。

    与网络故障、表格权限分开：重试没有意义，排查方向也完全不同，连通性测试把它归到身份那一项。
    """


class SheetsTransport(ABC):
    """一种鉴权方式下发出 Sheets API 请求的途径。"""

    #: 连通性测试里"身份"那一项的标题
    auth_title: str = ""

    @abstractmethod
    def principal(self) -> str:
        """实际写表的身份（邮箱），用于提示用户把表格共享给谁。未知时返回空串。"""

    @abstractmethod
    def preflight(self) -> List[CheckItem]:
        """
        连通性测试的前置检查项（身份相关）。最后一项为 error 时，调用方不再继续。

        只做本地能判断的事：凭据能不能加载、账号在不在。凭据是否真的有效，
        要等第一个真实请求才知道（届时抛 AuthRejected）。
        """

    @abstractmethod
    def call(self, method: str, params: Dict[str, Any], body: Optional[dict] = None, quick: bool = False) -> dict:
        """
        调一个 Sheets API 方法，返回响应 JSON。

        quick=True 用于连通性测试：少重试、短超时。错误一律转成可读的 DestinationError。
        """

    def describe_status(self, status: int, message: str) -> str:
        """把 HTTP 语义的错误翻译成能指导排查的一句话。两种传输共用同一套措辞。"""
        who = self.principal() or "写表的身份"
        if status == 403:
            return f"没有权限写入该表格，请确认已把表格以「编辑者」身份共享给{who}（{message}）"
        if status == 404:
            return f"表格不存在，或{who}无权访问（{message}）"
        return f"Google Sheets 返回 {status}：{message}"


# ---------------------------------------------------------------------------
# 服务账号
# ---------------------------------------------------------------------------

def _is_credentials_rejection(error: Exception) -> bool:
    """是否为换取访问令牌时被拒（google-auth 的 RefreshError）。按类名判断，避免未安装 google-auth 时导入失败。"""
    return any(cls.__name__ == "RefreshError" for cls in type(error).__mro__)


def _missing_credentials_message(raw: str, path: Path) -> str:
    """
    密钥文件找不到时的报错。

    "路径明明是对的"几乎总是下面两种情况之一，报错里直接点出来，省得用户去翻代码：
    - 服务运行在容器里，宿主机路径在容器内不存在；
    - 服务没装 PyYAML，简化解析器不会去掉行内注释，读到的值里带着引号或 # 注释。
    """
    try:
        user = getpass.getuser()
    except Exception:
        user = "未知"
    message = f"服务账号密钥文件不存在：{path}（配置原始值 {raw!r}，服务运行用户 {user}）"
    hints = []
    if any(mark in raw for mark in ('"', "'", "#")):
        hints.append("配置值里带有引号或 # 注释，请删掉这一行的行内注释")
    if Path("/.dockerenv").exists():
        hints.append("服务运行在容器内，需要把宿主机上的密钥文件挂载进容器，并在这里填写容器内的路径")
    if hints:
        message += "。" + "；".join(hints)
    return message


class ServiceAccountTransport(SheetsTransport):
    """
    google-auth 服务账号 + 直接调 Sheets REST API。

    不引 gspread / google-api-python-client：前者在批量更新时仍要自己拼范围，
    后者体积大、依赖多，而这里总共只用到五个接口。
    """

    auth_title = "服务账号凭据"

    def __init__(self, credentials_file: str, session: Any = None, sleep: Callable[[float], None] = time.sleep):
        """session 与 sleep 仅供测试注入；生产环境按 credentials_file 惰性创建会话。"""
        self.credentials_file = credentials_file
        self._session = session
        self._sleep = sleep
        self.client_email = ""

    def principal(self) -> str:
        """服务账号邮箱。"""
        return f"服务账号 {self.client_email}" if self.client_email else "服务账号"

    def _get_session(self) -> Any:
        """惰性创建带服务账号凭据的会话。"""
        if self._session is not None:
            return self._session
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
        except ImportError:
            raise DestinationError("缺少依赖 google-auth，请执行 pip install -r requirements.txt")

        raw = str(self.credentials_file or "")
        path = Path(raw).expanduser()
        if not path.is_file():
            raise DestinationError(_missing_credentials_message(raw, path))
        try:
            credentials = service_account.Credentials.from_service_account_file(
                str(path), scopes=[SPREADSHEETS_SCOPE]
            )
        except (ValueError, OSError) as e:
            raise DestinationError(f"服务账号密钥文件无法解析：{e}")
        self.client_email = getattr(credentials, "service_account_email", "") or ""
        self._session = AuthorizedSession(credentials)
        return self._session

    def preflight(self) -> List[CheckItem]:
        """密钥文件能否加载。"""
        try:
            self._get_session()
        except DestinationError as e:
            return [CheckItem(self.auth_title, CHECK_ERROR, str(e))]
        who = self.client_email or "服务账号"
        return [CheckItem(self.auth_title, CHECK_OK, f"已通过 {who} 访问 Google，表格需要以「编辑者」身份共享给它")]

    def call(self, method: str, params: Dict[str, Any], body: Optional[dict] = None, quick: bool = False) -> dict:
        """发一次请求；429 与 5xx 退避重试。"""
        http_method, template = _REST_ROUTES[method]
        path = template.format(**{k: quote(str(params[k]), safe="") for k in _PATH_PARAMS if k in params})
        query = {k: v for k, v in params.items() if k not in _PATH_PARAMS} or None
        url = f"{API_BASE}/{path}"
        attempts = CHECK_ATTEMPTS if quick else MAX_ATTEMPTS
        timeout = CHECK_TIMEOUT_SECONDS if quick else REQUEST_TIMEOUT_SECONDS

        session = self._get_session()
        for attempt in range(attempts):
            try:
                response = session.request(http_method, url, params=query, json=body, timeout=timeout)
            except Exception as e:
                if _is_credentials_rejection(e):
                    raise AuthRejected(
                        f"Google 拒绝了服务账号凭据（{e}）。常见原因：密钥已被删除或服务账号已停用、"
                        "密钥文件不是这个服务账号的最新密钥、服务器时钟偏差过大"
                    )
                # 网络抖动与 5xx 同等对待：本次推送失败的代价是整张台账晚一周更新
                if attempt < attempts - 1:
                    self._sleep(min(2 ** attempt, 32))
                    continue
                raise DestinationError(f"无法连接 Google Sheets：{e}")

            if response.status_code in RETRYABLE_STATUS and attempt < attempts - 1:
                delay = min(2 ** attempt, 32)
                retry_after = response.headers.get("Retry-After") if response.headers else None
                if retry_after and str(retry_after).isdigit():
                    delay = min(int(retry_after), 60)
                logger.info("Google Sheets %s, retrying in %ss", response.status_code, delay)
                self._sleep(delay)
                continue

            if response.status_code >= 400:
                try:
                    message = response.json().get("error", {}).get("message", "")
                except Exception:
                    message = (getattr(response, "text", "") or "")[:300]
                raise DestinationError(self.describe_status(response.status_code, message))
            if not response.content:
                return {}
            return response.json()
        raise DestinationError("Google Sheets 请求多次重试后仍失败")


# ---------------------------------------------------------------------------
# gogcli
# ---------------------------------------------------------------------------

# gogcli 的稳定退出码（internal/cmd/exit_codes.go）。这是我们自己维护的映射，gogcli 升级时需要复核。
GOG_EXIT_USAGE = 2
GOG_EXIT_AUTH_REQUIRED = 4
GOG_EXIT_NOT_FOUND = 5
GOG_EXIT_PERMISSION_DENIED = 6
GOG_EXIT_RATE_LIMITED = 7
GOG_EXIT_RETRYABLE = 8
GOG_EXIT_CONFIG = 10

# gogcli 自己已经对 429 重试 3 次、5xx 重试 1 次；这里只在它放弃之后再补两次，
# 间隔拉长到能跨过一个配额窗口的量级，而不是和它的秒级退避叠在一起。
GOGCLI_EXTRA_ATTEMPTS = 2
GOGCLI_RETRY_DELAYS = (15, 45)

# 单次调用的超时。gogcli 内部对 429 最多等 3 次、每次至多 60 秒，推送时要给足；
# 测试时放到 45 秒而不是更短：macOS 钥匙串等待授权弹窗本身就有 30 秒超时，
# 砍得比它短会让用户看到"超时"而不是 gogcli 给出的"去点始终允许"。
GOGCLI_TIMEOUT_SECONDS = 240
GOGCLI_CHECK_TIMEOUT_SECONDS = 45

# 文件存储的令牌锁默认只等 5 秒。推送与连通性测试可能在不同 worker 里同时刷新令牌，
# 5 秒不够时 gogcli 会以"可重试错误"退出，看起来像网络问题。
GOGCLI_LOCK_TIMEOUT = "30s"

# 进程内串行执行所有 gogcli 调用：每次调用都可能刷新令牌并写回存储，
# 并发只会放大令牌锁的争用，而推送本来就是串行的，不损失吞吐。
_gogcli_lock = threading.Lock()


class GogcliTransport(SheetsTransport):
    """调用 gogcli 的 `gog api call sheets v4 <方法>` 透传 Sheets API。"""

    auth_title = "gogcli 账号授权"

    def __init__(
        self,
        account: str,
        binary: str = "gog",
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ):
        """runner 与 sleep 仅供测试注入。"""
        self.account = account
        self.binary = binary or "gog"
        self._run = runner
        self._sleep = sleep

    def principal(self) -> str:
        """gogcli 里授权的账号。"""
        return f"账号 {self.account}"

    def _env(self) -> Dict[str, str]:
        """子进程环境：继承服务的环境（GOG_HOME、GOG_KEYRING_* 都从这里来），只补上锁等待时长。"""
        env = dict(os.environ)
        env.setdefault("GOG_KEYRING_LOCK_TIMEOUT", GOGCLI_LOCK_TIMEOUT)
        return env

    def _missing_binary_message(self) -> str:
        """找不到 gogcli 可执行文件时的报错。"""
        message = f"找不到 gogcli 可执行文件：{self.binary}"
        if Path("/.dockerenv").exists():
            return message + "。服务运行在容器内：镜像构建时是否用 --build-arg GOGCLI_VERSION= 跳过了 gogcli？"
        return message + (
            "。launchd 启动的服务只认 install.sh 写进 plist 的 PATH：安装 gogcli 后请重跑 ./install.sh，"
            "或在 gogcli_bin 填写绝对路径（在终端执行 `command -v gog` 查看）"
        )

    def _exec(self, args: List[str], timeout: int) -> subprocess.CompletedProcess:
        """执行一次 gogcli；可执行文件缺失与超时转成 DestinationError。"""
        try:
            with _gogcli_lock:
                return self._run(
                    [self.binary, *args],
                    capture_output=True, text=True, timeout=timeout, env=self._env(),
                )
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            raise DestinationError(self._missing_binary_message())
        except subprocess.TimeoutExpired:
            raise DestinationError(f"gogcli 在 {timeout} 秒内没有返回")

    def _describe_failure(self, code: int, stderr: str) -> DestinationError:
        """把 gogcli 的退出码与 stderr 翻译成可读的错误。"""
        detail = (stderr or "").strip()[:500]
        if "Keychain" in detail or "keychain" in detail:
            return AuthRejected(
                f"gogcli 无法读取钥匙串（{detail}）。后台服务访问钥匙串会被授权弹窗挡住："
                "请以运行服务的用户登录，在终端执行 `gog auth list`，在弹窗中点「始终允许」"
            )
        if "no TTY" in detail or "GOG_KEYRING_PASSWORD" in detail:
            return AuthRejected(
                f"gogcli 使用文件存储令牌，但服务环境里没有 GOG_KEYRING_PASSWORD（{detail}）。"
                "请在服务的运行环境（docker-compose 的 environment / .env）中设置它"
            )
        if code == GOG_EXIT_CONFIG:
            # 实测：数据目录里没有 OAuth 客户端信息时退出码是 10，此时 `gog auth add` 也会失败
            return AuthRejected(
                f"gogcli 缺少 OAuth 客户端信息（{detail}）。请在运行服务的环境里执行 "
                "`gog auth credentials set <client_secret.json>`，再确认账号已授权"
            )
        if code == GOG_EXIT_AUTH_REQUIRED:
            return AuthRejected(
                f"gogcli 中账号 {self.account} 的授权不可用（{detail}）。请在运行服务的机器上执行 "
                f"`gog auth add {self.account} --services sheets`；如果 OAuth 应用仍处于 Testing 状态，"
                "refresh token 7 天后就会过期，需要把应用发布为正式版本"
            )
        if code == GOG_EXIT_PERMISSION_DENIED:
            return DestinationError(self.describe_status(403, detail))
        if code == GOG_EXIT_NOT_FOUND:
            return DestinationError(self.describe_status(404, detail))
        if code == GOG_EXIT_RATE_LIMITED:
            return DestinationError(f"Google Sheets 限流，gogcli 重试后仍失败（{detail}）")
        if code == GOG_EXIT_RETRYABLE:
            return DestinationError(f"Google Sheets 暂时不可用或网络超时（{detail}）")
        if code == GOG_EXIT_USAGE:
            return DestinationError(f"gogcli 拒绝了调用参数，可能是 gogcli 版本不兼容（{detail}）")
        return DestinationError(f"gogcli 执行失败，退出码 {code}（{detail}）")

    def preflight(self) -> List[CheckItem]:
        """gogcli 能否执行、账号是否已授权并包含 Sheets。"""
        try:
            version = self._exec(["--version"], GOGCLI_CHECK_TIMEOUT_SECONDS)
        except DestinationError as e:
            return [CheckItem("gogcli 可执行文件", CHECK_ERROR, str(e))]
        if version.returncode != 0:
            return [CheckItem("gogcli 可执行文件", CHECK_ERROR, str(self._describe_failure(version.returncode, version.stderr)))]
        items = [CheckItem("gogcli 可执行文件", CHECK_OK, f"{(version.stdout or '').strip() or 'gogcli'}（{self.binary}）")]
        items.append(self.check_account()[0])
        return items

    def check_account(self) -> Tuple[CheckItem, bool]:
        """
        账号授权这一项的结论，以及问题能否靠 `gog auth add` 解决。

        后者供部署脚本决定要不要提示授权：钥匙串被拒、令牌读取失败这类问题重新授权也解决不了，
        把它们也引向 `gog auth add` 只会让人白走一遍浏览器授权。
        """
        try:
            listed = self._exec(["auth", "list", "--json", "--no-input"], GOGCLI_CHECK_TIMEOUT_SECONDS)
        except DestinationError as e:
            return CheckItem(self.auth_title, CHECK_ERROR, str(e)), False
        if listed.returncode != 0:
            return CheckItem(self.auth_title, CHECK_ERROR, str(self._describe_failure(listed.returncode, listed.stderr))), False
        try:
            accounts = json.loads(listed.stdout or "{}").get("accounts") or []
        except (ValueError, AttributeError):
            accounts = []

        wanted = self.account.strip().lower()
        entry = next((a for a in accounts if str(a.get("email") or "").strip().lower() == wanted), None)
        add_hint = f"请在运行服务的机器上执行 `gog auth add {self.account} --services sheets`"
        if entry is None:
            known = "、".join(str(a.get("email")) for a in accounts if a.get("email")) or "（没有任何账号）"
            return CheckItem(self.auth_title, CHECK_ERROR, f"gogcli 中没有账号 {self.account} 的授权，现有：{known}。{add_hint}"), True
        if entry.get("error"):
            hint = f"；{entry['hint']}" if entry.get("hint") else ""
            return CheckItem(self.auth_title, CHECK_ERROR, f"账号 {self.account} 的令牌读取失败：{entry['error']}{hint}"), False
        services = [str(s) for s in entry.get("services") or []]
        if services and "sheets" not in services:
            return CheckItem(
                self.auth_title, CHECK_ERROR,
                f"账号 {self.account} 授权时没有包含 Sheets（现有：{'、'.join(services)}）。{add_hint}",
            ), True
        level = CHECK_OK if services else CHECK_WARN
        note = "" if services else "（gogcli 没有记录授权范围，以下面的实际请求为准）"
        return CheckItem(
            self.auth_title, level, f"已使用账号 {self.account}{note}，表格需要以「编辑者」身份共享给该账号",
        ), False

    def call(self, method: str, params: Dict[str, Any], body: Optional[dict] = None, quick: bool = False) -> dict:
        """
        经 `gog api call` 发一次请求。

        请求体写进仅本用户可读的临时文件，用 `--body @文件` 传入，不放命令行参数：
        一是参数有长度上限（一批可达 2MB），二是参数会出现在进程列表里，而正文是 Guest 都看不到的内容。
        gogcli 的 --body 不支持从 stdin 读取，所以是临时文件而不是管道。
        """
        args = [
            "api", "call", "sheets", "v4", method,
            "--params", json.dumps(params, ensure_ascii=False),
            # 不显式给权限范围时 gogcli 会挑"最窄"的 drive.file，只能访问它自己创建或打开过的文件
            "--scope", SPREADSHEETS_SCOPE,
            "--account", self.account,
            "--no-input", "--json",
        ]
        if method in _WRITE_METHODS:
            args += ["--allow-write", "--force"]

        body_path = ""
        if body is not None:
            fd, body_path = tempfile.mkstemp(prefix="opencr-gogcli-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, ensure_ascii=False)
            args += ["--body", f"@{body_path}"]

        try:
            attempts = 1 if quick else 1 + GOGCLI_EXTRA_ATTEMPTS
            timeout = GOGCLI_CHECK_TIMEOUT_SECONDS if quick else GOGCLI_TIMEOUT_SECONDS
            for attempt in range(attempts):
                result = self._exec(args, timeout)
                if result.returncode == 0:
                    try:
                        return json.loads(result.stdout) if (result.stdout or "").strip() else {}
                    except ValueError:
                        raise DestinationError(f"gogcli 的输出不是 JSON：{(result.stdout or '')[:300]}")
                retryable = result.returncode in (GOG_EXIT_RATE_LIMITED, GOG_EXIT_RETRYABLE)
                if retryable and attempt < attempts - 1:
                    delay = GOGCLI_RETRY_DELAYS[min(attempt, len(GOGCLI_RETRY_DELAYS) - 1)]
                    logger.info("gogcli exited %s for %s, retrying in %ss", result.returncode, method, delay)
                    self._sleep(delay)
                    continue
                raise self._describe_failure(result.returncode, result.stderr)
            raise DestinationError("gogcli 多次重试后仍失败")
        finally:
            if body_path:
                try:
                    os.unlink(body_path)
                except OSError:
                    # 删不掉只会在临时目录里留一份本用户可读的请求体，不影响推送结果
                    logger.warning("Failed to remove gogcli body file: %s", body_path)
