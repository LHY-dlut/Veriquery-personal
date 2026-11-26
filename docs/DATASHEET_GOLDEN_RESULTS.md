# 使用 datasheet-rag 原题的评测与开源方案取舍

## 范围与样本

题库来自 datasheet-rag 固定提交 `885cbb4dacb633cc703cecad450c6be35a7b3668`。评测沿用原题号、题干、器件范围和原金标，使用 Veriquery 正常聊天 API。当前运行源码包与上一轮表行排序验收相同，SHA-256 为 `a0eafbe4098adbefec13ef1f07aa13d07190e00255fe07d2bbc9a40e10f7fc45`。 [上游题库](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/data/golden/golden.jsonl) · [厂商文件清单](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/data/manifest.json) · [题目与选页冻结记录](../data/eval/datasheet-golden/benchmark-plan.json)

37 份厂商文件中，11 份达到上游 SHA-256，17 份取到的 PDF 哈希不符，9 份未取到。按原题的器件约束，11 份匹配文件能覆盖 29 道题的器件范围。此次实际运行分为两组，保留各自的判分分母。

- **哈希匹配的 Advanced 组。** MCP3008、MCP3421、24LC256 的原件通过上游哈希校验，各用原第 1 页做入库。运行对应的 14 道原题，包含跨器件比较和两道应拒题。拒答的观察范围是实际入库页。
- **不同版本的同索引对照组。** ADS1115、INA226、OPA188、OPA211、TMP117 的原件与上游哈希不同。用已核对页码的 Standard 选页索引跑 19 道原题。两次使用相同索引和模型，只切换表行优先开关。新入库的产品默认档仍为 Advanced。

原题的 `gold_chunk_ids` 来自上游自己的切块，本次不把它转写为 Veriquery 的 Hit@K 或 MRR。证据诊断直接查本次 API 返回的检索行、上下文和引用。 [全部 37 份哈希核对](../data/eval/datasheet-golden/corpus-audit.json) · [实际回放驱动](../data/eval/datasheet-golden/benchmark.py)

### 金标与原文的冲突

`g063` 的原问题问 **24LC256**，金标写 **1.7–5.5 V**。已与上游哈希一致的 Microchip 原件第一页同时列出 `24AA256 1.7V–5.5V`、`24LC256 2.5V–5.5V` 和 `24FC256 1.7V–5.5V`。按题库和按厂商原表分别评分，原始金标可追溯。 [24LC256 原第 1 页文字](../data/eval/datasheet-golden/24LC256-page1-source.txt#L9)

## 真实问答结果

两组覆盖 33 道不同的原题，累计 52 次真实聊天 API 调用。以下统计以界面最终会收到的答案为准，包含正确的范围内拒答。答出主值但漏重要工况或混淆一个参数标签计为部分正确。公开答案拒答时，即使模型原始草稿中出现正确数字，也按最终输出计分。

| 语料和配置 | 完全正确 | 部分正确 | 错误或漏答 |
| --- | ---: | ---: | ---: |
| 5 份非哈希匹配原件的 Standard 选页，表行优先关闭 | 8/19 | 5/19 | 6/19 |
| 相同 Standard 索引、相同题，表行优先开启 | 8/19 | 6/19 | 5/19 |
| 3 份哈希匹配原件的 Advanced 选页，当前配置 | 8/14 | 0/14 | 6/14 |

Advanced 组包含 12 道可答题和 2 道应拒题，分别正确 7 道和 1 道。三份样本都实测为 `parser_tier=advanced`。在此次并行负载下，从上传到就绪的单页观测分别为 122.228 秒、234.348 秒和 101.965 秒。

19 道 A/B 题的实际模型上下文全部字节相同，精确表行优先触发 **0/19**。Advanced 组也触发 **0/14**。实际差异不能归因于这一开关。本次从 100 道题中单独静态核对，单文档且符合当前规则入口的只有 `g064/g065/g066`，仍需检索到符合条件的完整行才会真正启动。它们未纳入这 33 道。上一轮 7 道规则触发题的结果适用于定向回归。本轮验证了通用路径在上游题型上的表现。 [当前规则覆盖检查](../data/eval/datasheet-golden/focus-eligibility.json) · [逐题评分](../data/eval/datasheet-golden/manual-review.json) · [Standard 基线](../data/eval/datasheet-golden/provisional-before/results/cases.jsonl) · [Standard 当前](../data/eval/datasheet-golden/provisional-current/results/cases.jsonl) · [Advanced 问答](../data/eval/datasheet-golden/exact-advanced/results/cases.jsonl) · [Advanced 入库清单](../data/eval/datasheet-golden/exact-advanced/results/run.json)

### Trace 指向的具体问题

- **比较题的证据覆盖。** `g033` 比较 OPA188 和 OPA211 的最大供电电压。OPA188 的文档进了候选，其原第 1 页的 `4–36 V` 供电片段未出现在返回的前 12 段里。送入模型的 OPA188 片段偏向失调电压和封装，于是模型拿 `25 µV` 代替最大供电。比较题验收要分别检查每个器件的目标参数是否进入上下文。 [OPA188 原页证据](../data/eval/datasheet-golden/OPA188-source-anchors.txt) · [g033 trace 所在文件](../data/eval/datasheet-golden/provisional-current/results/cases.jsonl)
- **证据到了仍选错属性。** `g010` 的 8 档采样率完整进入上下文第 8 段，模型回答 `860–860 SPS`。`g046` 的 `64-Byte Page Write Buffer` 已出现在模型上下文，回答却用了读取边界。增加候选量对这两题缺少直接依据。 [Standard 当前 trace](../data/eval/datasheet-golden/provisional-current/results/cases.jsonl) · [Advanced trace](../data/eval/datasheet-golden/exact-advanced/results/cases.jsonl)
- **数字正确但引用缺失。** `g063` 生成草稿给出厂商原表的 `2.5–5.5 V`，`g081` 草稿包含 MCP3008 的 10 位和 MCP3421 的 18 位。两条草稿都没有任何编号引用，API 因此返回证据不足。下一轮应对同一证据测试结构化引用输出或一次引用修复回合，并保留“无有效引用拒答”的安全边界。 [原始草稿与引用检查](../data/eval/datasheet-golden/manual-review.json)

## 开源方案的取舍

| 参考实现 | 可核对做法 | Veriquery 的选择和当前验证边界 |
| --- | --- | --- |
| datasheet-rag | 表格按整行分片、重复表头，采用编号引用。其语料上推荐 Dense + CrossEncoder Rerank | 参考证据结构与配对消融，沿用我们的向量、BM25、SQLite 召回和 BGE，追加匹配表行的保留位、原行引用和投影证据。基于当前题库结果决定是否扩大触发范围。 [切块](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/src/datasheet_rag/ingest/chunk.py) · [排序消融](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/docs/ablation.md) |
| Docling、RAGFlow | Docling 保留表头和来源定位；RAGFlow 把召回、打分和送入生成的配置分开 | 借鉴表头随证据行、页码、引用与预算分离。当前源码把最终模型上下文与完整引用分开记录，便于判别是漏召回、漏选或误读。 [Docling 切块](https://docling-project.github.io/docling/concepts/chunking/) · [RAGFlow 检索配置](https://ragflow.io/docs/v1.0.0/chat_configuration) |
| ColPali / ColQwen | 以整页图像做多向量索引，使用 late interaction 找图表和版面相关页；ViDoRe 衡量页级检索 | 本项目已有电路图视觉检索接口与 MaxSim 风格实现。视觉页适合做找图和裁剪页的补充。精确的规格值仍要核对表格标签及列位。视觉检索是否能提升图表问答，需用独立的图、曲线和跨图文问题验证。 [代码](https://github.com/illuin-tech/colpali) · [ViDoRe 论文](https://arxiv.org/abs/2407.01449) · [本工程视觉索引](../ingestion/image_indexer.py) |
| RAG-Anything | 从多模态解析块生成图文有关的实体与关系，再进行跨模态检索。当前 README 对 MinerU 解析插件限定 3.x | 借鉴保留图框、标题、邻近正文的来源设计。当前仅保留两条业务路径，文本和表格问答加独立的电路图检索。跨模态图构建需要另一套标注和评测，现有 MinerU 4.0.12 也需要适配。 [RAG-Anything 实现](https://github.com/HKUDS/RAG-Anything) · [双图论文](https://arxiv.org/abs/2510.12323) · [本工程电路图接口](../api/routers/circuit.py) |

本次评测关闭视觉索引，问题只检验已解析文本和表格。图表分支的下一次对照应同时保存原页、证据区域和识别值，分别测页召回、区域命中、元件/数值+单位与引用。这样可以分离“找到了图”“看懂了图”和“用正确图回答”。本次 GitHub 方案比较以公开实现为依据，未用这些外部系统跑同题分数。
