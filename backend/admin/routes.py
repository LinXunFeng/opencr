#!/usr/bin/env python3
"""
后台 API 与 SPA 托管。

接口分两档：require_admin（登录后可用）与 require_viewer（Guest 开关打开时也可用）。
放行 Guest 不等于返回全部内容 —— Finding 正文的剔除在**服务端**按身份完成，
前端隐藏不是安全边界，见 docs/adr/0002-guest-read-scope.md。
"""

import logging
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from flask import Blueprint, current_app, jsonify, request, send_from_directory, session

from ..review.config import (
    get_app_version,
    load_admin_config,
    load_gitlab_config,
    load_openai_config,
    load_review_config,
    load_storage_config,
)
from ..storage import repo
from .auth import (
    IDENTITY_ADMIN,
    SESSION_IDENTITY_KEY,
    admin_console_available,
    current_identity,
    guest_read_enabled,
    is_admin,
    require_admin,
    require_viewer,
    verify_password,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

admin_bp = Blueprint("admin", __name__, static_folder=None)

ALLOWED_WINDOWS = (1, 7, 30, 90)

# 允许后台改写的配置项白名单。
# 刻意排除密钥、地址与端口：写坏它们会让服务下次重启起不来，
# 而后台本身就在这个服务里 —— 你会失去补救的入口。
WRITABLE_SETTINGS = {
    "guest_read": {"type": "bool"},
}


def _window_days(default: int) -> int:
    """
    解析统计时间窗参数。

    只接受白名单内的取值：这个参数会进 SQL 的时间比较，
    放开任意整数等于允许构造一次全表扫描。
    """
    raw = (request.args.get("days") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value in ALLOWED_WINDOWS else default


def _stale_after() -> int:
    """心跳超时阈值（秒）。超过它的进行中 ReviewRun 会被标注为 Stale。"""
    return int(load_storage_config()["stale_after_seconds"])


def _add_change_links(runs: list[dict]) -> list[dict]:
    """按配置平台补充合并请求名称与网页地址；未知平台或缺失路径时不生成链接。"""
    config = load_gitlab_config()
    platform = config.get("type", "gitlab")
    label, route = {"gitlab": ("MR", "-/merge_requests"), "github": ("PR", "pull")}.get(
        platform, ("合并请求", "")
    )
    base = urlsplit(str(config.get("url") or "").strip())
    # 链接只包含网页地址，不把配置中可能存在的凭据、查询参数带给浏览器。
    valid = base.scheme in {"http", "https"} and bool(base.netloc) and not base.username and not base.password
    for run in runs:
        project_path = str(run.get("project_path") or "").strip("/")
        run["change_label"] = label
        run["change_url"] = ""
        # 未知平台不能套用 GitLab 路由；数据库沿用 mr_iid，不为展示功能做迁移。
        if valid and route and project_path and run.get("mr_iid"):
            path = f"{base.path.rstrip('/')}/{quote(project_path, safe='/')}/{route}/{int(run['mr_iid'])}"
            run["change_url"] = urlunsplit((base.scheme, base.netloc, path, "", ""))
    return runs


def _int_arg(name: str, default: int, maximum: int) -> int:
    """读取整型查询参数并夹到合理区间，避免前端传一个巨大的 limit。"""
    try:
        value = int((request.args.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return max(0, min(value, maximum))


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

@admin_bp.route("/api/admin/login", methods=["POST"])
def api_login():
    """账密登录。成功后种 session cookie。"""
    available, rejection = admin_console_available()
    if not available:
        return rejection

    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")

    cfg = load_admin_config()
    password_hash = current_app.config.get("ADMIN_PASSWORD_HASH", "")

    if username != cfg["username"] or not verify_password(password, password_hash):
        # 不区分"用户名不存在"与"密码错误"，避免帮攻击者确认用户名
        logger.warning("Admin login failed from %s", request.remote_addr)
        return jsonify({"error": "用户名或密码错误"}), 401

    session.clear()
    session[SESSION_IDENTITY_KEY] = IDENTITY_ADMIN
    session.permanent = True
    logger.info("Admin logged in from %s", request.remote_addr)
    return jsonify({"identity": IDENTITY_ADMIN, "username": cfg["username"]})


@admin_bp.route("/api/admin/logout", methods=["POST"])
def api_logout():
    """登出。无论当前是否已登录都返回成功，调用方不必先判断状态。"""
    session.clear()
    return jsonify({"identity": "guest"})


@admin_bp.route("/api/admin/me", methods=["GET"])
def api_me():
    """
    当前身份与后台能力。

    这是前端唯一的入口探针：它决定登录页是否放行、左侧菜单显示到哪一级。
    因此它不能被 require_viewer 拦住 —— Guest 关闭时也要能拿到"需要登录"这个事实。
    """
    available, rejection = admin_console_available()
    if not available:
        return rejection

    cfg = load_admin_config()
    guest_enabled = guest_read_enabled()
    return jsonify(
        {
            "identity": current_identity(),
            "username": cfg["username"] if is_admin() else "",
            "guest_read": guest_enabled,
            # Guest 开关关闭且未登录时，前端应直接跳登录页
            "requires_login": not is_admin() and not guest_enabled,
            "version": get_app_version(),
        }
    )


# ---------------------------------------------------------------------------
# 控制台与列表
# ---------------------------------------------------------------------------

@admin_bp.route("/api/admin/dashboard", methods=["GET"])
@require_viewer
def api_dashboard():
    """控制台聚合数据：卡片数字 + 图表所需的分布与趋势。"""
    stale_after = _stale_after()
    openai_cfg = load_openai_config()
    errors = repo.error_stats(days=_window_days(7))
    verdicts = repo.verdict_stats(days=_window_days(30))

    return jsonify(
        {
            "service": {
                "version": get_app_version(),
                "model": openai_cfg.get("model", "unknown"),
                "stale_after_seconds": stale_after,
            },
            "active_runs": repo.list_active_runs(stale_after_seconds=stale_after),
            "errors": errors,
            "verdicts": verdicts,
            "trend": repo.daily_run_trend(days=_window_days(30)),
            "projects": repo.list_projects(),
        }
    )


@admin_bp.route("/api/admin/runs", methods=["GET"])
@require_viewer
def api_runs():
    """运行列表。"""
    return jsonify(
        {
            "items": _add_change_links(repo.list_recent_runs(
                limit=_int_arg("limit", 50, 200),
                status=(request.args.get("status") or "").strip(),
                stale_after_seconds=_stale_after(),
            ))
        }
    )


@admin_bp.route("/api/admin/runs/<run_uid>", methods=["GET"])
@require_viewer
def api_run_detail(run_uid: str):
    """
    运行详情。

    Guest 拿不到 Finding 正文 —— 这里在返回前直接删掉字段，
    而不是发给前端再隐藏。
    """
    detail = repo.get_run_detail(run_uid, stale_after_seconds=_stale_after())
    if detail is None:
        return jsonify({"error": "run not found"}), 404

    body_included = is_admin()
    if not body_included:
        for finding in detail.get("findings", []):
            finding.pop("body", None)
    detail["body_included"] = body_included
    _add_change_links([detail])
    return jsonify(detail)


@admin_bp.route("/api/admin/runs/<run_uid>/history", methods=["GET"])
@require_viewer
def api_change_history(run_uid: str):
    """同一合并请求的历史审查批次；Guest 可看批次与发现元数据，但拿不到正文。"""
    result = repo.get_change_history(
        run_uid, limit=_int_arg("limit", 20, 100), offset=_int_arg("offset", 0, 100000),
        severity=(request.args.get("severity") or "").strip(),
        verdict=(request.args.get("verdict") or "").strip(), stale_after_seconds=_stale_after(),
    )
    if result is None:
        return jsonify({"error": "未找到审查运行"}), 404
    body_included = is_admin()
    for run in result["items"]:
        if not body_included:
            for finding in run["findings"]:
                finding.pop("body", None)
        run["body_included"] = body_included
    result["body_included"] = body_included
    return jsonify(result)


@admin_bp.route("/api/admin/findings", methods=["GET"])
@require_viewer
def api_findings():
    """跨 ReviewRun 的审查发现列表。Guest 同样拿不到正文。"""
    project_id_raw = (request.args.get("project_id") or "").strip()
    try:
        project_id = int(project_id_raw) if project_id_raw else None
    except ValueError:
        project_id = None

    return jsonify(
        repo.list_findings(
            limit=_int_arg("limit", 50, 200),
            offset=_int_arg("offset", 0, 100000),
            project_id=project_id,
            verdict=(request.args.get("verdict") or "").strip(),
            severity=(request.args.get("severity") or "").strip(),
            days=_window_days(30),
            include_body=is_admin(),
        )
    )


@admin_bp.route("/api/admin/stats/verdicts", methods=["GET"])
@require_viewer
def api_stats_verdicts():
    """采纳分析。"""
    return jsonify(repo.verdict_stats(days=_window_days(30)))


@admin_bp.route("/api/admin/stats/errors", methods=["GET"])
@require_viewer
def api_stats_errors():
    """错误分析。"""
    return jsonify(
        {
            **repo.error_stats(days=_window_days(7)),
            "trend": repo.daily_run_trend(days=_window_days(30)),
        }
    )


@admin_bp.route("/api/admin/skills", methods=["GET"])
@require_viewer
def api_skills():
    """
    已加载的 skill 及其在近期审查中的命中次数。

    用 previews 而不是 load_available_review_skills：后者返回的是每个 skill 的**完整
    prompt 正文**，既没必要传给前端，也会让这个接口变得很重。
    """
    from ..review.skills import load_review_skill_previews

    review_cfg = load_review_config()
    try:
        previews = load_review_skill_previews(review_cfg["skills_dir"])
    except Exception:
        logger.warning("Failed to load skills for admin console", exc_info=True)
        previews = {}

    hits = repo.skill_hit_counts(days=_window_days(30))
    items = [
        {"name": name, "description": preview, "hits": hits.get(name, 0)}
        for name, preview in sorted(previews.items())
    ]
    # 有命中记录但当前目录里已不存在的 skill 也列出来 —— 它解释了历史数据的来源
    for name, count in sorted(hits.items()):
        if name not in previews:
            items.append({"name": name, "description": "（当前 skills 目录中不存在）", "hits": count})

    return jsonify(
        {
            "skills_dir": review_cfg["skills_dir"],
            "scripts_enabled": review_cfg["skill_scripts_enabled"],
            "items": items,
            "hits": hits,
        }
    )


# ---------------------------------------------------------------------------
# 系统设置（仅 Admin）
# ---------------------------------------------------------------------------

@admin_bp.route("/api/admin/settings", methods=["GET"])
@require_admin
def api_settings():
    """
    当前生效配置。

    密钥类字段只回报"是否已设置"，绝不回报内容；password 连脱敏占位符都不给 ——
    哈希虽不可逆，但泄露它等于打开离线爆破的门，而这个接口的价值不包含确认密码。
    """
    openai_cfg = load_openai_config()
    gitlab_cfg = load_gitlab_config()
    review_cfg = load_review_config()
    storage_cfg = load_storage_config()
    admin_cfg = load_admin_config()

    return jsonify(
        {
            "readonly": {
                "version": get_app_version(),
                "openai": {
                    "base_url": openai_cfg.get("base_url", ""),
                    "model": openai_cfg.get("model", ""),
                    "reasoning_effort": openai_cfg.get("reasoning_effort", ""),
                    "api_key_set": bool(openai_cfg.get("api_key")),
                },
                "code_platform": {
                    "url": gitlab_cfg.get("url", ""),
                    "token_set": bool(gitlab_cfg.get("token")),
                    "webhook_secret_set": bool(gitlab_cfg.get("webhook_secret")),
                },
                "review": review_cfg,
                "storage": storage_cfg,
                "admin": {
                    "username": admin_cfg["username"],
                    "bind_local_only": admin_cfg["bind_local_only"],
                },
            },
            "writable": {"guest_read": guest_read_enabled()},
        }
    )


@admin_bp.route("/api/admin/settings", methods=["PATCH"])
@require_admin
def api_update_settings():
    """
    改写可写子集。

    白名单之外的键一律拒绝并明确报错，而不是静默忽略 ——
    静默忽略会让调用方以为改成功了。
    """
    payload = request.get_json(silent=True) or {}
    unknown = [k for k in payload if k not in WRITABLE_SETTINGS]
    if unknown:
        return jsonify({"error": f"不可写的配置项: {', '.join(sorted(unknown))}"}), 400

    for key, value in payload.items():
        if WRITABLE_SETTINGS[key]["type"] == "bool":
            repo.set_setting(key, "1" if value else "0")
            logger.info("Admin changed setting %s -> %s", key, bool(value))

    return jsonify({"writable": {"guest_read": guest_read_enabled()}})


# ---------------------------------------------------------------------------
# SPA 托管
# ---------------------------------------------------------------------------

@admin_bp.route("/admin", defaults={"path": ""}, methods=["GET"])
@admin_bp.route("/admin/<path:path>", methods=["GET"])
def spa(path: str):
    """
    托管 Vue 打包产物。

    命中真实文件就直接返回，否则一律回 index.html 交给前端路由 ——
    刷新 /admin/runs/xxx 时浏览器请求的是这个路径，服务端并不认识它。

    这里刻意不做鉴权：index.html 与 JS 里没有任何数据，
    数据一律走 /api/admin/*，鉴权在那一层。
    """
    available, rejection = admin_console_available()
    if not available:
        return rejection

    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return (
            "后台前端产物缺失。请在 web/ 目录执行 pnpm install && pnpm build，"
            "或使用发布版本。",
            503,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    if path:
        candidate = STATIC_DIR / path
        # 防目录穿越：解析后必须仍在 STATIC_DIR 内
        try:
            candidate.resolve().relative_to(STATIC_DIR.resolve())
        except ValueError:
            return jsonify({"error": "invalid path"}), 400
        if candidate.is_file():
            return send_from_directory(STATIC_DIR, path)

    return send_from_directory(STATIC_DIR, "index.html")
