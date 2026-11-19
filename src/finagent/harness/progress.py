"""Concise terminal progress from observed tool events, without raw payloads."""
import json
import re
from time import monotonic


class ToolProgressReporter:
    # Free-form code, file contents, headers and credentials are never previewed.
    PARAMETERS = ("symbol", "symbols", "query", "name", "statement", "report_period",
                  "start_date", "end_date", "period", "limit", "path", "offset",
                  "mode", "task_id", "research_id", "teammate")
    STATUSES = {"ok": "完成", "completed": "完成", "partial": "返回部分数据",
                "empty": "未查到可用数据", "error": "失败", "failed": "失败",
                "cancelled": "已取消", "running": "已启动，正在后台运行",
                "pending": "等待执行", "needs_clarification": "等待补充信息"}

    def __init__(self, write, secrets=()):
        self.write = write
        self.secrets = tuple(secret for secret in secrets if secret)
        self.started = {}

    def clean(self, value, limit=100):
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, "[已隐藏]")
        text = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "[已隐藏]", text)
        text = re.sub(r"(?i)\b(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*[^\s,;]+",
                      "[已隐藏]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[已隐藏]", text)
        # Keep untrusted text on one line, including terminal escape sequences.
        text = "".join(c if c.isprintable() else " " for c in text)
        return text if len(text) <= limit else text[:limit] + "…"

    def parameters(self, raw):
        try:
            arguments = json.loads(raw)
        except (ValueError, TypeError):
            return ""
        if not isinstance(arguments, dict):
            return ""
        items = []
        for key in self.PARAMETERS:
            value = arguments.get(key)
            if isinstance(value, (str, int, float, bool)):
                items.append(key + "=" + self.clean(value))
            elif key == "symbols" and isinstance(value, list):
                items.append(key + "=" + self.clean(", ".join(str(v) for v in value[:5]
                                                          if isinstance(v, str))))
            if len(items) == 4:
                break
        return "，".join(items)

    def __call__(self, kind, payload):
        try:
            message = self.format(kind, payload)
            if message:
                self.write(message)
        except Exception:
            # A display failure must never stop a tool or alter its result.
            pass

    def format(self, kind, payload):
        if kind == "research_progress":
            return "[深度研究] " + self.clean(payload.get("text", ""), 240)
        name = self.clean(payload.get("name", ""), 80)
        call_id = payload.get("call_id")
        if kind == "tool_started":
            self.started[call_id] = monotonic()
            parameters = payload.get("parameters", "")
            return f"[工具] 调用 {name}" + (f"（{parameters}）" if parameters else "")
        if kind != "tool_result":
            return None
        started = self.started.pop(call_id, None)
        duration = f"，{monotonic() - started:.1f}s" if started is not None else ""
        value = payload.get("result")
        status, details = "已返回", []
        if isinstance(value, dict):
            error = value.get("error")
            error_code = error.get("code") if isinstance(error, dict) else error
            exit_code = value.get("exit_code", value.get("returncode"))
            if value.get("executed") is False:
                status = "未执行"
            elif error or value.get("status") in {"error", "failed"}:
                status = "失败"
            elif type(exit_code) is int:
                status = "完成" if exit_code == 0 else "执行失败"
                details.append("退出码=" + str(exit_code))
            else:
                status = self.STATUSES.get(value.get("status"), "已返回")
            if error_code:
                details.append(self.clean(error_code, 80))
            for key, label in (("source", "来源"), ("data_time", "数据时间")):
                if isinstance(value.get(key), str) and value[key]:
                    details.append(label + "=" + self.clean(value[key], 60))
            if value.get("stale") is True:
                details.append("数据已过期")
        suffix = "；".join(details)
        message = f"[工具] {name}：{status}" + (f"（{suffix}）" if suffix else "") + duration
        if name == "todo_write" and isinstance(value, dict) and not value.get("error"):
            todos = value.get("todos")
            if isinstance(todos, list):
                lines = ["[Todo]" if todos else "[Todo] 已清空"]
                for item in todos[:20]:
                    if isinstance(item, dict):
                        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(item.get("status"), "[ ]")
                        lines.append("  " + marker + " " + self.clean(item.get("content", ""), 160))
                message += "\n" + "\n".join(lines)
        return message
