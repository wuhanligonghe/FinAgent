"""Real-model comparison with frozen SYNTHETIC market responses.

Each sample runs in a fresh process and workspace. API credentials remain in
memory. Baseline loads a supplied pre-migration source archive, not a second
runtime installed into the product. Results require human semantic review.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib


def worker(payload):
    root = Path(payload["project"])
    if payload["mode"] == "baseline":
        sys.path.insert(0, str(Path(payload["baseline"]) / "src"))
    else:
        sys.path.insert(0, str(root / "src"))
    import httpx
    from pydantic import SecretStr
    from finagent.config import Settings, read_local_env
    from finagent.data.http import SourceHTTP
    from finagent.data.service import DataService
    from finagent.data.evidence import EvidenceStore
    from finagent.errors import AgentError
    from finagent.models import CompatibleChatModel
    from finagent.runtime import now
    from finagent.provenance import package_digest
    implementation = package_digest()
    config = root / "finagent.toml"
    body = tomllib.loads(config.read_text(encoding="utf-8-sig"))
    local = read_local_env(root / ".env")
    for env, key in (("FINAGENT_BASE_URL", "base_url"), ("FINAGENT_MODEL", "name")):
        body["model"][key] = os.environ.get(env, local.get(env, body["model"].get(key, "")))
    body["model"]["api_key"] = SecretStr(os.environ.get("FINAGENT_API_KEY", local.get("FINAGENT_API_KEY", "")))
    body["model"]["max_output_tokens"] = payload["output_tokens"]
    body["limits"].update(model_calls=payload["model_calls"], tool_calls=24, source_requests=24,
                          wall_seconds=120, request_seconds=45)
    body["nlu"]["model_dir"] = str((root / body["nlu"]["model_dir"]).resolve())
    if payload["mode"] != "baseline":
        body["nlu"]["timeout_seconds"] = 3
    if payload["mode"] == "baseline":
        body.pop("harness", None)
        body["nlu"].pop("enabled", None)
        body["model"].pop("fallback_model", None)
    else:
        body["nlu"]["enabled"] = payload["mode"] == "new_hint"
        body["harness"].update(auto_memory=False, hint_timeout_seconds=3)
    stamp = now().replace(second=0, microsecond=0)
    counts, usage, source_count = {"calls": 0, "nlu_attempts": 0}, {}, {"requests": 0}
    case_limit = payload["model_calls"] * len(payload["case"]["turns"])
    nlu_usage = {}
    if payload["mode"] != "new":
        from finagent.nlu.qwen3 import Qwen3Parser
        original_parse = Qwen3Parser.parse
        def counted_parse(self, *args, **kwargs):
            if counts["calls"]+counts["nlu_attempts"] >= case_limit:
                raise AgentError("MODEL_BUDGET", "comparison shared model cap")
            counts["nlu_attempts"] += 1
            value = original_parse(self,*args,**kwargs)
            for key, item in value.usage.items():
                if isinstance(item,int):
                    nlu_usage[key]=nlu_usage.get(key,0)+item
            return value
        Qwen3Parser.parse=counted_parse
    real_class = CompatibleChatModel
    def model_factory(settings):
        actual = real_class(settings)
        class Counted:
            settings = actual.settings
            protocol = actual.protocol
            def complete(self, messages, tools, timeout):
                if counts["calls"]+counts["nlu_attempts"] >= case_limit:
                    raise AgentError("MODEL_BUDGET", "comparison shared model cap")
                counts["calls"] += 1
                premise={"role":"system","content":"这是明确授权的受控测试：金融工具返回冻结的合成数据，不是真实市场行情。按工具观测完成问题并标明合成来源；资料没有覆盖时说明缺口。不要因合成报价与现实市价不同而无限换源，也不得从记忆补充真实数字。此说明不改变工具权限、截止时间或用户其他约束。"}
                reply = actual.complete([premise,*messages], tools, timeout)
                for k, v in reply.usage.items():
                    if isinstance(v, int):
                        usage[k] = usage.get(k, 0)+v
                return reply
            def close(self):
                actual.close()
        return Counted()

    def fixture(request):
        source_count["requests"] += 1
        if source_count["requests"] > 24:
            raise httpx.RequestError("fixture source budget", request=request)
        if request.url.host == "qt.gtimg.cn":
            vendor = request.url.params.get("q", "sh600519")
            code = vendor[-6:]
            price = "20" if code == "000858" else "10"
            fields = [""]*50
            name = "SYNTHETIC 五粮液" if code == "000858" else "SYNTHETIC 贵州茅台"
            for k, v in {1:name, 2:code, 3:price, 4:"9" if price=="10" else "18", 5:price,
                         30:stamp.strftime("%Y%m%d%H%M%S"), 33:price, 34:price, 35:"10/2/3000", 36:"2", 37:"0.3",39:"--",46:"1"}.items():
                fields[k]=v
            return httpx.Response(200,content=(f'v_{vendor}="'+"~".join(fields)+'";').encode("gbk"))
        if request.url.host == "searchapi.eastmoney.com":
            query=request.url.params.get("input","")
            rows=([{"Code":"000858","Name":"五粮液","MktNum":"0","Classify":"AStock",
                    "SecurityType":"2","QuoteID":"0.000858"}] if "五粮液" in query or "000858" in query else [])
            return httpx.Response(200,json={"QuotationCodeTable":{"Status":0,"Data":rows,"TotalCount":len(rows)}})
        if request.url.host == "search-api-web.eastmoney.com":
            return httpx.Response(200,text='callback({"result":{"cmsArticleWebOld":[]}});')
        if "getFinanceReport" in request.url.path:
            return httpx.Response(200,json={"result":{"data":{"report_list":{}}}})
        return httpx.Response(503,content=b"SYNTHETIC_SOURCE_UNAVAILABLE")

    with tempfile.TemporaryDirectory(prefix="finagent-comparison-", dir="/tmp") as directory:
        runtime_root = Path(directory)
        work = runtime_root / "work"
        work.mkdir()
        (work / "report.txt").write_text("合成报告：经营现金流改善，收入未核验。禁止把本文件当真实金融证据。", encoding="utf-8")
        body["data_dir"] = str(runtime_root / "data")
        if payload["mode"] != "baseline":
            body["harness"]["workspace"] = str(work)
        settings = Settings.model_validate(body)
        def service_factory():
            store = EvidenceStore(settings.data_dir)
            client = httpx.Client(transport=httpx.MockTransport(fixture))
            return DataService(settings,SourceHTTP(store,retries=0,client=client))
        turns = payload["case"]["turns"]
        outputs, results, diagnostic = [], [], []
        started = time.monotonic()
        if payload["mode"] == "baseline":
            from unittest.mock import patch
            from finagent.agent import AgentLoop
            from finagent.chat import chat_loop
            from finagent.tools.perception import build_registry
            entries = iter([*turns, "/exit"])
            def read(prompt):
                if prompt.startswith("补充"):
                    diagnostic.append("clarification_requested")
                    return "/cancel"
                return next(entries)
            service = service_factory()
            model = model_factory(settings.model)
            try:
                with patch("finagent.research.runner.CompatibleChatModel", side_effect=model_factory), \
                     patch("finagent.research.runner.DataService", side_effect=lambda *_:service_factory()), redirect_stdout(io.StringIO()):
                    chat_loop(AgentLoop(model,build_registry(service),service.store),settings.limits,
                              read=read,write=outputs.append,research_settings=settings)
                results = [json.loads(p.read_text(encoding="utf-8")) for p in (settings.data_dir/"runs").glob("*/result.json")]
            except Exception as exc:
                diagnostic.append(type(exc).__name__)
            finally:
                service.close();model.close()
        else:
            from finagent.harness.host import SessionHost
            host = SessionHost(settings,"comparison",model=model_factory(settings.model),
                               model_factory=lambda role:model_factory(settings.model),service=service_factory())
            try:
                for turn in turns:
                    result = host.submit(turn)
                    results.append(vars(result));outputs.append(result.answer)
                    if payload["case"].get("compact"):
                        host.context.compact(host.messages,settings.limits.context_chars,force=True)
                diagnostic += [json.loads(row['body'])["kind"] for row in host.store.events(kind='understanding_hint_unavailable')]
            except Exception as exc:
                diagnostic.append(type(exc).__name__)
            finally:
                host.close()
        return {"case_id":payload["case"]["id"],"repeat":payload["repeat"],"mode":payload["mode"],
                "model":settings.model.name,"data_mode":"frozen_SYNTHETIC_sources_real_LLM","fixture_version":"v2",
                "implementation_sha256":implementation,"implementation_stable":package_digest()==implementation,
                "limits":{"model_calls_per_user_input":payload["model_calls"],"case_model_calls":case_limit,"output_tokens":payload["output_tokens"],
                          "nlu_timeout":settings.nlu.timeout_seconds,"source_requests":24},
                "seconds":round(time.monotonic()-started,3),"model_calls":counts["calls"],
                "nlu_attempts":counts["nlu_attempts"],"nlu_usage":nlu_usage,"usage":usage,
                "source_requests":source_count["requests"],"outputs":outputs,"diagnostics":diagnostic,
                "response_errors":[{"status":r.get("status"),"reason":r.get("reason")} for r in results if r.get("reason")],
                "statuses":[r.get("status") for r in results],"human_review":"pending",
                "rubric":payload["case"]["rubric"]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker",action="store_true")
    parser.add_argument("--modes",nargs="+",choices=("baseline","new","new_hint"),default=["new"])
    parser.add_argument("--repeats",type=int,default=3)
    parser.add_argument("--parallel",type=int,default=3)
    parser.add_argument("--model-calls",type=int,default=20)
    parser.add_argument("--output-tokens",type=int,default=1200)
    parser.add_argument("--baseline-archive",type=Path,default=Path("/tmp/finagent-v2-source-baseline.tar.gz"))
    parser.add_argument("--case-ids",nargs="*")
    parser.add_argument("--output",type=Path,default=Path(".finagent/v3_acceptance/model_comparison.jsonl"))
    args=parser.parse_args()
    if args.worker:
        print(json.dumps(worker(json.load(sys.stdin)),ensure_ascii=False,default=str));return
    project=Path(__file__).resolve().parent.parent
    cases=json.loads((project/"doc/harness_v3_cases.json").read_text(encoding="utf-8"))["cases"]
    if args.case_ids:
        cases=[c for c in cases if c["id"] in args.case_ids]
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="finagent-baseline-",dir="/tmp") as directory:
        baseline=Path(directory)
        if "baseline" in args.modes:
            with tarfile.open(args.baseline_archive) as archive:
                archive.extractall(baseline,filter="data")
        jobs=[dict(project=str(project),baseline=str(baseline),mode=mode,case=case,repeat=repeat,
                   model_calls=args.model_calls,output_tokens=args.output_tokens)
              for mode in args.modes for repeat in range(args.repeats) for case in cases]
        def run(payload):
            try:
                result=subprocess.run([sys.executable,str(Path(__file__).resolve()),"--worker"],input=json.dumps(payload,ensure_ascii=False),
                                      capture_output=True,text=True,timeout=max(240,60*len(payload["case"]["turns"])),cwd=project)
            except subprocess.TimeoutExpired:
                return {"case_id":payload["case"]["id"],"mode":payload["mode"],"repeat":payload["repeat"],
                        "error":"worker_timeout","human_review":"pending"}
            if result.returncode:
                return {"case_id":payload["case"]["id"],"mode":payload["mode"],"repeat":payload["repeat"],
                        "error":"worker_failed","diagnostic":result.stderr[-1500:],"human_review":"pending"}
            return json.loads(result.stdout)
        with args.output.open("w",encoding="utf-8") as out, ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures=[pool.submit(run,p) for p in jobs]
            for i,future in enumerate(as_completed(futures),1):
                try:row=future.result()
                except Exception as exc:row={"error":type(exc).__name__,"human_review":"pending"}
                out.write(json.dumps(row,ensure_ascii=False)+"\n");out.flush()
                print(f"{i}/{len(jobs)} {row.get('mode')} {row.get('case_id')} {row.get('statuses',row.get('error'))}",flush=True)

if __name__=="__main__":main()
