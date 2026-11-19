"""Explicit real-source / real-model probes, bounded and never run by tests."""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from finagent.config import load_settings
from finagent.data.evidence import write_json
from finagent.data.service import DataService
from finagent.runtime import ExecutionContext, now
from finagent.tools.perception import build_registry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("sources", "model"), default="sources")
    parser.add_argument("--symbol", default="600519.SH")
    parser.add_argument("--analysts", nargs="+", default=["technical", "sentiment", "news", "fundamentals", "policy"])
    parser.add_argument("--request-seconds", type=float, default=20)
    args = parser.parse_args()
    settings = load_settings()
    settings.research = settings.research.model_copy(update={"request_seconds": args.request_seconds})
    stamp = now().strftime("%Y%m%d_%H%M%S")
    output = settings.data_dir / "phase2_validation" / f"{args.mode}_{stamp}.json"
    if args.mode == "sources":
        records = []
        with_service = DataService(settings)
        try:
            registry = build_registry(with_service)
            end = now().date() - timedelta(days=1)
            for tool in ("get_fund_flow", "get_market_attention", "get_macro_news"):
                ctx = ExecutionContext(settings.limits, symbol=args.symbol)
                result = registry.execute(tool, {"start": str(end - timedelta(days=20)), "end": str(now().date() if tool == "get_macro_news" else end), "mode": "online"}, ctx)
                saved = with_service.store.save_result(result)
                records.append({"tool": tool, "result": saved.model_dump(mode="json"), "source_requests": ctx.source_requests})
            body = {"mode": "live_sources", "checked_at": now().isoformat(), "checks": records}
        finally:
            with_service.close()
    else:
        from finagent.research.contracts import ResearchRequest
        from finagent.research.coordinator import ResearchCoordinator
        request = ResearchRequest(goal="针对所选股票完成证据驱动研究。使用中文自然语言报告；缺数据明确保留，不要编造。", symbol=args.symbol,
                                  as_of=now(), analysts=args.analysts)
        result = ResearchCoordinator(settings).run(request, progress=lambda text: print(text, file=sys.stderr))
        body = {"mode": "real_model", "checked_at": now().isoformat(), "result": result}
    write_json(output, body)
    print(json.dumps({"report": str(output), "mode": body["mode"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
