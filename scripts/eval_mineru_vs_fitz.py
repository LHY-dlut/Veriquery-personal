#!/usr/bin/env python3
"""A/B 对照评测：本地 PyMuPDF/Camelot 解析链路  vs  MinerU flash 解析链路。

用途
----
对同一份 PDF 分别跑两条解析链路，产出可比较的量化对照报告，用于 MinerU 接入
方案的决策（是否切换主链路、切到哪个 tier）。

两条链路
--------
* 本地链路 ``ingestion.document_processor.EnhancedDocumentProcessor.process_document``
  —— PyMuPDF(fitz) 抽文本/位图/矢量图 + Camelot/pdfplumber 抽表格。
* MinerU 链路 ``ingestion.mineru_client.MinerUClient.parse_pdf(path, tier=...)``
  —— 异步 job：upload → complete → /v1/parse/jobs → 轮询 → 取 content。
  该模块由同事并行实现；若尚未就绪，脚本优雅提示"等待 mineru_client.py"并退出，
  不会崩溃。

量化指标（全部自动判定，不依赖人眼）
-----------------------------------
1. 表格数值完整性 numeric_integrity
   - 探针正则（浮点模式）在两侧「表格文本」上的命中数。
   - 被切断迹象 split_evidence：相邻单元格形如 "-1" + "4.9"（拼回 "-14.9"）。
   - 数值覆盖率 coverage：对照 PDF 文本层（fitz words）这一独立真值源，
     统计两条链路各自能还原多少个真值数值，以及还原方式（完整 / 拼接 / 丢失）。
2. 块类型统计 block_types
   - MinerU 侧：image / table / text / equation / header / footer / page_number
     / paragraph_title 的数量分布。
   - 本地侧：位图数 / 矢量图数 / 表格数 / 文本块数（无版面分类能力）。
3. 图像产出能力 image_output
   - 本地侧：整页渲染图数量（页数×1）与每张图的面积占比（相对整页）。
   - MinerU 侧：裁剪图块数量、每块 bbox 面积占比、面积占比分布统计。
   - 派生指标：可供 VLM 消化的"局部视觉块"数量差距（本地 0 vs MinerU N）。
4. 解析耗时 latency
   - 两条链路各自的 wall clock；MinerU 额外记录服务端 parse.duration_ms。
5. 表格内容可比性 table_content
   - 从两侧表格文本抽取"参数名 → 数值"对，计算精确交集 / 各自独有项，
     以及模糊匹配（Jaccard ≥ 0.6）后的配对数量与数值一致率。
6. 多栏字符交错缺陷 interleave_defect
   - 用 PDF 文本层训练字符 bigram 语言模型；对每个长 token 计算
     "偶位/奇位拆分后得分" 相对 "原串得分" 的增益，增益超阈值即判定为交错乱码。
   - 两侧同法计算并互相作为对照组，量化 MinerU 多栏乱码的实际影响面。

用法
----
    python3 scripts/eval_mineru_vs_fitz.py <pdf_path> [--tier flash] [--json out.json]
    python3 scripts/eval_mineru_vs_fitz.py <pdf_path> --skip-mineru   # 仅跑本地链路

退出码
------
    0 成功；2 参数/环境错误（含 mineru_client.py 未就绪而无法跑 MinerU 侧）。
"""

import argparse
import asyncio
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 依赖（fitz / requests / camelot）只装在项目 venv 里；解释器不对时给出可执行的提示，
# 而不是抛一串 ModuleNotFoundError。
if sys.version_info < (3, 9):
    print(
        f"[错误] 当前解释器是 Python {sys.version.split()[0]}，本脚本需要 3.9+。\n"
        f"       请使用项目虚拟环境：{PROJECT_ROOT}/.venv/bin/python "
        f"{Path(__file__).name} ...",
        file=sys.stderr,
    )
    raise SystemExit(2)

NO_PROXIES = {"http": None, "https": None}

# ---------------------------------------------------------------------------
# 探针正则：浮点数模式（题目示例 r'(?<![d.])-d{1,2}.d(?![d])' 的严格化版本）
# ---------------------------------------------------------------------------
# 完整浮点数：可选符号 + 1~4 位整数 + 小数点 + 至少 1 位小数
FLOAT_RE = re.compile(r"(?<![\d.])[-+]?\d{1,4}\.\d+(?![\d])")
# 裸浮点数（不含符号）
BARE_FLOAT_RE = re.compile(r"(?<![\d.])\d{1,4}\.\d+(?![\d])")
# 任意数值（浮点或整数），用于数值覆盖率统计
NUM_RE = re.compile(r"(?<![\d.])[-+]?\d+(?:\.\d+)?(?![\d.])")

# 数值切断（split）片段模式：整数片段 + 小数片段，或 带尾点整数 + 整数
_SPLIT_INT_FRAG = re.compile(r"^[-+]?\d{1,3}$")
_SPLIT_DEC_FRAG = re.compile(r"^\d{1,3}\.\d{1,3}$")
_SPLIT_DOT_FRAG = re.compile(r"^[-+]?\d{1,3}\.$")
_SPLIT_PLAIN_INT = re.compile(r"^\d{1,4}$")

# 交错乱码检测参数
INTERLEAVE_MIN_TOKEN_LEN = 12
INTERLEAVE_MIN_GAIN = 0.5
# 模糊参数名匹配的 Jaccard 阈值
FUZZY_LABEL_JACCARD = 0.6


# ===========================================================================
# 通用文本工具
# ===========================================================================
def strip_html(text: str) -> str:
    """去掉 HTML 标签，保留 <sub>/<sup> 内的正文。"""
    if not text:
        return ""
    text = re.sub(r"<(sub|sup)\b[^>]*>(.*?)</\1>", r"\2", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def _norm_cell(text: str) -> str:
    return re.sub(r"\s+", "", strip_html(text or ""))


# ===========================================================================
# HTML 表格解析（标准库实现，无需 bs4/lxml）
# ===========================================================================
class _TableHTMLParser(HTMLParser):
    """把 <table> 片段解析成行/单元格列表，保留 rowspan/colspan。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[Dict[str, Any]]] = []
        self._row: Optional[List[Dict[str, Any]]] = None
        self._cell: Optional[Dict[str, Any]] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            try:
                rowspan = max(1, int(attr.get("rowspan", "1")))
            except ValueError:
                rowspan = 1
            try:
                colspan = max(1, int(attr.get("colspan", "1")))
            except ValueError:
                colspan = 1
            self._cell = {"text": [], "rowspan": rowspan, "colspan": colspan}

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell["text"])).strip()
            if self._row is not None:
                self._row.append(self._cell)
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)


def expand_html_table(html: str) -> List[List[str]]:
    """把 HTML 表格展开成矩形网格（rowspan 向下填充，colspan 横向复制）。"""
    parser = _TableHTMLParser()
    try:
        parser.feed(html)
    except Exception:
        return []

    grid: List[List[str]] = []
    carry: Dict[int, int] = {}
    for row in parser.rows:
        out: List[str] = []
        carry = {k: v for k, v in carry.items() if v > 0}
        col = 0
        for cell in row:
            while col in carry:
                out.append("")
                carry[col] -= 1
                if carry[col] <= 0:
                    del carry[col]
                col += 1
            text = re.sub(r"\s+", " ", "".join(cell["text"])).strip()
            for c in range(cell["colspan"]):
                out.append(text if c == 0 else "")
                if cell["rowspan"] > 1:
                    carry[col] = cell["rowspan"] - 1
                col += 1
        grid.append(out)
    return grid


def parse_plain_table(text: str) -> List[List[str]]:
    """解析 MinerU 的 ``` 纯文本表格（以 2 个以上空格分列）。"""
    body = text
    match = re.match(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", text, re.S)
    if match:
        body = match.group(1)
    rows = []
    for line in body.split("\n"):
        if not line.strip():
            continue
        rows.append([c.strip() for c in re.split(r"\s{2,}", line.strip())])
    return rows


def mineru_table_rows(content: str) -> List[List[str]]:
    """把 MinerU table block 的 content 统一成行/单元格结构。"""
    if not content:
        return []
    if "<td" in content or "<table" in content:
        return expand_html_table(content)
    return parse_plain_table(content)


# ===========================================================================
# 表格行解析：参数名抽取、数值抽取、切断迹象检测
# ===========================================================================
def _row_label(row: Sequence[str]) -> str:
    """从一行里抽出参数名（连续的、无数字的字母开头片段拼接）。"""
    parts: List[str] = []
    for cell in row:
        text = (cell or "").strip()
        if not text:
            if parts:
                break
            continue
        if re.search(r"\d", text):
            break
        compact = re.sub(r"\s+", "", text)
        if not compact or not re.match(r"^[A-Za-z]", compact):
            break
        parts.append(compact)
        if sum(len(p) for p in parts) > 70:
            break
    return "".join(parts)


def find_row_splits(row: Sequence[str]) -> List[Tuple[int, str, str]]:
    """检测一行内相邻单元格被列边界切碎的浮点数。

    返回 [(cell_index, 拼回的数值, 类型)]，例如 (7, "-14.9", "int+dec")。
    """
    found: List[Tuple[int, str, str]] = []
    for i in range(len(row) - 1):
        a = _norm_cell(row[i] or "")
        b = _norm_cell(row[i + 1] or "")
        if not a or not b:
            continue
        merged = a + b
        if _SPLIT_INT_FRAG.match(a) and _SPLIT_DEC_FRAG.match(b) and FLOAT_RE.fullmatch(merged):
            found.append((i, merged, "int+dec"))
        elif _SPLIT_DOT_FRAG.match(a) and _SPLIT_PLAIN_INT.match(b) and FLOAT_RE.fullmatch(merged):
            found.append((i, merged, "dot+int"))
    return found


def row_values(row: Sequence[str]) -> Tuple[List[str], List[str]]:
    """抽取一行里的所有数值。

    返回 (完整数值列表, 由相邻单元格拼接才能还原的数值列表)。
    """
    splits = find_row_splits(row)
    split_at = {idx: value for idx, value, _ in splits}
    split_idx = set(split_at)

    intact: List[str] = []
    restored: List[str] = []
    i = 0
    while i < len(row):
        if i in split_idx:
            restored.append(split_at[i])
            i += 2
            continue
        cell = row[i] or ""
        for match in NUM_RE.finditer(cell):
            intact.append(match.group(0))
        i += 1
    return intact, restored


def extract_param_value_pairs(rows: Iterable[Sequence[str]]) -> Dict[str, set]:
    """把表格行转成 {参数名: {数值...}} 字典。"""
    pairs: Dict[str, set] = defaultdict(set)
    for row in rows:
        label = _row_label(row)
        intact, restored = row_values(row)
        values = set(intact) | set(restored)
        if label and values:
            pairs[label].update(values)
    return dict(pairs)


def jaccard(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z]+", a))
    tb = set(re.findall(r"[a-z]+", b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ===========================================================================
# 多栏字符交错（interleave）检测
# ===========================================================================
class BigramLM:
    """字符 bigram 语言模型；用于判定一个 token 是否是"两路文本交错"的产物。"""

    def __init__(self, corpus: str) -> None:
        text = corpus.lower()
        self.bigrams: Counter = Counter()
        self.unigrams: Counter = Counter()
        for ch in text:
            self.unigrams[ch] += 1
        for i in range(len(text) - 1):
            self.bigrams[text[i : i + 2]] += 1
        self._n2 = max(1, sum(self.bigrams.values()))
        self._n1 = max(1, sum(self.unigrams.values()))

    def score(self, text: str) -> float:
        """平均对数概率；越接近 0 越像自然文本。"""
        s = re.sub(r"\s+", " ", text.lower())
        if len(s) < 4:
            return -99.0
        total = 0.0
        for i in range(len(s) - 1):
            gram = s[i : i + 2]
            p_bi = (self.bigrams[gram] + 0.3) / (self._n2 + 0.3 * max(1, len(self.bigrams)))
            p_uni = (self.unigrams[gram[0]] + 0.3) / (self._n1 + 0.3 * max(1, len(self.unigrams)))
            total += math.log(max(p_bi * p_uni, 1e-12))
        return total / (len(s) - 1)

    def interleave_gain(self, token: str) -> float:
        """交错增益：偶位/奇位拆分后更"像英文"的幅度。

        正常文本增益为负；交错乱码增益显著为正。
        """
        if len(token) < INTERLEAVE_MIN_TOKEN_LEN or not token.isalpha():
            return -99.0
        base = self.score(token)
        even = self.score(token[0::2])
        odd = self.score(token[1::2])
        return max(even, odd) - base


def detect_interleave(
    tokens: Iterable[str],
    lm: BigramLM,
    vocabulary: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """找出疑似交错乱码的 token。

    vocabulary 是 PDF 文本层里的正常词表。命中 token 若本身就是文档里
    出现过的正常单词（例如 authorization），则不是交错产物，直接排除 ——
    这能把"偶位/奇位拆分恰好也像英文"的假阳性滤掉。
    """
    hits: List[Dict[str, Any]] = []
    for token in tokens:
        if vocabulary is not None and token.lower() in vocabulary:
            continue
        gain = lm.interleave_gain(token)
        if gain >= INTERLEAVE_MIN_GAIN:
            hits.append({"token": token, "gain": round(gain, 3)})
    return hits


# ===========================================================================
# PDF 文本层真值（独立第三方参照）
# ===========================================================================
def pdf_text_layer_truth(pdf_path: Path) -> Dict[str, Any]:
    """从 PDF 文本层抽取真值数值集合与字符 bigram 训练语料。

    文本层与两条链路都独立，因此可以作为"数值覆盖率"的中立参照。
    """
    import fitz

    doc = fitz.open(str(pdf_path))
    try:
        corpus_parts: List[str] = []
        numbers: set = set()
        vocabulary: set = set()
        page_numbers: Dict[int, set] = defaultdict(set)
        for page_idx in range(doc.page_count):
            page = doc.load_page(page_idx)
            text = page.get_text()
            corpus_parts.append(text)
            vocabulary.update(w.lower() for w in re.findall(r"[A-Za-z]{3,}", text))
            for word in page.get_text("words"):
                for match in NUM_RE.finditer(word[4]):
                    numbers.add(match.group(0))
                    page_numbers[page_idx + 1].add(match.group(0))
        corpus = "\n".join(corpus_parts)
    finally:
        doc.close()

    return {
        "numbers": numbers,
        "page_numbers": {k: v for k, v in page_numbers.items()},
        "corpus": corpus,
        "lm": BigramLM(corpus),
        "vocabulary": vocabulary,
    }


# ===========================================================================
# 本地链路（PyMuPDF + Camelot/pdfplumber）
# ===========================================================================
def run_local_chain(pdf_path: Path) -> Dict[str, Any]:
    """跑本地 EnhancedDocumentProcessor.process_document，返回结构化指标。"""
    from ingestion.document_processor import EnhancedDocumentProcessor

    processor = EnhancedDocumentProcessor()
    started = time.perf_counter()
    result = asyncio.run(
        processor.process_document(
            str(pdf_path),
            options={"enable_colpali": False, "require_visual_embeddings": False},
        )
    )
    elapsed = time.perf_counter() - started

    pages = result.get("processed_pages", [])
    tables = result.get("extracted_tables", [])

    # --- 表格 ---
    table_rows: List[List[str]] = []
    table_pages: set = set()
    table_numbers_by_page: Dict[int, set] = defaultdict(set)
    for table in tables:
        page_no = table.get("page")
        page_key = int(page_no) if page_no else 0
        if page_no:
            table_pages.add(page_key)
        for row in table.get("data", []) or []:
            cells = [str(c) if c is not None else "" for c in row]
            table_rows.append(cells)
            i_vals, r_vals = row_values(cells)
            table_numbers_by_page[page_key].update(i_vals)
            table_numbers_by_page[page_key].update(r_vals)

    table_text = "\n".join(" | ".join(r) for r in table_rows)
    intact, restored = [], []
    for row in table_rows:
        i_vals, r_vals = row_values(row)
        intact.extend(i_vals)
        restored.extend(r_vals)

    # --- 图像 ---
    bitmap_count = 0
    vector_count = 0
    for page in pages:
        for image in getattr(page, "images", []) or []:
            if image.get("source") == "pymupdf_bitmap":
                bitmap_count += 1
            elif image.get("source") == "pymupdf_drawing":
                vector_count += 1

    # 本地侧"整页渲染图"：每个 page 最多一张 PIL 图（页数×1）
    page_render_count = 0
    for page in pages:
        if getattr(page, "_pil_image", None) is not None:
            page_render_count += 1

    # 页面文本 token（用于交错乱码检测的本地侧样本）
    page_text_tokens: List[str] = []
    for page in pages:
        page_text_tokens.extend(re.findall(r"[A-Za-z]{%d,}" % INTERLEAVE_MIN_TOKEN_LEN, page.text or ""))

    return {
        "elapsed_sec": round(elapsed, 3),
        "page_count": len(pages),
        "table_count": len(tables),
        "table_pages": sorted(table_pages),
        "table_numbers_by_page": {k: sorted(v) for k, v in table_numbers_by_page.items()},
        "table_rows": table_rows,
        "table_text": table_text,
        "table_pairs": extract_param_value_pairs(table_rows),
        "numbers_intact": intact,
        "numbers_split_restored": restored,
        "block_types": {
            "page": len(pages),
            "bitmap_image": bitmap_count,
            "vector_drawing": vector_count,
            "table": len(tables),
            "text_block": len(pages),
            "equation": 0,
            "header": 0,
            "footer": 0,
            "page_number": 0,
            "paragraph_title": 0,
        },
        "image_output": {
            "page_render_count": page_render_count,
            "localized_figure_count": 0,
            "page_render_area_ratio": 1.0,
            "note": "本地链路只有整页渲染（页数×1），无 bbox 裁剪图块",
        },
        "text_tokens": page_text_tokens,
        "raw_result": result,
    }


# ===========================================================================
# MinerU 链路
# ===========================================================================
def load_mineru_client():
    """导入同事实现的 ingestion.mineru_client.MinerUClient。

    未就绪时返回 (None, 提示信息)，由调用方优雅处理。
    """
    try:
        from ingestion.mineru_client import MinerUClient  # type: ignore
    except Exception as exc:  # ModuleNotFoundError 或其它导入错误
        return None, (
            f"等待 mineru_client.py：未找到 ingestion.mineru_client "
            f"({type(exc).__name__}: {exc})。"
        )
    return MinerUClient, None


def run_mineru_chain(pdf_path: Path, tier: str, client_cls) -> Dict[str, Any]:
    """跑 MinerUClient.parse_pdf，返回结构化指标。

    structured_content 是服务端原始块结构（权威来源），客户端另外给出
    已解析好的 images / tables 列表，作为交叉校验与兜底。
    """
    started = time.perf_counter()
    client = client_cls()
    parse_result = client.parse_pdf(str(pdf_path), tier=tier)
    if asyncio.iscoroutine(parse_result):
        parse_result = asyncio.run(parse_result)
    elapsed = time.perf_counter() - started

    structured = parse_result.get("structured_content") or {}
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except json.JSONDecodeError:
            structured = {}

    pages = structured.get("pages", []) or []

    block_counter: Counter = Counter()
    table_rows: List[List[str]] = []
    table_pages: set = set()
    table_numbers_by_page: Dict[int, set] = defaultdict(set)
    image_blocks: List[Dict[str, Any]] = []
    text_tokens: List[str] = []

    for page in pages:
        page_no = int(page.get("page_idx", 0)) + 1
        for block in page.get("blocks", []) or []:
            btype = block.get("type", "unknown")
            block_counter[btype] += 1
            content = block.get("content") or ""

            if btype == "table":
                table_pages.add(page_no)
                for row in mineru_table_rows(content):
                    cells = [str(c) if c is not None else "" for c in row]
                    table_rows.append(cells)
                    i_vals, r_vals = row_values(cells)
                    table_numbers_by_page[page_no].update(i_vals)
                    table_numbers_by_page[page_no].update(r_vals)
                    # 表格单元格里的文本同样可能是交错乱码，需要纳入检测样本
                    text_tokens.extend(
                        re.findall(
                            r"[A-Za-z]{%d,}" % INTERLEAVE_MIN_TOKEN_LEN,
                            strip_html(" ".join(row)),
                        )
                    )
            elif btype == "image":
                bbox = block.get("bbox") or [0, 0, 0, 0]
                try:
                    x0, y0, x1, y1 = (float(v) for v in bbox)
                except (TypeError, ValueError):
                    x0 = y0 = x1 = y1 = 0.0
                image_blocks.append(
                    {
                        "page": page_no,
                        "bbox": [x0, y0, x1, y1],
                        "area_ratio": round(max(0.0, (x1 - x0)) * max(0.0, (y1 - y0)), 6),
                        "has_caption": bool(block.get("captions")),
                        "has_image_source": bool(block.get("image_source")),
                    }
                )
            else:
                text_tokens.extend(
                    re.findall(r"[A-Za-z]{%d,}" % INTERLEAVE_MIN_TOKEN_LEN, strip_html(content))
                )

    # 客户端已解析的 images / tables（与服务端原始结构交叉校验）
    client_image_count = len(parse_result.get("images") or [])
    client_table_count = len(parse_result.get("tables") or [])
    if not image_blocks and client_image_count:
        for item in parse_result.get("images") or []:
            bbox = item.get("bbox") or [0, 0, 0, 0]
            try:
                x0, y0, x1, y1 = (float(v) for v in bbox)
            except (TypeError, ValueError):
                x0 = y0 = x1 = y1 = 0.0
            image_blocks.append(
                {
                    "page": int(item.get("page_number") or 0),
                    "bbox": [x0, y0, x1, y1],
                    "area_ratio": round(max(0.0, (x1 - x0)) * max(0.0, (y1 - y0)), 6),
                    "has_caption": bool(item.get("captions")),
                    "has_image_source": bool(item.get("image_source")),
                }
            )
    if not table_rows and client_table_count:
        for item in parse_result.get("tables") or []:
            table_pages.add(int(item.get("page_number") or 0))
            for row in item.get("rows") or []:
                table_rows.append([str(c) if c is not None else "" for c in row])

    table_text = "\n".join(" | ".join(r) for r in table_rows)
    intact, restored = [], []
    for row in table_rows:
        i_vals, r_vals = row_values(row)
        intact.extend(i_vals)
        restored.extend(r_vals)

    areas = sorted(b["area_ratio"] for b in image_blocks)

    def _pct(q: float) -> float:
        if not areas:
            return 0.0
        idx = min(len(areas) - 1, max(0, int(round(q * (len(areas) - 1)))))
        return round(areas[idx], 6)

    return {
        "elapsed_sec": round(elapsed, 3),
        "tier": tier,
        "page_count": len(pages),
        "table_count": block_counter.get("table", 0),
        "table_pages": sorted(table_pages),
        "table_numbers_by_page": {k: sorted(v) for k, v in table_numbers_by_page.items()},
        "table_rows": table_rows,
        "table_text": table_text,
        "table_pairs": extract_param_value_pairs(table_rows),
        "numbers_intact": intact,
        "numbers_split_restored": restored,
        "block_types": {
            "page": len(pages),
            "bitmap_image": 0,
            "vector_drawing": 0,
            "table": block_counter.get("table", 0),
            "text_block": block_counter.get("text", 0),
            "equation": block_counter.get("equation", 0),
            "header": block_counter.get("header", 0),
            "footer": block_counter.get("footer", 0),
            "page_number": block_counter.get("page_number", 0),
            "paragraph_title": block_counter.get("paragraph_title", 0),
            "image": block_counter.get("image", 0),
        },
        "image_output": {
            "page_render_count": 0,
            "localized_figure_count": len(image_blocks),
            "area_ratio_min": _pct(0.0),
            "area_ratio_median": _pct(0.5),
            "area_ratio_mean": round(sum(areas) / len(areas), 6) if areas else 0.0,
            "area_ratio_max": _pct(1.0),
            "blocks": image_blocks,
            "note": "每个 image block 均为带 bbox 的独立裁剪图块",
        },
        "text_tokens": text_tokens,
        "server_parse_ms": _extract_server_duration(parse_result),
        "client_image_count": client_image_count,
        "client_table_count": client_table_count,
    }


def _extract_server_duration(parse_result: Dict[str, Any]) -> Optional[int]:
    """从 MinerU 返回值里尽力挖服务端 parse.duration_ms。

    客户端把服务端解析耗时放在 meta.duration_ms；这里同时兼容裸 job 结构。
    """
    meta = parse_result.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("duration_ms"), (int, float)):
        return int(meta["duration_ms"])

    for key in ("job", "result", "parse"):
        node = parse_result.get(key)
        if isinstance(node, dict):
            ms = node.get("duration_ms")
            if isinstance(ms, (int, float)):
                return int(ms)
            files = node.get("files")
            if isinstance(files, list):
                for entry in files:
                    parse_meta = entry.get("parse") if isinstance(entry, dict) else None
                    if isinstance(parse_meta, dict) and isinstance(parse_meta.get("duration_ms"), (int, float)):
                        return int(parse_meta["duration_ms"])
    for entry in parse_result.get("files", []) or []:
        if isinstance(entry, dict):
            parse_meta = entry.get("parse")
            if isinstance(parse_meta, dict) and isinstance(parse_meta.get("duration_ms"), (int, float)):
                return int(parse_meta["duration_ms"])
    return None


# ===========================================================================
# 指标计算
# ===========================================================================
def numeric_integrity_metrics(side: Dict[str, Any], truth: Dict[str, Any]) -> Dict[str, Any]:
    """表格数值完整性：探针命中 + 切断迹象 + 对照文本层真值的覆盖率。

    覆盖率同时给出两个口径：
    - ``truth_coverage_pct``：全文档口径。本地链路在图表页也会误检出"表格"，
      数字来自曲线坐标轴，所以这个口径会系统性偏向本地。
    - ``truth_coverage_scoped_pct``：只看两条链路都检出表格的页面（common_pages），
      这是真正同题比较的口径。
    """
    table_text = side["table_text"]
    intact = set(side["numbers_intact"])
    restored = set(side["numbers_split_restored"])

    probe_signed = FLOAT_RE.findall(table_text)
    probe_bare = BARE_FLOAT_RE.findall(table_text)

    truth_numbers = truth["numbers"]
    covered = truth_numbers & (intact | restored)
    intact_hits = truth_numbers & intact
    restored_hits = truth_numbers & restored
    missing = truth_numbers - (intact | restored)

    # --- 同题口径：只在双方都检出表格的页面上比 ---
    common_pages = side.get("common_table_pages") or []
    scoped_truth: set = set()
    for page_no in common_pages:
        scoped_truth |= truth["page_numbers"].get(page_no, set())
    scoped_covered = scoped_truth & (intact | restored)
    scoped_missing = scoped_truth - (intact | restored)
    # 无需拼接即可命中的真值：这是"表格能否直接进 context / 直接检索"的关键口径
    scoped_intact = scoped_truth & intact
    scoped_restored = scoped_truth & restored

    return {
        "float_probe_signed": len(probe_signed),
        "float_probe_bare": len(probe_bare),
        "float_probe_signed_values": sorted(set(probe_signed))[:40],
        "numeric_tokens_total": len(intact) + len(restored),
        "numeric_tokens_intact": len(intact),
        "numeric_tokens_split_restored": len(restored),
        "split_evidence_count": len(restored),
        "split_evidence_values": sorted(restored)[:40],
        "truth_numbers_total": len(truth_numbers),
        "truth_covered": len(covered),
        "truth_coverage_pct": round(100.0 * len(covered) / len(truth_numbers), 2) if truth_numbers else 0.0,
        "truth_intact_hits": len(intact_hits),
        "truth_restored_only": len(restored_hits),
        "truth_missing": len(missing),
        "truth_missing_examples": sorted(missing)[:20],
        "scoped_pages": list(common_pages),
        "scoped_truth_total": len(scoped_truth),
        "scoped_truth_covered": len(scoped_covered),
        "scoped_truth_coverage_pct": (
            round(100.0 * len(scoped_covered) / len(scoped_truth), 2) if scoped_truth else 0.0
        ),
        "scoped_truth_intact": len(scoped_intact),
        "scoped_truth_restored_only": len(scoped_restored),
        "scoped_truth_intact_pct": (
            round(100.0 * len(scoped_intact) / len(scoped_truth), 2) if scoped_truth else 0.0
        ),
        "scoped_truth_missing": len(scoped_missing),
        "scoped_truth_missing_examples": sorted(scoped_missing)[:20],
    }


def table_page_agreement(local: Dict[str, Any], mineru: Dict[str, Any]) -> Dict[str, Any]:
    """表格页一致性：双方检出表格的页面是否重合。

    本地 pdfplumber 在纯图表页（时序曲线/波形图）也会误检出"表格"，
    这些表格里的数字其实来自坐标轴刻度，不是真实参数表。
    这里把「只有本地检出表格」的页面单列出来，作为误检规模的下界。
    """
    local_pages = set(local["table_pages"])
    mineru_pages = set(mineru["table_pages"])
    common = sorted(local_pages & mineru_pages)
    local_only = sorted(local_pages - mineru_pages)
    mineru_only = sorted(mineru_pages - local_pages)

    # 误检页里本地表格捞到的数字，有多少是 PDF 文本层里真实存在的坐标轴刻度
    spurious_digits = 0
    for page_no in local_only:
        spurious_digits += len(local.get("table_numbers_by_page", {}).get(page_no, set()))

    return {
        "local_table_pages": sorted(local_pages),
        "mineru_table_pages": sorted(mineru_pages),
        "common_pages": common,
        "local_only_pages": local_only,
        "mineru_only_pages": mineru_only,
        "local_only_page_count": len(local_only),
        "mineru_only_page_count": len(mineru_only),
        "spurious_page_numeric_tokens": spurious_digits,
        "note": "仅一方检出表格的页面：本地侧多为图表页误检的坐标轴数字",
    }


def table_content_metrics(local: Dict[str, Any], mineru: Dict[str, Any]) -> Dict[str, Any]:
    """参数名 → 数值 对的可比性。"""
    lp = local["table_pairs"]
    mp = mineru["table_pairs"]

    exact_common = sorted(set(lp) & set(mp))
    local_only = sorted(set(lp) - set(mp))
    mineru_only = sorted(set(mp) - set(lp))

    # 模糊匹配：Jaccard(字母词集合) >= 阈值
    fuzzy_pairs: List[Dict[str, Any]] = []
    for lname, lvals in lp.items():
        best_score, best_name = 0.0, None
        for mname in mp:
            score = jaccard(lname, mname)
            if score > best_score:
                best_score, best_name = score, mname
        if best_name is not None and best_score >= FUZZY_LABEL_JACCARD:
            fuzzy_pairs.append(
                {
                    "local_label": lname,
                    "mineru_label": best_name,
                    "jaccard": round(best_score, 3),
                    "local_values": sorted(lvals),
                    "mineru_values": sorted(mp[best_name]),
                    "shared_values": sorted(lvals & mp[best_name]),
                }
            )

    value_agree = 0
    value_total = 0
    for pair in fuzzy_pairs:
        for value in pair["mineru_values"]:
            value_total += 1
            if value in pair["local_values"]:
                value_agree += 1

    return {
        "local_param_count": len(lp),
        "mineru_param_count": len(mp),
        "exact_common_count": len(exact_common),
        "exact_common_labels": exact_common[:30],
        "local_only_count": len(local_only),
        "local_only_labels": local_only[:30],
        "mineru_only_count": len(mineru_only),
        "mineru_only_labels": mineru_only[:30],
        "fuzzy_matched_count": len(fuzzy_pairs),
        "fuzzy_matched": fuzzy_pairs[:20],
        "fuzzy_value_agreement": (
            round(value_agree / value_total, 4) if value_total else None
        ),
        "fuzzy_value_agree_num": value_agree,
        "fuzzy_value_total_num": value_total,
    }


def build_conclusion(
    local: Dict[str, Any],
    mineru: Dict[str, Any],
    local_num: Dict[str, Any],
    mineru_num: Dict[str, Any],
    content: Dict[str, Any],
    agreement: Dict[str, Any],
    local_inter: List[Dict[str, Any]],
    mineru_inter: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """逐指标给出"哪条链路更好"的直接结论。"""
    verdicts: List[Dict[str, str]] = []

    def add(metric: str, winner: str, detail: str) -> None:
        verdicts.append({"metric": metric, "winner": winner, "detail": detail})

    # 1. 表格数值可直接使用的比例（同题口径：双方都检出表格的页面）
    #    这一口径比"总覆盖率"更贴近实用价值：无需拼接即可命中的真值才算数，
    #    因为表格要直接进 LLM context / 参与向量检索。
    l_intact = local_num["scoped_truth_intact_pct"]
    m_intact = mineru_num["scoped_truth_intact_pct"]
    if l_intact > m_intact:
        add(
            "表格数值可直接使用率（同页同题）",
            "本地 fitz/camelot",
            f"无需拼接即命中 {l_intact}%（{local_num['scoped_truth_intact']}/{local_num['scoped_truth_total']}）"
            f" > MinerU {m_intact}%",
        )
    elif m_intact > l_intact:
        add(
            "表格数值可直接使用率（同页同题）",
            "MinerU",
            f"无需拼接即命中 {m_intact}%（{mineru_num['scoped_truth_intact']}/{mineru_num['scoped_truth_total']}）"
            f" > 本地 {l_intact}%",
        )
    else:
        add(
            "表格数值可直接使用率（同页同题）",
            "持平",
            f"两者均为 {l_intact}%",
        )

    # 1a. 补充：含拼接还原的总覆盖率（更宽松口径）
    l_scoped = local_num["scoped_truth_coverage_pct"]
    m_scoped = mineru_num["scoped_truth_coverage_pct"]
    if l_scoped > m_scoped:
        add(
            "表格数值总覆盖率（含拼接还原）",
            "本地 fitz/camelot",
            f"{l_scoped}% > MinerU {m_scoped}%（页 {local_num['scoped_pages']}）",
        )
    elif m_scoped > l_scoped:
        add(
            "表格数值总覆盖率（含拼接还原）",
            "MinerU",
            f"{m_scoped}% > 本地 {l_scoped}%（页 {local_num['scoped_pages']}）",
        )
    else:
        add("表格数值总覆盖率（含拼接还原）", "持平", f"两者均为 {l_scoped}%")

    # 1b. 数值被列边界切断
    if local_num["split_evidence_count"] > 0 and mineru_num["split_evidence_count"] == 0:
        add(
            "数值被列边界切断",
            "MinerU",
            f"本地出现 {local_num['split_evidence_count']} 处切断"
            f"（例：{', '.join(local_num['split_evidence_values'][:3])}），MinerU 0 处",
        )
    elif mineru_num["split_evidence_count"] > local_num["split_evidence_count"]:
        add(
            "数值被列边界切断",
            "本地 fitz/camelot",
            f"MinerU {mineru_num['split_evidence_count']} 处 > 本地 {local_num['split_evidence_count']} 处",
        )
    else:
        add("数值被列边界切断", "持平", "两侧均未检出切断")

    # 1c. 表格误检
    if agreement["local_only_page_count"] > 0:
        add(
            "表格误检（图表页当表格）",
            "MinerU",
            f"本地在 {agreement['local_only_page_count']} 页额外检出表格"
            f"（页 {agreement['local_only_pages']}），MinerU 未检出",
        )
    elif agreement["mineru_only_page_count"] > 0:
        add(
            "表格漏检",
            "本地 fitz/camelot",
            f"MinerU 在 {agreement['mineru_only_page_count']} 页检出表格而本地漏掉",
        )
    else:
        add("表格页一致性", "持平", "双方表格页完全一致")

    # 2. 局部视觉块
    local_fig = local["image_output"]["localized_figure_count"]
    mineru_fig = mineru["image_output"]["localized_figure_count"]
    if mineru_fig > local_fig:
        add(
            "可供 VLM 消化的局部视觉块",
            "MinerU",
            f"{mineru_fig} 个带 bbox 裁剪图块 vs 本地 {local_fig} 个"
            f"（本地仅 {local['image_output']['page_render_count']} 张整页图）",
        )
    else:
        add("可供 VLM 消化的局部视觉块", "本地 fitz/camelot", f"{local_fig} vs {mineru_fig}")

    # 3. 版面分类能力
    local_layout = sum(
        local["block_types"].get(k, 0)
        for k in ("header", "footer", "page_number", "paragraph_title", "equation")
    )
    mineru_layout = sum(
        mineru["block_types"].get(k, 0)
        for k in ("header", "footer", "page_number", "paragraph_title", "equation")
    )
    if mineru_layout > local_layout:
        add(
            "版面分类粒度",
            "MinerU",
            f"{mineru_layout} 个版面类块 vs 本地 0（本地仅有整页文本流）",
        )
    else:
        add("版面分类粒度", "持平", "两侧版面块数量相当")

    # 4. 延迟
    if mineru["elapsed_sec"] < local["elapsed_sec"]:
        add(
            "解析耗时",
            "MinerU",
            f"{mineru['elapsed_sec']}s vs 本地 {local['elapsed_sec']}s"
            + (
                f"（服务端 parse {mineru.get('server_parse_ms')}ms）"
                if mineru.get("server_parse_ms")
                else ""
            ),
        )
    else:
        add(
            "解析耗时",
            "本地 fitz/camelot",
            f"{local['elapsed_sec']}s vs MinerU {mineru['elapsed_sec']}s",
        )

    # 5. 参数→数值 可比性
    if content["fuzzy_matched_count"] > 0:
        add(
            "表格参数覆盖（模糊匹配）",
            "双方",
            f"{content['fuzzy_matched_count']} 个参数名可对应；"
            f"其中 MinerU 侧数值有 {content['fuzzy_value_agreement']} 比例能在本地侧找到同值",
        )
    else:
        add("表格参数覆盖（模糊匹配）", "无", "两侧未找到可对应的参数名")

    # 6. 多栏交错缺陷
    if mineru_inter and not local_inter:
        add(
            "多栏字符交错缺陷",
            "本地 fitz/camelot",
            f"MinerU 检出 {len(mineru_inter)} 个交错乱码 token，本地 0 个",
        )
    elif mineru_inter:
        add(
            "多栏字符交错缺陷",
            "本地 fitz/camelot" if len(local_inter) < len(mineru_inter) else "MinerU",
            f"MinerU {len(mineru_inter)} 个 / 本地 {len(local_inter)} 个交错乱码 token",
        )
    else:
        add("多栏字符交错缺陷", "未检出", "两侧均未检出交错乱码")

    return {"verdicts": verdicts}


# ===========================================================================
# 报告渲染
# ===========================================================================
def render_report(
    pdf_path: Path,
    tier: str,
    local: Optional[Dict[str, Any]],
    mineru: Optional[Dict[str, Any]],
    local_num: Optional[Dict[str, Any]],
    mineru_num: Optional[Dict[str, Any]],
    content: Optional[Dict[str, Any]],
    agreement: Optional[Dict[str, Any]],
    local_inter: List[Dict[str, Any]],
    mineru_inter: List[Dict[str, Any]],
    conclusion: Dict[str, Any],
) -> str:
    lines: List[str] = []
    add = lines.append

    add("=" * 78)
    add(f"MinerU vs 本地 fitz/camelot  A/B 对照报告   tier={tier}")
    add(f"PDF: {pdf_path}")
    add("=" * 78)

    if local:
        add("")
        add("【1】解析耗时 / 规模")
        add(f"  本地 fitz+camelot : {local['elapsed_sec']:>8.3f}s   页数={local['page_count']}  表格={local['table_count']}")
        if mineru:
            extra = f"  服务端parse={mineru.get('server_parse_ms')}ms" if mineru.get("server_parse_ms") else ""
            add(f"  MinerU {tier:<10} : {mineru['elapsed_sec']:>8.3f}s   页数={mineru['page_count']}  表格={mineru['table_count']}{extra}")

    if local and mineru:
        add("")
        add("【2】表格数值完整性（探针正则 + 切断迹象 + 对照 PDF 文本层真值）")
        add(f"  探针：FLOAT_RE = {FLOAT_RE.pattern}")
        add(f"  {'':<28}{'本地 fitz/camelot':>22}{'MinerU':>22}")
        rows = [
            ("浮点探针命中(带符号)", "float_probe_signed"),
            ("浮点探针命中(裸浮点)", "float_probe_bare"),
            ("数值token总数", "numeric_tokens_total"),
            ("其中完整未切断", "numeric_tokens_intact"),
            ("其中需拼接还原", "numeric_tokens_split_restored"),
            ("被切断迹象次数", "split_evidence_count"),
            ("真值数值总数(全文档)", "truth_numbers_total"),
            ("真值覆盖率(全文档,%)", "truth_coverage_pct"),
            ("真值仍丢失(全文档)", "truth_missing"),
            ("--- 同页同题口径 ---", None),
            ("同题页码", "scoped_pages"),
            ("同题真值总数", "scoped_truth_total"),
            ("同题真值覆盖率(%)", "scoped_truth_coverage_pct"),
            ("同题: 无需拼接即命中", "scoped_truth_intact"),
            ("同题: 仅靠拼接才还原", "scoped_truth_restored_only"),
            ("同题: 直接可用率(%)", "scoped_truth_intact_pct"),
            ("同题真值仍丢失", "scoped_truth_missing"),
        ]
        for label, key in rows:
            if key is None:
                add(f"  {label}")
                continue
            lv, mv = local_num[key], mineru_num[key]
            if isinstance(lv, list):
                add(f"  {label:<28}{str(lv):>22}{str(mv):>22}")
            else:
                add(f"  {label:<28}{lv:>22}{mv:>22}")
        if local_num["split_evidence_values"]:
            add(f"  本地被切断的数值样例: {', '.join(local_num['split_evidence_values'][:6])}")
        if mineru_num["split_evidence_values"]:
            add(f"  MinerU被切断的数值样例: {', '.join(mineru_num['split_evidence_values'][:6])}")
        add(f"  本地同题真值丢失样例: {', '.join(local_num['scoped_truth_missing_examples'][:8]) or '无'}")

    if agreement:
        add("")
        add("【2b】表格页一致性（检测图表页误检为表格）")
        add(f"  本地表格页  : {agreement['local_table_pages']}")
        add(f"  MinerU表格页: {agreement['mineru_table_pages']}")
        add(f"  双方共有    : {agreement['common_pages']}")
        add(f"  仅本地检出  : {agreement['local_only_pages']}"
            f"（这些页的数字多为曲线坐标轴刻度，非真实参数表）")
        add(f"  仅MinerU检出: {agreement['mineru_only_pages']}")

    if local and mineru:
        add("")
        add("【3】块类型统计")
        keys = [
            "page",
            "text_block",
            "table",
            "image",
            "bitmap_image",
            "vector_drawing",
            "equation",
            "header",
            "footer",
            "page_number",
            "paragraph_title",
        ]
        add(f"  {'':<20}{'本地':>12}{'MinerU':>12}")
        for key in keys:
            add(f"  {key:<20}{local['block_types'].get(key, 0):>12}{mineru['block_types'].get(key, 0):>12}")

        add("")
        add("【4】图像产出能力")
        li = local["image_output"]
        mi = mineru["image_output"]
        add(f"  本地：整页渲染图 {li['page_render_count']} 张（页数×1），"
            f"单张占整页面积 {li['page_render_area_ratio']:.0%}，无 bbox 裁剪块")
        add(f"  MinerU：裁剪图块 {mi['localized_figure_count']} 个（全部带 bbox）")
        if mi["localized_figure_count"]:
            add(f"          bbox 面积占比  min={mi['area_ratio_min']:.4f} "
                f"median={mi['area_ratio_median']:.4f} mean={mi['area_ratio_mean']:.4f} "
                f"max={mi['area_ratio_max']:.4f}")
        add(f"  → 局部视觉块数量差：本地 {li['localized_figure_count']} vs "
            f"MinerU {mi['localized_figure_count']}")

    if content:
        add("")
        add("【5】表格内容可比性（参数名 → 数值）")
        add(f"  参数名数量         本地={content['local_param_count']}  "
            f"MinerU={content['mineru_param_count']}")
        add(f"  精确交集           {content['exact_common_count']}  "
            f"（本地独有 {content['local_only_count']} / MinerU独有 {content['mineru_only_count']}）")
        add(f"  模糊匹配(Jaccard>={FUZZY_LABEL_JACCARD})  {content['fuzzy_matched_count']} 对")
        if content["fuzzy_value_total_num"]:
            add(f"  匹配对中 MinerU 数值能在本地找到同值的比例: "
                f"{content['fuzzy_value_agreement']} "
                f"({content['fuzzy_value_agree_num']}/{content['fuzzy_value_total_num']})")
        for pair in content["fuzzy_matched"][:6]:
            add(f"    - {pair['local_label']} → {pair['mineru_label']}  "
                f"本地={pair['local_values'][:5]} MinerU={pair['mineru_values'][:5]}")

    add("")
    add("【6】多栏字符交错缺陷（同法检测，互为对照）")
    if local:
        add(f"  本地 fitz/camelot : 检出 {len(local_inter)} 个交错乱码 token")
        for hit in local_inter[:5]:
            add(f"      - {hit['token']}  (gain={hit['gain']})")
    else:
        add("  本地 fitz/camelot : 未运行")
    if mineru:
        add(f"  MinerU {tier:<10} : 检出 {len(mineru_inter)} 个交错乱码 token")
        for hit in mineru_inter[:5]:
            add(f"      - {hit['token']}  (gain={hit['gain']})")
    else:
        add(f"  MinerU {tier:<10} : 未运行")

    add("")
    add("【7】结论：哪个指标上哪条链路更好")
    for verdict in conclusion["verdicts"]:
        add(f"  · {verdict['metric']:<24} → {verdict['winner']:<20} {verdict['detail']}")

    add("")
    add("=" * 78)
    return "\n".join(lines)


# ===========================================================================
# main
# ===========================================================================
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="MinerU 与本地 PyMuPDF/Camelot 解析链路的 A/B 对照评测",
    )
    parser.add_argument("pdf_path", help="待评测的 PDF 路径")
    parser.add_argument("--tier", default="flash", help="MinerU 解析 tier（默认 flash）")
    parser.add_argument("--json", dest="json_out", default=None, help="机器可读报告输出路径")
    parser.add_argument("--skip-local", action="store_true", help="跳过本地链路")
    parser.add_argument("--skip-mineru", action="store_true", help="跳过 MinerU 链路")
    args = parser.parse_args(argv)

    pdf_path = Path(args.pdf_path).expanduser().resolve()
    if not pdf_path.is_file():
        print(f"[错误] PDF 不存在: {pdf_path}", file=sys.stderr)
        return 2
    if not pdf_path.suffix.lower() == ".pdf":
        print(f"[警告] 文件不是 .pdf: {pdf_path}", file=sys.stderr)

    # MinerU 客户端：未就绪时优雅提示
    mineru_client_cls = None
    mineru_wait_msg = None
    if not args.skip_mineru:
        mineru_client_cls, mineru_wait_msg = load_mineru_client()
        if mineru_client_cls is None:
            print(f"[等待] {mineru_wait_msg}", file=sys.stderr)
            print(
                "[等待] 本次仅运行本地链路；MinerU 侧指标留空。"
                "同事的 ingestion/mineru_client.py 就绪后重跑即可得到完整对照。",
                file=sys.stderr,
            )

    truth = pdf_text_layer_truth(pdf_path)

    local = mineru = None
    local_num = mineru_num = None
    content = None
    agreement = None
    local_inter: List[Dict[str, Any]] = []
    mineru_inter: List[Dict[str, Any]] = []

    if not args.skip_local:
        print("[运行] 本地链路 EnhancedDocumentProcessor.process_document ...", file=sys.stderr)
        local = run_local_chain(pdf_path)
        local_inter = detect_interleave(local["text_tokens"], truth["lm"], truth.get("vocabulary"))
        print(f"[完成] 本地链路 {local['elapsed_sec']}s", file=sys.stderr)

    if mineru_client_cls is not None:
        print(f"[运行] MinerU 链路 (tier={args.tier}) ...", file=sys.stderr)
        mineru = run_mineru_chain(pdf_path, args.tier, mineru_client_cls)
        mineru_inter = detect_interleave(mineru["text_tokens"], truth["lm"], truth.get("vocabulary"))
        print(f"[完成] MinerU 链路 {mineru['elapsed_sec']}s", file=sys.stderr)

    if local and mineru:
        # 同题口径：只在双方都检出表格的页面上比较数值还原能力
        common_pages = sorted(set(local["table_pages"]) & set(mineru["table_pages"]))
        local["common_table_pages"] = common_pages
        mineru["common_table_pages"] = common_pages
        local_num = numeric_integrity_metrics(local, truth)
        mineru_num = numeric_integrity_metrics(mineru, truth)
        content = table_content_metrics(local, mineru)
        agreement = table_page_agreement(local, mineru)
    elif local:
        local["common_table_pages"] = []
        local_num = numeric_integrity_metrics(local, truth)
    elif mineru:
        mineru["common_table_pages"] = []
        mineru_num = numeric_integrity_metrics(mineru, truth)

    conclusion = (
        build_conclusion(
            local, mineru, local_num, mineru_num, content, agreement, local_inter, mineru_inter
        )
        if (local and mineru)
        else {"verdicts": [{"metric": "整体", "winner": "无法判定", "detail": "仅运行了一条链路"}]}
    )

    report = render_report(
        pdf_path,
        args.tier,
        local,
        mineru,
        local_num,
        mineru_num,
        content,
        agreement,
        local_inter,
        mineru_inter,
        conclusion,
    )
    print(report)

    if args.json_out:
        payload = {
            "pdf": str(pdf_path),
            "tier": args.tier,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mineru_client_available": mineru_client_cls is not None,
            "mineru_client_note": mineru_wait_msg,
            "local": _json_side(local, local_num, local_inter),
            "mineru": _json_side(mineru, mineru_num, mineru_inter),
            "table_content": content,
            "table_page_agreement": agreement,
            "conclusion": conclusion,
        }
        out_path = Path(args.json_out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[输出] JSON 报告: {out_path}", file=sys.stderr)

    return 0


def _json_side(
    side: Optional[Dict[str, Any]],
    numeric: Optional[Dict[str, Any]],
    interleave: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if side is None:
        return None
    return {
        "elapsed_sec": side["elapsed_sec"],
        "page_count": side["page_count"],
        "table_count": side["table_count"],
        "table_pages": side["table_pages"],
        "table_numbers_by_page": side.get("table_numbers_by_page", {}),
        "block_types": side["block_types"],
        "image_output": side["image_output"],
        "numeric_integrity": numeric,
        "interleave_tokens": interleave,
        "interleave_count": len(interleave),
        "param_count": len(side["table_pairs"]),
        "server_parse_ms": side.get("server_parse_ms"),
    }


if __name__ == "__main__":
    sys.exit(main())
