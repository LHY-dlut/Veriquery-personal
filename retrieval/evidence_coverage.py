"""Preserve complementary evidence and compare explicitly grounded quantities."""

from decimal import Decimal
from fractions import Fraction
import re
import unicodedata


_CLAUSE_BREAK = re.compile(r"[？?！!。；;]+|(?<=[^\d.])\.\s+")
_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
_QUANTITY = re.compile(rf"(?<![\w.+\-−±])(?P<sign>[+\-−]?)\s*(?P<value>{_NUMBER})\s*"
                       r"(?P<unit>°[CF]|[munµμ]?[VAFWHΩ]|kHz|MHz|GHz|Hz|ms|µs|μs|ns|s|%)"
                       r"(?![A-Za-z])", re.I)
_STEP_TOPIC = re.compile(r"\btemperature\s+(?:step(?: size|s)?|resolution)\b|温度(?:步长|分辨率)", re.I)
_STEP_VALUE = (rf"(?P<{{value}}>{_NUMBER})\s*(?P<{{unit}}>°[CF]|(?-i:mK|K))(?![A-Za-zΩ])"
               r"(?:\s*/\s*LSB)?(?!\s*(?:/|(?:to\b|[-–−])\s*\d))")
_STEP = re.compile(
    rf"\btemperature\s+(?:resolution|step(?: size)?)\s*(?:of\s+|is\s+|[:=]\s*)?{_STEP_VALUE.format(value='value', unit='unit')}"
    rf"|(?:\(\s*)?{_STEP_VALUE.format(value='leading_value', unit='leading_unit')}\s*\)?\s+temperature\s+(?:resolution|step(?: size)?)\b",
    re.I,
)
_FACTOR = re.compile(r"\b(?:by what factor|how many times|ratio)\b|(?:多少|几)倍|比值", re.I)
_QUALIFIED_STEP = re.compile(
    r"\b(?:finest|coarsest|maximum|minimum|selectable|programmable|at|under|when|mode|typical|faster|accuracy)\b"
    r"|工况|条件下|最大|最小|最高|最低|档位|典型",
    re.I,
)
_STEP_SCALE = {"°c": Fraction(1), "k": Fraction(1), "mk": Fraction(1, 1000), "°f": Fraction(5, 9)}


def clauses(question):
    return [fragment.strip(" ,，") for fragment in _CLAUSE_BREAK.split(question) if fragment.strip(" ,，")]


def quantity_anchors(text):
    """Read explicit number/unit mentions for coverage, without inferring a value."""
    visible = re.sub(r"\.{2,}", " ", unicodedata.normalize("NFKC", text))
    return {
        (Decimal(("-" if m["sign"] in {"-", "−"} else "") + m["value"]), m["unit"].casefold())
        for m in _QUANTITY.finditer(visible)
    }


def temperature_step_query(question):
    return bool(_STEP_TOPIC.search(question))


def temperature_step_passages(chunks):
    for chunk in chunks:
        visible = unicodedata.normalize("NFKC", chunk.text or "").replace("\uf0b0", "°")
        for match in _STEP.finditer(visible):
            value = match["value"] or match["leading_value"]
            unit = match["unit"] or match["leading_unit"]
            if Decimal(value) <= 0:
                continue
            # A temperature step is a difference. Celsius and kelvin steps are
            # equal in size, and neither receives an absolute-temperature offset.
            yield chunk, match[0].strip(), Fraction(value) * _STEP_SCALE[unit.casefold()], value, unit


def comparable_temperature_steps(question, document_ids, chunks):
    """Return one compatible labelled operand per device for a direct factor question."""
    if (len(document_ids) != 2 or not temperature_step_query(question)
            or not _FACTOR.search(question) or _QUALIFIED_STEP.search(question)):
        return None
    by_document = {document_id: {} for document_id in document_ids}
    for chunk, excerpt, magnitude, value, unit in temperature_step_passages(chunks):
        if chunk.document_id in by_document:
            by_document[chunk.document_id].setdefault(magnitude, (chunk, excerpt, magnitude, value, unit))
    if any(len(values) != 1 for values in by_document.values()):
        return None
    return [next(iter(values.values())) for values in by_document.values()]
