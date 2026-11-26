# MinerU 的输入输出契约与首个修复验收

以下记录对应移除 Resolver 之前的代码。当前流程保存原表列位与合并标记，问答依据正文和原表生成。运行方式见 [AutoDL 部署手册](AUTODL_SETUP.md)。

本次在 AutoDL 上只读核对部署配置和保存的 OPA211、ADS1115、BSO301SP 产物。把两份诊断样本的真实 HTML 重放给当前 `table_struct.py`，复现了数值列错误。本次未重跑 MinerU，也未变更应用源码。

## 实际档位

| 4.0.12 档位 | 官方用途 |
|---|---|
| `flash` | 快速抽取原生文字，扫描件用轻量 OCR |
| `basic` | 基础 OCR、公式和表格，小模型路径 |
| `standard` | 复杂版面，小模型结合 VLM |
| `advanced` | 和 Standard 共用模型与运行环境，投入更多推理计算 |

AutoDL `.env` 的有效值为 `USE_MINERU_PARSE=true`、`MINERU_TIER=standard`，服务启动脚本也显式使用 `--tier standard`。若按表顺序计数，这是第三档。本地 `.env.example` 仍为 `USE_MINERU_PARSE=false`、`MINERU_TIER=flash`。Standard 可作为 datasheet 首轮基线，已解析出真实表格，但两份 PDF 的选页中仍出现列合并和条件粘连。目前没有同页、同版本、同依赖的四档对照。早先 Basic 和 Standard 在旧 DocVortex 依赖组合上都报过同一种类型错误，该记录不能用于比较档位质量。 [MinerU 4.0.12 档位](https://github.com/opendatalab/MinerU/blob/mineru-4.0.12-released/docs/zh/usage/tiers.md) · [AutoDL 实测记录](AUTODL_ENVIRONMENT.md)

## 返回格式与当前接线

实际版本使用旧 `/v1/uploads`、`/v1/parse/jobs` 工作流，任务响应的 `files[].output_files` 给出各产物的 `file_id`，客户端再读取 `/v1/files/{file_id}/content`。MinerU 4.1 已改为 `/v1/tasks` 与结果下载契约，升版本需先适配 API。 [客户端](../ingestion/mineru_client.py#L694) · [MinerU 4.1 迁移说明](https://github.com/opendatalab/MinerU/blob/master/docs/zh/reference/migration_4_1.md)

| 产物 | 实际观察 | Veriquery 用法 |
|---|---|---|
| `markdown` | 字符串，文档正文、标题和表格等 | 可预览和保留，主业务解析基于结构化块 |
| `structured_content` | 根有 `pages`、`metadata`、`extensions`、`is_full_document`。`pages[].page_idx` 从 0 起，`blocks[]` 带 `type`、`bbox`、`content`。表格实测还有 `captions`、`footnotes`、`image_source`，`content` 是原始 HTML | 用于构建页面、正文、表格和图像候选。表格 HTML 经项目规则生成 `ParamRecord` |
| `middle_json` | OPA211 实测根有 `metadata`、`extensions`、`pages`、`is_full_document`、`schema`、`schema_version` | 已下载并保留，可用于排查版面；此版本主问答链没有用它补表格的列位 |

实测 OPA211 首页表格块的 `bbox` 为 `[0.518, 0.586, 0.912, 0.688]`，对应表格块位置。这个外框没有给出内部各数值的列位。当前 `MINERU_OUTPUT_FORMATS` 虽然在配置中声明，主 PDF 处理入口调用 `parse_pdf()` 时未传 `output_formats`，实际沿用客户端默认的三种产物。 [MinerU 4.0.12 产物说明](https://github.com/opendatalab/MinerU/blob/mineru-4.0.12-released/docs/zh/reference/output_files.md) · [产物读取](../ingestion/mineru_client.py#L943) · [当前调用入口](../ingestion/document_processor.py#L982)

## 核对到的问题

| 问题 | 证据与影响 |
|---|---|
| 多值格的列位缺失 | ADS1115 原第 6 页的 150 µA 对应 TYP、200 µA 对应 MAX。MinerU HTML 把表头合成 `MIN TYP MAX`，把值合成 `150 200`；重放现有代码得到 `min=150, typ=null, max=200`。原始端已经缺列边界，后置规则又作了确定性填列 |
| 枚举值变成典型值 | ADS1115 原第 5 页的 DR 是 8 个可选档位，真实 HTML 重放得到 `min=250, typ=475, max=860`。实际问典型值时答 475 SPS。下面的断言已实跑并失败 |
| 两个工况被合成一格 | BSO301SP 原第 1 页的两行被 MinerU 合成 `TA=25°CTA=70°C`、`-14.9-11.9`。结构记录缺有效工况和数值。同一输入的原第 2 页独立电阻行仍支持答对 12 mΩ 和 8 mΩ |
| 图表候选与引用粒度有限 | OPA211 原始 JSON 的首页含 `chart`，适配代码只有 `type == "image"` 会进入图像候选，此前独立探针得到 0 张图片。表格块本身保留 `bbox`，最终 `CitationInfo` 以文件、页、文本片段和章节标记定位，颗粒度到不了单元格 |
| 入库状态不能代表解析验收 | 应用捕获 MinerU 分支异常后会回退本地解析。追踪时同时记录解析器、实际档位、原始产物和来源页；只看 `ready` 可能把两条链混在一起 |

这些产物可以支持普通文本题和结构可靠的参数行。规格问答的值、工况、列位、单位和来源需要逐项通过验收，之后才交给 Resolver 决定数值。图表读数另做验收。项目其他实测问题，包括 Resolver 误抢答、BM25 空索引、表格检索词过严和上下文丢证据，保留在 [逐层诊断](RAG_DIAGNOSIS_20261126.md)。 [图像适配](../ingestion/mineru_client.py#L1095) · [引用字段](../api/routers/chat.py#L38) · [解析回退](../ingestion/document_processor.py#L929)

## 与开源方案的对照

| 参考对象 | 可核实的设计差别 | 本项目可直接采用的做法 |
|---|---|---|
| Docling | `DoclingDocument` 保留文档层级、表格对象、布局框和来源。表格结构模型会产出行列起止与跨度等单元格属性，chunker 允许附加层级上下文 | 从 PDF 到入库持续保留来源页、表格框、原始单元格和标题轨迹。数值候选携带列位是否可判定的状态。识别准确率用实际单元格单独验收 |
| RAGFlow | Dataset 有独立 Retrieval Testing，能查看命中 chunk、内容、来源和排名，配置混合召回与 Rerank | 用同一页 PDF 构造可重复的检索回归。我们已经做出了临时逐层追踪，固定下来检查向量／BM25／表格三路是否真返回、证据排位和最终提示词。当前 11 次请求有两路为空，适合先在现有链路定位 |

本项目的条件约束和列语义 Resolver 保留为领域逻辑。上表只比较可核实的实现设计，未在这 6 页上实跑 Docling 或 RAGFlow。 [Docling 文档模型](https://docling-project.github.io/docling/concepts/docling_document/) · [Docling 分块](https://docling-project.github.io/docling/concepts/chunking/) · [Docling 表格结构实现](https://github.com/docling-project/docling/blob/main/docling/models/stages/table_structure/table_structure_model_v2.py) · [RAGFlow 检索测试](https://ragflow.io/docs/v1.0.0/retrieval_testing)

## 第一项操作：ADS1115 数据率枚举

从当前版本把第一个错误锁成失败回归。以下命令使用服务器上保存的真实 MinerU JSON 和当前项目解析代码，MinerU 和 Xinference 关闭时也能执行。

```bash
cd /root/autodl-tmp/Veriquery-personal
.venv/bin/python -B - <<'PY'
from pathlib import Path
import json, runpy

root = Path('/root/autodl-tmp/veriquery-diagnosis-20261126')
raw = json.loads(next(root.glob('mineru-*diag-ads1115.json')).read_text())['structured_content']
module = runpy.run_path('ingestion/table_struct.py')
rows = [r for b in raw['pages'][1]['blocks'] if b.get('type') == 'table'
        for table in module['parse_tables'](b['content'])
        for r in module['extract_param_records'](table, 2, 'replay')]
rate = next(r for r in rows if r.symbol == 'DR' and '860' in r.raw_row)
print(rate.min, rate.typ, rate.max, rate.raw_row)
assert rate.typ is None, '可选档位不可填入 TYP'
PY
```

实跑输出中的 `min=250, typ=475, max=860` 触发断言失败。首刀只处理枚举语义。验收时保留八档，查询典型值时说明表中未指定，查询最大可选速率时仍可正确给出 860 SPS。已有 BSO301SP 两个导通电阻用例保持 12 mΩ、8 mΩ。下一刀再处理 `150 200` 的列位，先确认原 PDF 几何或表格结构能证明列归属，再写入 TYP/MAX。
