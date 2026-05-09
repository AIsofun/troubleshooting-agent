"""
Tests for the config loader — no external services required.
"""
import os
import pytest


def test_get_settings_returns_dict():
    from app.config import get_settings
    s = get_settings()
    assert isinstance(s, dict)
    assert "llm" in s
    assert "qdrant" in s
    assert "postgres" in s


def test_env_var_override(monkeypatch):
    # Clear lru_cache so changes take effect
    from app.config import get_settings
    get_settings.cache_clear()

    monkeypatch.setenv("LLM__MODEL", "test-model-override")
    s = get_settings()
    assert s["llm"]["model"] == "test-model-override"

    # cleanup
    get_settings.cache_clear()


def test_get_llm_returns_mock_when_configured(monkeypatch):
    from app.config import get_settings
    get_settings.cache_clear()

    monkeypatch.setenv("LLM__USE_MOCK", "true")
    from app.config import get_llm
    from app.agent.llm import MockLLM
    llm = get_llm()
    assert isinstance(llm, MockLLM)

    get_settings.cache_clear()
