"""
Alembic env.py — 从 app.config.get_settings() 读取数据库 URL，
保证与应用配置的单一来源原则一致。
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

# 将项目根目录加入 sys.path，使 app.* 可被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import context
from sqlalchemy import engine_from_config, pool

# ── 从 app 读取配置 ──────────────────────────────────────
from app.config import get_settings
from app.persistence.models import Base  # noqa: F401 — 注册所有 ORM 模型

_settings = get_settings()
_dsn: str = _settings.get("postgres", {}).get(
    "dsn",
    "postgresql+psycopg://agent:agent_dev_pwd@localhost:5432/agentdb",
)
# 确保使用同步 psycopg3 驱动
_dsn = _dsn.replace("postgresql+asyncpg", "postgresql+psycopg")
if not _dsn.startswith("postgresql+psycopg"):
    _dsn = _dsn.replace("postgresql://", "postgresql+psycopg://")

# ── Alembic Config 对象 ──────────────────────────────────
config = context.config
config.set_main_option("sqlalchemy.url", _dsn)

# 配置 logging（使用 alembic.ini 中的日志配置）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标 metadata（用于 autogenerate）
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """在 'offline' 模式下运行迁移（不需要数据库连接）。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在 'online' 模式下运行迁移（需要数据库连接）。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
