# 会话任务接续与 TODO

2026-11-17：已移除独立 Task Frame、更新/读取工具及金融工具前置门禁。本文保留原路径，供既有链接继续访问。

## 当前实现

- 主模型根据最新用户原话、会话摘要和 TODO 理解需求；简单查询直接调用领域工具。
- 多步骤工作通过 `todo_write` 保存完整步骤清单。每项仅有 `content` 与 `status`（pending、in_progress、completed），最多20项，同时最多一项执行中。
- TODO 写入会话状态，随 Redis 检查点保存，并通过 `runtime_context.session.todos` 提供给模型。
- 已有 TODO 且距离上次更新或提醒达到3次模型尝试时，宿主提醒按实际进展更新。空列表不强制创建计划。
- 历史压缩保留用户需求、已确认参数、进展和待办，原文归档可回读。TODO和摘要均不作为金融证据。
- 实体解析、真实用户来源校验、历史截止时间、证据快照验证和受限财务计算仍保留。已有明确执行口径时，实际事实仍需匹配币种、合并范围和累计/单季口径。
- 独立研究仍检查用户明确启动请求，并冻结用户原话、约束、执行范围及 TODO 副本；后续聊天更新不修改该副本。

## 多轮变更的边界

用户说“改成2023年，其他不变”后，由主模型根据原话和摘要继承公司、指标和排除项，更新需要重做的 TODO，并使用正确的领域工具参数。没有任务字段版本、自动日期锚定或独立任务状态机；TODO的更新提醒也不能保证语义理解正确。

旧会话恢复时清除遗留 Frame、派生约束及缓存工具定义；保留用户消息、TODO、证据和显式历史截止时间。审计 transcript 不改写。

## 评测与历史记录

旧 `evaluate_task_dialogues.py` 字段回放及 acceptance `chain` 模式依赖已移除的数据合同，现明确报错退出，不发起模型调用。历史语料和报告保留，仅代表旧版本。工具类别 `route` 评测继续保留。

新版业务实验为 `direct` 与 `agent` 两组，不再提供 Frame 消融组。历史三组实验仍可离线重分析，不应作为当前实现的结果。

源码：`harness/host.py`、`harness/extensions.py`、`harness/inherited.py`、`harness/legacy_session.py`、`answer_review.py`、`data/financial_facts.py`。

回归：`tests/test_conversation_state.py`、`tests/test_answer_review.py`、`tests/test_online_financials.py`。
