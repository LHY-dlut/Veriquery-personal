"""基线评测执行脚本：跑 5 条参数用例(×3) + 4 条引脚用例。"""
import json
import sys


import requests

API = "http://127.0.0.1:8000"
PROXIES = {"http": None, "https": None}
DOC = sys.argv[1] if len(sys.argv) > 1 else open("/tmp/docid.txt").read().strip()

PARAM_CASES = [
    ("P1", "BSO301SP 的击穿电压 V(BR)DSS 是多少？", "−30 V；条件 VGS=0V, ID=−250µA"),
    ("P2", "TA=25℃ 稳态条件下 BSO301SP 连续漏极电流 ID 是多少？", "稳态 −12.6 A；脉冲 −14.9 A"),
    ("P3", "VGS=-10 V，ID=-14.9 A 条件下 BSO301SP 的 RDS(on) 最大值是多少？", "8.0 mΩ"),
    ("P4", "VGS=-4.5 V，ID=-12 A 条件下 BSO301SP 的 RDS(on) 最大值是多少？", "12 mΩ"),
    ("P5", "BSO301SP 工作结温范围是多少？", "−55℃ ~ +150℃"),
]
PIN_CASES = [
    ("N1", "BSO301SP 引脚定义", "PG-DSO-8；1/2/4=S, 3=G, 5/6/7/8=D"),
    ("N2", "BSO301SP pinout", "同上"),
    ("N3", "SI2301 引脚定义", "应提示文档无该器件，禁止复用"),
    ("N4", "SOT-23-8 引脚定义", "应提示需完整型号"),
]


def ask(q):
    try:
        r = requests.post(f"{API}/api/v1/chat/", json={"query": q, "document_ids": [DOC]},
                          timeout=180, proxies=PROXIES)
        if r.status_code != 200:
            return f"[HTTP {r.status_code}] {r.text[:120]}"
        d = r.json()
        ans = (d.get("response") or "").replace("\n", " ").strip()
        cites = d.get("citations") or []
        pages = ",".join(str(c.get("page")) for c in cites[:2])
        return f"{ans[:220]}" + (f"  [引用p{pages}]" if pages else "  [无引用]")
    except Exception as e:
        return f"[异常] {e}"


out = []
print("=" * 90)
print("参数用例（各 3 次）")
print("=" * 90)
for cid, q, truth in PARAM_CASES:
    runs = [ask(q) for _ in range(3)]
    same = len(set(runs)) == 1
    print(f"\n[{cid}] {q}")
    print(f"   标准: {truth}")
    for i, r in enumerate(runs, 1):
        print(f"   第{i}次: {r}")
    print(f"   三次一致: {'是' if same else '否（抖动）'}")
    out.append({"id": cid, "query": q, "truth": truth, "runs": runs, "stable": same})

print("\n" + "=" * 90)
print("引脚用例")
print("=" * 90)
for cid, q, truth in PIN_CASES:
    r = ask(q)
    print(f"\n[{cid}] {q}\n   标准: {truth}\n   实际: {r}")
    out.append({"id": cid, "query": q, "truth": truth, "runs": [r], "stable": None})

with open("/tmp/baseline_results.json", "w", encoding="utf-8") as f:
    json.dump({"doc": DOC, "results": out}, f, ensure_ascii=False, indent=2)
print("\n结果已写入 /tmp/baseline_results.json")
