"""Synthetic divergence fixtures; no real-market effectiveness claims."""
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import unittest

from finagent.agent import AgentLoop, RunRequest
from finagent.data.evidence import write_json
from finagent.market import MarketImport, TradingCalendar, import_market
from finagent.models import ModelReply
from finagent.research.contracts import AnalysisSubmission
from finagent.research.review import validate_claims
from finagent.runtime import ExecutionContext, TZ, now
from finagent.technical import DivergenceArgs, compare_pivots, momentum
from tests.support import ScriptedModel, call_reply
from tests.test_p0 import P0Case


class DivergenceMathTests(unittest.TestCase):
    def fixture(self, bullish=True):
        bars = [{"date": str(date(2024, 1, 1) + timedelta(days=i)), "close": "100", "low": "99", "high": "101"} for i in range(90)]
        indicators = {"macd_dif": [Decimal(-2 if bullish else 2)] * 90,
                      "macd_histogram": [Decimal(-1 if bullish else 1)] * 90,
                      "rsi_14": [Decimal(50)] * 90}
        field = "low" if bullish else "high"
        bars[70][field], bars[85][field] = ("90", "88") if bullish else ("110", "112")
        for name, values in {"macd_dif": (-5, -3) if bullish else (5, 3),
                             "macd_histogram": (-3, -1) if bullish else (3, 1),
                             "rsi_14": (20, 35) if bullish else (80, 65)}.items():
            indicators[name][70], indicators[name][85] = map(Decimal, values)
        return bars, indicators

    def test_macd_seed_and_first_update_are_explicit(self):
        values = momentum([Decimal(100), Decimal(101)])
        self.assertEqual(values["macd_dif"][0], 0)
        self.assertAlmostEqual(float(values["macd_dif"][1]), 28 / 351, places=12)
        self.assertAlmostEqual(float(values["macd_dea"][1]), (28 / 351) / 5, places=12)
        self.assertAlmostEqual(float(values["macd_histogram"][1]), 2 * ((28 / 351) * .8), places=12)

    def test_wilder_rsi_seed_and_smoothing(self):
        values = momentum(list(map(Decimal, range(1, 16))) + [Decimal(14)])
        self.assertIsNone(values["rsi_14"][13])
        self.assertEqual(values["rsi_14"][14], 100)
        self.assertAlmostEqual(float(values["rsi_14"][15]), 100 * 13 / 14, places=10)
        self.assertEqual(momentum([Decimal(100)] * 70)["rsi_14"][-1], 50)
        self.assertEqual(momentum(list(map(Decimal, range(100, 30, -1))))["rsi_14"][-1], 0)

    def test_regular_bottom_and_top_are_symmetric(self):
        for bullish in (True, False):
            bars, indicators = self.fixture(bullish)
            _, signals = compare_pivots(bars, indicators, DivergenceArgs(result_id="result_" + "a" * 32), finality="final")
            self.assertEqual(len(signals), 1)
            signal = signals[0]
            self.assertEqual(signal["direction"], "bullish" if bullish else "bearish")
            self.assertEqual(signal["status"], "confirmed")
            self.assertEqual(set(signal["matched_indicators"]), {"macd_dif", "macd_histogram", "rsi_14"})
            self.assertEqual(signal["pivot_confirmation_date"], bars[87]["date"])
            self.assertFalse(signal["price_confirmation"]["confirmed"])

    def test_no_future_bars_confirm_a_pivot_early(self):
        bars, indicators = self.fixture()
        args = DivergenceArgs(result_id="result_" + "a" * 32)
        _, early = compare_pivots(bars[:86], {k: v[:86] for k, v in indicators.items()}, args, finality="final")
        self.assertEqual(early[0]["status"], "candidate")
        self.assertIsNone(early[0]["pivot_confirmation_date"])
        self.assertEqual(early[0]["observed_through"], bars[85]["date"])
        _, later = compare_pivots(bars[:88], {k: v[:88] for k, v in indicators.items()}, args, finality="final")
        self.assertEqual(later[0]["status"], "confirmed")
        self.assertEqual(later[0]["pivot_confirmation_date"], bars[87]["date"])

    def test_source_finality_unknown_keeps_confirmed_pivots_provisional(self):
        bars, indicators = self.fixture()
        _, signals = compare_pivots(bars, indicators, DivergenceArgs(result_id="result_" + "a" * 32), finality="unknown")
        self.assertTrue(signals[0]["pivot_confirmed"])
        self.assertEqual(signals[0]["status"], "candidate")

    def test_indicator_recovery_without_new_price_low_is_not_divergence(self):
        bars, indicators = self.fixture()
        bars[85]["low"] = "91"
        _, signals = compare_pivots(bars, indicators, DivergenceArgs(result_id="result_" + "a" * 32), finality="final")
        self.assertEqual(signals, [])

    def test_macd_zero_crossing_is_not_same_zone_divergence(self):
        bars, indicators = self.fixture()
        indicators["macd_dif"][85] = Decimal(1)
        indicators["macd_histogram"][85] = Decimal(1)
        _, signals = compare_pivots(bars, indicators, DivergenceArgs(result_id="result_" + "a" * 32), finality="final")
        self.assertEqual(signals[0]["matched_indicators"], ["rsi_14"])

    def test_later_lower_low_invalidates_old_structure(self):
        bars, indicators = self.fixture()
        bars[89]["low"] = "87"
        _, signals = compare_pivots(bars, indicators, DivergenceArgs(result_id="result_" + "a" * 32), finality="final")
        old = next(s for s in signals if s["second"]["bar_index"] == 85)
        self.assertEqual(old["status"], "invalidated")
        self.assertEqual(old["invalidation_date"], bars[89]["date"])

    def test_price_breakout_is_distinct_from_pivot_confirmation(self):
        bars, indicators = self.fixture()
        bars[88]["close"], bars[88]["high"] = "103", "104"
        _, signals = compare_pivots(bars, indicators, DivergenceArgs(result_id="result_" + "a" * 32), finality="final")
        self.assertEqual(signals[0]["price_confirmation"]["date"], bars[88]["date"])
        self.assertEqual(signals[0]["pivot_confirmation_date"], bars[87]["date"])

    def test_recent_pair_is_not_skipped_to_cherry_pick_older_signal(self):
        bars, indicators = self.fixture()
        bars[78]["low"] = "91"
        indicators["macd_dif"][78] = Decimal(-1)
        indicators["macd_histogram"][78] = Decimal("-.5")
        indicators["rsi_14"][78] = Decimal(50)
        comparisons, signals = compare_pivots(bars, indicators, DivergenceArgs(result_id="result_" + "a" * 32), finality="final")
        self.assertEqual(comparisons[0]["first"]["bar_index"], 78)
        self.assertFalse(signals)


class DivergenceToolTests(P0Case):
    def setUp(self):
        super().setUp()
        self.ctx = ExecutionContext(as_of=datetime(2024, 5, 1, tzinfo=TZ), symbol="600519.SH")
        self.closes = ([Decimal(100)] * 70 + [Decimal(100 - i) for i in range(1, 21)]
                       + [Decimal(80 + i) for i in range(1, 16)]
                       + [Decimal(95) - Decimal(".8") * i for i in range(1, 21)]
                       + [Decimal(79 + i) for i in range(1, 7)])

    def import_rows(self, *, final=True, gap=False, flat=False, count=None, action=False):
        closes = self.closes if count is None else self.closes[-count:]
        sessions = list(TradingCalendar().calendar.sessions_in_range("2023-01-03", "2024-04-30"))[-len(closes):]
        rows = [{"day": str(s.date()), "open": str(c), "close": str(c), "high": str(c + 1), "low": str(c - 1),
                 "volume": "100", "supplier_final": final} for s, c in zip(sessions, closes if not flat else [Decimal(100)] * len(closes))]
        if gap:
            rows.pop(30)
        if action:
            rows[-10]["corporate_action"] = True
        path = self.root / "synthetic-divergence.json"
        path.write_text(json.dumps(rows))
        import_market(path, MarketImport(instrument="600519.SH", source="SYNTHETIC", source_url="https://example.test/daily",
                                        fetched_at=now(), finality_field="supplier_final", synthetic=True), self.store)

    def series(self, **kwargs):
        self.import_rows(**kwargs)
        return self.invoke("get_market_data", {"trade_date": "2024-04-30", "window": 200, "mode": "replay"}, self.ctx)

    def compute(self, stock):
        return self.invoke("calculate_technical_divergence", {"result_id": stock.result_id}, self.ctx)

    def test_bound_calculation_detects_bottom_and_is_accepted_by_claim_validator(self):
        stock = self.series()
        result = self.compute(stock)
        self.assertEqual(result.status, "ok", result.model_dump())
        self.assertEqual(result.data["outcome"], "detected")
        self.assertTrue(any(s["direction"] == "bullish" for s in result.data["signals"]))
        self.assertEqual(result.snapshot_ids, [stock.snapshot_id])
        value = result.data["latest_indicators"]["macd_dif"]
        text = f"日线MACD DIF为{value}"
        submission = AnalysisSubmission(content_status="partial", summary=text, report_markdown=text,
            claims=[{"claim_id": "divergence", "kind": "calculation", "text": text, "calculation_refs": [result.result_id]}])
        validate_claims(submission, self.ctx, self.store)

    def test_insufficient_history_is_unknown_not_absence(self):
        result = self.compute(self.series(count=60))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["outcome"], "unavailable")
        self.assertFalse(result.data["signals"])
        self.assertTrue(result.data["missing"])

    def test_flat_series_is_a_valid_no_detection_result(self):
        result = self.compute(self.series(flat=True))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["outcome"], "not_detected")
        self.assertEqual(result.data["latest_indicators"]["rsi_14"], "50.00000000")

    def test_calendar_gap_prevents_structure_detection(self):
        result = self.compute(self.series(gap=True))
        self.assertEqual(result.data["outcome"], "unavailable")
        self.assertTrue(any("不连续" in gap for gap in result.data["missing"]))

    def test_known_corporate_action_blocks_unadjusted_comparison(self):
        result = self.compute(self.series(action=True))
        self.assertEqual(result.data["outcome"], "unavailable")
        self.assertTrue(any("公司行动" in gap for gap in result.data["missing"]))

    def test_untrusted_finality_returns_only_candidates(self):
        result = self.compute(self.series(final=False))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["data_finality"], "unknown")
        self.assertTrue(result.data["signals"])
        self.assertTrue(all(s["status"] == "candidate" for s in result.data["signals"]))

    def test_forged_price_is_rejected_against_raw_fields(self):
        stock = self.series()
        stock.data["bars"][85]["low"] = "1"
        write_json(self.root / "results" / f"{stock.result_id}.json", stock.model_dump(mode="json"))
        result = self.compute(stock)
        self.assertEqual(result.error.code, "PRICE_EVIDENCE_MISMATCH")

    def test_wrong_timeframe_or_unregistered_input_is_rejected(self):
        stock = self.series()
        bad = self.registry.execute("calculate_technical_divergence", {"result_id": stock.result_id, "timeframe": "weekly"}, self.ctx)
        self.assertEqual(bad.error.code, "VALIDATION_ERROR")
        self.ctx.evidence_ids.clear()
        self.assertEqual(self.compute(stock).error.code, "EVIDENCE_DENIED")

    def test_invalidated_input_is_rejected(self):
        stock = self.series()
        self.ctx.invalidated_result_ids.add(stock.result_id)
        self.assertEqual(self.compute(stock).error.code, "INVALIDATED_EVIDENCE")

    def test_native_loop_preserves_calculation_references_and_partial_finality(self):
        self.import_rows(final=False)

        def compute(messages):
            result = next(json.loads(m["content"]) for m in messages if m["role"] == "tool" and m["tool_call_id"] == "bars")
            return call_reply(("calculate", "calculate_technical_divergence", {"result_id": result["result_id"]}))

        def finish(messages):
            value = next(json.loads(m["content"]) for m in reversed(messages) if m["role"] == "tool")
            return ModelReply({"role": "assistant", "content": f"日线背离仍是快照候选，计算依据 {value['result_id']}。"})

        model = ScriptedModel(call_reply(("skill", "read_skill", {"name": "technical-divergence"}),
            ("bars", "get_market_data", {"trade_date": "2024-04-30", "window": 200, "mode": "replay"})), compute, finish)
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("核对日线底背离", require_data=True), self.ctx)
        self.assertEqual(result.model_calls, 3)
        self.assertEqual(result.status, "partial")
        self.assertIn("INCOMPLETE_DIVERGENCE", result.reason)
        self.assertEqual(len(result.calculation_ids), 1)
        self.assertFalse(self.requests)
