"""
参数条件感知选值器
==================

把"用户问某个参数" → "确定性的数值证据"这一步从模型手里拿走。

为什么需要
----------
datasheet 里同一个参数常有多行、多个工况：

    漏源导通电阻 RDS(on)   VGS=-4.5 V, ID=-12 A    → max 12 mΩ
    漏源导通电阻 RDS(on)   VGS=-10 V,  ID=-14.9 A  → max 8.0 mΩ

裸问题"导通电阻最大值是多少"**本身有歧义**。让模型在多行里挑，
结果就是同一问题答案摇摆；而把"参数 + 条件 + 列"作为选择依据，
才能得到可复核的答案。

选择优先级（依次收窄，条件冲突必须显式处理，不能靠相关性盖过去）：

1. 参数身份（归一化符号 / 受控别名）——**按匹配质量定身份，不按候选记录数量**
2. 用户指定的测试条件 —— 三态：match / conflict / unknown
3. 目标列（Min / Typ / Max） —— **缺列即 missing_value，绝不用其他列顶替**
4. 多组互不相同的工况 → ``ambiguous``（即使问题指定了 max，也不代表工况已确定）
5. 剩余合法候选按相关度排序

返回状态（**调用方必须完整保留，不得统一折叠为"回退 LLM"**）：

- ``ok``                —— 唯一确定证据，可直接渲染答案
- ``ambiguous``         —— 多组工况，返回工况对照，请用户补充条件
- ``condition_conflict``—— 用户条件与所有记录冲突，只报告冲突，不给数值
- ``missing_value``     —— 参数命中但目标列缺失
- ``not_found``         —— 没有可核验证据
- ``unsupported_parameter`` —— 查询里的参数不在受控别名/符号体系内

设计原则：**宁可回答"无法核验"，也不给一个自信的错误数值。**
"""
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ================================================================ 归一化
# 注意：归一化只用于比对；证据里的原始文本必须原样保留给人看。

_UNICODE_MINUS = "−"      # U+2212 数学减号
_EN_DASH = "–"


def normalize_dashes(text: str) -> str:
    """把各种 Unicode 负号/短横统一成 ASCII '-'。"""
    t = (text or "").replace(_UNICODE_MINUS, "-").replace(_EN_DASH, "-")
    t = t.replace("—", "-")           # em dash
    return t


def _norm_for_alias_match(text: str) -> str:
    """别名匹配的**唯一**文本归一：连字符统一 + '-'→空格 + 小写 + 空白折叠。

    背景：别名/文本两侧的归一化曾散落在 5 处（有的转 '-' 有的不转、
    有的用裸 \\b），导致同一别名在不同调用点失配（连字符三连 bug，
    2026-11-24 g028/g035 回归）。所有别名-文本匹配必须经此函数，
    禁止调用点自行 replace/lower。
    """
    t = normalize_dashes(text or "").lower().replace("-", " ")
    return re.sub(r"\s+", " ", t).strip()


def alias_mentioned_in_text(alias: str, text: str, min_len: int = 5) -> bool:
    """判断别名是否被文本"提及"（词组级、前后非字母数字）。

    边界用 lookaround 而非裸 \\b：括号结尾的符号别名（RDS(on)/VGS(th)）
    在裸 \\b 下恒失配（')' 后非 word→非 word 无边界）。
    min_len 限制过短别名误命中（'f'/'id' 等短别名由中文门控与符号路径负责）。
    """
    if len(alias or "") < min_len:
        return False
    a = _norm_for_alias_match(alias)
    t = _norm_for_alias_match(text)
    if not a or not t:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", t) is not None


def alias_in_text_substring(alias: str, text: str) -> bool:
    """片段级包含：别名（归一后 ≥3 字符）出现在已归一文本中。"""
    a = _norm_for_alias_match(alias)
    t = _norm_for_alias_match(text)
    return bool(a) and len(a) >= 3 and a in t


def normalize_units(text: str) -> str:
    """统一单位写法：℃→°c、µ→μ(保留区分)、全角符号转半角。"""
    t = normalize_dashes(text)
    t = t.replace("℃", "°c").replace("°C", "°c").replace("°c", "°c")
    t = t.replace("µ", "μ")                # micro sign → greek mu
    t = unicodedata.normalize("NFKC", t)
    return t


def norm_symbol(text: str) -> str:
    """归一化参数符号：去空格与下标，I D → ID，R DS(on) → RDS(ON)。"""
    t = normalize_units(text or "").strip().lower()
    t = re.sub(r"<sub>(.*?)</sub>", r"\1", t)
    t = re.sub(r"<sup>(.*?)</sup>", r"\1", t)
    t = re.sub(r"\s+", "", t)
    return t


def norm_text(text: str) -> str:
    """归一化自由文本：统一符号后折叠空白、小写。"""
    t = normalize_units(text or "")
    return re.sub(r"\s+", " ", t).strip().lower()


_NUM_RE = re.compile(r"^([-+]?[\d.]+)\s*([a-zA-Zμ°%Ω/]*[\d]*)$")


def norm_number(text: str) -> Optional[str]:
    """归一化数值，便于 -10 V / −10V / V= -10 v 之间比较。

    返回 "<数值><单位小写>"，无单位时只返回数值；无法解析返回 None。
    """
    t = normalize_units(text or "").strip()
    if not t:
        return None
    m = _NUM_RE.match(t.replace(" ", ""))
    if not m:
        return None
    num, unit = m.group(1), m.group(2)
    try:
        num = "%g" % float(num)
    except ValueError:
        pass
    return f"{num}{unit.lower()}" if unit else num


# ================================================================ 查询解析
# 常见参数的中英别名（**注意区分 ID / IDSS**：不允许前缀模糊混同）
PARAM_ALIASES: Dict[str, List[str]] = {
    "连续漏极电流": ["continuous drain current"],
    "漏极电流": ["drain current", "continuous drain current"],
    "漏电流": ["drain current", "continuous drain current"],
    "栅极漏电流": ["gate source leakage current", "gate leakage current", "i gss", "igss"],
    "栅源漏电流": ["gate source leakage current", "gate leakage current", "i gss", "igss"],
    "脉冲漏极电流": ["pulsed drain current"],
    "击穿电压": ["breakdown voltage", "v(br)dss", "vbrdss"],
    "导通电阻": ["r ds(on)", "rds(on)", "on-state resistance", "on resistance"],
    "工作温度": ["operating and storage temperature", "operating temperature"],
    "结温": ["junction temperature"],
    "存储温度": ["storage temperature"],
    "供电电压": ["supply voltage", "v ds", "vds"],
    "栅源电压": ["gate source voltage", "v gs", "vgs"],
    "阈值电压": ["gate threshold voltage", "vgs(th)"],
    "输入电容": ["input capacitance", "c iss", "ciss"],
    "输出电容": ["output capacitance", "c oss", "coss"],
    "雪崩能量": ["avalanche energy", "e as", "eas"],
    "热阻": ["thermal resistance", "rthja", "rthjs", "rthjc", "rth(j-a)"],
    "开启延时": ["turn-on delay", "td(on)"],
    "上升时间": ["rise time"],
    "下降时间": ["fall time"],
    "增益带宽": ["gain bandwidth", "gbw"],
    "压摆率": ["slew rate"],
    "输入偏移电压": ["input offset voltage", "offset voltage", "v os", "vos"],
    "失调电压": ["input offset voltage", "offset voltage", "v os", "vos"],
    "静态电流": ["quiescent current", "i q", "iq"],
    "过载恢复时间": ["overload recovery time"],
    "电压输出": ["voltage output", "v out", "vout"],
    "输出电压摆幅": ["output voltage swing", "voltage output swing from rail"],
    "精度": ["accuracy"],
    "共模电压": ["common-mode voltage range", "common-mode", "v cm", "vcm"],
}

# 列类型关键词
_COL_MAX = ("最大", "上限", "最高", "max", "maximum", "upper")
_COL_MIN = ("最小", "下限", "最低", "min", "minimum", "lower")
_COL_TYP = ("典型", "typ", "typical")
_COL_RANGE = ("范围", "区间", "range", "between")

# 条件抽取：VGS=-10 V / ID = -14.9 A / TA=25 °C / VDS=-30V
_COND_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[Aa][Tt][ \t]+)?"
    r"(?P<key>[A-Za-z]{1,4}\s?(?:[A-Za-z]{0,2})?(?:\([A-Za-z]{1,4}\))?)\s*[=:]\s*"
    r"(?P<val>[-+]?[\d.]+\s*[A-Za-zμ°%Ω/]*[\d]*)"
)


@dataclass
class ParamQuery:
    """从自然语言问题里解析出的结构化查询意图。"""

    raw: str = ""
    param_keys: List[str] = field(default_factory=list)
    conditions: Dict[str, str] = field(default_factory=dict)
    column: Optional[str] = None                          # min / typ / max / range
    wants_extremum: bool = False
    column_label: str = ""                                # 自定义工况列（如 steady state）
    matched_zh: List[str] = field(default_factory=list)   # 命中的中文别名条目（多参数题 ≥2）


_ALIAS_ALL_EN = sorted({a.lower() for aliases in PARAM_ALIASES.values() for a in aliases if len(a) >= 3},
                       key=len, reverse=True)


def looks_like_param_query(query: str) -> bool:
    """英文参数题判定：包含任一受控参数的英文别名（≥3 字符）。

    之前该门控只有中文标记，导致 golden 评测里的英文题全部绕过
    Resolver、走回 LLM 自由生成——修好的取数链路对英文题不生效。
    别名边界用 lookaround 而非裸 \\b：括号结尾的符号别名
    （RDS(on)/VGS(th)）在裸 \\b 下恒失配（`)` 后非 word→非 word 无边界）。
    """
    q = _norm_for_alias_match(query)
    if not q:
        return False
    for zh in PARAM_ALIASES:
        if zh in query:
            return True
    return any(
        alias_mentioned_in_text(a, query)
        for a in _ALIAS_ALL_EN
    )


# 条件键封闭集（norm_symbol 归一后）：电气参数表的测试条件键有限，
# 超出集合的"键"几乎都是 MinerU 多栏字符交错产生的碎片（2026-11-24 实测：
# 'VGS=0 V'×'VDS=-25 V' 逐字符交错成 'VVGDSS==-02 V5'，standard 档同样复现）。
_KNOWN_COND_KEYS = {
    "vgs", "vds", "id", "idss", "vdd", "vg", "ta", "tj", "f", "rg", "rgs",
    "v", "i", "r", "t", "g", "t a", "t j", "v gs", "v ds", "i d",
}


def _looks_garbled(cond: str) -> bool:
    """检测测试条件串是否为 MinerU 字符交错产物。

    三条规则：①含 '==' 连等（正常键值对不会）；②'=' 前的键簇归一化后
    不在封闭键集合；③右括号悬空（如 'T A=25 °C1)' 的行号残渣）。
    乱码条件在匹配层强制 unknown（防误判 conflict）、在渲染层降级提示，
    conditions_raw 原文保留不动（MinerU 修复后自然恢复）。
    """
    if not cond:
        return False
    if "==" in cond:
        return True
    if cond.count(")") > cond.count("("):
        return True
    for k in re.findall(r"([A-Za-z][A-Za-z0-9 ()]{0,5}?)\s*[=:]", cond):
        nk = norm_symbol(k)
        if nk and nk not in _KNOWN_COND_KEYS:
            return True
    return False


def _salvage_condition(cond: str) -> str:
    """从乱码条件串里打捞**可信片段**（键值对逐片段独立校验）。

    交错整体不可逆（节奏不规则，字符级拆分会产出似是而非的错误条件），
    但逗号分隔的片段常常有整段幸存者：'VVGDSS==-02 V5, V, f=1 MHz'
    里 'f=1 MHz' 完全可靠；'T A=25 °C1)' 修剪掉行号残渣 '1)' 后即为
    'T A=25 °C'。全部片段不可信时返回空串，调用方降级为"见原文"。
    """
    if not cond:
        return ""
    good: List[str] = []
    for raw in re.split(r"[,;，；]", cond):
        frag = raw.strip()
        if not frag:
            continue
        # 行号残渣尾巴修剪：'T A=25 °C1)' → 'T A=25 °C'
        frag = re.sub(r"\s*\d{1,3}\)\s*$", "", frag).strip()
        if "=" not in frag or _looks_garbled(frag):
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9 ()]{0,5}?)\s*[=:]\s*(.+)$", frag)
        if not m or norm_symbol(m.group(1)) not in _KNOWN_COND_KEYS:
            continue
        if frag not in good:
            good.append(frag)
    return ", ".join(good)


def query_has_param_signal(query: str) -> bool:
    """查询里是否含任何参数词信号（中文别名子串或英文别名提及）。

    供工作流守卫使用：parse_param_query 得不到 param_keys 且本函数
    为 False 时，说明查询（如"封装是什么/版本号/价格"）没有参数语义，
    应回退 LLM 而非让 Resolver 在无信号状态下靠符号撞表乱配。
    """
    text = query or ""
    if not text:
        return False
    for zh in PARAM_ALIASES:
        if zh in text:
            return True
    qnorm = _norm_for_alias_match(text)
    return any(
        alias_mentioned_in_text(a, text)
        for a in _ALIAS_ALL_EN
    )


def parse_param_query(query: str) -> ParamQuery:
    """把自然语言问题解析为结构化查询意图（规则优先，不调用模型）。"""
    pq = ParamQuery(raw=query or "")
    q_lower = normalize_units(query or "").lower()

    keys: List[str] = []
    for zh, aliases in PARAM_ALIASES.items():
        if zh in query:
            keys.extend(aliases)
            pq.matched_zh.append(zh)
    # 显式符号：I D / RDS(on) / V(BR)DSS / VGS
    for m in re.finditer(
        r"\b[A-Za-z]{1,4}\s?\([A-Za-z]{1,4}\)\s?[A-Za-z]{0,3}"
        r"|\b[A-Z]{1,3}\s?[A-Z]{0,2}\d[A-Za-z0-9()]*",
        query,
    ):
        s = norm_symbol(m.group())
        raw = m.group()
        # 参数符号未必含数字：RDS(on) / VGS / V(BR)DSS
        if s and len(s) >= 2 and (any(ch.isdigit() for ch in s) or "(" in s or raw.isupper()):
            # 器件名形态（OPA620/NE5532/LM358）是器件标识而非参数符号：
            # 若参与参数匹配，会把"参数名里提到器件名"的行全部误配
            if not re.fullmatch(r"[a-z]{1,6}\d{2,}[a-z0-9]*", s):
                keys.append(s)
    pq.param_keys = list(dict.fromkeys(keys))[:10]

    # 条件解析在**归一化后的文本**上做：否则 "TA=25℃" 的 ℃ 不在字符类里，
    # 会被截成 "25"，与证据中的 "25°c" 恒不相等 → 误判条件冲突
    for m in _COND_RE.finditer(normalize_units(query or "")):
        key = norm_symbol(m.group("key"))
        val = norm_number(m.group("val"))
        if key and val:
            pq.conditions[key] = val

    if any(k in q_lower for k in _COL_RANGE):
        pq.column = "range"
    elif any(k in q_lower for k in _COL_MAX):
        pq.column = "max"
        pq.wants_extremum = True
    elif any(k in q_lower for k in _COL_MIN):
        pq.column = "min"
        pq.wants_extremum = True
    elif any(k in q_lower for k in _COL_TYP):
        pq.column = "typ"

    # 自定义工况列（如 steady state / 稳态 / ≤10 secs / 脉冲）
    for label in ("steady state", "稳态", "pulse", "脉冲", "10 secs", "10秒"):
        if label in q_lower:
            pq.column_label = label
            break

    return pq


# ================================================================ 证据
@dataclass
class ParamEvidence:
    """一条候选参数记录（来自表格结构化 / 反序列化）。"""

    param: str = ""
    symbol: str = ""
    conditions: str = ""            # 展示用（结构化后）
    conditions_raw: str = ""        # 原始条件文本，匹配优先用它
    values: Dict[str, str] = field(default_factory=dict)   # min/typ/max/自定义列标签
    unit: str = ""
    page: int = 0
    table_id: str = ""
    row_index: int = 0
    score: float = 0.0
    source_text: str = ""
    raw_row: str = ""
    param_garbled: bool = False     # param 字段疑似交错乱码（symbol 通常完好）
    valid: bool = True              # 反序列化校验失败 → False

    @classmethod
    def from_record(cls, rec: Any, score: float = 0.0) -> "ParamEvidence":
        values: Dict[str, str] = {}
        for key in ("min", "typ", "max"):
            v = getattr(rec, key, None)
            if v:
                values[key] = v
        for k, v in getattr(rec, "value_columns", None) or []:
            values[k] = v
        for i, v in enumerate(getattr(rec, "extra_values", None) or []):
            values.setdefault(f"值{i + 1}", v)
        return cls(
            param=getattr(rec, "param", "") or "",
            symbol=getattr(rec, "symbol", "") or "",
            param_garbled=bool(getattr(rec, "param_garbled", False)),
            conditions=getattr(rec, "conditions", "") or "",
            conditions_raw=getattr(rec, "conditions_raw", "") or getattr(rec, "conditions", "") or "",
            values=values,
            unit=getattr(rec, "unit", "") or "",
            page=int(getattr(rec, "page", 0) or 0),
            table_id=str(getattr(rec, "table_id", "") or ""),
            row_index=int(getattr(rec, "row_index", 0) or 0),
            score=score,
            source_text=getattr(rec, "to_text", lambda: "")() if hasattr(rec, "to_text") else "",
            raw_row=getattr(rec, "raw_row", "") or "",
        )

    def condition_haystacks(self) -> List[str]:
        """用于条件匹配的文本源：原始行 > 原始条件 > 结构化条件。

        额外提供**去空白**的紧凑形式：表格里常写成 ``T A=25 °C``，
        按原样匹配会找不到 ``ta`` 键（P2 类问题的根因）。
        """
        out = []
        for text in (self.raw_row, self.conditions_raw, self.conditions):
            n = norm_text(text)
            if not n:
                continue
            if n not in out:
                out.append(n)
            compact = re.sub(r"\s+", "", n)
            if compact not in out:
                out.append(compact)
        return out

    def describe(self) -> str:
        parts = [f"参数：{self.param}"]
        if self.symbol:
            parts.append(f"符号：{self.symbol}")
        if self.conditions:
            parts.append(f"条件：{self.conditions}")
        vals = "；".join(f"{k}={v}" for k, v in self.values.items())
        if vals:
            parts.append(f"数值：{vals}")
        if self.unit:
            parts.append(f"单位：{self.unit}")
        parts.append(f"来源：第{self.page}页 表{self.table_id} 第{self.row_index}行")
        return " ".join(parts)


@dataclass
class Selection:
    """选值结果。status 必须由调用方完整保留。"""

    status: str = "not_found"
    value: Optional[str] = None
    unit: str = ""
    evidence: Optional[ParamEvidence] = None
    alternatives: List[ParamEvidence] = field(default_factory=list)
    reason: str = ""
    column_hint: str = ""

    @staticmethod
    def _display_name(ev: ParamEvidence) -> str:
        """证据的身份展示名：param 交错乱码时用 symbol 兜底（单行格完好）。"""
        if getattr(ev, "param_garbled", False) and ev.symbol:
            return ev.symbol
        return ev.param or ev.symbol

    def _render_evidence(self, ev: ParamEvidence) -> str:
        vals = "；".join(f"{k}={v}" for k, v in ev.values.items())
        cond = ev.conditions_raw or ev.conditions or "（文档未标注工况）"
        if cond and _looks_garbled(cond):
            salvaged = _salvage_condition(cond)
            cond = (
                f"（部分条件：{salvaged}；完整工况见来源页）"
                if salvaged
                else "（条件原文有乱码，见来源页）"
            )
        unit = f" {ev.unit}" if ev.unit else ""
        return (f"- 条件 {cond}：{vals}{unit}"
                f"（第{ev.page}页 表{ev.table_id} 第{ev.row_index}行）")

    def render(self) -> str:
        col_cn = _COL_CN.get(self.column_hint, "数值")

        if self.status == "ok" and self.evidence:
            ev = self.evidence
            cond = ev.conditions_raw or ev.conditions
            if cond and _looks_garbled(cond):
                # MinerU 字符交错：能打捞的可信片段照常回显，其余指引原文
                salvaged = _salvage_condition(cond)
                cond_s = (
                    f"（条件：{salvaged}；完整工况见原文对应表行）"
                    if salvaged
                    else "（测试条件见原文对应表行）"
                )
            else:
                cond_s = f"（条件：{cond}）" if cond else ""
            unit = f" {self.unit}" if self.unit else ""
            src = f"〔第{ev.page}页 表{ev.table_id} 第{ev.row_index}行〕"
            return f"{self._display_name(ev)} 的{col_cn}为 {self.value}{unit}{cond_s} {src}"

        if self.status == "ambiguous":
            lines = [f"{self.reason or '该参数在文档中有多组测试条件，请指明工况'}："]
            lines += [self._render_evidence(ev) for ev in self.alternatives[:6]]
            return "\n".join(lines)

        if self.status == "condition_conflict":
            lines = [f"{self.reason or '所给测试条件与文档记录不一致，无法给出确定数值'}："]
            lines += [self._render_evidence(ev) for ev in self.alternatives[:6]]
            return "\n".join(lines)

        if self.status == "missing_value":
            ev = self.evidence
            where = (f"（第{ev.page}页 表{ev.table_id} 第{ev.row_index}行）" if ev else "")
            extra = ""
            if ev and ev.values:
                listed = "；".join(f"{k}={v}" for k, v in ev.values.items())
                extra = f"。文档给出的数值列为：{listed}"
            return f"找到该参数记录，但文档未给出{col_cn}{where}{extra}。"

        if self.status == "unsupported_parameter":
            return self.reason or "该问题涉及的参数不在系统支持的结构化参数范围内，无法核验。"

        return self.reason or "文档中未找到可核验的参数记录。"


_COL_CN = {"min": "最小值", "typ": "典型值", "max": "最大值", "range": "取值范围"}


# ================================================================ 匹配
def _param_match(pq: ParamQuery, ev: ParamEvidence) -> float:
    """参数身份匹配得分（0 表示不匹配）。三层结构：

    1) 符号严格匹配（**无条件**，不分语言）：受控别名归一化 == 记录符号。
       g035 教训：英文题 "gain bandwidth" 曾因中文触发门完全绕过符号匹配。
    2) 参数名模糊匹配（需参数词线索：查询含中文别名词或英文别名）。
    3) 查询显式符号包含于长符号（如 ID ⊂ IDSS 仅在长符号时放行）。

    另：参数名以数字开头（"100MHz GBW, RRIO, CMOS"）是对比表垃圾行，
    不得参与身份匹配。
    """
    par = norm_text(ev.param)
    sym = norm_symbol(ev.symbol)
    sym_ok = bool(sym) and sym not in ("-", "--", "n/a") and len(sym) >= 2

    if par and par[0].isdigit():
        return 0.0

    # 查询提及的参数别名（提前构造，供第 1 层降档使用）。
    mentioned = set()
    for zh, aliases in PARAM_ALIASES.items():
        if zh in pq.raw:
            mentioned.update(aliases)
    for a in _ALIAS_ALL_EN:
        # 单点归一匹配：裸 \b（括号失配）与连字符归一缺失（common-mode
        # 失配）两类历史 bug 都已收敛进 alias_mentioned_in_text
        if alias_mentioned_in_text(a, pq.raw):
            mentioned.add(a)
    mentioned_norm = {norm_symbol(a) for a in mentioned}

    best = 0.0

    # 1) 符号严格匹配：仅当**查询提及**该别名（mentioned_norm）时才计分。
    #    实测教训（2026-11-24 十四题回归）：放开"未提及参数的符号撞表"
    #    （无论 1.0 还是降档 0.70）都会让热阻/封装/版本号/栅极漏电流类
    #    查询错配到恰好撞表的记录（avalanche 'eas'），身份候选被噪声
    #    独占后 missing_value 文不对题。未提及即不参与身份候选；
    #    仅写符号的查询由第 4 层（param_keys 符号相等 0.95）覆盖，
    #    g035 英文词组题由第 2 层（mentioned 模糊匹配）覆盖，无场景损失。
    for aliases in PARAM_ALIASES.values():
        for a in aliases:
            na = norm_symbol(a)
            if na and sym_ok and na == sym and na in mentioned_norm:
                best = max(best, 1.0)
                break

    # 2) 参数名模糊匹配（需线索，且别名必须被**查询提及**）
    #    只校验"别名出现在记录里"会让用户没问的参数撞车：
    #    查 gain bandwidth 时，THERMAL SHUTDOWN 行因含受控别名
    #    "junction temperature" 而被误配（g035 实测）。
    if mentioned:
        # 匹配面扩展到原始行/条件/symbol；连字符归一由
        # alias_in_text_substring 单点处理（'Gain-bandwidth'/'gain
        # bandwidth' 等价）。symbol 显式纳入：MinerU 折行格的 param 字段
        # 可能交错乱码（热阻行 'jTuhnecrtmioanl...'），symbol 是单行格
        # 几乎总完好（RthJA），是乱码行的身份兜底。
        par_ext = (
            (ev.param or "") + " " + (ev.symbol or "") + " "
            + (ev.raw_row or "") + " " + (ev.conditions_raw or "")
        )
        for a in sorted(mentioned, key=len, reverse=True):
            na = norm_symbol(a)
            if na and sym_ok and na == sym:
                best = max(best, 1.0)
            else:
                if a and alias_in_text_substring(a, par_ext):
                    best = max(best, 0.80 + 0.20 * min(len(a), 30) / 30.0)

    # 3) 查询显式符号包含于长符号
    for k in pq.param_keys:
        kk = norm_symbol(k)
        if kk and sym_ok and len(sym) >= 5 and len(kk) >= 4 and kk in sym:
            best = max(best, 0.85)

    # 4) 查询显式符号 == 记录符号（如查询 "ID"、记录符号 ID）
    keys_sym = [norm_symbol(k) for k in pq.param_keys if len(norm_symbol(k)) >= 2]
    if sym_ok and any(k == sym for k in keys_sym):
        best = max(best, 0.95)

    return best


# 条件匹配三态
COND_MATCH = "match"
COND_CONFLICT = "conflict"
COND_UNKNOWN = "unknown"


def _condition_match(pq: ParamQuery, ev: ParamEvidence) -> str:
    """条件匹配：match / conflict / unknown。

    - 用户给出的每个条件，必须能在证据里找到**对应数值**
    - 找不到键、或键后无数值（残缺条件）→ unknown（**不能当作匹配**）
    - 找到但数值不同 → conflict
    """
    if not pq.conditions:
        return COND_MATCH

    haystacks = ev.condition_haystacks()
    if not haystacks:
        return COND_UNKNOWN
    # 乱码条件不可信：交错串可能让某键"恰好匹配"出假 match/conflict。
    # 打捞出的可信片段仍可参与匹配（如 f=1 MHz），其余键视为未核验；
    # 整串无可信片段 → unknown（用户会看到"无法核验"而不是错数）。
    if ev.conditions_raw and _looks_garbled(ev.conditions_raw):
        salvaged = _salvage_condition(ev.conditions_raw)
        if not salvaged:
            return COND_UNKNOWN
        haystacks = [norm_text(salvaged)]

    any_unknown = False
    for key, val in pq.conditions.items():
        k = norm_symbol(key)
        v = norm_number(val)
        if not v:
            continue
        found_value = None
        key_seen = False
        for hay in haystacks:
            for m in re.finditer(re.escape(k) + r"\s*[=:]?\s*([-+]?[\d.]+\s*[a-zμ°%ω/]*)", hay):
                key_seen = True
                got = norm_number(m.group(1))
                if got is None:
                    continue
                found_value = got
                break
            if found_value is not None:
                break
        if found_value is None:
            # 键出现但无数值，或整条证据都没有该键 → 无法核验
            any_unknown = True
            continue
        if found_value != v:
            return COND_CONFLICT
    return COND_UNKNOWN if any_unknown else COND_MATCH


# 工况列标签的中英别名（表头可能是 "steady state"，用户用中文问"稳态"）
_LABEL_GROUPS = [
    ("steady", ("steady", "稳态", "稳定态")),
    ("pulse", ("pulse", "脉冲", "secs", "秒", "10 s")),
    ("min", ("min", "最小")),
    ("typ", ("typ", "typical", "典型")),
    ("max", ("max", "最大")),
]


def _label_aliases(label: str) -> List[str]:
    """把用户写法扩展成同组别名集合。"""
    n = norm_text(label)
    for _, group in _LABEL_GROUPS:
        if any(g in n for g in group):
            return [g for g in group]
    return [n]


def _column_value(ev: ParamEvidence, pq: ParamQuery) -> Optional[str]:
    """按目标列取数值。**缺列即 None，绝不用其他列顶替。**"""
    if not ev.values:
        return None

    # 1) 自定义工况列（steady state / 脉冲 等）：按标签别名定位
    if pq.column_label:
        wanted = _label_aliases(pq.column_label)
        for k, v in ev.values.items():
            k_norm = norm_text(k)
            if any(a in k_norm for a in wanted):
                return v

    column = pq.column
    if column in ("min", "typ", "max"):
        return ev.values.get(column)

    if column == "range":
        lo = ev.values.get("min")
        hi = ev.values.get("max")
        if lo and hi:
            return f"{lo} ~ {hi}"
        return None

    # 未指定列：按 max → typ → min 顺序取（这是展示偏好，不是跨列猜测）
    for key in ("max", "typ", "min"):
        if ev.values.get(key):
            return ev.values[key]
    # 无标准列但自定义工况列的**非空值唯一**时取它——雪崩能量等
    # "单工况自定义列"参数的兜底（≤10 secs 与 steady state 同为 248 mJ
    # 时不再 missing_value 误拒；两列值不同则仍拒绝，保持歧义诚实）
    custom_vals = {
        v for k, v in (ev.values or {}).items()
        if k not in ("min", "typ", "max") and v
    }
    if len(custom_vals) == 1:
        return next(iter(custom_vals))
    return None


def _pick_param_identity(scored: List[Tuple[float, ParamEvidence]]) -> Tuple[str, List[Tuple[float, ParamEvidence]]]:
    """按**匹配质量**确定参数身份（不按候选记录数量）。

    同一身份 = 归一化符号相同；无符号时退回归一化参数名。
    """
    groups: Dict[str, List[Tuple[float, ParamEvidence]]] = {}
    for s, ev in scored:
        ident = norm_symbol(ev.symbol) or norm_text(ev.param)
        groups.setdefault(ident, []).append((s, ev))

    def best_score(items):
        return max(s for s, _ in items)

    best_ident = max(groups, key=lambda k: best_score(groups[k]))
    return best_ident, groups[best_ident]


def select_value(pq: ParamQuery, evidences: List[ParamEvidence],
                 skip_identity: bool = False) -> Selection:
    """按优先级选值，返回带状态的 Selection。

    skip_identity=True：跳过别名身份匹配，把全部证据视为同一身份组，
    直接进入条件三态与列选择。仅供**语义兜底提名**路径使用（调用方
    已用检索分数提名了最相关记录并对答案做降置信标注）。
    """
    usable = [ev for ev in evidences if getattr(ev, "valid", True)]
    if not usable:
        return Selection(status="not_found", reason="文档中未找到可核验的参数记录。")

    # 1) 参数身份
    if skip_identity:
        group = [(1.0, ev) for ev in usable]
        if not pq.param_keys and not pq.conditions:
            # 无任何参数/条件线索的裸查询不宜直接选值（提名路径的调用方
            # 已保证至少有检索分数信号，这里防御性兜底）
            group = group[:1]
    else:
        scored = [(s, ev) for s, ev in ((_param_match(pq, ev), ev) for ev in usable) if s > 0]
        if not scored:
            if pq.param_keys:
                return Selection(
                    status="unsupported_parameter",
                    reason="该问题涉及的参数在文档的结构化参数记录中没有匹配项（可能不在受控参数范围内）。",
                )
            return Selection(status="not_found", reason="文档中未找到该参数的可核验记录。")

        _, group = _pick_param_identity(scored)

    # 2) 条件三态
    matched, conflicted, unknown = [], [], []
    for s, ev in group:
        state = _condition_match(pq, ev)
        {"match": matched, "conflict": conflicted, "unknown": unknown}[state].append((s, ev))

    if pq.conditions:
        if not matched and conflicted:
            return Selection(
                status="condition_conflict",
                reason="所给测试条件与文档记录不一致",
                alternatives=[ev for _, ev in sorted(conflicted, key=lambda x: -x[0])],
                column_hint=pq.column or "",
            )
        if not matched and unknown and not conflicted:
            # 条件无法核验（记录条件残缺）→ 不得给确定数值
            return Selection(
                status="ambiguous",
                reason="文档记录的测试条件不完整，无法按所给条件核验，请参考以下工况",
                alternatives=[ev for _, ev in sorted(unknown, key=lambda x: -x[0])],
                column_hint=pq.column or "",
            )
        pool = matched if matched else group
    else:
        pool = group

    # 3) 多组互不相同的工况 → 歧义（**即使问题指定 max，也不代表工况已确定**）
    distinct = {
        (norm_symbol(ev.symbol), norm_text(ev.conditions_raw or ev.conditions))
        for _, ev in pool
    }
    if len(distinct) > 1:
        uniq: List[ParamEvidence] = []
        seen = set()
        for _, ev in sorted(pool, key=lambda x: -x[0]):
            key = (norm_symbol(ev.symbol), norm_text(ev.conditions_raw or ev.conditions))
            if key not in seen:
                seen.add(key)
                uniq.append(ev)
        return Selection(
            status="ambiguous",
            reason="该参数在文档中有多组测试条件，请指明工况",
            alternatives=uniq,
            column_hint=pq.column or "",
        )

    # 4) 单一工况：取目标列
    pool.sort(key=lambda x: -x[0])
    _, ev = pool[0]
    val = _column_value(ev, pq)
    if not val:
        return Selection(
            status="missing_value",
            evidence=ev,
            column_hint=pq.column or "",
        )
    # 按自定义工况列取值时，措辞用"数值"而不是"最大值"
    hint = pq.column or ("" if pq.column_label else "max")
    return Selection(status="ok", value=val, unit=ev.unit, evidence=ev,
                     column_hint=hint)


# ================================================================ 多参数题
def select_values_multi(pq: ParamQuery, evidences: List[ParamEvidence]
                        ) -> Optional[Tuple[str, List[Selection]]]:
    """多参数题（如"上升时间和下降时间分别是多少"）：逐参数独立选值。

    单参数结构假设下，param_keys 是全部命中别名的并集，身份选择只会
    选出一个组，其余参数被静默丢弃（实测 Fall time 丢失）。这里按
    matched_zh 逐条目构造子查询分别 select_value，渲染段用换行拼接。
    全部条目都 not_found 时返回 None，调用方落回单参数路径给出统一文案。
    """
    if len(getattr(pq, "matched_zh", None) or []) < 2:
        return None
    sels: List[Selection] = []
    for zh in pq.matched_zh[:4]:
        sub = ParamQuery(
            # raw 只放本条目：第 2 层提及门控按 raw 做中文子串匹配，保留
            # 完整问句会让兄弟条目的别名也进 mentioned，两组并列分后按
            # 插入序重复选同一身份（实测两段全 Rise time）。conditions/
            # column 已在完整问句上解析好，直接继承。
            raw=zh,
            param_keys=list(PARAM_ALIASES.get(zh, [])),
            conditions=dict(pq.conditions),
            column=pq.column,
            wants_extremum=pq.wants_extremum,
            column_label=pq.column_label,
            matched_zh=[zh],
        )
        sels.append(select_value(sub, evidences))
    renders = [s.render() for s in sels if s.status != "not_found"]
    if not renders:
        return None
    return "\n".join(renders), sels
