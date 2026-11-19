# v3 Harness 来源与能力映射

参考内容固定在 [基线清单](../requirements/learn_claude_code_reference_baseline_20261030.json)，提交 `0dcafa2ae053a1ddd6a72f265431104b08a5aa13` 的本地工作树，57 个文件已按 SHA-256 校验。没有修改相邻参考项目，没有运行时外部源码依赖。

继承方式：s15 的持续 messages、动态工具池、hooks/权限、事件唤醒与子执行者模式拆为实例服务；s16、s17 接入同一 Kernel。原样移植的纯定义位于 `harness/inherited.py`（s05 TodoManager、s15 cron 规则、s16 简易 schema 参考），s15 的 26 个工具 schema 在 `harness/builtin_schemas.json`。其他实现针对持久会话、金融证据与多主体并发适配。实际 schema 验证使用 jsonschema，代替教程的简易验证器。

版权/许可见根目录 `LICENSE`、`NOTICE`、`THIRD_PARTY_NOTICES`。教学 Web、翻译和图片没有作为运行能力移入。

| 能力 | 参考来源 | 本项目实现 | 行为证据 |
| --- | --- | --- | --- |
| H01 | s01 agent_loop；s15 agent_loop | `harness/kernel.py`、`host.py` | A01/A04/A05/A10：持续追问，不重建任务 |
| H02 | s02 TOOLS/TOOL_HANDLERS；s15 BUILTIN_TOOLS | `tools.py`、`extensions.py`、`builtin_schemas.json` | A12/A14：真实文件产物、纠参、受控 Shell |
| H03 | s03 permission；s15 permission_hook | `tools.Permissions`、`terminal.ApprovalBroker` | A14/A15/A31：工作区边界、ask/deny、无 NLU 权限 |
| H04 | s04 HOOKS/trigger_hooks | `hooks.Hooks`、`kernel.AgentKernel` | A15：顺序与控制语义 |
| H05 | s05 TodoManager | `inherited.TodoManager`、`extensions.todo`、`host.prepare` | Todo 工具，复杂工作可更新，不强制问候建计划 |
| H06 | s06 subagent；s15 task | `host.child`、`extensions.child_task` | A11：隔离上下文、共享预算、返回后采用证据 |
| H07 | s07 SkillLoader；s15 assemble_system_prompt | 现有 `skill_library.py`、`extensions.load_skill`、动态目录 | A12；`tests.test_skills`：渐进读取、引用与权限 |
| H08 | s08 四级压缩；s15 prepare_context | `context.ContextManager` | A07：转存、微压缩、snip、摘要、配对、约束恢复 |
| H09 | s09 selection/extraction/consolidation | `context.MemoryStore`、`host.auxiliary` | A25：索引、按需召回、提取、整理、删除墓碑 |
| H10 | s10 TaskStore；s13 atomic claim | `tasks.TaskBoard`、SQLite 事务 | A18：依赖、原子认领、owner、恢复 |
| H11 | s11 background；s15 notifications | `background.Background`、统一事件宿主 | A16/A26：占位、完成/失败通知、所属会话消费 |
| H12 | s12 scheduler；s15 durable delivery | `background.Scheduler`、参考 cron 匹配器 | A17：五字段、时区、持久待投递、确认与取消 |
| H13 | s13 teams、protocols、worktrees；s15 team runtime | `background.Teams`、`tasks.TaskBoard` | A18/A19/A20：真实队友、计划版本、关机、worktree cwd |
| H14 | s14 MCPClient/assemble_tool_pool | `mcp.MCPManager/StdioMCP/MockMCP` | A21：真实进程 stdio 握手、发现、调用和冲突 |
| H15 | s15 integrated host/context/recovery | `host.SessionHost`、`terminal.run_terminal` | A11/A16/A19/A21/A27：统一运行、恢复、输入与事件协调 |
| H16 | s16 WorkflowTool/ExecutionState/Journal | `workflows.WorkflowRuntime/WorkflowContext` | A22：parallel/pipeline、schema 修正、journal、独占恢复 |
| H17 | s17 GoalController/Stop gate | `goals.GoalController`、Kernel Stop | A23/A24：独立只读判断、续跑、等待和保留失败状态 |

运行确定性验收：

```bash
PYTHONPATH=src .venv/bin/python -m unittest -v tests.test_harness_v3
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .
```

多个验收 ID 可由同一跨模块用例覆盖；其具体输入与断言在 `tests/test_harness_v3.py`。独立研究两种模式、来源/时间/计算规则和双 provider 合同继续由现有研究、数据、消息和 P0 专项测试覆盖。最终实际计数与限制以 [开发与验收记录](../history/V3_REBUILD.md) 为准。

## 明确的适配边界

- s14/s15 原生是 mock MCP；本项目保留明确标记的 mock，并增加真实 stdio。没有宣称已支持全部远程 transport。
- Shell 优先使用 bwrap 的无网络、隔离环境；如果内核拒绝 namespace，任意 Shell 不回退到宿主执行，只开放少量经过路径检查的只读 argv。`SANDBOX_UNAVAILABLE` 是真实执行环境限制，不是工具成功。
- worktree 是 Git working copy 隔离，不能当安全沙箱；删除只允许宿主显式检查并确认。
- cron 在活跃进程中调度，durable 数据在重启后恢复；没有独立系统守护进程。
- 普通聊天、子 Agent、队友与深度研究角色执行均进入 `AgentKernel`。`agent.py` 仅保留历史数据类及兼容 facade，`harness/compat.py` 负责研究记录序列化，不含模型工具 while 循环。
- Workflow 的默认子调用没有副作用工具，避免 journal 尚未落盘时恢复重复不确定的外部动作。普通隔离子 Agent 与研究角色可使用各自授权工具。
- 运行完成、Goal 达成和正文金融事实正确是不同状态；现价有程序检查，其他叙述仍需来源核验与真实质量评估。
