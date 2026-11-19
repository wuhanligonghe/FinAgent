"""Shared tool definitions, registration and dispatch for chat and research."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import json
import subprocess
from time import monotonic
from typing import Callable, Literal

import jsonschema
from pydantic import BaseModel, ValidationError
from referencing import Registry
from referencing.exceptions import NoSuchResource

from ..data.contracts import Observation, error_observation
from ..errors import AgentError
from ..runtime import ExecutionContext


def deny_schema_resource(uri):
    raise NoSuchResource(ref=uri)


def check_schema(schema):
    jsonschema.Draft202012Validator.check_schema(schema)

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                    raise ValueError("工具schema只允许本地引用，不能读取文件或访问远程URL")
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(schema)


def validate(instance, schema):
    check_schema(schema)
    jsonschema.Draft202012Validator(schema, registry=Registry(retrieve=deny_schema_resource)).validate(instance)


@dataclass
class Tool:
    name: str
    description: str
    arguments: type[BaseModel] | dict
    handler: Callable
    perception: bool = True
    permission: str = "read"
    output_model: type[BaseModel] | None = Observation
    _input_schema: dict = field(init=False, repr=False)

    def __post_init__(self):
        self._input_schema = (deepcopy(self.arguments) if isinstance(self.arguments, dict)
                              else self.arguments.model_json_schema())

    @property
    def input_schema(self):
        return deepcopy(self._input_schema)

    def schema(self):
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                "parameters": self.input_schema}}

    # Compatibility with extensions that used the old runtime definition API.
    definition = schema


@dataclass(frozen=True)
class ToolExecutionPolicy:
    """Scenario rules around one dispatcher; never an additional tool registry."""
    permissions: object | None = None
    before: Callable | None = None
    after: Callable | None = None
    error_format: Literal["observation", "dict"] = "observation"
    block_on_clarification: bool = True
    limit_submissions: bool = True


class ToolRegistry:
    def __init__(self, permissions=None, *, policy=None):
        self.tools: dict[str, Tool] = {}
        self.skill_library = None
        self.policy = policy or ToolExecutionPolicy(permissions=permissions)

    def register(self, tool: Tool):
        if tool.name in self.tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        check_schema(tool.input_schema)
        self.tools[tool.name] = tool

    def schemas(self):
        return [tool.schema() for tool in self.tools.values()]

    def view(self, names):
        view = ToolRegistry(policy=self.policy)
        for name in names:
            if name in self.tools:
                view.register(replace(self.tools[name]))
        if "read_skill" in view.tools and "read_skill_resource" in view.tools:
            view.skill_library = self.skill_library
        return view

    def _error(self, code, message, retryable=False):
        if self.policy.error_format == "observation":
            return error_observation(code, message, retryable)
        return {"error": code, "message": message, "retryable": retryable}

    def _permission(self, tool, args, ctx, ledger, foreground):
        started = monotonic()
        try:
            if self.policy.permissions is not None:
                self.policy.permissions.check(tool.name, args, tool.permission, foreground)
            elif tool.permission not in {"read", "internal"}:
                raise AgentError("PERMISSION_REQUIRED", "工具需要配置执行权限：" + tool.name)
        finally:
            if tool.permission == "ask":
                waiting = monotonic() - started
                ctx.started += waiting
                if ledger is not None:
                    ledger.pause(waiting)

    def execute(self, name: str, arguments: str | dict, ctx: ExecutionContext,
                ledger=None, foreground=True):
        # Normalize the previous ToolPool(call, ctx, ledger, foreground) API;
        # both forms enter the same validation and dispatch path below.
        if not isinstance(name, str):
            call = name
            if isinstance(ledger, bool):
                foreground = ledger
            name, arguments, ctx, ledger = call.name, call.arguments, arguments, ctx
        operation, tool, domain_execution = None, None, False
        def error(code, message, retryable=False):
            # Domain handler/codec failures retain the Observation contract;
            # surrounding runtime/schema/permission failures use the policy.
            if domain_execution and tool.output_model is Observation:
                return error_observation(code, message, retryable)
            return self._error(code, message, retryable)
        previous_ledger = ctx.operation_ledger
        if ledger is not None:
            ctx.operation_ledger = ledger
        try:
            ctx.check()
            if ctx.research_budget is not None:
                operation = ctx.research_budget.reserve("tool", ctx.research_task, ctx.research_reserved)
            if self.policy.limit_submissions and name == "submit_analysis":
                ctx.submission_attempts += 1
                if ctx.submission_attempts > 2:
                    raise AgentError("SUBMISSION_LIMIT", "内部提交最多一次修正")
            if name not in self.tools:
                raise AgentError("UNKNOWN_TOOL", "工具不在注册白名单内")
            tool = self.tools[name]
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            validate(args, tool.input_schema)
            self._permission(tool, args, ctx, ledger or ctx.operation_ledger, foreground)
            if self.policy.before is not None:
                args = self.policy.before(tool, args, ctx)
            domain_execution = tool.output_model is Observation
            if not isinstance(tool.arguments, dict):
                args = tool.arguments.model_validate(args)
            if self.policy.block_on_clarification and ctx.clarification and name != "ask_user":
                raise AgentError("CLARIFICATION_PENDING", "待用户澄清，依赖操作已暂停")
            value = tool.handler(args, ctx)
            ctx.check()
            if tool.output_model is not None:
                value = tool.output_model.model_validate(value.model_dump(mode="json") if isinstance(value, BaseModel) else value)
        except jsonschema.ValidationError as exc:
            fields = ", ".join(map(str, exc.absolute_path))
            value = error("VALIDATION_ERROR", "参数结构不符合合同：" + fields)
        except ValidationError as exc:
            fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors(include_input=False))
            value = error("VALIDATION_ERROR", f"参数或输出结构不符合合同：{fields}")
        except AgentError as exc:
            value = error(exc.code, exc.message, exc.retryable)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            message = str(exc)[:300] if self.policy.error_format == "dict" and not domain_execution else "工具参数无法解析"
            value = error("INVALID_ARGUMENTS", message)
        except (OSError, subprocess.SubprocessError) as exc:
            if self.policy.error_format == "dict" and not domain_execution:
                value = error("TOOL_ERROR", type(exc).__name__)
            else:
                value = error("INTERNAL_ERROR", "工具内部异常；未将异常文本暴露给模型")
        except Exception:
            value = error("INTERNAL_ERROR", "工具内部异常；未将异常文本暴露给模型")
        finally:
            ctx.operation_ledger = previous_ledger
            if operation and ctx.research_budget is not None:
                ctx.research_budget.settle(operation)
        if self.policy.after is not None:
            try:
                return self.policy.after(tool, value, ctx)
            except AgentError as exc:
                return self._error(exc.code, exc.message, exc.retryable)
            except Exception:
                return self._error("INTERNAL_ERROR", "工具结果处理失败；未将异常文本暴露给模型")
        return value


def runtime_tool(name, description, schema, handler, permission="read"):
    """Legacy constructor only: return the same Tool used by research."""
    return Tool(name, description, schema,
                lambda args, ctx: handler(args, ctx, ctx.operation_ledger),
                perception=False, permission=permission, output_model=None)
