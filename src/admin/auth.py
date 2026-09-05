#!/usr/bin/env python3
"""
后台鉴权。

单用户场景下 session 管理是纯粹的负担，因此只做一个共享 token：
请求头 X-Admin-Token、cookie、或 ?token= 三种带法，命中即放行。
"""

import hmac
import ipaddress
import logging
from functools import wraps

from flask import Response, request

from ..review.config import load_admin_config

logger = logging.getLogger(__name__)

COOKIE_NAME = "opencr_admin_token"


def _token_matches(provided: str, expected: str) -> bool:
    """常量时间比较，避免逐字符比较泄露 token 长度与前缀信息。"""
    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def _is_local_request() -> bool:
    """
    请求是否来自本机。

    取不到或解析不了 remote_addr 时一律判为非本机 —— 拿不准就从严，
    这个判断唯一的用途是收紧访问范围。
    """
    remote = (request.remote_addr or "").strip()
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


def extract_token() -> str:
    """按 请求头 > 查询参数 > cookie 的顺序取 token。"""
    header = (request.headers.get("X-Admin-Token") or "").strip()
    if header:
        return header
    query = (request.args.get("token") or "").strip()
    if query:
        return query
    return (request.cookies.get(COOKIE_NAME) or "").strip()


def require_admin(f):
    """后台路由的统一守卫。"""

    @wraps(f)
    def decorated(*args, **kwargs):
        """依次校验：后台是否启用 → 来源是否受限 → token 是否匹配。"""
        cfg = load_admin_config()

        if not cfg["enabled"]:
            return Response("Admin console is disabled", status=404, mimetype="text/plain")

        if cfg["bind_local_only"] and not _is_local_request():
            logger.warning("Admin access rejected from non-local address: %s", request.remote_addr)
            return Response("Admin console is restricted to localhost", status=403, mimetype="text/plain")

        if not _token_matches(extract_token(), cfg["token"]):
            return Response(
                "Unauthorized. Provide the admin token via the X-Admin-Token header or ?token=",
                status=401,
                mimetype="text/plain",
            )

        response = f(*args, **kwargs)
        # 用 ?token= 访问成功后种下 cookie，后续页面跳转不必再带参数
        if request.args.get("token"):
            try:
                from flask import make_response

                response = make_response(response)
                response.set_cookie(
                    COOKIE_NAME,
                    cfg["token"],
                    httponly=True,
                    samesite="Lax",
                    max_age=7 * 24 * 3600,
                )
            except Exception:
                logger.warning("Failed to set admin cookie", exc_info=True)
        return response

    return decorated
