# 多要点证据覆盖与联合比较回归

本轮把上一轮 LM75B / MAX30205 独立评测的 11 题转为回归，重点检查初始候选到实际模型输入之间的证据损失，以及比较题能否用两边的资料给出结论。沿用原来两份 Advanced 索引、bge-m3、bge-reranker-v2-m3 和 Qwen3.5-0.8B。以同一待验收源码分别关闭和开启 `RAG_EVIDENCE_COVERAGE`、`RAG_JOINT_COMPARISON`。之前的原文核验、表行路径和引用修复开关在两组均开启。 [冻结题单](../data/eval/heldout/questions.jsonl) · [上一轮失败定位](HELDOUT_QA_RESULTS.md)

## 实现

- 多问句保留原主检索顺序及其首条结果。在候选内优先保留能覆盖用户所问带单位数值的片段，用既有重排器分别给后续问句做二次评分。余下位置按文档、页和表格/正文交替填充，避免一页的相邻表行占完上下文。原始候选的分数与顺序仍可在 trace 查看。
- 跨器件比较仍按文档分别召回，将带原文件和页码的证据交给同一次生成。输出需要有实际答复内容，且引用覆盖所有进入上下文的器件。现有路径可通过开关做 A/B。
- 温度步长比值只在两个器件各有一个明确、相容的候选值时计算。用无浮点近似的有理数比值处理摄氏度步长、mK、K 和华氏度步长的单位转换。电源适用性在识别到一个询问电压、两个一致的工作范围时做区间判断，再把结果连同原始范围交给 LLM 综合；数值不一致时留给通用联合问答。

[问答路径](../agents/workflow_nodes.py) · [证据与温度量纲处理](../retrieval/evidence_coverage.py) · [联合比较提示词](../core/llm_client.py)

## AutoDL 实机结果

最终同源码三组共完成 33 次真实聊天 HTTP 调用。每种配置每题一次，问题、模型、索引和文档范围相同。补跑仅开启联合比较这一组，是为了观察普通问答保持原上下文组织时的表现。

| 指标 | 两项均关闭 | 仅联合比较 | 两项均开启 |
| --- | ---: | ---: | ---: |
| 可答题完整正确且引用支持 | 3/9 | 3/9 | 5/9 |
| 应拒题通过 | 2/2 | 2/2 | 1/2 |
| 严格达标 | 5/11 | 5/11 | 6/11 |
| 题目所需的完整证据进入答题阶段 | 6/9 | 7/9 | 8/9 |
| 有 LLM 生成记录的题数 | 11/11 | 10/11 | 10/11 |

答题阶段包括生成模型收到的上下文与确定性计算读取的原始证据。旧路径 h07 的两份证据分散在不同模型调用中，按调用合计时计入覆盖。两组联合比较均在一个生成上下文中得到两份证据。上一轮独立评测的 3/11 与本轮旧路径回放的 5/11 都保留。旧路径在相同证据上会给出不同答案，功能开关的影响以本轮同源码对照和逐题阶段证据解释。 [三组逐题评分](../data/eval/context-coverage/release/manual-review.json)

| 题目 | 可追溯结果 |
| --- | --- |
| h02，LM75B 故障队列 | 两项皆开后，原第 1 页块 `25567d75_p1_c3` 和原第 8 页表行 `25567d75_p2_tbl19` 同时进入模型上下文。模型仍把寄存器编码当作要运算的量，答成 8。证据缺口已修复，生成仍失败。 |
| h03，LM75B 总线字段 | 三组都把 16 位总线宽度和 11 位有效数据送到模型。模型均指导读 11 位。故障属于带证据后的生成。 |
| h07，比较 5 V 供电适用性 | 早期仅把证据合在一起，模型已读出 MAX30205 的 3.3 V 上限，却仍给出 5 V 可用。最终区间检查从两条来源得到 `true/false`，联合 LLM 在两组都说对可用器件。两项均开的答复还列出了两个所问范围，因而通过完整性评分。 |
| h08，比较温度步长 | 从两份原第 1 页分别读到 0.125 与 0.00390625 °C/LSB，计算结果为 32 倍，分别引用原块。两组联合比较通过该题。 |
| h09，无法给出 SPI 模式 | 两项全开的一次答复错误地把 I²C 指针值当成 SPI Mode 0。仅开启联合比较时，该题上下文与基线一致，两份答复都选择拒答。较早一轮两项全关也曾编造 SPI 模式，样本不足以断定拒答率变化由上下文开关导致。 |
| h11，供电上限 | 两项全开时，原第 2 页块 `a7bc38d8_p2_c5` 的 +4 V 已从旧候选第 9 进入上下文。解释额定值的 `a7bc38d8_p2_c7` 仍未进模型。最终答复证据不足。 |

交付配置默认开启联合比较，暂将上下文覆盖设为显式启用。这个取舍保留了两道比较题的证据与运算改动，同时让其他问答继续用原上下文顺序。33 条实机回放都显式设置开关，交付版本调整默认值后单独复跑自动回归。该题单已用于本轮迭代，接下来仍需用新器件检查是否能稳定保全事实与拒答。

[基线原始 trace](../data/eval/context-coverage/release/baseline.jsonl) · [仅联合比较 trace](../data/eval/context-coverage/release/joint-only.jsonl) · [两项全开 trace](../data/eval/context-coverage/release/candidate.jsonl) · [早期纯联合错答](../data/eval/context-coverage/diagnostic-first/candidate.jsonl) · [早期基线编造 SPI 模式](../data/eval/context-coverage/diagnostic-range/baseline.jsonl)

[实机源码校验](../data/eval/context-coverage/source-release.sha256) · [交付源码校验](../data/eval/context-coverage/source-delivery.sha256) · [交付回归日志](../data/eval/context-coverage/delivery/regressions.log)

## 故障复盘与下一次实验

在对应交付默认开关的回放中，9 道可答题有 6 道未严格通过。其中 h03、h04、h05、h07 的完整证据已经进入同一次模型调用，分别出现字段读错、编造典型值、结论与正文冲突、漏报工作范围。另两道 h02、h11 缺少完整上下文。7 道完整证据已到答题阶段的题中，h08 走确定性计算。剩下 6 道交给 LLM 的题只有 h01、h06 严格通过。以上是同一次 11 题回放的条件计数。 [默认开关原始 trace](../data/eval/context-coverage/release/joint-only.jsonl) · [逐题判定](../data/eval/context-coverage/release/manual-review.json)

下一次先对 h03、h04、h05、h07 做生成对照。已从保存的 trace 逐字提取真实模型上下文，并与此前人工核对的短证据按题号配对。保持同一问题、提示词、解码设置和评分口径，比较当前 0.8B 与一个更强指令模型，各格重复三次。四题两种输入、两种模型共 48 次生成。评分时逐条核对所问字段、工况、结论和引用，并保存生成原文、结束原因及清洗后答案。现有常规问答上限为 256 个输出 token，部分答复在半句结束，现存日志无法可靠区分输出预算与文本清洗。 [配对诊断输入](../data/eval/context-coverage/release/generation-controls.jsonl) · [模型调用与清洗](../core/llm_client.py) · [当前引用检查](../agents/workflow_nodes.py)

已有公开实现的借鉴可追到 datasheet-rag 的真实问答回放与检索、答案分开评分。后续可借鉴 ALCE 检查每条结论是否由其引用支持，用 UAEval4RAG 的知识库相关负例检验“缺条件、错误前提、跨器件借数”等拒答风险，再参考 T²-RAGBench 的文本加表格端到端任务做独立验收。这三篇是本次核对的实验设计参考。 [datasheet-rag 评测源码](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/src/datasheet_rag/eval/run.py) · [ALCE，EMNLP 2023](https://aclanthology.org/2023.emnlp-main.398/) · [UAEval4RAG，ACL 2025](https://aclanthology.org/2025.acl-long.415/) · [T²-RAGBench，EACL 2026](https://aclanthology.org/2026.eacl-long.8/)
