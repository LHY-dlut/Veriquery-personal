# 指定增益工况的规格书问答

上一轮 Advanced 回放中，用户只问 OPA211 在 `G=1` 时的增益带宽积。排序结果已召回两条不同增益的 GBW 表行，但原通用生成把同表 `G=100` 的 80 MHz 也写入答案。TI SBOS377L 原第 9 页的 GBW 行给出 `G=1 → 45 MHz` 和 `G=100 → 80 MHz`。 [上一轮 Advanced 逐题 trace](../data/eval/conditional-qa/final/advanced-after.jsonl) · [TI OPA211 规格书](https://www.ti.com/lit/ds/symlink/opa211.pdf#page=9)

这次复用单文档工况证据路径和现有检索结果，增加显式增益的字段级匹配。问题必须只询问命名器件的 GBW，并带有一个明确的数值增益。代码核对 GBW/GBP 的参数名、同一条证据中的 `G` 或 `A_V`、数值和频率单位。数值比较保留正负号，使用实际量纲消除不同单位表示的重复。数字从原文读取。

找到唯一可以核对的值时，回答只引用该工况。找到独立的原始表行时优先引用该行，trace 中的 `verified_gain` 可定位到原始文件、页码和块号。候选证据里没有同工况同属性的值，或出现不同值时，返回证据不足。trace 分别记录 `gain_evidence_missing` 与 `conflicting_gain_evidence`。带有多个工况、其他参数或明确 MIN/TYP/MAX 的问题仍按各自原有路径处理。本轮用独立开关 `RAG_EXACT_GAIN_EVIDENCE` 保留旧的多工况证据路径，单独测这次改动。 [证据匹配实现](../retrieval/verified_passages.py) · [实际问答入口](../agents/workflow_nodes.py)

## 独立器件的原文口径

选用 Analog Devices 的 ADA4625-1/ADA4625-2 Rev. C，取 PDF 原第 4、6 页作为 Advanced 选页样本。两页都使用 `A_V`，并把参数、符号按相反于 TI 表格的顺序排列。

| 证据 | 原文数值 | 评测要求 |
| --- | --- | --- |
| 原第 4 页 Table 2，±18 V 供电条件见原第 3 页 | GBP 在 `A_V=100` 为 18 MHz。Unity-Gain Crossover 在 `A_V=1` 为 12.4 MHz | 询问 `A_V=100` 但没有指定供电时，识别两组电压对应不同值，或保守拒绝给出唯一值 |
| 原第 6 页 Table 3，5 V 供电条件见原第 5 页 | GBP 在 `A_V=100` 为 16 MHz。`A_V=1` 的 Unity-Gain Crossover 为 11.2 MHz，−3 dB 带宽为 16 MHz | 询问 `A_V=1` 时，返回指定 GBP 证据不足 |

[ADI 官方规格书](https://www.analog.com/media/en/technical-documentation/data-sheets/ADA4625-1-4625-2.pdf) · [固定题目及评分要求](../data/eval/exact-gain/questions.jsonl)

真实 Advanced 表行把增益符号单独序列化为 `$A_V$ = 100`，第一次实现把这种写法漏判成没有证据。根据入库后的原始块补齐格式后，离线分别输入两页的真实块，抽取到 **18 MHz** 和 **16 MHz**。把两页同时输入，得到“同一增益有多个数值”的冲突原因。原第 6 页的 16 在 Advanced 文本中落入靠前的数值列，而原 PDF 标在 TYP 列。本轮只校验该行的增益、数值和单位。显式最值、典型值和跨页供电条件需另外验证。

## AutoDL 配对回放

沿用 bge-m3、bge-reranker-v2-m3 和 Qwen3.5-0.8B，固定两组索引。回归组使用已有的 OPA211、TMP117 Advanced 选页。独立器件组用真实上传接口将 ADA4625 选页以 MinerU Advanced 入库，得到 2 张表、81 个检索块。两组均经 `POST /api/v1/chat/`，仅切换 `RAG_EXACT_GAIN_EVIDENCE`。按题目要求核对数值、工况和拒答，同时检查引用原始块是否实际含有该值。

| 题目 | 关闭指定增益证据 | 开启指定增益证据 |
| --- | --- | --- |
| OPA211，`G=1` | 把 45 MHz 和 80 MHz 都归给 `G=1` | 仅给出 45 MHz，引用 `3e721399_p2_tbl17` |
| OPA211，`G=100` | 结论给出 80 MHz，说明误把 100 当作增益带宽积 | 给出 80 MHz，引用 `3e721399_p2_tbl16` |
| OPA211，`G=10` | 把 `G=100` 的 80 MHz 用在 `G=10` | 拒答，候选缺少该增益下的 GBW 证据 |
| ADA4625-1，`A_V=100` | 直接选了 16 MHz，正文还把源表的 +100 读成 −100 | 两条证据 `93981b40_p1_tbl7` 和 `93981b40_p2_tbl44` 均命中。因同一增益的数值不同而拒答 |
| ADA4625-1，`A_V=1` | 给出 16 MHz，混入相邻频率指标或其他增益的 GBP | 拒答，候选缺少该增益下的 GBP 证据 |

最终 **14 次真实 HTTP 调用**成功。按整段答复的严格口径，五个指定工况用例从 **0/5 到 5/5**。只按结论计分时，旧版 `G=100` 可计为正确，结果为 **1/5 到 5/5**。两条宽泛工况对照在开关前后均为 **2/2**。最终 27 项自动回归通过。较早一次实机回放中，旧版 `G=1` 曾给出 45 MHz，引用的首页块只有属性标签。这也说明生成答案和引用块都需要独立核对。

本轮覆盖显式单增益，新增的 ADI 器件参与了表示格式适配，结论适用于这次选页与冻结题单。供电与增益同时作为限定条件的问答仍需验收。遇到冲突时，聊天界面目前显示通用的证据不足，具体原因和证据保存于 trace。

[回归基线](../data/eval/exact-gain/final/regression-before.jsonl) · [回归候选](../data/eval/exact-gain/final/regression-after.jsonl) · [ADI 基线](../data/eval/exact-gain/final/holdout-before.jsonl) · [ADI 候选](../data/eval/exact-gain/final/holdout-after.jsonl) · [较早的基线](../data/eval/exact-gain/diagnostic/first-regression-before.jsonl)

[PDF 哈希和原页](../data/eval/exact-gain/final/ada-source.json) · [Advanced 入库记录](../data/eval/exact-gain/final/ada-document.json) · [真实入库原文块](../data/eval/exact-gain/final/ada-advanced-evidence.json) · [逐页抽取记录](../data/eval/exact-gain/final/ada-per-page-extractor.json) · [回归日志](../data/eval/exact-gain/final/regressions.log) · [源码校验值](../data/eval/exact-gain/source-final.sha256)
