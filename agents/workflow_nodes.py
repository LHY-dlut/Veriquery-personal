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

import logging
import re
from typing import Dict, List, Any, Optional, Tuple

from core.config import get_settings
from retrieval.hybrid_retriever import HybridRetriever
from knowledge.pinout_library import CommonPinoutLibrary

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
    """RAG流程核心节点集，封装"检索→清洗→生成"完整链路。"""

    CHIP_NAME_PATTERN = re.compile(
        r'(?:^|(?<=[\s,，。.；;：:（(]))('
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
        r')(?=[\s,，。.；;：:）)\u4e00-\u9fff]|$)',
        re.IGNORECASE
    )

    TECH_KEYWORDS = ['VCC', 'VDD', 'VS', '±', 'Supply voltage', 'Electrical Characteristics', 'Recommended Operating', 'Absolute Maximum']

    PARAM_PATTERNS = [
        r'VCC±?\s*[=：]\s*[±]?\d+\.?\d*\s*[Vv]?.{0,100}',
        r'VS±?\s*[=：]\s*[±]?\d+\.?\d*\s*[Vv]?.{0,100}',
        r'Supply\s+Voltage[:\s]*[±]?\d+\.?\d*\s*[Vv]?.{0,100}',
        r'Operating\s+Voltage[:\s]*[±]?\d+\.?\d*\s*[Vv]?.{0,100}'
    ]

    LATEX_INDICATORS = [
        r'\\begin{tabular}', r'\\end{tabular}',
        r'\\begin{table}', r'\\end{table}',
        r'\\hline', r'\\vline',
        r'\\cmidrule', r'\\toprule', r'\\bottomrule',
        r'\\multicolumn', r'\\multirow',
        r'\\text{', r'\\tag{', r'\\hspace',
        r'\\cmbox', r'\\vspace', r'\\hskip',
        r'\\tabular', r'\\begin', r'\\end',
        r'\&nbsp;', r'\&gt;', r'\&lt;',
        r'\\#', r'\\%', r'\\$',
        r'\\xspace', r'\\cmboxrule',
        r'rowspan={', r'num_rows', r'normalized_value',
        r'\tabs{}', r'\&',
    ]

    TECH_PATTERNS = [
        r'VCC[±+-]?\s*[=：]\s*[±]?\d+\.?\d*\s*[Vv]?',
        r'VDD[±+-]?\s*[=：]\s*[±]?\d+\.?\d*\s*[Vv]?',
        r'VS[±+-]?\s*[=：]\s*[±]?\d+\.?\d*\s*[Vv]?',
        r'Supply\s+voltage[:\s]*[±]?\d+\.?\d*\s*[Vv]?',
        r'Operating\s+voltage[:\s]*[±]?\d+\.?\d*\s*[Vv]?',
        r'Electrical\s+Characteristics',
        r'Recommended\s+Operating\s+Conditions',
        r'Absolute\s+Maximum\s+Ratings',
        r'\bMIN\b.*\bMAX\b.*\bUNIT\b',
        r'\bPARAMETER\b.*\bTEST\s+CONDITIONS\b',
        r'\b\d+\.?\d*\s*V\b.*\b\d+\.?\d*\s*V\b'
    ]

    PACKAGE_SECTION_INDICATORS = [
        'seating plane', 'pin 1 id area',
        'package dimensions', 'mechanical data', 'package information',
        'outline', 'drawing', 'dimensions (mm)', 'inches', 'millimeters',
        'package drawing', 'mechanical drawing'
    ]

    PACKAGE_EXIT_INDICATORS = [
        'electrical characteristics', 'recommended operating conditions', 'absolute maximum ratings'
    ]

    AXIS_LABELS = ['VDIFF (V)', 'VIN (V)', 'VOUT+ (V)', 'VOUTt (V)', 'VOUT- (V)', 'Figure 7-2', 'Figure 7-', 'Figure 8-']

    CHART_TITLES = ['Differential Output Voltage vs Input Voltage', 'Positive Output Voltage Node vs Input Voltage', 'Output Voltage']

    def __init__(self, settings=None):
        """初始化文本分析节点，重型组件延迟加载。"""
        self.settings = settings or get_settings()
        self.retriever = None
        self._table_store = None
        self._llm_client = None

        self._compiled_param_patterns = [re.compile(p, re.IGNORECASE) for p in self.PARAM_PATTERNS]
        self._number_unit_pattern = re.compile(r'±?\d+\.?\d*\s*[VvAaµuMmKkWw]?')

        self._compiled_tech_patterns = [re.compile(p, re.IGNORECASE) for p in self.TECH_PATTERNS]
        self._voltage_pattern = re.compile(r'[±]?\d+\.?\d*\s*V\b')
        self._current_pattern = re.compile(r'[±]?\d+\.?\d*\s*[µunm]?A\b')
        self._resistance_pattern = re.compile(r'\d+\.?\d*\s*[kM]?Ω?\b')
        self._frequency_pattern = re.compile(r'\d+\.?\d*\s*[kMGT]?Hz\b')
        self._temp_pattern = re.compile(r'[±]?\d+\.?\d*\s*°?C\b')
        # 布局码(如 "A001")必须整体匹配;旧正则 [A-Z]\d{3}\s* 会命中型号内部,
        # 把 BSO301SP→BSSP、NE5532→N2,导致检索侧二次清洗时型号再次被毁
        self._layout_code_pattern = re.compile(r'(?<![A-Za-z0-9])[A-Z]\d{3}(?![A-Za-z0-9])')

        self._init_retriever()
        logger.info("TextAnalysisNodes initialized")

    def _init_retriever(self):
        """初始化混合检索器，失败时优雅降级。"""
        try:
            self.retriever = HybridRetriever(settings=self.settings)
        except Exception as e:
            logger.warning(f"Failed to initialize retriever: {e}")

    @property
    def table_store(self):
        """延迟初始化TableStore，首次访问时创建。"""
        if self._table_store is None:
            try:
                from retrieval.table_store import TableStore
                self._table_store = TableStore()
            except Exception as e:
                logger.warning(f"Failed to initialize table_store: {e}")
        return self._table_store

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

    def _extract_chip_name(self, query: str) -> Optional[str]:
        """从用户查询中提取芯片型号，返回大写型号或None。"""
        match = self.CHIP_NAME_PATTERN.search(query)
        if match:
            return match.group(1).upper()
        return None

    def _extract_keywords(self, query: str) -> List[str]:
        """从查询中提取关键词，支持中英文混合。"""
        keywords = []
        chip_name = self._extract_chip_name(query)
        if chip_name:
            keywords.append(chip_name.lower())

        param_keywords = [
            'voltage', '电压', 'vcc', 'vdd', 'vs', 'supply',
            'current', '电流', 'power', '功耗',
            'temperature', '温度', 'operating', '工作',
            'frequency', '频率', 'bandwidth', '带宽',
            'supply voltage', '供电电压', '工作电压',
            'operating voltage', '电源电压', '电压范围'
        ]
        query_lower = query.lower()
        for kw in param_keywords:
            if kw in query_lower:
                keywords.append(kw)

        return list(set(keywords))

    _TRANSCRIPTION_PATTERN = re.compile(
        r'原文|逐行|逐条|转录|抄写|照抄|逐字|原文照录|完整列出|全部列出|完整转录'
    )

    _PARAM_QUERY_MARKERS = (
        '最大值', '最小值', '上限', '下限', '范围', '典型值', '额定值', '是多少',
        '多大', '多少', '参数值', '规格',
    )

    def _is_param_query(self, query: str) -> bool:
        """判断是否为"查某个参数数值"类问题(需要精确选值,不能用泛泛描述)。

        中英文都要识别:golden 评测的英文题(gain bandwidth 等)曾因
        只认中文标记而绕过 Resolver,走回 LLM 自由生成。
        """
        q = query or ''
        if any(zh in q for zh in self._PARAM_QUERY_MARKERS):
            return True
        if any(zh in q for zh in self._PARAM_KEYWORD_MAP):
            return True
        try:
            from core.param_resolver import looks_like_param_query
            return looks_like_param_query(q)
        except Exception:
            return False

    _COVER_QUERY_MARKERS = ("首页", "封面", "标题", "文档名", "文档编号", "文件编号",
                           "首页标题", "document title", "cover page")

    def _is_cover_query(self, query: str) -> bool:
        """判断是否为"首页/封面标题"类元信息问题。

        这类问题的答案在文档第 1 页正文里，而元信息查询没有参数词桥接，
        混合检索召回的往往是表格行或曲线页，导致模型答"未找到"。
        """
        q = (query or "").lower()
        return any(m in q for m in self._COVER_QUERY_MARKERS)

    def _find_first_page_text(self, results: List[Any]) -> str:
        """取第 1 页页首正文（首页标题所在处）。

        两点讲究：
          1) 优先非表格行 chunk —— MinerU 对多栏 PDF 的表格块存在字符交错
             （如 "J-TuNecrtmioanl - re ssoilsdtearnincge"），取到它会把
             乱码当成标题答出去；
          2) 只取开头 —— 标题位于页首，取前几百字符即可，避免正文噪音。
        """
        best = ""
        best_start = None
        for r in results[:30]:
            page = getattr(r, "page", None)
            if page is None and isinstance(r, dict):
                page = r.get("page")
            if str(page) != "1":
                continue
            meta = getattr(r, "metadata", None) or {}
            # 表格行不含标题，且多栏表格有字符交错，直接排除
            if meta.get("source") in ("table_text", "param_record"):
                continue
            text = getattr(r, "text", None) if hasattr(r, "text") else None
            if text is None and isinstance(r, dict):
                text = r.get("content", "")
            if not text:
                continue
            # 标题在页首：取字符起始位置最靠前的块，而非最长的块
            char_start = meta.get("char_start")
            if best_start is None or (char_start is not None and char_start < best_start):
                best = text
                best_start = char_start if char_start is not None else -1
        return best[:400]

    async def _top_param_record_nominee(self, query: str, results: List[Any],
                                        document_ids: List[str], min_score: float):
        """语义提名：纯向量检索找与查询最相关且达标的 param_record。

        分数信号选型（实测）：bge-reranker-v2-m3 对文档格式极度敏感
        （竖线表格行 0.26 vs 自然语言串 0.98）、器件名+疑问句式系统性
        压分——rerank 分数不可比、不稳定，不能作为提名置信信号。
        改用 bge-m3 纯向量相似度（0-1 稳定可比）：Settling 行 0.558
        正确高于封装垃圾行 0.515。

        主检索的每文档限额会把 param_record 行截在结果外
        （_is_param_query=False 时不放宽），因此提名独立走向量检索。
        """
        from core.param_resolver import ParamEvidence
        from ingestion.table_struct import ParamRecord
        from retrieval.embeddings import EmbeddingManager

        vs = getattr(getattr(self, "retriever", None), "vector_store", None)
        if vs is None or not document_ids:
            return None, 0.0

        em = EmbeddingManager(self.settings)
        qv = em.embed_query(query)
        filt = {"source": "param_record"}
        filt["document_id"] = (document_ids[0] if len(document_ids) == 1
                               else {"$in": [str(d) for d in document_ids]})
        res = await vs.search(qv, top_k=10, filter_dict=filt)
        for cand in res:
            meta = getattr(cand, "metadata", None) or {}
            try:
                rec = ParamRecord.from_metadata(meta)
            except Exception:
                continue
            if not rec.valid:
                continue
            score = float(getattr(cand, "score", 0) or 0)
            if score >= min_score:
                return ParamEvidence.from_record(rec, score=score), score
            break  # 向量降序，最高分不达标即止
        return None, 0.0

    async def _try_param_resolution(self, query: str, results: List[Any],
                              document_ids: Optional[List[str]] = None
                              ) -> Tuple[Optional[str], List[Dict]]:
        """参数类问题走确定性选值，**完整保留选值状态**。

        返回 (rendered_answer, citations)：citations 由选值证据
        （evidence/alternatives）构造，使"引用来源"与答案实际依据
        一致（此前 citations 一直是检索 top2，与 Resolver 答案脱节）。
        返回 None 仅表示"不是参数问题 / 文档没有结构化参数记录"，
        此时才允许回退普通 RAG-LLM。

        返回 None 仅表示"不是参数问题 / 文档没有结构化参数记录"，
        此时才允许回退普通 RAG-LLM。

        重要：not_found / missing_value / condition_conflict / ambiguous
        都是**结论**，必须原样呈现；把它们折叠成 None 再交给 LLM，
        等于让 LLM 在没有校验的情况下重新猜一个数值（本轮要消除的行为）。
        """
        # ── 语义兜底提名候选：先于门控计算。别名 miss 的查询门控为
        # False，但检索分数命中的参数记录（rerank 分数是厂商无关的语义
        # 信号）可作为身份提名放行——语义兜底路径的入口。
        fb_on = bool(getattr(self.settings, "PARAM_SEMANTIC_FALLBACK_ENABLED", True))
        fb_min_score = float(getattr(self.settings, "PARAM_SEMANTIC_FALLBACK_MIN_SCORE", 0.5))
        fb_nominee_ev, fb_nominee_score = None, 0.0
        if fb_on:
            fb_nominee_ev, fb_nominee_score = await self._top_param_record_nominee(
                query, results, document_ids, fb_min_score
            )

        if not self._is_param_query(query):
            if not fb_on or fb_nominee_ev is None:
                return None, []
            logger.info(
                f"门控放行（语义提名 {fb_nominee_ev.param or fb_nominee_ev.symbol} "
                f"score={fb_nominee_score:.2f}）: {query[:50]}"
            )

        try:
            from core.param_resolver import (
                ParamEvidence, parse_param_query, select_value, select_values_multi,
                query_has_param_signal,
            )
            from ingestion.table_struct import ParamRecord
        except Exception as e:
            logger.debug(f"param_resolver 不可用: {e}")
            return None, []

        # 证据必须来自**确定性全集**：同一参数的多个工况行靠语义检索 top-N
        # 可能只召回一条，会让条件匹配误判冲突/选错工况。
        raw_metas: List[Any] = []
        store = getattr(getattr(self, "retriever", None), "vector_store", None)
        if store is not None and document_ids:
            try:
                where = {"$and": [
                    {"document_id": {"$in": list(document_ids)}},
                    {"source": "param_record"},
                ]}
                for row in store.fetch_by_metadata(where, limit=2000):
                    raw_metas.append(row.get("metadata") or {})
                logger.info(f"参数证据全集: {len(raw_metas)} 条（按文档直取）")
            except Exception as e:
                logger.warning(f"按文档直取参数记录失败，回退检索结果: {e}")
        if not raw_metas:
            for r in results[:40]:
                meta = getattr(r, "metadata", None) or {}
                if meta.get("source") == "param_record":
                    raw_metas.append(meta)

        evidences: List[ParamEvidence] = []
        total = invalid = 0
        for meta in raw_metas:
            total += 1
            try:
                rec = ParamRecord.from_metadata(meta)
            except Exception as e:
                logger.debug(f"参数记录反序列化失败: {e}")
                invalid += 1
                continue
            if not rec.valid:
                invalid += 1
                continue
            evidences.append(ParamEvidence.from_record(rec))

        if total and not evidences:
            return ("该文档的参数记录为旧版结构（缺少原始条件/原始行），"
                    "无法核验条件，请重建索引后再查询。"), []

        if not evidences:
            return None, []

        pq = parse_param_query(query)

        # 守卫：解析不出参数词、查询也没有任何参数词信号（如"封装是什么/
        # 版本号/价格"）→ 不是参数语义查询，回退 LLM 从 context 作答。
        # 否则 Resolver 会在无信号状态下靠符号撞表乱配出文不对题的结论。
        # 例外：存在高分参数记录提名时放行（语义兜底路径）。
        if not pq.param_keys and not query_has_param_signal(query):
            if not fb_on or fb_nominee_ev is None:
                logger.info(f"无参数信号查询，回退 LLM: {query[:50]}")
                return None, []
            logger.info(
                f"无受控别名但有高分参数记录提名 "
                f"({fb_nominee_ev.param or fb_nominee_ev.symbol}, "
                f"score={fb_nominee_score:.2f})，走语义兜底"
            )
        # 多参数题（"上升时间和下降时间分别是多少"）：逐参数独立选值拼接，
        # 否则身份选择只出一个组、其余参数被静默丢弃（实测 Fall time 丢失）
        multi = select_values_multi(pq, evidences)
        if multi is not None:
            rendered, sels = multi
            logger.info(f"多参数题逐参数选值: {pq.matched_zh} → {len(sels)} 段")
            citations: List[Dict] = []
            for s in sels:
                citations.extend(self._param_evidence_citations(s, results))
            return rendered, citations
        sel = select_value(pq, evidences)

        # ── 语义兜底：受控别名未命中（unsupported/not_found）且有提名 ──
        if sel.status in ("unsupported_parameter", "not_found") and fb_on and fb_nominee_ev is not None:
            logger.info(
                f"语义兜底提名: {fb_nominee_ev.param or fb_nominee_ev.symbol} "
                f"(score={fb_nominee_score:.2f})"
            )
            sel2 = select_value(pq, [fb_nominee_ev], skip_identity=True)
            if sel2.status in ("ok", "ambiguous", "missing_value", "condition_conflict"):
                rendered = (
                    "根据语义匹配，您询问的可能是以下参数（未经受控别名确认，"
                    "若不是请指明参数全名）：\n" + sel2.render()
                )
                citations = self._param_evidence_citations(sel2, results)
                return rendered, citations
        logger.info(
            f"参数确定性选值: status={sel.status}, value={sel.value}, "
            f"records={len(evidences)}, invalid={invalid}"
        )
        citations = self._param_evidence_citations(sel, results)
        return sel.render(), citations

    def _param_evidence_citations(self, sel: Any, results: List[Any]) -> List[Dict]:
        """用选值证据（evidence/alternatives）构造 citations。

        此前 citations 一直取检索 top2，Resolver 答案明明是〔第X页 表Y
        第Z行〕的参数记录，引用却指向无关行——引用与答案脱节。这里让
        引用逐条对应证据行：file 借同页检索结果的 filename，snippet 用
        raw_row（完整行原文），section 标注表号行号。
        """
        evs: List[Any] = []
        if getattr(sel, "evidence", None) is not None:
            evs.append(sel.evidence)
        for alt in getattr(sel, "alternatives", None) or []:
            if alt is not sel.evidence:
                evs.append(alt)

        default_file = ""
        for r in results[:5]:
            fname = getattr(r, "filename", "") or ""
            if fname:
                default_file = fname
                break

        out: List[Dict] = []
        seen = set()
        for ev in evs[:4]:
            key = (ev.page, ev.table_id, ev.row_index)
            if key in seen:
                continue
            seen.add(key)
            fname = default_file
            for r in results[:20]:
                if getattr(r, "page", None) == ev.page and getattr(r, "filename", ""):
                    fname = r.filename
                    break
            snippet = (getattr(ev, "raw_row", "") or getattr(ev, "source_text", "") or "")[:200]
            section = f"表{ev.table_id} 第{ev.row_index}行" if ev.table_id else ""
            out.append({
                "file": fname or "未知文档",
                "page": ev.page or 1,
                "text_snippet": snippet,
                "section": section,
            })
        return out


    def _is_transcription_request(self, query: str) -> bool:
        """判断是否为"逐行/原文转录"类请求(需要放宽长度限制并保留原文)。"""
        return bool(self._TRANSCRIPTION_PATTERN.search(query or ""))

    # 常见参数中英文映射:用于在检索结果中定位与问题直接相关的行,
    # 并把该行前置到 context 开头(0.8B 对 context 开头的内容遵循最好)
    _PARAM_KEYWORD_MAP = {
        '连续漏电流': ['continuous drain current'],
        '漏电流': ['drain current'],
        'P沟道': ['p channel', 'p-channel'],
        'N沟道': ['n channel', 'n-channel'],
        '工作温度': ['operating', 'storage temperature'],
        '存储温度': ['storage temperature'],
        '温度': ['temperature'],
        '供电电压': ['supply voltage', 'gate source voltage', 'v gs'],
        '电压': ['voltage'],
        '导通电阻': ['ds(on)', 'r ds(on)', 'on-state resistance', 'on resistance'],
        '功耗': ['power dissipation'],
        '击穿电压': ['breakdown voltage'],
        '输入电容': ['input capacitance'],
        '输出电容': ['output capacitance'],
        '开启延时': ['turn-on delay'],
        '上升时间': ['rise time'],
        '热阻': ['thermal resistance'],
        '雪崩能量': ['avalanche energy'],
        '阈值电压': ['threshold voltage', 'gate threshold'],
        '封装': ['package'],
    }

    def _locate_param_snippets(self, query: str, results: List[Any]) -> List[str]:
        """在检索结果中定位与参数问题直接相关的文本片段。

        扁平化参数表里参数名与数值在同一句中(fitz 提取保序),而
        表格提取的列边界会把数值切碎。这里直接在扁平文本上定位参数
        关键词,取其前后窗口,供调用方前置到 context 开头。

        Returns:
            最多 3 个带页码标注的片段字符串。
        """
        query_lower = (query or "").lower()
        patterns = []
        for zh, ens in self._PARAM_KEYWORD_MAP.items():
            if zh in query_lower:
                patterns.extend(ens)
        if not patterns:
            # 英文查询:取查询中的长英文词直接匹配
            patterns = re.findall(r'[a-z][a-z\s-]{5,}', query_lower)[:3]
        if not patterns:
            return []

        snippets = []
        # 先按"命中参数名的长度"排序:参数表行(含具体数值)优先于
        # 首页产品摘要(只是复述特性,不含该参数的具体数值)
        scored = []
        for rank, result in enumerate(results[:30]):
            text = getattr(result, "text", None) if hasattr(result, "text") else None
            if text is None and isinstance(result, dict):
                text = result.get("content", "")
            if not text:
                continue
            text_lower = text.lower()
            best_len = 0
            for pat in patterns:
                idx = text_lower.find(pat)
                if idx >= 0:
                    best_len = max(best_len, len(pat))
            scored.append((best_len, -rank, result, text, text_lower))
        scored.sort(key=lambda x: (-x[0], -x[1]))

        for best_len, _, result, text, text_lower in scored:
            if best_len == 0:
                continue
            page = getattr(result, "page", "?")
            for pat in patterns:
                idx = text_lower.find(pat)
                if idx >= 0:
                    # 取参数所在的整行(表格行即完整参数),避免把数值截在窗口外
                    line_start = text.rfind("\n", 0, idx) + 1
                    line_end = text.find("\n", idx)
                    if line_end == -1:
                        line_end = len(text)
                    line = text[line_start:line_end].strip()
                    if len(line) < 30:
                        start = max(0, idx - 60)
                        end = min(len(text), idx + 240)
                        line = text[start:end].strip()
                    snippets.append(f"(第{page}页) {line}")
                    break
            if len(snippets) >= 3:
                break
        return snippets

    _ABSENT_TABLE_KEYWORDS = ("订购", "包装", "订货", "ordering", "packaging", "tape and reel")

    def _detect_absent_content(self, query: str, results: List[Any]) -> Optional[str]:
        """问题明确指向订购/包装表、但召回内容里根本没有 → 明确告知未找到。

        逐行转录订购表这类问题若把不相关的参数页喂给模型，模型会编造出
        看似合理的表格内容。此处做确定性拦截,避免幻觉。
        """
        query_lower = (query or "").lower()
        if not any(kw in query_lower for kw in self._ABSENT_TABLE_KEYWORDS):
            return None

        blob = []
        for result in results:
            text = getattr(result, "text", None) if hasattr(result, "text") else None
            if text is None and isinstance(result, dict):
                text = result.get("content", "")
            if text:
                blob.append(text)
        blob_lower = " ".join(blob).lower()

        if any(kw in blob_lower for kw in self._ABSENT_TABLE_KEYWORDS):
            return None
        # 措辞不能强于证据:仅凭"当前召回结果里没有"不能断言整份文档没有该内容
        return "文档中未找到相关内容：当前检索结果中未找到可核验的订购/包装信息。"

    async def text_retrieval_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """文本检索节点：执行混合检索并提取引用。

        工作流位置: intent_router → text_retrieval(本节点) → response_generation
        """
        try:
            query = state.get("query", "")
            document_ids = [str(d) for d in state.get("user_context", {}).get("selected_document_ids", [])]

            logger.info(f"执行文本检索: query='{query}', docs={len(document_ids)}")

            retrieval_results = []
            citations = []

            chip_name = self._extract_chip_name(query)
            keywords = self._extract_keywords(query)
            logger.info(f"提取芯片型号: {chip_name}, 关键词: {keywords}")

            if self.retriever and query:
                try:
                    enhanced_query = query
                    if chip_name:
                        param_keywords = [kw for kw in keywords if kw != chip_name.lower()]
                        # 中文参数词映射到规格书里的英文表格行(如 漏电流 -> drain current),
                        # 否则中文查询无法命中英文表格行,只能靠默认的供电电压兜底
                        en_terms = []
                        for zh, ens in self._PARAM_KEYWORD_MAP.items():
                            if zh in query:
                                en_terms.extend(ens[:2])
                        param_keywords = param_keywords + en_terms
                        if param_keywords:
                            enhanced_query = f"{chip_name} {' '.join(param_keywords[:4])}"
                        else:
                            # 无受控关键词时保留原句：原句语义完整（用户写的
                            # 问句优于虚构兜底词）——'Settling time of OPA211'
                            # 曾被换成 'supply voltage operating voltage'，
                            # 目标行分数跌到 0.006、语义提名无从触发
                            enhanced_query = query

                    # 取更多候选:表格行 chunk 在混合检索里常排在 top5 之外,
                    # 候选不够就找不到目标参数行,模型只能瞎猜或拒答
                    # 封面/首页标题类问题:直接按第 1 页检索,不靠排序运气
                    _is_cover = self._is_cover_query(query)
                    retrieve_filters = {"page": 1} if _is_cover else None
                    # 参数类查询放宽每文档限额:默认 5 条会提前截断同文档的参数表行；
                    # 首页类问题同样需要放宽,否则 5 条会被参数块占满,正文标题回不来
                    _is_param_q = self._is_param_query(query)
                    results = await self.retriever.retrieve(
                        enhanced_query, top_k=30, document_ids=document_ids,
                        filters=retrieve_filters,
                        max_per_doc=30 if (_is_param_q or _is_cover) else None,
                    )

                    if chip_name and results:
                        filtered_results = []
                        for r in results:
                            text_lower = r.text.lower() if hasattr(r, 'text') and r.text else ""
                            filename_lower = (r.filename or "").lower()
                            if chip_name.lower() in text_lower or chip_name.lower() in filename_lower:
                                filtered_results.append(r)

                        if filtered_results:
                            results = filtered_results
                            logger.info(f"按芯片型号 {chip_name} 过滤后剩余 {len(results)} 条结果")
                        else:
                            logger.warning(f"按芯片型号 {chip_name} 过滤后无结果，保留原始检索结果")

                    retrieval_results = results
                    citations = self._extract_citations(results)
                    logger.info(f"检索到 {len(results)} 个结果, {len(citations)} 个引用")
                except Exception as e:
                    logger.warning(f"检索失败: {e}")

            return {
                **state,
                "retrieval_results": retrieval_results,
                "citations": citations
            }
        except Exception as e:
            logger.error(f"文本检索节点失败: {e}")
            return {
                **state,
                "error": str(e),
                "retrieval_results": [],
                "citations": []
            }

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

    async def response_generation_node(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """响应生成节点：基于检索结果生成最终答案。

        工作流位置: text_retrieval → response_generation(本节点) → END
        生成策略: 无结果→提示信息; 有结果→表格优先→LLM生成→兜底错误提示
        """
        try:
            query = state.get("query", "")
            retrieval_results = state.get("retrieval_results", [])
            document_ids = [str(d) for d in state.get("user_context", {}).get("selected_document_ids", [])]

            logger.debug(f"生成响应: query='{query}', results={len(retrieval_results)}")

            citations = state.get("citations", [])
            if not retrieval_results:
                response = "抱歉，在文档中未找到相关信息。请尝试重新表述您的问题。"
            else:
                # 参数类问题：先用结构化证据做确定性选值（条件感知），
                # 选不出或非参数题再走 LLM 生成
                resolved, resolved_cites = await self._try_param_resolution(query, retrieval_results, document_ids)
                if resolved:
                    response = resolved
                    # 引用与答案对齐：Resolver 命中时用选值证据替代检索 top2，
                    # 否则 UI 的"引用来源"与答案依据〔第X页 表Y 第Z行〕脱节
                    if resolved_cites:
                        citations = resolved_cites
                else:
                    response = await self._generate_text_response_optimized(query, retrieval_results, document_ids)

            return {
                **state,
                "final_response": response,
                "citations": citations,
            }
        except Exception as e:
            logger.error(f"响应生成节点失败: {e}")
            return {
                **state,
                "error": str(e),
                "final_response": f"生成响应时发生错误: {str(e)}"
            }

    async def _generate_text_response_optimized(self, query: str, results: List[Any], document_ids: List[str] = None) -> str:
        """内容类型感知的答案生成: 表格优先→LLM文字生成→兜底错误提示。

        策略:
          1) 判断检索结果内容类型(表格/文字)
          2) 有表格 → _extract_from_tables() 精确提取
          3) 有文字 → 清洗噪音 → LLM生成答案
          4) 全部失败 → 返回错误提示
        """
        if not results:
            return "抱歉，在文档中未找到相关信息。"

        has_table_content = False
        has_text_content = False

        for result in results[:5]:
            if hasattr(result, 'source'):
                source = result.source
            else:
                source = result.get("source", "")

            if source == "table":
                has_table_content = True
            else:
                has_text_content = True

        logger.debug(f"检索结果分析: 表格内容={has_table_content}, 文字内容={has_text_content}")

        if has_table_content and self.table_store and document_ids:
            try:
                table_answer = await self._extract_from_tables(query, document_ids)
                if table_answer:
                    logger.debug("✅ 从表格中成功提取答案")
                    return table_answer
            except Exception as e:
                logger.warning(f"表格提取失败: {e}")

        if has_text_content:
            # 明确的"该内容不存在"判定:问题指向订购/包装等表格,但召回内容里
            # 根本没有这些字样 → 直接告知未找到,避免模型用别的页面凑答案
            absent_hint = self._detect_absent_content(query, results)
            if absent_hint:
                logger.info("命中缺失内容判定: %s", absent_hint)
                return absent_hint

            regex_answer = self._extract_params_by_regex(query, results[:5])
            if regex_answer:
                logger.debug("✅ 通过正则精确提取参数答案")
                return regex_answer

            transcription = self._is_transcription_request(query)
            param_query = self._is_param_query(query)

            # 参数类查询:只保留表格行 chunk(source=table_text)。
            # 混入正文/产品摘要会干扰 0.8B 的选值(同一参数在多处出现时
            # 模型会挑到宣传页里的那个数),造成同一问题答案来回变化。
            context_results = results
            if param_query and not transcription:
                table_rows = [
                    r for r in results
                    if (getattr(r, "metadata", None) or {}).get("source") == "table_text"
                ]
                if table_rows:
                    context_results = table_rows
                    logger.info(f"参数类查询: 仅保留 {len(table_rows)} 条表格行作为上下文")

            # 注:曾尝试把封面类问题的上下文限定为"第 1 页前 3 条",
            # 实测反而退化(标题块被切掉 → 模型答"未找到"),故保留混合 context,
            # 改由"页首 chunk 注入 context 头部"来引导模型。

            cleaned_contexts = []
            for i, result in enumerate(context_results[:12]):
                if hasattr(result, 'text'):
                    content = result.text
                else:
                    content = result.get("content", "")

                logger.debug(f"检索结果 {i+1}: 原始内容长度={len(content)}, 内容={content[:200] if content else 'EMPTY'}")

                if content:
                    # 转录类请求要原样输出,不再做图表噪音清洗(清洗会丢原文行)
                    cleaned = content if transcription else self._clean_chart_noise(content)
                    logger.debug(f"检索结果 {i+1}: 清理后长度={len(cleaned)}, 清理后内容={cleaned[:200] if cleaned else 'EMPTY'}")

                    if cleaned and len(cleaned) > 20:
                        cleaned_contexts.append(cleaned)
                        logger.debug(f"检索结果 {len(cleaned_contexts)}: Page {result.page}, Score={result.score:.3f}, Text={cleaned[:150]}...")
                    else:
                        logger.warning(f"检索结果 {i+1}: 清理后内容过短或为空，跳过")
                else:
                    logger.warning(f"检索结果 {i+1}: 原始内容为空，跳过")

            logger.debug(f"总共有 {len(cleaned_contexts)} 个有效上下文")

            if cleaned_contexts:
                # 转录类问题需要更宽的上下文,否则表格会被截断
                context_limit = 6 if transcription else 3
                combined_context = "\n\n".join(cleaned_contexts[:context_limit])
                logger.debug(f"合并上下文长度: {len(combined_context)} 字符 (limit={context_limit})")

                # 首页/封面标题类问题:把第 1 页正文注入 context 头部,
                # 否则检索召回的表格行/曲线页里没有标题,模型只会答"未找到"
                if self._is_cover_query(query):
                    cover_text = self._find_first_page_text(results)
                    if cover_text:
                        combined_context = (
                            "【文档第 1 页内容(标题通常在此)】\n"
                            + cover_text
                            + "\n\n【其他资料】\n" + combined_context
                        )
                        logger.debug("已注入首页内容用于封面类问题")

                # 参数类问题:把与问题直接相关的片段定位后前置到 context
                # 开头——0.8B 对 context 开头内容的遵循最好,而扁平化参数表
                # 里参数名与数值恰好保存在同一句中
                if not transcription:
                    # 必须用过滤后的 context_results:用原始 results 会把正文/
                    # 产品摘要里的同名字段又捞回来前置,抵消掉"只用表格行"的效果
                    param_snippets = self._locate_param_snippets(query, context_results)
                    if param_snippets:
                        combined_context = (
                            "【与问题直接相关的关键片段】\n"
                            + "\n".join(param_snippets)
                            + "\n\n【资料】\n" + combined_context
                        )
                        logger.debug(f"前置了 {len(param_snippets)} 个参数关键片段")

                try:
                    llm_client = self.llm_client
                    if llm_client:
                        answer = llm_client.generate_answer(
                            query=query,
                            context=combined_context,
                            max_new_tokens=1024 if transcription else 256,
                            temperature=0.3,
                            transcription=transcription,
                        )
                        logger.debug(f"✅ LLM从文字中提取答案: {answer[:100]}...")
                        # 只要求非空短下限（>=2 字符）：中文参数型答案天然短
                        # （实测"P 沟道"=4 字符被旧阈值 len>5 误杀，正确答案
                        # 变成"LLM生成答案失败"）。垃圾防护由 _clean_answer
                        # 的清理/去重/短片段过滤承担，且 llm_client 内部已有
                        # <5 触发的一次降温重试。
                        if answer and len(answer.strip()) >= 2:
                            return answer
                        else:
                            logger.warning(f"⚠️ LLM答案为空或过短: {answer}")
                except Exception as e:
                    logger.warning(f"LLM生成答案失败: {e}")
                    raise e

        return "抱歉，LLM生成答案失败，请检查系统配置。"

    async def _extract_from_tables(self, query: str, document_ids: List[str]) -> Optional[str]:
        """从结构化表格中提取答案，精确匹配无幻觉风险。

        匹配策略: 获取表格→拼接行文本→关键词匹配→查找数值+单位行→格式化返回。
        与LLM生成互补: 表格精确但覆盖面窄，LLM灵活但可能幻觉。
        """
        try:
            tables = self.table_store.get_tables_by_documents(document_ids, limit=50)
            if not tables:
                return None

            query_lower = query.lower()
            keywords = self._extract_keywords(query)
            chip_name = self._extract_chip_name(query)

            if not keywords:
                keywords = ['voltage', 'supply', 'operating', 'vcc', 'vdd']

            for table in tables[:10]:
                table_text = ""
                for row in table.data:
                    row_text = " ".join(str(cell) for cell in row if cell)
                    table_text += row_text + " "

                table_text_lower = table_text.lower()
                table_filename = getattr(table, 'filename', '') or ''

                if chip_name:
                    if chip_name.lower() not in table_text_lower and chip_name.lower() not in table_filename.lower():
                        continue

                has_keyword = any(kw in table_text_lower for kw in keywords)
                if not has_keyword:
                    continue

                for row in table.data:
                    row_str = " | ".join(str(cell) for cell in row if cell)
                    row_lower = row_str.lower()

                    if chip_name and chip_name.lower() not in row_lower:
                        if not any(kw in row_lower for kw in keywords):
                            continue

                    if re.search(r'[±]?\d+\.?\d*\s*[Vv]', row_str) or \
                       re.search(r'[±]?\d+\.?\d*\s*[AaµuMm]', row_str) or \
                       re.search(r'[±]?\d+\.?\d*\s*°?[Cc]', row_str):
                        source_info = f"（来源：{table_filename}）" if table_filename else ""
                        return f"根据文档第{table.page}页的表格数据{source_info}：{row_str}"

            return None
        except Exception as e:
            logger.warning(f"表格提取异常: {e}")
            return None

    def _extract_params_by_regex(self, query: str, results: List[Any]) -> Optional[str]:
        """基于正则从检索结果中精确提取技术参数，避免LLM幻觉。

        策略:
          1) 识别查询意图(电压/电流/温度等)
          2) 从检索结果中定位 Absolute Maximum / Recommended Operating 段落
          3) 用正则精确提取对应参数值
          4) 格式化返回，确保数值与文档一致
        """
        chip_name = self._extract_chip_name(query)
        query_lower = query.lower()

        param_intent = None
        if any(kw in query_lower for kw in ['电压', 'voltage', 'vcc', 'vdd', '供电', '电源']):
            param_intent = 'voltage'
        elif any(kw in query_lower for kw in ['电流', 'current']):
            param_intent = 'current'
        elif any(kw in query_lower for kw in ['温度', 'temperature']):
            param_intent = 'temperature'
        elif any(kw in query_lower for kw in ['功耗', 'power']):
            param_intent = 'power'

        if not param_intent:
            return None

        all_text = ""
        for r in results[:5]:
            text = r.text if hasattr(r, 'text') else r.get('content', '')
            if text:
                all_text += text + "\n"

        if not all_text:
            return None

        sections = {}
        abs_max_match = re.search(
            r'(Absolute\s+Maximum\s+Ratings.*?)(?=(?:Recommended\s+Operating|Electrical\s+Characteristics|Thermal\s+Information|$))',
            all_text, re.IGNORECASE | re.DOTALL,
        )
        if abs_max_match:
            sections['absolute_max'] = abs_max_match.group(1)

        rec_op_match = re.search(
            r'(Recommended\s+Operating\s+Conditions.*?)(?=(?:Electrical\s+Characteristics|Thermal\s+Information|5\.4|$))',
            all_text, re.IGNORECASE | re.DOTALL,
        )
        if rec_op_match:
            sections['recommended'] = rec_op_match.group(1)

        if not sections:
            return None

        parts = []

        if param_intent == 'voltage':
            if 'recommended' in sections:
                sec = sections['recommended']
                vcc_plus = re.search(r'VCC\+\s+Supply\s+voltage\s+([\d.]+)\s+([\d.]+)\s+V', sec, re.IGNORECASE)
                vcc_minus = re.search(r'VCC[–-]\s+Supply\s+voltage\s+([–\-][\d.]+)\s+([–\-][\d.]+)\s+V', sec, re.IGNORECASE)
                if vcc_plus:
                    parts.append(f"推荐工作电压: VCC+ 为 {vcc_plus.group(1)}~{vcc_plus.group(2)}V")
                if vcc_minus:
                    parts.append(f"VCC- 为 {vcc_minus.group(1)}~{vcc_minus.group(2)}V")

            if 'absolute_max' in sections:
                sec = sections['absolute_max']
                vcc_plus_max = re.search(r'VCC\+\s+0\s+([\+\-]?[\d.]+)\s+V', sec, re.IGNORECASE)
                vcc_minus_max = re.search(r'VCC[–-]\s+([–\-][\d.]+)\s+0\s+V', sec, re.IGNORECASE)
                input_v = re.search(r'Input\s+voltage.*?(?:\)\s*)?([–\-][\d.]+)\s+([\+\-]?[\d.]+)\s+V', sec, re.IGNORECASE)
                if vcc_plus_max:
                    parts.append(f"绝对最大电压: VCC+ 为 {vcc_plus_max.group(1)}V")
                if vcc_minus_max:
                    parts.append(f"VCC- 为 {vcc_minus_max.group(1)}V")
                if input_v:
                    parts.append(f"输入电压范围为 {input_v.group(1)}~{input_v.group(2)}V")

        elif param_intent == 'temperature':
            if 'absolute_max' in sections:
                sec = sections['absolute_max']
                tj = re.search(r'(?:Operating\s+virtual-junction|TJ)\s+temperature\s+([\+\-]?[\d.]+)\s*°C', sec, re.IGNORECASE)
                tstg = re.search(r'(?:Storage\s+temperature|Tstg)\s+range\s+([–\-][\d.]+)\s+([\+\-]?[\d.]+)\s*°C', sec, re.IGNORECASE)
                if tj:
                    parts.append(f"最高工作结温: {tj.group(1)}°C")
                if tstg:
                    parts.append(f"存储温度范围: {tstg.group(1)}~{tstg.group(2)}°C")
            if 'recommended' in sections:
                sec = sections['recommended']
                ta = re.search(r'(?:Operating\s+free-air\s+temperature|TA)\s+.*?\s+([–\-]?[\d.]+)\s+([\+\-]?[\d.]+)\s*°C', sec, re.IGNORECASE)
                if ta:
                    parts.append(f"推荐工作环境温度: {ta.group(1)}~{ta.group(2)}°C")

        elif param_intent == 'current':
            if 'absolute_max' in sections:
                sec = sections['absolute_max']
                ic = re.search(r'Input\s+current[^)]*?\)\s+([–\-]?[\d.]+)\s+([\+\-]?[\d.]+)\s+mA', sec, re.IGNORECASE)
                if ic:
                    parts.append(f"输入电流范围: {ic.group(1)}~{ic.group(2)}mA")

        if not parts:
            return None

        prefix = chip_name if chip_name else "该器件"
        intent_label = {'voltage': '电压', 'current': '电流', 'temperature': '温度', 'power': '功耗'}.get(param_intent, param_intent)
        return f"{prefix}的{intent_label}参数：{'; '.join(parts)}。"

    def _clean_chart_noise(self, text: str) -> str:
        """清理PDF解析噪音，保留技术参数信息。

        多级过滤策略(按优先级):
          1) LaTeX表格检测 → 跳过
          2) 技术参数保留 → 直接保留(最高优先级)
          3) 数值+单位检测 → 保留电压/电流/电阻/频率/温度行
          4) 图表区域检测 → 跳过数据行(最多20行后自动退出)
          5) 封装尺寸过滤 → 跳过机械尺寸章节
          6) 高密度数字行过滤 → 跳过图表数据点
          7) 布局代码过滤 → 移除参考编号
        """
        lines = text.split('\n')
        cleaned_lines = []

        in_chart_area = False
        chart_line_count = 0
        in_package_section = False
        in_latex_table = False
        in_table_data = False
        table_data_count = 0

        for line in lines:
            line_stripped = line.strip()

            if not line_stripped:
                if chart_line_count > 0 and chart_line_count < 10:
                    in_chart_area = False
                chart_line_count = 0
                continue

            # 第一级: LaTeX表格过滤
            if any(indicator in line_stripped for indicator in self.LATEX_INDICATORS):
                in_latex_table = True
                continue

            if in_latex_table:
                if r'\\end{tabular}' in line_stripped or r'\\end{table}' in line_stripped:
                    in_latex_table = False
                continue

            # 第二级: 技术参数保留(最高优先级)
            is_tech_line = any(pattern.search(line_stripped) for pattern in self._compiled_tech_patterns)

            if is_tech_line:
                in_chart_area = False
                chart_line_count = 0
                in_package_section = False
                in_latex_table = False
                in_table_data = False
                table_data_count = 0
                cleaned_lines.append(line_stripped)
                continue

            # 第三级: 数值+单位检测
            words = line_stripped.split()
            if len(words) >= 3:
                has_voltage = bool(self._voltage_pattern.search(line_stripped))
                has_current = bool(self._current_pattern.search(line_stripped))
                has_resistance = bool(self._resistance_pattern.search(line_stripped))
                has_frequency = bool(self._frequency_pattern.search(line_stripped))
                has_temp = bool(self._temp_pattern.search(line_stripped))

                if (has_voltage or has_current or has_resistance or has_frequency or has_temp):
                    digit_count = sum(1 for w in words if re.search(r'\d', w))
                    if digit_count >= 5 and len(line_stripped) > 200:
                        in_table_data = True
                        table_data_count += 1
                        if table_data_count <= 5:
                            cleaned_lines.append(line_stripped)
                        continue

            if in_table_data:
                table_data_count += 1
                if table_data_count > 10:
                    in_table_data = False
                    table_data_count = 0
                continue

            # 第四级: 带描述的电压值保留
            if self._voltage_pattern.search(line_stripped) and not re.search(r'^[±\d\s.]+$', line_stripped):
                if re.search(r'[A-Za-z]{2,}', line_stripped):
                    in_chart_area = False
                    chart_line_count = 0
                    in_package_section = False
                    in_latex_table = False
                    in_table_data = False
                    table_data_count = 0
                    cleaned_lines.append(line_stripped)
                    continue

            # 第五级: 封装尺寸章节过滤
            line_lower = line_stripped.lower()
            if any(kw.lower() in line_lower for kw in self.PACKAGE_SECTION_INDICATORS):
                in_package_section = True
                continue

            if in_package_section:
                if any(kw.lower() in line_lower for kw in self.PACKAGE_EXIT_INDICATORS):
                    in_package_section = False
                else:
                    continue

            # 第六级: 高密度数字行过滤
            words = line_stripped.split()
            if len(words) >= 5:
                digit_count = sum(1 for w in words if w.replace('±', '').replace('+', '').replace('-', '').replace('.', '').isdigit())
                if digit_count / len(words) > 0.6:
                    in_chart_area = True
                    chart_line_count += 1
                    continue

            # 第七级: 图表区域检测
            if '(V)' in line_stripped and line_stripped.endswith('(V)') and not re.search(r'[A-Za-z]{3,}', line_stripped):
                in_chart_area = True
                chart_line_count += 1
                continue

            if any(label in line_stripped for label in self.AXIS_LABELS):
                in_chart_area = True
                chart_line_count += 1
                continue

            if len(line_stripped) == 4 and line_stripped[0].isupper() and line_stripped[1:].isdigit():
                in_chart_area = True
                chart_line_count += 1
                continue

            if 'Figure' in line_stripped and any(char.isdigit() for char in line_stripped):
                in_chart_area = True
                chart_line_count = 0
                continue

            if any(title in line_stripped for title in self.CHART_TITLES):
                in_chart_area = True
                chart_line_count = 0
                continue

            if in_chart_area:
                chart_line_count += 1
                if chart_line_count > 20:
                    in_chart_area = False
                    chart_line_count = 0
                continue

            # 第八级: 布局代码过滤
            cleaned_line = self._layout_code_pattern.sub('', line_stripped)

            if cleaned_line.strip():
                cleaned_lines.append(cleaned_line)

        return '\n'.join(cleaned_lines).strip()


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

    def __init__(self, settings=None):
        """初始化统一Agent节点集合，组合意图路由和文本分析能力。"""
        self.settings = settings or get_settings()
        self.intent_router = IntentRouterNode()
        self.text_nodes = TextAnalysisNodes(settings)
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


def create_unified_agent_nodes(settings=None) -> UnifiedAgentNodes:
    """创建统一Agent节点集合实例的工厂函数。"""
    return UnifiedAgentNodes(settings)
