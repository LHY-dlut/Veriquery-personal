# 多工况规格问答验收

范围为 datasheet-rag 固定版本中的 g015（OPA211 增益带宽）和 g017（TMP117 工作供电范围）。两题在上一轮 33 题开发回归中都没有答全。此次按厂商原件逐个检查条件、数值、单位和引用。g017 的上游金标只列出 1.7–5.5 V。本次多工况验收要求同时给出两个温区，所以保留上一轮已发布的总分。 [上一轮评测](QA_VERIFIED_EVIDENCE_RESULTS.md) · [本轮题目和评分要求](../data/eval/conditional-qa/questions.jsonl)

## 原文与故障定位

| 原题 | 规格书原文定位 | 上一轮存量索引及答案 |
| --- | --- | --- |
| g015 OPA211 增益带宽积 | TI SBOS377L 原 PDF 第 1 页 Features 列出 `80 MHz (G = 100)` 和 `45 MHz (G = 1)`。原第 9 页 Frequency Response 的 GBW 表行也列出这两组值 | 旧回放的第 1、2 个候选分别是表格正文和首页 Features，表格的两个独立行也已召回。最终只返回 80 MHz。故障发生在证据已召回后的答案完整性 |
| g017 TMP117 工作供电范围 | TI SNOSD82D 原 PDF 第 1 页 Features 列出 `1.7 V to 5.5 V from −55 °C to 70 °C` 和 `1.8 V to 5.5 V from −55 °C to 150 °C`。原第 6 页 Electrical Characteristics 前置条件重复这两组约束。原第 35 页供电建议仅给简写 | 历史索引的首页块 `a6fb4dd8_p1_c2` 已保留两个工况，只是温度写成 MinerU 的 LaTeX。上一轮前 12 条没有这个块，排前的是无温区的 1.7–5.5 V 简写，最终拒答。当前案例确认了召回证据筛选的缺口 |

[厂商 PDF 哈希、页映射和文本](../data/eval/conditional-qa/sources.json) · [上一轮原始 trace](../data/eval/qa-quality/after/provisional-current/results/cases.jsonl)

## 实现

- 只为明确器件且只选择一个文档的宽泛规格题启用。目前支持增益带宽与增益、工作供电范围与温区两类。系统先按原流程取 12 条混合检索候选，并针对该属性在当前文档内另取最多 12 条 BM25 候选。补取的片段只用于检验完整的工况证据。
- 需要同一个召回块中同时出现两组有标签的值、单位和条件。OPA211 的两条相邻 GBW 表行也可构成该证据，直接采用行内明确的增益、数值及单位。条件重复、完整证据彼此冲突、问题要求多个字段或证据不全时，走通用问答。
- 答复引用完整来源块，trace 记录 `verified_conditions`，保留摘录和原始文本，还可检查属性定向检索的 `condition_candidates`。使用 `RAG_CONDITION_EVIDENCE` 对照。

[工况证据核验](../retrieval/verified_passages.py) · [实际问答入口](../agents/workflow_nodes.py) · [回归用例](../scripts/test_rag_pipeline.py)

仅把混合检索的返回上限从 12 增至 24，也没有召回 TMP117 的完整首页块。对同一索引搜索 `supply range voltage temperature` 时，该块在选中文档的 BM25 结果中排名第 1。此现象决定了采用词法补证据。Advanced 样本中，OPA211 首页只有 GBW 标签，原第 9 页仍保留两组完整表行。TMP117 原第 6 页的电压和温区出现 LaTeX/OCR 字符错乱，原第 1 页保留了完整的两组条件，适合本轮取证。

TMP117 首页的召回块恰好从另一条公式的后半段开始。旧的公式转换把行首的结束美元符号当成开头，跳过了后面四个可读的温度公式。另一个问题是“检查后续是否有第三组条件”把紧邻的 `16-bit resolution` 误当作第三组。最终代码只处理明确的摄氏公式，并要求后续出现完整的同属性条件与单位组合，才判定列表超过两组。

[历史索引穷举证据](../data/eval/conditional-qa/diagnostic/TMP117-archived-search.json) · [24 条候选对照](../data/eval/conditional-qa/diagnostic/advanced/archived-after.jsonl) · [Advanced OPA211 原文块](../data/eval/conditional-qa/diagnostic/advanced/OPA211-advanced-search.json) · [Advanced TMP117 原文块](../data/eval/conditional-qa/diagnostic/advanced/TMP117-advanced-search.json)

## AutoDL 配对验收

使用同一源码包、bge-m3、bge-reranker-v2-m3、Qwen3.5-0.8B，以及每组固定的落盘索引。各组基线关闭 `RAG_CONDITION_EVIDENCE`，候选开启。题目由同一个脚本经 `POST /api/v1/chat/` 发出，合计 16 次成功响应。存量组复用 Standard 选页索引。Advanced 组新上传两份厂商 PDF 的选页，OPA211 原第 1、9 页入库得到 43 块，TMP117 原第 1、6 页得到 51 块，两份均报告 `parser=mineru`、`parser_tier=advanced`。

| 文档组与题目 | 基线答复 | 候选答复 | 候选证据 |
| --- | --- | --- | --- |
| Standard g015 | 只答 80 MHz | G=100 为 80 MHz，G=1 为 45 MHz | `e5470b5a_p2_c8`，原第 9 页 |
| Standard g017 | 只答 1.7–5.5 V，缺少条件 | 1.7–5.5 V 对应 −55 至 70 °C，1.8–5.5 V 对应 −55 至 150 °C | `a6fb4dd8_p1_c2`，原第 1 页，定向 BM25 第 1 位 |
| Advanced g015 | 只答 80 MHz | G=100 为 80 MHz，G=1 为 45 MHz | `3e721399_p2_c9`，原第 9 页 |
| Advanced g017 | 证据不足，拒答 | 1.7–5.5 V 对应 −55 至 70 °C，1.8–5.5 V 对应 −55 至 150 °C | `6bfe5d92_p1_c2`，原第 1 页，定向 BM25 第 1 位 |

按原文完整覆盖口径，两道已知目标题在两组索引共四个用例，完全正确从 **0/4 到 4/4**。对照项两组均 **3/4**。两种索引的 TMP117 温度分辨率都答出 0.0078 °C。只问 G=1 时，Standard 回答 45 MHz，Advanced 在两次回复的正文均混入 80 MHz，按有矛盾内容扣分。

最终应用环境中 24 项回归通过。上一轮九条精确摘录用已归档来源离线重放，字段和引用文字全部一致。每个开关组合只实机执行一次。另一次诊断回放中 Standard 基线曾答全 g015，确认生成端有波动。两道修复题曾用来定位故障，独立器件、多种条件分布以及 33 题的本轮总正确率仍待重新评测。

[Standard 基线](../data/eval/conditional-qa/final/archived-before.jsonl) · [Standard 候选](../data/eval/conditional-qa/final/archived-after.jsonl) · [Advanced 基线](../data/eval/conditional-qa/final/advanced-before.jsonl) · [Advanced 候选](../data/eval/conditional-qa/final/advanced-after.jsonl) · [Advanced 入库](../data/eval/conditional-qa/final/advanced-documents.json) · [回归日志](../data/eval/conditional-qa/final/regressions.log) · [源码校验](../data/eval/conditional-qa/source-run.sha256) · [模型探针](../data/eval/conditional-qa/final/model-check.log)
