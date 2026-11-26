"""Compare control-page prose and all table rows across the two formula-ingest snapshots."""

import argparse
from collections import Counter
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch


PAGES = {"MCP4725": (1, 15), "TPS63020": (6, 15)}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(rows):
    return sha256(canonical(rows).encode("utf-8")).hexdigest()


def difference(before, after):
    b = Counter(map(canonical, before))
    a = Counter(map(canonical, after))
    return {
        "before": len(before), "after": len(after), "equal": before == after,
        "before_sha256": digest(before), "after_sha256": digest(after),
        "only_before": [json.loads(x) for x in (b - a).elements()],
        "only_after": [json.loads(x) for x in (a - b).elements()],
    }


def table_position(chunk):
    section = chunk["metadata"].get("section", "")
    return tuple(map(int, re.findall(r"\d+", section))), section, chunk["text"]


def collect(folder):
    chunks = read(folder / "indexed.json")
    tables = read(folder / "indexed-tables.json")
    result = {}
    for part, pages in PAGES.items():
        for page, original in enumerate(pages, 1):
            subset = [row for row in chunks[part] if int(row["metadata"]["page"]) == page]
            if not subset:
                raise ValueError(f"Missing indexed page {part} {original} in {folder}")
            groups = {}
            if page == 1:
                prose = sorted((r for r in subset if r["metadata"].get("source") == "pdf"),
                               key=lambda r: (r["metadata"]["char_start"], r["metadata"]["char_end"], r["text"]))
                if not prose or any(r["metadata"].get("formula") for r in prose):
                    raise ValueError(f"Unexpected formula or missing prose on control page {part} {original}")
                groups["control_prose"] = [
                    {"start": r["metadata"]["char_start"], "end": r["metadata"]["char_end"], "text": r["text"]}
                    for r in prose
                ]
                groups["control_prose_vectors"] = [r["embedding_sha256"] for r in prose]
            rows = sorted((r for r in subset if r["metadata"].get("source") == "table_text"), key=table_position)
            groups["index_table_rows"] = [
                {"text": r["text"], "section": r["metadata"].get("section"), "header": r["metadata"].get("table_header"),
                 "column_evidence": r["metadata"].get("pdf_column_evidence")} for r in rows
            ]
            groups["table_row_vectors"] = [r["embedding_sha256"] for r in rows]
            table_data = [
                {"header": json.loads(t["header"]), "rows": json.loads(t["data"])}
                for t in tables if t["filename"] == f"{part}.pdf" and int(t["page"]) == page
            ]
            groups["sqlite_tables"] = sorted(table_data, key=canonical)
            result[f"{part}_original_p{original}"] = groups
    return result


def replay_legacy(before, blocks_dir, pdf_dir, checkout):
    """Run in the application environment; compare the old code on the newly parsed inputs."""
    checkout = checkout.resolve()
    hashes = read(before / "index-manifest.json")["ingestion_files"]
    for filename, expected in hashes.items():
        if sha256((checkout / filename).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Legacy source hash mismatch: {filename}")
    sys.path.insert(0, str(checkout))
    from api.routers.documents import _build_chunks_from_pages
    from core.config import Settings
    from ingestion.document_processor import EnhancedDocumentProcessor
    from ingestion.mineru_client import MinerUClient

    settings = Settings()
    parts = {}
    for part, old in read(before / "indexed.json").items():
        source = json.loads(gzip.decompress((blocks_dir / f"{part}-structured.json.gz").read_bytes()))
        pages, _, _ = MinerUClient(settings)._build_pages(source, keep_pil=False)
        parsed = {"status": "success", "pages": [p.to_dict() for p in pages], "structured_content": source}
        with patch.object(MinerUClient, "parse_pdf", return_value=parsed):
            adapted = EnhancedDocumentProcessor._parse_pdf_mineru(SimpleNamespace(settings=settings), pdf_dir / f"{part}.pdf")
        chunks = _build_chunks_from_pages(adapted["pages"], settings, "legacy-replay", f"{part}.pdf")
        original = sorted((r["metadata"]["page"], r["metadata"]["source"], r["text"]) for r in old)
        rebuilt = sorted((r["metadata"]["page"], r["metadata"]["source"], r["content"]) for r in chunks)
        parts[part] = {"stored_before_chunks": len(original), "legacy_adapter_on_candidate_raw_chunks": len(rebuilt),
                       "page_source_text_equal_as_multiset": original == rebuilt}
    return {"method": "Original ingestion code replayed on new structured_content. Compare indexed "
                      "(page, source, text) triples with multiplicity.", "code_sha256": hashes, "parts": parts}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--before", type=Path)
    p.add_argument("--after", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--legacy-checkout", type=Path, help="Also replay new raw blocks with the archived before code")
    p.add_argument("--blocks", type=Path, help="Directory with <part>-structured.json.gz; defaults to root/mineru")
    p.add_argument("--pdf-dir", type=Path, help="Original two-page samples, needed for optional column verification")
    args = p.parse_args()
    if args.legacy_checkout:
        if not args.pdf_dir:
            p.error("--legacy-checkout requires --pdf-dir")
        result = replay_legacy(args.before or args.root / "before", args.blocks or args.root / "mineru",
                               args.pdf_dir, args.legacy_checkout)
        (args.root / "legacy-adapter-fresh-block-drift.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Legacy replay:", result["parts"])
    before, after = (collect(d) for d in (args.before or args.root / "before", args.after or args.root / "after"))
    comparisons = {k: {g: difference(rows, after[k][g]) for g, rows in groups.items()} for k, groups in before.items()}
    changed = [(page, group) for page, groups in comparisons.items()
               for group, result in groups.items() if not result["equal"]]
    report = {"scope": "Exact ordered control-page text and offsets, all indexed table rows/headers and SQLite grids. "
                       "Embedding checks are reported separately. Document IDs, chunk IDs and DB timestamps excluded. "
                       "Formula-page prose was intentionally regrouped and is outside this control.",
              "changed": changed, "pages": comparisons}
    (args.out or args.root / "parse-drift.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                                            encoding="utf-8")
    print("Changed control groups:", changed)
    for page, groups in comparisons.items():
        print(page, {g: (v["before"], v["after"], v["equal"]) for g, v in groups.items()})


if __name__ == "__main__":
    main()
