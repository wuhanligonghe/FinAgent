"""SYNTHETIC HTTP/model fixtures; none of these prices are market observations."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from finagent.config import Settings, NLUSettings
from finagent.data.evidence import EvidenceStore
from finagent.data.http import SourceHTTP
from finagent.data.service import DataService
from finagent.models import ModelReply, ToolCall
from finagent.runtime import now
from finagent.tools.perception import build_registry


def tencent_body(code="600519", vendor="sh600519", price="10", stamp=None):
    fields = [""] * 50
    for i, value in {1: "合成测试", 2: code, 3: price, 4: "9", 5: "9.5", 30: stamp or (now() - timedelta(days=1)).strftime("%Y%m%d%H%M%S"),
                     33: "11", 34: "8", 35: "10/2/3000", 36: "2", 37: "0.3", 39: "--", 46: "1"}.items():
        fields[i] = value
    return (f'v_{vendor}="' + "~".join(fields) + '";').encode("gbk")


def sina_body(vendor="sh600519"):
    fields = [""] * 33
    for i, value in {0: "合成测试", 1: "9.5", 2: "9", 3: "10", 4: "11", 5: "8", 8: "200", 9: "3000",
                     30: (now() - timedelta(days=1)).date().isoformat(), 31: "15:00:00"}.items():
        fields[i] = value
    return (f'var hq_str_{vendor}="' + ",".join(fields) + '";').encode("gbk")


def finance_body(published="20240430", value="123.45", raw_field="BIZINCO"):
    return {"result": {"data": {"report_list": {"20240331": {
        "publish_date": published, "rCurrency": "CNY", "data": [
            {"item_field": raw_field, "item_title": "合成营业收入", "item_value": value},
            {"item_field": "NETPROFIT", "item_title": "合成净利润", "item_value": "--"}]}}}}}


def call_reply(*calls, finish_reason="tool_calls"):
    values = [ToolCall(identifier, name, json.dumps(args, ensure_ascii=False) if not isinstance(args, str) else args) for identifier, name, args in calls]
    return ModelReply({"role": "assistant", "content": None, "tool_calls": [
        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in values]}, values, finish_reason)


def final_reply(evidence_ids=None, status="completed", answer="合成测试答案"):
    return ModelReply({"role": "assistant", "content": json.dumps({"status": status, "answer": answer, "evidence_ids": evidence_ids or [], "missing": []}, ensure_ascii=False)})


class ScriptedModel:
    def __init__(self, *steps):
        self.steps, self.index = steps, 0
        self.messages_seen = []

    def complete(self, messages, tools, timeout):
        self.messages_seen.append(list(messages))
        step = self.steps[min(self.index, len(self.steps) - 1)]
        self.index += 1
        return step(messages) if callable(step) else step


class ServiceCase(unittest.TestCase):
    def setUp(self):
        temp_root = Path(".finagent/test_tmp")
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp.name).resolve()
        # Existing suites explicitly exercise the legacy baseline. New NLU tests
        # use the production default and inject model/resolver fixtures.
        self.settings = Settings(data_dir=self.root, source_retries=0,
                                 nlu=NLUSettings(backend="legacy_regex", allow_legacy_regex=True))
        self.requests = []

        def dispatch(request):
            self.requests.append(request)
            return self.handler(request)

        self.client = httpx.Client(transport=httpx.MockTransport(dispatch))
        self.store = EvidenceStore(self.root)
        self.http = SourceHTTP(self.store, retries=0, client=self.client)
        self.service = DataService(self.settings, self.http)
        self.registry = build_registry(self.service)

    def handler(self, request):
        return httpx.Response(200, content=tencent_body())

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()
