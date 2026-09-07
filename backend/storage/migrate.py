#!/usr/bin/env python3
"""
数据库迁移入口。

由启动脚本 / 容器 entrypoint 调用，而不是在每个 gunicorn worker 里跑 ——
多个进程同时执行 DDL 只会互相抢锁，没有任何好处。

    python3 -m backend.storage.migrate
"""

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# backend/storage/migrate.py -> backend/storage -> backend
BACKEND_ROOT = Path(__file__).resolve().parent.parent
# 仓库根：alembic 的 env.py 需要它才能 import backend.*
PROJECT_ROOT = BACKEND_ROOT.parent


def upgrade_to_head() -> None:
    """把数据库升级到最新版本。"""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    from alembic import command
    from alembic.config import Config

    from ..review.config import load_storage_config

    database_url = load_storage_config()["database_url"]
    from .db import _ensure_sqlite_dir

    _ensure_sqlite_dir(database_url)

    # env.py 自己会从同一份配置解析连接串，这里不重复注入，避免两处真相
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))

    logger.info("Running alembic upgrade head on %s", database_url)
    command.upgrade(cfg, "head")


def main() -> int:
    """命令行入口。迁移失败返回非 0，让启动脚本能据此中止服务。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        upgrade_to_head()
    except Exception as e:
        print(f"[opencr] 数据库迁移失败: {e}", file=sys.stderr)
        return 1
    print("[opencr] 数据库已升级到最新版本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
