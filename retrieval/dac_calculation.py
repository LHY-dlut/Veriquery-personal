"""Evidence-bound calculations for straight-binary voltage DACs."""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import re
from pathlib import Path
import unicodedata


_UNIT = {"V": Decimal(1), "mV": Decimal(".001"), "µV": Decimal(".000001")}
_VOLTAGE = r"([+-]?\d+(?:\.\d+)?)\s*(mV|[uµμ]V|V)(?![a-zA-Z])"
_SUPPLY = re.compile(r"\b(AVDD|VDD|VREF|VA)\s*=\s*" + _VOLTAGE, re.I)
_CODE = re.compile(r"(?:输入码|寄存器码|码|(?:input\s+)?code)\s*(?:为|是|=|of|is|为十进制)?\s*"
                   r"(?:十进制|十六进制|decimal|hexadecimal)?\s*(0x[\da-f]+|0b[01]+|\d+)(?![a-z0-9_]|\.\d)", re.I)
_CONDITION = re.compile(r"\b([A-Z][A-Z0-9_/]*)\s*=\s*([A-Z][A-Z0-9_]*)\b", re.I)


def _volts(number, unit):
    scale = {"v": "V", "mv": "mV", "uv": "µV", "μv": "µV"}[unit.casefold()]
    return Decimal(number) * _UNIT[scale]


def _show(value):
    return format(value.normalize(), "f")


def _math(text):
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"\\(?:left|right|mathrm|mathsf|text|operatorname|bigl|bigr)\b", "", text)
    text = text.replace(r"\times", "*").replace(r"\cdot", "*").replace("×", "*")
    return re.sub(r"[\s{}_$&]", "", text)


@dataclass(frozen=True)
class Transfer:
    levels: int
    step_source: int
    output_source: int | None
    inl_source: int | None
    rail: str

    def code_fraction(self, code):
        return Fraction(code, self.levels)


_DEVICE = r"[a-z]{2,}\d[a-z0-9-]*"
_DIRECT = re.compile(r"vout=(avdd|vdd|vref|va)(?:\*|x)\(?(?:\\fracd|d/)(2\^n|\d+)\)?")


def _resolution_map(passage):
    """Read local, explicitly named resolution definitions, including family lists."""
    pairs = []
    for line in passage.splitlines():
        text = _math(line)
        pairs.extend(re.findall(rf"n=(\d{{1,2}})(?:for|inthe)({_DEVICE})", text))
        if "resolution" in line.lower() and "bit" in line.lower():
            pairs.extend(re.findall(rf"(?<!\d)(\d{{1,2}})\s*\(\s*({_DEVICE})\s*\)", line.lower()))
        pairs.extend(re.findall(rf"(?<!\d)(\d{{1,2}})[ -]bits?\s+({_DEVICE})\b", line.lower()))
    result = {}
    for width, model in pairs:
        if 1 <= int(width) <= 32:
            result.setdefault(model, set()).add(int(width))
    return result


def _transfer(passages, part, rail):
    """Bind a denominator and supply to a source and a specific device."""
    candidates, outputs, inls = [], [], []
    for i, item in passages:
        passage = item["text_snippet"]
        text = _math(passage)
        bindings = _resolution_map(passage)
        widths = bindings.get(part.casefold(), set())
        # A filename may name the family representative. A sibling needs local
        # proof of its identity before it can inherit this passage's equation.
        if part.casefold() != Path(item["file"]).stem.casefold() and not widths:
            continue
        if len(widths) > 1:
            return None
        for line in passage.splitlines():
            equation = re.sub(r"(?:\\tag\d+|\(\d+\))\.?$", "", _math(line).rstrip("."))
            linear = _DIRECT.fullmatch(equation)
            if not linear or rail and linear[1] != rail:
                continue
            if linear[2] == "2^n":
                if not widths:
                    continue
                levels = 2 ** next(iter(widths))
            else:
                levels = int(linear[2])
            if (2 <= levels <= 2**32 and levels & (levels - 1) == 0
                    and (not widths or levels == 2 ** next(iter(widths)))):
                candidates.append((levels, linear[1], i))
                outputs.append(i)
        if widths and re.search(r"lsb(?:ideal)?=(?:\\fracvref2\^n|vref/2\^n)(?==|\\tag|where|$)", text):
            ref = re.search(r"(?:referencevoltage|vref)=(avdd|vdd|va)(?=in|for|[.,;)]|$)", text)
            symbol = ref[1] if ref and rail != "vref" else "vref"
            if not rail or rail == symbol:
                candidates.append((2**next(iter(widths)), symbol, i))
        if "videal=code*lsb" in text:
            outputs.append(i)
        if re.search(r"inl=\\frac\(?vout-videal\)?lsb", text):
            inls.append(i)
    if len({(n, symbol) for n, symbol, _ in candidates}) != 1:
        return None
    n, symbol, src = candidates[0]
    return Transfer(n, src, next(iter(outputs), None), next(iter(inls), None), symbol)


def _feedback_row(query, passages, part):
    conditions = _CONDITION.findall(query)
    if len(conditions) != 1 or not re.search(r"feedback voltage|反馈电压", query, re.I):
        return None
    mode = "=".join(conditions[0])
    matches = []
    for i, item in passages:
        lines = item["text_snippet"].splitlines()
        if len(lines) != 2:
            continue
        header = [c.strip().upper() for c in lines[0].split("|")]
        row = [c.strip() for c in lines[1].split("|")]
        fields = ("TEST CONDITIONS", "MIN", "MAX", "UNIT")
        if len(header) != len(row) or any(header.count(field) != 1 for field in fields):
            continue
        condition, low, high, unit = (row[header.index(field)] for field in fields)
        label = " ".join(row[:header.index("TEST CONDITIONS")])
        if not re.search(rf"\b{re.escape(part)}\s+feedback voltage\b", label, re.I):
            continue
        if _math(condition) != _math(mode) or not all(re.fullmatch(r"[+-]?\d+(?:\.\d+)?", v) for v in (low, high)):
            continue
        if unit not in _UNIT:
            continue
        if Decimal(low) < Decimal(high):
            matches.append((Decimal(low), Decimal(high), unit, i))
    if len({row[:3] for row in matches}) == 1:
        return (*matches[0], mode)
    return None


def calculate_dac(query, evidence, context, selected):
    """Return an answer and its exact original evidence positions, or defer to RAG."""
    if not selected or not re.search(r"\b(?:code|steps)\b|码|步", query, re.I):
        return None
    rails = _SUPPLY.findall(query)
    if len(rails) > 1 or re.search(r"\bbipolar\b|differential output|双极|增益|误差范围|worst.case|"
                                    r"power.down|待机|关断|温度|temperature|°[CF]", query, re.I):
        return None
    rail = rails[0][0].lower() if rails else None
    supply = _volts(*rails[0][1:]) if rails else None
    if supply is not None and supply <= 0:
        return None
    measured = re.findall(r"(?:实测\s*(?:VOUT|输出电压)?|measured\s+(?:VOUT|output voltage))\s*(?:为|=|is)?\s*"
                          + _VOLTAGE, query, re.I)
    if len(re.findall(_VOLTAGE, query, re.I)) > len(rails) + len(measured):
        return None
    groups = {}
    names = {}
    for i, item in enumerate(evidence):
        doc = item["document_id"]
        # Evidence metadata can carry a longer source than the model excerpt.
        if item["text_snippet"] and item["text_snippet"] in context:
            groups.setdefault(doc, []).append((i, item))
    if set(groups) != selected or len(groups) not in (1, 2):
        return None
    mentioned = set(re.findall(rf"(?<![a-z0-9])({_DEVICE})(?![a-z0-9])", query.casefold()))
    for doc, passages in groups.items():
        filename = Path(passages[0][1]["file"]).stem
        members = {filename.casefold()}
        for _, item in passages:
            members.update(_resolution_map(item["text_snippet"]))
        scoped = members & mentioned
        if len(scoped) > 1:
            return None
        names[doc] = next(iter(scoped)).upper() if scoped else filename
    if mentioned - {v.casefold() for v in names.values()}:
        return None
    sources = [(doc, tr) for doc, rows in groups.items()
               if (tr := _transfer(rows, names[doc], rail))]
    if len(sources) != 1:
        return None
    doc, tr = sources[0]
    part = names[doc]
    refs = []

    def cite(i):
        if i not in refs:
            refs.append(i)
        return f"[{refs.index(i)+1}]"

    step = supply / tr.levels if supply is not None else None
    step_line = (f"{part} 按题设 {tr.rail.upper()}={_show(supply)} V，理想步长为 "
                 f"{_show(supply)} V / {tr.levels} = {_show(step*1000)} mV/LSB {cite(tr.step_source)}。"
                 if step is not None else "")
    if len(groups) == 2:
        if not (rail and re.search(r"steps|步数|多少.*步", query, re.I)
                and re.search(r"minimum.*maximum|min.*max|最小.*最大", query, re.I)
                and re.search(rf"\b{re.escape(part)}\s+{rail}\s*=", query, re.I)):
            return None
        other = next(k for k in groups if k != doc)
        if not re.search(rf"\b{re.escape(names[other])}\b", query, re.I):
            return None
        row = _feedback_row(query, groups[other], names[other])
        if row is None:
            return None
        low, high, units, src, mode = row
        width = (high-low) * _UNIT[units]
        answer = (f"{step_line}\n{names[other]} 在 {mode} 时，反馈电压 MIN={low} {units}、"
                  f"MAX={high} {units} {cite(src)}。全宽为 {_show(width*1000)} mV，"
                  f"对应 {_show(width*1000)} / {_show(step*1000)} = {_show(width/step)} 个理想步长 [1] [2]。")
        return answer, refs

    codes = _CODE.findall(query)
    literals = re.findall(r"(?<![a-z0-9_.-])0(?:x[0-9a-f]+|b[01]+)(?![a-z0-9_]|\.\d)", query, re.I)
    if not codes and re.search(r"\bcode\b|输入码", query, re.I):
        codes = literals
    if {v.lower() for v in literals} - {v.lower() for v in codes}:
        return None
    if len(codes) != 1 or tr.output_source is None:
        return None
    literal = codes[0].lower()
    code = int(literal, 0 if literal.startswith(("0x", "0b")) else 10)
    validity = re.search(r"\b(?:valid|allowed|fit|legal)\b|有效|合法|越界", query, re.I)
    output = re.search(r"(?:理想输出|ideal output|output voltage|输出电压)", query, re.I)
    if not (validity or output) or re.search(r"DNL|典型|最大|最小|settling|建立时间|功耗|power consumption", query, re.I):
        return None
    if not 0 <= code < tr.levels or validity and not output:
        # A converter data code and its serial transport word have different domains.
        if re.search(r"SPI|frame|serial word|指令|帧", query, re.I):
            return None
        refs.clear()
        fits = "可以" if 0 <= code < tr.levels else "无法"
        return (f"{part} 的 DAC 数据码范围为 0–{tr.levels-1}，{literal} = {code}（十进制），"
                f"{fits}表示为该器件的数据码 {cite(tr.step_source)}。"), refs
    if supply is None:
        if re.search(_VOLTAGE, query, re.I) or re.search(r"INL|DNL|实测|measured", query, re.I):
            return None
        ratio = tr.code_fraction(code)
        proof = " ".join(cite(i) for i in dict.fromkeys((tr.step_source, tr.output_source)))
        return (f"{part} 的输入码 {literal} = {code}（十进制）。理想输出为 "
                f"{code}/{tr.levels} × {tr.rail.upper()} = {ratio} × {tr.rail.upper()} "
                f"{proof}。题设缺少参考电压的数值，无法确定绝对输出电压。"), refs
    ideal = step * code
    wants_inl = bool(re.search(r"\bINL\b", query, re.I))
    if wants_inl != bool(measured) or wants_inl and (len(measured) != 1 or tr.inl_source is None):
        return None
    answer = (f"{step_line}\n输入码 {literal} 的十进制值为 {code}。按码值乘以步长，"
              f"理想输出为 {code} × {_show(step*1000)} mV = {_show(ideal*1000)} mV"
              f"（{_show(ideal)} V）{cite(tr.output_source)}。")
    if wants_inl:
        voltage = _volts(*measured[0])
        answer += (f"\n按题设实测值，有符号 INL = ({_show(voltage*1000)} − {_show(ideal*1000)})"
                   f" / {_show(step*1000)} = {_show((voltage-ideal)/step)} LSB {cite(tr.inl_source)}。")
    return answer, refs
