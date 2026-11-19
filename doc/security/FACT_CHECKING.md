# 最终回答的证据审核

2026-11-18 更新。生产配置采用独立只读 Reviewer Agent，复用 AgentKernel，通过去重目录、精确字段视图与有界取证审核完整回答。当前 `backend="main_model"` 继承主模型的接口、模型与密钥；审查提示词和方法保持既有实现。新链路、预算及审计详见 [Reviewer Agent](../features/REVIEWER_AGENT.md)；下文固定全文证据包与 Ollama 的示例保留为显式 `backend="ollama"`、`agentic=false` 兼容模式说明。

## 输入与执行链路

工具获取并保存 → 会话登记结果 ID 与任务/轮次来源 → 后端筛选当前任务结果 → 读取完整规范结果与原始快照元数据 → 组装证据包 → 小模型审核 → 通过交付或反馈修改。

- HTTP 层保留原始字节及提供方、URL、获取时间，生成 `snapshot_id`。`EvidenceStore` 保存完整 `Observation`，生成 `result_id`。
- 工具完成钩子在 `ctx.result_tools`、`ctx.evidence_ids` 登记引用，并在 `state.evidence_registry` 关联任务 ID、任务版本、轮次 ID、用户来源 ID 和工具名。这些信息随会话检查点恢复。
- 证据包按活动任务、公司、允许报告期、截止时间、有效状态和快照权限筛选。同一任务前轮的有效结果可复用；不同任务的已标记结果默认排除，显式回读的证据可在范围检查后复用。旧会话没有关联元数据时按已有对象、期间、时间规则兼容筛选，不宣称已有严格的历史任务归属。
- 财务结果保留匹配报告期的完整报告和事实字段、币种、口径、披露/可得时间及 warnings。新闻保留实际取得的摘要和来源信息，不将摘要或聚合财报称为完整原文。报告期、行情时间、获取时间、披露时间分别传递，缺失值不补造。
- 审核取证使用存储中的规范结果，不使用主模型生成的证据摘要。Agent 模式先读取代码提取的精确字段，可通过专用只读工具加载其余字段；固定请求兼容模式一次发送全文规范结果。原始快照保留元数据，完整响应仍可通过主流程 `read_evidence` 分页回读。
- Agent 模式区分来源登记、已加载范围与审核完成，正文体积过大不直接等于来源缺失；真正无法读取、未授权、快照未登记或预算不足时明确失败。固定请求模式仍对全文包实施原体积上限。两种模式均不能静默漏掉内容后判定通过。

实现：`src/finagent/answer_review.py`；工具登记：`src/finagent/harness/extensions.py`；主会话门禁：`SessionHost._review_answer()`。

## 审核职责与协议

输入包含最新用户请求、当前任务及继承限制、完整草稿、规范证据、来源 ID、源时间与获取时间、状态和警告、已执行计算的公式和操作数。

审核器检查数值、公司和报告期，也检查否定、限定词、比较关系、归因、证据不足和偏离用户要求。它只依据提供材料，不使用自身市场知识补证。输出 JSON 示例：

审核输出要求简短：JSON目标不超过3000字符，不复述完整草稿或行情表。后端从完整证据的标量字段提取短原文引用候选，并从草稿提取原文片段；请求的动态 JSON Schema 约束证据 ID 与其合法引用配对，以及问题引用必须选取草稿片段（遗漏要求可用 null）。候选仅辅助逐字引用，不代替完整证据或语义审核；后端仍校验实际返回引用的来源与原文一致性。

精度与表达规则：正确的四舍五入、等值的小数位差异、已有证据支持但没有逐句展示内部 result_id，均不单独构成证据不足；免责声明不作为需要行情来源证明的外部事实。以上由提示词约束语义判断，不自动删除审核问题或强制判定通过。

```json
{
  "complete": true,
  "verdict": "revise",
  "evidence_required": true,
  "summary": "存在过度归因",
  "issues": [{
    "answer_quote": "现金流增长完全来自回款改善",
    "kind": "overstatement",
    "reason": "证据同时提到采购付款减少",
    "suggestion": "删除唯一原因表述，并列说明相关原因",
    "citations": [{"evidence_id": "result_实际结果ID", "quote": "证据中的逐字内容"}]
  }],
  "citations": []
}
```

- `pass`：已完成审核且未发现需修改问题。`evidence_required=true` 表示草稿包含需要外部证据支持的事实，此时至少给出一处支持引用；纯概念、问候或澄清可为false，不强制引用旧数据。该分类本身也依赖审核模型，不能证明所有语句都被正确覆盖。
- `revise`：给出草稿原句、问题类型、原因和修改建议。`unsupported` 与 `contradicted` 分别表达证据不足和明确矛盾；建议可以补查、删除或表达不确定性。
- `unable_to_verify`：无法完成审核，停止交付该草稿。

后端验证完整生成、JSON Schema、判定与 issues 一致、草稿引用确实存在、来源 ID 属于本次证据包、证据引文可在对应材料中逐字找到。`complete` 指审核工作完整完成，不是草稿内容正确；遗漏用户要求时允许 `answer_quote=null, kind=scope`，不能编造不存在的草稿引文。规则检查引用和执行合同，不代替模型作完整语义判断。算术、公式、操作数快照及口径校验继续由原计算工具执行。

无外部证据时，审核器可允许概念解释、澄清或明确说明无法确认；不能将这些视为外部事实验证。模型仍可能漏检、误判或因证据选择不足而误拦，引用合法也不代表推理正确。

## 修改、预算与审计

`revise` 的问题作为不授予新权限的宿主反馈进入主执行循环。主模型修改或补查后重新组装证据并审核。默认最多修正两次；持续冲突返回 `FACT_CHECK_FAILED`。Stop Hook、预算耗尽和澄清路径均不能放行被拒绝草稿。

审核使用 Ollama `/api/chat`、JSON Schema、`think=false`，不另加逐条提取请求。无效协议允许默认一次修正，`protocol_retries` 可设为0到2；两次请求共享审核总超时，每次都计入模型调用和用量。审核调用与主模型修改共享当前请求的模型次数、活动时间和取消状态；用量按 `answer_review` 记录。超时、截断、错误模型、无效 JSON、虚构引用、不完整结果都不视为通过，不静默回退旧规则。

审核前的工具调用附带草稿文字不向用户输出；工具状态仍正常展示。主会话最终回答及澄清回答经过审核，内部子任务和 schema 产物不重复调用该审核器，研究产物仍有独立合同机制。

证据包、草稿和审核报告存为会话目录下 `reviews/<id>.json`；`state.fact_check`、`state.fact_validation` 与 `answer_fact_checked` 事件保留审核状态及引用。`review_passed` 仅指模型认为提供的证据支持回答，不是全文真实性、独立数据验证或生产质量验收。

## 配置与验证

```toml
[fact_check]
enabled = true
max_corrections = 2

[fact_check.review]
enabled = true
agentic = true
endpoint = "http://127.0.0.1:11435"
model = "qwen3.5:9b"
timeout_seconds = 90
context_tokens = 131072
output_tokens = 4096
max_input_chars = 12000
max_evidence_chars = 100000
max_evidence_results = 32
protocol_retries = 1
max_model_calls = 6
max_tool_calls = 14
initial_evidence_chars = 16000
max_read_chars = 24000
max_read_per_call = 6000
```

环境覆盖：`FINAGENT_ANSWER_REVIEW_ENABLED`、`FINAGENT_ANSWER_REVIEW_ENDPOINT`、`FINAGENT_ANSWER_REVIEW_MODEL`。后端须重启以加载新配置。默认 `Settings()` 保持审核关闭以兼容离线调用方；项目 TOML 已开启。旧 `[fact_check.extraction]` 与规则模块作为显式兼容路径保留，只有关闭 review 时才使用；review 开启时不运行旧的逐字段最终门禁或提取请求。

合成协议、证据筛选和修改循环测试：`tests/test_answer_review.py`。真实小模型合成冒烟：`scripts/check_answer_review_live.py`。程序测试验证控制流程，不证明模型审核准确率；上线效果需中文金融人工标注集比较错误放行、误拦、延迟与成本。
