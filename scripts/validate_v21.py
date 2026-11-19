"""Prepare a public-only validation workspace; never call a model by default."""
import argparse
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlparse
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from finagent.config import load_settings, DecisionSettings
from finagent.data.evidence import write_json, EvidenceStore
from finagent.research.decision_contracts import DecisionRequest
from finagent.research.coordinator import ResearchCoordinator
from finagent.runtime import now

PUBLIC = {"static.cninfo.com.cn", "www.gov.cn", "finance.eastmoney.com", "push2his.eastmoney.com", "eq.10jqka.com.cn"}


def prepare_public(settings, target):
    target.mkdir(parents=True, exist_ok=False)
    copied = []
    evidence = EvidenceStore(settings.data_dir)
    for folder in ("library", "policies", "signals"):
        for p in (settings.data_dir / folder).glob("*.json"):
            body = json.loads(p.read_text(encoding="utf-8"))
            url = urlparse(body.get("source_url", ""))
            raw = body.get("snapshot_id", "")
            if url.scheme != "https" or url.hostname not in PUBLIC or body.get("synthetic") or not evidence.has_raw(raw):
                continue
            (target / folder).mkdir(exist_ok=True)
            shutil.copyfile(p, target / folder / p.name)
            (target / "evidence").mkdir(exist_ok=True)
            for suffix in ("json", "bin"):
                shutil.copyfile(settings.data_dir / "evidence" / f"{raw}.{suffix}", target / "evidence" / f"{raw}.{suffix}")
            copied.append({"folder": folder, "source_url": body["source_url"], "file": p.name})
    write_json(target / "public_inputs.json", copied)
    return copied


def main():
    parser = argparse.ArgumentParser(description="默认只准备公开材料与运行计划；--execute会消耗API配额")
    parser.add_argument("--symbols", nargs="+", default=["600519.SH", "300750.SZ"])
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--request-seconds", type=float, default=20)
    args = parser.parse_args()
    if not 1 <= len(args.symbols) <= 2:
        parser.error("本入口一次最多两次研究；其他运行另行明确安排")
    settings = load_settings()
    cfg = DecisionSettings.model_validate({**settings.decision.model_dump(), "request_seconds": args.request_seconds})
    target = settings.data_dir / "v21_validation" / ("public_" + uuid4().hex)
    copied = prepare_public(settings, target)
    scoped = settings.model_copy(update={"data_dir": target, "decision": cfg})
    requests = [DecisionRequest(goal="使用公开材料完成五维、两轮多空和两轮风险研究。缺源如实保留，不编造，不输出执行价格或仓位。", symbol=s, as_of=now()) for s in args.symbols]
    body = {"workflow_version": "2.1", "mode": "real_model" if args.execute else "prepared_only", "executed_runs": 0,
        "model_host": urlparse(settings.model.base_url).hostname, "model": settings.model.name,
        "maximum_model_calls_per_run": cfg.model_calls, "maximum_runs": len(requests), "public_materials": copied, "results": []}
    output = target / "validation.json"
    write_json(output, body)
    if args.execute:
        for request in requests:
            result = ResearchCoordinator(scoped).run(request, progress=lambda x: print(x, file=sys.stderr))
            body["results"].append(result)
            body["executed_runs"] += 1
            write_json(output, body)
    print(json.dumps({"report": str(output), "mode": body["mode"], "executed_runs": body["executed_runs"], "public_inputs": len(copied)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
