# Veriquery datasheet 评测

题目 14，k=8，Judge 关闭

## 运行结果

- answer_for_review 10
- retrieval_evidence_incomplete 2
- unexpected_answer 2

## 指标与分母

| 类别 | hit_at_k | mrr_at_k | retrieval_recall_at_k | context_recall | must_include | refusal_correct | correctness | faithfulness |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| overall | 0.833 (n=12) | 0.667 (n=12) | 0.833 (n=12) | 0.833 (n=12) | 0.636 (n=11) | 0.000 (n=2) | N/A | N/A |
| conditions | 1.000 (n=5) | 0.900 (n=5) | 1.000 (n=5) | 1.000 (n=5) | 0.750 (n=4) | N/A | N/A | N/A |
| cross-section | 1.000 (n=1) | 0.167 (n=1) | 1.000 (n=1) | 1.000 (n=1) | 0.000 (n=1) | N/A | N/A | N/A |
| spec-lookup | 0.667 (n=6) | 0.556 (n=6) | 0.667 (n=6) | 0.667 (n=6) | 0.667 (n=6) | N/A | N/A | N/A |
| unanswerable | N/A | N/A | N/A | N/A | N/A | 0.000 (n=2) | N/A | N/A |

Judge 失败 0。
逐题参考答案、原文、检索、上下文、输出及 Judge 原始记录见同名 JSONL。
