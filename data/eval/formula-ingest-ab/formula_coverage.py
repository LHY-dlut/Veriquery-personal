"""Track the text of each captured original-page equation through the evaluation artifacts."""

import argparse
import json
from pathlib import Path
import unicodedata


def compact(text):
    return "".join(unicodedata.normalize("NFKC", text or "").split())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--blocks", required=True, type=Path, help="Folder with <part>-blocks.json captures")
    args = parser.parse_args()
    root = args.root
    captured = {}
    warnings = []
    names = {"MCP4725": ("4-1", "4-1-where", "4-2", "4-2-where", "4-3", "4-3-where"),
             "TPS63020": ("3",)}
    for part, labels in names.items():
        blockfile = args.blocks / f"{part}-blocks.json"
        source = json.loads(blockfile.read_text("utf-8"))
        blocks = [b for b in source["pages"][1] if b.get("type") == "equation"]
        if len(blocks) != len(labels):
            warnings.append(f"Expected {len(labels)} formula/definition blocks on {part} original page 15, got {len(blocks)}. Verify the unlabelled blocks against the PDF.")
            labels = [f"observed-block-{b['index']}" for b in blocks]
        for label, b in zip(labels, blocks):
            captured[f"{part}:{label}"] = {"part": part, "original_page": 15, "sample_page": 2,
                                           "job_id": source["job_id"], "block_index": b["index"],
                                           "bbox": b["bbox"], "content": b["content"]}
    report = {
        "matching": "Literal substring after NFKC and whitespace removal. This tracks captured formula bodies, not mathematically equivalent prose.",
        "reference_blocks": captured, "warnings": warnings, "arms": {},
    }
    for arm in ("before", "after"):
        indexed = json.loads((root / arm / "indexed.json").read_text("utf-8"))
        records = [json.loads(s) for s in (root / f"{arm}.jsonl").read_text("utf-8").splitlines()]
        evidence = {}
        for name, b in captured.items():
            reference = compact(b["content"])
            assert reference, name
            evidence[name] = {
                "index": [x["chunk_id"] for x in indexed[b["part"]] if x["metadata"].get("page")==2
                          and reference in compact(x["text"])],
                "cases": {},
            }
            for r in records:
                if b["part"] not in r["question"]["parts"]:
                    continue
                trace = r["prediction"]["trace"]
                evidence[name]["cases"][r["sample_id"]] = {
                    "retrieved": [hit["chunk_id"] for hit in trace.get("retrieved") or []
                                  if reference in compact(hit["text"])],
                    "in_model_input": reference in compact(trace.get("context")),
                    "cited": [i+1 for i,c in enumerate(r["prediction"]["citations"])
                              if reference in compact(c["text_snippet"])],
                }
        colocations = {}
        for label in ("4-1","4-2","4-3"):
            if f"MCP4725:{label}" not in evidence:
                colocations[label] = None
                continue
            body = set(evidence[f"MCP4725:{label}"]["index"])
            definitions = set(evidence[f"MCP4725:{label}-where"]["index"])
            colocations[label] = sorted(body & definitions)
        report["arms"][arm] = {"blocks": evidence, "coindexed_mcp_formula_and_definitions": colocations}
    (root / "formula-coverage.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    for message in warnings:
        print(message)
    for arm, data in report["arms"].items():
        print(arm, sum(bool(x["index"]) for x in data["blocks"].values()), "/", len(captured),
              "pair co-locations", data["coindexed_mcp_formula_and_definitions"])


if __name__=="__main__":
    main()
