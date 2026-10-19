#!/usr/bin/env python3
"""
后台 API 与 SPA 托管。

接口分两档：require_admin（登录后可用）与 require_viewer（Guest 开关打开时也可用）。
放行 Guest 不等于返回全部内容 —— Finding 正文的剔除在**服务端**按身份完成，
前端隐藏不是安全边界，见 docs/adr/0002-guest-read-scope.md。
"""

import json
import logging
import time
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
from ..review.gitlab import get_user_by_username, search_users
from ..storage import repo
from ..storage.models import FIX_MIN_SEVERITIES, FIX_MODES, FIX_TRIGGER_BATCH, PUSH_TRIGGER_MANUAL
from ..survey.common import MAX_REPOS_LOWER_BOUND, MAX_REPOS_UPPER_BOUND, SurveyError, slugify
from ..survey.config import (
    FIX_MR_TITLE_PREFIX_MAX,
    SETTING_FIX_MR_TITLE_PREFIX,
    SETTING_MAX_REPOS,
    load_fix_mr_title_prefix,
    load_max_repos,
    load_public_url,
    load_survey_config,
)
from ..survey.destinations import DESTINATION_TYPES, list_destination_types, load_destinations
from ..survey.fix import (
    FixBatchBusy,
    attach_fixes,
    execute_survey_fix_in_background,
    fix_batch_lease_name,
    fix_unsupported_reason,
    project_path_from_url,
    start_fix_batch,
)
from ..survey.fix_cleanup import CLEANUP_CLEANED, MAX_CLEANUP_PER_REQUEST, cleanup_fixes
from ..survey.profile import codegraph_available
from ..survey.push import PushRejected, begin_pushes, check_destination, execute_pushes_in_background
from ..survey.report import render_run_markdown
from ..survey.schedule import describe_schedule, next_fire_time
from ..survey.scheduler import trigger_survey_now
from ..survey.sources import live_repo_candidates
from ..survey.workspace import delete_workspace, workspace_size_bytes
from .auth import (
    IDENTITY_ADMIN,
    SESSION_IDENTITY_KEY,
    SETTING_REPO_OWNER_GUEST_READ,
    SETTING_SURVEY_FIX_GUEST_READ,
    admin_console_available,
    current_identity,
    guest_read_enabled,
    guest_retry_enabled,
    is_admin,
    repo_owner_guest_read_enabled,
    require_admin,
    require_repo_owner_viewer,
    require_survey_fix_viewer,
    require_survey_viewer,
    require_viewer,
    survey_fix_guest_read_enabled,
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
    # 再嵌套在 survey_guest_read 之下，默认关闭：这两页的内容超出了 ADR-0002 划给游客的范围
    SETTING_SURVEY_FIX_GUEST_READ: {"type": "bool"},
    SETTING_REPO_OWNER_GUEST_READ: {"type": "bool"},
    SETTING_MAX_REPOS: {"type": "int", "min": MAX_REPOS_LOWER_BOUND, "max": MAX_REPOS_UPPER_BOUND},
    # 允许空串：表示修复合并请求的标题不加前缀
    SETTING_FIX_MR_TITLE_PREFIX: {"type": "str", "max_len": FIX_MR_TITLE_PREFIX_MAX},
}


def _writable_settings() -> dict:
    """可写子集的当前生效值；GET 与 PATCH 共用，避免两处返回的字段漂移。"""
    return {
        "guest_read": guest_read_enabled(),
        "guest_retry": guest_retry_enabled(),
        "survey_guest_read": survey_guest_read_enabled(),
        SETTING_SURVEY_FIX_GUEST_READ: survey_fix_guest_read_enabled(),
        SETTING_REPO_OWNER_GUEST_READ: repo_owner_guest_read_enabled(),
        SETTING_MAX_REPOS: load_max_repos(),
        SETTING_FIX_MR_TITLE_PREFIX: load_fix_mr_title_prefix(),
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


# 各代码平台的展示名称、合并请求叫法与网页路由。界面文案按它显示平台名，不写死 GitLab；
# 未知平台退回通用叫法且不生成链接，不能套用 GitLab 的路由
_PLATFORMS = {
    "gitlab": ("GitLab", "MR", "-/merge_requests"),
    "github": ("GitHub", "PR", "pull"),
}
_UNKNOWN_PLATFORM = ("代码平台", "合并请求", "")


def _platform() -> tuple:
    """当前配置的代码平台：(展示名称, 合并请求叫法, 合并请求网页路由)。"""
    return _PLATFORMS.get(load_gitlab_config().get("type", "gitlab"), _UNKNOWN_PLATFORM)


def _add_change_links(runs: list[dict]) -> list[dict]:
    """按配置平台补充合并请求名称与网页地址；未知平台或缺失路径时不生成链接。"""
    config = load_gitlab_config()
    _, label, route = _platform()
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
            # 只决定菜单显隐；三个开关要同时打开才对游客生效，组合判断在前端与 require_* 各做一次
            SETTING_SURVEY_FIX_GUEST_READ: survey_fix_guest_read_enabled(),
            SETTING_REPO_OWNER_GUEST_READ: repo_owner_guest_read_enabled(),
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
        return jsonify({"error": "无法读取合并请求当前状态，请稍后重试"}), 502
    if mr["state"] != "opened":
        return jsonify({"error": "仅允许对仍处于打开状态的合并请求重新触发"}), 409
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


def _interrupt_response(outcome: str, run_uid: str, run_label: str):
    """把确认中断的结果翻译成响应。ReviewRun、SurveyRun 与修复运行共用同一套语义。"""
    if outcome == repo.INTERRUPT_NOT_FOUND:
        return jsonify({"error": "运行记录不存在或已清理"}), 404
    if outcome == repo.INTERRUPT_NOT_STALE:
        # 包括心跳恰好在确认前一刻恢复的情况：它在动，就不该被关掉
        return jsonify({"error": "该运行已结束，或心跳已恢复、不再是疑似中断，请刷新后查看"}), 409
    logger.warning("Admin confirmed %s interrupted: run=%s", run_label, run_uid)
    return jsonify({"run_uid": run_uid, "status": "failed"})


@admin_bp.route("/api/admin/runs/<run_uid>/interrupt", methods=["POST"])
@require_admin
def api_interrupt_run(run_uid: str):
    """
    确认一个疑似中断的 ReviewRun 已中断，记为失败。

    只关闭记录，不停止执行：多进程下本进程碰不到别的进程里的线程。
    只对 Admin 开放，即使游客重新触发已开启 —— 关错了的代价是同一个 MR 上两次审查并发发评论。
    """
    outcome = repo.confirm_run_interrupted(run_uid, _stale_after())
    return _interrupt_response(outcome, run_uid, "review run")


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
            "writable": _writable_settings(),
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
    if not isinstance(payload, dict):
        return jsonify({"error": "设置必须为对象"}), 400
    unknown = [k for k in payload if k not in WRITABLE_SETTINGS]
    if unknown:
        return jsonify({"error": f"不可写的配置项: {', '.join(sorted(unknown))}"}), 400

    # 先整体校验再写入：一半写进去、一半被拒的部分成功，调用方无从得知哪些生效了
    for key, value in payload.items():
        spec = WRITABLE_SETTINGS[key]
        if spec["type"] == "bool" and type(value) is not bool:
            return jsonify({"error": f"{key} 必须为布尔值"}), 400
        if spec["type"] == "int":
            # bool 是 int 的子类，true 会被当成 1 放过去，所以用 type() 精确判断
            if type(value) is not int or not spec["min"] <= value <= spec["max"]:
                return jsonify({"error": f"{key} 必须为 {spec['min']}~{spec['max']} 之间的整数"}), 400
        if spec["type"] == "str":
            if not isinstance(value, str) or len(value.strip()) > spec["max_len"]:
                return jsonify({"error": f"{key} 必须为不超过 {spec['max_len']} 个字符的文本"}), 400
            # 换行、制表符会把合并请求标题拆坏，宁可拒绝也不悄悄改掉用户填的内容；首尾空白照常去掉
            if any(ch in value.strip() for ch in "\r\n\t"):
                return jsonify({"error": f"{key} 不能包含换行或制表符"}), 400

    for key, value in payload.items():
        if WRITABLE_SETTINGS[key]["type"] == "bool":
            repo.set_setting(key, "1" if value else "0")
        elif WRITABLE_SETTINGS[key]["type"] == "str":
            # 与读取时（load_fix_mr_title_prefix）的规整一致：连续空白压成一个，存的就是实际生效的
            repo.set_setting(key, " ".join(value.split()))
        else:
            repo.set_setting(key, str(value))
        logger.info("Admin changed setting %s -> %s", key, value)

    return jsonify({"writable": _writable_settings()})


# ---------------------------------------------------------------------------
# 定期巡检
#
# 读接口走 require_survey_viewer（Guest 需两个开关同时打开），
# 写接口一律 require_admin —— 触发一次巡检会拉几十 GB 代码并花掉真金白银。
# ---------------------------------------------------------------------------

MAX_SURVEY_SOURCES = 30
MAX_SURVEY_BINDINGS = 10
# 修复提示词的长度上限。它每次修复要随生成与复核各发一遍，写成长文档只会稀释"只改一个文件、只改必需代码"这些硬要求
MAX_FIX_PROMPT_CHARS = 2000
# 合并请求标题模板的长度上限。平台标题上限 255，留出至少 55 个字给问题标题，否则标题只剩模板、分不出是哪个问题
MAX_FIX_MR_TITLE_TEMPLATE_CHARS = 200


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

    if "fix_mode" in payload:
        mode = str(payload.get("fix_mode") or "").strip().lower()
        if mode not in FIX_MODES:
            raise ValueError(f"fix_mode 只能是 {' / '.join(FIX_MODES)}")
        fields["fix_mode"] = mode
    if "fix_min_severity" in payload:
        severity = str(payload.get("fix_min_severity") or "").strip().lower()
        if severity not in FIX_MIN_SEVERITIES:
            raise ValueError(f"fix_min_severity 只能是 {' / '.join(FIX_MIN_SEVERITIES)}")
        fields["fix_min_severity"] = severity
    if "fix_prompt" in payload:
        prompt = str(payload.get("fix_prompt") or "").strip()
        if len(prompt) > MAX_FIX_PROMPT_CHARS:
            # 不截断：截掉的可能恰好是一句"不要……"，模型照着半句话改，比报错难察觉得多
            raise ValueError(f"修复提示词不能超过 {MAX_FIX_PROMPT_CHARS} 个字符")
        fields["fix_prompt"] = prompt or None
    if "fix_mr_title_template" in payload:
        template = str(payload.get("fix_mr_title_template") or "").strip()
        if len(template) > MAX_FIX_MR_TITLE_TEMPLATE_CHARS:
            # 不截断：截掉的往往是末尾的需求链接，标题照样提出去，依赖链接的自动合并却静默失效
            raise ValueError(f"合并请求标题模板不能超过 {MAX_FIX_MR_TITLE_TEMPLATE_CHARS} 个字符")
        # 与全局前缀同样的规则：换行、制表符会把标题拆坏，宁可拒绝也不悄悄改掉
        if any(ch in template for ch in "\r\n\t"):
            raise ValueError("合并请求标题模板不能包含换行或制表符")
        # 与读取时（fix.mr_title）的规整一致：连续空白压成一个，存的就是实际生效的
        fields["fix_mr_title_template"] = " ".join(template.split()) or None

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


def _parse_bindings(raw, survey_name: str, current: list) -> list:
    """
    校验并规整 Binding 清单，返回 [{destination, target}]；非法时抛 ValueError。

    实例已从 config.yaml 删掉的 Binding 允许原样保留（绑定不因配置变动而丢失，推送时记失败），
    但不允许新建指向未配置实例的 Binding —— 那只会得到一条注定失败的推送。
    """
    if not isinstance(raw, list):
        raise ValueError("bindings 必须是数组")
    if len(raw) > MAX_SURVEY_BINDINGS:
        raise ValueError(f"输出目标不能超过 {MAX_SURVEY_BINDINGS} 个")

    destinations = load_destinations()
    kept = {
        (b["destination"], json.dumps(b["target"], sort_keys=True, ensure_ascii=False))
        for b in current or []
    }
    bindings = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("每个输出目标必须是对象")
        name = str(item.get("destination") or "").strip()
        if not name:
            raise ValueError("请选择输出目标")
        target = item.get("target") if isinstance(item.get("target"), dict) else {}

        info = destinations.get(name)
        if info is None or info.error:
            identity = (name, json.dumps(target, sort_keys=True, ensure_ascii=False))
            if identity in kept:
                bindings.append({"destination": name, "target": target})
                continue
            reason = info.error if info else "config.yaml 的 destinations 中没有这一项"
            raise ValueError(f"输出目标 {name} 不可用：{reason}")

        try:
            normalized = DESTINATION_TYPES[info.type_name].normalize_target(target, survey_name)
        except ValueError as e:
            raise ValueError(f"输出目标 {name}：{e}")
        bindings.append({"destination": name, "target": normalized})
    return bindings


def _decorate_survey(survey: dict) -> dict:
    """
    给巡检配置补上展示用的派生字段。

    Guest 看不到 Binding 的目标位置：表格 ID 本身就是访问入口，
    推送出去的内容又恰恰是 Guest 看不到的发现正文。实例名与类型可见，说明"结果推到了哪类地方"。
    Guest 也看不到修复提示词：它是 Admin 写给模型的要求，可能写着仓库内部的约定，不在 ADR-0002 划给游客的范围里。
    合并请求标题模板同理：里面通常是内部需求单的链接，属于配置而不是运行状态。
    """
    destinations = load_destinations()
    admin = is_admin()
    bindings = []
    for binding in survey.get("bindings") or []:
        info = destinations.get(binding["destination"])
        cls = DESTINATION_TYPES.get(info.type_name) if info else None
        item = {
            "id": binding["id"],
            "destination": binding["destination"],
            "destination_type": cls.type_name if cls else "",
            "destination_label": cls.label if cls else "",
            "available": bool(info and not info.error),
        }
        if admin:
            item["target"] = binding["target"]
            item["target_desc"] = cls.describe_target(binding["target"]) if cls else ""
            # 链接由目标位置推出，与目标位置同属 Admin 可见范围
            url = cls.target_url(binding["target"]) if cls else ""
            # 前端直接把它放进 href，插件若把用户填写的内容透传出来，javascript: 链接点一下就会执行
            item["target_url"] = url if url.startswith("https://") else ""
            item["last_succeeded_push_at"] = binding["last_succeeded_push_at"]
        bindings.append(item)
    decorated = {
        **survey,
        "bindings": bindings,
        "schedule_desc": describe_schedule(
            survey["schedule_kind"], survey["schedule_expr"], survey["timezone"]
        ),
        "workspace_bytes": workspace_size_bytes(survey["slug"]),
    }
    if not admin:
        decorated.pop("fix_prompt", None)
        decorated.pop("fix_mr_title_template", None)
    return decorated


@admin_bp.route("/api/admin/destinations", methods=["GET"])
@require_admin
def api_destinations():
    """
    config.yaml 里配置的 Destination 实例，以及各类型声明的目标位置字段。

    只返回实例名、类型与配置错误，不返回任何凭据相关的配置项。
    """
    types = {t["type"]: t for t in list_destination_types()}
    items = []
    for info in load_destinations().values():
        type_info = types.get(info.type_name)
        items.append(
            {
                "name": info.name,
                "type": info.type_name,
                "type_label": type_info["label"] if type_info else "",
                "summary": (
                    DESTINATION_TYPES[info.type_name].describe_options(info.options)
                    if info.type_name in DESTINATION_TYPES and not info.error else ""
                ),
                "target_fields": type_info["target_fields"] if type_info else [],
                "error": info.error,
            }
        )
    return jsonify({"items": items, "types": list(types.values()), "public_url": load_public_url()})


@admin_bp.route("/api/admin/destinations/<name>/check", methods=["POST"])
@require_admin
def api_check_destination(name: str):
    """
    连通性测试。body：{"target": {...}, "survey_name": "..."}，目标位置用表单里尚未保存的值。

    检查结果（包括失败）一律以 200 返回检查项：失败是测试的正常结论，不是接口错误。
    只有 Admin 能调：它会用服务账号访问用户填写的任意表格，并发起一次不改变内容的写请求。
    """
    payload = request.get_json(silent=True) or {}
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    survey_name = str(payload.get("survey_name") or "").strip()
    result = check_destination(name, target, survey_name)
    logger.info("Admin checked destination %s: ok=%s", name, result["ok"])
    return jsonify(result)


@admin_bp.route("/api/admin/surveys", methods=["GET"])
@require_survey_viewer
def api_surveys():
    """巡检配置列表。"""
    return jsonify({"items": [_decorate_survey(s) for s in repo.list_surveys()]})


@admin_bp.route("/api/admin/surveys", methods=["POST"])
@require_admin
def api_create_survey():
    """
    新建巡检。

    拷贝巡检也走这里：配置由前端从源巡检预填、经 Admin 核对后整份提交，与新建没有区别；
    额外带上 copy_ignores_from（源巡检的 survey_uid）时，把源巡检的忽略清单一并复制过来。
    忽略清单不在表单里，前端拿不到能原样提交的形状，只能由服务端复制。
    """
    payload = request.get_json(silent=True) or {}
    try:
        fields, sources = _parse_survey_payload(payload, partial=False)
    except (ValueError, SurveyError) as e:
        return jsonify({"error": str(e)}), 400
    ignores_from = str(payload.get("copy_ignores_from") or "").strip() or None

    try:
        bindings = _parse_bindings(payload.get("bindings") or [], fields["name"], [])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    next_run = next_fire_time(fields["schedule_kind"], fields["schedule_expr"], fields["timezone"])
    try:
        survey = repo.create_survey(
            name=fields["name"],
            slug=slugify(fields["name"], fallback="survey"),
            schedule_kind=fields["schedule_kind"],
            schedule_expr=fields["schedule_expr"],
            timezone_name=fields["timezone"],
            sources=sources,
            next_run_at=next_run,
            bindings=bindings,
            ignores_from=ignores_from,
            **{k: v for k, v in fields.items() if k not in {"name", "schedule_kind", "schedule_expr", "timezone"}},
        )
    except ValueError as e:
        # 源巡检不存在（通常是打开拷贝表单之后被删掉了）；整个创建已回滚，不会留下一个没有忽略清单的副本
        return jsonify({"error": str(e)}), 400
    logger.info(
        "Admin created survey: %s (slug=%s, ignores copied from %s)", survey["name"], survey["slug"], ignores_from or "-"
    )
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

    bindings = None
    if "bindings" in payload:
        try:
            bindings = _parse_bindings(payload["bindings"], merged["name"], current["bindings"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    survey = repo.update_survey(survey_uid, fields, sources, bindings)
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
    """忽略清单。含问题标题、理由与命中记录，属于正文一侧，不对 Guest 开放。"""
    survey = repo.get_survey(survey_uid)
    if survey is None:
        return jsonify({"error": "巡检不存在"}), 404
    return jsonify({"survey_name": survey["name"], "items": repo.list_survey_ignores(survey_uid)})


@admin_bp.route("/api/admin/surveys/<survey_uid>/ignores", methods=["POST"])
@require_admin
def api_add_survey_ignore(survey_uid: str):
    """把一条发现标记为已忽略问题：后续巡检由模型认出同一个问题并隐藏它，同一文件的其他问题照常报告。"""
    payload = request.get_json(silent=True) or {}
    try:
        finding_id = int(payload.get("finding_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "finding_id 不能为空"}), 400
    ignore_id = repo.add_survey_ignore(survey_uid, finding_id, str(payload.get("note") or ""))
    if ignore_id is None:
        return jsonify({"error": "巡检或发现不存在"}), 404
    return jsonify({"id": ignore_id}), 201


@admin_bp.route("/api/admin/surveys/<survey_uid>/ignores/<int:ignore_id>", methods=["DELETE"])
@require_admin
def api_remove_survey_ignore(survey_uid: str, ignore_id: int):
    """取消忽略。被它隐藏的历史发现恢复显示。"""
    removed = repo.remove_survey_ignore(survey_uid, ignore_id)
    return jsonify({"removed": removed})


@admin_bp.route("/api/admin/surveys/<survey_uid>/ignores/<int:ignore_id>", methods=["PATCH"])
@require_admin
def api_update_survey_ignore(survey_uid: str, ignore_id: int):
    """修改忽略理由。"""
    payload = request.get_json(silent=True) or {}
    if not repo.update_survey_ignore_note(survey_uid, ignore_id, str(payload.get("note") or "")):
        return jsonify({"error": "忽略记录不存在"}), 404
    return jsonify({"updated": True})


@admin_bp.route("/api/admin/surveys/<survey_uid>/ignored-repos", methods=["GET"])
@require_admin
def api_survey_ignored_repos(survey_uid: str):
    """已忽略仓库，以及可供标记的候选仓库（最近一次运行展开出的清单）。与忽略清单同一档，不对 Guest 开放。"""
    if repo.get_survey(survey_uid) is None:
        return jsonify({"error": "巡检不存在"}), 404
    candidates = repo.list_survey_repo_candidates(survey_uid)
    return jsonify({
        "items": repo.list_survey_ignored_repos(survey_uid),
        # 刷新按钮与说明里显示的平台名
        "platform_name": _platform()[0],
        "candidates": candidates["items"],
        "candidates_run_uid": candidates["run_uid"],
        "candidates_run_started_at": candidates["run_started_at"],
    })


@admin_bp.route("/api/admin/surveys/<survey_uid>/ignored-repos/live-candidates", methods=["GET"])
@require_admin
def api_survey_live_repo_candidates(survey_uid: str):
    """
    按当前配置访问代码平台实时展开来源，返回候选仓库与展开失败的来源。只在管理员手动点刷新时调用。

    结果不落库：它只是给管理员挑仓库用的清单，写进运行记录会冒充一次没发生过的巡检。
    """
    survey = repo.get_survey(survey_uid)
    if survey is None:
        return jsonify({"error": "巡检不存在"}), 404
    try:
        items, errors = live_repo_candidates(
            survey["sources"], load_max_repos(), repo.list_survey_ignored_repo_slugs(survey_uid)
        )
    except Exception as e:
        # 单个组织的失败已经在 errors 里了，走到这里是意料之外的错误；管理员可以改为手填地址
        logger.exception("Live repo candidates failed: survey=%s", survey_uid)
        return jsonify({"error": f"获取仓库清单失败：{e}"}), 502
    return jsonify({"items": items, "errors": errors})


@admin_bp.route("/api/admin/surveys/<survey_uid>/ignored-repos", methods=["POST"])
@require_admin
def api_add_survey_ignored_repo(survey_uid: str):
    """
    把仓库标记为不再巡检：下一轮起不拉取、不分析，台账里这些仓库的行转为已忽略。

    接收 urls（数组，一批共用一条理由）或单个 url。任何一个不合法时整批都不写入，返回 400。
    """
    payload = request.get_json(silent=True) or {}
    urls = payload.get("urls")
    if urls is None:
        urls = [payload.get("url")]
    if not isinstance(urls, list):
        return jsonify({"error": "urls 必须是数组"}), 400
    try:
        added = repo.add_survey_ignored_repos(survey_uid, urls, str(payload.get("note") or ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if added is None:
        return jsonify({"error": "巡检不存在"}), 404
    # 回传换算出的仓库身份：手填地址时，管理员要能当场核对它是不是报告里的那个仓库。
    # 单个添加时 id / repo_slug 沿用旧的响应字段
    return jsonify({"id": added[0]["id"], "repo_slug": added[0]["repo_slug"], "items": added}), 201


@admin_bp.route("/api/admin/surveys/<survey_uid>/ignored-repos/<int:item_id>", methods=["DELETE"])
@require_admin
def api_remove_survey_ignored_repo(survey_uid: str, item_id: int):
    """取消忽略一个仓库，下一轮起它重新参与巡检。"""
    return jsonify({"removed": repo.remove_survey_ignored_repo(survey_uid, item_id)})


@admin_bp.route("/api/admin/surveys/<survey_uid>/ignored-repos/<int:item_id>", methods=["PATCH"])
@require_admin
def api_update_survey_ignored_repo(survey_uid: str, item_id: int):
    """修改已忽略仓库的理由。"""
    payload = request.get_json(silent=True) or {}
    if not repo.update_survey_ignored_repo_note(survey_uid, item_id, str(payload.get("note") or "")):
        return jsonify({"error": "忽略记录不存在"}), 404
    return jsonify({"updated": True})


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
    admin = is_admin()
    detail = repo.get_survey_run_detail(
        run_uid, include_body=admin, stale_after_seconds=_stale_after()
    )
    if detail is None:
        return jsonify({"error": "运行记录不存在"}), 404
    if admin:
        _attach_survey_fixes(run_uid, detail)
        context = repo.get_survey_push_context(run_uid)
        latest = bool(context and context["is_latest_succeeded"])
        # 一键全部修复只对最近一次成功的运行开放：旧报告里的问题可能已经被新一轮复核过，按旧报告批量提合并请求没有意义
        detail["fix_all_candidates"] = (
            len(repo.list_survey_fix_candidates(run_uid, context["survey"]["fix_min_severity"])) if latest else 0
        )
        detail["fix_all_available"] = latest
        detail["fix_batch_running"] = bool(context) and repo.lease_active(fix_batch_lease_name(context["survey_id"]))
        detail["fix_min_severity"] = context["survey"]["fix_min_severity"] if context else ""
    # 推送记录与报告同源同权：Guest 看得到推送状态，看不到目标位置与错误信息
    detail["pushes"] = repo.list_survey_pushes(run_uid, include_detail=admin)
    context = repo.get_survey_push_context(run_uid)
    detail["pushable"] = bool(
        context and context["is_latest_succeeded"] and context["survey"]["bindings"]
    )
    return jsonify(detail)


@admin_bp.route("/api/admin/survey-runs/<run_uid>/interrupt", methods=["POST"])
@require_admin
def api_interrupt_survey_run(run_uid: str):
    """
    确认一个疑似中断的 SurveyRun 已中断，记为失败，该 Survey 的定时与手动执行随之恢复。

    同样只关闭记录：原运行若其实还活着，之后的结果不会发布，但它仍在占用同一个工作区。
    """
    outcome = repo.confirm_survey_run_interrupted(run_uid, _stale_after())
    return _interrupt_response(outcome, run_uid, "survey run")


@admin_bp.route("/api/admin/survey-runs/<run_uid>/push", methods=["POST"])
@require_admin
def api_push_survey_run(run_uid: str):
    """
    手动推送到输出目标。body 可带 binding_id 只推其中一个，不带则推全部。

    只有 Admin 能做，guest_retry 也不放开它：推送是对外部平台的写入。
    推送在后台线程执行，接口登记完推送记录即返回 202。
    """
    payload = request.get_json(silent=True) or {}
    binding_id = payload.get("binding_id")
    try:
        binding_id = int(binding_id) if binding_id not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "binding_id 必须是整数"}), 400

    try:
        result = begin_pushes(run_uid, PUSH_TRIGGER_MANUAL, binding_id)
    except PushRejected as e:
        return jsonify({"error": str(e)}), e.status

    if not result["started"]:
        return jsonify({"error": "该输出目标正在推送中，请稍后再试", "busy": result["busy"]}), 409
    execute_pushes_in_background(result["started"])
    logger.info("Admin triggered survey push: run=%s pushes=%s", run_uid, result["started"])
    return jsonify({"started": len(result["started"]), "busy": result["busy"]}), 202


@admin_bp.route("/api/admin/survey-runs/<run_uid>/export", methods=["GET"])
@require_survey_viewer
def api_export_survey_run(run_uid: str):
    """
    导出 Markdown 报告。

    导出走的是和界面同一份数据，因此 Guest 导出的报告同样不含正文 ——
    否则导出就成了绕过可见范围的后门。
    """
    admin = is_admin()
    detail = repo.get_survey_run_detail(
        run_uid, include_body=admin, stale_after_seconds=_stale_after()
    )
    if detail is None:
        return jsonify({"error": "运行记录不存在"}), 404
    if admin:
        _attach_survey_fixes(run_uid, detail)
    markdown = render_run_markdown(detail)
    return current_app.response_class(
        markdown,
        mimetype="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="survey-{run_uid[:8]}.md"'},
    )


def _attach_survey_fixes(run_uid: str, detail: dict) -> None:
    """
    给报告附上修复运行：整体列表放在 detail["fixes"]，每条发现挂上自己的（finding["fixes"]）。

    只对 Admin 调用：修复记录带着问题标题、作者与合并请求，属于正文一侧（ADR-0002）。
    """
    sections = ("findings", "resolved_findings", "unchecked_findings", "ignored_repo_findings")
    # 四栏都要查：问题被合并请求修掉之后，下一轮正好落进「已消失」，那时最需要看到它对应的合并请求
    issues = [item for key in sections for item in detail.get(key) or []]
    fixes = repo.list_survey_fixes_for_issues(detail.get("survey_uid") or "", issues, stale_after_seconds=_stale_after())
    detail["fixes"] = fixes
    for key in sections:
        attach_fixes(detail.get(key) or [], fixes)


# 登记修复运行被拒绝时的响应
_FIX_START_ERRORS = {
    repo.FIX_START_NOT_FOUND: ("发现不存在", 404),
    repo.FIX_START_IGNORED: ("这条发现已在忽略清单里", 409),
    repo.FIX_START_NO_REPO: ("找不到这条发现所在仓库的地址", 409),
    repo.FIX_START_BUSY: ("这个问题已有正在进行的修复", 409),
}


@admin_bp.route("/api/admin/survey-findings/<int:finding_id>/fix", methods=["POST"])
@require_admin
def api_fix_survey_finding(finding_id: int):
    """
    针对一条巡检发现发起修复：生成修改、本地验证、在代码平台上提出合并请求并指派处理人。

    只有 Admin 能做，guest_retry 也不放开它：推分支、提合并请求都是对外部平台的写入。
    修复在后台线程执行，接口登记完即返回 202。
    """
    unsupported = fix_unsupported_reason()
    if unsupported:
        return jsonify({"error": unsupported}), 400
    outcome, run_uid = repo.start_survey_fix(finding_id)
    if outcome != repo.FIX_START_OK:
        message, status = _FIX_START_ERRORS[outcome]
        return jsonify({"error": message}), status
    execute_survey_fix_in_background(run_uid)
    logger.info("Admin triggered survey fix: finding=%s fix=%s", finding_id, run_uid)
    return jsonify({"run_uid": run_uid}), 202


@admin_bp.route("/api/admin/survey-runs/<run_uid>/fix-all", methods=["POST"])
@require_admin
def api_fix_all_survey_run(run_uid: str):
    """
    一键全部修复：为这一轮尚未修复过、且不低于巡检配置的严重度门槛的问题批量发起修复，后台逐个执行。

    只对该巡检最近一次成功的运行开放。每批有上限，返回本批排队的数量与候选总数，剩下的再点一次接着处理。
    """
    unsupported = fix_unsupported_reason()
    if unsupported:
        return jsonify({"error": unsupported}), 400
    context = repo.get_survey_push_context(run_uid)
    if context is None:
        return jsonify({"error": "运行记录不存在"}), 404
    if not context["is_latest_succeeded"]:
        return jsonify({"error": "只能对该巡检最近一次成功的运行一键修复"}), 409
    total = len(repo.list_survey_fix_candidates(run_uid, context["survey"]["fix_min_severity"]))
    try:
        batch = start_fix_batch(run_uid, FIX_TRIGGER_BATCH)
    except FixBatchBusy as e:
        return jsonify({"error": str(e)}), 409
    logger.info("Admin triggered fix-all: run=%s queued=%s total=%s", run_uid, len(batch), total)
    return jsonify({"queued": len(batch), "total": total}), 202


# 处理人筛选最多带多少个用户名：多选框里勾几十个人已经失去筛选的意义，上限只为挡住拼出来的超长查询
MAX_ASSIGNEE_FILTERS = 50


def _strip_fix_for_guest(fix: dict) -> dict:
    """
    Guest 视角的修复记录：返回去掉原始作者邮箱与问题正文的副本。

    开关打开即表示管理员同意游客看修复本身（标题、diff、验证过程），但两样东西仍然剔除：
    邮箱是个人信息，看懂一次修复用不到它；问题正文就是发现正文，ADR-0002 的硬性边界不因这个开关松动。
    原始作者的姓名保留，处理人一栏本来就显示具体的人。
    """
    return {k: v for k, v in fix.items() if k not in ("author_email", "body")}


@admin_bp.route("/api/admin/survey-fixes", methods=["GET"])
@require_survey_fix_viewer
def api_survey_fix_merge_requests():
    """
    修复提出的合并请求列表（跨巡检），可按巡检、处理人（可多选，含未指派）、关键字与清理状态（cleaned=no / yes）筛选。

    修复记录带问题标题、文件路径与处理人，属于正文一侧（见 ADR-0002）：
    默认只对 Admin，Guest 要等管理员打开「游客可见修复」。
    """
    result = repo.list_fix_merge_requests(
        survey_uid=(request.args.get("survey_uid") or "").strip(),
        # 逗号分隔而不是重复参数：前端请求库默认把数组序列化成 assignee[]=a 的形式，两边容易对不上；
        # 平台用户名不含逗号
        assignees=(request.args.get("assignees") or "").split(",")[:MAX_ASSIGNEE_FILTERS],
        keyword=(request.args.get("q") or "").strip()[:200],
        limit=_int_arg("limit", 50, 200),
        offset=_int_arg("offset", 0, 100000),
        stale_after_seconds=_stale_after(),
        cleaned=(request.args.get("cleaned") or "").strip().lower(),
    )
    if not is_admin():
        result["items"] = [_strip_fix_for_guest(item) for item in result["items"]]
    return jsonify(result)


@admin_bp.route("/api/admin/survey-fixes/<run_uid>", methods=["GET"])
@require_survey_fix_viewer
def api_survey_fix_detail(run_uid: str):
    """
    单次修复运行的完整记录：修改的 diff、每轮验证、原始作者与处理人、合并请求。

    合并请求说明里的「修复运行详情」链接指向这里，点进来的多半是没有后台账号的处理人，
    这正是游客开关存在的理由；Guest 看到的与 Admin 相同，只少原始作者的邮箱与问题正文。
    """
    fix = repo.get_survey_fix(run_uid, stale_after_seconds=_stale_after())
    if fix is None:
        return jsonify({"error": "修复记录不存在"}), 404
    fix.pop("survey_id", None)
    return jsonify(fix if is_admin() else _strip_fix_for_guest(fix))


@admin_bp.route("/api/admin/survey-fixes/<run_uid>/interrupt", methods=["POST"])
@require_admin
def api_interrupt_survey_fix(run_uid: str):
    """
    确认一个疑似中断的修复运行已中断，记为失败，同一个问题随之可以重新发起修复。

    同样只关闭记录：原运行若其实还活着、还没走到提交阶段，它不会再推分支或提合并请求；
    已经在提交的那一步照常完成，推上去的分支与合并请求留在平台上。
    """
    outcome = repo.confirm_survey_fix_interrupted(run_uid, _stale_after())
    return _interrupt_response(outcome, run_uid, "survey fix")


@admin_bp.route("/api/admin/survey-fixes/cleanup", methods=["POST"])
@require_admin
def api_cleanup_survey_fixes():
    """
    清理修复运行：关闭合并请求、删除修复分支，记录保留并标为已清理。入参 {"run_uids": [...]}，单条清理也走这里。

    同步执行、逐条返回结果（cleaned / rejected / failed）：清理是对外可见的动作，哪条没清掉、为什么，要当场告诉操作的人。
    """
    payload = request.get_json(silent=True) or {}
    run_uids = payload.get("run_uids")
    if not isinstance(run_uids, list) or not run_uids:
        return jsonify({"error": "run_uids 必须是非空数组"}), 400
    run_uids = [str(u).strip() for u in run_uids if str(u or "").strip()]
    if len(run_uids) > MAX_CLEANUP_PER_REQUEST:
        return jsonify({"error": f"一次最多清理 {MAX_CLEANUP_PER_REQUEST} 条"}), 400
    results = cleanup_fixes(run_uids)
    logger.info(
        "Admin cleaned survey fixes: requested=%s cleaned=%s", len(run_uids),
        sum(1 for r in results if r["outcome"] == CLEANUP_CLEANED),
    )
    return jsonify({"results": results})


def _owner_path_from_input(raw) -> str:
    """
    填写的项目或组织路径；可以是整条地址，剥成路径。空白返回空串，交给存储层报错。

    从浏览器地址栏粘贴的地址常带着页面部分：组织页是 /groups/<组织>，项目里的页面是 <项目>/-/tree/main 之类。
    GitLab 不允许顶层组织叫 groups，"-" 也不会是路径里的一段，所以两者都可以放心剥掉；不剥的话会存下一条
    永远匹配不上任何项目的配置，而且不报错。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    path = project_path_from_url(text, load_gitlab_config().get("url", ""))
    path = path.split("/-/", 1)[0]
    if path.endswith("/-"):
        path = path[:-2]
    return path[len("groups/"):] if path.lower().startswith("groups/") else path


def _split_list_input(raw) -> list:
    """把逗号（含全角）或换行分隔的字符串拆成列表，去掉空项；本来就是列表的原样返回，其他类型返回 None。"""
    if isinstance(raw, str):
        return [part for part in raw.replace("，", ",").replace("\n", ",").split(",") if part.strip()]
    return raw if isinstance(raw, list) else None


def _parse_repo_owner_payload(payload: dict) -> tuple:
    """
    校验仓库负责人配置的入参，返回 (路径, 用户名列表, 备注)。路径可以填整条仓库地址或组织地址。

    用户名在这里就清洗好：查姓名用的键必须和存进去的用户名一模一样，带着 @ 或空格去查，查到了也对不上。
    """
    path = _owner_path_from_input(payload.get("path"))
    raw_names = _split_list_input(payload.get("usernames"))
    if raw_names is None:
        raise ValueError("usernames 必须是数组")
    usernames = [n for n in (repo.clean_owner_username(n) for n in raw_names) if n]
    return path, usernames, str(payload.get("note") or "")


# 查负责人姓名的单次超时。查的是展示用的名字，平台慢时宁可这次不显示，也不能让配置页卡上半分钟
OWNER_NAME_LOOKUP_TIMEOUT = 5
# 一次请求里查姓名的总时长上限（秒）。单次超时挡不住"每个都慢、但都没超时"，补查 20 个人会让列表等上一分多钟
OWNER_NAME_LOOKUP_BUDGET = 8
# 加载列表时一次最多补查几个人的姓名，挡住配置很多、又都没有快照时的一长串请求；剩下的下次加载再补
OWNER_NAME_BACKFILL_MAX = 20


def _lookup_owner_names(usernames) -> dict:
    """
    向代码平台按用户名查姓名，返回 {用户名: 姓名}；平台上查无此人的姓名为空串，没查成的（出错、超时）不在结果里。

    姓名只用于展示，任何失败都不能挡住保存或列表：平台不是 GitLab 时直接不查（精确按用户名查用户只接了 GitLab）；
    出任何错就停，超过总时长也停——平台不可达或很慢时每个人各等一次只会让页面更慢，查到的结果一样是空。
    """
    if load_gitlab_config().get("type") != "gitlab":
        return {}
    found = {}
    deadline = time.monotonic() + OWNER_NAME_LOOKUP_BUDGET
    for username in dict.fromkeys(usernames):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            # 单次超时也不超过剩余预算，否则最后一次请求会把总时长拖过上限
            user = get_user_by_username(username, timeout=min(OWNER_NAME_LOOKUP_TIMEOUT, remaining))
        except Exception:
            # 后果只是这次显示用户名：保存照常，下次加载列表还会再补查
            logger.warning("Looking up platform user failed: username=%s", username, exc_info=True)
            break
        found[username] = str(user.get("name") or "")
    return found


@admin_bp.route("/api/admin/repo-owners", methods=["GET"])
@require_repo_owner_viewer
def api_repo_owners():
    """
    仓库负责人配置。含具体的人，默认只对 Admin，Guest 要等管理员打开「游客可见负责人」。

    还没有姓名快照的负责人（姓名快照出现之前保存的配置，或保存时没查到）在这里补查并写回，
    之后的加载直接读快照。GET 带写入是有意的：写的只是展示缓存，重复执行结果相同。
    平台上查无此人的也记进快照，不再反复查。
    """
    # 补查只由 Admin 触发：游客无需登录，放开后任何人刷新列表都能让服务端去代码平台连查一串用户，
    # 而游客少看到几个姓名的代价只是暂时显示用户名
    pending = repo.repo_owner_usernames_to_look_up()[:OWNER_NAME_BACKFILL_MAX] if is_admin() else []
    found = _lookup_owner_names(pending) if pending else {}
    if found:
        repo.fill_repo_owner_names(found)
    return jsonify({"items": repo.list_repo_owners()})


def _save_repo_owner_from_request(owner_id=None):
    """新增（owner_id 为空）或修改一条配置的公共部分：解析入参、查姓名、保存。入参不合法时抛 ValueError。"""
    path, usernames, note = _parse_repo_owner_payload(request.get_json(silent=True) or {})
    # 路径或负责人缺失时存储层会拒绝，先别去平台白查一趟
    names = _lookup_owner_names(usernames) if path and usernames else {}
    return repo.save_repo_owner(path, usernames, note, owner_id=owner_id, user_names=names)


@admin_bp.route("/api/admin/repo-owners", methods=["POST"])
@require_admin
def api_add_repo_owner():
    """新增一条仓库负责人配置；同一路径已有配置时覆盖它。"""
    try:
        item = _save_repo_owner_from_request()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(item), 201


@admin_bp.route("/api/admin/repo-owners/batch", methods=["POST"])
@require_admin
def api_add_repo_owner_paths():
    """
    按负责人填写：把一个人加为多个项目或组织的负责人。

    已有配置的路径把这个人追加到末尾，没有的新建；返回各路径的处理结果。
    paths 可以是数组，也可以是换行或逗号分隔的字符串，每一项都可以是整条地址。
    """
    payload = request.get_json(silent=True) or {}
    raw_paths = _split_list_input(payload.get("paths"))
    if raw_paths is None:
        return jsonify({"error": "paths 必须是数组"}), 400
    username = repo.clean_owner_username(payload.get("username"))
    try:
        paths = [_owner_path_from_input(p) for p in raw_paths]
        # 存储层一定会拒绝的入参先挡住，别去平台白查一趟
        valid = username and paths and all(paths) and len(paths) <= repo.REPO_OWNER_BATCH_MAX
        name = _lookup_owner_names([username]).get(username) if valid else None
        result = repo.add_repo_owner_paths(username, paths, str(payload.get("note") or ""), name=name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(result)


@admin_bp.route("/api/admin/repo-owners/<int:owner_id>", methods=["PATCH"])
@require_admin
def api_update_repo_owner(owner_id: int):
    """修改一条仓库负责人配置。"""
    try:
        item = _save_repo_owner_from_request(owner_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if item is None:
        return jsonify({"error": "配置不存在"}), 404
    return jsonify(item)


@admin_bp.route("/api/admin/repo-owners/<int:owner_id>", methods=["DELETE"])
@require_admin
def api_delete_repo_owner(owner_id: int):
    """删除一条仓库负责人配置。之后这个仓库在原始作者不可用时，合并请求不指派处理人。"""
    return jsonify({"removed": repo.delete_repo_owner(owner_id)})


# 用户搜索关键字的长度上限，只为挡住拼出来的超长查询
MAX_USER_SEARCH_CHARS = 100


@admin_bp.route("/api/admin/platform-users", methods=["GET"])
@require_admin
def api_platform_users():
    """
    按关键字（用户名、姓名；普通令牌下邮箱只能搜到公开了的）搜索代码平台上的用户，给仓库负责人配置当候选项。

    负责人按用户名匹配，手打的用户名拼错了要到发起修复时才会发现（那时只会默默跳过这个人），所以让人从平台上选。
    只回用户名、姓名、账号状态与是否机器人，不回邮箱：候选框用不到它。
    """
    query = (request.args.get("q") or "").strip()[:MAX_USER_SEARCH_CHARS]
    if not query:
        return jsonify({"items": []})
    unsupported = fix_unsupported_reason()
    if unsupported:
        return jsonify({"error": unsupported}), 409
    try:
        users = search_users(query)
    except Exception:
        # 搜不到只影响候选项，配置本身照常可以保存；报错原文可能带内网地址，只记日志
        logger.warning("Searching platform users failed: q=%s", query, exc_info=True)
        return jsonify({"error": "搜索代码平台用户失败，请稍后重试"}), 502
    items = [
        {
            "username": str(u.get("username") or ""),
            "name": str(u.get("name") or ""),
            "state": str(u.get("state") or "active"),
            "bot": bool(u.get("bot")),
        }
        for u in users if u.get("username")
    ]
    return jsonify({"items": items})


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
