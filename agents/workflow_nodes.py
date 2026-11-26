"""
Agentic RAG 节点实现 - 意图路由、检索增强与引脚分析

本文件提供工作流图中所有可执行节点的具体逻辑，与 workflow_graph.py(图编排层)配合使用:
  - workflow_graph.py: 定义节点连接(图拓扑、边路由规则)
  - workflow_nodes.py(本文件): 定义节点行为(每个节点的业务逻辑)

节点与类的对应关系:
  - intent_router   → IntentRouterNode.__call__()
  - text_retrieval  → TextAnalysisNodes.text_retrieval_node()
  - response_generation → TextAnalysisNodes.response_generation_node()
  - pinout          → UnifiedAgentNodes.pinout_node()

AgentState 字段在各节点间的流转:
  输入: { query, session_id, user_context: { selected_document_ids } }
    ↓ intent_router_node: 读取 query → 写入 intent
    ↓ text_retrieval_node: 读取 query, document_ids → 写入 retrieval_results, citations
    ↓ response_generation_node: 读取 query, retrieval_results → 写入 final_response
    ↓ pinout_node: 读取 query, document_ids → 写入 extracted_data, citations, final_response
"""

import asyncio
import logging
import re
from decimal import Decimal
from itertools import zip_longest
from typing import Dict, List, Any, Optional, Tuple

from core.config import get_settings
from knowledge.pinout_library import CommonPinoutLibrary
from retrieval.evidence_coverage import (
    clauses, comparable_temperature_steps, quantity_anchors, temperature_step_passages, temperature_step_query,
)
from retrieval.verified_passages import condition_search_terms, find_condition_excerpt, find_verified_excerpt

logger = logging.getLogger(__name__)


class IntentRouterNode:
    """基于正则匹配的轻量级意图识别节点。

    采用正则而非LLM做意图识别的原因:
    零延迟(毫秒级)、零成本(不消耗GPU)、可解释(规则透明)、当前场景够用(仅qa/pinout两种意图)。
    """

    INTENT_PATTERNS = {
        "pinout": [
            # 只在明确"查询引脚功能/定义"时触发。
            # 旧规则里的裸 "pin" 会把 "pin 1 标记"、"pinout 页标题" 之类
            # 的原文转录类问题误判为引脚查询,导致请求被短路到 pinout 节点。
            r"引脚(?:功能|定义|配置|说明|列表|有哪些|编号|名称|分配)",
            r"(?:各|每个|所有)\s*引脚",
            r"管脚(?:功能|定义|配置|说明|列表)",
            r"pinout",
            r"pin\s*(?:assignment|definition|function|configuration|out)",
            r"pin\s*\d+\s*(?:是|为|表示|功能|含义|定义)",
        ],
    }

    _compiled_patterns = None

    @classmethod
    def _get_compiled_patterns(cls):
        if cls._compiled_patterns is None:
            cls._compiled_patterns = {
                intent: [re.compile(p, re.IGNORECASE) for p in patterns]
                for intent, patterns in cls.INTENT_PATTERNS.items()
            }
        return cls._compiled_patterns

    def __call__(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """执行意图识别，返回更新后的状态字典。"""
        query = state.get("query", "")
        intent = "qa"

        compiled = self._get_compiled_patterns()
        for intent_type, patterns in compiled.items():
            for pattern in patterns:
                if pattern.search(query):
                    intent = intent_type
                    break
            if intent != "qa":
                break

        logger.info(f"意图识别: query='{query[:50]}...' -> intent={intent}")
        return {**state, "intent": intent}


def _chip_in_results(chip_name: str, results) -> bool:
    """判断检索结果里是否真的出现了该型号（跨器件保护）。"""
    if not chip_name:
        return False
    norm_chip = re.sub(r"[^A-Z0-9]", "", chip_name.upper())
    if not norm_chip:
        return False
    for r in results or []:
        filename = (getattr(r, "filename", "") or "")
        text = (getattr(r, "text", "") or "")
        for hay in (filename, text):
            if norm_chip in re.sub(r"[^A-Z0-9]", "", hay.upper()):
                return True
    return False


class TextAnalysisNodes:
    """文本问答节点，负责混合检索与证据生成。"""

    TECH_KEYWORDS = ['VCC', 'VDD', 'VS', '±', 'Supply voltage', 'Electrical Characteristics', 'Recommended Operating', 'Absolute Maximum']

    PARAM_PATTERNS = [
        r'VCC±?\s*[=：]\s*[±]?\d+\.?\d*\s*[Vv]?.{0,100}',
        r'VS±?\s*[=：]\s*[±]?\d+\.?\d*\s*[Vv]?.{0,100}',
        r'Supply\s+Voltage[:\s]*[±]?\d+\.?\d*\s*[Vv]?.{0,100}',
        r'Operating\s+Voltage[:\s]*[±]?\d+\.?\d*\s*[Vv]?.{0,100}'
    ]

    def __init__(self, settings=None, retriever=None):
        self.settings = settings or get_settings()
        self._retriever = retriever
        self._llm_client = None
        self._compiled_param_patterns = [re.compile(p, re.IGNORECASE) for p in self.PARAM_PATTERNS]
        self._number_unit_pattern = re.compile(r'±?\d+\.?\d*\s*[VvAaµuMmKkWw]?')

    @property
    def retriever(self):
        if self._retriever is None:
            from retrieval.hybrid_retriever import HybridRetriever
            self._retriever = HybridRetriever(settings=self.settings)
        return self._retriever

    @property
    def llm_client(self):
        """延迟初始化LLM客户端，避免启动时占用GPU显存。"""
        if self._llm_client is None:
            try:
                from core.llm_client import get_llm_client
                self._llm_client = get_llm_client(self.settings)
            except Exception as e:
                logger.warning(f"Failed to initialize llm_client: {e}")
        return self._llm_client

    _TRANSCRIPTION_PATTERN = re.compile(
        r'原文|逐行|逐条|转录|抄写|照抄|逐字|原文照录|完整列出|全部列出|完整转录'
    )
    _COMPARISON = re.compile(
        r"\b(?:compare|comparison|contrast|differ(?:ence|s)?|versus|vs)\b|比较|对比|区别|异同",
        re.IGNORECASE,
    )
    _PART_NUMBER = re.compile(r"\b(?:[A-Z]{2,}\d[A-Z0-9-]*|\d{2}[A-Z]{2,}\d[A-Z0-9-]*)\b", re.I)
    _OPERATING_SUPPLY = re.compile(
        r"wide supply range|(?:single or dual )?supplies can be used in the range",
        re.IGNORECASE,
    )
    _SUPPLY_SPAN = re.compile(r"\b\d+(?:\.\d+)?\s*V\s*(?:to|[-–])\s*(\d+(?:\.\d+)?)\s*V\b", re.I)
    _RANGE_LITERAL = r"\d+(?:\.\d+)?\s*V\s*(?:to|[-–])\s*\d+(?:\.\d+)?\s*V\b"
    _LABELED_SUPPLY_RANGE = re.compile(
        rf"\b(?:power\s+)?supply(?:\s+voltage)?\s+range\s*(?:(?:from|of|is)\s+|:\s*)?{_RANGE_LITERAL}"
        rf"|(?<![\d.+\-−±]){_RANGE_LITERAL}\s+(?:operating\s+)?supply(?:\s+voltage)?\s+range\b",
        re.I,
    )
    _ADC_RESOLUTION_SPAN = re.compile(r"\b\d{1,2}[- ]bit\s+(?:resolution|(?:ΔΣ|SAR)\s+ADC)\b", re.I)

    _PAGE_QUERY = re.compile(r"(?:第\s*(\d+)\s*页|\bpage\s+(\d+)\b)", re.IGNORECASE)
    _COVER_QUERY_MARKERS = ("首页", "封面", "文档标题", "文档名", "文档编号", "文件编号",
                           "document title", "cover page", "first page")

    def _requested_page(self, query: str) -> Optional[int]:
        match = self._PAGE_QUERY.search(query)
        if match:
            return int(match[1] or match[2]) or None
        if any(marker in query.lower() for marker in self._COVER_QUERY_MARKERS):
            return 1
        return None

    def _is_transcription_request(self, query: str) -> bool:
        """判断是否为"逐行/原文转录"类请求(需要放宽长度限制并保留原文)。"""
        return bool(self._TRANSCRIPTION_PATTERN.search(query or ""))

    # 向关键词检索补充规格书中的英文术语，同时保留原问题和工况。
    _PARAM_KEYWORD_MAP = {
        "连续漏电流": "continuous drain current",
        "漏电流": "drain current",
        "P沟道": "p channel",
        "N沟道": "n channel",
        "工作温度": "operating temperature",
        "存储温度": "storage temperature",
        "供电电压": "supply voltage",
        "分流电压": "shunt voltage",
        "总线电压": "bus voltage",
        "导通电阻": "on-state resistance",
        "电源电流": "supply current",
        "静态电流": "quiescent current",
        "关断电流": "shutdown current",
        "采样率": "data rate",
        "分辨率": "resolution",
        "增益带宽积": "gain bandwidth product",
        "功耗": "power dissipation",
        "击穿电压": "breakdown voltage",
        "输入电容": "input capacitance",
        "输出电容": "output capacitance",
        "开启延时": "turn-on delay",
        "上升时间": "rise time",
        "热阻": "thermal resistance",
        "雪崩能量": "avalanche energy",
        "阈值电压": "threshold voltage",
        "封装": "package",
    }

    async def text_retrieval_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Use the same persisted stores as ingestion and preserve the original query."""
        query = state.get("query", "")
        document_ids = list(dict.fromkeys(state.get("user_context", {}).get("selected_document_ids") or []))
        if not query:
            return {**state, "retrieval_results": [], "citations": []}

        aliases = [
            alias for term, alias in self._PARAM_KEYWORD_MAP.items() if term in query
        ]
        page = self._requested_page(query)
        filters = {"exclude_source": "param_record"}
        if page is not None:
            filters["page"] = page

        if (getattr(self.settings, "RAG_BALANCED_COMPARISON", True)
                and 2 <= len(document_ids) <= 8 and self._COMPARISON.search(query)
                and not self._is_transcription_request(query)):
            joint = getattr(self.settings, "RAG_JOINT_COMPARISON", True)
            topic = self._PART_NUMBER.sub("", query)
            topic = re.sub(r"^\s*(?:compare|contrast)\s+(?:the\s+)?", "", topic, flags=re.I)
            topic = re.sub(r"\bof\s+(?:the\s+)?(?:and|versus|vs\.?)\s+(?:the\s+)?", "of the ", topic, flags=re.I)
            topic = re.sub(r"(?:\s+(?:of|the|and|vs\.?|versus|between))+[\s?.!]*$", "", topic, flags=re.I)
            topic = " ".join(topic.split()).strip(" ?.,") or query
            supply = (re.search(r"\bsupply voltage\b|供电电压|电源电压", topic, re.I)
                      and not re.search(r"\babsolute\b|绝对", query, re.I))
            if joint:
                supply = bool((supply or re.search(r"(?:供电|电源)(?:电压)?范围|\bsupply ranges?\b", topic, re.I))
                              and not re.search(r"\babsolute\b|绝对", query, re.I))
            step = joint and temperature_step_query(topic)
            if supply:
                topic = "operating supply voltage range single supply dual supply"
            elif step:
                topic = "temperature resolution temperature step LSB size"
            lexical_topic = " ".join([topic, *dict.fromkeys(aliases)])
            gate = asyncio.Semaphore(2)

            async def retrieve_one(document_id):
                async with gate:
                    hits = await self.retriever.retrieve(
                        topic, top_k=6, document_ids=[document_id],
                        filters=filters, max_per_doc=6, lexical_query=lexical_topic,
                    )
                if supply:
                    for hit in hits:
                        if joint and (match := self._LABELED_SUPPLY_RANGE.search(hit.text)):
                            hit.metadata["comparison_excerpt"] = match[0]
                        elif match := self._OPERATING_SUPPLY.search(hit.text):
                            hit.metadata["comparison_excerpt"] = hit.text[match.start():match.start() + 155]
                    hits.sort(key=lambda hit: not hit.metadata.get("comparison_excerpt"))
                elif step:
                    terms = "temperature resolution step LSB"
                    try:
                        extra = await self.retriever.retrieve_lexical(
                            terms, 8, {**filters, "document_ids": [document_id]},
                        )
                    except Exception:
                        logger.warning("Temperature step lexical lookup failed", exc_info=True)
                        extra = []
                    seen = {hit.chunk_id for hit in hits}
                    hits.extend(hit for hit in extra if hit.chunk_id not in seen)
                    for hit, passage, *_ in temperature_step_passages(hits):
                        hit.metadata.setdefault("comparison_excerpt", passage)
                    hits.sort(key=lambda hit: not hit.metadata.get("comparison_excerpt"))
                return hits if joint else hits[:2]

            per_document = await asyncio.gather(*(retrieve_one(doc_id) for doc_id in document_ids))
            results = [item for group in zip_longest(*per_document) for item in group if item is not None]
            return {**state, "retrieval_results": results, "comparison_documents": document_ids,
                    "comparison_query": topic, "comparison_supply": bool(supply), "citations": []}

        results = await self.retriever.retrieve(
            query,
            top_k=12,
            document_ids=document_ids,
            filters=filters,
            max_per_doc=12,
            lexical_query=" ".join([query, *dict.fromkeys(aliases)]),
            focus_tables=bool(getattr(self.settings, "RAG_FOCUS_TABLE_EVIDENCE", True)
                              and len(set(document_ids)) == 1
                              and not self._is_transcription_request(query)),
        )
        if page == 1:
            # PDF 缩略页、表格和正文都可能命中首页，按正文位置优先取页首。
            results.sort(key=lambda result: (
                (result.metadata or {}).get("source") != "pdf", result.char_start
            ))
        lexical_evidence = []
        if (getattr(self.settings, "RAG_CONDITION_EVIDENCE", True) and len(document_ids) == 1
                and page is None and not self._is_transcription_request(query)):
            if terms := condition_search_terms(
                query, exact_gain=getattr(self.settings, "RAG_EXACT_GAIN_EVIDENCE", True),
            ):
                try:
                    lexical_evidence = await self.retriever.retrieve_lexical(
                        terms, 12, {**filters, "document_ids": document_ids},
                    )
                except Exception:
                    logger.warning("Conditional evidence lookup failed", exc_info=True)
        return {**state, "retrieval_results": results, "condition_candidates": lexical_evidence, "citations": []}

    def _extract_citations(self, results: List[Any]) -> List[Dict]:
        """从检索结果中提取最相关的引用，最多2个。

        相关性评分: 检索分数 + 关键词匹配加分(×0.1) + 数值出现加分(+0.2)
        """
        citations = []
        relevant_results = []

        for i, result in enumerate(results[:5]):
            if hasattr(result, 'filename'):
                filename = result.filename
                page = result.page
                content = result.text
                score = getattr(result, 'score', 0)
                section = result.metadata.get('section', '') if result.metadata else ''
            else:
                filename = result.get("source", "未知文档")
                page = result.get("page", 1)
                content = result.get("content", "")
                score = result.get("score", 0)
                section = result.get("section", "")

            relevance_score = score

            if content:
                keyword_count = sum(1 for kw in self.TECH_KEYWORDS if kw in content)
                relevance_score += keyword_count * 0.1

                has_numbers = bool(self._number_unit_pattern.search(content))
                if has_numbers:
                    relevance_score += 0.2

            relevant_results.append({
                'filename': filename,
                'page': page,
                'content': content,
                'score': relevance_score,
                'section': section,
                'index': i
            })

        relevant_results.sort(key=lambda x: x['score'], reverse=True)
        top_results = relevant_results[:2]

        logger.debug(f"引用筛选: 从{len(results)}个结果中选择了{len(top_results)}个最相关的引用")

        for result in top_results:
            content = result['content']
            snippet = content[:200] if content else ""

            for pattern in self._compiled_param_patterns:
                match = pattern.search(content)
                if match:
                    snippet = match.group(0).strip()
                    break

            citation = {
                "file": result['filename'],
                "page": result['page'],
                "text_snippet": snippet,
                "section": result['section']
            }
            citations.append(citation)

        return citations

    _CITATION_MARKER = re.compile(r"\[(\d{1,3})\]")
    _NO_EVIDENCE = "当前检索结果中没有足够的证据回答这个问题。"
    _REFUSAL = re.compile(
        r"^(?:当前检索结果中没有足够的证据|文档中未找到相关内容|"
        r"(?:the )?(?:provided |retrieved )?(?:document|sources?|context|evidence) "
        r"(?:does not|do not|is insufficient|are insufficient)|"
        r"(?:there is )?insufficient (?:evidence|information))",
        re.IGNORECASE,
    )

    def _build_evidence_context(
        self, query: str, results: List[Any], comparison: bool = False, joint: bool = False,
    ) -> Tuple[str, List[Dict]]:
        """Number the exact snippets that reach the model, in retrieval order."""
        transcription = self._is_transcription_request(query)
        budget = 8000 if transcription else 4200
        excerpt_limit = 3600 if transcription else (500 if comparison else 1400)
        snippets, citations = [], []
        seen = set()
        doc_counts = {}
        docs = {result.document_id for result in results} if joint else set()
        per_doc_limit = max(1, 8 // len(docs)) if docs else 0

        focused_rows = [
            result for result in results if (result.metadata or {}).get("focused_row")
        ] if not transcription else []
        if focused_rows:
            # These are complete table rows with the requested conditions.
            # Extra excerpts often contain competing modes or nearby rows.
            results = focused_rows[:3]

        if joint and docs:
            excerpt_limit = min(excerpt_limit, max(200, budget // len(docs) - 100))

        for result in results:
            metadata = result.metadata or {}
            if (metadata.get("source") == "param_record"
                    or joint and doc_counts.get(result.document_id, 0) >= min(per_doc_limit, 2)):
                continue
            content = (result.text or "").strip()
            if not content:
                continue
            excerpt = metadata.get("comparison_excerpt")
            key = (result.document_id, result.page, " ".join((excerpt if joint and excerpt else content).split()))
            if key in seen:
                continue

            filename = result.filename or "未知文档"
            section = metadata.get("section") or metadata.get("table_title") or ""
            label = f"[{len(citations) + 1}] {filename} 第{result.page}页"
            if section:
                label += f" {section}"
            column_header = metadata.get("table_header")
            if excerpt:
                content = excerpt
            model_text = metadata.get("focused_text") or content
            if column_header and model_text == content:
                label += f"\n表头 | {column_header}"

            limit = min(budget - len(label) - 3, excerpt_limit)
            if limit <= 0:
                break
            if len(model_text) > limit:
                # Whole-table candidates can be large. Prefer intact lines and
                # let later candidates provide rows past this excerpt.
                excerpt = model_text[:limit]
                if "\n" in excerpt:
                    excerpt = excerpt.rsplit("\n", 1)[0]
                elif metadata.get("source") == "table_text":
                    continue
                model_text = excerpt.rstrip() + "…"
                content = model_text

            entry = f"{label}\n{model_text}"
            if joint and len(docs) > 1 and doc_counts.get(result.document_id, 0) and budget < 500:
                continue
            snippets.append(entry)
            citations.append({
                "file": filename,
                "page": result.page,
                "text_snippet": f"{column_header}\n{content}" if column_header else content,
                "section": section,
                "document_id": result.document_id,
                "chunk_id": result.chunk_id,
                "focused_row": bool(metadata.get("focused_row")),
                "citation_literal": metadata.get("citation_literal"),
            })
            seen.add(key)
            doc_counts[result.document_id] = doc_counts.get(result.document_id, 0) + 1
            budget -= len(entry) + 2
            if len(citations) >= (10 if transcription else 8) or budget < 120:
                break

        return "\n\n".join(snippets), citations

    async def _prioritize_context(self, query, results):
        parts = clauses(query)
        if len(parts) < 2 or not results or any((item.metadata or {}).get("focused_row") for item in results):
            return results
        pinned = {results[0].chunk_id: results[0]}
        missing = quantity_anchors(query) - quantity_anchors(results[0].text)
        for hit in results[1:]:
            if anchors := missing & quantity_anchors(hit.text):
                pinned.setdefault(hit.chunk_id, hit)
                missing -= anchors
                if len(pinned) >= 3 or not missing:
                    break
        ranks = await asyncio.gather(*(self.retriever.rank_passages(part, results) for part in parts[-2:]))
        for rank in ranks:
            if rank:
                pinned.setdefault(rank[0].chunk_id, rank[0])
        groups = {}
        for hit in results:
            if hit.chunk_id in pinned:
                continue
            source = (hit.metadata or {}).get("source") or hit.source
            table = source in {"table", "table_text"} or hit.text.count(" | ") >= 4
            groups.setdefault((hit.document_id, hit.page, table), []).append(hit)
        diversified = [hit for group in zip_longest(*groups.values()) for hit in group if hit is not None]
        return [*pinned.values(), *diversified]

    def _supply_comparison_calculation(self, query, results, evidence):
        """Add an interval check only for one explicit voltage and consistent ranges."""
        targets = {value for value, unit in quantity_anchors(query) if unit == "v"}
        if len(targets) != 1 or len({item["document_id"] for item in evidence}) != 2:
            return None
        target = next(iter(targets))
        observed = {}
        for hit in results:
            for match in self._LABELED_SUPPLY_RANGE.finditer(hit.text):
                values = sorted(value for value, unit in quantity_anchors(match[0]) if unit == "v")
                if len(values) == 2:
                    observed.setdefault(hit.document_id, set()).add(tuple(values))

        checks = []
        for number, item in enumerate(evidence, 1):
            doc_id = item["document_id"]
            if (any(check["document_id"] == doc_id for check in checks)
                    or not self._LABELED_SUPPLY_RANGE.search(item["text_snippet"])):
                continue
            if len(observed.get(doc_id, ())) != 1:
                return None
            lower, upper = next(iter(observed[doc_id]))
            checks.append({"document_id": doc_id, "file": item["file"], "minimum": str(lower),
                           "maximum": str(upper), "within": lower <= target <= upper, "citation": number})
        if len(checks) == 2:
            return {"operation": "range_check", "voltage": str(target), "checks": checks}
        return None

    def _verified_ratio(self, state, results):
        operands = comparable_temperature_steps(state["query"], state["comparison_documents"], results)
        if not operands:
            return None
        ordered = sorted(operands, key=lambda item: item[2])
        quotient = ordered[1][2] / ordered[0][2]
        ratio = (str(quotient.numerator) if quotient.denominator == 1
                 else f"约 {Decimal(quotient.numerator) / Decimal(quotient.denominator):.6g}")
        names, lines, citations = [], [], []
        for number, (chunk, _, _, value, unit) in enumerate(ordered, 1):
            label = chunk.filename or chunk.document_id
            names.append(label.removesuffix(".pdf"))
            lines.append(f"{names[-1]} 温度步长为 {value} {unit}/LSB [{number}]")
            citations.append({"file": label, "page": chunk.page, "text_snippet": chunk.text,
                              "document_id": chunk.document_id, "chunk_id": chunk.chunk_id})
        summary = (f"两者步长相同 [1] [2]" if quotient == 1
                   else f"{names[0]} 的步长更小，{names[1]} 的步长是它的 {ratio} 倍 [1] [2]")
        answer = {**state, "final_response": "\n".join([*lines, summary]), "citations": citations}
        if state.get("user_context", {}).get("evaluation_trace"):
            answer.update(answer_mode="verified_ratio", context_evidence=citations,
                          comparison_calculation={"ratio": str(quotient),
                                                  "operands": [item[1] for item in ordered]})
        return answer

    async def response_generation_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Generate from numbered evidence and return only referenced sources."""
        query = state.get("query", "")
        comparison = set(state.get("comparison_documents") or [])
        joint = bool(comparison and getattr(self.settings, "RAG_JOINT_COMPARISON", True))
        if comparison and not joint:
            return await self._generate_by_document(state)
        results = state.get("retrieval_results") or []
        if joint and (ratio := self._verified_ratio(state, results)):
            return ratio
        user_context = state.get("user_context") or {}
        trace = bool(user_context.get("evaluation_trace"))
        transcription = self._is_transcription_request(query)
        selected_documents = set(user_context.get("selected_document_ids") or [])
        if (len(selected_documents) == 1
                and not state.get("comparison_subquery") and not transcription):
            proof = None
            mode = "verified_conditions"
            if getattr(self.settings, "RAG_CONDITION_EVIDENCE", True):
                candidates = [*results, *(state.get("condition_candidates") or [])]
                proof = find_condition_excerpt(
                    query, (chunk for chunk in candidates if chunk.document_id in selected_documents),
                    exact_gain=getattr(self.settings, "RAG_EXACT_GAIN_EVIDENCE", True),
                )
                if proof and proof.rule == "gain_bandwidth_at_gain":
                    mode = "verified_gain"
            if proof is None and getattr(self.settings, "RAG_VERIFIED_PASSAGES", True):
                proof = find_verified_excerpt(query, results)
                mode = "verified_excerpt"
            if proof:
                selected = {**state, "final_response": self._NO_EVIDENCE, "citations": []}
                if proof.chunk is not None:
                    chunk = proof.chunk
                    meta = chunk.metadata or {}
                    citation = {
                        "file": chunk.filename or "未知文档", "page": chunk.page,
                        "section": meta.get("section") or meta.get("table_title") or "",
                        "text_snippet": chunk.text,
                        "document_id": chunk.document_id, "chunk_id": chunk.chunk_id,
                    }
                    selected.update(final_response=f"规格书证据：{proof.excerpt} [1]", citations=[citation])
                if trace:
                    selected.update(
                        evaluation_context=None, context_evidence=selected["citations"],
                        answer_mode=mode if proof.chunk is not None else "verified_abstention",
                        verified_excerpt={"rule": proof.rule, "text": proof.excerpt},
                    )
                    if proof.reason:
                        selected["verified_excerpt"]["reason"] = proof.reason
                return selected

        if (getattr(self.settings, "RAG_EVIDENCE_COVERAGE", False) and not joint and not transcription
                and not state.get("comparison_subquery") and self._requested_page(query) is None):
            results = await self._prioritize_context(query, results)
        context, evidence = self._build_evidence_context(
            query, results, comparison=joint or bool(state.get("comparison_subquery")), joint=joint,
        )
        if joint and state.get("comparison_supply"):
            calculation = self._supply_comparison_calculation(query, results, evidence)
            if calculation:
                voltage = calculation["voltage"]
                summary = "\n".join(
                    f"{item['file']}：{voltage} V "
                    f"{'在工作范围内，可用' if item['within'] else '超出工作范围，不可用'} [{item['citation']}]"
                    for item in calculation["checks"]
                )
                context += f"\n\n【程序按工作范围核对】\n{summary}"
                state = {**state, "comparison_calculation": calculation}
        if trace:
            state = {**state, "evaluation_context": context, "context_evidence": evidence}
            if joint:
                state["answer_mode"] = "joint_comparison"
        if not evidence:
            return {**state, "final_response": self._NO_EVIDENCE, "citations": []}

        try:
            answer = await asyncio.to_thread(
                self.llm_client.generate_answer,
                query=query,
                context=context,
                max_new_tokens=1024 if transcription else 256,
                temperature=0.1,
                transcription=transcription,
                comparison=bool(state.get("comparison_subquery")),
                joint_comparison=joint,
            )
        except Exception as exc:
            logger.exception("Evidence-based answer generation failed")
            return {**state, "error": str(exc), "final_response": "LLM生成答案失败", "citations": []}

        answer = (answer or "").strip()
        if trace:
            state["model_answer"] = answer
        draft = answer
        required_docs = {item["document_id"] for item in evidence} if joint else None
        answer, references = self._checked_citations(answer, evidence, required_docs)
        if self._REFUSAL.match(answer):
            return {**state, "final_response": self._NO_EVIDENCE, "citations": []}
        if (not references and answer and self._CITATION_MARKER.sub("", answer).strip()
                and not transcription and getattr(self.settings, "RAG_CITATION_REPAIR", True)):
            try:
                repaired = await asyncio.to_thread(
                    self.llm_client.repair_citations, query, context, answer
                )
                if trace:
                    state["citation_repair"] = (repaired or "").strip()
                answer, references = self._checked_citations((repaired or "").strip(), evidence, required_docs)
            except Exception:
                logger.warning("Citation repair failed", exc_info=True)
            if not references and len(evidence) == 1 and evidence[0]["citation_literal"]:
                literal = evidence[0]["citation_literal"]
                if self._matches_supply_literal_answer(query, draft, literal):
                    answer, references = f"{draft} [1]", [1]
                    if trace:
                        state["citation_literal_verified"] = True
        if not references or self._REFUSAL.match(answer):
            return {**state, "final_response": self._NO_EVIDENCE, "citations": []}

        citation_order = {number: index for index, number in enumerate(references, 1)}
        answer = self._CITATION_MARKER.sub(
            lambda match: f"[{citation_order[int(match[1])]}]", answer
        )
        return {
            **state,
            "final_response": answer,
            "citations": [evidence[n - 1] for n in references],
        }

    async def _generate_by_document(self, state):
        """Generate each comparison claim against just its device's evidence."""
        topic = state["comparison_query"]
        question = (f"What is this device's {topic}?" if not state.get("comparison_supply")
                    else "What is this device's operating supply voltage range for single and dual supplies?")
        outputs, citations, contexts, evidence, calls = [], [], [], [], []
        trace = bool(state.get("user_context", {}).get("evaluation_trace"))
        for doc_id in state["comparison_documents"]:
            hits = [r for r in state["retrieval_results"] if r.document_id == doc_id]
            result = await self.response_generation_node({
                **state, "query": question,
                "retrieval_results": hits, "comparison_documents": [], "comparison_subquery": True,
            })
            label = (hits[0].filename or doc_id) if hits else doc_id
            if extract := self._comparison_quote(result, state.get("comparison_supply"), topic):
                source, passage = extract
                result["final_response"] = f"规格书原文：{passage} [1]"
                result["citations"] = [source]
                result["comparison_quote"] = passage
            offset = len(citations)
            answer = self._CITATION_MARKER.sub(
                lambda match: f"[{offset + int(match[1])}]", result["final_response"]
            )
            outputs.append(f"【{label}】\n{answer}")
            citations.extend(result.get("citations") or [])
            if trace:
                contexts.append(f"【{label}】\n{result.get('evaluation_context', '')}")
                evidence.extend(result.get("context_evidence") or [])
                calls.append({"document_id": doc_id, "query": result["query"],
                              "context": result.get("evaluation_context"), "draft": result.get("model_answer"),
                              "citation_repair": result.get("citation_repair"),
                              "comparison_quote": result.get("comparison_quote"),
                              "final_response": result["final_response"]})
        result = {**state, "final_response": "\n\n".join(outputs) if citations else self._NO_EVIDENCE,
                  "citations": citations}
        if trace:
            result.update(evaluation_context="\n\n".join(contexts), context_evidence=evidence,
                          comparison_calls=calls)
        return result

    def _comparison_quote(self, result, supply, topic):
        """Return a literal evidence excerpt when a supported field is missed."""
        for item in result.get("context_evidence") or result.get("citations") or []:
            text = item["text_snippet"]
            if supply and (start := self._OPERATING_SUPPLY.match(text)):
                if span := self._SUPPLY_SPAN.search(text, start.end()):
                    expected = re.sub(r"\s+", "", span[0]).casefold()
                    actual = re.sub(r"\s+", "", result["final_response"]).casefold()
                    # A correct upper bound alone cannot validate a range when
                    # the model changes the lower bound's sign or its units.
                    if not result.get("citations") or not re.search(
                        rf"(?<![\d.+±\-−]){re.escape(expected)}", actual
                    ):
                        return item, text[:span.end()]
                    if (getattr(self.settings, "RAG_VERIFIED_PASSAGES", True)
                            and re.search(r"\d+(?:\.\d+)?v(?![a-z])", actual.replace(expected, "", 1))):
                        return item, text[:span.end()]
            elif "resolution" in topic.casefold() and not result.get("citations"):
                if span := self._ADC_RESOLUTION_SPAN.search(text):
                    return item, span[0]
        return None

    def _matches_supply_literal_answer(self, query, answer, literal):
        """Accept only the bare range or a simple statement of that exact range."""
        match = self._PART_NUMBER.search(query)
        if not match:
            return False
        part = re.escape(match[0])
        answer = answer.strip().rstrip("。.")
        answer = re.sub(
            rf"^(?:the\s+)?{part}\s+operates\s+over\s+a\s+supply\s+voltage\s+range\s+of\s+",
            "", answer, flags=re.I,
        )
        answer = re.sub(rf"^{part}\s*的(?:电源|供电)电压范围(?:是|为)\s*", "", answer, flags=re.I)

        def normalize(text):
            return re.sub(r"\s+|\bto\b|[–−至]", lambda m: "" if m[0].isspace() else "-", text, flags=re.I).casefold()

        return normalize(answer) == normalize(literal)

    def _checked_citations(self, answer, evidence, required_docs=None):
        """Check that each citation refers to an existing evidence passage."""
        if len(evidence) == 1 and evidence[0]["focused_row"] and "[1]" in answer:
            lines = answer.splitlines()
            prose = self._CITATION_MARKER.sub("", "\n".join(lines))
            last = self._CITATION_MARKER.fullmatch(lines[-1].strip()) if lines else None
            if last and int(last[1]) > 1:
                literal = re.compile(rf"(?<![\w.]){re.escape(last[1])}(?![\d.])")
                if literal.search(prose) and literal.search(evidence[0]["text_snippet"]):
                    # Preserve the valid citation and remove a duplicated
                    # value such as a trailing "[200]", not an unsupported claim.
                    answer = "\n".join(lines[:-1]).strip()
        references = list(dict.fromkeys(int(n) for n in self._CITATION_MARKER.findall(answer)))
        prose = self._CITATION_MARKER.sub("", answer).strip()
        if required_docs:
            prose = re.sub(
                r"\b[A-Z0-9_.-]+\s*\.?\s*pdf\b\s*(?:第\s*\d+\s*页|page\s+\d+)?",
                "", prose, flags=re.I,
            )
        if (not references or any(n < 1 or n > len(evidence) for n in references)
                or not any(char.isalnum() for char in prose)):
            return answer, []
        if required_docs and {evidence[n - 1]["document_id"] for n in references} != required_docs:
            return answer, []
        return answer, references


class UnifiedAgentNodes:
    """统一Agent节点集合，组合意图路由、文本分析和引脚分析能力。

    使用组合模式(Composition)复用TextAnalysisNodes和IntentRouterNode，
    所有节点方法签名一致(state → state)，便于LangGraph注册。
    """

    CHIP_NAME_PATTERN = re.compile(
        r'\b('
        r'\d{2}[A-Z]{2,}\d+[A-Z0-9-]*'
        r'|[A-Z]{2,}\d+[A-Z0-9.-]*'
        r'|SN\d+[A-Z]+\d*'
        r'|NE\d+[A-Z0-9.-]*'
        r'|LM\d+[A-Z0-9.-]*'
        r'|CD\d+[A-Z0-9.-]*'
        r'|STM32[A-Z0-9.-]+'
        r'|STM8[A-Z0-9.-]*'
        r'|ATmega\d+[A-Z0-9.-]*'
        r'|ATtiny\d+[A-Z0-9.-]*'
        r'|AT89[A-Z0-9.-]*'
        r'|ESP32[A-Z0-9.-]*'
        r'|ESP8266[A-Z0-9.-]*'
        r'|CH340[A-Z0-9.-]*'
        r'|GD32[A-Z0-9.-]*'
        r'|HT32[A-Z0-9.-]*'
        r'|MSP430[A-Z0-9.-]*'
        r'|PIC\d+[A-Z0-9.-]*'
        r'|XC6206[A-Z0-9.-]*'
        r'|AMS1117[A-Z0-9.-]*'
        r'|ULN2003[A-Z0-9.-]*'
        r'|ULN2004[A-Z0-9.-]*'
        r'|TPS\d+[A-Z0-9.-]*'
        r'|CAT811[A-Z0-9.-]*'
        r'|W25Q\d+[A-Z0-9.-]*'
        r')\b',
        re.IGNORECASE
    )

    UNICODE_TRANS = str.maketrans({
        '\u2013': '-', '\u2014': '-', '\u2018': "'", '\u2019': "'",
        '\u201c': '"', '\u201d': '"'
    })

    COMMON_PACKAGES = {
        'DIP': re.compile(r'\b(?:PD)?DIP-?\d+\b', re.IGNORECASE),
        'SOIC': re.compile(r'\bSOIC-?\d+\b', re.IGNORECASE),
        'TSSOP': re.compile(r'\bTSSOP-?\d+\b', re.IGNORECASE),
        'SSOP': re.compile(r'\bSSOP-?\d+\b', re.IGNORECASE),
        'QFN': re.compile(r'\bQFN-?\d+\b', re.IGNORECASE),
        'QFP': re.compile(r'\bQFP-?\d+\b', re.IGNORECASE),
        'PLCC': re.compile(r'\bPLCC-?\d+\b', re.IGNORECASE),
        'LGA': re.compile(r'\bLGA-?\d+\b', re.IGNORECASE),
        'BGA': re.compile(r'\bBGA-?\d+\b', re.IGNORECASE),
        'SOP': re.compile(r'\bSOP-?\d+\b', re.IGNORECASE),
        'SOT': re.compile(r'\bSOT-?\d+\b', re.IGNORECASE),
        'TO': re.compile(r'\bTO-?\d+\b', re.IGNORECASE),
    }

    PIN_PATTERNS = [
        re.compile(r'(1IN\+|2IN\+|1IN\-|2IN\-|1OUT|2OUT|VCC\+|VCC\-|GND|VEE|VSS|OUT1|OUT2)\s+(\d+)\s', re.IGNORECASE | re.MULTILINE),
        re.compile(r'(\d+)\s+(1IN\+|2IN\+|1IN\-|2IN\-|1OUT|2OUT|VCC\+|VCC\-|GND|VEE|VSS|OUT1|OUT2)\s', re.IGNORECASE | re.MULTILINE),
        re.compile(r'Pin\s+(\d+)[:\s\-]+([A-Za-z][A-Za-z0-9_/-]{0,15})', re.IGNORECASE | re.MULTILINE),
        re.compile(r'(\d+)\s*[-]\s*([A-Za-z][A-Za-z0-9_+/-]{0,15})\s*(?!\s*\d{4}\b)', re.IGNORECASE | re.MULTILINE),
        re.compile(r'([A-Za-z][A-Za-z0-9_+/-]{0,15})\s*[-]\s*(\d+)', re.IGNORECASE | re.MULTILINE),
        re.compile(r'^\s*(\d+)\s+([A-Za-z][A-Za-z0-9_+/-]{1,10})\s*$', re.IGNORECASE | re.MULTILINE),
        re.compile(r'(\d+)\s+(GPIO\d+|[A-Za-z][A-Za-z0-9_+/-]{1,20})\s+(?:IO|电源|输入|输出|模拟|特殊)', re.IGNORECASE | re.MULTILINE),
        re.compile(r'(\d+)\s+(GPIO\d+)\b', re.IGNORECASE | re.MULTILINE),
        re.compile(r'(\d+)\s+([A-Z][A-Z0-9_]{1,20})\s+(?:VDD|VSS|IO|电源|GND)', re.IGNORECASE | re.MULTILINE),
    ]

    def __init__(self, settings=None, retriever=None):
        """初始化统一Agent节点集合，组合意图路由和文本分析能力。"""
        self.settings = settings or get_settings()
        self.intent_router = IntentRouterNode()
        self.text_nodes = TextAnalysisNodes(self.settings, retriever=retriever)
        logger.info("UnifiedAgentNodes initialized")

    @property
    def retriever(self):
        """代理到TextAnalysisNodes的检索器。"""
        return self.text_nodes.retriever

    async def intent_router_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """意图路由节点，async包装以保持LangGraph节点签名一致。"""
        return self.intent_router(state)

    async def text_retrieval_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """文本检索节点，代理到TextAnalysisNodes。"""
        return await self.text_nodes.text_retrieval_node(state)

    async def response_generation_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """响应生成节点，代理到TextAnalysisNodes。"""
        return await self.text_nodes.response_generation_node(state)

    async def pinout_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """引脚分析节点：提取芯片引脚定义，支持三级降级策略。

        工作流位置: intent_router → pinout(本节点) → END

        三级降级策略:
          1) 从文档检索结果中解析引脚定义
          2) 降级到标准引脚库(CommonPinoutLibrary)
          3) 引脚数量与标准库不匹配时降级

        降级触发条件(任一满足即降级):
          - 无引脚数据 / 引脚数<4 / io类型占比≥50% / 有重复引脚名
          - 无有意义引脚名 / 含垃圾数据 / 引脚编号不连续
        """
        try:
            query = state.get("query", "")
            document_ids = [str(d) for d in state.get("user_context", {}).get("selected_document_ids", [])]

            chip_name = self._extract_chip_name(query)
            if not chip_name:
                return {**state, "final_response": "请提供芯片型号以查询引脚信息。"}

            pinout_data = []
            package = None
            results = []
            citations = []
            chip_missing_in_doc = False
            pin_source = ""

            if self.retriever:
                retrieval_queries = [
                    f"{chip_name} 引脚定义 pinout 封装",
                    f"{chip_name} pin assignment pinout pin name pin number",
                    f"{chip_name} 引脚功能 引脚编号 pin description",
                ]
                for rq in retrieval_queries:
                    try:
                        results = await self.retriever.retrieve(rq, document_ids=document_ids)
                        if results:
                            logger.info(f"引脚检索命中: query='{rq}', results={len(results)}")
                            break
                        else:
                            logger.debug(f"引脚检索无结果: query='{rq}'")
                    except Exception as e:
                        logger.error(f"引脚检索异常: query='{rq}', error={e}")
                        results = []

                if not results and document_ids:
                    logger.warning(f"所有检索查询均无结果(chip={chip_name}, doc_ids={document_ids}), 尝试无过滤检索")
                    try:
                        results = await self.retriever.retrieve(
                            f"{chip_name} pinout pin definition",
                            top_k=30,
                        )
                        if results:
                            results = [r for r in results if r.document_id in document_ids] if document_ids else results
                            logger.info(f"无过滤检索后按doc_id过滤: 剩余{len(results)}条")
                    except Exception as e:
                        logger.error(f"无过滤检索也失败: {e}")
                        results = []

                # 跨器件保护：文档里若根本没有该型号，检索到的只是"别的器件"的
                # 页面；此时解析出的引脚属于别的器件，绝不能挂到本次查询的型号上。
                if results and not _chip_in_results(chip_name, results):
                    logger.warning(
                        f"跨器件保护: 文档检索结果中未出现 {chip_name}，"
                        f"拒绝复用其它器件的引脚数据"
                    )
                    chip_missing_in_doc = True
                    results = []

                pinout_data = self._parse_pinout_results(results) if results else []
                package = self._extract_package_from_results(results) if results else None
                # 从误抽的"引脚名"里反推封装（如名字是 "PG-DSO"、"SOT-23"）：
                # 这些其实是封装文字，恰好能给出引脚数上限
                if pinout_data and not package:
                    for _p in pinout_data:
                        _m = re.search(
                            r"(?:PG[-–]?|SOT[-–]?|SOIC[-–]?|SSOP[-–]?|TSSOP[-–]?|QFN[-–]?|DSO[-–]?)(\d{1,3})",
                            (_p.get("name", "") or ""), re.IGNORECASE)
                        if _m:
                            package = f"DSO-{_m.group(1)}"
                            logger.info(f"从引脚名反推封装: {package}")
                            break
                # 封装-引脚数合理性过滤：从正文正则抽出的"引脚"常是页面上
                # 其它数字（如 IEC61249-2、版本号）。若已知封装最大引脚数
                # （PG-DSO-8 → 8），超出的一律丢弃；滤完不足 4 脚视为无数据。
                if pinout_data and package:
                    max_pins = self._get_max_pins_for_package(package)
                    if max_pins and 4 <= max_pins <= 64:
                        kept = [p for p in pinout_data
                                if 1 <= int(p.get("number", 0) or 0) <= max_pins]
                        dropped = len(pinout_data) - len(kept)
                        if dropped:
                            logger.warning(
                                f"引脚合理性过滤: 封装 {package} 最多 {max_pins} 脚，"
                                f"丢弃 {dropped} 条超范围记录"
                            )
                        pinout_data = kept
                pin_source = "document" if pinout_data else ""
                citations = self.text_nodes._extract_citations(results) if results else []

                if not pinout_data:
                    logger.warning(f"检索结果无法解析出引脚数据: chip={chip_name}, results_count={len(results)}")
            else:
                logger.warning(f"检索器不可用(chip={chip_name}), 降级到标准引脚库")

            io_count = sum(1 for p in pinout_data if p.get("pin_type") == "io")
            total_pins = len(pinout_data)
            io_ratio = io_count / total_pins if total_pins > 0 else 1.0

            pin_names = [p.get("name", "").upper() for p in pinout_data]
            has_duplicates = len(pin_names) != len(set(pin_names)) if pin_names else False

            has_meaningful_names = any(
                self._is_meaningful_pin_name(p.get("name", ""))
                for p in pinout_data
            ) if pinout_data else False

            has_garbage = any(
                self._is_garbage_pin_name(p.get("name", ""))
                for p in pinout_data
            ) if pinout_data else False

            pin_numbers = sorted([p["number"] for p in pinout_data]) if pinout_data else []
            has_gap = False
            if len(pin_numbers) >= 3:
                gap_max_pins = self._get_max_pins_for_package(package)
                actual_max_pin = max(pin_numbers)
                if gap_max_pins <= 64 or (gap_max_pins > 100 and actual_max_pin <= 80):
                    effective_max = min(gap_max_pins, actual_max_pin) if gap_max_pins <= 64 else actual_max_pin
                    expected_max = min(actual_max_pin, effective_max)
                    actual_set = set(n for n in pin_numbers if 1 <= n <= expected_max)
                    expected_set = set(range(1, expected_max + 1))
                    missing = expected_set - actual_set
                    if missing and len(missing) <= len(actual_set):
                        has_gap = True

            needs_fallback = (
                not pinout_data or
                total_pins < 4 or
                io_ratio >= 0.5 or
                (total_pins > 0 and not has_meaningful_names) or
                has_duplicates or
                has_garbage or
                has_gap
            )

            if needs_fallback:
                standard_pinout = CommonPinoutLibrary.get_pinout(chip_name)
                if not standard_pinout:
                    # 降级失败 = 没有任何可信来源。保留正则抽出的乱码引脚
                    # (Pin8=PG-DSO、Pin21=IEC61249-2) 比不给答案更危险。
                    logger.warning(
                        f"引脚降级失败且标准库无 {chip_name}，清空正则抽取结果"
                    )
                    pinout_data = []
                    pin_source = ""
                if standard_pinout:
                    logger.info(f"使用标准引脚库数据: {chip_name} (io_ratio={io_ratio:.2f}, meaningful={has_meaningful_names}, dup={has_duplicates}, garbage={has_garbage}, gap={has_gap})")
                    pinout_data = standard_pinout.get("pinout", [])
                    pin_source = "standard_library"
                    standard_package = standard_pinout.get("package")
                    if standard_package:
                        package = standard_package
                        logger.info(f"使用标准库封装类型: {chip_name} -> {standard_package}")
                    if not citations:
                        citations = [{
                            "file": f"标准引脚库 - {chip_name}",
                            "page": 1,
                            "text_snippet": f"{chip_name} 标准引脚定义 ({standard_pinout.get('pin_count', len(pinout_data))}引脚, {standard_pinout.get('package', '未知封装')})",
                            "confidence": 1.0,
                            "source": "standard_library",
                        }]
            elif pinout_data:
                standard_pinout = CommonPinoutLibrary.get_pinout(chip_name)
                if standard_pinout:
                    standard_pin_count = standard_pinout.get("pin_count", 0)
                    if standard_pin_count > 0:
                        coverage_ratio = total_pins / standard_pin_count
                        if total_pins < standard_pin_count * 0.8:
                            logger.warning(f"引脚覆盖不足: {chip_name} 提取={total_pins}/{standard_pin_count}({coverage_ratio:.0%}), 使用标准库数据")
                            pinout_data = standard_pinout.get("pinout", [])
                            pin_source = "standard_library"
                            package = standard_pinout.get("package", package)
                            if not citations:
                                citations = [{
                                    "file": f"标准引脚库 - {chip_name}",
                                    "page": 1,
                                    "text_snippet": f"{chip_name} 标准引脚定义 ({standard_pin_count}引脚, {standard_pinout.get('package', '未知封装')})",
                                    "confidence": 1.0,
                                    "source": "standard_library",
                                }]
                        elif total_pins > standard_pin_count * 1.2:
                            logger.warning(f"引脚数异常偏多: {chip_name} 提取={total_pins}>>标准库{standard_pin_count}({coverage_ratio:.0%}), 使用标准库数据")
                            pinout_data = standard_pinout.get("pinout", [])
                            pin_source = "standard_library"
                            package = standard_pinout.get("package", package)
                            if not citations:
                                citations = [{
                                    "file": f"标准引脚库 - {chip_name}",
                                    "page": 1,
                                    "text_snippet": f"{chip_name} 标准引脚定义 ({standard_pin_count}引脚, {standard_pinout.get('package', '未知封装')})",
                                    "confidence": 1.0,
                                    "source": "standard_library",
                                }]
                        elif total_pins >= standard_pin_count:
                            logger.info(f"正则提取充足: {chip_name} 提取={total_pins}>=标准库{standard_pin_count}, 保留正则结果")
                        else:
                            logger.info(f"引脚覆盖率可接受: {chip_name} 提取={total_pins}/{standard_pin_count}({coverage_ratio:.0%}), 保留正则结果")

            if pinout_data and citations:
                pin_name_set = {p.get("name", "").upper() for p in pinout_data if p.get("name") and len(p.get("name", "")) >= 2}
                filtered = []
                for c in citations:
                    snippet = c.get("text_snippet", "").upper()
                    has_pin_name = any(name in snippet for name in pin_name_set)
                    if has_pin_name:
                        filtered.append(c)
                    else:
                        logger.debug(f"引脚分析过滤无关citation: page={c.get('page')}, snippet={c.get('text_snippet', '')[:80]}")
                if filtered:
                    citations = filtered
                seen_pages = set()
                deduped = []
                for c in citations:
                    key = f"{c.get('file', '')}_{c.get('page', 0)}"
                    if key not in seen_pages:
                        seen_pages.add(key)
                        deduped.append(c)
                citations = deduped

            # 最终安全闸：正则来源的引脚只有在"文档确实包含该型号"时才允许输出
            if pinout_data and pin_source == "document" and chip_missing_in_doc:
                pinout_data = []
                citations = []

            if not pinout_data:
                where = "（该文档中未出现此型号）" if chip_missing_in_doc else ""
                response = (
                    f"文档中未找到 {chip_name} 的可核验引脚信息{where}。"
                    f"若该器件引脚仅标注在封装示意图中，请查看对应页面的封装图；"
                    f"系统不会凭空生成引脚映射。"
                )
                return {
                    **state,
                    "extracted_data": {
                        "pinout": [],
                        "package": package,
                        "pinout_status": "no_structured_data",
                    },
                    "citations": [],
                    "final_response": response,
                }

            src_label = "标准引脚库" if pin_source == "standard_library" else "文档"
            lines = [f"{chip_name} 引脚定义（封装：{package or '未标注'}；来源：{src_label}）："]
            for pin in sorted(pinout_data, key=lambda x: int(x.get("number", 0) or 0)):
                name = pin.get("name", "") or "未命名"
                ptype = pin.get("pin_type", "") or ""
                lines.append(f"- Pin{pin.get('number')}: {name}" + (f"（{ptype}）" if ptype else ""))
            if citations:
                pages = sorted({c.get("page") for c in citations if c.get("page")})
                if pages:
                    lines.append(f"参考页码：第 {', '.join(str(p) for p in pages)} 页")

            return {
                **state,
                "extracted_data": {
                    "pinout": pinout_data,
                    "package": package,
                    "pinout_status": "ok",
                    "pinout_source": pin_source,
                },
                "citations": citations,
                "final_response": "\n".join(lines),
            }

        except Exception as e:
            logger.error(f"引脚分析节点失败: {e}")
            return {**state, "error": str(e), "final_response": f"引脚分析失败: {str(e)}"}

    def _extract_chip_name(self, query: str) -> Optional[str]:
        """从用户查询中提取芯片型号，返回大写型号或None。"""
        match = self.CHIP_NAME_PATTERN.search(query)
        if match:
            return match.group(1).upper()
        return None

    def _parse_pinout_results(self, results: List[Any]) -> List[Dict]:
        """从检索结果中解析引脚定义。

        策略: 遍历前5个结果→6种PIN_PATTERNS逐一匹配→去重→范围约束→按编号排序。
        """
        pinout_data = []
        seen_pin_numbers = set()

        package = self._extract_package_from_results(results)
        max_pins = self._get_max_pins_for_package(package)

        for result in results[:5]:
            text = result.text if hasattr(result, 'text') else result.get("content", "")
            text = text.translate(self.UNICODE_TRANS)

            for pattern in self.PIN_PATTERNS:
                matches = pattern.findall(text)
                for match in matches:
                    pin_num_str = match[0]
                    pin_name = match[1]
                    try:
                        if pin_name.strip() and pin_name.strip()[0].isdigit():
                            pin_num = int(pin_name.strip())
                            pin_name = pin_num_str.strip()
                        else:
                            pin_num = int(pin_num_str)
                    except ValueError:
                        continue

                    if pin_num < 1 or pin_num > max_pins:
                        continue

                    pin_name_clean = pin_name.strip()
                    if self._is_garbage_pin_name(pin_name_clean):
                        logger.debug(f"过滤垃圾引脚名: pin={pin_num}, name='{pin_name_clean}'")
                        continue

                    if pin_num not in seen_pin_numbers and pin_name_clean:
                        seen_pin_numbers.add(pin_num)
                        pinout_data.append({
                            "number": pin_num,
                            "name": pin_name_clean,
                            "pin_type": self._infer_pin_type(pin_name_clean),
                        })

            if len(pinout_data) >= max_pins:
                break

        pinout_data.sort(key=lambda x: x["number"])

        return pinout_data

    def _get_max_pins_for_package(self, package: Optional[str]) -> int:
        """根据封装类型返回引脚数量上限。"""
        if not package:
            return 128
        package_upper = package.upper().replace("-", "").replace(" ", "")

        _EXACT_MAP = {
            'DIP4': 4, 'DIP6': 6, 'DIP8': 8, 'DIP14': 14, 'DIP16': 16,
            'DIP18': 18, 'DIP20': 20, 'DIP24': 24, 'DIP28': 28, 'DIP40': 40,
            'SOIC4': 4, 'SOIC6': 6, 'SOIC8': 8, 'SOIC14': 14, 'SOIC16': 16,
            'SOIC18': 18, 'SOIC20': 20, 'SOIC24': 24, 'SOIC28': 28,
            'SOP4': 4, 'SOP6': 6, 'SOP8': 8, 'SOP14': 14, 'SOP16': 16,
            'SOP20': 20, 'SOP24': 24, 'SOP28': 28,
            'TSSOP8': 8, 'TSSOP14': 14, 'TSSOP16': 16, 'TSSOP20': 20,
            'TSSOP24': 24, 'TSSOP28': 28, 'TSSOP38': 38, 'TSSOP48': 48,
            'MSOP8': 8, 'MSOP10': 10, 'MSOP12': 12,
            'SSOP16': 16, 'SSOP20': 20, 'SSOP24': 24, 'SSOP28': 28,
            'SOT233': 3, 'SOT235': 5, 'SOT236': 6, 'SOT363': 6, 'SOT893': 3,
            'TO2203': 3, 'TO2205': 5, 'TO2523': 3, 'TO2633': 3,
            'QFN16': 16, 'QFN20': 20, 'QFN24': 24, 'QFN28': 28,
            'QFN32': 32, 'QFN40': 40, 'QFN48': 48, 'QFN56': 57, 'QFN57': 57, 'QFN64': 64,
            'DFN8': 8, 'DFN10': 10, 'DFN12': 12, 'DFN16': 16, 'DFN20': 20, 'DFN24': 24,
            'LQFP32': 32, 'LQFP48': 48, 'LQFP64': 64, 'LQFP80': 80,
            'LQFP100': 100, 'LQFP144': 144,
            'QFP32': 32, 'QFP44': 44, 'QFP48': 48, 'QFP64': 64,
            'QFP80': 80, 'QFP100': 100, 'QFP144': 144,
            'TQFP32': 32, 'TQFP44': 44, 'TQFP48': 48, 'TQFP64': 64, 'TQFP100': 100,
            'BGA48': 48, 'BGA64': 64, 'BGA100': 100, 'BGA144': 144,
            'BGA196': 196, 'BGA256': 256, 'BGA324': 324, 'BGA484': 484,
            'WLCSP16': 16, 'WLCSP25': 25, 'WLCSP36': 36, 'WLCSP49': 49,
        }
        if package_upper in _EXACT_MAP:
            return _EXACT_MAP[package_upper]

        _PATTERN_MAP = [
            (r'(?:DIP|SOIC|SOP|SSOP)(\d+)', None),
            (r'(?:TSSOP|MSOP)(\d+)', None),
            (r'(?:L?QFP|TQFP)(\d+)', None),
            (r'(?:QFN|DFN|WLCSP)(\d+)', None),
            (r'BGA(\d+)', None),
            (r'SOT(\d+)', lambda m: max(int(m.group(1)) // 10, 3)),
            (r'TO(\d+)', lambda m: max(int(m.group(1)) // 100, 3)),
        ]
        for pattern, transform in _PATTERN_MAP:
            m = re.search(pattern, package_upper)
            if m:
                return transform(m) if transform else int(m.group(1))

        return 48

    def _infer_pin_type(self, pin_name: str) -> str:
        """根据引脚名称推断引脚类型。"""
        pin_name_lower = pin_name.lower().strip()
        pin_name_upper = pin_name.upper().strip()

        if any(kw in pin_name_lower for kw in [
            'vcc', 'vdd', 'power', 'supply', 'vee', 'v+', 'v-',
            'vbat', 'vref', 'vdda', 'vssa', 'avcc', 'avdd', 'dvcc', 'dvdd',
            'vddio', 'vout', '3v3', '5v', '1v8', 'vin',
        ]):
            return "power"
        if any(kw in pin_name_lower for kw in [
            'gnd', 'vss', 'ground', 'pgnd', 'agnd', 'dgnd', 'sgnd',
        ]):
            return "ground"
        if any(kw in pin_name_lower for kw in [
            'nrst', 'reset', 'boot', 'swdio', 'swclk', 'jtdi', 'jtck',
            'jtms', 'jtdo', 'jtrst', 'trst', 'tck', 'tms', 'tdi', 'tdo',
            'test', 'mode', 'por', 'wdt',
        ]):
            return "special"
        if any(kw in pin_name_lower for kw in [
            'xtal', 'osc', 'clk', 'clkout', 'mco', 'hse', 'hsi', 'lse', 'lsi',
        ]):
            return "special"
        if any(kw in pin_name_lower for kw in ['output', 'out', '_o']) and 'out' in pin_name_lower:
            if not any(kw in pin_name_lower for kw in ['out1', 'out2', 'out3', 'out4', 'out5', 'out6']):
                pass
            return "output"
        if re.match(r'^\d*(?:OUT|O)\d*$', pin_name_upper):
            return "output"
        if any(kw in pin_name_lower for kw in ['input', 'in+', 'in-', '_i']):
            return "input"
        if re.match(r'^\d*(?:IN|I)[N\d]*[+-]?$', pin_name_upper):
            return "input"
        if '+' in pin_name or '-' in pin_name:
            return "input"
        if any(kw in pin_name_lower for kw in [
            'nc', 'n.c.', 'no connect', 'n/a', 'dnc', 'reserved',
        ]):
            return "nc"
        if any(kw in pin_name_lower for kw in [
            'adc', 'dac', 'analog', 'aout', 'ain',
        ]):
            return "analog"
        if re.match(r'^P[A-Z]\d+', pin_name, re.IGNORECASE):
            return "bidirectional"
        if re.match(r'^[A-E]\d+', pin_name_upper):
            return "bidirectional"
        if re.match(r'^GP\d+', pin_name_upper):
            return "bidirectional"
        if re.match(r'^IO\d+', pin_name_upper):
            return "bidirectional"
        if re.match(r'^RB\d+', pin_name_upper):
            return "bidirectional"
        if re.match(r'^RC\d+', pin_name_upper):
            return "bidirectional"
        if re.match(r'^RD\d+', pin_name_upper):
            return "bidirectional"
        if re.match(r'^RA\d+', pin_name_upper):
            return "bidirectional"
        if re.match(r'^GPIO\d*', pin_name_upper):
            return "bidirectional"
        return "io"

    def _is_meaningful_pin_name(self, pin_name: str) -> bool:
        """判断引脚名称是否有语义(VCC/GND/IN/OUT等)。"""
        pin_upper = pin_name.upper().strip()
        pin_lower = pin_name.lower().strip()
        meaningful_prefixes = [
            'VCC', 'VEE', 'VDD', 'VSS', 'GND', 'V+', 'V-',
            'VBAT', 'VREF', 'VDDA', 'VSSA', 'AVCC', 'AVDD',
        ]
        for prefix in meaningful_prefixes:
            if pin_upper.startswith(prefix):
                return True
        if any(kw in pin_lower for kw in [
            'input', 'output', 'in+', 'in-', 'out+', 'out-',
            'out1', 'out2', 'in1', 'in2', 'reset', 'nrst',
            'boot', 'xtal', 'osc', 'clk', 'adc', 'dac',
            'swdio', 'swclk', 'mco',
        ]):
            return True
        if 'IN' in pin_upper or 'OUT' in pin_upper:
            return True
        if re.match(r'^P[A-Z]\d+', pin_name, re.IGNORECASE):
            return True
        if re.match(r'^[A-E]\d+', pin_upper):
            return True
        if re.match(r'^GP\d+', pin_upper):
            return True
        return False

    def _is_garbage_pin_name(self, pin_name: str) -> bool:
        """判断引脚名称是否为垃圾数据(日期/版本号/页码等)。"""
        name = pin_name.strip()
        if not name:
            return True
        name_upper = name.upper()
        name_lower = name.lower()
        month_names = {'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
                       'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'}
        if any(name_upper.startswith(m) or name_lower.startswith(m.lower()) for m in month_names):
            if re.search(r'\d{4}', name) or re.search(r'-\d+', name):
                return True
        garbage_patterns = [
            r'^(?:REV|REVISION|VERSION|VER|V\d*\.?\d*)$',
            r'^PAGE\s*\d+$',
            r'^\d{4}$',
            r'^[A-Z]{3}-\d{4}$',
            r'^DOC(?:UMENT)?\s*\d*$',
            r'^SHEET\s*\d+/\d+$',
            r'^(?:WWW\.|HTTP|HTTPS|@)',
            r'^\d+\s*[\/\-\.]\s*\d+\s*[\/\-\.]\s*\d+$',
            r'^FIGURE\s*\d+|^TABLE\s*\d+',
            r'^(?:CONFIDENTIAL|PROPRIETARY|DRAFT|PRELIMINARY)$',
        ]
        for pat in garbage_patterns:
            if re.match(pat, name_upper, re.IGNORECASE):
                return True
        return False

    def _extract_package_from_results(self, results: List[Any]) -> Optional[str]:
        """从检索结果中提取封装类型。

        三级尝试: 文本匹配→元数据匹配→引脚数量推断。
        """
        for result in results[:5]:
            text = result.text if hasattr(result, 'text') else result.get("content", "")
            metadata = result.metadata if hasattr(result, 'metadata') else {}

            for pkg_name, pattern in self.COMMON_PACKAGES.items():
                if pattern.search(text):
                    logger.info(f"从检索结果中提取到封装: {pkg_name}")
                    return pkg_name

            for pkg_name, pattern in self.COMMON_PACKAGES.items():
                metadata_str = str(metadata)
                if pattern.search(metadata_str):
                    logger.info(f"从元数据中提取到封装: {pkg_name}")
                    return pkg_name

        if results:
            first_text = results[0].text if hasattr(results[0], 'text') else results[0].get("content", "")
            gpio_count = len(re.findall(r'GPIO\d+', first_text, re.IGNORECASE))
            if gpio_count >= 20:
                return "LQFP48"
            elif gpio_count >= 10:
                return "QFP"
            pin_count = len(re.findall(r'Pin\s*\d+', first_text, re.IGNORECASE))
            if pin_count == 8:
                return "DIP-8 / SOIC-8"
            elif pin_count == 14:
                return "DIP-14 / SOIC-14"
            elif pin_count == 16:
                return "DIP-16 / SOIC-16"

        return None


def create_unified_agent_nodes(settings=None, retriever=None) -> UnifiedAgentNodes:
    """创建统一Agent节点集合实例的工厂函数。"""
    return UnifiedAgentNodes(settings, retriever=retriever)
