"""Recover collapsed specification cells using the native PDF text geometry."""

from collections import Counter
import re

import fitz


_HEADINGS = ("MIN", "TYP", "MAX")
_NUMBER = r"[+±\-−–]?\d+(?:\.\d+)?"
_VALUES = re.compile(rf"{_NUMBER}(?:\s+{_NUMBER}){{0,2}}")
_OPTIONS = re.compile(rf"{_NUMBER}(?:\s*,\s*{_NUMBER}){{2,}}")


def _normalize_number(value):
    return value.replace("−", "-").replace("–", "-").lstrip("+")


def _compact_label(value):
    value = re.sub(r"\[跨\d+列合并\]", "", value)
    return re.sub(r"[\W_]+", "", value.casefold())


def _word_lines(page, clip):
    """Cluster by baseline within 2 PDF points in a single table region."""
    lines = []
    for word in sorted(page.get_text("words", clip=clip), key=lambda item: item[3]):
        if not lines or word[3] - lines[-1][0][3] > 2:
            lines.append([])
        lines[-1].append(word)
    return [sorted(line, key=lambda item: item[0]) for line in lines]


def _find_value_cells(page, bbox):
    if page.rotation or len(bbox or []) != 4:
        return {}
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        return {}
    clip = fitz.Rect(x0 * page.rect.width, y0 * page.rect.height,
                     x1 * page.rect.width, y1 * page.rect.height)
    lines = _word_lines(page, clip)
    headers = []
    for index, line in enumerate(lines):
        heading_words = [word for word in line if word[4].upper() in _HEADINGS]
        if tuple(word[4].upper() for word in heading_words) == _HEADINGS:
            headers.append((index, [word[2] for word in heading_words]))
    if len(headers) != 1:
        return {}

    start, anchors = headers[0]
    gaps = [b - a for a, b in zip(anchors, anchors[1:])]
    if min(gaps) < 14:
        return {}
    # Numeric tokens must be near the printed columns and clearly closer to
    # one heading than to the next. The right edge supports right-aligned data.
    tolerance = min(13, min(gaps) * .35)
    by_text = {}
    for line in lines[start + 1:]:
        cells = {}
        values = []
        for word in line:
            label, x_end = word[4], word[2]
            if not re.fullmatch(_NUMBER, label) or not anchors[0] - tolerance <= x_end <= anchors[-1] + tolerance:
                continue
            distance, column = min((abs(x_end - x), i) for i, x in enumerate(anchors))
            values.append(_normalize_number(label))
            if distance > tolerance or column in cells:
                continue
            cells[column] = {"value": label, "column": _HEADINGS[column],
                             "bbox_pt": [round(x, 2) for x in word[:4]]}
        if values:
            key = tuple(values)
            source_label = _compact_label(" ".join(
                word[4] for word in line if word[2] < anchors[0] - tolerance
            ))
            reading = (source_label, list(cells.values())) if len(cells) == len(values) else None
            # Repeated sequences on the source page cannot identify a unique row.
            by_text[key] = reading if key not in by_text else None
    return by_text


def annotate_spec_columns(path, page_number, rows, bbox):
    """Return evidence annotations for explicitly collapsed MIN/TYP/MAX cells.

    Raw MinerU HTML stays intact. A value receives a column label only when
    the same numeric sequence occurs once in the PDF table and once in the
    parsed table. Comma-separated choices stay an enumeration.
    """
    headers = [
        (index, column)
        for index, row in enumerate(rows)
        for column, cell in enumerate(row)
        if " ".join(cell.upper().split()) == "MIN TYP MAX"
    ]
    if len(headers) != 1:
        return rows, {}
    header_row, column = headers[0]
    candidates = {
        index: row[column].strip()
        for index, row in enumerate(rows)
        if index > header_row and len(row) == len(rows[header_row]) and row[column].strip()
    }
    numeric = {
        index: tuple(map(_normalize_number, value.split()))
        for index, value in candidates.items() if _VALUES.fullmatch(value)
    }
    evidence = {}
    annotated = [list(row) for row in rows]
    nearby = {}
    if numeric:
        try:
            with fitz.open(path) as pdf:
                nearby = _find_value_cells(pdf[page_number - 1], bbox)
        except (IndexError, OSError, RuntimeError, ValueError):
            pass
    counts = Counter(numeric.values())
    for index, value in candidates.items():
        key = numeric.get(index)
        match = nearby.get(key) if counts[key] == 1 else None
        source_labels = map(_compact_label, reversed(rows[index][:column]))
        row_label = next((label for label in source_labels if len(label) >= 4), "")
        if match and row_label and row_label in match[0]:
            _, cells = match
            evidence[index] = {"original_cell": value, "page": page_number, "cells": cells}
            annotated[index][column] = "PDF verified: " + "; ".join(
                f"{cell['column']} = {cell['value']}" for cell in cells
            )
        elif _OPTIONS.fullmatch(value):
            annotated[index][column] = f"Enumerated options: {value}"
        elif key or re.search(r"\d", value):
            annotated[index][column] = f"Unverified column: {value}"
    return annotated, evidence
