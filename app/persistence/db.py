"""
数据库连接管理。

- 使用 SQLAlchemy 2.0（同步模式 + psycopg3 驱动）
- 启动时探测连接可用性；不可用则降级运行（不阻断 Agent 功能）
- 向外暴露 get_session() 上下文管理器 和 is_db_available() 检查函数
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Generator

from app.observability.logging import get_logger

log = get_logger(__name__)

_engine = None
_db_available: bool = False


def _build_engine():
    """构建 SQLAlchemy Engine，使用 psycopg3 同步驱动。"""
    from sqlalchemy import create_engine, text

    from app.config import get_settings

    dsn: str = get_settings().get("postgres", {}).get(
        "dsn",
        "postgresql+psycopg://agent:agent_dev_pwd@localhost:5432/agentdb",
    )
    # 确保使用同步 psycopg3 驱动前缀
    dsn = dsn.replace("postgresql+asyncpg", "postgresql+psycopg")
    if not dsn.startswith("postgresql+psycopg"):
        dsn = dsn.replace("postgresql://", "postgresql+psycopg://")

    engine = create_engine(
        dsn,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,   # 每次借连接时先 ping，自动处理连接断开
        echo=False,
    )
    # 探活
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return engine


def init_db() -> bool:
    """
    初始化数据库连接。
    返回 True 表示连接成功，False 表示数据库不可用（降级模式）。
    应在进程启动时调用一次。
    """
    global _engine, _db_available  # noqa: PLW0603
    if _db_available:
        return True
    try:
        _engine = _build_engine()
        _db_available = True
        log.info("db_connected", msg="Postgres connection established")
    except Exception as exc:
        log.warning(
            "db_unavailable",
            error=str(exc),
            msg="Postgres not available; persistence disabled. Agent will still work.",
        )
        _db_available = False
    return _db_available


def is_db_available() -> bool:
    return _db_available


def get_engine():
    """返回当前 SQLAlchemy Engine（必须先调用 init_db()）。"""
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine


@contextmanager
def get_session() -> Generator:
    """
    获取数据库 Session 的上下文管理器。
    Usage:
        with get_session() as session:
            session.add(obj)
            session.commit()
    """
    from sqlalchemy.orm import Session

    if not _db_available:
        raise RuntimeError("Database is not available.")

    with Session(get_engine()) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
