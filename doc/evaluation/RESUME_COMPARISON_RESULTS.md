# 路由与记忆检索对照结果

日期：2026-11-05。用于为 Agent 开发简历提供可以核验的组件对照数据。以下均为开发者编写、尚未独立审核的候选数据，固定配置单次运行；不代表独立验收、线上效果或最终回答正确率。

## 可直接使用的简历表述

> **评测与优化：** 在自建 130 轮候选任务数据上，对比关键词与 Qwen 意图路由，工具类别集合匹配率由 59.23% 提升至 96.15%，提高 36.92 个百分点；在 24 条合成记忆留出查询上，混合召回 Recall@5 达到 98.21%，与最佳单路召回持平，引入重排后 NDCG@5 由 0.9137 提升至 0.9306。

重排在提高排序指标的同时，将 Recall@5 从 98.21% 降至 92.86%。简历中的 98.21% 属于**未重排的混合召回阶段**，不能说成重排后的最终召回率。面试时应同时解释排序和召回的取舍，不能宣称所有指标均提升。

如果版面较紧，可保留路由的量化收益，把记忆对照结果放入“长期记忆”条目或面试材料。

## 预先固定的实验口径

推理前冻结生产源码、评测脚本、数据、配置和协议，冻结包摘要为 `0288331dddaf0a0de4fc3fb1b55aa900ae4b06536f6c8da2341b5e7624c09bd0`。不修改生产逻辑、提示词、标签、阈值或模型权重，不在得分后选择重复运行的最高值。

- 路由主结果：`acceptance_v2` 中的候选验收部分，108 组、130 轮，含 21 组多轮对话。32 组、41 轮校准部分单独报告。
- 记忆主结果：`memory_v1` 中的 24 条留出候选查询，来自 2 个合成用户/项目组。另 24 条开发查询单独报告。留出划分不等于独立人工审核或作者未见数据。
- 金标只在评分阶段使用；HTTP 原始输入中没有 `grade`、`relevance`、`requirements`、`route_required` 等评估字段。
- 只访问本机分类、Embedding 与 Reranker 服务，不调用外部主模型、不运行真实金融任务、不写入生产用户记忆。

模型为 `qwen3.5:9b`、`qwen3-embedding:8b` 和 CUDA 4-bit 的 `Qwen/Qwen3-Reranker-8B`。Embedding 摘要为 `64b933495768fbd3b87c20583d379728a07471e0c66733a9df87cd1901b3c44b`；分类模型摘要为 `56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90`。

## 路由对照

关键词方案使用现有生产 `social_rule` 加 `fallback_route`，未额外补规则。Qwen 方案使用生产 `IntentRouter`，保留其合同纠正与失败兜底。两组均仅使用当前输入和最近三条真实用户历史，不使用金标任务状态或编造的助手历史。

匹配条件为：预测集合包含全部必要类别，且不超出必要与可选类别的并集。**两组都按真实输出评分**：关键词函数虽然将来源标成 `fallback`，也不会因此自动判错；Qwen 出错后的兜底同样按实际集合计分。此指标衡量路由最终输出，不等同于纯 Qwen 模型分类准确率。

| 集合 | 组／轮 | 关键词匹配 | Qwen 路由匹配 | 差值 |
| --- | --- | --- | --- | --- |
| 候选验收，主结果 | 108／130 | 77/130，59.23% | 125/130，96.15% | +36.92 个百分点 |
| 校准，辅助结果 | 32／41 | 28/41，68.29% | 38/41，92.68% | +24.39 个百分点 |
| 合计，描述性汇总 | 140／171 | 105/171，61.40% | 163/171，95.32% | +33.92 个百分点 |

候选验收部分的支持类别 Macro-F1 为 42.17% → 90.77%。Qwen 路由有 2 轮输出合同纠正后仍失败并进入生产兜底，其中 1 轮兜底集合匹配；若要求必须由有效模型输出且无兜底，匹配为 124/130（95.38%）。该严格统计不能直接与被一律标成 fallback 的关键词方案比较。

171 轮计分输入共发生 177 次真实分类 HTTP 请求，输入 115,256、输出 5,838 token；包含纠正与失败尝试，不含预热。没有网络错误或缺失预测。数据未独立审核，也未测量主 Agent 的工具调用成功率、任务完成率或回答质量。

## 记忆四组对照

四组都调用生产 `MemoryStore.select`，只关闭对应的检索或重排通道：

1. BM25：关闭向量与重排。
2. 纯向量：关闭 BM25 与重排。
3. 混合：BM25＋向量＋RRF，关闭重排。
4. 混合＋重排：在相同混合候选上调用专用 Reranker。

保持生产的候选上限 20、召回上限 5、字符预算、余弦阈值 0.35 和重排阈值 0.5。所有种子记录按真实用户/项目归属装载，保留原 ID、时间、正文与来源；实际回放 `forget`，不依据金标预先删除干扰项。常驻偏好单独处理，不混进检索排序分数。

主结果的 24 条查询中，14 条存在相关非核心记忆，另外 10 条没有相关非核心记忆。Recall、MRR、NDCG 对空相关集合记 null，不补满分；下表的这三项按 14 条正样例取宏平均。无相关查询另报误召回。

| 方法 | Recall@5 | MRR@5 | NDCG@5 | 无相关非核心记忆查询中的误召回 |
| --- | --- | --- | --- | --- |
| BM25 | 92.86% | 0.8095 | 0.8112 | 8/10 |
| 纯向量 | 98.21% | 0.9286 | 0.9246 | 8/10 |
| 混合 | 98.21% | 0.9286 | 0.9137 | 10/10 |
| 混合＋重排 | 92.86% | 0.9643 | 0.9306 | 1/10 |

与预先约定的**最佳单路**基线纯向量相比，混合 Recall@5 的差值为 **0.00 个百分点**。混合相对 BM25 提高 5.36 个百分点，但不能把这个较弱基线的收益冒充“较最佳单路提升”。

重排相对未重排的混合方案：NDCG@5 增加 **0.0169**（若按百分制展示为 +1.69 个百分点），Recall@5 降低 **5.36 个百分点**。模型评分及相关性阈值同时改变排序和返回条数；该结果是当前完整重排阶段的效果，不能单独归因于排序算法。

| 辅助集合 | BM25 Recall@5 | 纯向量 Recall@5 | 混合 Recall@5 | 重排 Recall@5 | 混合 → 重排 NDCG@5 |
| --- | --- | --- | --- | --- | --- |
| 开发 24 条 | 89.29% | 96.43% | 96.43% | 92.86% | 0.9350 → 0.9482 |
| 合计 48 条 | 91.07% | 97.32% | 97.32% | 92.86% | 0.9244 → 0.9394 |

48 条查询、4 组方案全部完成，共 144 次计分 HTTP 请求，无 Embedding 异常、重排降级、缺失结果或跨用户/项目及删除记录回流。各方案复用同一条查询实际生成的向量，不将缓存次序造成的延迟差异作为性能收益。没有生成自然语言答案，因此不能将检索分数写成记忆正确采用率。

小记忆池和很少的合成用户组限制了结论，尤其 Recall@5 接近上限；没有据此宣称统计显著或真实用户收益。后续优化应在新的更大候选集上验证，而不是调试这批留出标签后继续声称未见测试。

## 复现与核验

脚本：[evaluate_resume_comparisons.py](../../scripts/evaluate_resume_comparisons.py)。运行顺序：

```bash
.venv/bin/python scripts/evaluate_resume_comparisons.py prepare --output .finagent/resume_comparisons/<new-run>
.venv/bin/python scripts/evaluate_resume_comparisons.py route --output .finagent/resume_comparisons/<new-run>
.venv/bin/python scripts/evaluate_resume_comparisons.py memory --output .finagent/resume_comparisons/<new-run>
.venv/bin/python scripts/evaluate_resume_comparisons.py report --output .finagent/resume_comparisons/<new-run>
.venv/bin/python scripts/evaluate_resume_comparisons.py verify --output .finagent/resume_comparisons/<new-run>
```

真实运行目录：[20261105_candidate_v2](../../.finagent/resume_comparisons/20261105_candidate_v2/)。主要证据：

- [冻结清单](../../.finagent/resume_comparisons/20261105_candidate_v2/manifest.json)与[事前协议](../../.finagent/resume_comparisons/20261105_candidate_v2/frozen/protocol.json)。
- [机器可读汇总](../../.finagent/resume_comparisons/20261105_candidate_v2/summary.json)。
- [逐轮路由预测](../../.finagent/resume_comparisons/20261105_candidate_v2/routing/predictions.jsonl)及同目录 `requests/` 中的逐次请求、响应。
- [逐查询四组召回](../../.finagent/resume_comparisons/20261105_candidate_v2/memory/predictions.jsonl)、[逐题评分](../../.finagent/resume_comparisons/20261105_candidate_v2/memory/per_case_scores.json)及同目录 `requests/`。

新增比较脚本测试和相关评分/路由回归共 **37 项通过**，覆盖关键词基线公平计分、失败分母、可选类别、真实归属与删除回放、冻结防篡改、HTTP 日志适配及凭证排除。另独立重算主记忆结果的 Recall@5、NDCG@5，与项目评分器一致；原始输入无金标字段，冻结文件摘要校验通过。这些程序检查不证明标签本身正确。

`20261105_candidate_v1` 保留为中止记录：HTTP 日志适配误拒绝 `transport=None`，只完成模型清单 GET，未产生任何计分预测；修正适配并新增测试后重新冻结为 v2。没有根据已产生的得分挑选重跑。
