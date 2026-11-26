"""
表格结构化与参数记录抽取
=========================

把 MinerU 输出的表格块（原样 HTML / Markdown 管道表 / 空格对齐文本）
解析成**规整的表格结构**，并抽取成**带工况与列语义的参数记录**。

为什么需要这一层
----------------
原始表格在检索侧丢失了三样关键信息，导致"参数题"只能靠模型在多行里猜：

1. **合并单元格**：``rowspan`` / ``colspan`` 未展开，导致行列错位，
   一个数值到底属于哪一列无从判断。
2. **列语义**：规格书里数值列通常是 Min / Typ / Max 三列，
   展平成文本后只剩"某行第 N 个数"。
3. **工况**：同一参数常有多行、不同测试条件（如 RDS(on) 在 VGS=-10V 与
   VGS=-4.5V 下最大值不同），行与条件必须绑定。

输出分层（对应"整表 + 参数行块 + 检索文本"三层存储）：

- ``ParsedTable``：整表（表头展平、行展开、列角色、单位）
- ``ParamRecord``：参数行块（参数名/符号/条件/min/typ/max/单位/来源定位）
- ``record_to_text``：把参数行序列化成给 BGE / BM25 / Rerank 用的检索文本

列角色的判定顺序
----------------
1. 表头文本命中关键词（min / typ / max / unit / conditions / symbol / parameter）；
2. 位置启发式：去掉名称/符号/条件/单位列后，末尾三列依次视为 min/typ/max；
3. 两者都判不出的列保持未知，**不猜**——由调用方按"条件冲突需显式处理"的原则返回候选。
"""
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 表头关键词
_ROLE_KEYWORDS = {
    "min": ("min", "minimum"),
    "typ": ("typ", "typical"),
    "max": ("max", "maximum"),
    "unit": ("unit", "units", "单位"),
    "conditions": ("condition", "conditions", "test condition", "条件"),
    "symbol": ("symbol", "符号"),
    # 注意：不能用 "package" —— 器件信息表的表头(Type|Package|Marking)
    # 会被误判成参数表头
    "parameter": ("parameter", "parameters", "参数", "param"),
    "value": ("value", "values", "数值"),
}

# 数值列缺失时的占位（规格书里用 "-" 表示"未给出"，不是 0）
MISSING = "-"


def _collapse_ws(text: str) -> str:
    """折叠连续空白。"""
    return re.sub(r"[ \t　]+", " ", text or "").strip()


def _clean_latex(text: str) -> str:
    """还原 MinerU 数学包裹：把 `$...$` LaTeX 片段还原为可读文本。

    MinerU 对表格/正文里的符号常输出 LaTeX 数学模式：`$t_S$`（tS）、
    `$V_{OUT}$`（VOUT）、`$1 ^ { 2 } \\mathbb { C }$`（I²C 的 OCR 形态）。
    这些标记会让 embedding 语义偏移（实测 Settling time 行因 `$t_S$`
    前缀导致向量召回跌出 top10）、BM25 分词错乱。还原策略：
    常见命令（mathbb/mathrm/text/boldsymbol）取参数、剥结构符号
    （^ _ { }）、保留字母数字；无法还原的复杂公式剥定界符保留内容。
    """
    if not text or "$" not in text:
        return text

    def _还原(m: "re.Match[str]") -> str:
        inner = m.group(1)
        inner = re.sub(r"\\(?:mathbb|mathrm|text|boldsymbol|hat|bar|tilde)\s*\{([^}]*)\}", r"\1", inner)
        inner = re.sub(r"\\[A-Za-z]+", " ", inner)          # 其余命令名丢弃
        inner = re.sub(r"[\\{}^_~]", " ", inner)            # 结构符号
        inner = re.sub(r"\s+", "", inner)                    # 公式内空白无语义
        return inner

    out = re.sub(r"\$([^$]+)\$", _还原, text)
    return _collapse_ws(out)


def _clean_cell(text: str) -> str:
    """折叠空白并去掉 HTML 残留标签与 LaTeX 数学包裹。

    <sub> 内容保留（C<sub>iss</sub>→Ciss，下标是符号语义）；
    <sup> 内容保留（m<sup>2</sup>→m2，单位上标是语义）；词尾/串尾的
    脚注引用残渣剥除（Characteristics<sup>3</sup>)→Characteristics）；
    `$...$` LaTeX 数学包裹还原为可读文本（MinerU 公式模式曾污染
    embedding 与 BM25——实测 Settling time 行因 `$t_S$` 前缀跌出
    向量召回 top10）。
    """
    text = _clean_latex(text)
    text = re.sub(r"<sub>(.*?)</sub>", r"\1", text or "", flags=re.S)
    text = re.sub(r"<sup>(.*?)</sup>", r"\1", text or "", flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"[ \t　]+", " ", text)
    # 脚注引用残渣：字母/右括号后紧跟的 1-2 位数字+')'（如 '°C1)'、's3)'）
    text = re.sub(r"(?<=[A-Za-z%）)])\d{1,2}\)(?=\s|$|[,;，；])", "", text)
    return text.strip()


def _split_param_symbol(param: str) -> Tuple[str, str]:
    """从"符号+参数名"合并格里切出 symbol（TI 系表格常见）。

    如 'VCM Common-mode voltage range' → ('VCM', 'Common-mode voltage range')。
    保守切分：首 token 呈符号形态（大写占比高或带括号后缀）且剩余部分
    含 ≥2 个连续小写字母（是描述词组）才切；普通词组（'Input capacitance'）
    与节标题行（'OFFSET VOLTAGE'）均不切。
    """
    text = (param or "").strip()
    m = re.match(r"^([A-Z][A-Za-z]{0,4}(?:\([A-Za-z0-9]+\))?)\s+([A-Za-z].{1,})$", text)
    if not m:
        return "", text
    sym, rest = m.group(1), m.group(2).strip()
    if not re.search(r"[a-z]{2,}", rest):
        return "", text                       # 全大写剩余 → 节标题行，不切
    letters = [c for c in sym if c.isalpha()]
    upper_ok = sym.endswith(")") or sum(c.isupper() for c in letters) >= max(1, len(letters) - 1)
    if not upper_ok:
        return "", text
    return sym, rest


def _norm(text: str) -> str:
    """归一化：去掉所有空白并转小写，用于表头关键词匹配。"""
    return re.sub(r"\s+", "", (text or "").lower())


def _classify_header(text: str) -> Optional[str]:
    """判断一个表头单元格对应的列角色。"""
    n = _norm(text)
    if not n:
        return None
    for role, keys in _ROLE_KEYWORDS.items():
        for k in keys:
            if k in n:
                return role
    return None


# ---------------------------------------------------------------- HTML 表格
class _SpanTableParser(HTMLParser):
    """解析 HTML 表格，保留 rowspan / colspan。

    每个单元格存为 (text, colspan, rowspan)；展平在 _expand_spans 中完成。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[Tuple[str, int, int]]] = []
        self._row: Optional[List[Tuple[str, int, int]]] = None
        self._cell: Optional[List[str]] = None
        self._colspan = 1
        self._rowspan = 1

    @staticmethod
    def _to_int(value: Any, default: int = 1) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            if self._row is None:
                return
            self._cell = []
            attr = {k.lower(): (v or "") for k, v in attrs}
            self._colspan = self._to_int(attr.get("colspan"), 1)
            self._rowspan = self._to_int(attr.get("rowspan"), 1)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append((_clean_cell("".join(self._cell)), self._colspan, self._rowspan))
            self._cell = None
            self._colspan = 1
            self._rowspan = 1
        elif tag == "tr" and self._row is not None:
            if any(c[0] for c in self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _expand_spans(rows: List[List[Tuple[str, int, int]]]) -> List[List[str]]:
    """按占用矩阵展开 rowspan / colspan。

    旧实现把 colspan 当成 rowspan 做纵向填充，且只填左上角一个格子，
    遇到跨行跨列同时出现时会行列错位、制造脏记录。这里改为：
      - 先算总列宽（每行 colspan 之和的最大值）
      - 用 None 标记"已被上方跨行占用的格子"，逐行跳过
      - 一个单元格同时覆盖 rowspan × colspan 的所有位置
    """
    if not rows:
        return []
    width = max(1, max(sum(c[1] for c in row) for row in rows))
    grid: List[List[Optional[str]]] = [[None] * width for _ in rows]

    for r, row in enumerate(rows):
        col = 0
        for text, colspan, rowspan in row:
            while col < width and grid[r][col] is not None:
                col += 1
            for dr in range(rowspan):
                rr = r + dr
                if rr >= len(grid):
                    break
                for dc in range(colspan):
                    cc = col + dc
                    if cc < width and grid[rr][cc] is None:
                        grid[rr][cc] = text
            col += colspan

    return [["" if cell is None else cell for cell in row] for row in grid]


def _expand_spans_annotated(rows: List[List[Tuple[str, int, int]]]
                            ) -> Tuple[List[List[str]], List[Dict[int, Tuple[int, int]]]]:
    """展开 rowspan/colspan，并返回每个**原始格**覆盖的物理列区间。

    返回 (matrix, cell_ranges)：cell_ranges[r][原始格序号] = (c1, c2)
    （0-based 闭区间）。同一原始格展开出的多个物理格共享同一序号。

    用途：OPA211 类表格的值格用 colspan 表达列角色——值格横跨
    MIN/TYP/MAX 全部三列 = 典型值；横跨 MIN~TYP = 下限；只占 MAX = 上限。
    展开后 span 信息丢失会使这些角色无法恢复（实测 g015/g028）。
    """
    if not rows:
        return [], []
    width = max(1, max(sum(c[1] for c in row) for row in rows))
    grid: List[List[Optional[str]]] = [[None] * width for _ in rows]
    cell_ranges: List[Dict[int, Tuple[int, int]]] = []

    for r, row in enumerate(rows):
        ranges: Dict[int, Tuple[int, int]] = {}
        col = 0
        for gi, (text, colspan, rowspan) in enumerate(row):
            while col < width and grid[r][col] is not None:
                col += 1
            for dr in range(rowspan):
                rr = r + dr
                if rr >= len(grid):
                    break
                for dc in range(colspan):
                    cc = col + dc
                    if cc < width and grid[rr][cc] is None:
                        grid[rr][cc] = text
            ranges[gi] = (col, col + colspan - 1)
            col += colspan
        cell_ranges.append(ranges)

    matrix = [["" if cell is None else cell for cell in row] for row in grid]
    return matrix, cell_ranges


def _value_role_for_range(c1: int, c2: int, c_min: int, c_typ: int, c_max: int) -> Optional[str]:
    """按值格覆盖的物理列区间推断 min/typ/max 角色。

    - 起点 MAX（含只占 MAX）→ max（V_OUT (V+)-0.2 类）
    - 覆盖 [MIN..MAX] 全部 → typ（未分级典型值，GBW=80 类）
    - 起点 MIN、未到 MAX → min（范围下限，V_OUT (V-)+0.2 类）
    - 起点 TYP（含只占 TYP）→ typ
    """
    if c_min < 0 or c_max < 0:
        return None
    if c1 >= c_max:
        return "max"
    if c1 <= c_min and c2 >= c_max:
        return "typ"
    if c1 <= c_min:
        return "min"
    if c1 <= c_typ:
        return "typ"
    return None


# ---------------------------------------------------------------- 结构化结果
@dataclass
class ParsedTable:
    """结构化表格。

    Attributes:
        headers: 展平后的表头（多级表头按列合并，缺失为空串）
        rows: 展开后的数据行（与 headers 等宽）
        column_roles: 每列的角色（min/typ/max/unit/conditions/symbol/parameter/''）
        units: 每列识别到的单位
        source_format: html | markdown | spaced
    """

    headers: List[str] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)
    column_roles: List[str] = field(default_factory=list)
    units: List[str] = field(default_factory=list)
    column_labels: List[str] = field(default_factory=list)
    raw_rows: List[str] = field(default_factory=list)
    header_row_index: int = -1
    source_format: str = ""
    cell_ranges: Optional[List[Dict[int, Tuple[int, int]]]] = None  # 原始格覆盖的物理列区间

    @property
    def n_cols(self) -> int:
        return len(self.rows[0]) if self.rows else len(self.headers)


@dataclass
class ParamRecord:
    """一条参数记录（参数行块）。

    Attributes:
        param: 参数名，如 "Drain-source on-state resistance"
        symbol: 参数符号，如 "RDS(on)"
        conditions: 测试条件原文，如 "VGS=-10 V, ID=-14.9 A"
        min / typ / max: 三列数值（缺失为 None，不把 "-" 当 0）
        unit: 单位
        page: 页码
        table_id: 表格标识（页码+序号）
        row_index: 该记录在表内的行号
        extra_values: 未能归入 min/typ/max 的其他数值（保持可见，不丢弃）
    """

    param: str = ""
    symbol: str = ""
    conditions: str = ""
    min: Optional[str] = None
    typ: Optional[str] = None
    max: Optional[str] = None
    unit: str = ""
    page: int = 0
    table_id: str = ""
    row_index: int = 0
    extra_values: List[str] = field(default_factory=list)
    value_columns: List[Tuple[str, str]] = field(default_factory=list)
    raw_row: str = ""          # 原始行文本（列切分前的完整内容，用于条件匹配兜底）
    conditions_raw: str = ""    # 未经清洗的原始条件文本（匹配优先用它）
    param_garbled: bool = False  # param 字段疑似 MinerU 交错乱码（symbol 通常完好）
    schema_version: int = 2     # 记录结构版本；反序列化时校验
    valid: bool = True          # 结构校验失败 → False（调用方不得采信）

    SCHEMA_VERSION = 2

    def to_text(self, focus: bool = False) -> str:
        """序列化成检索/上下文用文本。

        focus=False（默认）：完整字段（参数/条件/数值列/单位/来源），
        供 LLM context 与展示。
        focus=True：**向量 embedding 专用**的语义聚焦文本——只保留
        param/symbol/数值/单位。实测完整文本里的条件串（'VS=±15 V,
        G=-1, 10-V step, CL=100 pF'）与来源行会摊薄向量语义：
        'Settling time' 行全文 sim 0.509 低于封装垃圾行 0.515
        （跌出召回 top10），聚焦文本 sim 0.604 反超。
        """
        def _v(x):
            return x if x else "未给出"

        if focus:
            parts = [self.param or self.symbol or ""]
            if self.symbol and self.param and self.symbol != self.param:
                parts.append(self.symbol)
            vals = [v for v in (self.min, self.typ, self.max) if v]
            parts.extend(vals)
            if self.unit:
                parts.append(self.unit)
            seen: List[str] = []
            for p_ in parts:
                p_ = str(p_).strip()
                if p_ and p_ not in seen:
                    seen.append(p_)
            return " ".join(seen)

        parts = [f"参数：{self.param}" if self.param else "", f"符号：{self.symbol}" if self.symbol else ""]

        parts = [f"参数：{self.param}" if self.param else "", f"符号：{self.symbol}" if self.symbol else ""]
        lines = []
        if parts:
            lines.append(" ".join(p for p in parts if p))
        if self.conditions:
            lines.append(f"条件：{self.conditions}")
        lines.append(
            f"数值列：Min={_v(self.min)}；Typ={_v(self.typ)}；Max={_v(self.max)}"
        )
        if self.value_columns:
            lines.append("数值列明细：" + "；".join(f"{k}={v}" for k, v in self.value_columns))
        if self.extra_values:
            lines.append("其他数值：" + "、".join(self.extra_values))
        if self.unit:
            lines.append(f"单位：{self.unit}")
        lines.append(f"来源：第{self.page}页 表{self.table_id} 第{self.row_index}行")
        return "\n".join(lines)

    def to_metadata(self) -> Dict[str, Any]:
        """可写入 ChromaDB metadata 的完整表示。

        Chroma 只接受简单类型，因此嵌套结构用 JSON 字符串承载；
        同时保留扁平字段，便于旧代码/检索侧直接读取。
        """
        import json as _json
        return {
            # 扁平字段（兼容 + 检索展示）
            "param": self.param,
            "symbol": self.symbol,
            "conditions": self.conditions,
            "pmin": self.min or "",
            "ptyp": self.typ or "",
            "pmax": self.max or "",
            "punit": self.unit,
            "ppage": self.page,
            "ptable": self.table_id,
            "prow": self.row_index,
            "raw_row": self.raw_row or "",
            "conditions_raw": self.conditions_raw or self.conditions or "",
            "param_garbled": bool(self.param_garbled),
            "schema_version": self.SCHEMA_VERSION,
            # 复杂结构 → JSON 字符串
            "pvals": _json.dumps(dict(self.value_columns or []), ensure_ascii=False),
            "pextra": _json.dumps(list(self.extra_values or []), ensure_ascii=False),
        }

    @classmethod
    def from_metadata(cls, meta: Dict[str, Any]) -> "ParamRecord":
        """从 Chroma metadata 还原记录；结构不符则返回 valid=False 的记录。

        不做"尽力兼容旧脏数据"——旧 schema 缺少 conditions_raw / raw_row 时
        条件匹配不可信，标记 invalid_structure，由调用方拒绝采信。
        """
        import json as _json

        meta = meta or {}
        version = int(meta.get("schema_version") or 0)

        def _load(text, default):
            if not text:
                return default
            try:
                return _json.loads(text)
            except (TypeError, ValueError):
                return default

        value_columns = _load(meta.get("pvals"), {})
        if isinstance(value_columns, list):        # 旧格式 "k=v|k=v"
            pairs = {}
            for part in value_columns:
                if isinstance(part, str) and "=" in part:
                    k, v = part.split("=", 1)
                    pairs[k.strip()] = v.strip()
            value_columns = pairs

        rec = cls(
            param=meta.get("param", "") or "",
            symbol=meta.get("symbol", "") or "",
            conditions=meta.get("conditions", "") or "",
            min=meta.get("pmin") or None,
            typ=meta.get("ptyp") or None,
            max=meta.get("pmax") or None,
            unit=meta.get("punit", "") or "",
            page=int(meta.get("ppage") or meta.get("page") or 0),
            table_id=str(meta.get("ptable", "") or ""),
            row_index=int(meta.get("prow") or 0),
            raw_row=meta.get("raw_row", "") or "",
            conditions_raw=meta.get("conditions_raw", "") or "",
            param_garbled=bool(meta.get("param_garbled") or False),
            extra_values=_load(meta.get("pextra"), []),
            value_columns=list((value_columns or {}).items()) if isinstance(value_columns, dict) else [],
            schema_version=version,
        )
        # 结构校验：新版必须带 conditions_raw 或 raw_row，否则条件匹配不可信
        if version < cls.SCHEMA_VERSION and not (rec.conditions_raw or rec.raw_row):
            rec.valid = False
        return rec


# ---------------------------------------------------------------- 表头识别
def _detect_header_rows(rows: List[List[str]]) -> int:
    """判断开头有几行是表头。

    判据比"命中数>=2"更严：参数表表头必须同时出现
    「名称/符号/条件类」与「数值/单位类」列，否则像器件信息表
    (Type|Package|Marking|Leadfree) 这类表头会被误当成参数表头。
    """
    header_hits = 0
    for row in rows[:3]:
        roles = [_classify_header(c) for c in row]
        name_like = any(r in ("parameter", "symbol", "conditions") for r in roles)
        value_like = any(r in ("min", "typ", "max", "unit", "value") for r in roles)
        if name_like and value_like:
            header_hits += 1
        elif header_hits:
            break
        else:
            break
    return header_hits


def _merge_header_rows(rows: List[List[str]]) -> List[str]:
    """把多行表头按列合并成一个表头字符串。"""
    if not rows:
        return []
    width = max(len(r) for r in rows)
    merged = []
    for c in range(width):
        parts = []
        for row in rows:
            if c < len(row):
                cell = row[c].strip()
                if cell and cell not in parts:
                    parts.append(cell)
        merged.append(" ".join(parts))
    return merged


_UNIT_LIKE = re.compile(r"^[A-Za-zΩμ°%][A-Za-zΩμ°%/\d\s\-]{0,10}$")


def _assign_roles(headers: List[str], n_cols: int,
                  body: Optional[List[List[str]]] = None) -> List[str]:
    """给每列分配角色。

    顺序：表头关键词 → 单位列剥离 → 位置启发式（末尾三列为 min/typ/max）。
    没有表头的 HTML 参数表必须靠这一步，顺序错了会让整列数值错位。
    """
    roles = [_classify_header(h) for h in headers]

    # 1) 末列若是单位样式（如 "A"、"mΩ"、"K/W"）→ 单位列
    if body and n_cols and not roles[-1]:
        col_values = [row[-1] for row in body if row and row[-1]]
        unit_like = sum(1 for v in col_values if _UNIT_LIKE.match(v))
        if col_values and unit_like >= max(1, len(col_values) // 2):
            roles[-1] = "unit"

    # 2) 数值列 = 不属于名称/符号/条件/单位的列
    value_cols = [i for i, r in enumerate(roles)
                  if r not in ("parameter", "symbol", "conditions", "unit")]

    # 3) 表头没给出 min/typ/max 时按位置分配（末尾三列）
    if not any(r in ("min", "typ", "max") for r in roles):
        if len(value_cols) >= 3:
            for idx, r in zip(value_cols[-3:], ("min", "typ", "max")):
                if roles[idx] in (None, ""):
                    roles[idx] = r
        else:
            for idx in value_cols:
                if roles[idx] in (None, ""):
                    roles[idx] = "value"

    # 4) 无表头时补名称/符号列：首列非数值 → 参数名；第二列短且非数值 → 符号
    if not any(r == "parameter" for r in roles) and body:
        first_col = [row[0] for row in body if row]
        non_value = sum(1 for v in first_col if v and not _is_value(v))
        if first_col and non_value >= max(1, len(first_col) // 2):
            roles[0] = "parameter"
            if n_cols > 1 and not any(r == "symbol" for r in roles):
                second = [row[1] for row in body if len(row) > 1]
                sv = sum(1 for v in second if v and not _is_value(v) and len(v) <= 24)
                if second and sv >= max(1, len(second) // 2):
                    roles[1] = "symbol"
                # 条件列：符号列之后第一个"文本型"列（如 "VGS=0 V, ID=-250µA"）
                if n_cols > 2 and not any(r == "conditions" for r in roles):
                    for ci in range(2, n_cols):
                        if roles[ci] in (None, "", "value"):
                            vals = [row[ci] for row in body if len(row) > ci and row[ci]]
                            text_like = sum(1 for v in vals if not _is_value(v) and len(v) <= 80)
                            if vals and text_like >= max(1, len(vals) // 2):
                                roles[ci] = "conditions"
                                break

    return [r or "" for r in roles]


def _extract_units(headers: List[str], rows: List[List[str]], roles: List[str]) -> List[str]:
    """从表头或数值列尾部提取单位。"""
    units = [""] * len(headers)
    for i, h in enumerate(headers):
        if roles[i] == "unit":
            units[i] = h
    # 数值列后一列若是纯单位（如 "A"、"mΩ"）也当单位
    for i, role in enumerate(roles):
        if role not in ("min", "typ", "max"):
            continue
        nxt = i + 1
        if nxt < len(roles) and not roles[nxt]:
            cand = headers[nxt].strip()
            if cand and re.fullmatch(r"[A-Za-zΩμ°%/\d\s\-]{1,12}", cand):
                units[nxt] = cand
    return units


# ---------------------------------------------------------------- 解析入口
def _parse_html(html: str) -> Optional[ParsedTable]:
    parser = _SpanTableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return None
    rows, cell_ranges = _expand_spans_annotated(parser.rows)
    if not rows:
        return None
    raw_rows = [" | ".join(c for c in r if (c or "").strip()) for r in rows]
    return _build_table(rows, "html", raw_rows=raw_rows, cell_ranges=cell_ranges)


def _parse_markdown(markdown: str) -> Optional[ParsedTable]:
    rows = []
    for line in (markdown or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells if c != ""):
            continue
        if any(cells):
            rows.append(cells)
    if not rows:
        return None
    return _build_table(rows, "markdown")


def _fragment_spans(line: str):
    """按 2+ 空格切分一行，返回 (起始位置, 片段文本)。"""
    spans = []
    for m in re.finditer(r"\S(?:.*?\S)?(?=\s{2,}|$)", line):
        spans.append((m.start(), m.group().strip()))
    return spans


_KW_SCAN = [(kw, role) for role, kws in _ROLE_KEYWORDS.items() for kw in kws]


def _keyword_positions(line: str):
    """在表头行里按**关键词出现位置**定位列边界。

    仅按"2+ 空格"切分不够：``Parameter  Symbol Conditions  Value  Unit``
    中 Symbol 与 Conditions 只隔一个空格，会被合并成一列。
    """
    low = (line or "").lower()
    found = {}
    for kw, role in _KW_SCAN:
        idx = low.find(kw)
        if idx < 0:
            continue
        # 同一位置优先更具体的角色（parameter/symbol/conditions 优先于 value）
        if idx not in found:
            found[idx] = role
    return sorted(found.items())


def _header_hits(line: str):
    """一行是否是表头：同时含「名称/符号/条件」与「数值/单位」类列。"""
    dedup = _keyword_positions(line)
    seen, uniq = set(), []
    for pos, r in dedup:
        if r not in seen:
            seen.add(r)
            uniq.append((pos, r))
    dedup = uniq
    name_like = any(r in ("parameter", "symbol", "conditions") for _, r in dedup)
    value_like = any(r in ("min", "typ", "max", "unit", "value") for _, r in dedup)
    return dedup if (name_like and value_like) else []


def _parse_aligned_segment(lines, header_spans):
    """按表头 + 子表头的位置切分一段行（表格子段）。

    只信任**表头行与紧随其后的子表头行**：
      - 表头给主列（Parameter / Symbol / Conditions / Value / Unit）
      - 子表头给子列标签（如 "≤10 secs"、"steady state"、"Pulse"）
    绝不再从数据行反推列边界——数值反复出现在同一位移会被误判成新列，
    把 -14.9 / -12.6 拆成孤立列（F05 的根因）。
    """
    header_positions = [pos for pos, _ in header_spans]
    header_roles = dict(header_spans)

    boundaries = list(header_positions)
    sub_labels = {}

    # 子表头：紧邻表头的下一行（若它不是纯数值行）
    sub_positions = []
    if len(lines) > 1:
        sub_line = lines[1]
        frags = _fragment_spans(sub_line)
        numeric = sum(1 for _, txt in frags if _is_value(txt))
        if frags and numeric <= max(1, len(frags) // 3):
            # 子表头里的工况标签可能只隔一个空格（"≤10 secs steady state"），
            # 先用已知标签短语定位，再回退到 2+ 空格的片段
            known = []
            for pat, label in (
                (r"≤?\s*10\s*secs?", "≤10 secs"),
                (r"steady\s*state", "steady state"),
                (r"pulse", "pulse"),
            ):
                for m in re.finditer(pat, sub_line, re.IGNORECASE):
                    if any(abs(m.start() - p0) <= 1 for p0, _ in known):
                        continue
                    known.append((m.start(), label))
            if len(known) >= 2:
                sub_positions = [(pos, lab) for pos, lab in sorted(known)]
            else:
                for pos, txt in frags:
                    if not txt or _is_value(txt) or pos in header_roles:
                        continue
                    if any(pos > hp for hp in header_positions):
                        sub_positions.append((pos, txt))

    if len(sub_positions) >= 2:
        # 子表头把 "Value" 区域拆成多个数据列（如 ≤10 secs / steady state）。
        # 这些列一律是**数据列**，不能继承 Conditions 的语义；
        # 同时移除落在该区域内的原表头边界，避免出现空列。
        lo = sub_positions[0][0]
        hi = sub_positions[-1][0]
        boundaries = [p for p in boundaries if not (lo <= p <= hi or (p > hi and header_roles.get(p) == "value"))]
        for pos, txt in sub_positions:
            boundaries.append(pos)
            sub_labels[pos] = txt
        sub_role = {pos: "value" for pos, _ in sub_positions}
    else:
        sub_role = {}
        for pos, txt in sub_positions:
            boundaries.append(pos)
            sub_labels[pos] = txt

    boundaries = sorted(set(boundaries))

    def _owner_role(pos):
        owner = None
        for hp in sorted(header_roles):
            if hp <= pos:
                owner = hp
            else:
                break
        return header_roles.get(owner, "") if owner is not None else ""

    labels, roles = [], []
    header_text = {}
    for pos, txt in _fragment_spans(lines[0]):
        header_text.setdefault(pos, txt)

    for pos in boundaries:
        labels.append(sub_labels.get(pos) or header_text.get(pos, ""))
        roles.append(sub_role.get(pos) or _owner_role(pos))

    rows = []
    for line in lines:
        cells = []
        for j, pos in enumerate(boundaries):
            start = pos
            end = boundaries[j + 1] if j + 1 < len(boundaries) else len(line)
            cells.append(line[start:end].strip() if start < len(line) else "")
        # 跨列片段回填：一个原始片段被列边界切开时补回首列
        for frag_start, frag_text in _fragment_spans(line):
            frag_end = frag_start + len(frag_text)
            covered = []
            for i, pos in enumerate(boundaries):
                col_end = boundaries[i + 1] if i + 1 < len(boundaries) else len(line)
                if pos < frag_end and col_end > frag_start:
                    covered.append(i)
            if len(covered) > 1:
                first = covered[0]
                cells[first] = _collapse_ws((cells[first] + " " + frag_text).strip())
                for other in covered[1:]:
                    cells[other] = _collapse_ws(cells[other].replace(frag_text, ""))
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            rows.append(cells)

    if len(rows) < 2:
        return None
    return _build_table(rows, "spaced", forced_roles=roles, forced_labels=labels,
                        forced_header_index=0, raw_rows=list(lines))


def _parse_spaced(text: str):
    """解析空格对齐的表格（MinerU 的 code-fence 输出）。

    一个 code-fence 块里可能包含**多张表**（如"器件信息表"+"最大额定值表"），
    因此按表头行把块切分成多个子表分别解析。
    """
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if len(lines) < 2:
        return []

    header_idxs = []
    header_spans_list = []
    for i, line in enumerate(lines):
        hits = _header_hits(line)
        if hits:
            header_idxs.append(i)
            header_spans_list.append(hits)
    if not header_idxs:
        return []

    tables = []
    for hi, hidx in enumerate(header_idxs):
        end = header_idxs[hi + 1] if hi + 1 < len(header_idxs) else len(lines)
        seg = lines[hidx:end]
        if len(seg) < 2:
            continue
        tbl = _parse_aligned_segment(seg, header_spans_list[hi])
        if tbl:
            tables.append(tbl)
    return tables



def _build_table(rows, fmt, forced_roles=None, forced_labels=None,
                 forced_header_index=None, raw_rows=None, cell_ranges=None):
    """把原始二维行整理成结构化表格（识别表头、列角色、单位）。

    forced_roles / forced_labels：空格对齐表已按列位置切分并算好角色时传入，
    避免再按文本猜测表头。
    cell_ranges：_expand_spans_annotated 的原始格列区间（colspan 跨列
    值格的列角色推断需要，OPA211 类表格）。
    """
    width = max(len(r) for r in rows)
    rows = [list(r) + [""] * (width - len(r)) for r in rows]

    if forced_roles is not None:
        headers = list(forced_labels or [])[:width]
        header_index = forced_header_index if forced_header_index is not None else -1
        headers = headers + [""] * (width - len(headers))
        body = rows
        roles = list(forced_roles)[:width]
        roles = roles + [""] * (width - len(roles))
        body_ranges = cell_ranges
    else:
        n_header = _detect_header_rows(rows)
        header_rows = rows[:n_header]
        body = rows[n_header:]
        # cell_ranges 切齐到 body 坐标系（调用方传的是含表头的全行区间）
        body_ranges = cell_ranges[n_header:] if cell_ranges else None

        headers = _merge_header_rows(header_rows) if header_rows else []
        if not headers:
            headers = [""] * width
        headers = headers + [""] * (width - len(headers))
        roles = _assign_roles(headers, width, body)

    units = _extract_units(headers, body, roles)
    raw = list(raw_rows or [])
    if raw and len(raw) == len(rows):
        raw = raw[len(rows) - len(body):]
    return ParsedTable(
        headers=headers,
        rows=body,
        raw_rows=raw,
        column_roles=roles,
        units=units,
        column_labels=list(headers),
        header_row_index=(header_index if forced_roles is not None else -1),
        source_format=fmt,
        cell_ranges=body_ranges,
    )



def parse_tables(content: str) -> List[ParsedTable]:
    """把 MinerU 的表格块内容解析为**结构化表格列表**。

    一个 code-fence 块可能包含多张表，因此返回列表；
    HTML / Markdown 形态只有一张表。
    """
    raw = content or ""
    if "<table" in raw.lower():
        table = _parse_html(raw)
        return [table] if table else []
    if "|" in raw:
        table = _parse_markdown(raw)
        return [table] if table and table.rows else []
    stripped = re.sub(r"^```.*$", "", raw, flags=re.MULTILINE).strip()
    if stripped:
        return [t for t in _parse_spaced(stripped) if t and t.rows]
    return []


def parse_table_content(content: str) -> Optional[ParsedTable]:
    """解析单个表格（兼容入口；多表时返回第一张）。"""
    tables = parse_tables(content)
    return tables[0] if tables else None


# ---------------------------------------------------------------- 参数记录
def _is_value(cell: str) -> bool:
    """判断单元格是否像数值（允许 -12.6 / 4430 / 8.8 / 1.5e3 等）。"""
    c = (cell or "").strip()
    if not c or c == MISSING:
        return False
    return bool(re.fullmatch(r"[-+]?[\d.,]+\s*[A-Za-zΩμ°%]*", c))


def _normalize_value(cell: str) -> Optional[str]:
    """把数值单元格规范化；"-" 视为未给出（None）。"""
    c = (cell or "").strip()
    if not c or c == MISSING:
        return None
    return c


def extract_param_records(table: ParsedTable, page: int, table_id: str) -> List[ParamRecord]:
    """把结构化表格抽成参数记录列表。

    参数名取第 0 列（角色 parameter 或首列文本），符号取第 1 列，
    条件取 conditions 角色列，其余数值列按 min/typ/max 归位，
    未能归位的数值保留在 extra_values（可见但不猜）。
    """
    records: List[ParamRecord] = []
    roles = table.column_roles or [""] * table.n_cols

    # ── 表头定位：MIN/TYP/MAX 的物理列号（格驱动取值用）─────────────
    # 从 headers 数组本身判定（'MIN TYP MAX' 合并格 = 单物理列承载三列
    # 语义，c_typ 取 c_min+1 仅供区间规则参考）。cell_ranges 已在
    # _build_table 内切齐到 body 坐标系。
    cell_ranges = table.cell_ranges
    c_min_c = c_typ_c = c_max_c = -1
    for _ci, h in enumerate(table.headers or []):
        htxt = _norm(h)
        if not htxt:
            continue
        if "min" in htxt and "typ" in htxt and "max" in htxt:
            c_min_c, c_typ_c, c_max_c = _ci, _ci + 1, _ci
        elif "min" in htxt:
            c_min_c = _ci
        elif "typ" in htxt:
            c_typ_c = _ci
        elif "max" in htxt:
            c_max_c = _ci

    def _row_ranges(ri: int):
        """数据行 ri（body 索引）对应的原始格区间。"""
        if not cell_ranges:
            return None
        return cell_ranges[ri] if 0 <= ri < len(cell_ranges) else None

    param_cols = [i for i, r in enumerate(roles) if r == "parameter"]
    symbol_cols = [i for i, r in enumerate(roles) if r == "symbol"]
    cond_cols = [i for i, r in enumerate(roles) if r == "conditions"]
    min_cols = [i for i, r in enumerate(roles) if r == "min"]
    typ_cols = [i for i, r in enumerate(roles) if r == "typ"]
    max_cols = [i for i, r in enumerate(roles) if r == "max"]
    unit_cols = [i for i, r in enumerate(roles) if r == "unit"]

    for ri, row in enumerate(table.rows):
        if not any((c or "").strip() for c in row):
            continue
        if table.header_row_index >= 0 and ri == table.header_row_index:
            continue  # 表头行本身不是参数记录

        param = ""
        if param_cols:
            # PARAMETER 列常被 colspan 拆成多格（如 'V_OUT'+'Voltage
            # output'）：全部拼接（相邻去重）才是完整参数名
            parts = [(row[i] or "").strip() for i in param_cols if i < len(row)]
            dedup = [p for i, p in enumerate(parts) if p and (i == 0 or p != parts[i - 1])]
            param = " ".join(dedup).strip()
        else:
            param = (row[0] or "").strip()
            # 首列是数值则不是参数名列
            if _is_value(param):
                param = ""
        # param/symbol 的格驱动提取：colspan 跨列表里 param 文本可能
        # 不在第一个 parameter 列（如 Overload recovery 行首列为空）
        gd_param = gd_symbol = ""
        gd_param_parts: List[str] = []
        row_ranges0 = _row_ranges(ri)
        if row_ranges0 is not None:
            prev = None
            for _gi, (c1, c2) in sorted(row_ranges0.items(), key=lambda kv: kv[1][0]):
                cell_txt = " ".join(
                    (row[c] or "").strip()
                    for c in range(max(0, c1), min(c2, len(row) - 1) + 1)
                ).strip()
                if not cell_txt:
                    continue
                if any(c in param_cols for c in range(c1, c2 + 1)):
                    if cell_txt != prev:
                        gd_param_parts.append(cell_txt)
                    prev = cell_txt
                elif not gd_symbol and any(c in symbol_cols for c in range(c1, c2 + 1)):
                    gd_symbol = cell_txt
            gd_param = " ".join(gd_param_parts).strip()
        if not param:
            param = gd_param
        if not param:
            continue

        symbol = (row[symbol_cols[0]] or "").strip() if symbol_cols else ""
        if not symbol:
            symbol = gd_symbol
        if param and not symbol:
            # "符号+参数名"合并格切分（TI 系常见，如 'VOS Input offset voltage'）
            sym_cand, param = _split_param_symbol(param)
            if sym_cand:
                symbol = sym_cand
        conditions = ""
        if cond_cols:
            parts = [(row[i] or "").strip() for i in cond_cols]
            # colspan 展开后相邻列是同一格的副本，拼接去重
            dedup = [p for i, p in enumerate(parts) if p and (i == 0 or p != parts[i - 1])]
            conditions = " ".join(dedup).strip()

        def _first(cols):
            for i in cols:
                v = _normalize_value(row[i] if i < len(row) else "")
                if v:
                    return v
            return None

        # ── "MIN TYP MAX 合并列"拆分 ──────────────────────────────
        # TI 系表格把三列数值合写一格（表头格 'MIN TYP MAX'，数据格
        # '±6 ±25' / '1.7 5.5'）：物理列数 < 逻辑列数，_assign_roles 会把
        # 整格塞进 min 列、单位串挤进 max。按符号语义从右对齐拆分：
        #   双值带 ± → (typ, max)——零点偏移类参数 min 无意义
        #   双值无 ± → (min, max)——范围类参数（供电 1.7~5.5）
        #   单值 → max；三值 → min/typ/max
        # 这是电气参数表的行业通用符号语义，非单一文档特例。
        # ── 格驱动取值（colspan 跨列值格，OPA211 类）──────────────
        # 值格用 colspan 表达列角色：横跨 MIN~MAX 全部 = typ（未分级典型
        # 值，GBW=80）；横跨 MIN~TYP = min（范围下限）；起点 MAX = max。
        # 单格多值文本走 ± 拆分（'±6 ±25' → typ/max）。
        # 仅当表头定位到数值列且该行存在与数值区相交的格时启用；
        # 值格必须含数字（'See Typical Characteristics' 类说明文字排除）。
        row_ranges = _row_ranges(ri)
        gd_vals: Dict[str, str] = {}
        if row_ranges is not None and c_min_c >= 0:
            for _gi, (c1, c2) in sorted(row_ranges.items(), key=lambda kv: kv[1][0]):
                if c2 < c_min_c or c1 > c_max_c:
                    continue  # 与数值区不相交（param/symbol/conditions/unit 格）
                cell_txt = " ".join(
                    (row[c] or "").strip()
                    for c in range(max(0, c1), min(c2, len(row) - 1) + 1)
                ).strip()
                if not cell_txt or not re.search(r"\d", cell_txt):
                    continue
                toks = re.findall(r"[-+±]?[\d.,]+", cell_txt)
                # 与数值区的交集宽度：≥2 是真跨列格（span 展开的副本是
                # 同一值的拷贝，不能当多值）；=1 是单物理列格
                span = min(c2, c_max_c) - max(c1, c_min_c) + 1
                if span >= 2:
                    # 跨列值格：覆盖范围决定角色（GBW 横跨三列=typ，
                    # V_OUT (V-)+0.2 跨 MIN~TYP=min，(V+)-0.2 占 MAX=max）。
                    # 值取首格原文：span 展开的副本是同一值的拷贝
                    # （join 会得到 '80 80 80'），且 '(V-) + 0.2' 的轨语义
                    # 前缀不能被数值截取丢掉
                    role = _value_role_for_range(c1, c2, c_min_c, c_typ_c, c_max_c)
                    if role and c1 < len(row):
                        gd_vals[role] = (row[c1] or "").strip()
                elif len(toks) >= 2:
                    # 单物理列内的多值文本：± 语义拆分（'±6 ±25'→typ/max，
                    # '1.7 5.5'→min/max，三值→min/typ/max）
                    vals = toks[-3:]
                    seq = (("min", "typ", "max") if len(vals) == 3
                           else (("typ", "max") if any(("±" in t or t.startswith("+")) for t in vals)
                                 else ("min", "max")))
                    for r_, v_ in zip(seq, vals):
                        gd_vals[r_] = v_
                else:
                    role = _value_role_for_range(c1, c2, c_min_c, c_typ_c, c_max_c)
                    if role:
                        gd_vals[role] = cell_txt

        if gd_vals:
            v_min = gd_vals.get("min") or None
            v_typ = gd_vals.get("typ") or None
            v_max = gd_vals.get("max") or None
        else:
            v_min, v_typ, v_max = _first(min_cols), _first(typ_cols), _first(max_cols)
            for ci, role in enumerate(roles):
                if role not in ("min", "typ", "max"):
                    continue
                cell_v = (row[ci] if ci < len(row) else "") or ""
                toks = re.findall(r"[-+±]?[\d.,]+", cell_v)
                if len(toks) < 2:
                    continue
                vals = toks[-3:]
                if len(vals) == 3:
                    seq = ("min", "typ", "max")
                elif any(("±" in t or t.startswith("+")) for t in vals):
                    seq = ("typ", "max")
                else:
                    seq = ("min", "max")
                assigned = dict(zip(seq, vals))
                v_min, v_typ, v_max = (assigned.get("min"), assigned.get("typ"),
                                       assigned.get("max"))
                break

        unit = ""
        for i in unit_cols:
            u = (row[i] or "").strip()
            if u:
                unit = u
                break
        if not unit:
            for i, u in enumerate(table.units):
                if u and i in (min_cols + typ_cols + max_cols):
                    unit = u
                    break

        raw_line = ""
        if table.raw_rows and ri < len(table.raw_rows):
            raw_line = table.raw_rows[ri]
        rec = ParamRecord(
            raw_row=raw_line or " | ".join(c for c in row if (c or "").strip()),
            param=param,
            symbol=symbol,
            conditions=_collapse_ws(conditions),
            conditions_raw=_collapse_ws(conditions),   # 原样保留，绝不清洗掉数值
            min=v_min,
            typ=v_typ,
            max=v_max,
            unit=unit,
            page=page,
            table_id=table_id,
            row_index=ri + 1,
        )
        # 交错乱码检测（保守启发式）：MinerU 折行格的字符交错产物呈
        # "小写开头的超长 token 且内部混入大写字母"形态（如
        # 'jTuhnecrtmioanl' = Junction×Thermal 交替）。标记仅供查询侧
        # 与覆盖率统计使用；symbol 字段（单行格）几乎总是完好的，
        # 身份匹配靠符号别名兜底。
        garbled = any(
            t[0].islower() and any(c.isupper() for c in t[1:])
            for t in re.findall(r"[A-Za-z]{9,}", param or "")
        )
        if garbled:
            rec.param_garbled = True

        # 条件列里若混入了**不属于任何 KEY=value 的孤立数值**（空格对齐表常见），
        # 才把它归入 extra_values；KEY=value 里的数值必须留在条件原文中，
        # 否则会变成 "VGS= , ID=" 这种残缺条件，条件匹配彻底失效。
        if conditions:
            pairs = list(re.finditer(
                r"[A-Za-z]{1,4}\s?(?:[A-Za-z]{0,2})?(?:\([A-Za-z]{1,4}\))?\s*[=:]\s*"
                r"[-+]?[\d.]+\s*[A-Za-zμ°%Ω/]*[\d]*",
                conditions,
            ))
            covered = [(m.start(), m.end()) for m in pairs]
            for m in re.finditer(r"[-+]?\d+\.?\d*\s*[A-Za-zμ°%Ω/]*", conditions):
                if any(s <= m.start() < e for s, e in covered):
                    continue
                token = m.group().strip()
                if token and token not in rec.extra_values:
                    rec.extra_values.append(token)

        # 未归入 min/typ/max 的**带标签列**（如 ≤10 secs / steady state / 自定义工况列）
        for i, role in enumerate(roles):
            if role in ("min", "typ", "max", "parameter", "symbol", "conditions", "unit"):
                continue
            cell = (row[i] or "").strip() if i < len(row) else ""
            if not cell or not _is_value(cell):
                continue
            label = ""
            if table.column_labels and i < len(table.column_labels):
                label = (table.column_labels[i] or "").strip()
            if not label:
                label = f"列{i + 1}"
            rec.value_columns.append((label, cell))

        if rec.extra_values:
            rec.extra_values = rec.extra_values[:4]

        records.append(rec)
    return records
