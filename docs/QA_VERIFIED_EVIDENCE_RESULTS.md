# 智能问答的原文核验与配对评测

本轮针对有明确答案的规格题。上一轮的 33 道 datasheet-rag 原题已经暴露出多个“证据存在、模型仍读错属性或单位”的问题。例如 ADS1115 的完整速率枚举已经进入上下文，模型却只答 860 SPS。MCP3008 没有可核验的工作海拔，模型却把 125 °C 当成回答。此次保留现有检索、模型和索引，新增一条从已召回原文中找可直接核对证据的路径。 [上一轮逐题结果](DATASHEET_GOLDEN_RESULTS.md)

## 实现与边界

1. 限定所选单文档和明确器件型号。匹配的是原文中的参数标签、值、单位和必要限定词组成的完整短语。例如 `64-Byte Page Write Buffer` 和 `low offset voltage (25-μV maximum)`。规则只有字段表达式，数值来自实际召回片段。
2. 有唯一明确的字面依据时，返回原文摘录和文件、页码、块编号。 LaTeX 温度中的明确小数可以整理为可读形式，引用仍保存该来源的原始字符串。不同规格值并存、题目附加工况或原文格式无法可靠解释时，保留正常 RAG 路径。
3. 对工作海拔和 CPU 核心数这类明确字段，当前作用域的检索证据没有对应概念时，返回证据不足。证据提及该概念但无法精确解释格式时，仍由通用问答处理。供电比较中，模型在已核实范围之外又带入无标签电压时，使用原文供电范围摘录。
4. `RAG_VERIFIED_PASSAGES` 控制本轮新增路径。Trace 中 `answer_mode` 区分 `verified_excerpt`、`verified_abstention` 与 `generated`。摘录路径只输出已定位的证据行，没有生成模型上下文。

[原文核验模块](../retrieval/verified_passages.py) · [问答入口](../agents/workflow_nodes.py) · [回归测试](../scripts/test_rag_pipeline.py)

## 评测口径

- 固定 datasheet-rag 提交 `885cbb4dacb633cc703cecad450c6be35a7b3668` 的 33 道原题。分别沿用现存 19 道 Standard 选页索引和 14 道哈希匹配原件的 Advanced 选页索引。
- 同一 AutoDL 实例、原有 bge-m3、bge-reranker-v2-m3、Qwen3.5-0.8B，两组的应用源码、资料版本、页码及器件范围一致。基线关闭 `RAG_VERIFIED_PASSAGES`，候选开启。
- 完全正确要求答出所问字段、单位和必要工况。明显混淆其他属性或缺少关键事实记为部分正确。可答题拒答为错误，应拒题不补一个相近指标。按最终返回计分，模型未穿过引用校验的草稿单独记录。
- `g063` 同时保留厂商原文的 2.5–5.5 V 和原题金标的 1.7–5.5 V 两种统计口径。因这批原题曾用于诊断规则，应把本次结果视为回归验证。未对独立设备集估计泛化准确率。

[原题与样本定义](DATASHEET_GOLDEN_RESULTS.md) · [配对回放脚本](../data/eval/qa-quality/replay.py)

## 配对结果

共执行 66 次真实聊天 API 调用。按厂商原文口径，33 道题完全正确由 21 道增加到 26 道，即 **63.6% → 78.8%（+15.2 个百分点）**。保留 g063 的原金标时是 **20/33 → 25/33**，该题的评分口径没有改变观察到的净增量。

| 范围 | 基线完全正确 | 候选完全正确 |
| --- | ---: | ---: |
| 19 道 Standard 选页题 | 10/19 | 15/19 |
| 14 道 Advanced 选页题 | 11/14 | 11/14 |
| 33 道合计，按厂商原文 | 21/33 | 26/33 |
| 12 道实际触发本轮改动的题 | 6/12 | 12/12 |

12 道触发题包括 9 道精确摘录、2 道范围内拒答与 1 道比较题的原文回退。其余仍走原有生成流程，单次结果存在波动。总体部分正确从 6 道变为 4 道，错误或漏答从 6 道变为 3 道。 [逐题判定和分母](../data/eval/qa-quality/manual-review.json)

- OPA188 最大输入失调电压由误读零漂变为引用 `25 µV maximum`。ADS1115 分辨率由误拒答变为 `16-bit`，数据率题返回全部八个档位。
- TMP117 结果位宽由温度分辨率的误答变为 `16-bit temperature result`。OPA211 / OPA188 的供电比较分别引用原文，去掉了之前无标签的 18 V。
- MCP3008 的工作海拔在两组都拒答。24LC256 的 CPU 核心数由无规格依据的 “0 个” 变为证据不足。
- 多条件题还有残留错误。OPA211 增益带宽的生成漏掉 `G=1` 的 45 MHz。MCP3008 单端通道数的新回放出现漏答，且该题未触发新规则。TMP117 供电范围仍未完整处理温度条件。

[Standard 基线](../data/eval/qa-quality/before/provisional-current/results/cases.jsonl) · [Standard 候选](../data/eval/qa-quality/after/provisional-current/results/cases.jsonl) · [Advanced 基线](../data/eval/qa-quality/before/exact-advanced/results/cases.jsonl) · [Advanced 候选](../data/eval/qa-quality/after/exact-advanced/results/cases.jsonl)

实机回放使用同一源码包 SHA-256 `7e3936c60c60be9f0bae4d9b7fedfc7c143652fe2d12b2080c504f119136bead`。回放期间另加两项防误判保护。用捕获到的单文档检索结果离线复核，所有原题的规则选择和摘录与实机记录一致。最终本地 19 项回归通过。 [实机源码校验值](../data/eval/qa-quality/source.sha256) · [模型探针](../data/eval/qa-quality/model-check.log)
