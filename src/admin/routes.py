#!/usr/bin/env python3
"""
后台管理路由。

页面只有两个：总览 /admin 与运行详情 /admin/runs/<run_uid>。
数据量小，先把口径做对；多维筛选等真有人抱怨再加。
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from ..review.config import get_app_version, load_openai_config, load_storage_config
from ..storage import repo
from .auth import require_admin

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, template_folder="templates")

ALLOWED_WINDOWS = (1, 7, 30)


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


@admin_bp.route("/admin", methods=["GET"])
@require_admin
def dashboard():
    """后台总览页。页面数据由前端轮询 /api/admin/overview 获取。"""
    return render_template("dashboard.html", version=get_app_version())


@admin_bp.route("/admin/runs/<run_uid>", methods=["GET"])
@require_admin
def run_detail_page(run_uid: str):
    """单次 ReviewRun 的详情页，列出该次运行的全部 Finding 与采纳结论。"""
    detail = repo.get_run_detail(run_uid, stale_after_seconds=_stale_after())
    if detail is None:
        return render_template("run_detail.html", run=None, run_uid=run_uid), 404
    return render_template("run_detail.html", run=detail, run_uid=run_uid)


@admin_bp.route("/api/admin/overview", methods=["GET"])
@require_admin
def api_overview():
    """总览页的全部数据。前端每 5 秒轮询一次，因此这里不做任何重计算。"""
    stale_after = _stale_after()
    openai_cfg = load_openai_config()
    error_days = _window_days(7)
    verdict_days = _window_days(30)

    return jsonify(
        {
            "service": {
                "version": get_app_version(),
                "model": openai_cfg.get("model", "unknown"),
                "base_url": openai_cfg.get("base_url", "unknown"),
                "stale_after_seconds": stale_after,
            },
            "active_runs": repo.list_active_runs(stale_after_seconds=stale_after),
            "recent_runs": repo.list_recent_runs(
                limit=50,
                status=(request.args.get("status") or "").strip(),
                stale_after_seconds=stale_after,
            ),
            "errors": repo.error_stats(days=error_days),
            "verdicts": repo.verdict_stats(days=verdict_days),
        }
    )


@admin_bp.route("/api/admin/runs/<run_uid>", methods=["GET"])
@require_admin
def api_run_detail(run_uid: str):
    """单次 ReviewRun 的详情。手动触发审查后可用它查询进度。"""
    detail = repo.get_run_detail(run_uid, stale_after_seconds=_stale_after())
    if detail is None:
        return jsonify({"error": "run not found"}), 404
    return jsonify(detail)
