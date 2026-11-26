# 规格表证据的召回、排序与选取

## 原版与参考实现

| 实现 | 具体做法 | 我们从中采用的设计 |
| --- | --- | --- |
| 原版 Veriquery | 向量、BM25 和结构表结果做 RRF，按文档限额后取排名靠前的结果。回答入口先尝试表格或正则提取，再把前三段清洗文本交给模型 | 保留各知识来源和混合召回，精确问答以完整的证据行参加后续排序 |
| datasheet-rag | 表格和正文分开。大表按行分段并重复表头，切块前缀带器件与章节。推荐 dense 先取 30 个，BGE CrossEncoder 用问题–证据对重排，默认取 8 段交给模型。生成结果用编号指回来源，缺证据有明确拒答前缀 | 同一个表行保留表头、页码和原文。候选池和最终上下文使用不同预算，trace 区分排序和实际送入模型的内容 |
| Docling | 表格切块可重复列标题，避免后续行脱离列含义 | 将表头用于解释数据行的列位 |
| RAGFlow | 独立设置召回、重排候选范围以及交给生成器的结果规模 | 分开调节检索覆盖和生成噪声，并记录重排与送入上下文的位置 |

[原版 RRF](https://github.com/FinalSunFlower/Veriquery/blob/663c3304ee50b7b3b0b131b1b78d6ad9b4ec4f5e/retrieval/hybrid_retriever.py#L245) · [原版回答路径](https://github.com/FinalSunFlower/Veriquery/blob/663c3304ee50b7b3b0b131b1b78d6ad9b4ec4f5e/agents/workflow_nodes.py#L403) · [datasheet-rag 切块](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/src/datasheet_rag/ingest/chunk.py#L8) · [重排](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/src/datasheet_rag/rag/rerank.py#L22) · [回答与引用](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/src/datasheet_rag/rag/pipeline.py#L34) · [生成契约](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/src/datasheet_rag/rag/generate.py#L29) · [Docling](https://docling-project.github.io/docling/concepts/chunking/) · [RAGFlow](https://ragflow.io/docs/v1.0.0/chat_configuration)

datasheet-rag 在自己的 91 道可答题上测得 Dense Hit@8 为 0.923，混合 RRF 为 0.857，Dense + Rerank 为 0.967。比较类小样本的 Hit@8 同时由 1.0 降到 0.9。它推荐 Dense + Rerank 的依据是该语料上的消融结果。本轮 Veriquery 保留原有混合召回，对证据行排序和上下文做配对测试，便于分离改动的影响。 [上游消融](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/docs/ablation.md)

## 这次代码变化

1. 原有索引仍可复用。遇到明确工况和列名的单文档问题，从召回的 SQLite 整表补出完整匹配行。同一行带表头参加 BGE 重排。匹配行有保留位，避免整表和正文副本挤掉精确证据。
2. 工作与关断电流、分流与总线 LSB、以及带温度的寄存器 HEX 等精确要求分别匹配。只在找到对应的完整行时优先使用短证据。读 HEX 只需温度和 HEX，读明确的 TYP 或 MAX 只取该列，来源引用仍保留原始整行及页码。查找失败时走通用检索。多文档比较和逐行转录沿用通用上下文。
3. Trace 保存候选表行、BGE 重排名次、最终上下文、原始引用和模型回答。证据进入模型后，生成仍可能误读，需单独验收。流程中由模型生成答复。

[证据行匹配和列选择](../retrieval/table_evidence.py) · [重排候选](../retrieval/hybrid_retriever.py) · [上下文与引用](../agents/workflow_nodes.py)

## MinerU Advanced

应用新默认配置为 `USE_MINERU_PARSE=true`、`MINERU_TIER=advanced`、`MINERU_TIMEOUT=1800` 和 `MINERU_ALLOW_LOCAL_FALLBACK=false`。MinerU 4.0.12 的服务用 `mineru-kit api-server --tier standard` 启动模型能力，解析任务请求 `advanced`。服务 `/v1/tiers` 要列出 Advanced，入库详情要报告 `parser_tier=advanced`。若请求档与返回不符，应用将失败显示给调用者。旧索引记录创建时的真实档位，需要用 Advanced 重新入库才能替换。

已在 AutoDL 真实上传 ADS1115 原第 6 页的单页样本，服务返回 `parser=mineru`、`parser_tier=advanced`、`status=ready`，保留 2 张表和 29 个块。上传到就绪约 186 秒，为该次单页实测。 [Advanced 入库记录](../data/eval/ranked-evidence/advanced-document.json) · [官方档位说明](https://github.com/opendatalab/MinerU/blob/mineru-4.0.12-released/docs/zh/usage/tiers.md)

## 最终配对回放

在 AutoDL 使用相同的 4 份已有 Standard 索引、同样的 14 道开发回归题和 bge-m3、bge-reranker-v2-m3、Qwen3.5-0.8B。只切换 `RAG_FOCUS_TABLE_EVIDENCE`。两组从相同的落盘数据复制出隔离实例。单页 Advanced 单独验收，避免把解析档位变化计入排序效果。结果各执行一次，按原表判定答案及单位、工况、拒答。 [基线 trace](../data/eval/ranked-evidence/final/baseline/results/cases.jsonl) · [候选 trace](../data/eval/ranked-evidence/final/focused/results/cases.jsonl) · [逐题复核和分母](../data/eval/ranked-evidence/manual-review.json)

| 指标 | 通用上下文 | 优先精确证据行 |
| --- | ---: | ---: |
| 7 道实际触发规则的题，完全正确 | 4/7 | 7/7 |
| 14 道回归题，完全正确 | 7/14 | 11/14 |
| 标注证据 Hit@8（12 道可答题） | 12/12 | 12/12 |
| 标注证据 MRR@8（12 道可答题） | 0.778 | 0.944 |
| 7 道触发题的上下文中位字符数 | 3,556 | 97 |
| HTTP 总耗时 P50 / P95（14 次） | 2.938 / 5.311 秒 | 1.748 / 5.146 秒 |

7 道触发题中，ADS1115 典型工作电流从关断值 `0.5 µA` 改为 `150 µA`，TMP117 在 `−25 °C` 的 HEX 从 `FF0` 改为 `F380`，INA226 总线步长从输入量程 `0–36 V` 改为 `1.25 mV/LSB`。TMP117 的完整行落盘后，老的无表头向量块曾凭 RRF 得分赢过去重，修复后的相同文档、页码和原文行会继承 SQLite 表头。ADS1115 最大值题第一轮的模型答对但追加了一个 `[200]`，正式结果保留有效的 `[1]` 并移除可由唯一来源及已写正文核对的重复尾标。

未触发规则的 ADS1115 全部采样档位、TMP117 关断和错文档题使用完全相同的上下文，仍出现采样波动。14 题的总差异含这些波动。ADS1115 单端通道数仍答成 2，原文为 4；TMP117 关断值最终回放仍有错答。当前拒答格式的自动指标只识别固定前缀，人工评审将明确写出“未指定 TYP”且引用正确表行视为正确，原始自动结果也保留。

Advanced 单页的工作 TYP、工作 MAX 和关断 TYP 三题分别返回 **150 µA、200 µA、0.5 µA**，三次都使用页内完整行。只覆盖已验收的 ADS1115 电流页。 [Advanced 逐题 trace](../data/eval/ranked-evidence/final/advanced/results/cases.jsonl)
