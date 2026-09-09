#!/usr/bin/env python3
"""
后台鉴权：单个 Admin 账号 + 可选的 Guest 浏览。

系统里只有一个管理账号，因此不做用户表、不做角色。Guest 不是一种账号，
而是身份的缺席——见 CONTEXT.md「访问身份」与 docs/adr/0002-guest-read-scope.md。

密码的唯一真相是 config.yaml。首次启动时若发现写的是明文，会就地改写成哈希；
数据库不存任何凭据，避免出现"文件里一个、库里另一个"的双真相。
"""

import ipaddress
import logging
import os
import re
import secrets
import tempfile
from functools import wraps
from pathlib import Path

from flask import jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from ..review.config import load_admin_config, resolve_active_config_path
from ..storage import repo

logger = logging.getLogger(__name__)

IDENTITY_ADMIN = "admin"
IDENTITY_GUEST = "guest"

SESSION_IDENTITY_KEY = "identity"

# Werkzeug 生成的哈希前缀；据此判断 config.yaml 里写的是明文还是已处理过的哈希
HASH_PREFIXES = ("scrypt:", "pbkdf2:", "argon2", "bcrypt")


def is_hashed(value: str) -> bool:
    """判断配置里的密码是否已经是哈希。"""
    return str(value or "").startswith(HASH_PREFIXES)


def hash_password(plain: str) -> str:
    """生成密码哈希。算法用 Werkzeug 默认的 scrypt——它是 Flask 既有依赖，不额外引包。"""
    return generate_password_hash(plain)


def _rewrite_password_line(config_path: Path, plain: str, hashed: str) -> bool:
    """
    把 config.yaml 里的明文密码就地换成哈希。

    刻意做成对单行的定点替换而不是 YAML 反序列化再 dump：本项目没有 PyYAML 依赖
    （容器里走的是 parse_basic_yaml 简化解析器），而且 dump 会把配置文件里那套
    中英双语注释全部抹掉。

    写入用同目录临时文件 + os.replace 原子替换，并预先把权限设成 600
    （config.yaml 本身是 600，直接写会丢权限）。
    """
    try:
        original = config_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("无法读取 %s，跳过密码哈希化: %s", config_path, e)
        return False

    # 只替换 admin 段里那一行 password，且值必须正好是我们读到的明文
    pattern = re.compile(
        r"^(?P<indent>[ \t]*)password[ \t]*:[ \t]*(?P<quote>[\"']?)"
        + re.escape(plain)
        + r"(?P=quote)[ \t]*$",
        flags=re.MULTILINE,
    )
    replacement = (
        "\\g<indent># 首次启动时已自动将明文密码替换为哈希。\n"
        "\\g<indent># 想改密码：把这一行的值换回明文并重启，服务会重新哈希。\n"
        f"\\g<indent>password: \"{hashed}\""
    )
    updated, count = pattern.subn(replacement, original, count=1)
    if count == 0:
        logger.warning("在 %s 中未找到匹配的明文 password 行，跳过哈希化", config_path)
        return False

    if not _atomic_write(config_path, updated) and not _inplace_write(config_path, updated):
        return False

    logger.info("已将 %s 中的明文密码替换为哈希", config_path)
    return True


def _atomic_write(config_path: Path, content: str) -> bool:
    """
    同目录临时文件 + os.replace 原子替换。

    这是常规文件系统上的正确写法：中途崩溃不会留下写了一半的配置。
    """
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(config_path.parent), prefix=".config-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
        except Exception:
            os.unlink(tmp_name)
            raise
        os.replace(tmp_name, config_path)
        return True
    except OSError as e:
        logger.info("原子替换 %s 失败（%s），改用原地写入", config_path, e)
        return False


def _inplace_write(config_path: Path, content: str) -> bool:
    """
    原地覆盖写入，作为原子替换失败后的兜底。

    Docker 把 config.yaml 挂载成**单文件** bind mount 时，os.replace 会以
    "Device or resource busy" 失败——rename 无法覆盖一个挂载点。而 Docker 恰好是
    推荐的部署方式，不兜底就意味着这条路径上的明文密码永远清理不掉。

    代价是有一个极短的非原子窗口。考虑到文件只有几 KB、一次写入、且只在首次启动时
    发生，这个代价小于"推荐路径上明文长期留在磁盘上"。
    """
    try:
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return True
    except OSError as e:
        logger.warning("无法写入 %s，跳过密码哈希化: %s", config_path, e)
        return False


def ensure_password_hashed() -> str:
    """
    确保拿到可用的密码哈希，并尽量把 config.yaml 里的明文清理掉。

    返回本进程使用的哈希；后台未启用或未配密码时返回空串。

    改写失败（只读挂载、只读文件系统等）不会中断启动：哈希留在内存里照常工作，
    但每次启动都会打一条醒目警告——静默留下明文才是最坏的结果。
    """
    cfg = load_admin_config()
    if not cfg["enabled"]:
        return ""

    raw = cfg["password"]
    if not raw:
        return ""
    if is_hashed(raw):
        return raw

    hashed = hash_password(raw)
    config_path = resolve_active_config_path()
    rewritten = bool(config_path) and _rewrite_password_line(Path(config_path), raw, hashed)
    if not rewritten:
        logger.warning(
            "⚠️  config.yaml 中的明文密码未能自动替换为哈希（文件可能是只读挂载）。"
            "服务可正常使用，但请手动把 admin.password 换成哈希，或让该文件可写后重启。"
        )
    return hashed


def get_secret_key() -> str:
    """
    Flask session 签名密钥，持久化在数据库里。

    不能每次启动随机生成——那样每次重启和每个 worker 都会让已登录的人被登出。
    """
    existing = repo.get_setting("secret_key")
    if existing:
        return existing
    generated = secrets.token_hex(32)
    return repo.set_setting_if_absent("secret_key", generated)


def guest_read_enabled() -> bool:
    """
    是否允许 Guest 浏览。

    存数据库而非 config.yaml：这个开关的使用场景天然是临时性的，
    要求改文件加重启就等于没人会用。见 ADR-0002。
    """
    value = repo.get_setting("guest_read")
    if value is None:
        return True  # 默认开启
    return value == "1"


def guest_retry_enabled() -> bool:
    """游客重新触发开关，未设置时默认关闭，独立于游客浏览开关保存。"""
    return repo.get_setting("guest_retry") == "1"


def survey_guest_read_enabled() -> bool:
    """
    是否允许 Guest 浏览巡检结果。

    嵌套在 guest_read 之下：guest_read 关着的时候这个开关没有意义。
    默认开启，与 MR 审查一致 —— 依据同样是 ADR-0002 的那条边界（正文已剔除），
    而不是"巡检不敏感"。开关控制的是"看不看得到这个模块"，
    打开后 Finding 正文与整合叙述**依然剔除**。
    """
    value = repo.get_setting("survey_guest_read")
    if value is None:
        return True  # 默认开启
    return value == "1"


def current_identity() -> str:
    """
    当前请求的身份：admin 或 guest。

    Guest 是身份的缺席，因此"未登录"永远返回 guest，不返回 None——
    调用方不必到处判空，只需判断是不是 admin。
    """
    if session.get(SESSION_IDENTITY_KEY) == IDENTITY_ADMIN:
        return IDENTITY_ADMIN
    return IDENTITY_GUEST


def is_admin() -> bool:
    return current_identity() == IDENTITY_ADMIN


def verify_password(plain: str, password_hash: str) -> bool:
    """校验密码。任一侧为空一律不通过，避免"没配密码等于免密登录"。"""
    if not plain or not password_hash:
        return False
    return check_password_hash(password_hash, plain)


def _is_local_request() -> bool:
    """
    请求是否来自本机。

    取不到或解析不了 remote_addr 时一律判为非本机——拿不准就从严，
    这个判断唯一的用途是收紧访问范围。
    """
    remote = (request.remote_addr or "").strip()
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


def _reject(message: str, status: int):
    return jsonify({"error": message}), status


def admin_console_available() -> tuple:
    """
    后台是否可访问。返回 (是否可用, 错误响应)。

    把"后台没开"和"来源受限"这两种前置检查抽出来，登录接口与数据接口共用。
    """
    cfg = load_admin_config()
    if not cfg["enabled"]:
        return False, _reject("Admin console is disabled", 404)
    if cfg["bind_local_only"] and not _is_local_request():
        logger.warning("Admin access rejected from non-local address: %s", request.remote_addr)
        return False, _reject("Admin console is restricted to localhost", 403)
    return True, None


def require_admin(f):
    """只有登录后的 Admin 能访问。用于配置读写等敏感接口。"""

    @wraps(f)
    def decorated(*args, **kwargs):
        available, rejection = admin_console_available()
        if not available:
            return rejection
        if not is_admin():
            return _reject("Authentication required", 401)
        return f(*args, **kwargs)

    return decorated


def require_survey_viewer(f):
    """
    巡检接口的访问档位：Admin 恒可访问；Guest 需要 guest_read 与 survey_guest_read 同时打开。

    与 require_viewer 分开而不是加参数，是为了让"这个接口属于哪一档"
    在路由定义处一眼可见 —— 漏加一个参数不会报错，但会静默放开访问。
    """

    @wraps(f)
    def decorated(*args, **kwargs):
        available, rejection = admin_console_available()
        if not available:
            return rejection
        if is_admin():
            return f(*args, **kwargs)
        if guest_read_enabled() and survey_guest_read_enabled():
            return f(*args, **kwargs)
        return _reject("Authentication required", 401)

    return decorated


def require_viewer(f):
    """
    Admin 可访问；Guest 在开关打开时也可访问。

    注意：放行 Guest 不等于返回全部内容。凡是可能包含 Finding 正文的接口，
    必须在序列化前按 is_admin() 剔除该字段——前端隐藏不是安全边界。
    """

    @wraps(f)
    def decorated(*args, **kwargs):
        available, rejection = admin_console_available()
        if not available:
            return rejection
        if is_admin():
            return f(*args, **kwargs)
        if guest_read_enabled():
            return f(*args, **kwargs)
        return _reject("Authentication required", 401)

    return decorated
