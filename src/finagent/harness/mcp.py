"""s14 dynamic tool pools, including a real newline-delimited stdio MCP client."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
from threading import RLock, Thread

from ..errors import AgentError
from ..tools.registry import Tool, check_schema


def normalize_name(name):
    value = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not value or len(value) > 60:
        raise ValueError("无效MCP名称")
    return value


class StdioMCP:
    def __init__(self, config, cwd=None):
        command = config.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ValueError("MCP配置command必须为宿主登记的argv数组")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
        for name in config.get("env_allow", []):
            if name in os.environ:
                env[name] = os.environ[name]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env, cwd=cwd)
        self.lines, self.lock, self.counter = queue.Queue(), RLock(), 0
        self.timeout = min(60, config.get("timeout", 15))
        def read():
            for line in self.process.stdout:
                try:
                    value = json.loads(line)
                    self.lines.put(value)
                except ValueError:
                    self.lines.put({"error": {"message": "无效MCP JSON响应"}})
            self.lines.put(None)
        Thread(target=read, name="mcp_reader", daemon=True).start()
        try:
            self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "finsight", "version": "3.0"}})
            self.notify("notifications/initialized", {})
        except BaseException:
            self.close()
            raise

    def notify(self, method, params):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        self.process.stdin.flush()

    def request(self, method, params):
        with self.lock:
            self.counter += 1
            request_id = self.counter
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                                "method": method, "params": params}) + "\n")
            self.process.stdin.flush()
            from time import monotonic
            deadline = monotonic() + self.timeout
            while True:
                try:
                    value = self.lines.get(timeout=max(.001, deadline - monotonic()))
                except queue.Empty as exc:
                    raise AgentError("MCP_TIMEOUT", "MCP服务响应超时") from exc
                if value is None:
                    raise AgentError("MCP_CLOSED", "MCP服务已关闭")
                if value.get("id") != request_id:
                    if monotonic() >= deadline:
                        raise AgentError("MCP_TIMEOUT", "MCP响应ID不匹配")
                    continue
                if "error" in value:
                    raise AgentError("MCP_ERROR", str(value["error"].get("message", "MCP调用失败"))[:300])
                return value.get("result", {})

    def tools(self):
        tools, cursor = [], None
        for _ in range(100):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise AgentError("MCP_PAGINATION", "MCP工具目录分页超过上限")

    def call(self, name, args):
        return self.request("tools/call", {"name": name, "arguments": args})

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


class MockMCP:
    """Explicit deterministic test server, never reported as a live transport."""
    def tools(self):
        return [{"name": "search", "description": "搜索模拟文档（合成结果）",
                 "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]

    def call(self, name, args):
        return {"content": [{"type": "text", "text": "SYNTHETIC: " + args["query"]}], "synthetic": True}

    def close(self):
        pass


class MCPManager:
    def __init__(self, pool, configurations=None):
        self.pool, self.configurations = pool, configurations or {}
        self.clients = {}

    def connect(self, name):
        if name in self.clients:
            return {"server": name, "already_connected": True}
        config = self.configurations.get(name)
        if config is None:
            raise ValueError("MCP服务尚未由宿主登记")
        client = MockMCP() if config.get("transport") == "mock" else StdioMCP(config, self.pool.policy.permissions.workspace)
        try:
            tools = []
            for definition in client.tools():
                tool_name = "mcp__" + normalize_name(name) + "__" + normalize_name(definition["name"])
                if tool_name in self.pool.tools or any(t.name == tool_name for t in tools):
                    raise ValueError("MCP工具规范化名称冲突")
                original = definition["name"]
                check_schema(definition.get("inputSchema", {"type":"object"}))
                kind = "read" if original in config.get("read_only", []) else "ask"
                tools.append(Tool(tool_name, definition.get("description", "MCP tool"),
                             definition.get("inputSchema", {"type": "object"}),
                             lambda args, ctx, original=original: client.call(original, args),
                             perception=False, permission=kind, output_model=None))
            for tool in tools:
                self.pool.register(tool)
            self.clients[name] = client
            return {"server": name, "tools": [t.name for t in tools], "transport": config.get("transport", "stdio")}
        except BaseException:
            client.close()
            raise

    def close(self):
        for client in self.clients.values():
            client.close()
