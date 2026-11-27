# Veriquery 文档问答全链路复盘

**2026-11-27｜AutoDL 实机｜Qwen3.5-0.8B**

同批九道问题通过真实问答 API 各运行一次，**严格通过 7/9**。两份失败答复都在实际模型消息里收到了所需公式、变量或表格。随后在网页独立复测四题，**严格通过 2/4**，暴露了另一种错误：数值算对，但把相邻行的工况写进答案。同步 API 与网页提交是不同会话，分别计分。

当前最先要解决的是**生成时把数值绑定到正确的物理量、单位和原文行**。也发现一处独立的入库问题：推荐电阻表的独立向量行缺列名，出现内部表序号与厂家表号不一致。整表在本次仍到达模型，现有证据不足以把模型错答直接归因于这个入库问题。

[原始证据 ZIP](evidence/leader-audit/VERIQUERY_AUDIT_EVIDENCE_20261127.zip) · [逐题阶段索引](evidence/leader-audit/case-index.json)

## 复现范围与口径

- 重新走上传接口，分别解析 MCP4725 原第 1、15 页，TPS63020 原第 6、15 页，AD5300 原第 1、8 页。三份 PDF 共六个选页，均返回 MinerU 4.0.12 Advanced，分别落盘 11、53、19 个向量块。样本页码是上传件内的第 1、2 页。
- 使用本次新索引、bge-m3、bge-reranker-v2-m3 和 0.8B。普通生成预算为 512 token，温度 0.1。八个关键源码文件在本地与实机的 SHA-256 一致。观测脚本保留原业务实现的返回值，并记录请求和中间结果。
- 沿用上一轮题目与已定义的证据匹配口径。以型号、数值、单位、关键推导和引用支持关系逐份人工评分，额外的错误事实也计失败。本轮使用已知回归题，统计只适用于这批选页与请求。

[Microchip MCP4725](https://ww1.microchip.com/downloads/aemDocuments/documents/MSLD/ProductDocuments/DataSheets/MCP4725-Data-Sheet-20002039E.pdf) · [TI TPS63020](https://www.ti.com/lit/ds/symlink/tps63020.pdf) · [ADI AD5300](https://www.analog.com/media/en/technical-documentation/data-sheets/AD5300.pdf)

## 分层复核

| 环节 | 本轮发现 | 核对依据 |
|---|---|---|
| 请求入口 | 9 个 API 请求和 4 次实际界面提交的题面及文档 ID 均与工作流入口一致。跨器件题确实选择 MCP4725 与 TPS63020 | 每题 `api.request`、`graph_entry`、`ui.request.body` |
| PDF 解析 | 三次任务完成。核对到 MCP4725 的 6 个公式或变量块、TPS 的式 (3) 与推荐电阻表、ADI 的传递公式及码域。总解析准确率需要更大的逐页标注样本 | `inputs/`、`raw/*/job.json`、`raw/*/structured_content.json`、PDF 裁图 |
| 结构化入库 | TPS 推荐电阻表的独立行 `fc04aab8_p2_tbl44` 缺 `table_header`，其 `section` 写成“表 2 第 3 行”。原厂是 Table 3；SQLite 整表仍有 `VOUT / R1 / R2` | `index/indexed.json`、`index/indexed-tables.json`、TPS 原第 15 页 |
| 三路召回 | 9 题合计 24 项题目所需事实，在三路候选并集中找到 **24/24**。例如 n07 的 Dense、BM25、Table 原始候选数分别为 **53、11、3**，完整推荐表由结构化路返回 | 每题三个 `*_raw` 事件及对应包装后的候选 |
| RRF 与重排 | 13 个会话的 15 个检索分支中，逐项复算 RRF 分数并核对 BGE 请求位置、返回索引和最终候选次序，均一致。9 个主请求的重排后前八候选覆盖 **24/24** 项事实 | `_rrf_fusion_input/output`、`_rerank_candidates_input/output`、`backend_http` |
| 最终上下文 | 9 个主请求共 **24/24** 项事实保留。生成路径核对了实际首轮模型消息；程序路径核对了决策所读正文和采用的引用 | `assembled.context`、下游 `messages`、`calculation_decision` |
| 数值与答案生成 | n03、n07 的同步首稿已出现错误。浏览器 n06 的三个读数都对，额外工况错误在首稿中已出现 | `backend_http.reply.choices[0].message.content`、`model_clean` |
| 引用与展示 | 错答的引用编号通过了校验。四次网页提交中，SSE 内容、最终回包与界面显示的正文一致。n04 的两个同页来源分别保留编号并能展开 | `citation_check`、`graph_output`、`*-ui.json` |
| 评测 | 同步通过 n02、n04、n06、n10、v1、v2、v3；n03、n07 未通过。页面通过 n04、n10；n06、n07 未通过 | `answers.jsonl`、`case-index.json`、逐题会话证据 |

24 项事实按照每题所需的公式、关系、位数或带列名表行计数。同一事实在不同题中出现时，按每题分别计一次。该覆盖率衡量本批已标注的证据。

## 三个实际失败与一个入库缺陷

### 1. n03｜首个错误发生在量纲换算和运算

原 PDF 第 15 页式 4-1 给出理想步长关系，式 4-3 给出 `DNL = (ΔVOUT − LSB) / LSB`。理想步长块 `3d3c99b3_p2_c8` 和 DNL 块 `3d3c99b3_p2_c10` 都在上下文中。DNL 块在 Dense 第 2、BM25 第 1、RRF 第 2、BGE 第 1。

模型首稿先写出理想步长 **0.001 V** 和实际步长 **0.00125 V**，接着执行 `(0.00125 − 1) / 1`，最终报 **−0.99875 LSB**。统一为毫伏后应为 `(1.25 − 1) / 1 = +0.25 LSB`。首稿 496 token，已有完整错误结论，清洗与 API 最终答案均保留了该错误。引用编号有对应的公式块，但公式块无法支持错误的代入。

证据入口为 ZIP 内 `cases/n03.json`，会话 `33a9d727-66ff-4c60-9349-5a7ab747641b`。原式见 `pdf-excerpts/MCP4725-p15-DAC-formulas.png`。

### 2. n07｜首个错误发生在生成时对参数的绑定

原 PDF 第 15 页说明 `V_FB` 标称为 **500 mV**，式 (3) 计算的是 **R1**。题给 VOUT=3.3 V、R2=180 kΩ，因此理论值是 `180 kΩ × (3.3 V / 0.5 V − 1) = 1008 kΩ`。Table 3 的 3.3 V 行给出实际推荐 **1 MΩ**。

| 观测 | 原始证据 |
|---|---|
| 必要证据已到达 | 两个会话的上下文都包含式 (3) 与 500 mV 的同一块 `fc04aab8_p2_c12`，以及含列名和 3.3 V 推荐值的整表 `table_79f2736d`。完整推荐表首次命中的 RRF 排名为第 10，BGE 提升到第 3 |
| 同步首稿错报物理量和单位 | 把求解变量写成 `V_FB_PWM`，把 **1008 kΩ** 写成 **1008 mV**。首稿还拿电气特性表的反馈电压范围去核对这个错误量，引用校验接受了编号 |
| 网页首稿错取相邻行 | 把 **3.6 V 行的推荐 R1=1.1 MΩ** 当成 **3.3 V 工况的标称反馈电压**，作为除数代入，得到 **−174.6 kΩ**。错误在首稿中出现，界面如实显示 |

证据入口为 ZIP 内 `cases/n07.json`。同步会话 `981e5d41-922b-4035-b648-d2bca28816be`，网页会话 `d299eb5d-7cdb-47`。相邻行、单位和公式的原图见 `pdf-excerpts/TPS63020-p15-equation-and-table.png`。

### 3. n06｜首稿将相邻行的工况套到了目标行

原 PDF 第 6 页的振荡频率行是 **MIN 2200 / TYP 2400 / MAX 2600 kHz**，其 TEST CONDITIONS 单元格为空，表首有通用测试条件。**0°C ≤ TA ≤ 85°C** 在同一张表中属于 **Minimum input voltage for start-up** 的一行。

同步 API 正确给出全宽 **0.4 MHz**、最大单侧偏差 **0.2 MHz**。独立网页会话也算出这两个值，但首稿把 **0°C 至 85°C** 写成了振荡频率的试验条件。正确的振荡行和错误借用的启动行都在同一张入模表里。页面、SSE 回包和工作流结果一致，严格评分因工况错误未通过。

证据入口为 ZIP 内 `cases/n06.json` 的 `ui_events`，网页会话 `a085eadc-47f5-46`。原表见 `pdf-excerpts/TPS63020-p6-electrical-characteristics.png`。

### 4. TPS Table 3｜列语义在独立行入库时丢失

MinerU 原始 `pages[1].blocks[22]` 保留了 `VOUT | R1 | R2` 与三行数据。SQLite 的整表也保留三列。Chroma 中单独的推荐行是 `3.3 V | 1 MΩ | 180 kΩ`，缺列名，且显示内部表序号“表 2”。源码的表头识别覆盖 MIN/TYP/MAX 等显式标题，当前未识别这个普通三列表头。这是可在索引阶段直接复现的问题，和 n07 这次已证实的生成错误分别处理。

证据入口为 ZIP 内 `raw/TPS63020/structured_content.json`、`index/indexed.json` 和 `index/indexed-tables.json`。[表头识别源码](../retrieval/table_evidence.py)

## 用 n10 验证跨器件链路

MCP 的理想步长公式在该文档分支内由 **Dense 第 3、BM25 第 2 → RRF 第 2 → BGE 第 3**。双文档候选交错后位于第 5，最终正文含完整公式和变量定义。TPS 的带表头原行 `fc04aab8_p1_tbl7` 在最终上下文 [8]，其中明确给出 `PS/SYNC = VIN` 下的 **495、500、505 mV**。

计算路径绑定了双方证据，得到 `(505 − 495) mV / (1 mV/步) = 10 步`。同步和网页两次都引用了 MCP 的公式块和 TPS 的独立反馈行。两份回执的下游答案生成调用均为 0。证据入口为 ZIP 内 `cases/n10.json`。

## 建议验收顺序

1. 先检查生成答复中的参数身份与量纲。让 n03 的两个操作数抵达同一电压单位，让 n07 的计算结果保留电阻单位，并让每个操作数能指回具体来源。以这两个现存失败会话作为回归，再加不同器件和不同量纲验证。
2. 给普通多列表格的行明确保留列名与厂商表号。验收时比较 PDF、MinerU 原始块、SQLite 整表、向量行和模型实收内容。频率表用 n06 核验工况不会跨行。
3. 提升引用校验粒度。用 n03、n07 验证仅有合法引用编号时仍可识别不受该片段支持的数值或断言。

## 原始证据包怎么查

ZIP 内 `cases/<题号>.json` 同时提供请求、实际下游模型消息与返回、逐层候选、最终引用，以及该题存在时的独立界面会话。 `raw/<器件>/` 包含 MinerU 原始 job、Markdown、middle JSON、structured content 和 PDF 提取文本。`inputs/` 是本次上传原件，`pdf-excerpts/` 是可直接打开的原页裁图。 `source.sha256` 与 `documents.json` 分别固定源码文件和选页样本指纹。
