"""
Hybrid Retriever — Multi-path Retrieval with RRF Fusion & Xinference Rerank
===========================================================================

Core orchestrator for VeriQuery's retrieval pipeline. Combines three
heterogeneous retrieval paths (Dense, Sparse, Structured) via Reciprocal
Rank Fusion (RRF) and provides a unified async retrieve() interface.
Additionally, it uses Xinference API for Rerank refinement.

Architecture:
    ┌──────────────────────────────────────────────────────────────────────┐
    │                 HybridRetriever (RRF Fusion -> Rerank)               │
    │                                                                      │
    │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
    │  │  VectorStore  │  │  BM25Store   │  │  TableStore   │               │
    │  │  (Dense)      │  │  (Sparse)    │  │  (Structured) │               │
    │  │  ChromaDB     │  │  rank_bm25   │  │  SQLite+FTS5  │               │
    │  │  Semantic     │  │  Keyword     │  │  Table data   │               │
    │  └──────────────┘  └──────────────┘  └──────────────┘               │
    │        w=0.5            w=0.35           w=0.15                      │
    │                                                                      │
    │  Results → Normalize → RRF Fusion → Dedup → API Rerank → Final       │
    └──────────────────────────────────────────────────────────────────────┘

Module Call Chain:
    embeddings.py ← vector_store.py ← hybrid_retriever.py ← agents/workflow_nodes.py
    bm25_store.py  ─────────────────→ hybrid_retriever.py
    table_store.py ─────────────────→ hybrid_retriever.py

Key Concepts:
    - Hybrid Retrieval: Combines Dense (semantic), Sparse (keyword), and
      Structured (table) retrieval for complementary coverage. Lin et al.
      (2021) demonstrated that hybrid retrieval significantly outperforms
      any single retrieval method.
    - RRF (Reciprocal Rank Fusion): Score-based fusion that relies only on
      rank positions, avoiding the problem of incomparable score scales
      across different retrieval paths. Cormack et al. (2009) proved RRF
      outperforms CombSUM and Condorcet fusion.
        Formula: score(d) = Σ_s w_s × 1/(k + rank_s(d) + 1)
        k=60 is the recommended smoothing constant from the SIGIR paper.
    - Async Concurrency: asyncio.gather with return_exceptions=True
      executes all three retrieval paths concurrently. Total latency is
      max(T1, T2, T3) instead of T1+T2+T3. Individual failures are
      tolerated — other paths still return results.

Data Flow:
    User query "NE5532 supply voltage range?"
        │
        ├──→ _vector_retrieve(query, top_k*2)  → List[RetrievedChunk] (source="vector")
        ├──→ retrieve_lexical(query, top_k*2)  → List[RetrievedChunk] (source="bm25")
        └──→ _table_retrieve(query, top_k)     → List[RetrievedChunk] (source="table")
        │
        ▼ (asyncio.gather, return_exceptions=True)
        │
        _rrf_fusion(all_results, k=60)
        │   ├── Group by source → Sort within groups → Compute RRF scores
        │   ├── Multi-source hits accumulate RRF scores (consensus bonus)
        │   ├── Deduplicate identical text on a document page
        │   └── Normalize final scores to [0, 1]
        │
        ▼
        _rerank_candidates(query, candidates, top_n)
        │   └── Call Xinference Rerank API (bge-reranker) to refine ranks
        │
        ▼
        Apply per-document limit → List[RetrievedChunk] (source="hybrid")

Configuration (from core/config.py Settings):
    VECTOR_WEIGHT: Vector retrieval weight, default 0.5
    BM25_WEIGHT: BM25 retrieval weight, default 0.35
    STRUCTURED_WEIGHT: Table retrieval weight, default 0.15
    MAX_RESULTS_PER_DOC: Max chunks per document, default 5
    MAX_RESULTS_PER_DOC_CROSS_SOURCE: Max chunks per document (cross-source), default 8
    EMBEDDING_DIMENSION: Embedding dimension, default 1024
    XINFERENCE_API_BASE: Xinference API endpoint
    XINFERENCE_API_KEY: Xinference API key
    RERANK_MODEL: Rerank model UID, default bge-reranker-v2-m3
"""

import logging
from typing import List, Dict, Any, Optional
import asyncio
import math
import requests

from core.config import get_settings
from core.schema import RetrievedChunk
from retrieval.table_evidence import TableEvidenceQuery, matching_store_rows, table_row_key

logger = logging.getLogger(__name__)


class HybridRetriever:
    """Hybrid retriever orchestrating Dense + Sparse + Structured retrieval.

    Facade pattern: encapsulates three heterogeneous retrieval subsystems
    behind a unified async retrieve() interface. Callers (agents/workflow_nodes.py)
    only need to call retrieve() without worrying about multi-path dispatch.

    Features:
        - Dependency injection: all retrieval components are injectable for
          testing; auto-created with lazy init when not provided.
        - Fault tolerance: each component initializes independently; single
          component failure does not affect others. Only raises RuntimeError
          when ALL components fail.
        - RRF fusion with configurable per-source weights.
        - Deduplication and per-document result limiting.
        - Final refinement using Rerank model via Xinference API.

    Example:
        retriever = HybridRetriever()
        results = await retriever.retrieve("NE5532 supply voltage", top_k=10)
    """

    def __init__(
        self,
        settings=None,
        embedding_manager=None,
        vector_store=None,
        bm25_store=None,
        table_store=None,
    ):
        """Initialize hybrid retriever with optional dependency injection.

        All parameters are optional. When not provided, components are
        auto-created. Each component has independent error handling —
        single component failure is logged but does not prevent others
        from initializing.

        Args:
            settings: Configuration object. Defaults to get_settings() singleton.
            embedding_manager: EmbeddingManager instance for query vectorization.
                               Auto-created (singleton) if not provided.
            vector_store: ChromaDBVectorStore for dense retrieval.
            bm25_store: BM25Store for sparse retrieval.
            table_store: TableStore for structured retrieval.
        """
        self.settings = settings or get_settings()
        self.embedding_manager = embedding_manager

        # 读取 Xinference Rerank 配置
        self.rerank_model = getattr(self.settings, "RERANK_MODEL", "bge-reranker-v2-m3")
        self.api_base = getattr(self.settings, "XINFERENCE_API_BASE", "http://127.0.0.1:9997/v1")
        self.api_key = getattr(self.settings, "XINFERENCE_API_KEY", "")

        try:
            from retrieval.vector_store import create_vector_store
            from retrieval.bm25_store import BM25Store
            from retrieval.table_store import TableStore

            if self.embedding_manager is None:
                try:
                    from retrieval.embeddings import EmbeddingManager
                    self.embedding_manager = EmbeddingManager(settings=self.settings)
                except Exception as e:
                    logger.warning(f"EmbeddingManager init failed: {e}")
                    self.embedding_manager = None

            if self.embedding_manager and hasattr(self.embedding_manager, 'dimension'):
                self.settings.EMBEDDING_DIMENSION = self.embedding_manager.dimension

            try:
                self.vector_store = vector_store or create_vector_store(settings=self.settings)
            except Exception as e:
                logger.error(f"Vector store init failed: {e}", exc_info=True)
                self.vector_store = None

            try:
                self.bm25_store = bm25_store or BM25Store(
                    settings=self.settings,
                    persist_path=str(self.settings.DATA_DIR / "bm25_index.pkl"),
                )
            except Exception as e:
                logger.error(f"BM25 store init failed: {e}", exc_info=True)
                self.bm25_store = None

            try:
                self.table_store = table_store or TableStore(settings=self.settings)
            except Exception as e:
                logger.error(f"Table store init failed: {e}", exc_info=True)
                self.table_store = None

            self.source_weights = {
                "vector": getattr(self.settings, "VECTOR_WEIGHT", 0.5),
                "bm25": getattr(self.settings, "BM25_WEIGHT", 0.35),
                "table": getattr(self.settings, "STRUCTURED_WEIGHT", 0.15),
            }

            if not any([self.vector_store, self.bm25_store, self.table_store]):
                raise RuntimeError("All retrievers failed to initialize")

        except ImportError as e:
            raise RuntimeError(f"Retrieval component import failed: {e}") from e
        except Exception:
            raise

    async def retrieve(
        self,
        query: str,
        top_k: int = 50,
        include_tables: bool = True,
        filters: Optional[Dict[str, Any]] = None,
        document_ids: Optional[List[str]] = None,
        max_per_doc: Optional[int] = None,
        lexical_query: Optional[str] = None,
        focus_tables: bool = False,
    ) -> List[RetrievedChunk]:
        """Unified hybrid retrieval interface.

        Three-phase architecture:
            Phase 1: Concurrently execute all available retrieval paths.
            Phase 2: RRF fusion and deduplication.
            Phase 3: API Rerank, then per-document and output limits.

        Args:
            query: Query text, e.g. "NE5532的供电电压范围是多少？".
            top_k: Maximum number of results to return. Defaults to 50.
                   Vector and BM25 have a wider candidate window.
            include_tables: Whether to include table retrieval. Defaults to True.
            filters: Optional filter dict (document_id, source, etc.).
            document_ids: Optional document ID list to scope retrieval.
                          Merged into filters internally.
            max_per_doc: Override the final number of results per document.
            lexical_query: Optional augmented query for BM25 and table search.
            focus_tables: Reserve complete rows matching explicit query conditions.

        Returns:
            Fused and reranked List[RetrievedChunk] with source="hybrid".
            Returns empty list if all paths return no results.
        """
        try:
            if top_k <= 0:
                return []
            merged_filters = dict(filters or {})
            if document_ids:
                merged_filters["document_ids"] = [str(d) for d in document_ids]
            candidate_count = max(top_k * 2, 60)
            keyword_query = lexical_query or query
            tasks = []

            if self.vector_store:
                tasks.append(self._vector_retrieve(query, candidate_count, merged_filters))
            if self.bm25_store:
                tasks.append(self.retrieve_lexical(keyword_query, candidate_count, merged_filters))
            if include_tables and self.table_store:
                tasks.append(self._table_retrieve(keyword_query, top_k, merged_filters))

            if not tasks:
                raise RuntimeError("No retrieval components available")

            results = await asyncio.gather(*tasks, return_exceptions=True)

            all_results = []
            retrieval_errors = []

            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    error_msg = f"Retrieval task {i+1} failed: {result}"
                    logger.warning(error_msg)
                    retrieval_errors.append(error_msg)
                elif result:
                    logger.debug("Retrieved %s candidates: %s", result[0].source,
                                 [chunk.chunk_id for chunk in result])
                    all_results.extend(result)

            if retrieval_errors:
                logger.warning(f"{len(retrieval_errors)} retrieval task(s) failed")

            if not all_results:
                logger.info("All retrieval paths returned no results")
                return []

            focus = TableEvidenceQuery.from_text(keyword_query) if focus_tables else None
            if focus:
                recovered = matching_store_rows(all_results, focus)
                original_headers = {table_row_key(chunk): chunk.metadata["table_header"] for chunk in recovered}
                all_results.extend(recovered)
            fused = self._rrf_fusion(all_results)
            if focus:
                for chunk in fused:
                    metadata = chunk.metadata
                    original_header = original_headers.get(table_row_key(chunk))
                    if metadata.get("source") != "table_text" and original_header is None:
                        continue
                    header = original_header or metadata.get("table_header") or ""
                    if focus.matches(chunk.text, header):
                        metadata["table_header"] = header
                        metadata["focused_row"] = True
                        metadata["focused_text"] = focus.model_text(chunk.text, header)
                        if literal := focus.literal_value(chunk.text, header):
                            metadata["citation_literal"] = literal
                matched = [chunk for chunk in fused if chunk.metadata.get("focused_row")]
                others = [chunk for chunk in fused if not chunk.metadata.get("focused_row")]
                candidates = (matched[:12] + others)[:candidate_count]
            else:
                candidates = fused[:candidate_count]
            logger.debug("RRF candidate order: %s", [chunk.chunk_id for chunk in candidates])
            ranked = await asyncio.to_thread(
                self._rerank_candidates, query, candidates, len(candidates)
            )
            logger.debug("Rerank candidate order: %s", [chunk.chunk_id for chunk in ranked])
            if focus:
                for rank, chunk in enumerate(ranked, 1):
                    chunk.metadata["rerank_position"] = rank
                ranked.sort(key=lambda chunk: not chunk.metadata.get("focused_row", False))
            return self._limit_results(ranked, top_k, max_per_doc)

        except Exception as e:
            logger.error(f"Hybrid retrieval failed: {e}")
            raise

    async def rank_passages(self, query: str, candidates: List[RetrievedChunk]) -> List[RetrievedChunk]:
        """Rank an existing candidate set for a secondary question without changing scores."""
        return await asyncio.to_thread(self._rerank_candidates, query, candidates, len(candidates), False)

    def _rerank_candidates(
        self, query: str, candidates: List[RetrievedChunk], top_n: int, update_scores: bool = True,
    ) -> List[RetrievedChunk]:
        """Refines candidate chunks using Xinference Rerank API.

        Sends the query and candidate texts to the Rerank model (e.g., bge-reranker-v2-m3)
        to compute precise relevance scores. Overrides the RRF scores if successful.

        Args:
            query: User query text.
            candidates: List of chunks from RRF fusion to be reranked.
            top_n: Number of top chunks to return after reranking.

        Returns:
            List of reranked chunks, limited to top_n. Falls back to
            original RRF candidates if the API call fails.
        """
        if not candidates:
            return []

        url = f"{self.api_base}/rerank"
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.rerank_model,
            "query": query,
            "documents": [
                "\n".join(filter(None, (c.metadata.get("table_header"),
                                        c.metadata.get("focused_text") or c.text)))
                if c.metadata.get("focused_row") else c.text
                for c in candidates
            ],
            "top_n": top_n,
        }

        try:
            logger.info(f"Calling Rerank API ({self.rerank_model}) for {len(candidates)} candidates...")
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            response.raise_for_status()
            rerank_data = response.json()

            scores = {
                item["index"]: float(item["relevance_score"])
                for item in rerank_data.get("results", [])
                if type(item.get("index")) is int and 0 <= item["index"] < len(candidates)
            }
            scores = {index: score for index, score in scores.items() if math.isfinite(score)}
            if not scores:
                logger.warning("Rerank returned no valid scores, keeping RRF order")
                return candidates[:top_n]

            ranked = []
            for index in sorted(scores, key=scores.get, reverse=True):
                chunk = candidates[index]
                if update_scores:
                    chunk.score = scores[index]
                    chunk.metadata["rerank_score"] = chunk.score
                ranked.append(chunk)
            ranked.extend(chunk for index, chunk in enumerate(candidates) if index not in scores)
            return ranked[:top_n]

        except Exception as e:
            logger.error(f"Rerank API 调用失败，降级使用 RRF 结果: {e}")
            # 降级策略：如果 Rerank 接口挂了或超时，直接截取前 top_n 个返回，保证主流程不断
            return candidates[:top_n]

    async def _vector_retrieve(
        self, query: str, top_k: int, filters: Optional[Dict[str, Any]] = None
    ) -> List[RetrievedChunk]:
        """Dense retrieval via semantic similarity (VectorStore + ChromaDB).

        Pipeline:
            1. Encode query into embedding vector via EmbeddingManager.
            2. Filter conditions to ChromaDB-supported keys only.
            3. Search ChromaDB (HNSW approximate nearest neighbor).
            4. Convert VectorSearchResult to RetrievedChunk.

        Args:
            query: Query text.
            top_k: Number of candidates to retrieve (typically top_k*2).
            filters: Filter dict supporting document_id and document_ids.

        Returns:
            List[RetrievedChunk] with source="vector". Scores are cosine
            similarity (1 - distance), already in [0, 1].
        """
        try:
            query_embedding = self._get_query_embedding(query)
            if not query_embedding:
                raise RuntimeError("Query embedding is empty, cannot perform vector retrieval")

            filter_dict = None
            if filters:
                supported_filters = {}
                for key in self.vector_store.SUPPORTED_FILTER_KEYS:
                    if key in filters:
                        supported_filters[key] = filters[key]
                if supported_filters:
                    filter_dict = supported_filters

            exclude_source = (filters or {}).get("exclude_source")
            # Chroma 0.4.24's $ne omits legacy records with no source field.
            # Fetch a wider window before dropping old synthetic records.
            vector_results = await self.vector_store.search(
                query_embedding=query_embedding,
                top_k=top_k * 2 if exclude_source else top_k,
                filter_dict=filter_dict,
            )

            results = []
            for result in vector_results:
                metadata = result.metadata or {}
                if exclude_source and metadata.get("source") == exclude_source:
                    continue

                results.append(
                    RetrievedChunk(
                        chunk_id=str(result.id),
                        text=result.text or "",
                        score=float(result.score or 0.0),
                        document_id=str(metadata.get("document_id", "")),
                        filename=str(metadata.get("filename", "")),
                        page=int(metadata.get("page", 1) or 1),
                        char_start=int(metadata.get("char_start", 0) or 0),
                        char_end=int(metadata.get("char_end", 0) or 0),
                        source="vector",
                        metadata=metadata,
                    )
                )

            return results[:top_k]

        except Exception as e:
            logger.error(f"Vector retrieval failed: {e}")
            raise

    async def retrieve_lexical(
        self, query: str, top_k: int, filters: Optional[Dict[str, Any]] = None
    ) -> List[RetrievedChunk]:
        """Sparse retrieval via BM25 keyword matching.

        Pipeline:
            1. Construct filter function from filters dict.
            2. Search BM25Store (tokenize → synonym expand → BM25 score).
            3. Normalize raw BM25 scores to [0, 1] via max normalization.
            4. Convert BM25SearchResult to RetrievedChunk.

        Args:
            query: Query text.
            top_k: Number of candidates to retrieve (typically top_k*2).
            filters: Filter dict supporting document_ids and source.

        Returns:
            List[RetrievedChunk] with source="bm25". Scores normalized to [0, 1].
        """
        if not self.bm25_store:
            return []
        try:
            filters = filters or {}
            selected_ids = filters.get("document_ids")
            if not selected_ids and filters.get("document_id"):
                selected_ids = [filters["document_id"]]
            selected_ids = set(map(str, selected_ids or []))
            page = filters.get("page")
            source = filters.get("source")
            exclude_source = filters.get("exclude_source")

            def matches(doc_id, metadata):
                metadata = metadata or {}
                return (
                    (not selected_ids or str(metadata.get("document_id") or doc_id) in selected_ids)
                    and (page is None or str(metadata.get("page")) == str(page))
                    and (source is None or metadata.get("source") == source)
                    and (exclude_source is None or metadata.get("source") != exclude_source)
                )

            bm25_results = self.bm25_store.search(query, top_k=top_k, filter_func=matches)

            if not bm25_results:
                return []

            max_score = max(r.score for r in bm25_results)
            if max_score <= 0:
                max_score = 1.0

            results = []
            for result in bm25_results:
                normalized_score = result.score / max_score

                metadata = result.metadata or {}

                results.append(
                    RetrievedChunk(
                        chunk_id=result.doc_id,
                        text=result.text or "",
                        score=float(normalized_score),
                        document_id=str(metadata.get("document_id", "")),
                        filename=str(metadata.get("filename", "")),
                        page=int(metadata.get("page", 1) or 1),
                        char_start=int(metadata.get("char_start", 0) or 0),
                        char_end=int(metadata.get("char_end", 0) or 0),
                        source="bm25",
                        metadata=metadata,
                    )
                )

            return results

        except Exception as e:
            logger.error(f"BM25 retrieval failed: {e}")
            raise

    async def _table_retrieve(
        self, query: str, top_k: int, filters: Optional[Dict[str, Any]] = None
    ) -> List[RetrievedChunk]:
        """Structured retrieval via TableStore (SQLite + FTS5).

        Table retrieval preserves row/column structure that is lost when
        tables are flattened into text for vector/BM25 retrieval.

        Pipeline:
            1. Search TableStore (FTS5 full-text or LIKE fallback).
            2. Format table data into readable text.
            3. Convert to RetrievedChunk.

        Args:
            query: Query text.
            top_k: Number of candidates to retrieve (typically top_k, not *2).
            filters: Filter dict supporting document_ids.

        Returns:
            List[RetrievedChunk] with source="table".
        """
        try:
            table_results = await self.table_store.search(query=query, top_k=top_k, filters=filters)

            results = []
            for result in table_results:
                table_text = self._format_table_text(result.get("table_data", []))

                results.append(
                    RetrievedChunk(
                        chunk_id=result.get("id", ""),
                        text=table_text,
                        score=float(result.get("score", 0.0)),
                        document_id=result.get("document_id", ""),
                        filename=result.get("filename", ""),
                        page=int(result.get("page", 1)),
                        source="table",
                        metadata={
                            **result.get("metadata", {}),
                            "table_data": result.get("table_data", []),
                            "table_title": result.get("title", ""),
                        },
                    )
                )

            return results

        except Exception as e:
            logger.error(f"Table retrieval failed: {e}")
            raise

    def _format_table_text(self, table_data: List[List[str]]) -> str:
        """Format 2D table data into pipe-delimited text for LLM consumption.

        Each row is joined with " | " separators (simplified Markdown table).
        Empty cells retain their positions, including MIN/TYP/MAX columns.

        Args:
            table_data: 2D string list, e.g. [["Param", "Min", "Typ", "Max"],
                                              ["VOH", "2.4V", "3.0V", ""]].

        Returns:
            Formatted text, e.g. "Param | Min | Typ | Max\\nVOH | 2.4V | 3.0V".
        """
        if not table_data:
            return ""

        return "\n".join(
            " | ".join("" if cell is None else str(cell) for cell in row)
            for row in table_data if any(cell is not None and str(cell).strip() for cell in row)
        )

    def _rrf_fusion(self, results: List[RetrievedChunk], k: int = 60) -> List[RetrievedChunk]:
        """Reciprocal Rank Fusion of multi-source retrieval results.

        RRF formula (Cormack et al., SIGIR 2009):
            score(d) = Σ_s w_s × 1/(k + rank_s(d) + 1)

        where s is the retrieval source, rank_s(d) is the 0-based rank of
        document d in source s, and k=60 is the smoothing constant.

        Pipeline:
            1. Group results by source.
            2. Sort within each group by score (descending) to determine ranks.
            3. Compute RRF scores: weight × 1/(k + rank + 1).
            4. Accumulate scores for chunks hit by multiple sources.
            5. Deduplicate identical text on the same document page.
            6. Normalize final scores to [0, 1].

        Multi-source consensus: when the same chunk is returned by multiple
        retrieval paths, its RRF scores accumulate, giving it a significant
        boost. This reflects the principle that multi-source agreement
        indicates higher relevance.

        Args:
            results: All retrieval results from vector/bm25/table paths.
            k: RRF smoothing constant. Defaults to 60 (paper recommendation).

        Returns:
            Fused and ranked List[RetrievedChunk] with source="hybrid".
        """
        if not results:
            return []

        results_by_source = {}
        for result in results:
            source = result.source
            if source not in results_by_source:
                results_by_source[source] = []
            results_by_source[source].append(result)

        for source in results_by_source:
            results_by_source[source].sort(key=lambda x: x.score, reverse=True)

        rrf_scores = {}

        for source, source_results in results_by_source.items():
            weight = self.source_weights.get(source, 0.1)

            for rank, result in enumerate(source_results):
                result_id = f"{result.document_id}_{result.chunk_id}"

                rrf_score = weight * (1 / (k + rank + 1))

                if result_id not in rrf_scores:
                    rrf_scores[result_id] = {
                        "result": result,
                        "score": rrf_score,
                        "sources": [source],
                        "rank": rank,
                    }
                else:
                    rrf_scores[result_id]["score"] += rrf_score
                    rrf_scores[result_id]["sources"].append(source)
                    rrf_scores[result_id]["rank"] = min(rrf_scores[result_id]["rank"], rank)

        seen_hashes = {}
        to_remove = set()

        for result_id, score_data in rrf_scores.items():
            result = score_data["result"]
            content_hash = (result.document_id, result.page, " ".join(result.text.split()))

            if content_hash not in seen_hashes:
                seen_hashes[content_hash] = result_id
            else:
                existing_id = seen_hashes[content_hash]
                if score_data["score"] > rrf_scores[existing_id]["score"]:
                    to_remove.add(existing_id)
                    seen_hashes[content_hash] = result_id
                else:
                    to_remove.add(result_id)

        for rid in to_remove:
            del rrf_scores[rid]

        sorted_results = sorted(rrf_scores.values(), key=lambda x: x["score"], reverse=True)

        peak = sorted_results[0]["score"]
        for item in sorted_results:
            result = item["result"]
            result.source = "hybrid"
            result.metadata = {
                **result.metadata,
                "fusion_sources": item["sources"],
                "rrf_score": item["score"],
                "fusion_rank": item["rank"],
            }
            if peak > 0:
                result.score = item["score"] / peak

        return [item["result"] for item in sorted_results]

    def _limit_results(self, ranked, top_k, max_per_doc):
        """Apply the presentation cap after all candidates have been reranked."""
        base_cap = max_per_doc if max_per_doc is not None else getattr(self.settings, "MAX_RESULTS_PER_DOC", 5)
        cross_cap = max(base_cap, getattr(self.settings, "MAX_RESULTS_PER_DOC_CROSS_SOURCE", 8))
        by_document = {}
        final = []
        for result in ranked:
            count = by_document.get(result.document_id, 0)
            from_multiple_sources = len(result.metadata["fusion_sources"]) > 1
            cap = cross_cap if from_multiple_sources else base_cap
            if count < cap:
                final.append(result)
                by_document[result.document_id] = count + 1
                if len(final) >= top_k:
                    break
        return final

    def _get_query_embedding(self, query: str):
        """Generate query embedding vector via EmbeddingManager.

        Uses asymmetric encoding: embed_query adds instruction prompts
        (model-specific) to the query, while documents are encoded
        without instructions via embed_batch.

        Args:
            query: Raw query text.

        Returns:
            Embedding as List[float] for ChromaDB compatibility.

        Raises:
            RuntimeError: If EmbeddingManager is not initialized.
            TypeError: If embedding type cannot be converted to list.
        """
        if self.embedding_manager is None:
            error_msg = (
                "EmbeddingManager not initialized. "
                "Pass embedding_manager to HybridRetriever or check vector store init."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        try:
            embedding = self.embedding_manager.embed_query(query)

            if isinstance(embedding, (list, tuple)):
                return list(embedding)
            elif hasattr(embedding, "tolist"):
                return embedding.tolist()
            else:
                raise TypeError(f"Cannot convert embedding type: {type(embedding)}")

        except Exception as e:
            logger.error(f"Query embedding generation failed: {e}")
            raise
