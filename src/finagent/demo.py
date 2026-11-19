"""Offline, synthetic demo. This verifies wiring, not real model capability."""
import json

from .agent import AgentLoop, RunRequest
from .data.contracts import Observation
from .data.evidence import EvidenceStore
from .models import ModelReply, ToolCall
from .runtime import ExecutionContext, now
from .tools.perception import SymbolArgs
from .tools.registry import Tool, ToolRegistry


class DemoModel:
    def complete(self, messages, tools, timeout):
        returned = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not returned:
            call = ToolCall("demo_1", "get_quote", '{"symbol":"600519.SH"}')
        elif len(returned) == 1:
            # The next call takes its symbol from the first observation.
            call = ToolCall("demo_2", "get_company_profile", json.dumps({"symbol": returned[0]["symbol"]}))
        else:
            content = {"status": "partial", "answer": "离线演示完成两轮反馈；全部数据及模型行为均为合成替身，不代表线上取数或真实模型验收。",
                       "evidence_ids": [r["snapshot_id"] for r in returned], "missing": ["真实模型与线上数据验证"]}
            return ModelReply({"role": "assistant", "content": json.dumps(content, ensure_ascii=False)})
        return ModelReply({"role": "assistant", "content": None, "tool_calls": [
            {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}]}, [call], "tool_calls")


def run_demo(root):
    store, registry = EvidenceStore(root), ToolRegistry()

    def fixture(name, args, ctx):
        ctx.bind_symbol(args.symbol)
        content = {"synthetic": True, "tool": name, "symbol": args.symbol, "notice": "DEMO ONLY"}
        key = store.save_raw(json.dumps(content).encode(), {"source": "synthetic_demo", "encoding": "utf-8", "fetched_at": now().isoformat()})
        return Observation(status="partial", data=content, symbol=args.symbol, market="CN_A", source="synthetic_demo",
                           fetched_at=now(), snapshot_id=key, snapshot_ids=[key], warnings=["合成演示资料，不可作为金融事实"])

    for name in ("get_quote", "get_company_profile"):
        registry.register(Tool(name, "Synthetic offline fixture", SymbolArgs, lambda a, c, n=name: fixture(n, a, c)))
    return AgentLoop(DemoModel(), registry, store).run(
        RunRequest("离线演示两轮观测反馈，不查询真实行情。", ("get_quote", "get_company_profile")), ExecutionContext())
