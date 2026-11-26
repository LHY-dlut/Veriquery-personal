# ADS1115 的 MinerU 档位对照与故障归因

**原版会把采样率数字切开，MinerU 的前三个档位会合并电流列，旧个人版又把无列位依据的数字填进了 MIN/TYP/MAX。** 同一页的 Advanced 实测找回了工作和关断电流的列位，功耗行仍跨列。

## 输入与对照方法

- 厂商原件为 TI ADS111x `SBAS444E`（2024 年 12 月修订），物理页 1、5、6 从原 PDF 截成固定的三页样本。样本页 2 对应原第 5 页，样本页 3 对应原第 6 页。
- 三页样本 SHA-256 为 `ca8b3fa49f87d33e08489a6d3e0fba5d71ccda17d36bec0cbd50187376cb6d19`。四个档位提交相同文件。使用 AutoDL 现有 MinerU 4.0.12、DocVortex 0.5.16 和缓存权重。独立请求 MinerU 服务，保留 `structured_content`、`middle_json`、Markdown 及任务状态。
- 原版对照使用 GitHub commit `663c3304` 的表格提取器，服务端运行前核对源码 Git blob 为 `743d6a53e84eca313e614cdfc0170fde95afea17`。同样输入三页样本。 [厂商 PDF](https://www.ti.com/lit/ds/symlink/ads1115.pdf) · [选页清单与哈希](../data/eval/mineru-tier-ads1115/manifest.json) · [原版表格提取器](https://github.com/FinalSunFlower/Veriquery/blob/663c3304ee50b7b3b0b131b1b78d6ad9b4ec4f5e/extraction/table_extractor.py#L806)

## 三页实验

| 档位 | 实际结果 | 原页 1 / 5 / 6 的表格块数 | 第 5 页 DR | 第 6 页电流列 | 客户端耗时 |
| --- | --- | --- | --- | --- | --- |
| Flash | `flash / txt`，成功 | 1 / 1 / 2 | 8 档完整 | `MIN TYP MAX` 和 `150 200` 各被压为单格 | 5.002 秒 |
| Basic | `basic / txt`，成功 | 2 / 1 / 2 | 8 档完整 | 同上 | 13.698 秒 |
| Standard | `standard / txt`，成功 | 2 / 1 / 2 | 8 档完整 | 同上 | 8.677 秒 |
| Advanced | 225 秒客户端限时内未返回产物 | — | 无可比产物 | 无可比产物 | 超时 |

Flash 首页给出封装表。Basic 与 Standard 还把 `Device Information` 识别成独立的器件配置表。这是本次前三档的实际差别。原第 5、6 页一共 3 个表格块在三档之间的原始内容逐字节相同。耗时是单次观测，受模型和缓存启动影响。 [Flash 表块](evidence/ads1115/flash.json) · [Basic 表块](evidence/ads1115/basic.json) · [Standard 表块](evidence/ads1115/standard.json) · [Advanced 超时记录](../data/eval/mineru-tier-ads1115/advanced.json)

### 只请求原第 6 页

复试继续使用相同的三页文件，四档统一传 `page_range="3"`。四档均成功并回报 `txt`，各提取两张表。前三档的两张表也与首轮逐字节相同。 [四档单页原始行与任务编号](../data/eval/mineru-tier-ads1115/single-page-comparison.json)

| 档位 | 工作电流在 25 °C 的原始输出 | 关断电流在 25 °C 的原始输出 | 客户端耗时 |
| --- | --- | --- | --- |
| Flash | 单格 `150 200` | 单格 `0.5 2` | 2.678 秒 |
| Basic | 单格 `150 200` | 单格 `0.5 2` | 8.563 秒 |
| Standard | 单格 `150 200` | 单格 `0.5 2` | 4.984 秒 |
| Advanced | MIN 空，TYP 150，MAX 200 µA | MIN 空，TYP 0.5，MAX 2 µA | 402.011 秒 |

Advanced 本次比 Standard 慢约 81 倍。服务日志显示高级路径走了 llama.cpp，两步提取约 392 秒。运行中快照看到 GPU 占用 4 MiB、利用率 0%，MinerU 进程 CPU 约 1149%。这是当前运行环境的单次观测。 [Advanced 表块](evidence/ads1115/advanced.json) · [服务日志](../data/eval/mineru-tier-ads1115/p6/mineru.log)

## PDF 与原始解析证据

### 原第 5 页的 DR 是档位枚举

PDF 的 DR 行给出 `8, 16, 32, 64, 128, 250, 475, 860 SPS`。这行的八个数值都是可选档位，`475` 没有独立的 TYP 标记。Flash、Basic、Standard 的原始表格 HTML 都包含以下行。 [厂商原第 5 页](https://www.ti.com/lit/ds/symlink/ads1115.pdf#page=5) · [三档逐行重放](../data/eval/mineru-tier-ads1115/adapter-replay.json)

```html
<tr><td>DR</td><td>Data rate</td><td></td><td>8, 16, 32, 64, 128, 250, 475, 860</td><td>SPS</td></tr>
```

历史个人版用这段原始 HTML 离线重放，产出 `min=250,`、`typ=475,`、`max=860`，末尾逗号来自原字符串。此前实际聊天亦答出「典型 475 SPS」。这个典型值由入库规则引入。当前工作树已移除那条规则。 [历史问答与原文证据](RAG_DIAGNOSIS_20261126.md#最早发生错误的阶段)

### 原第 6 页的 150 µA 和 200 µA 各有列位

`Supply current → Operating → TA=25°C` 的 MIN 留空，TYP 是 150 µA，MAX 是 200 µA。PDF 字符坐标显示 TYP 表头 x=438.03，150 的 x=440.23；MAX 表头 x=485.50，200 的 x=489.92。两数位于同一行。 [厂商原第 6 页](https://www.ti.com/lit/ds/symlink/ads1115.pdf#page=6) · [文字与坐标](../data/eval/mineru-tier-ads1115/original-pdf-evidence.json)

三档的原始 `structured_content` 都给出以下表头和数据行。在同一产物的 `middle_json` 里，对应 `table_body.content` 和这张表的 HTML 完全相同。Standard 表块 bbox 为 `[0.091, 0.138, 0.907, 0.299]`，可据此定位原 PDF 表区。HTML 中两个数字已合入同一单元格。 [Standard 表块](evidence/ads1115/standard.json)

```html
<tr><td colspan="2">PARAMETER</td><td colspan="2">TEST CONDITIONS</td><td>MIN TYP MAX</td><td>UNIT</td></tr>
<tr><td rowspan="2">Operating</td><td>T<sub>A</sub> = 25°C</td><td>150 200</td></tr>
```

历史个人版将这段重放为 `min=150, typ=null, max=200`。新版适配器保留行 `IVDD | Supply current | Operating | TA = 25°C | 150 200 | µA`。由于服务在该数值格未提供 `colspan`，当前针对显式跨度的标记也不能识别这个隐式合并。工作树没有新增可靠的列位证据。 [重放结果](../data/eval/mineru-tier-ads1115/adapter-replay.json) · [当前表格适配代码](../ingestion/mineru_client.py#L315)

单页 Advanced 的电流表保留了独立列位。工作电流的 HTML 如下，关断电流行对应的数值单元格为 `<td></td><td>0.5</td><td>2</td>`。用当前适配及切块代码重放后，两个 chunk 都带有 `MIN | TYP | MAX` 表头及正确的空格位。 [Advanced 表块](evidence/ads1115/advanced.json) · [新版切块重放](../data/eval/mineru-tier-ads1115/advanced-adapter-replay.json)

```html
<tr><td colspan="2">PARAMETER</td><td colspan="2">TEST CONDITIONS</td><td>MIN</td><td>TYP</td><td>MAX</td><td>UNIT</td></tr>
<tr><td rowspan="2">Operating</td><td> $T_A=25°C$ </td><td></td><td>150</td><td>200</td></tr>
```

同一张表的功耗行还有列位问题。原 PDF 的 `0.9/0.5/0.3 mW` 横坐标均约 x=442.47，位于 TYP 区。Advanced 对工作在 5.0 V 的 `0.9` 返回 `<td colspan="3">0.9</td>`，其余两行也覆盖三列。电流两行验收通过，功耗三行仍需对照 PDF。 [原第 6 页完整电源表截图](../data/eval/mineru-tier-ads1115/original-p6-power-table.png)

## 归因和影响

| 观察 | 归因 | 同输入证据 |
| --- | --- | --- |
| 原版结构化表的数字碎裂 | 原始 GitHub 表格链路已有 | 原第 5 页的 DR 行中，`128/475/860` 横跨字符串 `64, 1`、`28, 250, 47`、`5, 8`、`60`。原第 6 页没有独立结构化表返回。这次只运行原版表格提取器。原版 PyMuPDF 正文还可能带回电流值，未在本轮运行原版 QA。 [原版结果](../data/eval/mineru-tier-ads1115/original-github-extractor.json) |
| 第 6 页电流列折叠 | MinerU 4.0.12 在该样本的输出限制，通过个人版接入后传给业务 | 原 PDF 数值分列，MinerU 原始 HTML 已将多个值写入同一单元格，发生在 Veriquery 适配之前。 |
| 475 被写成 TYP，150 被写成 MIN | 历史个人版的参数结构化和选值改造 | 同一份原始 HTML 重放旧转换模块复现，移除 Resolver 前的实际对话也有记录。 |
| BM25 未读到落盘索引，部分查询进 Rerank 前只剩五条 | 原版聊天构造和 RRF 设计已有 | GitHub 原版聊天自行建检索器，应用容器里的 BM25 才传递落盘路径，RRF 默认每文档五条。当前工作树已改成共享索引，先重排再限制。此次解析实验不涉及召回。 [原版聊天构造](https://github.com/FinalSunFlower/Veriquery/blob/663c3304ee50b7b3b0b131b1b78d6ad9b4ec4f5e/agents/workflow_nodes.py#L188) · [原版 BM25 配置](https://github.com/FinalSunFlower/Veriquery/blob/663c3304ee50b7b3b0b131b1b78d6ad9b4ec4f5e/api/dependencies.py#L137) · [原版 RRF 限额](https://github.com/FinalSunFlower/Veriquery/blob/663c3304ee50b7b3b0b131b1b78d6ad9b4ec4f5e/retrieval/hybrid_retriever.py#L565) |
| 厂商下载与 datasheet-rag 清单的 SHA-256 不同 | 评测语料匹配问题 | 本次对照的原版和各档使用字节相同的 ADS1115 三页样本。上游清单差异只影响能否复用其严格基准。 [评测语料记录](DATASHEET_RAG_EVALUATION.md#题库和语料) |

这份样本支持继续用 Standard 作通用基线。规格表出现疑似合并列时，优先用 PDF 字符坐标核对，也可将高价值的少量页面交给 Advanced 并复查列位。把 **工作电流 150 µA TYP、200 µA MAX，关断电流 0.5 µA TYP、2 µA MAX，八档枚举无 TYP，以及功耗三行的列位** 纳入验收。本次实测范围是这份 PDF 的选页及入库前切块重放。整体解析准确率、原版和新版的完整问答准确率仍待用相同语料实测。
