"""
配置加载器 — 三层优先级（高到低）：
  1. 环境变量   LLM__BASE_URL=...  (双下划线分隔层级)
  2. 环境专用 YAML   config/{APP_ENV}.yaml
  3. 公共基础 YAML   config/base.yaml
  4. 兜底 config.yaml（向后兼容旧版）

用法：
    from app.config import get_settings, get_llm
    s = get_settings()
    agent = Agent(llm=get_llm())
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

# ── 根目录 ──────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _ROOT / "config"
_LEGACY_CONFIG = _ROOT / "config.yaml"


# ── YAML 合并工具 ────────────────────────────────────────────

def _deep_merge(base: Dict, override: Dict) -> Dict:
    """递归合并两个 dict，override 优先。"""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise ImportError("请先安装: pip install pyyaml") from exc
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    将环境变量映射回 cfg dict。
    规则：LLM__BASE_URL → cfg["llm"]["base_url"]
    支持任意深度（双下划线分隔）。
    """
    for key, value in os.environ.items():
        parts = key.lower().split("__")
        if len(parts) < 2:
            continue
        # 只处理已知顶层节
        if parts[0] not in cfg:
            continue
        node = cfg
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                break
            node = node[part]
        else:
            if isinstance(node, dict):
                leaf = parts[-1]
                # 类型推断：尝试保持原有类型
                original = node.get(leaf)
                if isinstance(original, bool):
                    node[leaf] = value.lower() in ("1", "true", "yes")
                elif isinstance(original, int):
                    node[leaf] = int(value)
                elif isinstance(original, float):
                    node[leaf] = float(value)
                else:
                    node[leaf] = value
    return cfg


@lru_cache(maxsize=1)
def get_settings() -> Dict[str, Any]:
    """
    返回合并后的配置字典。进程内只计算一次。
    """
    # 1. base
    cfg = _load_yaml(_CONFIG_DIR / "base.yaml")

    # 2. env-specific override
    env = os.getenv("APP_ENV", "prod")
    env_cfg = _load_yaml(_CONFIG_DIR / f"{env}.yaml")
    cfg = _deep_merge(cfg, env_cfg)

    # 3. legacy config.yaml（向后兼容，只合并 llm 节）
    legacy = _load_yaml(_LEGACY_CONFIG)
    if legacy.get("llm"):
        cfg = _deep_merge(cfg, {"llm": legacy["llm"]})

    # 4. env var overrides（最高优先级）
    cfg = _apply_env_overrides(cfg)

    return cfg


def get_llm():
    """
    根据配置构造并返回 LLM 实例。
    use_mock=true  → MockLLM
    use_mock=false → OllamaLLM
    """
    from app.agent.llm import MockLLM, OllamaLLM

    cfg = get_settings().get("llm", {})

    if cfg.get("use_mock", False):
        return MockLLM()

    return OllamaLLM(
        base_url=cfg.get("base_url", "http://localhost:11434/v1"),
        model=cfg.get("model", "qwen2.5:14b"),
        api_key=cfg.get("api_key", "ollama"),
        temperature=float(cfg.get("temperature", 0.0)),
    )
