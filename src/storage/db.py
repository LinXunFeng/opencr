#!/usr/bin/env python3
"""
数据库引擎与会话管理。

SQLite + WAL：本服务是低频 IO bound 场景（每个 MR 事件一次审查），
WAL 足以支撑多 worker 并发写，不值得为此引入独立数据库进程。
"""

import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..review.config import load_storage_config

logger = logging.getLogger(__name__)

_engine = None
_session_factory = None
_init_lock = threading.Lock()


def _ensure_sqlite_dir(database_url: str) -> None:
    """SQLite 文件所在目录不存在时自动创建，避免首次启动因目录缺失而失败。"""
    if not database_url.startswith("sqlite"):
        return
    path_part = urlparse(database_url).path or ""
    # sqlite:///relative.db -> "/relative.db"；sqlite:////abs.db -> "//abs.db"
    db_path = Path(path_part.lstrip("/") if not path_part.startswith("//") else path_part[1:])
    if str(db_path) in {"", ":memory:"}:
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)


def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """每次建立连接时设置 SQLite 参数。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # 并发写冲突时在驱动层重试 5s，而不是立刻抛 "database is locked"
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def get_engine():
    """返回进程内单例 engine（懒初始化，因为配置在运行时才可读）。"""
    global _engine, _session_factory
    if _engine is not None:
        return _engine

    with _init_lock:
        if _engine is not None:
            return _engine

        cfg = load_storage_config()
        database_url = cfg["database_url"]
        _ensure_sqlite_dir(database_url)

        connect_args = {}
        if database_url.startswith("sqlite"):
            # 审查跑在后台线程里，连接必然跨线程使用
            connect_args["check_same_thread"] = False

        engine = create_engine(database_url, future=True, connect_args=connect_args)
        if database_url.startswith("sqlite"):
            event.listen(engine, "connect", _apply_sqlite_pragmas)

        _engine = engine
        _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        logger.info("Database engine initialized: %s", database_url)
        return _engine


def get_session_factory():
    """返回 sessionmaker；引擎尚未初始化时顺带初始化。"""
    if _session_factory is None:
        get_engine()
    return _session_factory


@contextmanager
def session_scope() -> Session:
    """事务边界。异常时回滚并向上抛，绝不吞掉。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_for_tests(database_url: str = "") -> None:
    """仅供测试：丢弃当前 engine，可选地强制指定 URL。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
    if database_url:
        import os

        os.environ["OPENCR_DATABASE_URL"] = database_url
