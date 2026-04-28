"""
配置加载器。

所有模块统一从这里获取配置，不要在业务代码里硬编码 URL / model 名称。

用法：
    from app.config import get_llm
    agent = Agent(llm=get_llm())
"""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

# 配置文件在项目根目录
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@lru_cache(maxsize=1)           # 进程内只读一次，多次调用不重复 IO
def _load_config() -> Dict[str, Any]:
    try:
        import yaml             # pip install pyyaml
    except ImportError as e:
        raise ImportError("请先安装: pip install pyyaml") from e

    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"配置文件不存在: {_CONFIG_PATH}")

    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_llm():
    """
    根据 config.yaml 构造并返回 LLM 实例。
    use_mock=true  → MockLLM（无需真实大模型）
    use_mock=false → OllamaLLM（接真实 API）
    """
    from app.agent.llm import MockLLM, OllamaLLM

    cfg = _load_config().get("llm", {})

    if cfg.get("use_mock", False):
        return MockLLM()

    return OllamaLLM(
        base_url=cfg.get("base_url", "http://localhost:11434/v1"),
        model=cfg.get("model", "qwen2.5:14b"),
        api_key=cfg.get("api_key", "ollama"),
        temperature=float(cfg.get("temperature", 0.0)),
    )
