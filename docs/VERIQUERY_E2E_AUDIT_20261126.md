# Veriquery 全流程问题与追溯证据

实测日期为 2026-11-26，北京时间。工作目录为 `D:\Project\veriquery\Veriquery-personal`。本报告汇总本轮三份新文档、此前两份诊断样本和 OPA211 部署验证中的已知问题。运行复现、代码静态风险和未覆盖项目分别标明。

## 1. 本轮做了什么

重新启动已有的 MinerU 4.0.12、Xinference 和应用诊断进程，复用模型权重与依赖。通过真实上传和 Chat HTTP 接口测试，使用进程级插桩记录解析产物、分块、每路召回、RRF、Rerank、Resolver 提名、模型上下文和引用。三个文本模型的独立 API 探测均通过。服务器与本机的 `core/param_resolver.py`、`retrieval/hybrid_retriever.py`、`agents/workflow_nodes.py` SHA-256 一致。应用源码未改动。

| 文档 | 完整原件／实际解析的原 PDF 页 | 文档 ID | 实际入库 |
|---|---|---|---|
| TMP117 Rev. D | 50 页，选第 1、6、13 页 | `d319889a` | 3 页，3 张表，107 个 chunk |
| INA226 Rev. C | 41 页，选第 1、5 页 | `8fda09af` | 2 页，3 张表，96 个 chunk |
| OPA188 Rev. C | 42 页，选第 1、5、6 页 | `5d087fb5` | 3 页，3 张表，201 个 chunk |

服务器下载 ST 的 STM32F103RB 原件时收到 HTTP 567，本轮选用 OPA188。三份通过 `%PDF-` 签名检查的完整原件和选页衍生文件均保留。原件与样本 SHA-256 见 `manifest.json`。三份样本的 MinerU 产物均显示 `success`，并保存了原始 `structured_content`。

数据手册：[TI TMP117](https://www.ti.com/lit/ds/symlink/tmp117.pdf) · [TI INA226](https://www.ti.com/lit/ds/symlink/ina226.pdf) · [TI OPA188](https://www.ti.com/lit/ds/symlink/opa188.pdf)

**本轮 11 道正常问题中，答案判为 2 项通过、2 项部分正确、7 项错答或漏答。** 其中 1 项通过是正确拒答，仍有无关引用。部分正确指数字正确但凭空加上了「最大值」列语义。另有 3 个仅绕过 Resolver 的对照。14 次请求均为 HTTP 200 且 `success=true`。这是有针对性的故障样本，统计只描述本批用例。

## 2. 逐题结果

下表页码为**原 PDF 页码**。追踪里的页码对应选页衍生文件。TMP117 样本第 2、3 页对应原第 6、13 页，INA226 样本第 2 页对原第 5 页，OPA188 样本第 2、3 页对原第 5、6 页。问句和参考答案在发出请求前已保存于 `questions-gold.json`，完整响应见 `cases.jsonl`。

| 用例 | 原文与预期 | 实际答复 | 判定与首个已定位环节 |
|---|---|---|---|
| `tmp-resolution` 温度分辨率 | 第 13 页，0.0078125 °C/LSB，接受 0.0078 的近似 | 最大温度精度误差 0.1 °C，引用原第 6 页的精度表 | **错答。** 分辨率证据在向量第 27，未进入前 5 条 RRF。随后 Resolver 以精度参数代答 |
| `tmp-shutdown` 25 °C、串行总线空闲时的典型关断电流 | 第 6 页，0.15 µA，即 150 nA | 给出正常工作电流 3.5 µA 作为参考，并称找不到关断值 | **漏答。** 表格转换误将 0.15 写为 MIN。正确关断行未进最终上下文，模型所见的主要数值属于正常工作模式 |
| `tmp-accuracy` −20 至 +50 °C 的最大测温误差 | 第 1、6 页，±0.1 °C | 最大值 0.1 °C，引用第 6 页对应温区行 | **通过。** 最大误差幅值正确，原表行支撑该温区 |
| `tmp-hex` −25 °C 对应的寄存器十六进制值 | 第 13 页，`F380` | 最大温度精度误差 0.25 °C，引用原第 6 页另一温区 | **错答。** `F380` 已入库，向量第 42，RRF 前 5 未保留。Resolver 又把寄存器题发给精度参数 |
| `ina-shunt-lsb` 分流电压每 LSB | 第 5 页，2.5 µV | 1.25 mV，引用总线电压行 | **错答。** 正确分流表行全程排首位，语义提名仍选择另一测量通道 |
| `ina-bus-lsb` 总线电压每 LSB | 第 5 页，1.25 mV | 「最大值 1.25 mV」，条件为 Bus voltage | **部分正确。** 数字、单位和通道对应，但把步进值写成最大值 |
| `ina-current` 典型电源电流 | 第 1 页，330 µA | 说典型值未给出，列出 `max=INA226`，引用第 5 页热阻表头 | **错答。** 表头进了参数索引，Resolver 将其当成电流候选。真实电流文本在召回首位 |
| `ina-wrong-doc` 范围控制 | 仅选 INA226 文档，询问 TMP117 分辨率，应拒答 | 「文档中未找到相关内容」，附两条 INA226 无关引用 | **答案通过，引用有问题。** 本例没有输出域外数值，拒答仍附上了无关候选 |
| `opa-offset` 25 °C、高压工作下最大输入失调电压幅值 | 第 5 页，25 µV，表列为 ±25 µV | 回报工况冲突，展示 ±0.085 µV/°C 的漂移行及另一工作区表行 | **漏答。** 正确 ±25 µV 记录仍在候选，最终给出了不同量纲和温区的信息 |
| `opa-slew` G=+1 的典型压摆率 | 第 5、6 页，0.8 V/µs | 说未给典型值，列 `max=0.8` | **漏答。** 同一参数在两页被分到不同列。检索已召回正确数值，选用的记录列位错误 |
| `opa-gbw` 增益带宽积 | 第 1、5、6 页，2 MHz | 「最大值 2 MHz」，引用第 5 页 | **部分正确。** 数字和单位对，原表第 5 页为 TYP |

## 3. 问题清单与可追溯证据

### A. PDF 与 MinerU

1. **原始解析会丢失表格列位或多工况的配对。** 本轮 TMP117 第 6 页的布局文本中，TYP/MAX 分别从字符位 132/145 开始，`0.15`/`0.5` 分别位于 132/147。MinerU 原始 HTML 只剩一个 `0.15 0.5` 的数据格。此前 ADS1115 原第 6 页同样把 150、200 合成一格，BSO301SP 原第 1 页则把两个温度及两组负电流合并为 `TA=25°CTA=70°C` 与 `-14.9-11.9`。同类型故障跨三份文档出现。
2. **同一份 PDF 的不同页可能输出不同表结构。** OPA188 第 5、6 页的原版面都把 0.8 V/µs、2 MHz 放在 TYP；第 5 页的典型列起始位置为 141，0.8 的起始位置为 142。MinerU 第 5 页把 `0.8` 吐成普通值格，第 6 页吐成 `colspan="3"`。项目随后分别写成 `pmax=0.8` 与 `ptyp=0.8`。不能仅按器件型号或整份文档设定统一列规则。
3. **图表候选的适配有缺口。** 本轮 TMP117 的原始结构含 2 个 `chart`，文档记录 `images=0`。早先 OPA211 也观测到 `chart`。项目只在 `btype == "image"` 时进入图像处理。本轮对视觉问答本身未做验证。 [图像适配代码](../ingestion/mineru_client.py#L1100)
4. **解析状态与实际解析器的记录不足。** MinerU 出错后主链有 PyMuPDF 回退，所以仅凭 `ready` 无法区分实际解析器。本轮三份原始 JSON 都已留存，确认本批走 MinerU。`MINERU_OUTPUT_FORMATS` 在设置中有声明，主 PDF 入口沿用 `parse_pdf()` 的默认产物参数。 [解析入口](../ingestion/document_processor.py#L917) · [调用点](../ingestion/document_processor.py#L982) · [前次格式核对](MINERU_ACCEPTANCE_20261126.md)

### B. 表格到参数记录

5. **合并格解码会填错 Min/Typ/Max。** TMP117 `d319889a_p2_pr44` 写成 `pmin=0.15, ptyp为空, pmax=0.5`。此前 ADS1115 电流题为 `min=150, typ为空, max=200`。OPA188 的 `5d087fb5_p2_pr41` 把 0.8 写在 `pmax`，低压页 `5d087fb5_p3_pr137` 又保存为 `ptyp=0.8`。值本身仍在，列语义已经变错。 [合并值格规则](../ingestion/table_struct.py#L1016)
6. **枚举和表头被当成参数数值。** 既有 ADS1115 数据率一行的合法档位被写成 `min=250, typ=475, max=860`，端到端答出了原文没有的典型 475 SPS。本轮 `8fda09af_p2_pr10` 从 `THERMAL METRIC | INA226 | UNIT` 生成了 `pmax=INA226`，被电源电流题选中。另一个实例 `d319889a_p3_pr89` 将 `TEMPERATURE REGISTER VALUE (0.0078125 °C RESOLUTION)` 表头文本写进数值字段。需要在记录提交前验证值类型和行属性。
7. **表级默认工况没有进入相应记录。** OPA188 原第 5 页表头的高压工作与 `TA=25°C` 是表级默认工况。正确失调电压记录 `5d087fb5_p2_pr14` 已保留 `ptyp=±6, pmax=±25`，`conditions_raw` 为空。它进入 RRF 第 13、Rerank 第 17。实际输出选择的是带温区的 `μV/°C` 行并报冲突。需要将表级默认工况与行内覆盖关系一起保留，并区分电压和漂移量纲。 [参数记录构造](../ingestion/table_struct.py#L907) · [MinerU 表适配](../ingestion/document_processor.py#L1038)

### C. 切块、向量与混合检索

8. **本批关键事实仍存在于 chunk，候选限额先阻断了部分证据。** `d319889a_p3_tbl94` 中有 `-25 ... F380`，包含同一值的 `d319889a_p3_c16` 在向量分支排第 42，5 条 RRF 候选都不含它。分辨率证据在向量分支第 27，同样未进 5 条候选。此前 ADS1115 连续转换电流的首页 chunk 排第 10，已在 Rerank 之前被截断。还没有测试能将这些故障归因于切块尺寸本身。 [RRF 每文档限额](../retrieval/hybrid_retriever.py#L663)
9. **BM25 已落盘但聊天召回支路为空。** 本轮 14 次调用该支路均为 0 条；上一轮 11 次也为 0。上一轮实测同一个 `supply current` 查询，默认 BM25Store 返回 0 条，加载现有 `bm25_index.pkl` 后返回 5 条。聊天节点自行构造 `HybridRetriever`，默认 BM25Store 没有持久化路径。 [聊天检索构造](../agents/workflow_nodes.py#L213) · [BM25 默认构造](../retrieval/hybrid_retriever.py#L168) · [落盘对照](RAG_DIAGNOSIS_20261126.md#切块重排与上下文的额外对照)
10. **SQLite 表格支路随查询改写波动，正确命中也可能在融合时丢失。** 本轮 14 次只有 5 次返回表格。TMP117 关断题的唯一命中 `table_d7ec0e29` 同时包含精度表内容和真实的 `Shutdown current … TA = 25 °C | 0.15 0.5 | µA`，完整关断证据未进入 RRF 的 5 条候选。上一轮同一文档过滤下，`ADS1115 current supply` 返回 0 张，改成 `supply current` 命中 2 张，数据库已写入 5 张。当前实现把清洗后的词直接拼成 FTS5 查询。 [FTS 查询](../retrieval/table_store.py#L570)
11. **实测重排改变名次，本批主要故障仍可追溯到前后不同环节。** 本轮 OPA188 正确失调记录从第 13 降到第 17，仍在候选集；INA226 分流正确行重排前后都第 1。此前 ADS1115 电流记录从第 4 到第 5，旁路重排仍答错；BSO301SP 的 -4.5 V 电阻行从第 2 到第 1，旁路也答对。先用同一题保留这些排名作为回归。 [重排入口](../retrieval/hybrid_retriever.py#L285)

### D. Resolver、上下文与答案

12. **Resolver 提名会跨题型、测量对象或数据行类型。** `ina-shunt-lsb` 的正确分流行 `8fda09af_p2_tbl70` 在向量、RRF、Rerank 都第 1。Resolver 独立提名了 `Bus voltage | 1.25 | mV`，旁路 Resolver 后，同一问句及排序下答成 `2.5 µV`，且上下文含分流行。`tmp-resolution` 和 `tmp-hex` 都被提名为温度精度。`ina-current` 更提名了热阻标题 `max=INA226`。此前 ADS1115 的 4 个通道题被提名为 ADS1113，单独绕过后答对。 [语义提名与路由](../agents/workflow_nodes.py#L339)
13. **语义提名的来源过滤有静态缺口。** 提名调用向量检索时传 `source=param_record`，向量库过滤白名单只有 `document_id`、`document_ids`、`page`，会忽略 `source`。本批已证实提名选错对象，尚需单独开关该过滤验证它对各案例的贡献。 [向量过滤白名单](../retrieval/vector_store.py#L125)
14. **最终提示词存在缺证据和有证据仍拒答两种故障。** TMP117 关断题的最终上下文没有 `0.15 µA`，却带了正常工作值 3.5 µA。TMP117 十六进制题旁路 Resolver 后仍因缺 `F380` 而拒答。INA226 电流题旁路 Resolver 后，实际 1172 字符上下文已有 330 µA，模型仍说未找到。历史 ADS1115 电流对照也曾把同一句 150 µA 交给生产格式和简短直问，前者原始响应拒答，后者正确。先验收事实是否到达上下文，再比较提示词。 [上下文组装](../agents/workflow_nodes.py#L946) · [历史提示词对照](RAG_DIAGNOSIS_20261126.md#切块重排与上下文的额外对照)
15. **`success=true` 和有引用都不能用作答案正确性指标。** 14 次响应均 `success=true`；温度分辨率题仍引用了温度精度行，INA226 范围控制题正确拒答却带无关的 INA226 引用。当前引用结构只有文件、页、片段和章节，可定位职责不同。派生样本需要保存来源页映射，数值题引用还应绑定参数行和列。 [回答与引用模型](../api/routers/chat.py#L38)

## 4. 部署与覆盖边界

| 环节 | 状态与证据 |
|---|---|
| MinerU 版本兼容 | **已复现并绕过。** 4.0.12 搭配 DocVortex 0.6.26 曾实测因 `private_ranges` 类型失败，固定 DocVortex 0.5.16 后通过。客户端走旧 Jobs 契约，升级 MinerU 4.1 需迁移接口。 [部署证据](AUTODL_ENVIRONMENT.md#mineru-实测) · [官方迁移说明](https://github.com/opendatalab/MinerU/blob/master/docs/zh/reference/migration_4_1.md) |
| 运行状态和自动清理 | **代码检查。** `/health` 是应用探活，不能证实模型或实答。启动包含 SQLite 修复以及失败时的删除路径，也触发孤儿清理。此前实例重启后已验证同一 OPA211 ID 保留 77 个 chunk。本轮未对损坏库或迁移后的路径做破坏性演练。 [生命周期](../api/main.py#L49) · [探活](../api/main.py#L231) |
| 图像接口 | **代码检查。** `/image/by-path` 的路径解析接受客户端输入，并按存在性选择路径，未限制在图像资产目录。本轮在回环地址用 SSH 访问，未做对外访问测试。对外提供前应使用固定资源 ID 并限制路径范围。 [路径解析](../api/routers/circuit.py#L788) |
| Golden 评测 | **未运行全量。** 本地 `eval/golden.jsonl` 仍缺失。当前脚本的 `--doc-id` 会把所有题固定到同一文档，评测时需核对真实问题对应的各个文档 ID。本报告的 11 题来自独立核对过的原文，与 Golden 百题分开。 [评测入口](../eval/run_eval.py#L244) |
| 未覆盖的功能 | 这次只测三份样本选页、API 文本链路和 resolver 对照。全量 PDF、更多并发与重启持久性、浏览器点击流程、图片读数、引脚、ERC/对比专项均没有在本轮测试。 |

## 5. 修复顺序与回归门槛

1. **先封住假数值。** 考查 ADS1115 `DR` 八个档位，典型值要说明未指定，最大可选速率仍为 860 SPS。`INA226` 这类型号不能通过数值字段校验。对列位无充分证据的合并格标成待核验，恢复 TMP117、ADS1115 电流和 OPA188 压摆率的列位时，用原表逐行验证。
2. **再管住 Resolver 的提名范围。** 用 INA226 的 Shunt/Bus 两题查对象区分，预期分别为 2.5 µV、1.25 mV。TMP117 的寄存器值、分辨率和 ADS1115 的通道数不能被同名或相关参数抢答。引用验证要匹配对象、工况和量纲。
3. **恢复实际召回。** 用同一个落盘 BM25 索引复验聊天检索能收到结果，并让 FTS 通过参数短词命中已知表。将 `d319889a_p3_tbl94`/`p3_c16` 的 `F380` 从存储追到 RRF、Rerank 和最终上下文。候选限额、文档 scope 与提示词单变量回归。
4. **修完一处复跑同一组问题。** 分开核对答案、工况和引用，保留 OPA211 `G=100` 为 80 MHz、`G=1` 为 45 MHz，以及 BSO301SP 12 mΩ／8 mΩ 的已通过基线。大批量评测放在证据可靠后。

## 6. 原始证据索引

服务器本轮路径为 `/root/autodl-tmp/veriquery-audit-20261126/`。三份原件、衍生样本及 SHA-256 分别见 `*-original.pdf`、`diag-*.pdf` 和 `manifest.json`。真实原文选页的版面文本在 `*-source-pages.json`。`mineru-*.json` 是实际解析输出，`*-chunks.json` 包含入库前的行、单位、数值和 chunk ID。`column-evidence.json` 保存原文列位置和原始 HTML 单元格。

`questions-gold.json` 保存预期问答，`cases.jsonl` 保存实际请求与响应，`trace.jsonl` 保存每个 `case` 的分阶段输入输出，`audit-summary.json` 汇总候选排名和引用的原 PDF 页。`model-check.log` 记录模型 API 实测。以 `case` 值定位，例如 `ina-shunt-lsb` 与 `ina-shunt-lsb-no-resolver` 是同一问题的可追溯对照。

更早的证据可以从 [ADS1115 / BSO301SP 实测](RAG_DIAGNOSIS_20261126.md)、[MinerU 原始 HTML 回放](MINERU_ACCEPTANCE_20261126.md) 和 [OPA211 部署验收](AUTODL_ENVIRONMENT.md) 跳转。本次临时启动的三个服务已停止，已有数据和日志继续保留。
