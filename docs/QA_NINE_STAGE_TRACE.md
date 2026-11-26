# 0.8B 文档问答的九层溯源

使用已修复公式入库的源码和落盘索引副本，分别从真实 Streamlit 问答页提交六道原题一次。通过 Streamlit AppTest 操作文档选择框、问题框与发送按钮，访问回环地址上的真实 FastAPI、BGE 与 Qwen3.5-0.8B。观测脚本记录函数的实际入参和返回值。UI 端验证到 Streamlit 发出的聊天气泡 HTML，覆盖服务端渲染，不含浏览器像素、外部代理和断线重连。本轮六次流式调用是新采样；上轮 36 次非流式配对的分数沿用原报告。

## 九层证据

| 层 | 核对结果 |
| --- | --- |
| 1. 请求入口 | 页面从 ready 文档选项同步 document_id。六次真实请求体均含 query 与 document_ids，和图入口完全相同。MCP4725 对应 `c06748dc`，TPS63020 对应 `386a2473`。UI 调 `/api/v1/chat/stream`，请求体省略 session_id、trace、历史问答。schema 将 session_id 设为可选，trace 默认 false。六次提交由后端生成六个不同 session_id，即使页面保留连续的聊天历史。此行为对本轮自包含题没有造成丢题；需要依赖前文的多轮问答是单独的验收范围。 |
| 2. 文档解析 | 回用相同两份上传件对应的真实 Advanced 解析产物，按原 PDF 坐标和 PyMuPDF 文本核对 DAC 的三条分式和 TPS 电阻式的括号、减号、单位。原第 15 页均映射到选页第 2 页。两个文档共有 7 个 equation 类型块，含三个 DAC 变量组。MCP 的一个 image 和一个 chart 块各带内嵌像素及归一化 bbox；将 bbox 换算成 PDF 点并重新裁剪，128×128 灰度缩放后的平均绝对像素差分别为 5.75 和 4.60（0–255）。这是这两处裁剪的局部检查。两页 TPS 有 3 个 table 块。实际文本问答使用正文和表行，未调用视觉模型。 |
| 3. 结构化入库 | Chroma 保存 MCP 11 块、TPS 53 块，公式块带 formula 标记，MCP 三组公式和变量分别同块。TPS 的 SQLite 包含表头、行矩阵、原选页、文档 ID，FTS 包含 3 条整表记录。推荐行 `386a2473_p2_tbl44` 的文本为 `3.3 V \| 1 MΩ \| 180 kΩ`。**SQLite 有 VOUT/R1/R2 表头，独立向量行只有 section，没有 table_header**；n07 的入模资料 [3] 又取到了完整表头。当前运行索引中没有 ParamRecord、raw_row、pvals 三个专用字段。知识图库的 parameters 表使用 param_name/param_value/unit/condition 等字段；本次聊天走 Dense/BM25/Table。旧笔记中的 ParamRecord 描述属于早期实现。 |
| 4. 检索召回 | 完整保存三个 store 的候选（ID、原文、metadata、分数），以及包装成 hybrid 候选后的顺序。64 个向量块与 BM25/SQLite 索引来自上一轮修复后的数据。Chroma 的过滤白名单是 document_id、document_ids、page，作用域下传，exclude_source=param_record 在 Dense 返回后另行执行。这里的白名单是查询过滤白名单；例如 formula 虽不在过滤白名单，仍真实落盘。n10 两个文档独立检索，MCP 原始 Dense/BM25/Table 的返回数是 11/9/0，TPS 是 53/30/3。MCP 没有 SQLite 结构表，其公式由正文索引召回。 |
| 5. 融合重排 | 实际权重 Dense 0.50、BM25 0.35、Table 0.15，RRF 的 k=60。以未归一化分数逐项重算所有返回的 RRF 项，并逐条验证 BGE 请求 documents 的顺序、返回 index 对应 chunk_id、重排后的 ID 顺序，7 个检索分支全部一致。n10 的目标 LSB 块在 MCP 路 Dense 第 3、BM25 第 3、RRF 第 2、BGE 第 3、该文档 top-6 第 3。本次它没有在 RRF 候选窗口消失。 |
| 6. 最终证据 | 保存实际出网的 chat/completions messages、编号上下文和对应 chunk_id。n10 两路交错后 LSB 块居总候选第 5。联合上下文每个文档最多保留两块，且单段有字符上限。发给 LLM 的 MCP 片段顺序是 DNL、INL，理想 LSB 关系和变量说明都缺席。TPS 的 495/500/505 mV 反馈行仍在 [4]。n07 则在同一次模型输入里看到 [1] 式 (3) 与 500 mV、[2] 推荐行、[3] 含表头的完整推荐表。 |
| 7. 决策生成 | n07 模型原始响应已把 R1 改叫反馈电压，把 `180 × (6.6−1) = 1008` 写成 1008 mV；它还把输入 [5] 的另一张电气特性表当作依据。最终引用重编号为 [1]，指向选页第 1 页。公式实际在选页第 2 页。n02 的后台首答算到 1 mV，用满 256 个输出 token，止于十六进制换算的开头。因首答缺有效编号触发引用修复，第二次生成直接拒答。六道题总计 7 次生成，均核对到了实际 0.8B UID。 |
| 8. 最终展示 | 六个会话均对比了 SSE chunk 拼接、complete.response、UI 历史中的 assistant.content 和解码后的聊天气泡正文，六组文本一致。页面引用卡按“文件名＋选页”去重，只展示文件、页码和页图入口。n03 的 3 个 API 引用显示为 2 张卡；同页不等于同一证据块。公开 CitationInfo 暴露 file、page、text_snippet、section，内部 chunk_id/document_id 需看审计记录。另一个已证实的故障位于**生成后的服务端文本清洗**：此前 n06 长预算原始回答有 200 kHz，清洗只留前 12 段后丢失；单独移除该上限即可保留。勿将该诊断与本次流式 n06 混算。 |
| 9. 评测评分 | 沿用原题单、厂家原页、关键算式与冻结的匿名严格口径。上轮 0/18 → 0/18 来自逐份核对。本轮额外运行项目的可选 Judge，用同款 0.8B 对六份实际显示答案交叉检查，每条输入包括 question、reference、answer、实际 context。只有 n04、n10 返回可解析评分，两者均为 correctness=1、faithfulness=1，理由却主要在复述参考答案。其余 4 条报 JSON 提取/解析错误，其中 3 条正好用满 Judge 的 400 token 上限。Judge 没有收到独立的最终引用列表，本身也不足以核验“该编号是否支持该断言”。原始 Judge 消息、返回正文、解析结果和理由分别留存，解析失败单列。 |

[六个会话与机器校验结果](../data/eval/nine-stage/results/verified.json) · [全量阶段快照（gzip JSONL）](../data/eval/nine-stage/results/events.jsonl.gz) · [实际 SQLite 字段和库状态](../data/eval/nine-stage/results/store-schema.json) · [已落盘的 Chroma 块](../data/eval/formula-ingest-ab/results/after/indexed.json) · [结构表原始行](../data/eval/formula-ingest-ab/results/after/indexed-tables.json) · [PDF 裁剪与图像对照](../data/eval/nine-stage/material/crop-audit.json) · [原件公式坐标](../data/eval/unseen-model-ab/formula-source-audit.json) · [Judge 全部输入、原始输出与判定](../data/eval/nine-stage/results/judge.jsonl)

## 六个真实请求的首个主要断点

| 题号 | 有据结论与实测断点 | 逐层记录 |
| --- | --- | --- |
| n02 | 应答 1 mV、1536 码、1.536 V。步长证据 Dense、BM25、RRF、BGE 都第 1。首答只完成第一问，修复后统一拒答。 | [候选、实际 messages 和生成原文](../data/eval/nine-stage/results/n02-chain.json) |
| n03 | DNL 与 LSB 公式均在前几条输入，模型投入大量篇幅解释理想步长，正文停在变量名，缺有符号的 +0.25 LSB。 | [同会话记录](../data/eval/nine-stage/results/n03-chain.json) |
| n04 | LSB 与 INL 公式都进入输入。模型用端点表达式的 4095 替代参考电压表达式的 4096，又把单步值当成码 1000 的理想输出，漏报 −1.5 LSB。 | [同会话记录](../data/eval/nine-stage/results/n04-chain.json) |
| n06 | 表行所需的 2200、2400、2600 kHz 到达模型。本次页面输出有 0.4 MHz 和下限的 −200 kHz，在第二段中止，没有完整给出最大绝对偏差的结论。只看数字命中会高估正确性。 | [同会话记录](../data/eval/nine-stage/results/n06-chain.json) · [独立长预算清洗诊断](../data/eval/formula-ingest-ab/results/budget-diagnosis.json) |
| n07 | 公式和推荐行分别排 BGE 第 1、2 位。算出数字 1008，模型原文已写错对象和单位，且引用了错误页。正确结果为理论 1008 kΩ、表荐 1 MΩ。 | [完整链路](../data/eval/nine-stage/results/n07-chain.json) · [实际页面请求、回包和气泡](../data/eval/nine-stage/results/n07-ui.json) |
| n10 | 应得 (505−495) mV / (1 mV/步) = 10 步。正确 LSB 公式在候选中排 MCP 第 3，被每文档两块的上下文配额挡掉；模型转而计算 DNL 并套用不相关的输出电压范围。 | [完整链路](../data/eval/nine-stage/results/n10-chain.json) · [实际页面请求、回包和气泡](../data/eval/nine-stage/results/n10-ui.json) |

## 修复优先级与验收

1. 用已保存的 n06 长回答做清洗回归，要求清洗后同时保留全宽与单侧偏差及其单位。这个样本可以逐字验收，无需再次采样。
2. 用 n10 检验多文档证据组装。发送前要求在同一份 messages 中定位到 MCP 的步长与参考电压关系，以及 TPS 带 PS/SYNC 工况的反馈端点。单看总体 Hit@K 会把本题误判成已解决。
3. 用 n07 检验参数角色与量纲。保留 MIN/TYP/MAX 与普通列名两类表头；把 R1、两端电压、R2、理论结果和表荐值分别绑定引用。只有能定位操作数和单位时才执行算术。评分沿用整题口径，同时记录草稿、清洗和引用修复各自的得失。
