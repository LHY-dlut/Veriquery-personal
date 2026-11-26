# Veriquery 器件规格书评测

实际运行结果、逐题复核及多模态开源方案取舍见 [datasheet-rag 原题评测](DATASHEET_GOLDEN_RESULTS.md)。

评测要回答两个问题。正确证据能否到达生成模型，以及模型能否按原表的型号、数值、单位和工况回答。使用标准聊天接口和同一次调用的证据记录。

## 题库和语料

题库固定在 `palginpav/datasheet-rag` 的 `885cbb4`。实际题库有 100 题，其中 91 道可回答、9 道拒答题。题型分布为规格查询 40、MCU 16、工况 14、比较 10、跨节 6、拒答 9、引脚 5。38 个型号在上游清单中对应 37 个独立的 PDF 哈希。导入保留题号、题型、题干、参考答案、必含项、器件与备注，移除上游切块 ID。拒答题也按原题约定限定文档。 [固定题库](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/data/golden/golden.jsonl) · [厂商 PDF 清单](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/data/manifest.json)

运行 `python eval/prepare_golden.py` 将题库及校验信息保存到 `data/eval/`。加入 `--download-pdfs` 下载厂商 PDF。可用 `--parts OPA211,OPA188,TMP117,INA226,ADS1115 --keep-unmatched` 准备首批 PDF。哈希一致的文件进入 `pdfs/`，其他候选另存 `unmatched/`。题库、厂商 PDF 和带原文的评测产物保存在本机忽略目录。

首批官网下载结果为 **0/5 通过上游哈希校验**。5 份文件均可读取，哈希差异是否由修订或 PDF 封装变化导致尚未确定。以下是对候选文件核对的物理页，可用于单独的 Veriquery 试验。运行前按实际 PDF 哈希冻结语料，并在入库后补齐 `document_id` 与原文片段。 [预期哈希和 URL](../data/eval/source.json) · [实收哈希](../data/eval/downloads.json)

| 题号 | 厂商候选 PDF | 核对结果 |
| --- | --- | --- |
| g007、g056、g057 | TMP117 第 1 页 | 精度示例涵盖不同温区，分辨率 0.0078 °C，结果宽度 16 位 |
| g017 | TMP117 第 1、6 页 | 参考答案的 1.7–5.5 V 有工况限定。低至 1.7 V 适用于 −55 至 70 °C，覆盖至 150 °C 时最低 1.8 V。人工复核时保留这一差异 |
| g008、g010 | ADS1115 第 1、5 页 | 第 1 页支持 16 位。第 5 页的 DR 行列出 8 个档位 |
| g015 | OPA211 第 9 页 | 两行分别为 G=100 时 80 MHz、G=1 时 45 MHz，分别标注为两个证据事实 |
| g025、g026 | OPA211 | g025 的 Wi-Fi 发射功率按题库约定在 OPA211 范围内验证拒答。g026 的工作电源范围在第 28 页 |
| g031 | INA226 第 1、29 页 | 分流电压和总线供电电压均有说明 |
| g033 | OPA211 第 28 页及 OPA188 第 1 页 | 两个器件的工作电源上限均为 36 V；两个文档都要在范围内 |

## 运行方法

1. 在 SSH 隧道或本机可访问的验收实例上设置 `EVAL_TRACE_ENABLED=true`。评测脚本向 `POST /api/v1/chat/` 请求 trace。该实例应只允许内部访问，因为 trace 包含原文和模型上下文。
2. 哈希校验通过的 PDF 可使用 `--pdf-dir data/eval/pdfs` 入库。已有文档则使用 `--doc-map data/eval/doc_map.json`，内容示例为 `{"OPA211":"入库后的真实ID","OPA188":"入库后的真实ID"}`。人工核对 doc-map 对应的 PDF 哈希。缺任何必需文档的题会跳过。旧的单 `--doc-id` 用法只验 API。
3. 用 JSONL 标注证据。每行包含题号和 `evidence` 数组。每项写入 `document_id`、从 1 开始的 PDF 物理页 `page`、可在片段中匹配的 `quote`。同一事实有等价证据时共享 `fact` 标签。比较题的每个器件、工况题所需的各条数据分别标注。将 PDF 原文跟抽取后的行核对，后续指标对证据匹配失败的边角案例需要抽查。
4. 对首批题跑实际 HTTP 评测。确认能重现证据和输出后，再用相同索引与配置扩展到 100 题。

```bash
python eval/run_eval.py --doc-map data/eval/doc_map.json --evidence data/eval/evidence.jsonl \
  --devices OPA211,OPA188,TMP117,INA226,ADS1115 --out data/eval/pilot.md
```

## 指标和排查口径

- **检索与上下文。** Hit@8 衡量前 8 条是否命中任一标注证据，MRR@8 衡量首个命中排名，检索 Recall@8 衡量前 8 条覆盖的证据事实比例，Context Recall 衡量真正进入模型的事实比例。缺证据映射的题或没有生成上下文的专用引脚路由，相关指标记为 N/A。
- **回答。** 统计必含项覆盖和拒答命中。启用 `--judge` 后记录中文 Judge 的 1–5 分 Correctness 和 Faithfulness，分别依据参考答案和实际上下文。必含项只检查字符串，单位换算、工况和顺序需语义复核。参考项目公开报告尚无人工一致性数据。每类题抽样人工校准，优先复核错题、明显低分、歧义以及安全拒答边界。 [上游评分定义](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/docs/eval-scorecard.md) · [上游 Judge 对齐记录](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/docs/judge-agreement.md)
- **错题溯源。** 报告输出 Markdown 和逐题 JSONL，保留原文证据、排序片段、实际上下文、引用校验前的生成文本、最终回答、引用及 Judge 原始输出。命中证据却未进上下文先查上下文组装。最终检索没返回证据时，先核对 MinerU 原始结果与入库记录，再追各路候选和排序，避免将解析缺失误记为召回失败。服务错误与 Judge 错误各自记录。
- **基线和消融。** 上游实验在自己的语料上发现朴素 BM25+RRF 降低 Hit@8，dense+rerank 提升。Veriquery 应冻结 PDF 哈希、问题、证据、切块、模型和检索范围，对照自己的 dense+rerank 与现有混合方案，逐题比较证据命中、延迟和答题差异。跨系统分数仅作为背景。 [上游消融](https://github.com/palginpav/datasheet-rag/blob/885cbb4dacb633cc703cecad450c6be35a7b3668/docs/ablation.md)

## 当前验证状态

题库已导入，首批 5 份候选 PDF 已核对证据页。API 回归使用模拟模型验证真实工作流能返回实际上下文；离线评测通过多证据召回、上下文丢失、跳过、拒答和异常分离检查。本机 `127.0.0.1:8000` 当前拒绝连接，真实模型准确率尚未产出。上一轮移除 Resolver 的工作树为本次待评测版本。

## 可发给 Leader

> 我们已锁定 datasheet-rag 的 100 道题及 PDF 清单，评测通过 Veriquery 的正常聊天接口执行，并保存该次请求的检索、模型上下文和引用。会分别统计检索、生成和拒答，再用人工复核定位错题。首批 5 份官网下载 PDF 与上游哈希不同，已保留差异和证据页。我们会在相同 PDF、题目和模型上比较 Veriquery 的检索方案，并注明实际测试覆盖和缺失项。
