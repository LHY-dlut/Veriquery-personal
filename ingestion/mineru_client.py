"""
MinerU 解析客户端 —— 对接 MinerU 异步 PDF 解析服务。

本模块封装 MinerU 服务真实的异步接口契约（服务地址默认 http://127.0.0.1:8001），
完整流程为：

    1. POST /v1/uploads                     申请上传凭证（返回 upload_url / upload_method）
    2. PUT  {upload_url}                   上传 PDF 二进制
    3. POST /v1/uploads/{upload_id}/complete  完成上传，响应中的 ``file.id`` 才是正式文件 ID
    4. POST /v1/parse/jobs                 以正式 file_id 提交解析任务（tier / output_formats）
    5. GET  /v1/parse/jobs/{job_id}        轮询任务直到 completed / partial / failed
    6. GET  /v1/files/{file_id}/content    读取 markdown / structured_content / middle_json

注意：第 3 步返回的 ``file.id``（形如 ``file-xxxx``）与第 1 步返回的 upload id（形如
``upload_xxx``）不是同一个东西，用 upload id 建 job 会报 "File ... not found"。

主要能力：
    - MinerUClient: 同步封装的异步客户端（上传→建任务→轮询→取结果）
    - MinerUParseResult / MinerUPage / MinerUImageBlock / MinerUTableBlock: 结果数据结构
    - parse_document_for_pages: 顶层入口，输出可直接映射到 ingestion 的 PageContent 结构

已知上游缺陷：多栏 PDF 可能出现字符交错（例如 "jTuhnecrtmioanl - re ssoilsdtearnincge"），
本客户端不做修复，只在结果中如实返回，由上层对照评测负责甄别。
"""


import base64
import binascii
import hashlib
import io
import ipaddress
import json
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# ==================== 默认配置 ====================

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_TIER = "flash"
DEFAULT_OUTPUT_FORMATS = ("markdown", "middle_json", "structured_content")
SUPPORTED_TIERS = ("flash", "basic", "standard", "advanced")
TERMINAL_JOB_STATUS = ("completed", "partial", "failed", "canceled")

_PROXY_BYPASS: Dict[str, Optional[str]] = {"http": None, "https": None}

#: 需要保留正文的块类型（页眉/页脚/页码不进正文，避免检索噪声）
_TEXT_BLOCK_TYPES = ("text", "paragraph_title", "list", "list_item")


class MinerUError(Exception):
    """MinerU 服务调用异常。

    Attributes:
        message: 错误描述（不含 HTTP 状态码等附加信息）。
        status_code: HTTP 状态码；网络层异常时为 None。
        response_snippet: 响应体片段（最多 500 字符），便于定位服务端错误。
        url: 出错时使用的请求地址。
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_snippet: Optional[str] = None,
        url: Optional[str] = None,
    ):
        detail = message
        if status_code is not None:
            detail = f"[HTTP {status_code}] {message}"
        if url:
            detail = f"{detail} | url={url}"
        if response_snippet:
            detail = f"{detail} | body={response_snippet}"
        super().__init__(detail)
        self.message = message
        self.status_code = status_code
        self.response_snippet = response_snippet
        self.url = url


# ==================== 结果数据结构 ====================


@dataclass
class MinerUImageBlock:
    """MinerU 输出的图像块（对应带 bbox 的独立图表裁剪图）。

    Attributes:
        page_number: 所在页码（1 基）。
        page_idx: 原始 page_idx（0 基）。
        block_index: 该页内块序号（0 基）。
        bbox: 归一化坐标 [x0, y0, x1, y1]。
        captions: 块标题/图注列表。
        image_source: 原始 data URI（data:image/jpeg;base64,...）。
        raw_bytes: data URI 解码后的原始二进制。
        png_bytes: 统一转成 PNG 的字节，与项目内 images["data"] 约定一致。
        pil_image: PIL.Image 对象；PIL 不可用或解码失败时为 None。
        width: 图像像素宽。
        height: 图像像素高。
        mime_type: 原始 MIME 类型。
    """

    page_number: int
    page_idx: int
    block_index: int
    bbox: List[float]
    captions: List[str]
    image_source: str
    raw_bytes: bytes
    png_bytes: Optional[bytes] = None
    pil_image: Any = None
    width: int = 0
    height: int = 0
    mime_type: str = ""

    def to_dict(self, include_binary: bool = False) -> Dict[str, Any]:
        """转为普通字典。

        Args:
            include_binary: 是否包含 png_bytes / pil_image 二进制内容。

        Returns:
            字典形式的块描述。
        """
        data = {
            "page_number": self.page_number,
            "page_idx": self.page_idx,
            "block_index": self.block_index,
            "bbox": list(self.bbox),
            "captions": list(self.captions),
            "image_source": self.image_source,
            "width": self.width,
            "height": self.height,
            "mime_type": self.mime_type,
        }
        if include_binary:
            data["raw_bytes"] = self.raw_bytes
            data["png_bytes"] = self.png_bytes
            data["pil_image"] = self.pil_image
        return data


@dataclass
class MinerUTableBlock:
    """MinerU 输出的表格块。

    Attributes:
        page_number: 所在页码（1 基）。
        page_idx: 原始 page_idx（0 基）。
        block_index: 该页内块序号（0 基）。
        bbox: 归一化坐标。
        captions: 表格标题/图注列表。
        raw_content: 原始 content（Markdown 管道表 或 <table> HTML），原样保留。
        content_format: 内容格式，"markdown" / "html" / "text"。
        rows: 解析出的表格行（二维字符串数组），解析失败时为空。
        plain_text: 清洗后的纯文本版本，便于直接进 LLM context。
    """

    page_number: int
    page_idx: int
    block_index: int
    bbox: List[float]
    captions: List[str]
    raw_content: str
    content_format: str
    rows: List[List[str]] = field(default_factory=list)
    plain_text: str = ""

    def to_dict(self, include_raw: bool = True) -> Dict[str, Any]:
        """转为普通字典。

        Args:
            include_raw: 是否包含 raw_content 原文。

        Returns:
            字典形式的表格描述。
        """
        data = {
            "page_number": self.page_number,
            "page_idx": self.page_idx,
            "block_index": self.block_index,
            "bbox": list(self.bbox),
            "captions": list(self.captions),
            "content_format": self.content_format,
            "rows": [list(r) for r in self.rows],
            "plain_text": self.plain_text,
        }
        if include_raw:
            data["raw_content"] = self.raw_content
        return data


@dataclass
class MinerUPage:
    """MinerU 解析出的单页内容。

    Attributes:
        page_number: 页码（1 基）。
        page_idx: 原始 page_idx（0 基）。
        width: 页宽（归一化坐标 0-1 或页面像素，取决于服务端返回）。
        height: 页高。
        text: 正文文本（已排除页眉/页脚/页码/表格/图片块）。
        full_text: 全部文本块拼接结果（含页眉页脚）。
        blocks: 原始 block 字典列表（未加工）。
        images: 该页图像块列表。
        tables: 该页表格块列表。
    """

    page_number: int
    page_idx: int
    width: float = 0.0
    height: float = 0.0
    text: str = ""
    full_text: str = ""
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    images: List[MinerUImageBlock] = field(default_factory=list)
    tables: List[MinerUTableBlock] = field(default_factory=list)

    def to_dict(self, include_binary: bool = False) -> Dict[str, Any]:
        """转为普通字典。

        Args:
            include_binary: 是否包含图像二进制内容。

        Returns:
            字典形式的页面描述。
        """
        return {
            "page_number": self.page_number,
            "page_idx": self.page_idx,
            "width": self.width,
            "height": self.height,
            "text": self.text,
            "full_text": self.full_text,
            "blocks": self.blocks,
            "images": [img.to_dict(include_binary=include_binary) for img in self.images],
            "tables": [tbl.to_dict() for tbl in self.tables],
        }


@dataclass
class MinerUParseResult:
    """一次 MinerU 解析任务的完整结果。

    Attributes:
        status: "success" / "partial" / "error"。
        filename: 提交的文件名。
        tier: 使用的解析档位。
        page_count: 解析页数。
        pages: 逐页结果。
        markdown: 整篇 Markdown 文本。
        markdown_clean: 去掉 base64 内嵌图后的 Markdown（适合进 LLM context）。
        structured_content: 服务端返回的 structured_content 原文（pages[].blocks）。
        middle_json: 服务端返回的 middle_json（可能为空）。
        images: 全部图像块（扁平列表）。
        tables: 全部表格块（扁平列表）。
        meta: 解析元信息（duration_ms / parser_version / job_id / file_id 等）。
        errors: 非致命问题列表（部分块解码失败等）。
    """

    status: str
    filename: str
    tier: str
    page_count: int
    pages: List[MinerUPage]
    markdown: str
    markdown_clean: str
    structured_content: Dict[str, Any]
    middle_json: Dict[str, Any]
    images: List[MinerUImageBlock]
    tables: List[MinerUTableBlock]
    meta: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self, include_binary: bool = False) -> Dict[str, Any]:
        """转为普通字典（parse_pdf 的返回结构）。

        Args:
            include_binary: 是否包含图像二进制内容。

        Returns:
            字典形式的解析结果。
        """
        return {
            "status": self.status,
            "filename": self.filename,
            "tier": self.tier,
            "page_count": self.page_count,
            "pages": [p.to_dict(include_binary=include_binary) for p in self.pages],
            "markdown": self.markdown,
            "markdown_clean": self.markdown_clean,
            "structured_content": self.structured_content,
            "middle_json": self.middle_json,
            "images": [img.to_dict(include_binary=include_binary) for img in self.images],
            "tables": [tbl.to_dict() for tbl in self.tables],
            "meta": dict(self.meta),
            "errors": list(self.errors),
        }


# ==================== 表格/HTML 解析工具 ====================


class _TableHTMLParser(HTMLParser):
    """把 ``<table>`` HTML 解析成二维数组（用于表格块行提取）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(_collapse_ws("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(c for c in self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


class _TextHTMLParser(HTMLParser):
    """把 HTML 片段转成纯文本（保留块级换行）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag.lower() in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        """返回清洗后的纯文本。"""
        lines = [_collapse_ws(line) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _collapse_ws(text: str) -> str:
    """把连续空白折叠为单个空格。"""
    return re.sub(r"[ \t ]+", " ", text or "").strip()


def _parse_html_table(html: str) -> List[List[str]]:
    """解析 ``<table>`` HTML 为二维数组。"""
    parser = _TableHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as e:  # pragma: no cover - 极端畸形 HTML
        logger.debug(f"HTML 表格解析失败: {e}")
    return parser.rows


def _parse_markdown_table(markdown: str) -> List[List[str]]:
    """解析 Markdown 管道表格为二维数组。"""
    rows: List[List[str]] = []
    for line in (markdown or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        # 跳过 |---|---| 分隔行
        if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells if c != ""):
            continue
        if any(c for c in cells):
            rows.append(cells)
    return rows


def _strip_code_fence(content: str) -> str:
    """去掉 ``` 代码围栏，保留内部列对齐空格（对齐本身承载表格结构）。"""
    lines = (content or "").splitlines()
    while lines and lines[0].strip().startswith("```"):
        lines.pop(0)
    while lines and lines[-1].strip().startswith("```"):
        lines.pop()
    cleaned = [line.rstrip() for line in lines]
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned)


def _parse_spaced_table(text: str) -> List[List[str]]:
    """按 2 个以上连续空格切列（仅当所有行列数一致时才认为可信）。"""
    rows: List[List[str]] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        cells = [c.strip() for c in re.split(r"\s{2,}", line.strip())]
        rows.append(cells)
    if len(rows) < 2:
        return []
    counts = {len(r) for r in rows}
    if len(counts) != 1 or counts.pop() < 2:
        return []
    return rows


def _rows_to_plain_text(rows: List[List[str]]) -> str:
    """把二维数组渲染成纯文本（每行用 `` | `` 分隔）。"""
    return "\n".join(" | ".join(c for c in row) for row in rows if row)


def _parse_table_content(content: str) -> Tuple[str, List[List[str]], str]:
    """解析表格块内容。

    支持三种实际出现过的格式：
      - HTML ``<table>``（最常见，单元格对齐完整）
      - Markdown 管道表 ``| a | b |``
      - ``` 代码围栏包裹的空格对齐文本（列对齐靠空格，围栏需去掉）

    Args:
        content: 原始 content。

    Returns:
        三元组 ``(content_format, rows, plain_text)``；content_format 取值
        "html" / "markdown" / "code" / "text"。
    """
    content = content or ""
    lowered = content.lower()
    if "<table" in lowered or "<tr" in lowered:
        rows = _parse_html_table(content)
        plain = _rows_to_plain_text(rows)
        if not plain.strip():
            parser = _TextHTMLParser()
            try:
                parser.feed(content)
                parser.close()
            except Exception:  # pragma: no cover
                pass
            plain = parser.text()
        return "html", rows, plain
    if content.strip().startswith("```"):
        body = _strip_code_fence(content)
        rows = _parse_spaced_table(body)
        if rows:
            return "code", rows, _rows_to_plain_text(rows)
        # 列对齐不规则时保留原始对齐文本（对齐本身就是结构信息）
        return "code", [], body
    if "|" in content:
        rows = _parse_markdown_table(content)
        plain = _rows_to_plain_text(rows)
        return "markdown", rows, plain
    return "text", [], _collapse_ws(content)


#: Markdown 内嵌 base64 图片（体积巨大，不适合进 LLM context）
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*data:image/[^)]*\)", re.IGNORECASE)


def _strip_markdown_base64_images(markdown: str) -> Tuple[str, int]:
    """把 Markdown 中的 base64 内嵌图替换为短占位符。

    MinerU 的 markdown 会把每张裁剪图以 base64 内嵌，单页就有几十 KB，
    直接进 LLM context 会挤爆 token 预算，这里统一替换为 ``mineru_img_N`` 占位。

    Args:
        markdown: 原始 markdown 文本。

    Returns:
        二元组 ``(清洗后的 markdown, 被替换的图片数)``。
    """
    count = 0

    def _replace(match: "re.Match[str]") -> str:
        nonlocal count
        count += 1
        alt = match.group(1) or "image"
        return f"![{alt}](mineru_img_{count})"

    return _MARKDOWN_IMAGE_RE.sub(_replace, markdown or ""), count


def _normalize_captions(captions: Any) -> List[str]:
    """把 captions 字段规整为字符串列表。"""
    if not captions:
        return []
    if isinstance(captions, str):
        return [captions]
    out: List[str] = []
    if isinstance(captions, (list, tuple)):
        for item in captions:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("caption") or ""
                if text:
                    out.append(str(text))
    return out


def _is_internal_host(host: str) -> bool:
    """判断主机是否为内网地址（内网直连、禁用公司代理）。"""
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        pass
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
        return ip.is_private or ip.is_loopback
    except Exception:
        return False


# ==================== 客户端 ====================


class MinerUClient:
    """MinerU 异步解析服务客户端。

    用法::

        client = MinerUClient(base_url="http://127.0.0.1:8001")
        result = client.parse_pdf("/path/to/datasheet.pdf", tier="flash")
        print(result["meta"]["duration_ms"], len(result["images"]))
    """

    def __init__(
        self,
        settings: Any = None,
        base_url: Optional[str] = None,
        timeout: int = 600,
        api_key: Optional[str] = None,
        max_retries: int = 2,
    ):
        """初始化客户端。

        Args:
            settings: 可选的全局 settings 对象；从中读取 MinerU 相关配置
                （MINERU_API_BASE / MINERU_BASE_URL / MINERU_TIMEOUT / MINERU_API_KEY）。
            base_url: 显式服务地址，优先级高于 settings。
            timeout: 默认的解析总超时（秒），可被 parse_pdf 覆盖。
            api_key: 可选鉴权 token，配置后自动附加 Authorization 头。
            max_retries: 单次 HTTP 请求的重试次数（指数退避）。
        """
        self.settings = settings
        if settings is None and base_url is None:
            try:
                from core.config import get_settings

                settings = get_settings()
                self.settings = settings
            except Exception as e:  # pragma: no cover - 配置不可用时退回默认值
                logger.debug(f"读取全局 settings 失败，使用默认配置: {e}")

        resolved_base = (
            base_url
            or getattr(self.settings, "MINERU_API_BASE", None)
            or getattr(self.settings, "MINERU_BASE_URL", None)
            or DEFAULT_BASE_URL
        )
        self.base_url = str(resolved_base).rstrip("/")
        self.timeout = int(timeout)
        self.max_retries = max(0, int(max_retries))
        self.api_key = (
            api_key
            or getattr(self.settings, "MINERU_API_KEY", None)
        )
        self._headers = {"Accept": "application/json"}
        if self.api_key and str(self.api_key) not in ("", "empty", "none", "None"):
            self._headers["Authorization"] = f"Bearer {self.api_key}"
        logger.info(f"MinerUClient 初始化: base={self.base_url}, timeout={self.timeout}s")

    # ---------------- HTTP 基础 ----------------

    def _proxies(self) -> Optional[Dict[str, Optional[str]]]:
        """内网地址禁用公司代理，外网地址沿用环境代理设置。"""
        host = urlparse(self.base_url).hostname or ""
        return _PROXY_BYPASS if _is_internal_host(host) else None

    def _request(
        self,
        method: str,
        url: str,
        timeout: float = 120,
        retries: Optional[int] = None,
        **kwargs: Any,
    ) -> requests.Response:
        """发起 HTTP 请求，失败时按指数退避重试。

        网络异常与 5xx / 429 会重试；4xx 客户端错误直接抛出，不做无谓重试。

        Args:
            method: HTTP 方法。
            url: 完整请求地址。
            timeout: 单次请求超时（秒）。
            retries: 重试次数，默认取构造参数 max_retries。
            **kwargs: 透传给 requests.request 的参数。

        Returns:
            成功（状态码 < 400）的 Response 对象。

        Raises:
            MinerUError: 重试耗尽或遇到不可重试的 4xx 错误。
        """
        retries = self.max_retries if retries is None else retries
        kwargs.setdefault("proxies", self._proxies())
        last_error: Optional[MinerUError] = None

        for attempt in range(retries + 1):
            try:
                resp = requests.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as e:
                last_error = MinerUError(
                    f"请求失败: {type(e).__name__}: {e}", url=url
                )
            else:
                if resp.status_code < 400:
                    return resp
                snippet = (resp.text or "")[:500]
                last_error = MinerUError(
                    f"MinerU 服务返回错误状态 {resp.status_code}",
                    status_code=resp.status_code,
                    response_snippet=snippet,
                    url=url,
                )
                # 4xx（除 429 外）属于请求本身有问题，重试无意义
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise last_error

            if attempt < retries:
                delay = float(2 ** attempt)
                logger.warning(
                    f"MinerU 请求 {method} {url} 第 {attempt + 1} 次失败: {last_error}；"
                    f"{delay:.1f}s 后重试"
                )
                time.sleep(delay)

        raise last_error or MinerUError(f"请求失败: {method} {url}", url=url)

    def _request_json(
        self,
        method: str,
        url: str,
        timeout: float = 120,
        retries: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """发起请求并解析 JSON 响应。"""
        resp = self._request(method, url, timeout=timeout, retries=retries, **kwargs)
        try:
            return resp.json()
        except ValueError as e:
            raise MinerUError(
                f"响应不是合法 JSON: {e}",
                status_code=resp.status_code,
                response_snippet=(resp.text or "")[:500],
                url=url,
            ) from e

    # ---------------- 流程各步骤 ----------------

    def _create_upload(self, filename: str, size: int) -> Dict[str, Any]:
        """第 1 步：申请上传凭证。"""
        payload = {
            "filename": filename,
            "mime_type": "application/pdf",
            "purpose": "parse",
            "bytes": int(size),
        }
        logger.debug(f"创建 MinerU upload: {payload}")
        return self._request_json(
            "POST", f"{self.base_url}/v1/uploads", json=payload, timeout=120
        )

    def _upload_content(self, upload: Dict[str, Any], raw: bytes) -> None:
        """第 2 步：向 upload_url 上传 PDF 二进制。"""
        upload_id = upload.get("id") or upload.get("upload_id")
        upload_url = upload.get("upload_url") or f"{self.base_url}/v1/uploads/{upload_id}/content"
        method = str(upload.get("upload_method") or "PUT").upper()
        headers = dict(upload.get("upload_headers") or {})
        headers.setdefault("Content-Type", "application/pdf")
        resp = self._request(method, upload_url, data=raw, headers=headers, timeout=300)
        logger.debug(f"MinerU 上传完成: {method} {upload_url} -> {resp.status_code}")

    def _complete_upload(self, upload_id: str) -> Dict[str, Any]:
        """第 3 步：完成上传，返回正式文件对象（file.id）。"""
        resp = self._request_json(
            "POST", f"{self.base_url}/v1/uploads/{upload_id}/complete", json={}, timeout=120
        )
        file_obj = resp.get("file") or {}
        file_id = file_obj.get("id") or resp.get("file_id")
        if not file_id:
            raise MinerUError(
                "完成上传后未返回正式 file.id，无法提交解析任务",
                response_snippet=json.dumps(resp, ensure_ascii=False)[:500],
                url=f"{self.base_url}/v1/uploads/{upload_id}/complete",
            )
        logger.debug(f"MinerU upload 完成: upload_id={upload_id} -> file_id={file_id}")
        return {"file_id": file_id, "file": file_obj, "raw": resp}

    def _create_job(
        self,
        file_id: str,
        tier: str,
        output_formats: List[str],
        ocr_mode: str = "auto",
        page_range: Optional[str] = None,
    ) -> Dict[str, Any]:
        """第 4 步：提交解析任务。"""
        if tier not in SUPPORTED_TIERS:
            logger.warning(
                f"tier={tier!r} 不在已知档位 {SUPPORTED_TIERS} 中，仍提交给服务端校验"
            )
        entry: Dict[str, Any] = {"source": {"type": "file_id", "file_id": file_id}}
        if page_range:
            entry["page_range"] = page_range
        payload = {
            "files": [entry],
            "tier": tier,
            "output_formats": list(output_formats),
        }
        if ocr_mode:
            payload["ocr_mode"] = ocr_mode
        resp = self._request(
            "POST", f"{self.base_url}/v1/parse/jobs", json=payload, timeout=120
        )
        try:
            job = resp.json()
        except ValueError as e:
            raise MinerUError(
                f"创建解析任务返回非 JSON: {e}",
                status_code=resp.status_code,
                response_snippet=(resp.text or "")[:500],
            ) from e
        job_id = job.get("job_id")
        if not job_id:
            raise MinerUError(
                "创建解析任务未返回 job_id",
                status_code=resp.status_code,
                response_snippet=(resp.text or "")[:500],
            )
        logger.debug(f"MinerU 解析任务已创建: job_id={job_id}, tier={tier}")
        return job

    def _wait_for_job(
        self,
        job_id: str,
        deadline: float,
        poll_interval: float = 1.0,
        max_poll_interval: float = 10.0,
    ) -> Dict[str, Any]:
        """第 5 步：轮询任务直到进入终态。

        Args:
            job_id: 任务 ID。
            deadline: time.time() 形式的绝对截止时间。
            poll_interval: 初始轮询间隔（秒）。
            max_poll_interval: 最大轮询间隔（秒）。

        Returns:
            终态任务的 JSON 响应。

        Raises:
            MinerUError: 任务失败 / 被取消，或超过总超时。
        """
        interval = max(0.5, float(poll_interval))
        max_interval = max(interval, float(max_poll_interval))
        waited = 0.0
        attempt = 0

        while True:
            if time.time() > deadline:
                raise MinerUError(
                    f"等待 MinerU 解析任务超时（已等待 {waited:.1f}s），job_id={job_id}"
                )
            job = self._request_json(
                "GET", f"{self.base_url}/v1/parse/jobs/{job_id}", timeout=60
            )
            status = (job.get("status") or "").lower()
            progress = job.get("progress") or {}
            attempt += 1
            if status in TERMINAL_JOB_STATUS or attempt % 5 == 0:
                logger.info(
                    f"MinerU 任务状态: job_id={job_id}, status={status}, "
                    f"progress={progress}, 已等待={waited:.1f}s"
                )
            if status in TERMINAL_JOB_STATUS:
                if status in ("failed", "canceled"):
                    raise MinerUError(
                        f"MinerU 解析任务{status}，job_id={job_id}",
                        response_snippet=json.dumps(job, ensure_ascii=False)[:500],
                    )
                return job

            time.sleep(min(interval, max(0.0, deadline - time.time())))
            waited += interval
            interval = min(interval * 1.5, max_interval)

    def _fetch_output_content(self, file_id: str, kind: str, timeout: float = 120) -> Any:
        """第 6 步：读取输出文件内容。"""
        resp = self._request(
            "GET", f"{self.base_url}/v1/files/{file_id}/content", timeout=timeout
        )
        text = resp.text or ""
        if kind in ("structured_content", "middle_json"):
            try:
                return json.loads(text)
            except ValueError:
                logger.warning(f"MinerU 输出 {kind} 不是合法 JSON，按原文保存")
                return {"raw": text}
        return text

    # ---------------- 主入口 ----------------

    def parse_pdf(
        self,
        pdf_path_or_bytes: Union[str, Path, bytes],
        tier: str = DEFAULT_TIER,
        output_formats: Optional[List[str]] = None,
        timeout: int = 600,
        filename: Optional[str] = None,
        ocr_mode: str = "auto",
        page_range: Optional[str] = None,
        poll_interval: float = 1.0,
        max_poll_interval: float = 10.0,
        keep_pil: bool = True,
    ) -> Dict[str, Any]:
        """解析 PDF 并返回结构化结果。

        完整走通 上传 → complete → 建任务 → 轮询 → 取内容 流程。

        Args:
            pdf_path_or_bytes: PDF 文件路径，或 PDF 二进制字节。
            tier: 解析档位（flash / basic / standard / advanced）。
            output_formats: 需要的输出格式，默认
                ["markdown", "middle_json", "structured_content"]。
            timeout: 解析总超时（秒），覆盖上传到取结果的全过程。
            filename: 提交时使用的文件名（传字节时必填或自动命名）。
            ocr_mode: OCR 模式（auto / txt / ocr）。
            page_range: 页码范围，如 "1-5,8"；None 表示全部。
            poll_interval: 轮询初始间隔（秒）。
            max_poll_interval: 轮询最大间隔（秒）。
            keep_pil: 是否保留 PIL.Image 对象（保留会占用内存）。

        Returns:
            字典，包含 status / filename / tier / page_count / pages / markdown /
            structured_content / middle_json / images / tables / meta / errors；
            images 每项带 bbox、页码、png_bytes 与 pil_image。

        Raises:
            MinerUError: 服务不可达、上传失败、任务失败或超时等任何硬错误。
        """
        started = time.time()
        deadline = started + float(timeout)
        errors: List[str] = []

        raw, default_name = self._read_input(pdf_path_or_bytes)
        upload_name = filename or default_name
        formats = list(output_formats or DEFAULT_OUTPUT_FORMATS)

        logger.info(
            f"开始 MinerU 解析: file={upload_name}, size={len(raw)}B, "
            f"tier={tier}, formats={formats}, timeout={timeout}s"
        )

        # 1) 创建 upload
        upload = self._create_upload(upload_name, len(raw))
        upload_id = upload.get("id") or upload.get("upload_id")
        if not upload_id:
            raise MinerUError(
                "创建 upload 未返回 id",
                response_snippet=json.dumps(upload, ensure_ascii=False)[:500],
            )

        # 2) 上传二进制
        self._upload_content(upload, raw)

        # 3) 完成上传，取正式 file.id
        completed = self._complete_upload(upload_id)
        file_id = completed["file_id"]

        # 4) 提交解析任务
        job = self._create_job(file_id, tier, formats, ocr_mode=ocr_mode, page_range=page_range)
        job_id = job.get("job_id")

        # 5) 轮询直到终态
        final_job = self._wait_for_job(
            job_id,
            deadline=deadline,
            poll_interval=poll_interval,
            max_poll_interval=max_poll_interval,
        )

        # 6) 取输出内容
        file_entries = final_job.get("files") or []
        if not file_entries:
            raise MinerUError(
                f"MinerU 任务 {job_id} 未返回任何文件结果",
                response_snippet=json.dumps(final_job, ensure_ascii=False)[:500],
            )
        entry = file_entries[0]
        entry_status = (entry.get("status") or "").lower()
        if entry_status == "failed":
            raise MinerUError(
                f"MinerU 文件解析失败: {entry.get('name')}",
                response_snippet=json.dumps(entry.get("error"), ensure_ascii=False)[:500],
            )
        if entry.get("error"):
            errors.append(f"文件 {entry.get('name')} 返回 error: {entry.get('error')}")

        output_files = entry.get("output_files") or {}
        markdown = ""
        structured_content: Dict[str, Any] = {}
        middle_json: Dict[str, Any] = {}

        md_ref = output_files.get("markdown")
        if isinstance(md_ref, dict) and md_ref.get("file_id"):
            markdown = self._fetch_output_content(md_ref["file_id"], "markdown") or ""

        sc_ref = output_files.get("structured_content")
        if isinstance(sc_ref, dict) and sc_ref.get("file_id"):
            structured_content = self._fetch_output_content(sc_ref["file_id"], "structured_content")
            if not isinstance(structured_content, dict):
                structured_content = {"raw": structured_content}

        mj_ref = output_files.get("middle_json")
        if isinstance(mj_ref, dict) and mj_ref.get("file_id"):
            middle_json = self._fetch_output_content(mj_ref["file_id"], "middle_json")
            if not isinstance(middle_json, dict):
                middle_json = {"raw": middle_json}

        if not structured_content and "structured_content" in formats:
            raise MinerUError(
                "MinerU 未返回 structured_content，无法提取页面块结构",
                response_snippet=json.dumps(entry, ensure_ascii=False)[:500],
            )

        # 组装结果
        pages, images, tables = self._build_pages(structured_content, keep_pil=keep_pil, errors=errors)
        markdown_clean, stripped_images = _strip_markdown_base64_images(markdown)
        if not structured_content:
            errors.append(
                "本次未请求 structured_content（或服务端未返回该产物），"
                "pages/images/tables 为空，仅 markdown 可用"
            )

        parse_info = entry.get("parse") or {}
        meta = {
            "job_id": job_id,
            "file_id": file_id,
            "upload_id": upload_id,
            "tier": tier,
            "output_formats": formats,
            "ocr_mode": ocr_mode,
            "page_range": page_range,
            "status": final_job.get("status"),
            "file_status": entry_status,
            "duration_ms": parse_info.get("duration_ms"),
            "parser_version": parse_info.get("parser_version"),
            "model_used": parse_info.get("model_used"),
            "page_range_reported": entry.get("page_range"),
            "markdown_bytes": len(markdown.encode("utf-8")),
            "markdown_clean_bytes": len(markdown_clean.encode("utf-8")),
            "markdown_images_stripped": stripped_images,
            "client_elapsed_ms": int((time.time() - started) * 1000),
        }

        status = "success"
        if final_job.get("status") == "partial" or entry_status == "partial":
            status = "partial"
        if errors:
            status = "partial"

        result = MinerUParseResult(
            status=status,
            filename=upload_name,
            tier=tier,
            page_count=len(pages),
            pages=pages,
            markdown=markdown,
            markdown_clean=markdown_clean,
            structured_content=structured_content,
            middle_json=middle_json,
            images=images,
            tables=tables,
            meta=meta,
            errors=errors,
        )
        logger.info(
            f"MinerU 解析完成: file={upload_name}, pages={len(pages)}, "
            f"images={len(images)}, tables={len(tables)}, "
            f"duration_ms={meta.get('duration_ms')}, status={status}"
        )
        # include_binary=True：图像块需携带 png_bytes / pil_image，便于下游直接送 VLM
        return result.to_dict(include_binary=True)

    # ---------------- 结果组装 ----------------

    @staticmethod
    def _read_input(pdf_path_or_bytes: Union[str, Path, bytes]) -> Tuple[bytes, str]:
        """读取输入 PDF（路径或字节）。

        Returns:
            二元组 ``(二进制内容, 默认文件名)``。
        """
        if isinstance(pdf_path_or_bytes, (bytes, bytearray)):
            raw = bytes(pdf_path_or_bytes)
            if not raw:
                raise MinerUError("传入的 PDF 字节为空")
            return raw, "document.pdf"
        path = Path(pdf_path_or_bytes)
        if not path.exists():
            raise MinerUError(f"PDF 文件不存在: {path}")
        raw = path.read_bytes()
        if not raw:
            raise MinerUError(f"PDF 文件为空: {path}")
        return raw, path.name

    def _build_pages(
        self,
        structured_content: Dict[str, Any],
        keep_pil: bool = True,
        errors: Optional[List[str]] = None,
    ) -> Tuple[List[MinerUPage], List[MinerUImageBlock], List[MinerUTableBlock]]:
        """把 structured_content 拆成页 / 图像块 / 表格块。

        Args:
            structured_content: 服务端返回的 {"pages": [{"page_idx":0,"blocks":[...]}]}。
            keep_pil: 是否保留 PIL.Image 对象。
            errors: 非致命错误收集列表（原地追加）。

        Returns:
            三元组 ``(pages, images, tables)``。
        """
        errors = errors if errors is not None else []
        pages: List[MinerUPage] = []
        all_images: List[MinerUImageBlock] = []
        all_tables: List[MinerUTableBlock] = []

        for page_pos, raw_page in enumerate(structured_content.get("pages") or []):
            page_idx = int(raw_page.get("page_idx", page_pos))
            page_number = page_idx + 1
            blocks = raw_page.get("blocks") or []
            page = MinerUPage(
                page_number=page_number,
                page_idx=page_idx,
                width=float(raw_page.get("width") or raw_page.get("page_width") or 0.0),
                height=float(raw_page.get("height") or raw_page.get("page_height") or 0.0),
                blocks=blocks,
            )

            body_parts: List[str] = []
            full_parts: List[str] = []

            for block_idx, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                btype = (block.get("type") or "").lower()
                content = block.get("content") or ""
                bbox = list(block.get("bbox") or [])
                captions = _normalize_captions(block.get("captions"))

                if isinstance(content, str) and content.strip():
                    full_parts.append(content.strip())
                    if btype in _TEXT_BLOCK_TYPES:
                        body_parts.append(content.strip())

                if btype == "image":
                    image_block = self._decode_image_block(
                        block=block,
                        page_idx=page_idx,
                        page_number=page_number,
                        block_idx=block_idx,
                        bbox=bbox,
                        captions=captions,
                        keep_pil=keep_pil,
                    )
                    if image_block is not None:
                        page.images.append(image_block)
                        all_images.append(image_block)
                    else:
                        errors.append(
                            f"第 {page_number} 页图像块 {block_idx} 解码失败（已跳过）"
                        )

                elif btype == "table":
                    content_format, rows, plain_text = _parse_table_content(content)
                    table_block = MinerUTableBlock(
                        page_number=page_number,
                        page_idx=page_idx,
                        block_index=block_idx,
                        bbox=bbox,
                        captions=captions,
                        raw_content=content,
                        content_format=content_format,
                        rows=rows,
                        plain_text=plain_text,
                    )
                    page.tables.append(table_block)
                    all_tables.append(table_block)

            page.text = "\n\n".join(body_parts)
            page.full_text = "\n\n".join(full_parts)
            pages.append(page)

        return pages, all_images, all_tables

    def _decode_image_block(
        self,
        block: Dict[str, Any],
        page_idx: int,
        page_number: int,
        block_idx: int,
        bbox: List[float],
        captions: List[str],
        keep_pil: bool = True,
    ) -> Optional[MinerUImageBlock]:
        """把 image_source（data URI）解码成二进制 + PIL 图像。"""
        source = block.get("image_source") or ""
        if not source:
            return None

        mime_type = "image/jpeg"
        payload = source
        if source.startswith("data:"):
            header, _, data = source.partition(",")
            if not _:
                return None
            if header.startswith("data:") and ";" in header:
                mime_type = header[5:].split(";")[0] or mime_type
            payload = data

        try:
            raw_bytes = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as e:
            logger.warning(f"图像块 base64 解码失败 (页 {page_number}, 块 {block_idx}): {e}")
            return None
        if not raw_bytes:
            return None

        pil_image = None
        png_bytes = None
        width = 0
        height = 0
        try:
            from PIL import Image as PILImage

            pil_image = PILImage.open(io.BytesIO(raw_bytes))
            pil_image.load()
            width, height = pil_image.size
            buffer = io.BytesIO()
            pil_image.convert("RGB").save(buffer, format="PNG")
            png_bytes = buffer.getvalue()
            if not keep_pil:
                pil_image.close()
                pil_image = None
        except Exception as e:
            logger.warning(
                f"图像块转 PIL 失败 (页 {page_number}, 块 {block_idx}): {e}（保留原始二进制）"
            )

        return MinerUImageBlock(
            page_number=page_number,
            page_idx=page_idx,
            block_index=block_idx,
            bbox=bbox,
            captions=captions,
            image_source=source,
            raw_bytes=raw_bytes,
            png_bytes=png_bytes,
            pil_image=pil_image,
            width=width,
            height=height,
            mime_type=mime_type,
        )


# ==================== 顶层入口 ====================


def parse_document_for_pages(
    pdf_path: Union[str, Path],
    tier: str = DEFAULT_TIER,
    settings: Any = None,
    client: Optional[MinerUClient] = None,
    output_formats: Optional[List[str]] = None,
    timeout: int = 600,
    image_dir: Optional[Union[str, Path]] = None,
    document_id: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """用 MinerU 解析 PDF，并输出可直接映射到 ingestion PageContent 的页面结构。

    供 ingestion/document_processor.py 后续替换本地 PyMuPDF 解析链路使用：
    返回的每一页都包含 page_number / text / images / tables / metadata，
    与 ``PageContent`` 字段一一对应。

    Args:
        pdf_path: PDF 路径。
        tier: 解析档位。
        settings: 全局 settings（用于取 MinerU 配置）。
        client: 已存在的 MinerUClient 实例（复用时传入，避免重复建连接配置）。
        output_formats: 输出格式列表。
        timeout: 解析总超时（秒）。
        image_dir: 若指定，图像块 PNG 会落盘到该目录并在 image_info 中给出 path。
        document_id: 文档 ID，缺省用文件路径 MD5（与 document_processor 约定一致）。
        **kwargs: 透传给 MinerUClient.parse_pdf 的其余参数。

    Returns:
        字典，包含 source / document_id / filename / page_count / pages / markdown / meta。

    Raises:
        MinerUError: 解析失败时抛出（不静默降级）。
    """
    path = Path(pdf_path)
    if not path.exists():
        raise MinerUError(f"PDF 文件不存在: {path}")

    cli = client or MinerUClient(settings=settings)
    parsed = cli.parse_pdf(
        path,
        tier=tier,
        output_formats=output_formats,
        timeout=timeout,
        **kwargs,
    )

    doc_id = document_id or hashlib.md5(str(path).encode()).hexdigest()
    img_dir = Path(image_dir) if image_dir else None
    if img_dir is not None:
        img_dir.mkdir(parents=True, exist_ok=True)

    # 图像二进制索引：(page_number, block_index) -> png_bytes
    binary_index: Dict[Tuple[int, int], bytes] = {}
    for img in parsed.get("images", []):
        png_bytes = img.get("png_bytes")
        if png_bytes:
            binary_index[(img.get("page_number", 0), img.get("block_index", 0))] = png_bytes

    pages_out: List[Dict[str, Any]] = []
    for page in parsed.get("pages", []):
        page_number = page.get("page_number", 1)
        images_out: List[Dict[str, Any]] = []
        for img in page.get("images", []):
            block_index = img.get("block_index", 0)
            png_bytes = binary_index.get((page_number, block_index))
            image_info: Dict[str, Any] = {
                "image_id": f"{doc_id}_mineru_image_{page_number}_{block_index}",
                "document_id": doc_id,
                "filename": path.name,
                "page": page_number,
                "width": img.get("width", 0),
                "height": img.get("height", 0),
                "source": "mineru_block",
                "data": png_bytes,
                "is_circuit": None,
                "filter_reason": "",
                "bbox": img.get("bbox", []),
                "block_index": block_index,
                "captions": img.get("captions", []),
                "path": None,
            }
            if img_dir is not None and png_bytes:
                file_path = img_dir / f"{doc_id}_p{page_number}_b{block_index}.png"
                file_path.write_bytes(png_bytes)
                image_info["path"] = str(file_path)
            images_out.append(image_info)

        tables_out: List[Dict[str, Any]] = []
        for tbl in page.get("tables", []):
            rows = tbl.get("rows", [])
            col_count = max((len(r) for r in rows), default=0)
            empty_cells = sum(1 for r in rows for c in r if not str(c).strip())
            tables_out.append(
                {
                    "data": rows,
                    "page": page_number,
                    "source": "mineru",
                    "confidence": 1.0,
                    "row_count": len(rows),
                    "column_count": col_count,
                    "empty_cells": empty_cells,
                    "metadata": {
                        "bbox": tbl.get("bbox", []),
                        "format": tbl.get("content_format"),
                        "captions": tbl.get("captions", []),
                    },
                    "table_type": "mineru",
                    "header_keywords": [],
                    "is_cross_page": False,
                    "bbox": tbl.get("bbox", []),
                    "raw": tbl.get("raw_content", ""),
                    "text": tbl.get("plain_text", ""),
                }
            )

        pages_out.append(
            {
                "page_number": page_number,
                "text": page.get("text", ""),
                "full_text": page.get("full_text", ""),
                "tables": tables_out,
                "images": images_out,
                "metadata": {
                    "source": "mineru",
                    "image_count": len(images_out),
                    "table_count": len(tables_out),
                    "text_length": len(page.get("text", "")),
                    "block_count": len(page.get("blocks", []) or []),
                },
                "has_error": False,
                "error_message": None,
            }
        )

    # 回填图像二进制（parse_pdf 的 to_dict 默认不含二进制）
    return {
        "source": "mineru",
        "document_id": doc_id,
        "filename": path.name,
        "page_count": len(pages_out),
        "pages": pages_out,
        "markdown": parsed.get("markdown", ""),
        "markdown_clean": parsed.get("markdown_clean", ""),
        "structured_content": parsed.get("structured_content", {}),
        "meta": parsed.get("meta", {}),
        "errors": parsed.get("errors", []),
        "status": parsed.get("status", "success"),
    }


def get_mineru_client(settings: Any = None) -> MinerUClient:
    """构造 MinerUClient 的便捷函数（供上层按需调用）。"""
    return MinerUClient(settings=settings)
