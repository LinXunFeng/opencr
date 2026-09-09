#!/usr/bin/env python3
"""
OpenCR - 自动代码审查服务（入口与路由）
优先读取 config.yaml，支持环境变量覆盖，兼容 ~/.codex 回退

审查执行逻辑在 backend/review/runner.py，采纳结算在 backend/review/settlement.py，
本文件只负责 HTTP 路由、线程调度与后台任务。
"""

import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request


def _load_review_dependencies() -> dict:
    """
    统一加载 review 依赖。
    脚本模式（cd backend && python3 review_server.py）下先补齐项目根路径。
    """
    if __package__ in {None, ""}:
        project_root = str(Path(__file__).resolve().parent.parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

    from backend.admin.routes import admin_bp
    from backend.review.ai import build_review_prompt, call_codex_review, review_changes, review_changes_with_inline_notes
    from backend.review.common import (
        DEFAULT_REVIEW_SKILLS_DIR,
        REVIEW_MODE_FILE,
        REVIEW_MODE_HYBRID,
        REVIEW_MODE_OVERALL,
        ReviewError,
        normalize_review_mode,
        normalize_review_skill,
    )
    from backend.admin.auth import ensure_password_hashed, get_secret_key
    from backend.review.config import (
        _pick_config_value,
        get_app_version,
        load_admin_config,
        load_file_config,
        load_gitlab_config,
        load_openai_config,
        load_review_config,
        load_storage_config,
        resolve_active_config_path,
    )
    from backend.review.diff import build_diff_from_changes, normalize_change_diff, truncate_diff
    from backend.review.gitlab import (
        enrich_changes_with_file_info,
        get_compare_changes,
        get_mr_changes,
        get_mr_changes_with_refs,
        get_mr_diff,
        get_mr_discussions,
        get_mr_state,
        post_mr_comment,
        post_mr_file_comment,
        post_mr_inline_comment,
        require_gitlab_config,
    )
    from backend.review.runner import execute_review_run
    from backend.review.settlement import settle_mr
    from backend.review.skills import (
        _parse_selected_skill,
        auto_select_review_skill,
        auto_select_review_skills,
        load_available_review_skills,
        load_review_skill_previews,
        load_review_skill_prompt,
        load_review_skill_prompts,
        resolve_review_options,
    )
    from backend.storage import repo
    from backend.storage.models import (
        TRIGGER_MANUAL,
        TRIGGER_WEBHOOK_OPEN,
        TRIGGER_WEBHOOK_UPDATE,
    )

    return {
        "admin_bp": admin_bp,
        "ensure_password_hashed": ensure_password_hashed,
        "get_secret_key": get_secret_key,
        "resolve_active_config_path": resolve_active_config_path,
        "build_review_prompt": build_review_prompt,
        "call_codex_review": call_codex_review,
        "review_changes": review_changes,
        "review_changes_with_inline_notes": review_changes_with_inline_notes,
        "DEFAULT_REVIEW_SKILLS_DIR": DEFAULT_REVIEW_SKILLS_DIR,
        "REVIEW_MODE_FILE": REVIEW_MODE_FILE,
        "REVIEW_MODE_HYBRID": REVIEW_MODE_HYBRID,
        "REVIEW_MODE_OVERALL": REVIEW_MODE_OVERALL,
        "ReviewError": ReviewError,
        "normalize_review_mode": normalize_review_mode,
        "normalize_review_skill": normalize_review_skill,
        "_pick_config_value": _pick_config_value,
        "get_app_version": get_app_version,
        "load_admin_config": load_admin_config,
        "load_file_config": load_file_config,
        "load_gitlab_config": load_gitlab_config,
        "load_openai_config": load_openai_config,
        "load_review_config": load_review_config,
        "load_storage_config": load_storage_config,
        "build_diff_from_changes": build_diff_from_changes,
        "normalize_change_diff": normalize_change_diff,
        "truncate_diff": truncate_diff,
        "enrich_changes_with_file_info": enrich_changes_with_file_info,
        "get_mr_changes": get_mr_changes,
        "get_mr_changes_with_refs": get_mr_changes_with_refs,
        "get_compare_changes": get_compare_changes,
        "get_mr_diff": get_mr_diff,
        "get_mr_discussions": get_mr_discussions,
        "get_mr_state": get_mr_state,
        "post_mr_comment": post_mr_comment,
        "post_mr_file_comment": post_mr_file_comment,
        "post_mr_inline_comment": post_mr_inline_comment,
        "require_gitlab_config": require_gitlab_config,
        "execute_review_run": execute_review_run,
        "settle_mr": settle_mr,
        "repo": repo,
        "TRIGGER_MANUAL": TRIGGER_MANUAL,
        "TRIGGER_WEBHOOK_OPEN": TRIGGER_WEBHOOK_OPEN,
        "TRIGGER_WEBHOOK_UPDATE": TRIGGER_WEBHOOK_UPDATE,
        "_parse_selected_skill": _parse_selected_skill,
        "auto_select_review_skill": auto_select_review_skill,
        "auto_select_review_skills": auto_select_review_skills,
        "load_available_review_skills": load_available_review_skills,
        "load_review_skill_previews": load_review_skill_previews,
        "load_review_skill_prompt": load_review_skill_prompt,
        "load_review_skill_prompts": load_review_skill_prompts,
        "resolve_review_options": resolve_review_options,
    }


globals().update(_load_review_dependencies())

# 兼容旧私有函数名（供历史调用/测试）
# 旧测试/历史调用可能仍引用该私有别名，保留以避免破坏兼容性。
_normalize_review_mode = normalize_review_mode
_normalize_review_skill = normalize_review_skill
_normalize_change_diff = normalize_change_diff


def _build_log_handlers() -> list:
    """
    日志落盘目录不存在时自动创建。

    容器里 ~/opencr/logs 不会预先存在，缺了这一步服务会在 import 阶段就崩。
    仍然失败时退回仅标准输出 —— 拿不到文件日志也好过起不来。
    """
    handlers = [logging.StreamHandler(sys.stdout)]
    log_path = Path(os.getenv("OPENCR_LOG_FILE", "~/opencr/logs/server.log")).expanduser()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))
    except OSError as e:
        print(f"[opencr] file logging disabled ({log_path}): {e}", file=sys.stderr)
    return handlers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=_build_log_handlers(),
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.register_blueprint(admin_bp)


def _init_admin_console() -> None:
    """
    初始化后台：校验配置、准备密码哈希、装配 session。

    启用后台却没配密码时直接拒绝启动 —— 静默放行等于把一个无鉴权的管理面板
    挂在 webhook 端口上，而这个端口按部署方式通常是内网可达的。
    """
    cfg = load_admin_config()
    if not cfg["enabled"]:
        return

    if not cfg["password"]:
        raise RuntimeError(
            "admin.enabled=true 但 admin.password 为空。"
            "请在 config.yaml 的 admin 段填写密码（直接写明文，首次启动后会自动替换为哈希），"
            "或设置 OPENCR_ADMIN_PASSWORD 环境变量。"
        )

    # 明文密码会在这里被哈希并就地写回 config.yaml；写不进去也不中断启动
    app.config["ADMIN_PASSWORD_HASH"] = ensure_password_hashed()
    # session 密钥持久化在数据库里：每次启动随机生成会让重启和多 worker 互相踢下线
    app.secret_key = get_secret_key()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.permanent_session_lifetime = timedelta(days=7)


_init_admin_console()


def validate_webhook_token(f):
    """Webhook 安全验证装饰器"""

    @wraps(f)
    def decorated(*args, **kwargs):
        """未配置 webhook_secret 时不做校验，保持与旧版本的兼容行为。"""
        secret_token = load_gitlab_config()["webhook_secret"]
        if secret_token:
            header_token = request.headers.get("X-Gitlab-Token")
            if header_token != secret_token:
                logger.warning("Invalid webhook token")
                return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated


def _is_merge_commit_update(attrs: dict) -> bool:
    """判断 MR update 是否由 merge commit 引起。"""
    last_commit = attrs.get("last_commit")
    if not isinstance(last_commit, dict):
        return False

    message = str(last_commit.get("message") or last_commit.get("title") or "").strip().lower()
    if not message:
        return False

    return bool(re.match(r"^merge (branch|remote-tracking branch|commit|pull request)\b", message))


def should_review_mr(data: dict) -> tuple:
    """判断是否应该审查此 MR，并返回建议审查模式。"""
    attrs = data.get("object_attributes", {})

    state = (attrs.get("state") or "").strip().lower()
    if state and state != "opened":
        return False, f"忽略 state={state} 的 MR（仅处理 opened）", ""

    action = (attrs.get("action") or "").strip().lower()
    if action == "open":
        target_mode = REVIEW_MODE_HYBRID
        action_reason = "MR 创建，触发整体+文件级审查"
    elif action == "reopen":
        return False, "忽略 action=reopen（关闭后重新开启不触发审查）", ""
    elif action == "update":
        oldrev = str(attrs.get("oldrev") or "").strip().lower()
        is_commit_update = bool(re.fullmatch(r"[0-9a-f]{40}", oldrev) and oldrev != ("0" * 40))
        if not is_commit_update:
            return False, f"忽略 action=update（非新提交触发）", ""
        if _is_merge_commit_update(attrs):
            return False, "忽略 merge commit 导致的 MR 更新", ""
        target_mode = REVIEW_MODE_FILE
        action_reason = f"检测到 MR 新提交 oldrev={oldrev[:8]}，触发文件级审查"
    else:
        return False, f"忽略 action={action or '<empty>'} 的 MR", ""

    title = attrs.get("title", "").lower()
    skip_keywords = ["wip", "draft", "skip-review", "[skip ci]"]
    for kw in skip_keywords:
        if kw in title:
            return False, f"标题包含跳过标记: {kw}", ""

    source_branch = attrs.get("source_branch", "")
    if source_branch.startswith("dependabot/"):
        return False, "跳过依赖更新 MR", ""

    return True, action_reason, target_mode


def _is_settlement_event(data: dict) -> str:
    """
    判断这次 webhook 是否是 MR 到达终态。返回终态名，否则空串。

    合并/关闭事件原先在 should_review_mr 的第一行就被挡掉了，
    结算必须插在它之前 —— 这是采纳统计唯一的触发时机。
    """
    attrs = data.get("object_attributes", {})
    action = (attrs.get("action") or "").strip().lower()
    state = (attrs.get("state") or "").strip().lower()
    if action == "merge" or state == "merged":
        return "merged"
    if action == "close" or state == "closed":
        return "closed"
    return ""


def _is_reviewable_event(data: dict) -> bool:
    """
    这次事件本来有没有可能触发审查。

    只有 opened 状态下的 open/update 才值得为"被跳过"留痕（WIP、dependabot、merge commit 等）。
    merge/close/reopen 不被审查是理所当然的，记下来只会把面板刷满噪音。
    """
    attrs = data.get("object_attributes", {})
    state = (attrs.get("state") or "").strip().lower()
    action = (attrs.get("action") or "").strip().lower()
    return state in {"", "opened"} and action in {"open", "update"}


@app.route("/health", methods=["GET"])
def health_check():
    """健康检查端点"""
    cfg = load_openai_config()
    return jsonify(
        {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "version": get_app_version(),
            "model": cfg.get("model", "unknown"),
            "base_url": cfg.get("base_url", "unknown"),
        }
    )


def process_review_async(
    run_uid,
    project_id,
    mr_iid,
    mr_title,
    review_mode,
    review_skill,
    action="",
    update_from_sha="",
    update_to_sha="",
    log_prefix="[Async]",
    original_input=None,
):
    """后台线程入口：直接委托给统一的 ReviewRun 执行器。"""
    execute_review_run(
        run_uid=run_uid,
        project_id=project_id,
        mr_iid=mr_iid,
        mr_title=mr_title,
        review_mode=review_mode,
        review_skill=review_skill,
        action=action,
        update_from_sha=update_from_sha,
        update_to_sha=update_to_sha,
        log_prefix=log_prefix,
        original_input=original_input,
    )


def _start_review_thread(**kwargs) -> None:
    """把一次 ReviewRun 丢到后台线程，让 HTTP 请求立刻返回。"""
    thread = threading.Thread(target=process_review_async, kwargs=kwargs)
    thread.daemon = True
    thread.start()


# 通过回调复用线程调度，后台蓝图不反向导入 HTTP 服务模块。
app.config["START_REVIEW_THREAD"] = _start_review_thread


def _settle_async(project_id: int, mr_iid: int, mr_state: str) -> None:
    """
    异步结算一个已到终态的 MR。

    结算要逐条查 award emoji，一个有 20 条发现的 MR 就是 20 次 API 调用；
    放在 webhook 请求里同步做会让 GitLab 侧超时重发。
    """

    def _run():
        """结算失败只记录不重抛：兜底的 reconciler 下一轮还会再试。"""
        try:
            settle_mr(project_id, mr_iid, mr_state)
        except Exception:
            logger.exception("Settlement failed for MR !%s", mr_iid)

    thread = threading.Thread(target=_run)
    thread.daemon = True
    thread.start()


@app.route("/webhook", methods=["POST"])
@validate_webhook_token
def handle_webhook():
    """处理 GitLab Webhook - 立即返回，后台处理"""
    data = request.json

    if not data:
        return jsonify({"error": "No JSON payload"}), 400

    event_type = data.get("object_kind")
    if event_type != "merge_request":
        return jsonify({"message": f"Ignored event type: {event_type}"}), 200

    attrs = data.get("object_attributes", {})
    project = data.get("project", {})

    project_id = project.get("id")
    project_path = str(project.get("path_with_namespace") or "")
    mr_iid = attrs.get("iid")
    mr_title = attrs.get("title")

    logger.info(
        "Received MR webhook: project_id=%s, mr_iid=%s, title=%s, action=%s",
        project_id,
        mr_iid,
        mr_title,
        attrs.get("action"),
    )

    # 结算必须在 should_review_mr 之前 —— 后者会把所有非 opened 的 MR 直接挡掉
    settlement_state = _is_settlement_event(data)
    if settlement_state and project_id and mr_iid:
        logger.info("MR !%s reached %s, scheduling settlement", mr_iid, settlement_state)
        _settle_async(int(project_id), int(mr_iid), settlement_state)

    should_review, reason, trigger_mode = should_review_mr(data)
    if not should_review:
        logger.info(f"Skipping MR !{mr_iid}: {reason}")
        if project_id and mr_iid and _is_reviewable_event(data):
            try:
                repo.record_skipped_run(
                    project_id=int(project_id),
                    mr_iid=int(mr_iid),
                    trigger=(
                        TRIGGER_WEBHOOK_OPEN
                        if (attrs.get("action") or "").strip().lower() == "open"
                        else TRIGGER_WEBHOOK_UPDATE
                    ),
                    skip_reason=reason,
                    mr_title=mr_title or "",
                    project_path=project_path,
                )
            except Exception:
                logger.warning("Failed to record skipped run for MR !%s", mr_iid, exc_info=True)
        return jsonify({"message": f"Skipped: {reason}"}), 200

    review_mode, review_skill = resolve_review_options(
        data,
        default_skill="",
        manual_mode=trigger_mode,
    )
    action = (attrs.get("action") or "").strip().lower()
    update_from_sha = ""
    update_to_sha = ""
    if action == "update":
        update_from_sha = str(attrs.get("oldrev") or "").strip()
        last_commit = attrs.get("last_commit")
        if isinstance(last_commit, dict):
            update_to_sha = str(last_commit.get("id") or last_commit.get("sha") or "").strip()
        elif isinstance(last_commit, str):
            update_to_sha = last_commit.strip()
        if not update_to_sha:
            update_to_sha = str(attrs.get("newrev") or "").strip()
    logger.info(
        "Webhook resolved review options: action=%s, trigger_mode=%s, final_mode=%s, skill=%s, "
        "reason=%s, update_from=%s, update_to=%s",
        action,
        trigger_mode,
        review_mode,
        review_skill,
        reason,
        update_from_sha[:8] if update_from_sha else "<empty>",
        update_to_sha[:8] if update_to_sha else "<empty>",
    )

    run_uid = repo.start_run(
        project_id=int(project_id),
        mr_iid=int(mr_iid),
        trigger=TRIGGER_WEBHOOK_OPEN if action == "open" else TRIGGER_WEBHOOK_UPDATE,
        review_mode=review_mode,
        mr_title=mr_title or "",
        project_path=project_path,
    )

    _start_review_thread(
        run_uid=run_uid,
        project_id=project_id,
        mr_iid=mr_iid,
        mr_title=mr_title,
        review_mode=review_mode,
        review_skill=review_skill,
        action=action,
        update_from_sha=update_from_sha,
        update_to_sha=update_to_sha,
        log_prefix="[Async]",
    )

    logger.info(f"Started async review for MR !{mr_iid} (run={run_uid})")
    return (
        jsonify(
            {
                "message": "Review started",
                "run_uid": run_uid,
                "mr_iid": mr_iid,
                "status": "processing",
                "review_mode": review_mode,
                "review_skill": review_skill,
            }
        ),
        202,
    )


@app.route("/manual-review", methods=["POST"])
def manual_review():
    """
    手动触发审查。

    与 webhook 走同一条执行路径，因此同样是异步的：立即返回 202 与 run_uid，
    进度到 /admin 或 /api/admin/runs/<run_uid> 查看。
    """
    data = request.json or {}
    project_id = data.get("project_id")
    mr_iid = data.get("mr_iid")

    if not project_id or not mr_iid:
        return jsonify({"error": "Missing project_id or mr_iid"}), 400

    logger.info(
        "Manual review requested: project_id=%s, mr_iid=%s, body_review_mode=%s, body_review_skill=%s",
        project_id,
        mr_iid,
        data.get("review_mode", ""),
        data.get("review_skill", ""),
    )

    try:
        review_mode, review_skill = resolve_review_options(
            {},
            default_skill="",
            manual_mode=str(data.get("review_mode", "")),
            manual_skill=str(data.get("review_skill", "")),
        )
        logger.info(
            "Manual review resolved options: mode=%s, skill=%s",
            review_mode,
            review_skill,
        )

        run_uid = repo.start_run(
            project_id=int(project_id),
            mr_iid=int(mr_iid),
            trigger=TRIGGER_MANUAL,
            review_mode=review_mode,
            mr_title=str(data.get("mr_title") or ""),
            project_path=str(data.get("project_path") or ""),
        )
        _start_review_thread(
            run_uid=run_uid,
            project_id=project_id,
            mr_iid=mr_iid,
            mr_title=str(data.get("mr_title") or ""),
            review_mode=review_mode,
            review_skill=review_skill,
            log_prefix="[Manual]",
        )
        return (
            jsonify(
                {
                    "message": "Review started",
                    "run_uid": run_uid,
                    "mr_iid": mr_iid,
                    "status": "processing",
                    "review_mode": review_mode,
                    "review_skill": review_skill,
                }
            ),
            202,
        )

    except Exception as e:
        logger.exception("Manual review failed to start")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Reconciler：兜底结算 + 保留期清理
# ---------------------------------------------------------------------------

RECONCILER_LEASE = "reconciler"


def _reconcile_once() -> None:
    """
    兜底一轮：结算错过 webhook 的 MR，并清理过期数据。

    服务宕机期间收不到 merge 事件，没有这一轮那些 Finding 会永远停在 undecided。
    """
    storage_cfg = load_storage_config()
    holder = f"{os.getpid()}@{os.uname().nodename}"
    interval = int(storage_cfg["reconcile_interval_seconds"])

    # 多 worker 下只让一个进程干活；租约 TTL 取一个周期多一点，避免抢锁抖动
    if not repo.acquire_lease(RECONCILER_LEASE, holder, interval + 60):
        return

    pending = repo.list_mrs_pending_settlement(limit=50)
    for item in pending:
        try:
            settle_mr(item["project_id"], item["mr_iid"])
        except Exception:
            logger.warning(
                "Reconcile settlement failed for MR !%s", item["mr_iid"], exc_info=True
            )

    purged = repo.purge_old_runs(int(storage_cfg["retention_days"]))
    if purged:
        logger.info("Reconciler purged %s runs beyond retention", purged)


def _reconciler_loop() -> None:
    """
    兜底循环。先睡再干，避免服务启动瞬间就抢锁、拖慢首个请求。

    单轮异常不退出循环：reconciler 挂掉是静默故障，比多打一行日志危险得多。
    """
    interval = int(load_storage_config()["reconcile_interval_seconds"])
    while True:
        time.sleep(interval)
        try:
            _reconcile_once()
        except Exception:
            logger.exception("Reconciler iteration failed")


_reconciler_started = False
_reconciler_lock = threading.Lock()


def start_reconciler() -> None:
    """
    启动 reconciler 后台线程（每个进程至多一个）。

    刻意在首个请求时才启动，而不是在 import 阶段：gunicorn 的 --preload 会先 import
    再 fork，import 期启动的线程不会被子进程继承，结果只有 master 里有这个线程。
    延迟到请求期启动，无论是否 preload、是容器还是 launchd，行为都一致；
    真正的互斥由数据库租约保证，多个进程各起一个线程也只有一个会干活。
    """
    global _reconciler_started
    if os.getenv("OPENCR_DISABLE_RECONCILER", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    with _reconciler_lock:
        if _reconciler_started:
            return
        _reconciler_started = True
    thread = threading.Thread(target=_reconciler_loop, name="opencr-reconciler")
    thread.daemon = True
    thread.start()
    logger.info("Reconciler thread started (pid=%s)", os.getpid())


def start_survey_scheduler() -> None:
    """
    启动巡检调度线程。

    与 reconciler 同样延迟到请求期启动，理由完全一致（见 start_reconciler 的注释）；
    但用的是**另一条租约**：reconciler 一轮要做结算加清理，跑得慢会把巡检的
    触发点往后拖，共用一条租约还意味着其中一个卡死另一个也停摆。
    """
    if os.getenv("OPENCR_DISABLE_SURVEY_SCHEDULER", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    from backend.survey.scheduler import start_scheduler

    start_scheduler()


@app.before_request
def _ensure_background_workers_running():
    """每个请求前确认本进程的后台线程已启动（两者内部都有幂等保护）。"""
    start_reconciler()
    start_survey_scheduler()


if __name__ == "__main__":
    config_data = load_file_config()
    app_version = get_app_version()

    host = os.getenv("REVIEW_SERVER_HOST") or _pick_config_value(config_data, "server.host", "REVIEW_SERVER_HOST") or "0.0.0.0"
    port_value = os.getenv("REVIEW_SERVER_PORT") or _pick_config_value(config_data, "server.port", "REVIEW_SERVER_PORT") or "9034"
    try:
        port = int(port_value)
    except ValueError:
        logger.warning(f"Invalid REVIEW_SERVER_PORT={port_value}, fallback to 9034")
        port = 9034

    cfg = load_openai_config()
    logger.info(f"Starting OpenCR v{app_version} on {host}:{port}")
    logger.info(f"Model: {cfg['model']}, Base URL: {cfg['base_url']}")

    gitlab_cfg = load_gitlab_config()
    gitlab_url = gitlab_cfg["url"]
    gitlab_token = gitlab_cfg["token"]
    logger.info(f"GitLab URL: {gitlab_url[:30] if gitlab_url else 'NOT SET'}...")
    logger.info(f"GitLab Token: {'SET' if gitlab_token else 'NOT SET'}")

    admin_cfg = load_admin_config()
    logger.info(
        "Admin console: %s",
        f"ENABLED at /admin (user={admin_cfg['username']})" if admin_cfg["enabled"] else "disabled",
    )

    app.run(host=host, port=port, debug=False)
