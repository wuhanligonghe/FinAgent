# FinSight 可独立验收测试集 v1（待审核候选版）

120组、146轮，包含96条单轮请求和24组多轮对话；按场景族分为22组校准、98组验收。原话与答案分开：inputs.jsonl仅含用户输入与场景元数据，gold.jsonl包含必要/可选工具类别、任务字段、必须/禁止工具、研究次数、标注理由和人工答案规则。每个工具类别都覆盖，包含纯解释、混合意图、否定/引用、跨轮指代、多对象歧义、更正、范围排除、预测期限、截止、资料缺失、未支持市场和注入文本。

这是为独立审核准备的完整候选包，标签由开发阶段编写，尚未具备“已经独立验收”的身份。已用于调试的指代/预测期限场景分入校准集；验收集也不声称对作者不可见或与所有历史开发语义不重叠。校准与验收按场景族隔离，不按公司隔离。结构校验会拒绝跨集合完全相同原话与跨集合场景族；不能自动证明所有语义近似样例不存在。

## 冻结与审核

```bash
.venv/bin/python scripts/evaluate_acceptance_suite.py validate
.venv/bin/python scripts/evaluate_acceptance_suite.py prepare \
  --output .finagent/acceptance_v1/frozen
.venv/bin/python scripts/evaluate_acceptance_suite.py verify \
  --bundle .finagent/acceptance_v1/frozen
```

prepare不调用模型。它冻结数据、合成金融响应配置、实际运行源码、模型配置指纹和文件摘要，并生成全部pending的label_review.json。逐条审核规则见ANNOTATION_GUIDE.md。不能从模型输入目录加载gold；评测运行器仅在评分时读取gold。

## 运行校准（允许未审核）

```bash
.venv/bin/python scripts/evaluate_acceptance_suite.py route \
  --bundle .finagent/acceptance_v1/frozen \
  --output .finagent/acceptance_v1/route_calibration --max-model-calls 80
.venv/bin/python scripts/evaluate_acceptance_suite.py chain \
  --bundle .finagent/acceptance_v1/frozen \
  --case a001 --output .finagent/acceptance_v1/chain_probe --max-model-calls 12
```

route调用真实本地Qwen及社交规则，chain调用真实Qwen和配置中的主模型；每个运行可用case选择小批样例。两种模型共用总调用预算；失败调用也占预算，规则不占。main记录tokens，成功的Qwen返回token使用量；超时/失败可能没有token。chain可能消耗较多主模型API额度；正式98组全链路建议根据校准记录设置预算，不为了预算把失败或未运行样例移出分母。

金融HTTP完全走MockTransport、固定领域时钟与合成原始财报。支持合成报价、日线、证券搜索和年度合并报表，新闻为空，其他未提供的来源返回503。原文通过实际KnowledgeBase入库，每个case独立数据目录。网页工具白名单同样生效，文件/Shell/MCP/记忆/自动化等请求分类可以正确，而在网页能力范围内应明确不支持；这部分必须人工核对。研究授权走真实主会话边界，执行为stub，禁止花费真实研究预算。只有模型服务会访问外部网络。

## 正式独立验收

独立审核者须先完成冻结包label_review.json，再运行：

```bash
.venv/bin/python scripts/evaluate_acceptance_suite.py route --split acceptance \
  --bundle .finagent/acceptance_v1/frozen --review /path/to/approved_label_review.json \
  --output .finagent/acceptance_v1/route_acceptance --max-model-calls 180
.venv/bin/python scripts/evaluate_acceptance_suite.py chain --split acceptance \
  --bundle .finagent/acceptance_v1/frozen --review /path/to/approved_label_review.json \
  --output .finagent/acceptance_v1/chain_acceptance --max-model-calls 1600
```

正式验收前未审核会被拒绝；仍想进行开发诊断可显式加candidate，但该运行不可事后改成独立验收。正式结论只能通过adjudicate产生。分类结果可直接按已审核标签裁决；全链路需独立审核者填完运行生成的answer_review.json：

```bash
.venv/bin/python scripts/evaluate_acceptance_suite.py adjudicate \
  --bundle .finagent/acceptance_v1/frozen --output .finagent/acceptance_v1/route_acceptance
.venv/bin/python scripts/evaluate_acceptance_suite.py adjudicate \
  --bundle .finagent/acceptance_v1/frozen --output .finagent/acceptance_v1/chain_acceptance \
  --review /path/to/completed_answer_review.json
```

## 指标与证据

输出predictions.jsonl、report.json、answer_review.json、requests/及隔离会话transcript和source_index。分类统计工具集合匹配率（满足必要类别且不越出允许类别）、逐类Precision/Recall/F1、支持类别Macro-F1、降级/缺失率；全选不能获得满分。全链路检查任务字段、实际工具返回、禁止工具和研究次数，人工再核对数值证据与自然语言。初始分类与主模型补选后的工具集合分别保存。

所有计划轮次均计入分母，缺失和失败保留。report始终不自行宣布独立验收通过；adjudicate验证完整集合、推理前审核证据、预测摘要及人工结论，重新计算分类指标并生成独立decision.json。冻结清单摘要需由外部流程可信保管，本工具不认证审核者身份。

该测试集不评估真实股价预测收益、长期记忆跨会话产品效果、网页像素/交互、真实网络源稳定性、报告专业质量或并发负载；这些仍需各自的真实数据与验证。既有单元测试继续验证错误协议、超时、权限、重启恢复等实现边界，不能替代本集的真实模型成绩。
