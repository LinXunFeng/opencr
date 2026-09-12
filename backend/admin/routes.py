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
from ..survey.common import SurveyError, slugify
from ..survey.config import load_survey_config
from ..survey.profile import codegraph_available
from ..survey.report import render_run_markdown
from ..survey.schedule import describe_schedule, next_fire_time
from ..survey.scheduler import trigger_survey_now
from ..survey.workspace import delete_workspace, workspace_size_bytes
from .auth import (
    IDENTITY_ADMIN,
    SESSION_IDENTITY_KEY,
    admin_console_available,
    current_identity,
    guest_read_enabled,
    guest_retry_enabled,
    is_admin,
    require_admin,
    require_survey_viewer,
    require_viewer,
    survey_guest_read_enabled,
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
    "guest_retry": {"type": "bool"},
    # 嵌套在 guest_read 之下：guest_read 关着时它没有意义。
    # 默认开启，与 MR 审查一致；打开后巡检发现的正文与整合叙述**依然剔除**。
    "survey_guest_read": {"type": "bool"},
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
            "survey_guest_read": survey_guest_read_enabled(),
            "survey_enabled": load_survey_config()["enabled"],
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
    detail["can_retry"] = is_admin() or (guest_read_enabled() and guest_retry_enabled())
    detail["body_included"] = body_included
    _add_change_links([detail])
    return jsonify(detail)


@admin_bp.route("/api/admin/runs/<run_uid>/retry", methods=["POST"])
@require_viewer
def api_retry_run(run_uid: str):
    """按指定范围重新触发失败运行，返回新运行或拒绝原因。"""
    from ..review.gitlab import get_mr_state
    from ..review.skills import resolve_review_options
    from ..storage.models import ERROR_UNEXPECTED, RUN_FAILED

    if not is_admin() and not guest_retry_enabled():
        return jsonify({"error": "游客重新触发未开启"}), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"scope"} or payload["scope"] not in ("latest", "original"):
        return jsonify({"error": "请仅提供 scope，取值为 latest 或 original"}), 400
    source = repo.get_run_detail(run_uid)
    if source is None:
        return jsonify({"error": "原审查运行不存在或已清理"}), 404
    if source["status"] != RUN_FAILED:
        return jsonify({"error": "仅明确失败的审查运行可以重新触发"}), 409
    try:
        mr = get_mr_state(source["project_id"], source["mr_iid"])
    except Exception:
        # 无法确认 MR 状态时不启动，原运行不受影响，也不向游客暴露上游错误中的凭据。
        logger.exception("重新触发前读取 MR 状态失败")
        return jsonify({"error": "无法读取 MR 当前状态，请稍后重试"}), 502
    if mr["state"] != "opened":
        return jsonify({"error": "仅允许对仍处于 opened 状态的 MR 重新触发"}), 409
    try:
        mode, skill = resolve_review_options({}, default_skill="")
        params = repo.start_retry_run(run_uid, payload["scope"], mode, skill)
    except repo.RetryRejected as exc:
        return jsonify({"error": str(exc), "active_run_uid": exc.active_run_uid}), exc.status
    try:
        current_app.config["START_REVIEW_THREAD"](**params, log_prefix="[Retry]")
    except Exception:
        # 线程未启动也必须收尾，否则新记录会永久挡住后续重试。
        logger.exception("重试审查线程启动失败")
        repo.finish_run(params["run_uid"], RUN_FAILED, ERROR_UNEXPECTED, "审查线程启动失败")
        return jsonify({"error": "审查线程启动失败", "run_uid": params["run_uid"]}), 500
    return jsonify({"run_uid": params["run_uid"], "status": "processing"}), 202


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
                "survey": {
                    **load_survey_config(),
                    # codegraph 是可选依赖，装没装是运维最常问的一件事
                    "codegraph_available": codegraph_available(),
                },
                "admin": {
                    "username": admin_cfg["username"],
                    "bind_local_only": admin_cfg["bind_local_only"],
                },
            },
            "writable": {
                "guest_read": guest_read_enabled(),
                "guest_retry": guest_retry_enabled(),
                "survey_guest_read": survey_guest_read_enabled(),
            },
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
    if not isinstance(payload, dict) or any(type(v) is not bool for v in payload.values()):
        return jsonify({"error": "设置必须为布尔值对象"}), 400
    unknown = [k for k in payload if k not in WRITABLE_SETTINGS]
    if unknown:
        return jsonify({"error": f"不可写的配置项: {', '.join(sorted(unknown))}"}), 400

    for key, value in payload.items():
        if WRITABLE_SETTINGS[key]["type"] == "bool":
            repo.set_setting(key, "1" if value else "0")
            logger.info("Admin changed setting %s -> %s", key, bool(value))

    return jsonify(
        {
            "writable": {
                "guest_read": guest_read_enabled(),
                "guest_retry": guest_retry_enabled(),
                "survey_guest_read": survey_guest_read_enabled(),
            }
        }
    )


# ---------------------------------------------------------------------------
# 定期巡检
#
# 读接口走 require_survey_viewer（Guest 需两个开关同时打开），
# 写接口一律 require_admin —— 触发一次巡检会拉几十 GB 代码并花掉真金白银。
# ---------------------------------------------------------------------------

MAX_SURVEY_SOURCES = 30


def _parse_sources(raw) -> list:
    """校验并规整来源清单。返回规整后的列表，非法时抛 ValueError。"""
    if not isinstance(raw, list):
        raise ValueError("sources 必须是数组")
    if len(raw) > MAX_SURVEY_SOURCES:
        raise ValueError(f"来源数量不能超过 {MAX_SURVEY_SOURCES} 条")

    sources = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("每条来源必须是对象")
        url = str(item.get("url") or "").strip()
        if not url:
            raise ValueError("来源地址不能为空")
        kind = str(item.get("kind") or "repo").strip().lower()
        if kind not in {"repo", "org"}:
            raise ValueError(f"未知的来源类型：{kind}")
        patterns = item.get("exclude_patterns") or []
        if not isinstance(patterns, list):
            raise ValueError("exclude_patterns 必须是数组")
        sources.append(
            {
                "kind": kind,
                "url": url,
                # 分支只对具体仓库有意义；组织下各仓库用各自的默认分支
                "branch": str(item.get("branch") or "").strip() if kind == "repo" else "",
                "exclude_patterns": [str(x).strip() for x in patterns if str(x).strip()],
            }
        )
    return sources


def _parse_survey_payload(payload: dict, partial: bool) -> tuple:
    """
    校验巡检配置入参，返回 (fields, sources)。

    周期在这里就折算一次 cron：非法表达式必须在保存时被挡住，
    而不是等到某个周一早上没有触发时才由人去翻日志。
    """
    fields = {}

    if "name" in payload or not partial:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("巡检名称不能为空")
        fields["name"] = name[:255]

    if any(k in payload for k in ("schedule_kind", "schedule_expr", "timezone")) or not partial:
        kind = str(payload.get("schedule_kind") or "weekly").strip().lower()
        expr = str(payload.get("schedule_expr") or "1 09:00").strip()
        tz = str(payload.get("timezone") or "UTC").strip()
        # 抛出的 SurveyError 由调用方转成 400，措辞对用户已经足够具体
        next_fire_time(kind, expr, tz)
        fields.update({"schedule_kind": kind, "schedule_expr": expr, "timezone": tz})

    for key in ("enabled", "delete_workspace_after"):
        if key in payload:
            fields[key] = bool(payload[key])

    if "excluded_skills" in payload:
        raw = payload["excluded_skills"]
        if not isinstance(raw, list):
            raise ValueError("excluded_skills 必须是数组")
        fields["excluded_skills"] = [str(x).strip() for x in raw if str(x).strip()]

    for key in (
        "budget_wall_clock_minutes",
        "budget_l1_max_chars",
        "budget_l2_max_focus",
        "budget_index_timeout_seconds",
        "retention_runs",
    ):
        if key in payload:
            value = payload[key]
            if value in (None, ""):
                fields[key] = None
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是整数")
            if parsed <= 0:
                raise ValueError(f"{key} 必须是正整数")
            fields[key] = parsed

    sources = None
    if "sources" in payload:
        sources = _parse_sources(payload["sources"])
    elif not partial:
        raise ValueError("必须至少提供一条仓库来源")
    if sources is not None and not sources:
        raise ValueError("必须至少提供一条仓库来源")

    return fields, sources


def _decorate_survey(survey: dict) -> dict:
    """给巡检配置补上展示用的派生字段。"""
    return {
        **survey,
        "schedule_desc": describe_schedule(
            survey["schedule_kind"], survey["schedule_expr"], survey["timezone"]
        ),
        "workspace_bytes": workspace_size_bytes(survey["slug"]),
    }


@admin_bp.route("/api/admin/surveys", methods=["GET"])
@require_survey_viewer
def api_surveys():
    """巡检配置列表。"""
    return jsonify({"items": [_decorate_survey(s) for s in repo.list_surveys()]})


@admin_bp.route("/api/admin/surveys", methods=["POST"])
@require_admin
def api_create_survey():
    """新建巡检。"""
    payload = request.get_json(silent=True) or {}
    try:
        fields, sources = _parse_survey_payload(payload, partial=False)
    except (ValueError, SurveyError) as e:
        return jsonify({"error": str(e)}), 400

    next_run = next_fire_time(fields["schedule_kind"], fields["schedule_expr"], fields["timezone"])
    survey = repo.create_survey(
        name=fields["name"],
        slug=slugify(fields["name"], fallback="survey"),
        schedule_kind=fields["schedule_kind"],
        schedule_expr=fields["schedule_expr"],
        timezone_name=fields["timezone"],
        sources=sources,
        next_run_at=next_run,
        **{k: v for k, v in fields.items() if k not in {"name", "schedule_kind", "schedule_expr", "timezone"}},
    )
    logger.info("Admin created survey: %s (slug=%s)", survey["name"], survey["slug"])
    return jsonify(_decorate_survey(survey)), 201


@admin_bp.route("/api/admin/surveys/<survey_uid>", methods=["GET"])
@require_survey_viewer
def api_survey_detail(survey_uid: str):
    """单个巡检配置。"""
    survey = repo.get_survey(survey_uid)
    if survey is None:
        return jsonify({"error": "巡检不存在"}), 404
    return jsonify(_decorate_survey(survey))


@admin_bp.route("/api/admin/surveys/<survey_uid>", methods=["PATCH"])
@require_admin
def api_update_survey(survey_uid: str):
    """更新巡检配置。周期或启用状态变化时重算下次触发时间。"""
    payload = request.get_json(silent=True) or {}
    try:
        fields, sources = _parse_survey_payload(payload, partial=True)
    except (ValueError, SurveyError) as e:
        return jsonify({"error": str(e)}), 400

    current = repo.get_survey(survey_uid)
    if current is None:
        return jsonify({"error": "巡检不存在"}), 404

    merged = {**current, **fields}
    if any(k in fields for k in ("schedule_kind", "schedule_expr", "timezone", "enabled")):
        fields["next_run_at"] = (
            next_fire_time(merged["schedule_kind"], merged["schedule_expr"], merged["timezone"])
            if merged.get("enabled", True)
            else None
        )

    survey = repo.update_survey(survey_uid, fields, sources)
    if survey is None:
        return jsonify({"error": "巡检不存在"}), 404
    logger.info("Admin updated survey: %s", survey["name"])
    return jsonify(_decorate_survey(survey))


@admin_bp.route("/api/admin/surveys/<survey_uid>", methods=["DELETE"])
@require_admin
def api_delete_survey(survey_uid: str):
    """
    删除巡检配置与其全部运行记录。

    **不连带删工作区** —— 误删一个巡检顺手把几十 GB 代码删掉是不可逆的。
    工作区清理是下面那个独立接口。
    """
    slug = repo.delete_survey(survey_uid)
    if slug is None:
        return jsonify({"error": "巡检不存在"}), 404
    logger.info("Admin deleted survey: slug=%s (workspace kept)", slug)
    return jsonify({"deleted": True, "slug": slug, "workspace_kept": True})


@admin_bp.route("/api/admin/surveys/<survey_uid>/run", methods=["POST"])
@require_admin
def api_run_survey(survey_uid: str):
    """
    立即执行一次巡检。

    走的是和定时触发完全相同的入口，只有 trigger 字段不同。
    """
    if repo.get_survey(survey_uid) is None:
        return jsonify({"error": "巡检不存在"}), 404
    if not trigger_survey_now(survey_uid):
        return jsonify({"error": "该巡检已有正在进行的运行"}), 409
    logger.info("Admin manually triggered survey: %s", survey_uid)
    return jsonify({"started": True}), 202


@admin_bp.route("/api/admin/surveys/<survey_uid>/workspace", methods=["DELETE"])
@require_admin
def api_clear_workspace(survey_uid: str):
    """清理某个巡检的本地工作区。下次执行会退回全量克隆。"""
    survey = repo.get_survey(survey_uid)
    if survey is None:
        return jsonify({"error": "巡检不存在"}), 404
    if repo.has_running_survey_run(survey_uid):
        # 正在跑的时候删工作区，等于让那次运行读到半个仓库
        return jsonify({"error": "该巡检正在运行，无法清理工作区"}), 409
    removed = delete_workspace(survey["slug"])
    logger.info("Admin cleared workspace: slug=%s removed=%s", survey["slug"], removed)
    return jsonify({"removed": removed})


@admin_bp.route("/api/admin/surveys/<survey_uid>/ignores", methods=["GET"])
@require_admin
def api_survey_ignores(survey_uid: str):
    """忽略清单。含指纹与理由，属于正文一侧，不对 Guest 开放。"""
    return jsonify({"items": repo.list_survey_ignores(survey_uid)})


@admin_bp.route("/api/admin/surveys/<survey_uid>/ignores", methods=["POST"])
@require_admin
def api_add_survey_ignore(survey_uid: str):
    """把一条发现标记为已知问题，后续巡检不再产出它。"""
    payload = request.get_json(silent=True) or {}
    fingerprint = str(payload.get("fingerprint") or "").strip()
    if not fingerprint:
        return jsonify({"error": "fingerprint 不能为空"}), 400
    if not repo.add_survey_ignore(survey_uid, fingerprint, str(payload.get("note") or "")):
        return jsonify({"error": "巡检不存在"}), 404
    return jsonify({"ignored": True}), 201


@admin_bp.route("/api/admin/surveys/<survey_uid>/ignores/<fingerprint>", methods=["DELETE"])
@require_admin
def api_remove_survey_ignore(survey_uid: str, fingerprint: str):
    """取消忽略。"""
    removed = repo.remove_survey_ignore(survey_uid, fingerprint)
    return jsonify({"removed": removed})


@admin_bp.route("/api/admin/survey-runs", methods=["GET"])
@require_survey_viewer
def api_survey_runs():
    """巡检运行列表。"""
    return jsonify(
        {
            "items": repo.list_survey_runs(
                survey_uid=(request.args.get("survey_uid") or "").strip(),
                limit=_int_arg("limit", 50, 200),
                stale_after_seconds=_stale_after(),
            )
        }
    )


@admin_bp.route("/api/admin/survey-runs/<run_uid>", methods=["GET"])
@require_survey_viewer
def api_survey_run_detail(run_uid: str):
    """
    单次巡检的完整报告。

    Guest 拿不到发现正文与整合叙述 —— 巡检正文描述的是整个代码库的架构与弱点，
    正文密度比 MR 审查更高，这条边界只会更严格，不会更松。
    """
    detail = repo.get_survey_run_detail(
        run_uid, include_body=is_admin(), stale_after_seconds=_stale_after()
    )
    if detail is None:
        return jsonify({"error": "运行记录不存在"}), 404
    return jsonify(detail)


@admin_bp.route("/api/admin/survey-runs/<run_uid>/export", methods=["GET"])
@require_survey_viewer
def api_export_survey_run(run_uid: str):
    """
    导出 Markdown 报告。

    导出走的是和界面同一份数据，因此 Guest 导出的报告同样不含正文 ——
    否则导出就成了绕过可见范围的后门。
    """
    detail = repo.get_survey_run_detail(
        run_uid, include_body=is_admin(), stale_after_seconds=_stale_after()
    )
    if detail is None:
        return jsonify({"error": "运行记录不存在"}), 404
    markdown = render_run_markdown(detail)
    return current_app.response_class(
        markdown,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="survey-{run_uid[:8]}.md"'},
    )


@admin_bp.route("/api/admin/survey-stats", methods=["GET"])
@require_survey_viewer
def api_survey_stats():
    """巡检聚合统计。不含任何正文。"""
    return jsonify(repo.survey_dashboard_stats(days=_window_days(90)))


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
