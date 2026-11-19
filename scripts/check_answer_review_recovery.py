"""Replay a saved review with complete stored evidence, without changing its session."""
import argparse
from datetime import datetime
import json
from pathlib import Path

import redis
import httpx

from finagent.answer_review import QwenAnswerReviewer, build_evidence_packet
from finagent.config import load_settings
from finagent.harness.state import atomic_json
from finagent.runtime import ExecutionContext
from finagent.security import SecurityGuard
from finagent.data.evidence import EvidenceStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("review_path", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--frozen-review", type=Path,
                        help="Use an already archived complete packet for an identical-input comparison")
    args = parser.parse_args()
    settings = load_settings()
    review_path = args.review_path.resolve(strict=True)
    if not review_path.is_relative_to(settings.data_dir.resolve()):
        parser.error("review_path must be inside the configured data directory")
    original = json.loads(review_path.read_text(encoding="utf-8"))
    if args.frozen_review:
        frozen_path = args.frozen_review.resolve(strict=True)
        if not frozen_path.is_relative_to(settings.data_dir.resolve()):
            parser.error("frozen-review must be inside the configured data directory")
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
        if frozen["answer"] != original["answer"] or not frozen["packet"].get("complete"):
            parser.error("frozen-review must contain the same draft and a complete archived packet")
        packet = frozen["packet"]
    else:
        session_dir = review_path.parent.parent
        runtime_root = session_dir.parent.parent
        user_root = runtime_root.parent
        marker = json.loads((runtime_root / ".runtime_redis.json").read_text())
        client = redis.Redis.from_url(settings.runtime.redis_url.get_secret_value())
        try:
            checkpoint = json.loads(client.hget(marker["namespace"] + "sessions", session_dir.name))
        finally:
            client.close()
        ctx = ExecutionContext()
        ctx.as_of = datetime.fromisoformat(checkpoint["as_of"])
        ctx.symbol = original["packet"]["execution_context"]["symbol"]
        for key in ("scope", "scope_constraints", "result_tools"):
            setattr(ctx, key, checkpoint["context"].get(key, {}))
        for key in ("evidence_ids", "invalidated_result_ids"):
            setattr(ctx, key, set(checkpoint["context"].get(key, [])))
        packet = build_evidence_packet(ctx, EvidenceStore(user_root), checkpoint["state"], settings.fact_check.review)
    packet["request_context"]["conversation_summary"] = original["packet"]["request_context"].get("conversation_summary")
    report = {"source_review": str(review_path), "answer": original["answer"], "packet": packet}
    if args.frozen_review:
        report["frozen_review"] = str(frozen_path)
    atomic_json(args.output, report)
    print(json.dumps({"complete": packet["complete"], "evidence_count": len(packet["evidence"]),
                      "incomplete": packet["incomplete"], "packet_chars": len(json.dumps(packet, ensure_ascii=False))}), flush=True)
    if not packet["complete"]:
        raise ValueError("Restored evidence packet is incomplete")
    try:
        class RecordingTransport(httpx.BaseTransport):
            def __init__(self):
                self.inner = httpx.HTTPTransport(trust_env=False,
                    proxy=settings.model.proxy_url if settings.fact_check.review.backend == "main_model" else None)

            def handle_request(self, request):
                response = self.inner.handle_request(request)
                response.read()
                report.setdefault("model_responses", []).append(response.json())
                atomic_json(args.output, report)
                return response

            def close(self):
                # The reviewer creates a short-lived client for each protocol
                # attempt; keep this transport alive across those attempts.
                pass

        transport = RecordingTransport()
        try:
            report["audit"] = {}
            report["decision"] = QwenAnswerReviewer(settings.fact_check.review, model_settings=settings.model, transport=transport).review(
                original["answer"], packet, guard=SecurityGuard(settings), audit=report["audit"])
        finally:
            transport.inner.close()
    except Exception as exc:
        report["error"] = str(exc)
        atomic_json(args.output, report)
        raise
    atomic_json(args.output, report)
    print(json.dumps({k: report["decision"][k] for k in ("complete", "verdict", "elapsed_ms")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
