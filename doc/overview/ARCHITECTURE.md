# v3 当前代码结构

2026-11-15 存储更新：当前源码的认证、Web 元数据、研究账本和向量缓存已改用 MySQL，Redis 与文件继续保留。正式迁移和服务切换已完成；初始化、旧库迁移和实测状态见 [MySQL 存储交付记录](../features/MYSQL_STORAGE.md)。下文历史 SQLite 路径按迁移前版本理解。

默认入口是 `cli.py → harness.terminal → SessionHost → AgentKernel`。SessionHost 先以规则或原版 Qwen3.5-9B 分类并筛选工具类别，主模型继续负责具体执行路径；旧 NLU 是可选理解建议，深度研究独立进入 Research Service。详见[工具意图分类](../models/INTENT_ROUTING.md)。

| 模块 | 职责 |
| --- | --- |
| `harness/kernel.py` | 唯一模型—工具—观测循环；协议配对、预算、重试、停止 |
| `harness/host.py` | 持续对话、上下文/状态、来源复用、动态 prompt、按需子 Agent |
| `intent_routing.py` | 原版 Qwen JSON 分类、规则兜底、多标签工具类别与按需补选 |
| `harness/legacy_session.py` | 恢复旧会话时清理已移除的Task Frame状态，保留TODO和用户来源 |
| `dialogue_evaluation.py` | 共享合成夹具、时钟与模型预算；旧Frame字段回放已停用 |
| `experiments/` | 隔离的直接模型/当前Agent业务对照、财报数值/引用评分、冻结审核与统计报告；默认聊天不依赖 |
| `storage/` | MySQL 连接池、工作区隔离、表定义、事务与旧 SQLite 离线导入 |
| `migrations/` | Alembic 结构版本；运行时 Store 不执行建表 |
| `harness/state.py` | Redis 检查点、原子有序事件/确认、乐观任务事务；本机 lease 与 transcript 文件 |
| `harness/runtime_migration.py` | 离线导入旧会话 SQLite，保留 WAL、事件游标与备份；不是运行时回退 |
| `tools/registry.py` | 聊天 / 研究共享的 Tool 与 ToolRegistry：Schema、参数与输出、权限、视图和执行策略 |
| `harness/hooks.py` / `tools.py` | 生命周期 hooks、权限、工作区工具与沙箱；旧工具类型名仅兼容统一注册表 |
| `harness/extensions.py` | 会话控制工具注册、聊天任务范围与领域结果处理策略；金融工具不再二次注册 |
| `harness/context.py` | 分级压缩与原文回读；保留 MemoryStore 兼容导出 |
| `harness/memory.py`、`harness/memory_search.py` | 记忆范围隔离、常驻偏好、BM25/向量混合召回、重排、提取、整理与删除 |
| `harness/tasks.py` / `background.py` | 任务依赖、认领、worktree；后台、cron、队友与协议 |
| `harness/mcp.py` | mock 与真实 stdio MCP，动态发现/命名/权限 |
| `harness/workflows.py` / `goals.py` | 注册 workflow、journal 与恢复；可选独立完成判断 |
| `harness/terminal.py` | 用户输入/异步通知协调、审批、会话命令 |
| `harness/research.py` | 独立深度研究启动/状态/报告采用 |
| `harness/compat.py` / `agent.py` | 研究角色/旧产物数据适配；执行复用唯一 Kernel |
| `models.py` | Chat Completions、Anthropic Messages 适配与 provider 原始字段 |
| `data/`（含 `financial_facts.py`）、`market.py`、`technical.py` | 受控来源、接口证据、在线三表与行情/财务/背离确定性计算 |
| `research/` | 独立 deep_research/full_decision 的角色拓扑、合同、质量门、预算、报告 |
| `research/role_prompts.py` / `research/prompts/` | 独立研究角色方法，执行时加载对应正文；兼容旧综合报告 |
| `nlu/` | 可选 Qwen hint 与独立训练/诊断；旧 prepare 不在默认入口中 |
| `skill_library.py` / `skills/` | 8个通用分析方法＋1个深度研究入口，按需正文、参考与版本 |
| `maintenance.py` | 导入、旧记录查看、来源/模型诊断、独立研究 CLI |

会话是上下文所有者。用户原话、有效约束、开放问题、失败路径、来源和产物可恢复；工具请求与结果保持完整配对。工作区文件不是金融证据库，外部文本、记忆与摘要不能授予权限。

子 Agent、队友和研究角色拥有不同上下文/工具集合，执行逻辑共用 Kernel。固定多空与风险拓扑只属于独立深度研究。默认普通研究的路径由主模型决定。

v3.1 工具统一的接口、策略与兼容边界见[统一工具定义与注册](../features/UNIFIED_TOOLS.md)。

[来源对照与实际适配边界](HARNESS_PARITY.md) · [迁移和验证](../history/V3_REBUILD.md)
