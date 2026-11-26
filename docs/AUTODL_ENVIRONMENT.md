# AutoDL 实例检查

检查和联调日期 2026-11-25 至 26（北京时间）。当前部署源码位于实例的 `/root/autodl-tmp/Veriquery-personal`。

| 检查项 | 实测结果 |
|---|---|
| 系统 | Ubuntu 22.04.5 LTS，Linux x86-64 |
| 可用 CPU、内存配额 | 12 核，90 GiB（cgroup） |
| GPU | `nvidia-smi` 在实例内报告一张 RTX 4090，49,140 MiB 总显存，检查时 48,510 MiB 空闲 |
| CUDA | 驱动 595.71.05，计算能力 8.9；基础环境 PyTorch 2.8.0+cu128 成功创建 GPU 张量 |
| 磁盘 | 系统盘约 30 GB；持久数据盘 `/root/autodl-tmp` 约 100 GB，准备完成后已用约 50 MB |
| Python / SQLite | 已有 Miniconda，Python 3.12.3，SQLite 3.45.3。应用 `.venv` 已建立 |
| 网络 | PyPI 和 ModelScope 可达；Hugging Face 直连检查超时。ModelScope 上三个首批文本模型的元数据接口返回 HTTP 200 |
| 项目传输 | 源码包 SHA-256 校验通过后在数据盘解包。服务器 `.env` 来自脱敏模板，文件权限为 `600` |
| Xinference 验收脚本 | 服务器上的离线配置预览通过，模型请求尚未执行 |

项目和缓存使用数据盘，以便开关机后复用。首批 BGE-M3、BGE Reranker 和 Qwen3.5-0.8B 的权重已从 ModelScope 下载并通过缓存完整性检查。Xinference 的权重加载与 API 结果单独验收。

## MinerU 实测

MinerU 4.0.12 的 OpenAPI 包含当前客户端所需的 `/v1/uploads` 与 `/v1/parse/jobs`。初次使用 DocVortex 0.6.26 时，`standard` 和 `basic` 都在 PDF 原生文字提取阶段触发 `private_ranges` 类型错误。将 MinerU 环境的 DocVortex 固定为 `0.5.16` 并重启后，用 `standard` 解析 OPA211 的两页样本通过。样本来自原 PDF 第 1 和第 9 页，返回 2 页和 2 张表，服务报告耗时 107,346 毫秒。

实例上的原始文档和样本保存在 `/root/autodl-tmp/veriquery-corpus`。解析结果保存在 `/root/autodl-tmp/veriquery-transfer/opa211-mineru-probe.json`。

## 文本问答验收

- Xinference 4.2.0 在回环端口 9997 运行。BGE-M3 返回两组有效的 1024 维向量，Reranker 将相关文本排在第一，Qwen3.5-0.8B 返回验收题的预期数值。模型通过本地缓存路径加载。
- FastAPI 和 Streamlit 在实例回环端口 8000 和 8501 运行。本机 SSH 隧道将它们转发到 18000 和 18501。前后端的健康接口均已实测。完整数据链路由 HTTP API 触发。
- 通过应用上传 `opa211-pages-1-9.pdf`，文档 ID 为 `abc12ad1`。入库得到 2 页、2 张表、77 个文本 chunk。重启 FastAPI 后用原 ID 查到同一状态和记录数。
- 对 `What is the typical gain-bandwidth product of OPA211 at G=100?` 的第一次请求回显两组工况。修复将英文 `at` 从工况键中排除，并把增益符号 `G` 加入合法工况键。T01–T25 本机和实例均为 25/25。复跑同一个文档 ID 和问题得到 **80 MHz**，引用样本第 2 页表 T1 第 2 行，对应原 PDF 第 9 页。`G=1` 返回 **45 MHz**，省略 `G` 则列出两组工况。
- 首轮另外两条请求留作后续回归。器件清单题触发了参数提名，IQ 记录存在数值列错位。视觉、全量 Golden 集、52 页原 PDF 全量入库和浏览器点击流程没有纳入本轮验收。

实例上的 `/root/autodl-tmp/veriquery-transfer` 保存 `opa211-app-smoke.json`、`opa211-gbw-retest.json`、`opa211-parser-records.json` 和各服务日志。`start-xinference.sh`、`start-mineru.sh`、`load-cached-text.sh` 用于从同一数据盘恢复模型服务。服务器 `.env` 已存入 `USE_MINERU_PARSE=true`、`MINERU_TIER=standard`。当前应用依赖经 `app-constraints.txt` 安装，以避免旧 Chroma 的 NumPy 约束与新 OpenCV 冲突。重复开机时可复用既有环境和权重缓存。

验收后的显存读数为 7,558 MiB，数据盘占用约 14 GB。首轮使用的服务进程按批准的测试窗口限时。到期后实例的电源状态由 AutoDL 控制台管理。
