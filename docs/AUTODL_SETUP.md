# Veriquery Personal AutoDL 部署手册

本机部署工作目录为 `D:\Project\veriquery\Veriquery-personal`。在 AutoDL 上从上传后的同名项目目录启动。应用和服务配置使用 [`.env.example`](../.env.example)，实际凭据填入服务器端 `.env`。首轮以文本和表格为主，PDF 与 Golden 题库单独准备。

当前文本与表格的配对运行、失败用例和重启检查见 [落地记录与评测证据](VERIQUERY_DELIVERY_AND_EVALUATION.md)。`MINERU_VERIFY_TABLE_COLUMNS=true` 会在新入库时对合并规格列补充 PDF 坐标证据。

当前应用默认请求 MinerU Advanced，并优先使用匹配明确工况和表列的短证据行。服务能力、档位验收及同索引回放见 [规格表证据的召回、排序与选取](RANKED_TABLE_EVIDENCE.md)。

| 本地验证 | 结果 |
|---|---|
| 当前 68 个 Python 源文件语法编译 | `PASS` |
| 旧版参数选值测试 | 首次部署时 25/25。当前聊天链路使用下文的证据回归脚本 |
| `.env.example` 解析，LLM、MinerU 及 Judge 的认证隔离 | `PASS`，使用合成凭据做进程内检查 |
| Xinference 配置预览和 API 检查 | `PASS`，API 使用模拟响应 |
| 本机导入 FastAPI 应用 | `BLOCKED`，系统 Python 缺 `langgraph`，同环境也缺 Chroma |
| AutoDL、MinerU、Xinference、真实 PDF 入库和问答、Golden 评测 | `NOT RUN` |

## 1. 先建独立环境

同一台 Linux 实例运行应用、Xinference 和 MinerU，三者使用各自的 Python 环境。先查 `nvidia-smi`、`python --version`、`df -h`，记录 CUDA 驱动、系统镜像、磁盘挂载及剩余空间。把项目放在确认过的持久数据盘下。应用的 `data/` 初期保持为该项目下的新目录，模型缓存和评测 PDF 可放在同一挂载的独立目录。旧索引与新模型的向量空间需隔离。

首轮使用一个 Uvicorn 进程。Chroma、BM25 和 SQLite 会随入库写盘，启动还有孤儿数据清理和数据库兼容检查。在空白 `data/` 完成第一次试验，再备份并迁移明确要复用的文件。使用 `DATA_DIR_STR` 改数据根时，还要一并核对 `CHROMA_PERSIST_DIR`、当前有相对默认路径的 SQLite 和上传清理位置。 [启动行为](../api/main.py#L69) · [SQLite 默认路径](../retrieval/table_store.py#L125) · [上传清理](../core/cleanup_manager.py#L729)

按量实例先为单 PDF 做小规模试跑，记录实际 GPU 峰值、解析耗时和磁盘用量，再确定 GPU 规格和同驻模型数。费用按控制台实际单价与开机时长估算，付费扩容盘另计。绑定 `127.0.0.1`，从个人电脑经 SSH 隧道访问。 [AutoDL 计费](https://api.autodl.com/docs/price/) · [AutoDL 端口](https://www.autodl.com/docs/port)

在服务器上进入上传后的 `Veriquery-personal` 目录。应用先以已安装的 Python 3.11 建候选环境，Xinference 和 MinerU 的解释器按其实际版本匹配。

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
python -c 'import sqlite3, chromadb, langgraph; print(sqlite3.sqlite_version)'
cp -n .env.example .env
export NO_PROXY=localhost,127.0.0.1
python scripts/test_rag_pipeline.py
```

依赖尚未在目标镜像上解析锁定。`numpy<2` 对应旧 Chroma 的 `np.float_` 兼容问题。Qwen3.5 的实际推理版本在 Xinference 环境单独选型。应用里的 CLIP/本地视觉回退仍可能使用本地 Transformers。应用用下文的模块入口启动。 [Chroma 0.4.24 源码](https://github.com/chroma-core/chroma/blob/0.4.24/chromadb/api/types.py) · [NumPy 2 迁移说明](https://numpy.org/doc/2.0/numpy_2_0_migration_guide.html#main-namespace) · [Qwen3.5 模型卡](https://huggingface.co/Qwen/Qwen3.5-0.8B)

## 2. 把模型服务和解析器各自跑通

| 服务 | 建议本机地址 | 先验证什么 |
|---|---|---|
| Xinference | `http://127.0.0.1:9997`，应用 base 加 `/v1` | `GET /v1/models` 取得真正的模型 UID。按 `bge-m3`、`bge-reranker-v2-m3`、Qwen3.5-0.8B 的顺序单独测试；实际 UID 分别写入 `EMBEDDING_MODEL`、`RERANK_MODEL`、`XINFERENCE_LLM_MODEL` |
| MinerU | `http://127.0.0.1:8001`，应用 base 用服务根 | 从服务的 OpenAPI 核对上传、任务和结果接口。用同一份公开 PDF 保存实际结构化结果，检查页码、表格块与坐标 |
| FastAPI | `http://127.0.0.1:8000` | 文档上传、状态、`POST /api/v1/chat/` 的业务返回和引用。`/health` 作为进程探活 |
| Streamlit | `http://127.0.0.1:8501` | 浏览器通过隧道访问，页面与 API 结果对应。进程 `API_URL=http://127.0.0.1:8000` |

### Xinference 模型、UID 与 API 验收

本项目使用社区版 Xinference 加公开模型权重。同机推理由租用实例计算，无需购买第三方模型 API 或 token 套餐。按量 AutoDL 实例按开机时长计费，下载权重也占用开机时间，付费扩容数据盘另计。各模型按其许可使用。 [Xinference 许可](https://github.com/xorbitsai/inference/blob/main/LICENSE) · [AutoDL 计费规则](https://api.autodl.com/docs/price/)

在与应用隔离的环境安装与实例 GPU、驱动和引擎匹配的 Xinference。确认实际持久化路径后启动，首轮通过 SSH 隧道访问管理界面。启用认证的版本在界面初始化本机管理员并创建一个供 Veriquery 调用的推理 key，填入服务器端 `.env` 的 `XINFERENCE_API_KEY`。这个 key 由自托管服务签发。 [本机启动](https://inference.readthedocs.io/en/stable/getting_started/using_xinference.html) · [认证配置](https://inference.readthedocs.io/en/stable/user_guide/auth_system.html)

```bash
export XINFERENCE_HOME=/path/on/verified-data-disk/xinference
xinference-local --host 127.0.0.1 --port 9997
```

| 启用阶段 | 权重与许可 | Xinference 注册名 / 类型 | 应用默认 UID 与 API |
|---|---|---|---|
| 文本 1 | [BAAI/bge-m3，MIT](https://huggingface.co/BAAI/bge-m3) | `bge-m3` / `embedding` | `bge-m3`，`/v1/embeddings`，1024 维 |
| 文本 2 | [BAAI/bge-reranker-v2-m3，Apache 2.0](https://huggingface.co/BAAI/bge-reranker-v2-m3) | `bge-reranker-v2-m3` / `rerank` | `bge-reranker-v2-m3`，`/v1/rerank` |
| 文本 3 | [Qwen/Qwen3.5-0.8B，Apache 2.0](https://huggingface.co/Qwen/Qwen3.5-0.8B) | `qwen3.5` / `LLM`，大小选 `0_8` | `qwen3.5`，`/v1/chat/completions` |
| 后续 Caption / Judge | [Qwen/Qwen3.5-2B，Apache 2.0](https://huggingface.co/Qwen/Qwen3.5-2B) | `qwen3.5` / `LLM`，大小选 `2` | `qwen3.5-2b`，`/v1/chat/completions` |
| 后续图文向量 | [Qwen/Qwen3-VL-Embedding-2B，Apache 2.0](https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B) | `Qwen3-VL-Embedding-2B` / `embedding` | `Qwen3-VL-Embedding-2B`，`/v1/embeddings`，2048 维 |

逐个启动前三项。Qwen 家族的两个大小共用注册名，必须分别指定 UID。以下是首轮文本的 CLI 起始配置，前提是当前版本已支持对应模型组合、CLI 已完成登录，并已确认模型下载的盘空间和费用。示例 Qwen 使用 `Transformers + pytorch + none`，也可根据实际显存选目标版本明确支持的组合。用 `GET /v1/models` 核对实际 UID 后填写应用 `.env`。 [Xinference 启动说明](https://inference.readthedocs.io/en/stable/user_guide/launch.html) · [Qwen3.5 规格](https://inference.readthedocs.io/en/stable/models/builtin/llm/qwen3.5.html)

```bash
xinference launch -e http://127.0.0.1:9997 --model-name bge-m3 --model-type embedding --model-uid bge-m3
xinference launch -e http://127.0.0.1:9997 --model-name bge-reranker-v2-m3 --model-type rerank --model-uid bge-reranker-v2-m3
xinference launch -e http://127.0.0.1:9997 --model-name qwen3.5 --model-uid qwen3.5 \
  --model-engine Transformers --size-in-billions 0_8 --model-format pytorch --quantization none
```

在应用环境从项目根目录执行。默认只检查本地配置并显示计划。加 `--check` 才向运行中的服务发真实请求。

```bash
python scripts/check_xinference.py
python scripts/check_xinference.py --stage embedding --check
python scripts/check_xinference.py --check
python scripts/check_xinference.py --stage caption --check --image /path/to/figure.png
python scripts/check_xinference.py --stage visual-embedding --check
python scripts/check_xinference.py --stage visual-embedding --check \
  --image /path/to/figure.png --image /path/to/another-figure.png
```

单卡分时可依次用 `--stage embedding`、`--stage rerank` 和 `--stage llm` 单独验收，三个模型均常驻时用默认的 `--check` 一次验完。文本阶段核对 UID、两条 BGE-M3 向量的有效性、Rerank 的相对排序和 LLM 的实际正文。Caption 获得非空响应以后还要人工确认描述与图相符。图文向量阶段分别验证文本与图片，只有用项目实际图片请求格式得到有区分度的向量，才开启 `VL_IMAGE_ENCODING_AVAILABLE`。未上机之前，配置预览和模拟响应不能作为真实模型验收。

**先核对 MinerU 接口和实际档位。** `ingestion/mineru_client.py` 使用 `/v1/uploads`、`/v1/parse/jobs` 和 `/v1/files/{file_id}/content`，当前验收使用 4.0.12。服务用 `mineru-kit api-server --tier standard` 启动，在 `/v1/tiers` 确认 Advanced 可用，应用用 `MINERU_TIER=advanced` 提交任务。默认解析超时为 1800 秒。档位不符或解析失败会报告错误，只有打开 `MINERU_ALLOW_LOCAL_FALLBACK=true` 才回退 PyMuPDF。上传就绪后核对 `parser_tier=advanced`。使用 4.1 的 Tasks API 需要另行适配客户端。 [MinerU 4.1 官方迁移说明](https://github.com/opendatalab/MinerU/blob/master/docs/zh/reference/migration_4_1.md)

兼容 MinerU 服务起来后，可先独立验证解析，不经过 FastAPI 上传。脚本会读取指定 PDF，`--json` 保存此次解析对照报告。

```bash
mkdir -p reports
python scripts/eval_mineru_vs_fitz.py /path/to/OPA211.pdf --tier advanced --json reports/opa211-parse.json
```

## 3. 一份 PDF 走完整应用链路

用两个终端从项目目录启动 API 和 UI。以下命令使用应用 `.venv`。

```bash
.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
API_URL=http://127.0.0.1:8000 .venv/bin/python -m streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501
```

1. 用公开 OPA211 PDF 记录来源、修订版、页数与 `sha256sum`。先通过 `POST /api/v1/documents/upload` 上传，保存真实 `document_id`，再请求 `GET /api/v1/documents/{document_id}`。核对解析器、原表条件、分块和三个索引中的记录。`ready` 说明核心路径已写状态，仍要检查实际数据。 [TI OPA211 PDF](https://www.ti.com/lit/ds/symlink/opa211.pdf)
2. 先提一个易人工核对的正文问题，例如 OPAx211 系列包含哪些型号。再选一个页、表、条件和列都能人工验证的参数题。保留 `/api/v1/chat/` 的 `query`、`document_ids`、`success`、`response` 和 `citations`。用同一个 ID 从 UI 重走，记录正确证据在哪个候选位置。
3. 保存一份逐阶段记录，串起 PDF 页 → MinerU 表格块和正文 → `PageContent` → 表行和正文 chunk → 各路召回 → RRF 候选 → Rerank → 编号上下文 → 答案和引用。运行 `python scripts/trace_query.py "问题" --docs DOCUMENT_ID` 查看各阶段，最终以 HTTP 结果复核。

升级时可继续查询已入库文档。聊天会跳过旧的合成参数记录。需要使用新版合并格和表头信息的 PDF，保留旧数据作为对照，将 PDF 导入独立验收环境后重测。优先核对 INA226 的 shunt/bus LSB、ADS1115 的数据率档位、TMP117 的 `F380`，并复测 OPA211 和 BSO301SP 的工况题。
4. 用 T24 和同一份 OPA211 样本回归 `at G=100` 工况提取与 80 MHz 的引用。Chat 自建 BM25 实例默认没有载入文档入库写入的 BM25，也应通过可复现召回来检查。先保存实际结果，再逐个定位处理。 [AutoDL 实测记录](AUTODL_ENVIRONMENT.md#文本问答验收)

文本链路有证据后，再部署 Qwen3.5-2B 做图片 Caption，确认图 ID、页码和原图。如果开启 `USE_XINFERENCE_VL_EMBEDDING`，先验单独的文字编码服务和实际模型 UID，在 `VL_IMAGE_ENCODING_AVAILABLE=false` 下用 Caption 测回读，再单独验证图片向量能力。引脚、器件比较和 ERC 使用各自的 API 和定向样例。

## 4. 按 eval_usage_v2.md 复现评测

`eval/run_eval.py` 已合入，默认读取 `eval/golden.jsonl` 并调用 `POST /api/v1/chat/`。说明记录了历史 100 题、约 38 个器件。本地交付里没有题库与对应 PDF，所以当前可跑构造单测，Golden 评测还缺输入。`fetch_pdfs.py` 的硬编码列表实测 37 项，覆盖范围以找回的 Golden 的 `gold_parts` 为准。历史用 BSO301SP 文档问 OPA 五题的结果用于冒烟，准确率需要题目、器件和 PDF 范围对应。

先备齐一个器件的匹配 PDF 和 3–5 道人工核对题，保存一份从原题库筛出的 `reports/golden-smoke.jsonl`，保留原题字段和参考答案。确保入库完成再启动 Judge，Judge UID 从 `/v1/models` 核对。`EVAL_JUDGE_MODEL` 默认 `qwen3.5-2b`，配置可指向独立的 OpenAI 兼容端点。用以下方式启动，输出包含 Markdown 和同名前缀 JSON。

```bash
.venv/bin/python eval/run_eval.py \
  --golden reports/golden-smoke.jsonl \
  --pdf-dir /path/to/validated-datasheets \
  --out reports/eval-smoke.md
```

`--devices` 按交集筛题，`--limit` 只截断选出的行，`--doc-id` 会把所有题指向同一份文档。脚本可能对缺件的多器件题使用剩余文档，并且打印文档状态后仍向后执行。先人工确认每题全部 `gold_parts` 已映射且就绪，单文档与多文档分开验收。真实报告分开记录服务错误、数据缺失、错误拒答和无依据作答。小模型 Judge 的输出再抽样核对原文。

