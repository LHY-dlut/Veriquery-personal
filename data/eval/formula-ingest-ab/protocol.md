# 公式入库前后的六题回放

复用上一轮 12 题的原始文件、原始 PDF 选页和判分口径，按原顺序选取含有 `calculation` 字段的 n02、n03、n04、n06、n07、n10。每臂运行三轮，每轮按相同顺序发起六个独立聊天会话，取每个重复编号的首次成功业务请求。评分时区分结论、计算过程、所引依据和证据缺口。[原题](../unseen-model-ab/questions.jsonl) · [评分口径](../unseen-model-ab/rubric.md)

两臂的 PDF 字节哈希、提问、模型、生成与检索侧被核验的源码文件、BGE 模型以及业务开关由 `comparison-config.json` 固定。每臂分别用自己的文档 ID、块数和向量、BM25、结构表快照。索引差异是本实验要测量的变量。

从当臂的应用源码根目录执行，例如在已设置业务环境的终端中运行下列命令。将 `--arm` 替换为 `after` 即可回放新索引。API 与驱动使用同一个 `XINFERENCE_LLM_MODEL=qwen3.5` 和同一个本机转发器地址。`<run>/questions.jsonl` 为原始 12 题。`<run>/before-documents.json` 与 `after-documents.json` 分别保存两款器件的真实入库记录。

```bash
python <run>/formula_replay.py --root <run> --arm before --port <api-port> \
  --driver <original-eval-dir>/replay.py --documents <run>/before-documents.json
```

只导出索引时附加 `--inventory-only`。每臂正常回放也会先导出快照。同一臂续跑时恢复其记录目录与转发日志，失败编号会新增一次 attempt，完成编号跳过。每份成功响应保留最终答复、引用、trace、实际入模文本和下游模型回执。

| 文件 | 记录 |
| --- | --- |
| `arithmetic-questions.jsonl` | 从冻结输入逐行抽取的六道原题，保留原始字节 |
| `<arm>/indexed.json`、`<arm>/indexed-tables.json`、`<arm>/index-manifest.json` | 实际 Chroma 文本、元数据、向量哈希、SQLite 表以及当前 BM25 指纹 |
| `<arm>-config.json` | 当前臂服务端模型登记、开关、相关代码与索引指纹 |
| `<arm>.jsonl` | 以题号和重复编号组织的 18 条 API 请求与最终响应 |
| `model-calls-<arm>.jsonl` | [原转发器](../unseen-model-ab/model_tap.py)逐次记录的真实 UID、提示词哈希、原始模型内容、结束原因和 token 用量 |
| `<arm>/*.attempt-*.json` | 单次实际 HTTP 尝试及失败原因 |
