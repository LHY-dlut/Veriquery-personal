# Datasheet 问答逐层诊断

2026-11-26 在已部署的 Veriquery Personal 上实测。下载两份完整 PDF，分别截取 3 页为诊断样本。用实际上传、MinerU `standard`、检索、Resolver 和 Xinference 问答路径记录 11 次请求，其中 7 次为原路径问题，4 次为单阶段对照。旁路只在临时诊断进程中生效。

| 文档 | 原 PDF / 所用页 | 文档 ID | 入库结果 |
|---|---|---|---|
| ADS1115 | 57 页，选原第 1、5、6 页 | `2744a43b` | 3 页，5 张表，153 个 chunk |
| BSO301SP | 8 页，选原第 1、2、3 页 | `7dd47cbd` | 3 页，9 张表，96 个 chunk |

源文件取自 [TI ADS1115 Rev. E](https://www.ti.com/lit/ds/symlink/ads1115.pdf) 和 [Infineon BSO301SP Datasheet 的 DigiKey 存档](https://media.digikey.com/pdf/Data%20Sheets/Infineon%20PDFs/BSO301SP_170702.pdf)。ADS1115 样本第 2、3 页分别对应原第 5、6 页。BSO301SP 的诊断以实际下载的 8 页版本为准。

## 最早发生错误的阶段

| 问题 | 对照证据 | 定位 |
|---|---|---|
| ADS1115 连续转换的典型电源电流 | 原第 6 页的 150 µA 在 TYP 列，200 µA 在 MAX 列。PDF 布局文本的 TYP/MAX 起始位置是 120/135，两个数值位置是 121/136。MinerU 原始 HTML 把表头吐成单格 `MIN TYP MAX`，数据吐成单格 `150 200`。入库 `2744a43b_p3_pr111` 把它写成 `min=150, max=200`，没有 `typ`。正常和关闭 Rerank 均回答典型值未给出 | 原始 HTML 丢了数值列信息，记录构造再用双值规则误填 MIN |
| ADS1115 可编程数据率的典型值 | 原第 5 页给的是可选档位 `8,16,32,64,128,250,475,860 SPS`。结构化记录 `2744a43b_p2_pr34` 变成 `min=250, typ=475, max=860`，实测回答典型值 `475 SPS`。最大值题碰巧得到正确的 860 SPS | 参数记录把档位枚举误当成最小／典型／最大列 |
| ADS1115 单端输入通道数 | 向量、RRF、Rerank 的第一名都保留了 `four single-ended`。正常请求却提名 ADS1113 的参数记录，报告无数值。仅绕过 Resolver 后，同一问题、同一排序，958 字符的上下文包含正确证据，模型回答 **4 个**并引用第 1 页 | 语义提名越过问题类型和器件身份，提前截断生成路径 |
| BSO301SP 在 TA=25°C 下的连续漏极电流 | 原第 1 页分别列 `TA=25°C → -14.9 A`、`TA=70°C → -11.9 A`。MinerU 原始格已经把条件连成 `TA=25°CTA=70°C`，把值连成 `-14.9-11.9`。入库记录带着同一合并串，Resolver 回答条件冲突 | 最早的配对信息损失发生在 MinerU 原始表格 |
| BSO301SP 的导通电阻最大值 | 原第 2 页两行分别为 `VGS=-4.5 V, ID=-12.1 A → 12 mΩ` 和 `VGS=-10 V, ID=-14.9 A → 8 mΩ`。两问均答对并引用同页不同表行。关闭 Rerank 后，第一题仍为 12 mΩ | 这两行的解析、分块和参数选值是通过样例 |

相关实现可从 [合并数值列的规则](https://github.com/LHY-dlut/Veriquery-personal/blob/b0d9f2551b45a6ce4321614c81b17b95d3ee273a/ingestion/table_struct.py#L1016)、[语义提名与门控](../agents/workflow_nodes.py#L399) 追查。

## 切块、重排与上下文的额外对照

- **ADS1115 电流的首页依据保留在 chunk。** 源 PDF、MinerU 原始文本、适配后页面文本与 `2744a43b_p1_c0` 都保留了 150 µA 和连续转换模式。该 chunk 在向量分支排第 10，未进入该单文档查询的 5 条 RRF 候选。表格记录 `2744a43b_p3_pr111` 从向量第 4、RRF 第 4 移到 Rerank 第 5。绕过 Resolver 的正常生成上下文没有 `150`，与实际只拼接前 3 个清理后片段一致。
- **同一模型的提示词对照。** 把现有首页完整 chunk 作为唯一上下文，生产生成逻辑仍拒答。缩为原 chunk 中包含 150 µA 的一句后，插桩确认该句已到达模型请求，生产提示词下原始响应仍说未找到。相同模型、同一原句加简短直接提问，原始响应给出 **150 µA**。单次对照支持继续检查生产提示词与模型遵循，且排除了这次是输出清洗把 150 删掉。
- **重排影响具体名次。** ADS1115 电流的表格记录从 4 降到 5，关闭重排后仍然回报典型值缺失。BSO301SP 的 -4.5 V 行从 2 升到 1，开启和关闭都答对。本批错误样例应先处理入参和路由，保留这些名次作为后续重排回归。
- **独立召回支路有问题。** 11 次实际查询的 BM25 和表格检索返回数都为 0。用同一个短查询 `supply current`，新构造的 BM25Store 返回 0 条，读入现有落盘索引返回 5 条。SQLite 中 ADS1115 有 5 张表，实际改写 `ADS1115 current supply` 在相同文档过滤下匹配 0 张，`supply current` 匹配 2 张。

比对入口为 [融合限额](../retrieval/hybrid_retriever.py#L663)、[生成上下文长度](../agents/workflow_nodes.py#L970)、[BM25 默认构造](../retrieval/hybrid_retriever.py#L168) 和 [表格检索的词拼接](../retrieval/table_store.py#L570)。

## 修复优先顺序与验收

1. 先处理表格证据。保留源行、多工况和列位。遇合并数据格且列位无法可靠还原时显式标注不确定。把数据率档位保存为枚举，回归 150 µA 的列归属、475 SPS 的伪典型值和 BSO 的两个温度工况。
2. 再修语义提名门控。器件属性题走普通问答，参数提名还要核对器件身份与来源。回归 ADS1115 单端通道数。
3. 恢复 BM25 与表格召回。复验现有落盘索引能进入聊天检索，结构化分支允许去掉非必要的器件名。跟踪正确 chunk 从向量前 60 到 RRF 候选、Rerank 和生成上下文的每个名次。随后做提示词对照，选定有效的电流题模板。

原始文件、SHA-256 清单和复算证据在服务器 `/root/autodl-tmp/veriquery-diagnosis-20261126/`。`manifest.json` 记录页映射，`mineru-*.json` 是原始解析，`*-chunks.json` 是分块，`cases.jsonl` 是 HTTP 请求和响应，`trace.jsonl` 是分阶段运行记录。`column-evidence.json`、`rank-evidence.json`、`retrieval-store-evidence.json` 与 `ads-prompt-ablation.json` 保存本文关键对照。全量原 PDF 已保存在同一目录，本轮解析覆盖清单中的选页。
