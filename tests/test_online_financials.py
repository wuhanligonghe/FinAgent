"""Online statement workflow with synthetic HTTP; no original-document library."""
from datetime import datetime
import json

import httpx

from finagent.data.evidence import write_json
from finagent.data.financial_facts import StatementFacts
from finagent.errors import AgentError
from finagent.finish import FinalAnswer, finish_errors
from finagent.harness.host import SessionHost
from finagent.models import ModelReply
from finagent.research.review import coverage
from finagent.runtime import ExecutionContext
from .support import ServiceCase, ScriptedModel, call_reply


class OnlineFinancialTests(ServiceCase):
    def handler(self, request):
        if "getFinanceReport" not in request.url.path:
            return super().handler(request)
        fields = {"lrb": [("BIZINCO", "营业收入", ("120", "100")),
                          ("NETPROFIT", "净利润", ("12", "8"))],
                  "fzb": [("TOTASSET", "资产总计", ("1000", "900")),
                          ("TOTLIAB", "负债合计", ("400", "360"))],
                  "llb": [("MANANETR", "经营活动产生的现金流量净额", ("15", "10"))]}
        reports = {f"{year}1231": {"publish_date": f"{year + 1}0430", "rCurrency": "CNY",
            "data": [{"item_field": field, "item_title": label, "item_value": values[index]}
                     for field, label, values in fields[request.url.params["source"]]]}
                   for index, year in enumerate((2024, 2023))}
        return httpx.Response(200, json={"result": {"data": {"report_list": reports}}})

    def context(self):
        return ExecutionContext(as_of=datetime.fromisoformat("2025-05-02T00:00:00+08:00"), symbol="600519.SH")

    def invoke(self, name, args, ctx):
        result = self.store.save_result(self.registry.execute(name, args, ctx))
        ctx.result_tools[result.result_id] = name
        ctx.evidence_ids.update([result.result_id, *result.snapshot_ids])
        if result.snapshot_id:
            ctx.evidence_ids.add(result.snapshot_id)
        if self.registry.tools[name].perception:
            ctx.perception_results.add(result.result_id)
        return result

    def income(self, ctx):
        return self.invoke("get_financial_statements", {"symbol": "600519.SH", "statement": "income"}, ctx)

    def growth(self, result):
        facts = [f["fact_id"] for f in result.data["facts"] if f["metric"] == "revenue"]
        return {"purpose": "growth", "fact_ids": facts, "steps": [
            {"op": "subtract", "left": "input:0", "right": "input:1"},
            {"op": "divide", "left": "step:0", "right": "input:1"},
            {"op": "multiply", "left": "step:1", "right": "const:100"}]}

    def test_no_document_tools_and_online_growth_needs_no_library(self):
        self.assertFalse({"search_evidence", "get_company_disclosures", "read_document", "search_company_events"} & self.registry.tools.keys())
        ctx = self.context()
        income = self.income(ctx)
        self.assertEqual(income.status, "partial")
        self.assertEqual(income.data["facts"][0]["scope"], "provider_reported")
        args = self.growth(income)
        draft = self.invoke("generate_program", args, ctx)
        self.assertFalse(draft.data["executed"])
        result = self.invoke("execute_program", args, ctx)
        self.assertEqual(result.data["value"], "20.0")
        self.assertEqual(result.data["input_result_ids"], [income.result_id])
        self.assertFalse((self.root / "library").exists())
        final = FinalAnswer(status="completed", answer="合成营收同比增长20%。", evidence_ids=result.snapshot_ids,
            calculation_ids=[result.result_id], facts=[{"reference": result.result_id, "value": "20", "unit": "%"}])
        self.assertEqual(finish_errors(final, ctx, self.store, required_tools={"execute_program"}), [])

    def test_three_statements_and_calculation_satisfy_fundamental_coverage(self):
        ctx = self.context()
        income = self.income(ctx)
        for statement in ("balance", "cashflow"):
            self.invoke("get_financial_statements", {"symbol": ctx.symbol, "statement": statement}, ctx)
        self.invoke("execute_program", self.growth(income), ctx)
        self.assertEqual(coverage("fundamentals", ctx, self.store), [])

    def test_modified_operand_is_rejected_against_original_api_response(self):
        ctx = self.context()
        result = self.income(ctx)
        result.data["facts"][0]["normalized_value"] = "999"
        write_json(self.root / "results" / f"{result.result_id}.json", result.model_dump(mode="json"))
        output = self.invoke("execute_program", self.growth(result), ctx)
        self.assertEqual(output.error.code, "UNVERIFIED_FACT")

    def test_cross_task_invalidated_and_stale_operands_are_denied(self):
        ctx = self.context()
        result = self.income(ctx)
        key = result.data["facts"][0]["fact_id"]
        facts = StatementFacts(self.store)
        with self.assertRaises(AgentError):
            facts.fact(key, self.context())
        ctx.invalidated_result_ids.add(result.result_id)
        with self.assertRaises(AgentError):
            facts.fact(key, ctx)
        ctx.invalidated_result_ids.clear()
        result.stale = True
        write_json(self.root / "results" / f"{result.result_id}.json", result.model_dump(mode="json"))
        with self.assertRaises(AgentError):
            facts.fact(key, ctx)

    def test_company_period_scope_and_quarter_constraints_are_enforced(self):
        ctx = self.context()
        result = self.income(ctx)
        key = result.data["facts"][0]["fact_id"]
        facts = StatementFacts(self.store)
        ctx.symbol = "000858.SZ"
        with self.assertRaises(AgentError):
            facts.fact(key, ctx)
        ctx.symbol = "600519.SH"
        ctx.scope_constraints["allowed_report_periods"] = ["2023-12-31"]
        with self.assertRaises(AgentError):
            facts.fact(key, ctx)
        ctx.scope_constraints.clear()
        for constraints in ({"consolidation": "parent"}, {"financial_basis": "single_quarter"}, {"currency": "USD"}):
            ctx.scope_constraints = constraints
            with self.assertRaises(AgentError):
                facts.fact(key, ctx)

    def test_corrupted_raw_snapshot_is_rejected(self):
        ctx = self.context()
        result = self.income(ctx)
        (self.root / "evidence" / f"{result.snapshot_id}.bin").write_text("{}")
        output = self.invoke("execute_program", self.growth(result), ctx)
        self.assertEqual(output.error.code, "EVIDENCE_CORRUPTED")

    def test_margin_margin_change_and_debt_ratio_use_online_operands(self):
        ctx = self.context()
        income = self.income(ctx)
        balance = self.invoke("get_financial_statements", {"symbol": ctx.symbol, "statement": "balance"}, ctx)
        fact = lambda source, metric, year: next(f["fact_id"] for f in source.data["facts"]
            if f["metric"] == metric and f["period_end"] == f"{year}-12-31")
        ids = [fact(income, metric, year) for year in (2024, 2023) for metric in ("net_profit", "revenue")]
        steps = [{"op": "divide", "left": "input:0", "right": "input:1"},
                 {"op": "multiply", "left": "step:0", "right": "const:100"}]
        margin = self.invoke("execute_program", {"purpose": "net_margin", "fact_ids": ids[:2], "steps": steps}, ctx)
        self.assertEqual(margin.data["value"], "10.0")
        steps += [{"op": "divide", "left": "input:2", "right": "input:3"},
                  {"op": "multiply", "left": "step:2", "right": "const:100"},
                  {"op": "subtract", "left": "step:1", "right": "step:3"}]
        change = self.invoke("execute_program", {"purpose": "margin_change", "fact_ids": ids, "steps": steps}, ctx)
        self.assertEqual(change.data["value"], "2.00")
        self.assertEqual(change.data["unit"], "percentage_points")
        debt = self.invoke("execute_program", {"purpose": "debt_ratio", "fact_ids": [
            fact(balance, metric, 2024) for metric in ("total_liabilities", "total_assets")], "steps": steps[:2]}, ctx)
        self.assertEqual(debt.data["value"], "40.0")

    def test_wrong_business_formula_and_inline_operands_are_rejected(self):
        ctx = self.context()
        args = self.growth(self.income(ctx))
        args["steps"] = [{"op": "add", "left": "input:0", "right": "input:1"}]
        self.assertEqual(self.invoke("execute_program", args, ctx).error.code, "FORMULA_MISMATCH")
        args["fact_ids"] = ["120", "100"]
        self.assertEqual(self.invoke("execute_program", args, ctx).error.code, "EVIDENCE_DENIED")

    def test_session_can_query_calculate_and_answer_without_documents(self):
        pending = {}
        def calculate(messages):
            result = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
            pending.update(self.growth(type("Result", (), {"data": result["data"]})()))
            return call_reply(("calculate", "execute_program", pending))
        host = SessionHost(self.settings, "online-financial-test", service=self.service, model=ScriptedModel(
            call_reply(("query", "get_financial_statements", {"symbol": "600519.SH", "statement": "income"})),
            calculate, ModelReply({"role": "assistant", "content": "按供应商口径，2024年营业收入同比增长20%。"})))
        try:
            host.ctx.as_of = self.context().as_of
            host.ctx.cutoff_explicit = True
            result = host.submit("比较茅台2024年与2023年营业收入的同比变化")
            self.assertEqual(result.status, "completed", (result.reason, host.state.get("fact_check"), [(tool, self.store.load_result(key).error) for key, tool in host.ctx.result_tools.items()]))
            self.assertIn("20%", result.answer)
            self.assertFalse((self.root / "library").exists())
        finally:
            host.close()
