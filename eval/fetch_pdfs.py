#!/usr/bin/env python3
"""从厂商官网下载评测用 datasheet PDF，并校验内容确实包含该型号。

用法：
    .venv/bin/python eval/fetch_pdfs.py --parts OPA188,STM32F103RB --out-dir eval_pdfs
    .venv/bin/python eval/fetch_pdfs.py --all          # 全量 38 个器件
"""
import argparse
import re
import sys
from pathlib import Path

import requests

# 走系统代理访问外网（NO_PROXY 只对内网生效）； requests 默认读环境变量

def candidate_urls(part: str):
    """按器件前缀猜测各厂商的 datasheet 直链（多候选逐个尝试）。"""
    p = part.lower()
    urls = []
    # TI：symlink 直链（绝大多数 TI 型号可用）
    urls.append(f"https://www.ti.com/lit/ds/symlink/{p}.pdf")
    # ST：resource 直链
    urls.append(f"https://www.st.com/resource/en/datasheet/{p}.pdf")
    # Microchip：老文档号直链（命中率低，仅试常见 doc id）
    microchip_docids = {
        "24lc256": ["20001203f"], "mcp3008": ["21295c"], "mcp3421": ["22036c"],
        "mcp4725": ["22039d"], "mcp9700": ["21942e"],
    }
    if p in microchip_docids:
        for d in microchip_docids[p]:
            urls.append(f"https://ww1.microchip.com/downloads/en/devicedoc/{d}.pdf")
    # NXP / onsemi
    if p.startswith("tja"):
        urls.append(f"https://www.nxp.com/docs/en/data-sheet/{part}.pdf")
    if p.startswith("ncp"):
        urls.append(f"https://www.onsemi.com/pdf/datasheet/{p}.pdf")
    return urls


def verify_pdf(path: Path, part: str) -> bool:
    """校验：能打开 + 前两页文本含该型号（忽略大小写与分隔符）。"""
    try:
        import fitz
        doc = fitz.open(str(path))
        text = "".join(doc[i].get_text() for i in range(min(2, len(doc))))
        doc.close()
    except Exception:
        return False
    if len(text.strip()) < 100:
        return False
    npart = re.sub(r"[^A-Za-z0-9]", "", part).upper()
    return npart in re.sub(r"[^A-Za-z0-9]", "", text.upper())


def fetch(part: str, out_dir: Path, timeout=120) -> str:
    out = out_dir / f"{part}.pdf"
    if out.exists() and verify_pdf(out, part):
        return "cached"
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
    for url in candidate_urls(part):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, stream=True)
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200 or "pdf" not in ctype.lower():
                continue
            tmp = out.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
            if verify_pdf(tmp, part):
                tmp.rename(out)
                return f"ok <- {url.split('/')[2]}"
            tmp.unlink(missing_ok=True)
        except Exception:
            continue
    return "failed"


ALL_PARTS = [
    "OPA2993", "OPA188", "OPA2188", "OPA211", "OPA2591", "OPA620",
    "TLV886", "TLV9061", "TLV9062", "TMP117", "INA226", "ADS1115",
    "ADS131M04", "REF5025", "LM2596", "LM4040", "TPS63020",
    "SN65HVD230", "SN75176B", "THVD1450", "MF10",
    "STM32F103RB", "STM32L476RG", "STM32H743ZI", "STM32G474RE",
    "STM32G071C8", "STM32F072RB", "STM32C031C4", "STM32C5A3ZG",
    "LD1117", "24LC256", "MCP3008", "MCP3421", "MCP4725", "MCP9700",
    "NCP1117", "TJA1051",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", default="", help="逗号分隔的器件列表")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out-dir", default="eval_pdfs")
    args = ap.parse_args()

    parts = ALL_PARTS if args.all else [p.strip() for p in args.parts.split(",") if p.strip()]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)

    ok, failed = [], []
    for part in parts:
        status = fetch(part, out_dir)
        print(f"  {part:14s} {status}", flush=True)
        (ok if status != "failed" else failed).append(part)

    print(f"\n成功 {len(ok)} / 失败 {len(failed)}")
    if failed:
        print("失败清单（需人工下载）:", ", ".join(failed))


if __name__ == "__main__":
    main()
