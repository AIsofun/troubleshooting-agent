"""
Embedder — 文本向量化客户端。

后端：Ollama OpenAI-compatible API（bge-m3 默认，dimension=1024）。
降级：Ollama 不可达时返回 None，让调用方决定是否跳过向量检索。

bge-m3 特点（适合工业场景）：
  - 支持中英混合文本，无需分词
  - 对型号、参数、报警码等短字符串保有语义区分度
  - 1024 维，精度与速度平衡好

使用示例：
    embedder = Embedder()
    vec = embedder.embed("曝光过高导致金属边缘高反光")
"""
from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from app.observability.logging import get_logger

log = get_logger(__name__)


class Embedder:
    """
    向量嵌入客户端。
    使用 OpenAI SDK 调用 Ollama embedding 接口。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        from app.config import get_settings
        cfg = get_settings().get("embedding", {})
        self.base_url = base_url or cfg.get("base_url", "http://ollama:11434/v1")
        self.model = model or cfg.get("model", "bge-m3")
        self.api_key = api_key or cfg.get("api_key", "ollama")
        self.dimension: int = int(cfg.get("dimension", 1024))
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        return self._client

    def embed(self, text: str) -> Optional[List[float]]:
        """
        将单条文本转为向量。
        返回 None 表示 embedding 服务不可达（调用方降级到仅关键词检索）。
        """
        if not text or not text.strip():
            return None
        try:
            client = self._get_client()
            resp = client.embeddings.create(model=self.model, input=text.strip())
            vec = resp.data[0].embedding
            return vec
        except Exception as exc:
            log.warning("embedding_failed", model=self.model, error=str(exc))
            return None

    def embed_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """批量嵌入，对单条失败进行容错（返回 None 占位）。"""
        results: List[Optional[List[float]]] = []
        for text in texts:
            results.append(self.embed(text))
        return results

    def is_available(self) -> bool:
        """探活：返回 True 表示 embedding 服务可用。"""
        try:
            vec = self.embed("health check")
            return vec is not None
        except Exception:
            return False


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """进程内单例 Embedder。"""
    return Embedder()
