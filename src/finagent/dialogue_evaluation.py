"""Synthetic fixtures and shared evaluation budgets.

The old Task Frame replay protocol is retired. Its corpus and saved reports are
historical artifacts; running it against a host without frames is invalid.
"""
from collections import Counter
from contextlib import contextmanager, ExitStack
from importlib.resources import files
import json
import sys
from threading import Lock
from unittest.mock import patch

import httpx
from .errors import AgentError


def load_dialogues():
    """Read the historical corpus for inspection, not current acceptance."""
    return json.loads(files("finagent").joinpath("resources/task_dialogues.json").read_text(encoding="utf-8"))


def synthetic_response(request):
    """Explicit closed-world fixture: missing data stays missing."""
    if request.url.host == "searchapi.eastmoney.com":
        query = request.url.params.get("input", "")
        candidates = [("000858", "五粮液", "0"), ("000568", "泸州老窖", "0"),
                      ("002298", "中电鑫龙", "0"), ("600764", "中国海防", "1")]
        rows = [{"Code": code, "Name": name, "MktNum": market, "Classify": "AStock", "SecurityType": "1" if market == "1" else "2", "QuoteID": f"{market}.{code}"}
                for code, name, market in candidates if query in name or query == code or (query == "中电" and code in {"002298", "600764"})]
        return httpx.Response(200, json={"QuotationCodeTable": {"Status": 0, "Data": rows, "TotalCount": len(rows)}})
    if request.url.host == "qt.gtimg.cn":
        vendor = request.url.params.get("q", "sh600519")
        fields = [""] * 50
        for i, value in {1: "合成行情", 2: vendor[-6:], 3: "10", 4: "9", 5: "9.5", 30: "20261101095900", 33: "11", 34: "8", 35: "10/2/3000", 36: "2", 37: "0.3", 39: "--", 46: "1"}.items():
            fields[i] = value
        return httpx.Response(200, content=(f'v_{vendor}="' + "~".join(fields) + '";').encode("gbk"))
    if request.url.host == "search-api-web.eastmoney.com":
        return httpx.Response(200, text='callback({"result":{"cmsArticleWebOld":[]}});')
    if "getFinanceReport" in request.url.path:
        rows = [("BIZINCO", "营业收入", "100"), ("NETPROFIT", "净利润", "20"),
                ("PARENETP", "归属于母公司股东的净利润", "18"), ("MANANETR", "经营活动产生的现金流量净额", "25")]
        reports = {f"{year}1231": {"publish_date": f"{year+1}0430", "rCurrency": "CNY",
                   "data": [{"item_field": key, "item_title": title, "item_value": value} for key, title, value in rows]}
                   for year in (2023, 2024, 2025)}
        return httpx.Response(200, json={"result": {"data": {"report_list": reports}}})
    return httpx.Response(503, text="SYNTHETIC_DATA_NOT_AVAILABLE")


@contextmanager
def evaluation_clock(stamp):
    """Freeze domain clocks, including already imported aliases, for repeatability."""
    from . import runtime
    actual = runtime.now
    frozen = lambda: stamp
    with ExitStack() as stack:
        for name, module in list(sys.modules.items()):
            if name.startswith("finagent") and getattr(module, "now", None) is actual:
                stack.enter_context(patch.object(module, "now", frozen))
        try:
            yield
        finally:
            # Modules imported during the evaluation may retain the patched alias.
            for name, module in list(sys.modules.items()):
                if name.startswith("finagent") and getattr(module, "now", None) is frozen:
                    module.now = actual


class ModelBudget:
    def __init__(self, maximum):
        self.maximum, self.calls = maximum, 0
        self.usage, self.lock = Counter(), Lock()

    def wrap(self, model):
        budget = self
        class CountedModel:
            @property
            def settings(self):
                # Preserve the production kernel's bounded truncation recovery.
                return getattr(model, "settings", None)

            def complete(self, messages, tools, timeout):
                with budget.lock:
                    if budget.calls >= budget.maximum:
                        raise AgentError("MODEL_BUDGET", "对话评测全局模型调用预算已耗尽")
                    budget.calls += 1
                premise = {"role": "system", "content": "这是授权的任务理解评测，金融工具使用冻结合成数据，不是真实市场观测。缺少财报或新闻时明确缺口，禁止从记忆补数。不要因数据缺口反复请求。此说明不改变用户目标或工具权限。"}
                reply = model.complete([premise, *messages], tools, timeout)
                with budget.lock:
                    budget.usage.update({k: v for k, v in reply.usage.items() if isinstance(v, int)})
                return reply

            def close(self):
                if hasattr(model, "close"):
                    model.close()
        return CountedModel()


def evaluate(settings, output, **options):
    raise ValueError("Task Frame 已移除，旧任务字段回放评测已停用；历史报告仅代表旧实现。请使用当前宿主回归测试。")
