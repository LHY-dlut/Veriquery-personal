"""Build compact, question-matched evidence rows from existing table chunks."""
import re
import unicodedata
from dataclasses import dataclass

from core.schema import RetrievedChunk


_MODES = {
    "active": re.compile(r"\boperating\b|\bcontinuous[- ]conversion\b|工作模式|工作电流|工作时"),
    "inactive": re.compile(r"\b(?:power[- ]down|shutdown)\b|关断|掉电"),
    "standby": re.compile(r"\b(?:standby|sleep)\b|待机|休眠"),
}
_TEMPERATURE = re.compile(r"(?<![\d.])([+-]?\d+(?:\.\d+)?)\s*°\s*c(?![a-z0-9])")
_ASSIGNMENT = re.compile(r"(?<![a-z0-9])(ta|tj|vdd|vs|g)\s*=\s*([+-]?\d+(?:\.\d+)?)(?![\d.])")
_FIELD_NAMES = {
    "min": re.compile(r"\b(?:min|minimum)\b|最小值"),
    "typ": re.compile(r"\b(?:typ|typical)\b|典型"),
    "max": re.compile(r"\b(?:max|maximum)\b|最大值"),
}
_HEADINGS = re.compile(r"\b(?:min|typ|max|binary|hex|parameter|conditions?|unit)\b")
_SUPPLY_RANGE_HEADER = re.compile(r"(?:vcc|vdd|v\+|supply(?: voltage)?)\s+range")
_NAMED_PART = re.compile(r"\b(?:[a-z]{2,}\d[a-z0-9-]*|\d{2}[a-z]{2,}\d[a-z0-9-]*)\b")
_VOLTAGE_RANGE = re.compile(r"\d+(?:\.\d+)?\s*v?\s*(?:-|to|至|~)\s*\d+(?:\.\d+)?\s*v")


def _normalize(text):
    text = unicodedata.normalize("NFKC", text).casefold().replace("−", "-").replace("–", "-")
    return re.sub(r"(?<=[a-z])_(?:\{([a-z]+)\}|([a-z]+))", lambda m: m[1] or m[2], text)


def _cells(text):
    return [cell.strip() for cell in text.split("|")]


def is_table_header(row):
    cells = [_normalize(cell) for cell in _cells(row)]
    if any(_SUPPLY_RANGE_HEADER.fullmatch(cell) for cell in cells) and "part number" in cells:
        return True
    words = set(_HEADINGS.findall(_normalize(row)))
    # A data cell can contain "PDF verified: TYP = ...; MAX = ...".
    # A header has whole label cells, rather than assignments or values.
    return len(words) >= 2 and any(
        re.fullmatch(r"(?:min|typ|max)(?:\s+(?:min|typ|max))*|binary|hex", _normalize(c))
        for c in _cells(row)
    )


def table_rows(table):
    """Attach the applicable column labels to every complete stored row."""
    label = ""
    for number, cells in enumerate(table, 1):
        row = " | ".join("" if value is None else str(value).strip() for value in cells)
        if not any(cell for cell in _cells(row)):
            continue
        if is_table_header(row):
            label = row
            yield number, row, ""
        else:
            yield number, row, label


@dataclass(frozen=True)
class TableEvidenceQuery:
    mode: str | None
    temperature: float | None
    assignments: tuple[tuple[str, float], ...]
    field: str | None
    subject: str | None
    channel: str | None
    part: str | None = None

    @classmethod
    def from_text(cls, query):
        query = _normalize(query)
        modes = [key for key, pattern in _MODES.items() if pattern.search(query)]
        temps = {float(value) for value in _TEMPERATURE.findall(query)}
        assignments = tuple(dict.fromkeys((label, float(value)) for label, value in _ASSIGNMENT.findall(query)))
        if len(modes) > 1 or len(temps) > 1 or len({label for label, _ in assignments}) != len(assignments):
            return None
        fields = [key for key, pattern in _FIELD_NAMES.items() if pattern.search(query)]
        field = fields[0] if len(fields) == 1 else None
        if re.search(r"\bsupply voltage range\b|供电电压范围|电源电压范围", query):
            parts = list(dict.fromkeys(_NAMED_PART.findall(query)))
            if len(parts) == 1 and not (temps or assignments or modes):
                return cls(None, None, (), None, "supply range", None, parts[0])
        if re.search(r"\bhex(?:adecimal)?\b|十六进制", query) and temps:
            return cls(None, next(iter(temps)), assignments, "hex", "register", None)
        if re.search(r"\blsb\b|最低有效位", query):
            channels = [term for term in ("bus voltage", "shunt voltage") if term in query]
            if len(channels) == 1:
                return cls(None, None, (), field, "lsb", channels[0])
        if field and re.search(r"\b(?:data|sampling) rates?\b|数据速率|采样率", query):
            return cls(None, None, (), field, "data rate", None)
        if re.search(r"\bcurrent\b|电流", query) and (modes or temps or assignments):
            return cls(modes[0] if modes else None, next(iter(temps), None), assignments,
                       field, "current", None)
        return None

    def matches(self, text, header=""):
        """Require literal parameter and test-condition matches for promotion."""
        line = _normalize(text)
        if "\n" in line:
            return False
        if self.subject == "register":
            cells = _cells(line)
            try:
                return ("hex" in map(_normalize, _cells(header)) and
                        float(cells[0]) == self.temperature)
            except ValueError:
                return False
        if self.subject == "supply range":
            return self.literal_value(text, header) is not None
        if self.subject == "lsb":
            return bool(re.search(r"\blsb\b", line)) and self.channel in line
        if self.subject == "data rate":
            cells = _cells(line)
            return any(re.fullmatch(r"(?:programmable )?(?:data|sampling) rate(?:s)?", cell)
                       for cell in cells)
        if not re.search(r"\bcurrent\b", line):
            return False
        if self.mode and not _MODES[self.mode].search(line):
            return False
        if self.temperature is not None and self.temperature not in map(float, _TEMPERATURE.findall(line)):
            return False
        values = {(label, float(value)) for label, value in _ASSIGNMENT.findall(line)}
        if not all(condition in values for condition in self.assignments):
            return False
        return self.field not in _FIELD_NAMES or self._role_projection(text, header) is not None

    def model_text(self, text, header):
        """Project a requested column when the parser exposes its exact position."""
        cells = _cells(text)
        headings = [_normalize(cell) for cell in _cells(header or "")]
        if self.subject == "supply range":
            value = self.literal_value(text, header)
            return f"{self.part.upper()} | VCC Range: {value}" if value else text
        if self.field == "hex" and len(headings) == len(cells) and headings.count("hex") == 1:
            return f"{headings[0]}: {cells[0]} | HEX: {cells[headings.index('hex')]}"
        if self.field in _FIELD_NAMES:
            projected = self._role_projection(text, header)
            if projected is not None:
                return projected
            if "Enumerated options:" in text:
                return f"{text}\nThis row lists options; it does not specify a {self.field.upper()} value."
        return text

    def literal_value(self, text, header):
        """Verify the named part and value in a labeled supply range column."""
        if self.subject != "supply range":
            return None
        values = _cells(text)
        headings = [_normalize(cell) for cell in _cells(header or "")]
        if len(headings) != len(values) or headings.count("part number") != 1:
            return None
        cols = [i for i, heading in enumerate(headings) if _SUPPLY_RANGE_HEADER.fullmatch(heading)]
        if len(cols) != 1 or _normalize(values[headings.index("part number")]) != self.part:
            return None
        value = values[cols[0]]
        return value if _VOLTAGE_RANGE.fullmatch(_normalize(value)) else None

    def _role_projection(self, text, header):
        cells = _cells(text)
        headings = [_normalize(cell) for cell in _cells(header or "")]
        if headings.count(self.field) == 1 and len(headings) == len(cells):
            value = cells[headings.index(self.field)]
            if not value or "[跨" in value or value.startswith(("Unverified column:", "Enumerated options:")):
                return None
            return " | ".join(
                f"{heading.upper()}: {cell}" if heading == self.field else cell
                for heading, cell in zip(headings, cells)
                if cell and (heading == self.field or heading not in _FIELD_NAMES)
            )
        for i, cell in enumerate(cells):
            if not cell.startswith("PDF verified:"):
                continue
            match = re.search(rf"\b{self.field.upper()}\s*=\s*([^;]+)", cell)
            if match:
                cells[i] = f"{self.field.upper()}: {match[1].strip()}"
                return " | ".join(cell for cell in cells if cell)
        return None


def table_row_key(chunk):
    return chunk.document_id, chunk.page, " ".join(chunk.text.split())


def matching_store_rows(results, focus):
    """Recover literal matches even if the whole SQLite table ranked poorly."""
    matches = []
    for result in results:
        if result.source != "table":
            continue
        selected = 0
        for number, row, header in table_rows(result.metadata.get("table_data") or []):
            if not focus.matches(row, header):
                continue
            matches.append(RetrievedChunk(
                chunk_id=f"{result.chunk_id}#row{number}",
                document_id=result.document_id, filename=result.filename, page=result.page,
                source="table", score=result.score, text=row,
                metadata={"source": "table_text", "table_header": header,
                          "section": f"{result.metadata.get('table_title') or '表格'} 第 {number} 行"},
            ))
            selected += 1
            if selected >= 3:
                break
    return matches[:12]
