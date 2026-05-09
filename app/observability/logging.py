"""
结构化日志 — 基于 structlog。

使用方式：
    from app.observability.logging import get_logger
    log = get_logger(__name__)
    log.info("tool_call", tool="get_camera_status", trace_id="abc")

生产环境（log_format=json）：每行一个 JSON 对象，含 timestamp/level/event/trace_id 等字段。
开发环境（log_format=console）：彩色可读格式，适合终端调试。
"""
from __future__ import annotations

import logging
import sys
from typing import Any

try:
    import structlog
    _HAS_STRUCTLOG = True
except ImportError:
    _HAS_STRUCTLOG = False

_configured = False


def setup_logging(log_level: str = "INFO", log_format: str = "json") -> None:
    """
    初始化全局日志配置。应在进程启动时调用一次。
    log_format: "json" | "console"
    """
    global _configured  # noqa: PLW0603
    if _configured:
        return
    _configured = True

    level = getattr(logging, log_level.upper(), logging.INFO)

    if not _HAS_STRUCTLOG:
        # 降级到标准库 logging
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            stream=sys.stdout,
        )
        return

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if log_format == "json":
        renderer: Any = structlog.processors.JSONRenderer(ensure_ascii=False)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


def get_logger(name: str) -> Any:
    """
    返回一个 structlog BoundLogger（或降级时的 stdlib logger）。
    Usage: log = get_logger(__name__)
    """
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name)
