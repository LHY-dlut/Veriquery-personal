"""Find explicit datasheet statements for tightly specified fact questions.

This consumes retrieved passages and returns visible evidence. It does not
infer values from unlabeled cells or choose among conflicting specifications.
"""

from dataclasses import dataclass
from decimal import Decimal
import re
import unicodedata


_PART = re.compile(r"\b(?:[a-z]{2,}\d[a-z0-9-]*|\d{2}[a-z]{2,}\d[a-z0-9-]*)\b", re.I)
_NUM = r"[±+\-−]?\d+(?:\.\d+)?"
_VOLTAGE = r"(?:[µμu]V|mV|V)(?![a-z/])"


@dataclass(frozen=True)
class FactRule:
    name: str
    question: re.Pattern
    evidence: re.Pattern
    abstain_without_match: bool = False


def _rule(name, question, evidence, abstain_without_match=False):
    return FactRule(name, re.compile(question, re.I), re.compile(evidence, re.I), abstain_without_match)


_RULES = (
    _rule(
        "auto_zero_offset",
        r"(?=.*(?:auto.zero|自动调零))(?=.*(?:offset voltage|失调电压))",
        rf"\bauto[- ]zeroing techniques?\s+to provide low offset voltage\s*"
        rf"\((?P<value>{_NUM}[\s-]*{_VOLTAGE})\s*(?:maximum|max)\)"
        r"\s+and\s+near\s+zero[- ]drift\s+over time and temperature",
    ),
    _rule(
        "offset_maximum",
        r"(?=.*(?:maximum|\bmax\b|最大))(?=.*(?:input offset voltage|输入失调电压))",
        rf"\b(?:low |input )?offset voltage\s*[:(]?\s*"
        rf"(?P<value>{_NUM}[\s-]*{_VOLTAGE})\s*\(?(?:maximum|max)\b\)?",
    ),
    _rule(
        "converter_resolution",
        r"(?=.*(?:resolution|分辨率))(?=.*(?:\b(?:ADC|DAC)\b|analog.to.digital|digital.to.analog|模数|数模))",
        r"\b(?P<value>\d{1,2}[- ]bit)\s*(?:"
        r"resolution\b|(?:(?:ΔΣ|SAR|delta.sigma)\s+)?(?:ADCs?|DACs?)\b|"
        r",\s*(?:I2C.compatible,\s*)?analog.to.digital converters?\b)",
    ),
    _rule(
        "temperature_result_width",
        r"(?:temperature (?:result|data) (?:width|bit width)|温度(?:结果|数据)位宽)",
        r"\b(?P<value>\d{1,2}[- ]bit)\s+temperature (?:result|data)\b",
    ),
    _rule(
        "temperature_resolution",
        r"(?:temperature resolution|温度分辨率)",
        rf"\btemperature result with a resolution of\s*(?P<value>{_NUM}\s*°C)\b",
    ),
    _rule(
        "data_rate_options",
        r"(?=.*(?:\bdata rates\b|采样率|数据速率))(?=.*(?:support|list|available|支持|列出|可选))"
        r"(?!.*\b(?:typical|minimum|maximum|min|max|default)\b)(?!.*(?:最大|最小|典型|默认))",
        r"\b(?:DR\s*\|\s*)?data rate\s*\|\s*(?:\|\s*)*(?:enumerated options:\s*)?"
        r"(?P<value>\d+(?:\s*,\s*\d+){2,}\s*\|\s*(?:SPS|kSPS))\b",
    ),
    _rule(
        "page_write_size",
        r"(?:page[- ]write (?:size|buffer)|页写(?:大小|缓冲区|容量))",
        r"\b(?P<value>\d+)[- ]byte\s+page[- ]write\s+buffer\b",
    ),
    _rule(
        "memory_capacity",
        r"(?:memory capacity|存储容量)",
        r"\b\d+\s*K?\s*[x×]\s*\d+\s*\((?P<value>\d+[- ]?\s*[KMG]?bit)\)\s*"
        r"(?:serial\s+electrically\s+erasable\s+PROM\s*\(EEPROM\)|(?:serial\s+)?EEPROM)\b",
    ),
    _rule(
        "altitude_limit",
        r"\b(?:operating|operation)\s+altitude\b|工作海拔",
        rf"\b(?:maximum\s+)?(?:operating|operation)\s+altitude\s*[:=]?\s*"
        rf"(?:up to\s+)?(?P<value>{_NUM}\s*(?:m|ft|feet|meters))\b",
        abstain_without_match=True,
    ),
    _rule(
        "cpu_cores",
        r"\bCPU cores?\b|CPU\s*核(?:心|数)",
        r"\b(?P<value>\d+)\s+CPU cores?\b",
        abstain_without_match=True,
    ),
    _rule(
        "shunt_monitor_inputs",
        r"\bwhat does\b.+\bmeasure\b|测量哪些",
        r"\b[a-z0-9-]+\s+is\s+a\s+(?P<value>current shunt and power monitor)"
        r"\s+with\s+[^.\n]{1,95}\.\s*The device monitors both\s+"
        r"a shunt voltage drop and bus supply voltage\b",
    ),
)
_ABSENCE_TERMS = {
    "altitude_limit": re.compile(r"\baltitude\b|\bsea level\b|海拔", re.I),
    "cpu_cores": re.compile(r"\bcores?\b|\bCortex\b|核心", re.I),
}

_GAIN_LABEL = r"gain[\s-]+bandwidth\s+product"
_GAIN_BANDWIDTH = (
    rf"\b{_GAIN_LABEL}\b|(?<![A-Za-z0-9_])(?:GBW|GBP|GBWP)(?![A-Za-z0-9_])"
    r"|增益\s*[-–−]?\s*带宽(?:积|乘积)|增益带宽"
)
_OPERATING_SUPPLY = (
    r"\boperating\s+(?:power[\s-]*)?supply(?:\s+voltage)?\s+range\b"
    r"|(?:电源|供电)(?:电压)?范围"
)
_CONDITION_PART = re.compile(_PART.pattern, re.I | re.ASCII)
_TEMP_RANGE = rf"{_NUM}\s*°\s*C\s+to\s+{_NUM}\s*°\s*C"
_VOLT_RANGE = rf"{_NUM}\s*V\s+to\s+{_NUM}\s*V"
_SEPARATOR = r"(?:[,;]\s*(?:or\s+|and\s+)?|\s+(?:or|and)\s+|\s+[–—•-]\s*)"
_PAIR_SEPARATOR = re.compile(_SEPARATOR, re.I)
_GAIN_VALUE = r"{value}\s*MHz\s*\(\s*G\s*=\s*{condition}\s*\)"
_COLSPAN = r"(?:\s+\[跨\d+列合并\])?"
_GAIN_TABLE_ROW = (
    r"\bGBW\s*\|\s*Gain[\s-]+bandwidth\s+product\s*\|\s*G\s*=\s*{condition}"
    + _COLSPAN + r"\s*\|(?:\s*\|)*\s*{value}" + _COLSPAN + r"\s*\|(?:\s*\|)*\s*MHz\b"
)
_SUPPLY_VALUE = (
    r"(?:V\s*\+\s*=\s*)?{value}\s+(?:from|for\s+T\s*A\s*=)\s+{condition}"
)


def _pair(template, value, condition, separator=_SEPARATOR):
    first = template.format(value=rf"(?P<value1>{value})", condition=rf"(?P<condition1>{condition})")
    second = template.format(value=rf"(?P<value2>{value})", condition=rf"(?P<condition2>{condition})")
    return first + separator + second


_CONDITION_RULES = (
    _rule(
        "gain_bandwidth_by_gain",
        _GAIN_BANDWIDTH,
        r"\bgain[\s-]+bandwidth\s+product\s*:\s*(?:[–—•-]\s*)?"
        + _pair(_GAIN_VALUE, _NUM, _NUM),
    ),
    _rule(
        "supply_range_by_temperature",
        _OPERATING_SUPPLY,
        r"\b(?:supply\s+(?:voltage\s+)?range\s*:\s*(?:[–—•-]\s*)?"
        r"|over\s+free[\s-]+air\s+temperature\s+range\s+and\s+(?=V\s*\+\s*=))"
        + _pair(_SUPPLY_VALUE, _VOLT_RANGE, _TEMP_RANGE),
    ),
)
_GAIN_TABLE = re.compile(_pair(_GAIN_TABLE_ROW, _NUM, _NUM, r"\s+"), re.I)
_PAIR_CONTINUATION = {
    "gain_bandwidth_by_gain": re.compile(_SEPARATOR + _GAIN_VALUE.format(value=_NUM, condition=_NUM), re.I),
    "supply_range_by_temperature": re.compile(
        _SEPARATOR + _SUPPLY_VALUE.format(value=_VOLT_RANGE, condition=_TEMP_RANGE), re.I,
    ),
}
_CONDITION_SEARCH_TERMS = {
    "gain_bandwidth_by_gain": "gain bandwidth product GBW",
    "supply_range_by_temperature": "supply range voltage temperature",
}
_UNSUPPORTED_CONDITION_TOPIC = re.compile(
    r"\d|\b(?:maximum|minimum|max|min|typical|typ|absolute|single|dual|unity|at|under|with|for)\b"
    r"|\b(?:and|or|compare|versus|vs|why|how)\b|最[大小]|典型|绝对|单电源|双电源"
    r"|单位增益|条件下|以及|对比|比较|为什么",
    re.I,
)
_GAIN_NUMBER = r"[+\-−–]?\d+(?:\.\d+)?"
_GAIN_VARIABLE = r"(?:G|A\s*(?:_\s*)?(?:\{\s*V\s*\}|V))"
_GAIN_REQUEST = re.compile(
    rf"(?:\b(?:at|with|for|when|under)\s+)?(?<![A-Za-z0-9_])(?:{_GAIN_VARIABLE}|gain|增益)"
    rf"\s*(?:=|为)\s*(?P<gain>{_GAIN_NUMBER})(?![\d.])"
    r"(?:\s*V\s*/\s*V)?(?:\s*(?:时|下|条件下|工况下))?",
    re.I,
)
_GAIN_TITLE = re.compile(rf"\b{_GAIN_LABEL}\s*:\s*(?:[–—•-]\s*)?", re.I)
_GAIN_INLINE = re.compile(
    _GAIN_VALUE.format(value=rf"(?P<value>{_NUM})", condition=rf"(?P<condition>{_GAIN_NUMBER})"), re.I,
)
_GAIN_SYMBOL = r"(?:GBW|GBP|GBWP)"
_FIELD_SEPARATOR = r"\s*\|\s*"
_EXACT_GAIN_ROW = re.compile(
    rf"\b(?:{_GAIN_SYMBOL}{_FIELD_SEPARATOR}{_GAIN_LABEL}|{_GAIN_LABEL}{_FIELD_SEPARATOR}{_GAIN_SYMBOL})"
    rf"{_FIELD_SEPARATOR}\$?\s*{_GAIN_VARIABLE}\s*\$?\s*=\s*"
    rf"(?P<condition>{_GAIN_NUMBER})(?:\s*V\s*/\s*V)?\s*\$?"
    + _COLSPAN + rf"\s*\|(?:\s*\|)*\s*(?:PDF verified:\s*TYP\s*=\s*)?(?P<value>{_NUM})" + _COLSPAN
    + r"\s*\|(?:\s*\|)*\s*(?P<unit>GHz|MHz|kHz|Hz)\b",
    re.I,
)
_FREQUENCY_UNITS = {"hz": ("Hz", 0), "khz": ("kHz", 3), "mhz": ("MHz", 6), "ghz": ("GHz", 9)}


@dataclass(frozen=True)
class VerifiedExcerpt:
    rule: str
    chunk: object | None
    excerpt: str = ""
    reason: str | None = None


def _temperature_math(match):
    inner = re.sub(r"\\(?:mathrm|mathsf|mathbf|text)\b|[{}~]", "", match[1])
    inner = re.sub(r"\\[;,!]", "", inner)
    inner = re.sub(r"\s+", "", inner).replace(r"\pm", "±").replace(r"^\circ", "°")
    if re.fullmatch(r"(?:of)?[±+\-−]?(?:0|[1-9]\d*)(?:\.\d+)?°C", inner):
        return inner.replace("of", "of ", 1) if inner.startswith("of") else inner
    return match[0]


def _view(text):
    # A chunk can start with the closing delimiter of a different formula.
    text = re.sub(r"\$([^$\n]*\\circ[^$\n]*)\$", _temperature_math, text)
    text = re.sub(r"<[^>\n]+>", "", text)
    return re.sub(r"\[([^\]\n]+)\]\([^) \n]+\)", r"\1", text)


def _condition_topic(question):
    parts = list(dict.fromkeys(_CONDITION_PART.findall(question.casefold())))
    if len(parts) != 1:
        return None
    part = re.compile(rf"(?<!\w){re.escape(parts[0])}(?!\w)", re.I | re.ASCII)
    return part, part.sub("", question)


def _condition_rule(topic):
    if _UNSUPPORTED_CONDITION_TOPIC.search(topic):
        return None
    return next((item for item in _CONDITION_RULES if item.question.search(topic)), None)


def _requested_gain(topic):
    if not re.search(_GAIN_BANDWIDTH, topic, re.I):
        return None
    topic = unicodedata.normalize("NFKC", topic)
    matches = list(_GAIN_REQUEST.finditer(topic))
    if len(matches) != 1:
        return None
    match = matches[0]
    remainder = topic[:match.start()] + " " + topic[match.end():]
    if (_UNSUPPORTED_CONDITION_TOPIC.search(remainder)
            or re.search(r"\b(?:[kMG]?Hz|dB|V|mA|difference|ratio|noise)\b|[、和与及]|分别|相差|噪声", remainder, re.I)):
        return None
    return _gain_decimal(match["gain"])


def _gain_decimal(value):
    return Decimal(value.replace("−", "-").replace("–", "-"))


def _gain_evidence(text):
    for match in _EXACT_GAIN_ROW.finditer(text):
        yield match, match["unit"], text.strip() == match[0]
    for title in _GAIN_TITLE.finditer(text):
        offset = title.end()
        while match := _GAIN_INLINE.match(text, offset):
            yield match, "MHz", False
            separator = _PAIR_SEPARATOR.match(text, match.end())
            if not separator:
                break
            offset = separator.end()


def _find_exact_gain_excerpt(part, gain, chunks):
    rule = "gain_bandwidth_at_gain"
    matches = {}
    for chunk in chunks:
        raw = chunk.text or ""
        if not part.search(chunk.filename or "") and not part.search(raw):
            continue
        text = unicodedata.normalize("NFKC", _view(raw))
        for match, unit, standalone in _gain_evidence(text):
            if _gain_decimal(match["condition"]) != gain or match["value"].startswith("±"):
                continue
            value = _gain_decimal(match["value"])
            if value <= 0:
                continue
            symbol, exponent = _FREQUENCY_UNITS[unit.casefold()]
            key = value.scaleb(exponent)
            if key not in matches or (standalone and not matches[key][0]):
                excerpt = f"Gain bandwidth product (G = {match['condition']}): {match['value']} {symbol}"
                matches[key] = (standalone, VerifiedExcerpt(rule, chunk, excerpt))
    if len(matches) == 1:
        return next(iter(matches.values()))[1]
    reason = "conflicting_gain_evidence" if matches else "gain_evidence_missing"
    return VerifiedExcerpt(rule, None, reason=reason)


def condition_search_terms(question, *, exact_gain=True):
    scope = _condition_topic(question)
    if not scope:
        return None
    _, topic = scope
    gain = _requested_gain(topic) if exact_gain else None
    if gain is not None:
        return f"gain bandwidth product GBW GBP G AV {gain:f}"
    rule = _condition_rule(topic)
    return _CONDITION_SEARCH_TERMS[rule.name] if rule else None


def find_condition_excerpt(question, chunks, *, exact_gain=True):
    """Find explicitly labelled conditions in the selected document's passages."""
    scope = _condition_topic(question)
    if not scope:
        return None
    part, topic = scope
    gain = _requested_gain(topic) if exact_gain else None
    if gain is not None:
        return _find_exact_gain_excerpt(part, gain, chunks)
    rule = _condition_rule(topic)
    if not rule:
        return None
    matches = {}
    for chunk in chunks:
        raw = chunk.text or ""
        if not part.search(chunk.filename or "") and not part.search(raw):
            continue
        visible = unicodedata.normalize("NFKC", _view(raw))
        visible = re.sub(r"[−–](?=\d)", "-", visible)
        patterns = (rule.evidence, _GAIN_TABLE) if rule.name == "gain_bandwidth_by_gain" else (rule.evidence,)
        for pattern, match in ((p, m) for p in patterns for m in p.finditer(visible)):
            pairs = {}
            for index in ("1", "2"):
                condition = re.sub(r"\s+", "", match[f"condition{index}"]).casefold()
                value = re.sub(r"\s+", "", match[f"value{index}"]).casefold()
                pairs[condition] = value
            if len(pairs) != 2:
                return None
            # Do not present a two-entry prefix of a longer list as complete.
            tail = visible[match.end():]
            if _PAIR_CONTINUATION[rule.name].match(tail):
                return None
            if pattern is _GAIN_TABLE and re.match(r"\s*GBW\s*\|", tail, re.I):
                return None
            key = tuple(sorted(pairs.items()))
            if pattern is _GAIN_TABLE:
                excerpt = "Gain bandwidth product: " + " – ".join(
                    f"{value} MHz (G = {condition})" for condition, value in pairs.items()
                )
            else:
                excerpt = " ".join(match[0].split())
            matches.setdefault(key, VerifiedExcerpt(rule.name, chunk, excerpt))
    return next(iter(matches.values())) if len(matches) == 1 else None


def find_verified_excerpt(question, chunks):
    """Find one named, unambiguous statement in the already retrieved evidence."""
    parts = list(dict.fromkeys(_PART.findall(question.casefold())))
    if len(parts) != 1:
        return None
    part = re.compile(rf"(?<![\w]){re.escape(parts[0])}(?![\w])", re.I)
    rule = next((rule for rule in _RULES if rule.question.search(question)), None)
    if not rule:
        return None
    if re.search(r"\b(?:at|under|when|with)\s+[+-]?\d|[+-]?\d+\s*°\s*C|工况为|条件下", question, re.I):
        return None
    if rule.name not in {"offset_maximum", "altitude_limit"} and re.search(
        r"\b(?:min|max|minimum|maximum|typ|typical)\b|最[大小]|典型", question, re.I
    ):
        return None
    matches = {}
    concept_seen = False
    for chunk in chunks:
        raw = chunk.text or ""
        if not part.search(chunk.filename or "") and not part.search(raw):
            continue
        visible = _view(raw)
        if rule.abstain_without_match and _ABSENCE_TERMS[rule.name].search(visible):
            concept_seen = True
        for match in rule.evidence.finditer(visible):
            value = unicodedata.normalize("NFKC", match["value"]).casefold()
            value = re.sub(r"\s+", "", value)
            value = re.sub(r"(?<=\d)-(?=bit|μv|uv|mv|v)", "", value)
            matches.setdefault(value, VerifiedExcerpt(rule.name, chunk, match[0]))
    if len(matches) == 1:
        return next(iter(matches.values()))
    if not matches and rule.abstain_without_match and not concept_seen:
        return VerifiedExcerpt(rule.name, None)
    return None
