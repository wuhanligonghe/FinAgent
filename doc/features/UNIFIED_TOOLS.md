# 统一工具定义与注册

更新日期：2026-11-04。适用于 v3.1。更新方案及验证记录见 [v3.1 更新文档](../requirements/FinSight_v3.1_统一工具注册更新方案.md)。

## 一套定义，多种执行策略

普通会话、研究角色、内置控制工具和 MCP 扩展统一使用 `tools/registry.py` 中的 `Tool` 和 `ToolRegistry`。每个用户、会话或研究角色仍有各自实例与授权范围；“统一”指共享实现，不是共享全部可调用工具。

```text
Tool：名称 + 描述 + 参数合同 + handler + 权限 + 输出合同
    ↓
ToolRegistry：注册、Schema、视图、校验、权限、分派
    ├── 聊天策略：任务范围、会话证据和结果转存
    └── 研究策略：角色白名单、独立预算与交接
```

SessionHost 只创建一个注册表，`registry` 是正式属性，`pool` 和 `finance` 为兼容别名，指向同一对象。金融工具不会再包装成第二种类型，也不会在执行时回调另一张注册表。

## 定义工具

类型化领域工具沿用研究侧的写法：

```python
from finagent.config import StrictModel
from finagent.data.contracts import Observation
from finagent.tools.registry import Tool, ToolRegistry

class EchoArgs(StrictModel):
    text: str

def echo(args, ctx):
    return Observation(status="ok", data={"text": args.text})

registry = ToolRegistry()
registry.register(Tool("echo", "返回输入文本", EchoArgs, echo))
```

参数通过 Pydantic 转换为 `EchoArgs`，默认输出必须符合 Observation 合同。日期、证券代码等领域转换与校验继续由对应参数模型负责。

已有 JSON Schema 的控制工具和 MCP 工具使用相同类型：

```python
registry.register(Tool(
    "control_echo", "返回控制结果",
    {"type": "object", "properties": {"text": {"type": "string"}},
     "required": ["text"], "additionalProperties": False},
    lambda args, ctx: {"accepted": args["text"]},
    perception=False, permission="internal", output_model=None,
))
```

这类 handler 接收字典。`output_model=None` 保留自身 JSON 结果，不会被强制转成金融观测。两种输入合同共享同一注册和执行实现；不是两套注册表。

正式 handler 合同为 `(args, ctx)`。需要共享操作预算的宿主控制工具可以使用 `ctx.operation_ledger`；登记到模型请求中的工具 Schema 不包含 handler 和权限对象。

## 注册与执行

注册时拒绝重名，检查 Schema 结构，并禁止 `$ref` / `$dynamicRef` 读取外部文件或网络资源。模型工具定义统一由 `Tool.schema()` / `ToolRegistry.schemas()` 生成。

执行入口：

```python
result = registry.execute("echo", {"text": "example"}, ctx, ledger)
```

参数既可以是字典，也可以是 JSON 字符串。执行顺序为上下文检查、研究工具预算预留、工具查找、Schema 校验、权限、场景前置检查、参数转换、handler、输出合同检查和结果处理。研究预留在成功、错误及中断路径中结算，每次调用只分派一次。

聊天与研究最终仍进入同一个 AgentKernel。场景相关的证据保存和角色交接分别由聊天策略、ResearchRuntime 完成，不写入金融取数函数。

## 聊天与研究如何保留差异

`ToolExecutionPolicy` 配置权限对象、before / after 回调、错误形式、待澄清限制及提交次数限制。

| 场景 | 执行前 | 执行后 |
| --- | --- | --- |
| 聊天领域工具 | 领域参数校验和澄清约束 | 保存 Observation，登记证据、已知事实和大结果回读位置 |
| 聊天控制工具 | 统一参数和权限检查，以及处理函数自身校验 | 保留任务、记忆、后台工作等 JSON 结果 |
| 研究角色工具 | 角色视图、冻结上下文、研究预算与澄清约束 | 返回 Observation，由 ResearchRuntime 授予证据、保存研究记录 |

注册表基础错误按策略返回：研究使用错误 Observation，聊天基础参数 / 权限 / 范围错误使用结构化字典。领域处理函数和领域参数转换中的错误仍保留 Observation 合同并由聊天结果策略登记。

`view(allowed)` 保留原执行策略，并复制工具描述对象，筛掉未授权工具。研究 DataBroker 和冻结评测替换 handler 时使用 `dataclasses.replace`，避免丢失 permission、output_model 等元数据。

## 工具发现与 Web 白名单

工具的注册、发现和执行授权是不同步骤。eager / deferred 继续从统一注册表生成工具定义；宿主的角色过滤、工具类别规则、任务范围和权限检查继续生效。历史 tool_reference 不能恢复已移除的执行器或权限。

`finance` 现在指向完整会话注册表，不能再用 `set(host.finance.tools)` 推导 Web 可见范围。Web 和验收入口使用 `host.finance_tool_names`，该集合在宿主添加控制工具前固定，仅记录已允许的领域工具；再显式加入原有 Web 控制工具。文件、Shell、队友和 MCP 能力不会因属性合并而自动开放。

## 兼容入口

- `harness.tools.ToolPool` 是 `ToolRegistry` 的别名，没有独立实现。
- `harness.tools.RuntimeTool(...)` 是兼容构造函数，返回统一 Tool，仅将旧的三参数 handler 适配为正式两参数合同。
- 旧的 `execute(ToolCall, ctx, ledger, foreground)` 在同一 execute 入口规范化后，走相同的校验与分派流程。
- 仓库正式注册代码使用 Tool；金融工具使用参数模型，内置和 MCP 工具使用 JSON Schema。

新增代码优先使用正式类型和名称。已有用户数据不需要迁移；更换工具实现后需要重启运行进程，旧研究恢复仍受实现指纹检查约束。

## 验证入口

跨场景合同见 [test_unified_tools.py](../../tests/test_unified_tools.py)，现有工具发现、任务范围、研究失败、Web 权限和 MCP 回归继续有效。运行完整回归前准备隔离测试 Redis，避免对业务命名空间进行清理。实际执行记录见版本更新文档末尾；这些检查不等于真实模型或金融数据质量验收。
