# 工具渐进披露与前缀缓存

v3.1 聊天与研究的工具定义和执行注册统一为 `Tool / ToolRegistry`，见[统一工具说明](UNIFIED_TOOLS.md)。本文描述的是注册之后的模型披露与缓存策略。

当前会话宿主支持两条工具加载路径。配置位于 `[model]`：

```toml
tool_loading = "deferred"
prompt_caching = true
```

项目配置为当前 Anthropic Messages 兼容网关启用上述设置；`ModelSettings` 的通用默认值仍为 `eager`、`false`。可通过 `FINAGENT_TOOL_LOADING=eager`、`FINAGENT_PROMPT_CACHING=false`（进程环境或 `.env`）恢复普通工具分组路径。改变配置后重启应用。`deferred` 需要服务端实现 Anthropic 的 `defer_loading`、`tool_reference`；仅兼容普通工具调用不代表支持该能力。`prompt_caching=true` 另外要求接受顶层 `cache_control`。若接口不支持，设置 `tool_loading="eager"`、`prompt_caching=false`；不会静默声称已使用延迟加载。Chat Completions 配置使用 `deferred` 时会在发送请求前报 `MODEL_TOOL_LOADING_UNSUPPORTED`。

## 原生延迟加载

1. 主会话将允许范围内的工具定义登记到会话目录，顺序及已登记 schema 在会话内保持稳定，重启恢复后仍复用。该目录保存在宿主状态 `_tool_catalog`，不会作为普通会话上下文发给模型。
2. `search_tools`、`select_tool_groups`、`ask_user` 为核心工具；其余定义标记 `defer_loading=true`。不提供发现工具的窄角色直接加载其允许的工具，避免全被延迟而无法调用。
3. `search_tools(query, limit)` 按名称、描述和类别检索；`select_tool_groups(groups)` 可一次加载一组。分类结果在延迟模式中是发现建议，不再用于逐轮替换顶层工具列表。
4. 只有这两个宿主发现工具的成功结果可以附加 `_tool_references`。模型适配器将其转换为标准 `tool_result.content` 中的结构化 `tool_reference`。普通工具输出中出现同名字段不会变成协议能力。
5. API 在发现结果出现的对话位置展开完整定义。后续原样保留引用，不将新定义插回历史之前。工具完整定义仍随请求提供给 API，以便展开引用。
6. 角色 `tool_filter`、工作区限制、外部操作权限、任务与研究授权仍在执行时检查，发现不代表授权。已删除或后来被过滤的工具，其已披露定义可以继续用于展开历史引用；它们不会出现在新发现结果中，也不会恢复执行权限或被删除的执行器。这也覆盖 Web 报告追问临时禁止新建研究的场景。

新增的延迟工具不会更改原有核心定义。改变已有工具的实现合同、角色范围、模型、加载模式或系统指令时，应开始新会话；不要把跨配置变化的缓存复用视为保证。宿主保存的旧 schema 不会自动跟随代码热更新。

## 普通接口

`eager` 保留原来的按工具类别筛选与补选行为，不发送原生引用或延迟标记。它兼容普通 Chat Completions 和 Messages 服务，但顶层工具列表在补选或重新分类时可能改变，因此可能影响前缀缓存。

## 上下文与用量

系统指令保持在请求前部。模型工作历史只保留最新的 `runtime_context` 状态快照，完整快照继续按变化追加到 transcript 审计文件；快照不增加权限。宿主版本号、累计用量、上次结果/预算、工具目录等记账字段不进入快照。分类和可选 NLU 的历史不读取这些宿主快照。

2026-11-18 起不再为维持追加前缀而让全部旧快照占据工作上下文。删除旧快照和历史压缩都可能改变消息前缀、重建部分缓存；静态指令与工具定义仍保持稳定，但不能保证每次缓存命中。摘要次数、冷却和回读策略见[历史摘要预算](CONTEXT_SUMMARY_BUDGET.md)。

Messages 响应用量保留 `cache_read_input_tokens`、`cache_creation_input_tokens` 等原始字段，同时规范化总 `prompt_tokens`；Chat Completions 保留供应商原始用量及嵌套字段。`model_usage` 事件保留完整数据，`state.usage.<role>` 累计包含 cache 的整数计数，嵌套字段使用点号，例如 `prompt_tokens_details.cached_tokens`。缺少缓存字段表示未取得测量值，不等于零命中。

## 验证

```bash
.venv/bin/python -m unittest tests.test_tool_discovery tests.test_messages tests.test_intent_routing tests.test_security tests.test_conversation_state -q
```

以上使用合成 HTTP/模型，检查定义稳定、结构化引用的传输与恢复、业务消息的前缀一致性、最新状态快照、权限及用量。不能据此认定真实网关支持协议，也不能从这些测试推导缓存命中率或费用下降比例。

2026-11-04 对当前配置网关另做无业务副作用的 echo 实测：完成 `search_tools → tool_reference → probe_echo` 交互，后续请求保留引用，返回 `cache_creation_input_tokens` 与 `cache_read_input_tokens` 字段。该结果验证当前网关的协议兼容性，没有测量真实金融任务的缓存收益。

协议依据：[Anthropic 工具搜索](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)、[前缀缓存](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。
