"""Frozen, externally reviewable routing and full-chain acceptance protocol.

Gold never enters model inputs. Candidate runs cannot become acceptance runs
retroactively. Financial HTTP is a closed-world fixture; research is stubbed.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
from time import monotonic
from unittest.mock import patch

import httpx

from .data.evidence import EvidenceStore
from .data.http import SourceHTTP
from .data.service import DataService
from .dialogue_evaluation import ModelBudget, evaluation_clock
from .experiments.fixtures import COMPANIES, amount, seed_fixture
from .harness.host import SessionHost
from .harness.research import ResearchService
from .intent_routing import GROUPS, IntentRouter, QwenIntentClassifier, routing_context
from .models import CompatibleChatModel
from .provenance import package_digest

# Keep the frozen v1 label schema auditable after a capability is retired.
# This does not restore that category in live routing or grant any tools.
FROZEN_ROUTE_GROUPS = {*GROUPS, "documents"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def model_fingerprint(settings):
    return sha256(json.dumps(settings.model.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def validate(directory):
    directory = Path(directory)
    protocol = read(directory/"protocol.json")
    current = protocol["version"] == "acceptance-v2-candidate"
    allowed_groups = set(GROUPS) if current else FROZEN_ROUTE_GROUPS
    if current and set(protocol.get("route_groups", [])) != allowed_groups:
        raise ValueError("v2类别与当前实现不一致")
    cases, labels = jsonl(directory/"inputs.jsonl"), jsonl(directory/"gold.jsonl")
    if not cases or len({c["id"] for c in cases}) != len(cases) or {c["id"] for c in cases} != {g["id"] for g in labels} or len(cases) != len(labels):
        raise ValueError("输入/标签ID缺失、重复或不一致")
    gold = {g["id"]: g for g in labels}
    families, utterances = {}, {}
    for case in cases:
        if not re.fullmatch(r"a\d{3}", case["id"]) or case["split"] not in {"calibration", "acceptance"}:
            raise ValueError("case ID或split无效")
        if families.setdefault(case["family"], case["split"]) != case["split"]:
            raise ValueError("场景族跨集合泄漏")
        turns = case["turns"]
        if not turns or len(turns) != len(gold[case["id"]]["turns"]):
            raise ValueError("对话轮次不一致")
        for turn, expected in zip(turns, gold[case["id"]]["turns"]):
            if set(turn) != {"user"} or not isinstance(turn["user"], str) or not turn["user"].strip():
                raise ValueError("模型输入仅允许用户原话，不能包含标签或脚本动作")
            key = re.sub(r"\W+", "", turn["user"]).casefold()
            if key in utterances and utterances[key] != case["split"]:
                raise ValueError("相同原话跨集合泄漏")
            utterances[key] = case["split"]
            required, optional = set(expected["route_required"]), set(expected["route_optional"])
            if required & optional or not required | optional <= allowed_groups:
                raise ValueError("工具标签未知或必要/可选重叠")
            if current:
                from typing import get_args
                from .intent_routing import Action
                actions = expected.get("route_actions", [])
                if not actions or len(set(actions)) != len(actions) or not set(actions) <= set(get_args(Action)):
                    raise ValueError("v2动作标签无效")
            if not expected["rubric"] or not expected["rationale"]:
                raise ValueError("缺少人工验收规则与标注理由")
    if datetime.fromisoformat(protocol["clock"]).tzinfo is None:
        raise ValueError("冻结时钟必须带时区")
    return {"dialogues": len(cases), "turns": sum(len(c["turns"]) for c in cases),
            "splits": dict(Counter(c["split"] for c in cases)),
            "categories": dict(Counter(c["category"] for c in cases)), "annotation_status": protocol["annotation_status"]}


def fixtures():
    companies = {}
    fields = [("BIZINCO", "营业收入", "revenue"), ("NETPROFIT", "净利润", "net_profit"),
              ("PARENETP", "归属于母公司股东的净利润", "attributable_net_profit"),
              ("MANANETR", "经营活动产生的现金流量净额", "operating_cashflow"),
              ("TOTASSET", "资产总计", "total_assets"), ("TOTLIAB", "负债合计", "total_liabilities")]
    for symbol, name in COMPANIES.items():
        reports = {f"{year}1231": {"publish_date": f"{year+1}0430", "rCurrency": "CNY", "data": [
            {"item_field": raw, "item_title": label, "item_value": str(amount(symbol, year, metric))}
            for raw, label, metric in fields]} for year in (2024, 2025)}
        companies[symbol] = {"name": name, "reports": reports}
    return {"synthetic": True, "companies": companies, "unavailable_response": 503,
            "fixture_scope": "quote=10/previous=9; annual consolidated statements and original HTML; empty news; daily synthetic; other sources unavailable"}


def prepare(source, output, settings):
    source, output = Path(source), Path(output)
    coverage = validate(source)
    if output.exists() and any(output.iterdir()):
        raise ValueError("冻结目录必须为空，禁止覆盖旧证据")
    output.mkdir(parents=True, exist_ok=True)
    for name in ("inputs.jsonl", "gold.jsonl", "protocol.json"):
        shutil.copyfile(source/name, output/name)
    write(output/"fixtures.json", fixtures())
    for name in ("ANNOTATION_GUIDE.md", "README.md", "lineage.json"):
        if (source/name).exists():
            shutil.copyfile(source/name, output/name)
    package = Path(__file__).parent
    shutil.copytree(package, output/"implementation/finagent", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    manifest = {"version": 1, "created_at": datetime.now().astimezone().isoformat(), "coverage": coverage,
                "implementation_digest": package_digest(), "main_model": settings.model.name,
                "main_model_config_sha256": model_fingerprint(settings),
                "fact_check_config": settings.fact_check.model_dump(mode="json"),
                "security_config": settings.security.model_dump(mode="json"),
                "intent_config": settings.intent.model_dump(mode="json"), "files": {}}
    manifest["files"] = {str(p.relative_to(output)): digest(p) for p in sorted(output.rglob("*")) if p.is_file()}
    write(output/"manifest.json", manifest)
    bundle_sha = digest(output/"manifest.json")
    write(output/"label_review.json", {"bundle_sha256": bundle_sha, "reviewer": "", "reviewed_at": "",
        "independent_of_authoring": False, "cases": [{"id": c["id"], "decision": "pending", "notes": ""}
                                                     for c in jsonl(source/"inputs.jsonl")]})
    return {"bundle": str(output.resolve()), "bundle_sha256": bundle_sha, **coverage}


def verify(bundle):
    bundle = Path(bundle).resolve()
    manifest = read(bundle/"manifest.json")
    for relative, expected in manifest["files"].items():
        path = (bundle/relative).resolve()
        if not path.is_relative_to(bundle) or not path.is_file() or digest(path) != expected:
            raise ValueError("冻结文件摘要不匹配："+relative)
    validate(bundle)
    return manifest


def require_label_review(bundle, review_path, selected):
    review = read(review_path)
    if review.get("bundle_sha256") != digest(Path(bundle)/"manifest.json"):
        raise ValueError("审核未绑定当前冻结包")
    if not review.get("reviewer", "").strip() or review.get("independent_of_authoring") is not True:
        raise ValueError("需要真实独立审核者的具名确认；不能由开发者自动批准")
    stamp = datetime.fromisoformat(review.get("reviewed_at", ""))
    if stamp.tzinfo is None or stamp > datetime.now().astimezone():
        raise ValueError("审核时间无效")
    rows = review.get("cases", [])
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("审核条目重复")
    approved = {r["id"] for r in rows if r.get("decision") == "approved" and r.get("notes", "").strip()}
    if not set(selected) <= approved:
        raise ValueError("所选对话存在未审核或未批准标签")
    return review


def frozen_http(data):
    def respond(request):
        host, path, params = request.url.host, request.url.path, request.url.params
        if host == "searchapi.eastmoney.com":
            query = params.get("input", "")
            rows = [{"Code": s[:6], "Name": c["name"], "MktNum": "1" if s.endswith("SH") else "0",
                     "SecurityType": "1" if s.endswith("SH") else "2", "Classify": "AStock",
                     "QuoteID": ("1" if s.endswith("SH") else "0")+"."+s[:6]}
                    for s, c in data["companies"].items() if query in c["name"] or query == s[:6]]
            return httpx.Response(200, json={"QuotationCodeTable":{"Status":0,"Data":rows,"TotalCount":len(rows)}})
        if host == "qt.gtimg.cn":
            vendor = params.get("q", "sh600519")
            fields = [""]*50
            for index, value in {1:"合成行情",2:vendor[-6:],3:"10",4:"9",5:"9.5",30:"20261103095900",33:"11",34:"8",35:"10/2/3000",36:"2",37:"0.3",39:"--",46:"1"}.items():
                fields[index] = value
            return httpx.Response(200, content=(f'v_{vendor}="'+"~".join(fields)+'";').encode("gbk"))
        if "getFinanceReport" in path:
            code = params.get("paperCode", "")[2:]
            company = next((c for s,c in data["companies"].items() if s[:6] == code), None)
            return httpx.Response(200, json={"result":{"data":{"report_list":company["reports"] if company else {}}}})
        if host == "search-api-web.eastmoney.com":
            return httpx.Response(200, text='callback({"result":{"cmsArticleWebOld":[]}});')
        if "getKLineData" in path:
            start = datetime(2023, 10, 1)
            bars = []
            for i in range(1130):
                day = start+timedelta(days=i)
                if day.weekday() < 5 and day.date().isoformat() <= "2026-11-02":
                    close = 9 if day.strftime("%Y-%m-%d")=="2026-11-02" else round(9+(i%23)/100, 2)
                    bars.append({"day":day.strftime("%Y-%m-%d"),"open":str(close),"high":str(close+0.2),
                                 "low":str(close-0.2),"close":str(close),"volume":"100000"})
            return httpx.Response(200, json=bars[-800:])
        return httpx.Response(data["unavailable_response"], text="FROZEN_SYNTHETIC_SOURCE_NOT_AVAILABLE")
    return respond


def route_metrics(rows, expected, *, groups=None):
    groups = set(groups) if groups is not None else FROZEN_ROUTE_GROUPS
    counts = {g: Counter() for g in sorted(groups)}
    exact = fallbacks = action_total = action_correct = joint_correct = 0
    for index, gold in enumerate(expected):
        actual = rows[index] if index < len(rows) else None
        predicted = set((actual or {}).get("tool_groups", []))
        required, optional = set(gold["route_required"]), set(gold["route_optional"])
        valid = actual is not None and actual.get("source") in {"qwen", "rule"} and not actual.get("fallback_reason")
        exact += int(valid and required <= predicted <= required | optional)
        if gold.get("route_actions"):
            action_total += 1
            action_ok = valid and actual.get("action") in gold["route_actions"]
            action_correct += int(action_ok)
            joint_correct += int(action_ok and required <= predicted <= required | optional)
        fallbacks += int(not valid)
        for g in groups:
            counts[g]["tp"] += int(valid and g in predicted and g in required)
            counts[g]["fp"] += int(g in predicted and g not in required | optional)
            counts[g]["fn"] += int(g in required and (not valid or g not in predicted))
    per_class = {}
    for g, c in counts.items():
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        per_class[g] = {**dict(c), "precision": tp/(tp+fp) if tp+fp else 0,
                        "recall":tp/(tp+fn) if tp+fn else 0, "f1":2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0}
    supported = [v["f1"] for v in per_class.values() if v.get("tp",0)+v.get("fn",0)>0]
    return {"planned_turns":len(expected),"exact_match":exact/len(expected) if expected else 0,
            "fallback_or_missing_rate":fallbacks/len(expected) if expected else 0,
            "macro_f1_supported":sum(supported)/len(supported) if supported else 0,"per_class":per_class,
            "action_labelled_turns":action_total,
            "action_accuracy":action_correct/action_total if action_total else None,
            "action_and_groups_match":joint_correct/action_total if action_total else None}


def state_checks(actual, gold):
    return [{"field": key, "expected": value, "actual": actual.get(key),
             "passed": (set(value) <= set(actual.get(key) or []) if key == "pending_fields" else
                        sorted(value) == sorted(actual.get(key) or []) if isinstance(value, list) else actual.get(key) == value)}
            for key,value in gold.items()]


class LoggedModel:
    def __init__(self, model, output, serial):
        self.model, self.output, self.serial = model, output, serial

    @property
    def settings(self):
        return getattr(self.model, "settings", None)

    def complete(self, messages, tools, timeout):
        self.serial[0] += 1
        directory = self.output/"requests"
        directory.mkdir(exist_ok=True)
        write(directory/f"main_{self.serial[0]:05d}.json", {"messages":messages,"tools":tools,"timeout":timeout})
        return self.model.complete(messages, tools, timeout)


class CapturedRouter:
    """Preserve the initial classification before agent-driven tool expansion."""
    def __init__(self, router):
        self.router, self.initial = router, None

    def route(self, *args, **kwargs):
        result = self.router.route(*args, **kwargs)
        self.initial = result.model_dump(mode="json")
        return result


def run(bundle, output, settings, *, mode="route", split="calibration", candidate=False,
        review_path=None, case_ids=None, max_calls=100, model_factory=None, router_factory=IntentRouter):
    if mode == "chain":
        raise ValueError("Task Frame 已移除，旧chain验收的任务字段标签已停用；route评测仍可使用。")
    bundle, output = Path(bundle), Path(output)
    manifest = verify(bundle)
    if package_digest() != manifest["implementation_digest"]:
        raise ValueError("运行源码与冻结源码不一致；使用冻结包的implementation或重新冻结")
    if (model_fingerprint(settings) != manifest["main_model_config_sha256"] or settings.intent.model_dump(mode="json") != manifest["intent_config"]
            or settings.fact_check.model_dump(mode="json") != manifest.get("fact_check_config")
            or settings.security.model_dump(mode="json") != manifest.get("security_config")):
        raise ValueError("模型/意图配置与冻结清单不一致")
    cases = [c for c in jsonl(bundle/"inputs.jsonl") if c["split"] == split and (not case_ids or c["id"] in case_ids)]
    if not cases or case_ids and set(case_ids)-{c["id"] for c in cases}:
        raise ValueError("case选择为空或包含错误ID/split")
    if mode not in {"route", "chain"} or max_calls < 1:
        raise ValueError("运行模式或预算无效")
    review = None
    if split == "acceptance" and not candidate:
        if not review_path:
            raise ValueError("正式验收需要冻结后的独立标签审核；诊断运行须显式candidate")
        review = require_label_review(bundle, review_path, [c["id"] for c in cases])
    if output.exists() and any(output.iterdir()):
        raise ValueError("输出目录必须为空")
    output.mkdir(parents=True, exist_ok=True)
    if review:
        shutil.copyfile(review_path, output/"label_review_used.json")
    gold = {g["id"]: g for g in jsonl(bundle/"gold.jsonl")}
    protocol, data = read(bundle/"protocol.json"), read(bundle/"fixtures.json")
    budget = ModelBudget(max_calls)
    serial = [0]
    class CountedClassifier:
        def __init__(self, configuration):
            self.inner = QwenIntentClassifier(configuration)

        def classify(self, text, context=None, **kwargs):
            from .errors import AgentError
            parent_before = kwargs.pop("before_model", None)
            call = None
            def charge():
                nonlocal call
                with budget.lock:
                    if budget.calls >= budget.maximum:
                        raise AgentError("MODEL_BUDGET", "验收全局模型调用预算耗尽")
                    if parent_before:
                        parent_before()
                    budget.calls += 1
                    call = budget.calls
            def log(attempt):
                directory = output/"requests"
                directory.mkdir(exist_ok=True)
                write(directory/f"intent_{call:05d}.json",{**attempt,"model":settings.intent.model})
            return self.inner.classify(text,context,before_model=charge,
                                      on_usage=budget.usage.update,on_attempt=log,**kwargs)
    rows, expected_routes, routes = [], [], []
    for case in cases:
        expected_routes.extend(gold[case["id"]]["turns"])
    launch = {"count": 0}
    def stub_research(*args, **kwargs):
        launch["count"] += 1
        return {"id":"e"*32,"status":"completed","content":"unavailable","report":None}
    with evaluation_clock(datetime.fromisoformat(protocol["clock"])), patch.object(ResearchService,"start",stub_research):
        for case in cases:
            config = settings.model_copy(deep=True)
            config.data_dir = output/"sessions"/case["id"]/"data"
            config.harness.workspace = output/"sessions"/case["id"]/"workspace"
            config.harness.workspace.mkdir(parents=True,exist_ok=True)
            config.harness.memory_enabled = config.harness.auto_memory = False
            config.nlu.enabled, config.intent.enabled = False, True
            config.harness.model_retries = 0
            config.limits = config.limits.model_copy(update={"model_calls":10,"tool_calls":20,"wall_seconds":180,"request_seconds":45})
            host, service = None, None
            history = []
            router = (router_factory(config.intent) if router_factory is not IntentRouter else
                      IntentRouter(config.intent,classifier=CountedClassifier(config.intent)))
            if mode == "chain":
                store = EvidenceStore(config.data_dir)
                service = DataService(config, SourceHTTP(store,retries=0,client=httpx.Client(transport=httpx.MockTransport(frozen_http(data)))))
                documents = (seed_fixture(store,[{"symbol":s,"year":y} for s in COMPANIES for y in (2024,2025)])
                             if protocol["version"] == "acceptance-v1-candidate" else
                             {"synthetic":True,"transport":"frozen_http","companies":data["companies"],
                              "legacy_library_seeded":False})
                write(output/"sessions"/case["id"]/"source_index.json",documents)
                base_model = model_factory(config.model) if model_factory else CompatibleChatModel(config.model)
                model = budget.wrap(LoggedModel(base_model, output, serial))
                host = SessionHost(config,case["id"],model=model,service=service)
                host.intent_router = CapturedRouter(router)
                host.tool_filter = set(host.finance_tool_names) | {
                    "select_tool_groups","todo_write","load_skill","compact","read_output","update_state",
                    "start_deep_research","list_research","research_status","read_research_report"}
            try:
                for index, (turn, expected) in enumerate(zip(case["turns"],gold[case["id"]]["turns"])):
                    started = monotonic()
                    row = {"id":case["id"],"turn":index,"category":case["category"],"user":turn["user"],"checks":[]}
                    if mode == "route":
                        try:
                            route = router.route(turn["user"],routing_context(history)).model_dump(mode="json")
                        except Exception as exc:
                            route = {"source":"fallback","tool_groups":[],"fallback_reason":"EVALUATOR_ERROR"}
                            row["exception_type"] = type(exc).__name__
                        history.append({"role":"user","content":turn["user"]})
                        row["route"] = route
                        row["context_mode"] = "user_history_only_no_oracle_or_synthetic_assistant"
                    else:
                        before, prior = len(host.messages), launch["count"]
                        try:
                            result = host.submit(turn["user"])
                        except Exception as exc:
                            from .harness.kernel import KernelResult
                            result = KernelResult(status="failed",reason="EVALUATOR_ERROR:"+type(exc).__name__)
                        for thread,_ in list(host.background.handles.values()):
                            thread.join(timeout=5)
                        observed = {"symbol": host.ctx.symbol, "scope": dict(host.ctx.scope)}
                        calls = [c for m in host.messages[before:] for c in m.get("tool_calls",[])]
                        names = [c["function"]["name"] for c in calls]
                        outputs = [json.loads(m["content"]) for m in host.messages[before:] if m.get("role")=="tool"]
                        successful = [c["function"]["name"] for c,v in zip(calls,outputs) if isinstance(v,dict) and not v.get("error") and v.get("status") not in {"error","unavailable"}]
                        row.update(answer=result.answer,status=result.status,reason=result.reason,observed=observed,
                                   tools=names,successful_tools=successful,tool_results=outputs,
                                   route=host.intent_router.initial,
                                   effective_tool_groups=host.tool_route.tool_groups if host.tool_route else None)
                        row["checks"] = state_checks(observed,expected["state"])
                        row["checks"] += [
                            {"field":"runtime","passed":result.status in {"completed","needs_clarification"}},
                            {"field":"forbidden_tools","passed":not bool(set(names)&set(expected["forbidden_tools"]))},
                            {"field":"required_any_tools","passed":not expected["required_any_tools"] or bool(set(successful)&set(expected["required_any_tools"]))},
                            {"field":"research_launches","passed":launch["count"]-prior==expected["research_launches"]}]
                    row["elapsed_ms"] = round((monotonic()-started)*1000)
                    row["automatic_pass"] = all(c["passed"] for c in row["checks"]) if mode == "chain" else None
                    rows.append(row)
                    routes.append(row["route"])
                    with (output/"predictions.jsonl").open("a",encoding="utf-8") as f:
                        f.write(json.dumps(row,ensure_ascii=False,default=str)+"\n")
            finally:
                if host: host.close()
                if service: service.close()
    routes.extend([None]*(len(expected_routes)-len(routes)))
    metrics = route_metrics(routes,expected_routes,groups=protocol.get("route_groups"))
    successes = sum(all(r["automatic_pass"] for r in rows if r["id"]==c["id"]) and
                    sum(r["id"]==c["id"] for r in rows)==len(c["turns"]) for c in cases)
    report = {"version":1,"bundle_sha256":digest(bundle/"manifest.json"),"mode":mode,"split":split,
              "predictions_sha256":digest(output/"predictions.jsonl"),
              "label_review_sha256":digest(output/"label_review_used.json") if review else None,
              "candidate":candidate,"reviewer":review["reviewer"] if review else None,
              "model":settings.model.name,"intent_model":settings.intent.model,"implementation_digest":package_digest(),
              "selected_cases":[c["id"] for c in cases],"full_split":not case_ids,
              "dialogues_planned":len(cases),"turns_planned":len(expected_routes),"turns_run":len(rows),
              "route":metrics,"chain_automatic_dialogue_success":successes/len(cases) if mode=="chain" else None,
              "model_calls":budget.calls,"usage":dict(budget.usage),"research_stub_launches":launch["count"],
              "independent_acceptance_passed":False,"answer_review_status":"pending" if mode=="chain" else "not_applicable",
              "limitations":protocol["limitations"],"thresholds":protocol["thresholds"]}
    write(output/"report.json",report)
    write(output/"answer_review.json",{"predictions_sha256":digest(output/"predictions.jsonl"),"reviewer":"","reviewed_at":"",
        "independent_of_authoring":False,"turns":[{"id":r["id"],"turn":r["turn"],"decision":"pending",
        "dimensions":{d:"pending" for d in protocol["rubric_dimensions"]},"notes":""} for r in rows]})
    return report


def adjudicate(bundle, output, review_path=None):
    """Produce a separate final decision; never rewrite raw predictions/reports."""
    bundle, output = Path(bundle), Path(output)
    verify(bundle)
    report = read(output/"report.json")
    rows = jsonl(output/"predictions.jsonl")
    if report["bundle_sha256"] != digest(bundle/"manifest.json"):
        raise ValueError("运行报告不属于当前冻结包")
    # Run manifests bind raw predictions below; an edit invalidates adjudication.
    if report.get("predictions_sha256") != digest(output/"predictions.jsonl"):
        raise ValueError("预测记录已被修改或缺少运行摘要")
    cases = [c for c in jsonl(bundle/"inputs.jsonl") if c["split"]=="acceptance"]
    all_ids = {c["id"] for c in cases}
    if report["candidate"] or report["split"]!="acceptance" or set(report["selected_cases"])!=all_ids:
        raise ValueError("候选诊断或部分样本运行不能成为完整独立验收")
    if not report.get("label_review_sha256") or not (output/"label_review_used.json").is_file() or digest(output/"label_review_used.json")!=report["label_review_sha256"]:
        raise ValueError("缺少推理前绑定的标签审核证据")
    require_label_review(bundle, output/"label_review_used.json",all_ids)
    protocol = read(bundle/"protocol.json")
    thresholds = protocol["thresholds"]
    gold={g["id"]:g for g in jsonl(bundle/"gold.jsonl")}
    prediction_index={(r["id"],r["turn"]):r for r in rows}
    if len(prediction_index)!=len(rows):
        raise ValueError("预测记录包含重复轮次")
    order=[(c["id"],i) for c in cases for i in range(len(c["turns"]))]
    route = route_metrics([prediction_index.get(key,{}).get("route") for key in order],
                          [gold[key]["turns"][i] for key,i in order],groups=protocol.get("route_groups"))
    passed = (route["exact_match"]>=thresholds["route_exact"] and
              route["macro_f1_supported"]>=thresholds["route_macro_f1"] and
              route["fallback_or_missing_rate"]<=thresholds["route_fallback_rate_max"])
    if report["mode"]=="chain":
        if not review_path:
            raise ValueError("全链路验收需要独立人工答案审核")
        review = read(review_path)
        if (review.get("predictions_sha256")!=report["predictions_sha256"] or not review.get("reviewer","").strip()
                or review.get("independent_of_authoring") is not True):
            raise ValueError("答案审核来源或摘要无效")
        stamp=datetime.fromisoformat(review.get("reviewed_at",""))
        if stamp.tzinfo is None or stamp>datetime.now().astimezone():
            raise ValueError("答案审核时间无效")
        expected={(c["id"],i) for c in cases for i in range(len(c["turns"]))}
        turns=review.get("turns",[])
        keys={(r["id"],r["turn"]) for r in turns}
        if keys!=expected or len(keys)!=len(turns):
            raise ValueError("答案审核覆盖不全或重复")
        dimensions=read(bundle/"protocol.json")["rubric_dimensions"]
        for r in turns:
            if r.get("decision") not in {"approved","rejected"} or not r.get("notes","").strip() or set(r.get("dimensions",{}))!=set(dimensions) or any(v not in {"pass","fail"} for v in r["dimensions"].values()):
                raise ValueError("答案审核尚未完成")
        manual_ok={(r["id"],r["turn"]):r["decision"]=="approved" and all(v=="pass" for v in r["dimensions"].values()) for r in turns}
        result_index={(r["id"],r["turn"]):r for r in rows}
        successful=sum(all(manual_ok[(c["id"],i)] and result_index.get((c["id"],i),{}).get("automatic_pass",False)
                           for i in range(len(c["turns"]))) for c in cases)
        critical=sum(not check["passed"] for r in rows for check in r["checks"] if check["field"] in {"forbidden_tools","research_launches"})
        passed &= successful/len(cases)>=thresholds["chain_dialogue_success"] and critical<=thresholds["critical_violations_max"]
    decision={"independent_acceptance_passed":bool(passed),"scope":report["mode"],"recomputed_route":route,
              "report_sha256":digest(output/"report.json"),"predictions_sha256":report["predictions_sha256"],
              "answer_review_sha256":digest(review_path) if review_path else None,
              "note":"审核身份与独立性由外部验收流程确认；本工具只校验具名声明和证据绑定。"}
    if (output/"decision.json").exists():
        raise ValueError("最终决定已存在，不能覆盖")
    write(output/"decision.json",decision)
    return decision
