"""
Text Embedding Service (Xinference API)
=======================================

Converts natural language text into dense vector embeddings for semantic
retrieval. All inference is delegated to the Xinference platform via the
OpenAI-compatible /v1/embeddings endpoint (bge-m3); no local model weights
are loaded (the workstation has no GPU).

Data Flow:
    User query / document chunk (text)
        → embed_query / embed_batch (API vectorization)
            → vector_store.py (vector storage / retrieval)
                → hybrid_retriever.py (multi-path retrieval + RRF fusion)
                    → agents/workflow_nodes.py (Agent workflow)

Module Call Chain:
    embeddings.py ← vector_store.py ← hybrid_retriever.py ← agents/workflow_nodes.py

Key Concepts:
    - Dense Embedding: Maps discrete text to continuous vector space where
      semantically similar texts are closer. Complements sparse retrieval
      (BM25/TF-IDF) which captures lexical exact matching.
    - bge-m3: Symmetric multilingual embedding model (1024-dim). Unlike
      bge-large-zh-v1.5, it requires NO query instruction prefix — queries
      and documents are encoded as-is.
    - LRU Cache: Least Recently Used eviction via OrderedDict; O(1) lookup
      and insertion. Cache key is MD5 hash of input text. Kept to avoid
      redundant API round-trips.
    - Singleton Pattern: Thread-safe via Double-Checked Locking (DCL) to
      ensure only one manager instance exists globally.
    - Vector space isolation: switching the embedding model (e.g. to
      Qwen3-VL-Embedding later) invalidates all persisted vector indexes;
      they must be rebuilt from source documents.

Configuration (from core/config.py Settings):
    XINFERENCE_API_BASE: Xinference endpoint, default http://127.0.0.1:9997/v1
    XINFERENCE_API_KEY: API key
    EMBEDDING_MODEL: Model UID on Xinference, default "bge-m3"
    XINFERENCE_TIMEOUT: Request timeout in seconds, default 120
"""


import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from typing import List, Optional

import numpy as np
import requests

from core import get_settings, ConfigurationError

logger = logging.getLogger(__name__)

# 单次 /v1/embeddings 请求携带的文本条数(网络往返分片粒度,与本地 GPU batch 无关)
_API_BATCH_SIZE = 32


class EmbeddingManager:
    """Singleton embedding manager backed by the Xinference /v1/embeddings API.

    Public interface is identical to the previous local SentenceTransformer
    implementation (embed_query / embed_batch), so downstream callers
    (vector_store, hybrid_retriever) require no changes.

    Example:
        manager = EmbeddingManager()
        embeddings = manager.embed_batch(["text1", "text2"])
        query_vec = manager.embed_query("NE5532 supply voltage")
    """

    _instance = None
    _initialized = False
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        """Singleton creation with Double-Checked Locking (DCL).

        First check (no lock): fast path when instance already exists.
        Lock: ensures only one thread creates the instance.
        Second check (under lock): prevents duplicate creation when
        multiple threads pass the first check simultaneously.
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, settings=None, model_name: str = None):
        """Initialize embedding manager (API mode, no local weights).

        Args:
            settings: Configuration object. Defaults to get_settings() singleton.
                      Allows injection for testing.
            model_name: Model UID on Xinference. Defaults to settings.EMBEDDING_MODEL.
        """
        if EmbeddingManager._initialized:
            return

        EmbeddingManager._initialized = True

        self.settings = settings or get_settings()
        self.model_name = model_name or self.settings.EMBEDDING_MODEL
        self.api_base = str(self.settings.XINFERENCE_API_BASE).rstrip("/")
        self.api_key = self.settings.XINFERENCE_API_KEY
        self.timeout = self.settings.XINFERENCE_TIMEOUT
        self.dimension = self.settings.EMBEDDING_DIMENSION

        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            self._headers["Authorization"] = f"Bearer {self.api_key}"

        self._cache = OrderedDict()
        self._cache_max_size = 2000

        logger.info(
            f"EmbeddingManager initialized (Xinference API, model={self.model_name}, "
            f"base={self.api_base}, timeout={self.timeout}s)"
        )

    def _load_model(self):
        """Compatibility no-op: API mode loads no local weights.

        Kept because older call sites may still invoke it during warm-up.
        """
        logger.debug("EmbeddingManager runs in Xinference API mode; no local model to load")

    def _api_embed(self, texts: List[str]) -> np.ndarray:
        """Call /v1/embeddings for a list of texts (single HTTP request).

        Retries once on network errors and 5xx responses; 4xx errors fail
        immediately (retrying a malformed request is pointless).

        Args:
            texts: Texts to encode (len(texts) <= _API_BATCH_SIZE).

        Returns:
            float32 array of shape (len(texts), dimension), L2-normalized.

        Raises:
            RuntimeError: If the API fails after retry.
        """
        payload = {"model": self.model_name, "input": list(texts)}
        last_error: Optional[Exception] = None

        for attempt in range(2):
            try:
                resp = requests.post(
                    f"{self.api_base}/embeddings",
                    headers=self._headers,
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    if len(data) != len(texts):
                        raise RuntimeError(
                            f"Embedding API returned {len(data)} vectors for {len(texts)} inputs"
                        )
                    # 按 index 排序,保证与输入顺序一致
                    ordered = sorted(data, key=lambda d: d.get("index", 0))
                    vectors = np.asarray(
                        [item["embedding"] for item in ordered], dtype=np.float32
                    )
                    vectors = self._l2_normalize(vectors)
                    self.dimension = int(vectors.shape[1])
                    return vectors

                error = RuntimeError(
                    f"Embedding API HTTP {resp.status_code}: {resp.text[:300]}"
                )
                if 400 <= resp.status_code < 500:
                    raise error
                last_error = error
            except (requests.RequestException, ValueError) as e:
                last_error = RuntimeError(f"Embedding API request failed: {e}")

            if attempt == 0:
                logger.warning(f"Embedding API attempt 1 failed ({last_error}), retrying in 1s...")
                time.sleep(1.0)

        raise RuntimeError(f"Embedding API failed after retry: {last_error}")

    @staticmethod
    def _l2_normalize(vectors: np.ndarray) -> np.ndarray:
        """L2-normalize row vectors (idempotent — safe if API already normalizes)."""
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms

    def embed_query(self, query: str) -> np.ndarray:
        """Encode a user query into an embedding vector.

        bge-m3 is a symmetric retrieval model: queries and documents are
        encoded the same way, with NO instruction prefix. (The previous
        bge-large-zh-v1.5 required a Chinese instruction prefix for
        queries; that logic was intentionally dropped when switching to
        bge-m3.)

        Args:
            query: User query text, e.g. "NE5532的供电电压范围是多少？".

        Returns:
            Embedding vector of shape (dimension,), e.g. (1024,) float32.
        """
        result = self.embed_batch([query], use_cache=False)
        return result[0] if len(result) > 0 else np.array([])

    def embed_batch(self, texts: List[str],
                    use_cache: bool = True) -> np.ndarray:
        """Batch encode texts into embedding vectors via the API.

        Large batches are split into _API_BATCH_SIZE chunks, one HTTP
        request per chunk. LRU caching (use_cache=True) avoids re-encoding
        repeated texts during document ingestion.

        Args:
            texts: Text list to encode, e.g. ["text1", "text2", "text3"].
            use_cache: Whether to use the LRU cache. Defaults to True.
                - True: suitable for document ingestion where texts repeat.
                - False: suitable for unique ad-hoc queries.

        Returns:
            Embedding matrix of shape (len(texts), dimension), e.g. (3, 1024).

        Raises:
            RuntimeError: If the API fails after retry.
        """
        if not texts:
            return np.array([])

        start_time = time.time()
        total_texts = len(texts)

        if not use_cache:
            # 分片并行：TI 大规格书会切出几百批，串行逐批等 HTTP 往返是
            # 入库耗时的大头。4 路并发即可把批间等待压掉约 3/4。
            ranges = list(range(0, total_texts, _API_BATCH_SIZE))
            slices = [texts[i:i + _API_BATCH_SIZE] for i in ranges]
            with ThreadPoolExecutor(max_workers=4) as pool:
                chunks = list(pool.map(self._api_embed, slices))
            embeddings = np.vstack(chunks) if chunks else np.array([])

            elapsed = (time.time() - start_time) * 1000
            logger.debug(
                f"Batch embedding done (API, no cache): {total_texts} texts, "
                f"elapsed {elapsed:.0f}ms"
            )
            return embeddings

        cached_results = {}
        texts_to_encode = []
        text_indices = []

        for i, text in enumerate(texts):
            cache_key = self._get_cache_key(text)
            cached_embedding = self._get_from_cache(cache_key)
            if cached_embedding is not None:
                cached_results[i] = cached_embedding
            else:
                texts_to_encode.append(text)
                text_indices.append(i)

        cache_hit_count = len(cached_results)

        if texts_to_encode:
            new_embeddings = self.embed_batch(texts_to_encode, use_cache=False)
            for j, idx in enumerate(text_indices):
                cache_key = self._get_cache_key(texts[idx])
                self._add_to_cache(cache_key, new_embeddings[j])
                cached_results[idx] = new_embeddings[j]

        result = np.array([cached_results[i] for i in range(len(texts))])

        elapsed = (time.time() - start_time) * 1000
        cache_hit_rate = cache_hit_count / total_texts * 100
        logger.debug(
            f"Batch embedding done (API, cache): {total_texts} texts, "
            f"cache hit {cache_hit_count} ({cache_hit_rate:.1f}%), elapsed {elapsed:.0f}ms"
        )

        return result

    def _get_cache_key(self, text: str) -> str:
        """Generate cache key from text via MD5 hash.

        MD5 maps arbitrary-length text to a fixed 32-char hex string.
        Collision probability is negligible for caching purposes.

        Args:
            text: Original text string.

        Returns:
            32-character MD5 hex digest string.
        """
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    def _add_to_cache(self, key: str, embedding: np.ndarray):
        """Add an entry to the LRU cache.

        If the key already exists, it is re-inserted at the end (most recent).
        If the cache is full, the least recently used entry (head of OrderedDict)
        is evicted.

        Args:
            key: Cache key (MD5 hex digest, 32 chars).
            embedding: Embedding vector (shape=(dimension,), dtype=float32).
        """
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= self._cache_max_size:
            self._cache.popitem(last=False)
            logger.debug(f"Cache full, LRU eviction, current size: {len(self._cache)}")

        self._cache[key] = embedding

    def _get_from_cache(self, key: str) -> Optional[np.ndarray]:
        """Retrieve an embedding from the LRU cache.

        On cache hit, the entry is moved to the end of the OrderedDict
        (marked as most recently used) by pop + re-insert.

        Args:
            key: Cache key (MD5 hex digest).

        Returns:
            Cached embedding vector if hit, None if miss.
        """
        if key in self._cache:
            value = self._cache.pop(key)
            self._cache[key] = value
            return value
        return None
