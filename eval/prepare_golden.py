#!/usr/bin/env python3
"""Stage a pinned datasheet-rag benchmark without importing its chunk IDs."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import requests

REVISION = "885cbb4dacb633cc703cecad450c6be35a7b3668"
RAW = f"https://raw.githubusercontent.com/palginpav/datasheet-rag/{REVISION}"
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "data" / "eval"
QUESTION_FIELDS = ("id", "category", "question", "answerable", "gold_answer",
                   "must_include", "gold_parts", "notes")


def fetch(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.content


def prepare(session, out):
    golden = fetch(session, f"{RAW}/data/golden/golden.jsonl")
    manifest = fetch(session, f"{RAW}/data/manifest.json")
    license_text = fetch(session, f"{RAW}/LICENSE")
    questions = [
        {key: row[key] for key in QUESTION_FIELDS if key in row}
        for line in golden.decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("//")
        for row in [json.loads(line)]
    ]
    parts = {part for row in questions for part in row["gold_parts"]}
    entries = {row["part"]: row for row in json.loads(manifest)["entries"]}
    by_hash = {}
    for part in sorted(parts):
        row = entries[part]
        if not row.get("sha256"):
            raise ValueError(f"Missing pinned PDF hash: {part}")
        pdf = by_hash.setdefault(row["sha256"], {
            "sha256": row["sha256"], "filename": f"{part}.pdf", "parts": [], "urls": [],
        })
        pdf["parts"].append(part)
        if row["url"] not in pdf["urls"]:
            pdf["urls"].append(row["url"])
    provenance = {
        "repository": "https://github.com/palginpav/datasheet-rag",
        "revision": REVISION,
        "golden_sha256": hashlib.sha256(golden).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "question_count": len(questions),
        "answerable": sum(q["answerable"] for q in questions),
        "categories": dict(Counter(q["category"] for q in questions)),
        "pdfs": list(by_hash.values()),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "golden.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in questions), encoding="utf-8"
    )
    (out / "source.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    (out / "LICENSE.datasheet-rag").write_bytes(license_text)
    return provenance


def download_pdf(session, entry, directory, unmatched=None):
    target = directory / entry["filename"]
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == entry["sha256"]:
        return "verified"
    failures = []
    for url in entry["urls"]:
        try:
            body = fetch(session, url)
            if not body.startswith(b"%PDF-"):
                raise ValueError("response is not a PDF")
            digest = hashlib.sha256(body).hexdigest()
            if digest != entry["sha256"]:
                if unmatched is not None:
                    candidate = unmatched / f"{target.stem}_{digest}.pdf"
                    if not candidate.exists():
                        candidate.write_bytes(body)
                raise ValueError(f"PDF SHA-256 mismatch ({digest})")
            target.write_bytes(body)
            return "downloaded"
        except (requests.RequestException, ValueError) as exc:
            failures.append(f"{url} ({exc})")
    return "; ".join(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--download-pdfs", action="store_true")
    parser.add_argument("--parts", help="逗号分隔的下载型号，省略时下载全部")
    parser.add_argument("--keep-unmatched", action="store_true", help="另存不匹配 PDF，供人工复核")
    args = parser.parse_args()
    with requests.Session() as session:
        source = prepare(session, args.out_dir)
        print(f"{source['question_count']} questions; {len(source['pdfs'])} distinct PDF hashes")
        if not args.download_pdfs:
            return 0
        directory = args.out_dir / "pdfs"
        directory.mkdir(exist_ok=True)
        unmatched = args.out_dir / "unmatched" if args.keep_unmatched else None
        if unmatched is not None:
            unmatched.mkdir(exist_ok=True)
        parts = {x.strip().upper() for x in (args.parts or "").split(",") if x.strip()}
        entries = [p for p in source["pdfs"] if not parts or parts.intersection(p["parts"])]
        unknown = parts - {part for entry in entries for part in entry["parts"]}
        if unknown:
            parser.error(f"未知型号 {', '.join(sorted(unknown))}")
        results = {p["filename"]: download_pdf(session, p, directory, unmatched) for p in entries}
        (args.out_dir / "downloads.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for filename, result in results.items():
            print(f"{filename} {result}")
        return int(any(result not in {"verified", "downloaded"} for result in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
