"""Qwen3/Ollama diagnostics; diagnostic predictions never grant host admission."""
import math
import platform
from pathlib import Path
import statistics
import time

from ..data.evidence import write_json
from ..errors import AgentError
from .bundle import file_hash
from .qwen3 import Qwen3Parser
from .semantic import strict_json


def configured(args, settings):
    local = settings.model_copy(deep=True)
    overrides = {k: v for k, v in (("model_dir", getattr(args, "model_dir", None)),
                 ("endpoint", getattr(args, "endpoint", None)), ("model", getattr(args, "model", None))) if v is not None}
    local.nlu = type(settings.nlu).model_validate({**settings.nlu.model_dump(), **overrides})
    return local


def run_command(args, settings):
    local = configured(args, settings)
    parser = Qwen3Parser(local, diagnostic=args.diagnostic)
    status = parser.check()
    if args.command == "nlu-check":
        return status
    if args.command == "nlu-parse":
        text = args.named_text or args.text
        if not text:
            raise AgentError("EMPTY_GOAL", "需要 --text 或位置参数原话")
        return parser.parse(text, strict_json(args.context)).model_dump(mode="json")
    rows = [strict_json(s) for s in Path(args.samples).read_text(encoding="utf-8-sig").splitlines() if s.strip()]
    if len(rows) < 500:
        raise AgentError("NLU_BENCHMARK_INPUT", "性能验收需要至少500条固定样本")

    def sample(row):
        if "messages" in row:
            value = strict_json(row["messages"][1]["content"])
            return value["text"], value.get("context", {})
        return row["text"], row.get("context", {})

    first_start = time.perf_counter()
    first = parser.parse(*sample(rows[0]))
    first_ms = (time.perf_counter() - first_start) * 1000
    for row in rows[:20]:
        parser.parse(*sample(row))
    elapsed, errors, usage = [], [], []
    for index, row in enumerate(rows):
        started = time.perf_counter()
        try:
            result = parser.parse(*sample(row))
            usage.append(result.usage)
        except AgentError as exc:
            errors.append({"index": index, "code": exc.code})
        elapsed.append((time.perf_counter() - started) * 1000)
    final_status = parser.check()
    report = {"schema_version": "nlu_sft2.benchmark.1", **status,
              "platform": platform.platform(), "processor": platform.processor(),
              "endpoint": local.nlu.endpoint, "samples": len(rows), "warmup": 20,
              "sample_sha256": file_hash(args.samples), "first_request_ms": first_ms,
              "first_request_usage": first.usage, "initial_residency": status["running"],
              "final_residency": final_status["running"], "latencies_ms": elapsed, "usage": usage,
              "errors": errors, "p50_ms": statistics.median(elapsed),
              "p95_ms": sorted(elapsed)[math.ceil(.95 * len(elapsed)) - 1],
              "note": "首轮与热态分别记录；首轮是否冷加载以initial_residency和load_duration判断，未主动卸载模型"}
    report["target_met"] = not errors and report["p95_ms"] <= 2000
    write_json(Path(args.output), report)
    return report
