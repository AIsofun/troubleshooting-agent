"""app/observability/__init__.py — 可观测性子包"""
from app.observability.logging import get_logger, setup_logging

__all__ = ["get_logger", "setup_logging"]
