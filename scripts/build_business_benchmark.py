"""Deterministically build developer-authored candidate labels, never human approval."""
import json
from pathlib import Path

from finagent.experiments.fixtures import COMPANIES


def build():
    dialogues, retrieval = [], []
    splits = {"development": ["600519.SH", "300750.SZ", "000651.SZ"], "holdout": ["601088.SH", "601012.SH", "000333.SZ"]}

    def request(symbols, periods, metrics, **extra):
        return dict(symbols=sorted(symbols), periods=sorted(periods), metrics=sorted(metrics), scope="consolidated", basis="annual", currency="CNY",
                    unit="%" if metrics and all(m.endswith("_yoy") for m in metrics) else "元", exclude=[], **extra)

    def claim(symbol, year, metric, scope="consolidated", basis="annual"):
        return dict(symbol=symbol, period_end=f"{year}-12-31" if basis == "annual" else f"{year}-09-30", metric=metric, scope=scope, basis=basis)

    def turn(user, req=None, claims=None, action="answer", clarification_field=None):
        return dict(user=user, gold=dict(action=action, request=req, claims=claims or [], clarification_field=clarification_field))

    def add(split, symbol, family, turns, extras=None, missing=False):
        fixtures = [dict(symbol=symbol, year=y) for y in ([2023, 2025] if missing else [2023, 2024, 2025])]
        fixtures.extend(extras or [])
        dialogues.append(dict(id=f"{split}_{symbol[:6]}_{family}", group_id=f"{split}_{symbol}_{family}", split=split,
            category=family, annotation_status="developer_candidate", real_research=False, fixtures=fixtures, turns=turns))

    for split, symbols in splits.items():
        for index, symbol in enumerate(symbols):
            name, other = COMPANIES[symbol], symbols[(index + 1) % len(symbols)]
            other_name = COMPANIES[other]
            r24 = request([symbol], ["2024-12-31"], ["revenue"])
            r25 = request([symbol], ["2025-12-31"], ["revenue"])
            if split == "development":
                add(split, symbol, "lookup", [turn(f"查询{name}2024年营业收入，引用原文。", r24, [claim(symbol, 2024, "revenue")])])
                add(split, symbol, "year_correction", [turn(f"查询{name}2025年营业收入。", r25, [claim(symbol, 2025, "revenue")]),
                    turn("改成2024年，其他条件保留。", r24, [claim(symbol, 2024, "revenue")])])
                add(split, symbol, "profit_clarification", [turn(f"查{name}2024年利润。", action="clarify", clarification_field="profit_basis"),
                    turn("我指归母净利润，请给原文依据。", request([symbol], ["2024-12-31"], ["attributable_net_profit"]), [claim(symbol, 2024, "attributable_net_profit")])])
                add(split, symbol, "company_correction", [turn(f"给出{name}2024年经营现金流。", request([symbol], ["2024-12-31"], ["operating_cashflow"]), [claim(symbol, 2024, "operating_cashflow")]),
                    turn(f"公司换成{other_name}，期间和指标照旧。", request([other], ["2024-12-31"], ["operating_cashflow"]), [claim(other, 2024, "operating_cashflow")])], extras=[dict(symbol=other, year=2024)])
                add(split, symbol, "comparison", [turn(f"比较{name}和{other_name}2024年营业收入，只列金额和来源。", request([symbol, other], ["2024-12-31"], ["revenue"]),
                    [claim(symbol, 2024, "revenue"), claim(other, 2024, "revenue")])], extras=[dict(symbol=other, year=2024)])
                add(split, symbol, "growth", [turn(f"计算{name}2025年营业收入同比增长率，附上两期原文。", request([symbol], ["2025-12-31"], ["revenue_yoy"]), [claim(symbol, 2025, "revenue_yoy")])])
                rp = request([symbol], ["2024-12-31"], ["revenue"]); rp["scope"] = "parent"
                add(split, symbol, "consolidation_correction", [turn(f"查{name}2024年合并营业收入。", r24, [claim(symbol, 2024, "revenue")]),
                    turn("改用母公司口径，其他不变。", rp, [claim(symbol, 2024, "revenue", "parent")])], extras=[dict(symbol=symbol, year=2024, scope="parent")])
                rq = request([symbol], ["2024-09-30"], ["revenue"]); rq["basis"] = "quarter"
                add(split, symbol, "quarter", [turn(f"查{name}2024年三季度营收。", action="clarify", clarification_field="period_basis"),
                    turn("要第三季度单季度，不是前三季度累计。", rq, [claim(symbol, 2024, "revenue", basis="quarter")])], extras=[dict(symbol=symbol, year=2024, basis="quarter"), dict(symbol=symbol, year=2024, basis="ytd")])
                add(split, symbol, "missing", [turn(f"给{name}2024年归母净利润，资料没有就直说。", request([symbol], ["2024-12-31"], ["attributable_net_profit"]), action="unavailable")], missing=True)
                re = request([symbol], ["2024-12-31"], ["operating_cashflow"]); re["exclude"] = ["price"]
                add(split, symbol, "exclusion", [turn(f"只查{name}2024年经营现金流，不看股价。", re, [claim(symbol, 2024, "operating_cashflow")])])
            else:
                add(split, symbol, "heldout_lookup", [turn(f"请从资料中摘出{ name }2024年度的营业收入，并定位到原始表格。", r24, [claim(symbol, 2024, "revenue")])])
                add(split, symbol, "heldout_followup", [turn(f"我要{name}2025年度的营业收入金额。", r25, [claim(symbol, 2025, "revenue")]),
                    turn("刚才年份说错了，应该是2024年度。", r24, [claim(symbol, 2024, "revenue")])])
                add(split, symbol, "heldout_profit", [turn(f"{name}2024年的利润是多少？", action="clarify", clarification_field="profit_basis"),
                    turn("按归属于母公司股东的净利润来回答。", request([symbol], ["2024-12-31"], ["attributable_net_profit"]), [claim(symbol, 2024, "attributable_net_profit")])])
                add(split, symbol, "heldout_compare", [turn(f"把{name}与{other_name}2024年的经营活动现金流净额放在一起列示，需来源。", request([symbol, other], ["2024-12-31"], ["operating_cashflow"]),
                    [claim(symbol, 2024, "operating_cashflow"), claim(other, 2024, "operating_cashflow")])], extras=[dict(symbol=other, year=2024)])
                add(split, symbol, "heldout_growth", [turn(f"用两份原始资料算{name}2025年度营收较上一年的增长百分比。", request([symbol], ["2025-12-31"], ["revenue_yoy"]), [claim(symbol, 2025, "revenue_yoy")])])
                add(split, symbol, "heldout_missing", [turn(f"资料里有{name}2024年度净利润吗？没有请明确说明未覆盖。", request([symbol], ["2024-12-31"], ["net_profit"]), action="unavailable")], missing=True)
            queries = {"revenue": ["营业收入", "卖货收入"], "net_profit": ["净利润", "税后赚了多少"],
                       "attributable_net_profit": ["归母净利润", "股东应占利润"], "operating_cashflow": ["经营现金流", "日常经营带来的净现金"],
                       "total_assets": ["总资产", "资产盘子"], "total_liabilities": ["总负债", "债务总盘子"]}
            if split == "holdout":
                queries = {"revenue": ["请查营业收入科目", "销售业务的收入金额"],
                    "net_profit": ["净利润科目的期末披露值", "扣除所有费用及所得税后剩余的盈利"],
                    "attributable_net_profit": ["归属于母公司股东的净利润科目", "本期应归属母公司股东的盈利"],
                    "operating_cashflow": ["经营活动产生的现金流量净额", "经营产生的净现金流入金额"],
                    "total_assets": ["资产总计的账面金额", "企业拥有的全部资产价值"],
                    "total_liabilities": ["负债合计金额", "企业对外承担的全部债务"]}
            for metric, variants in queries.items():
                for variant_index, query in enumerate(variants):
                    retrieval.append(dict(id=f"retrieval_{symbol[:6]}_{metric}_{variant_index}", split=split, symbol=symbol,
                        report_period="2024-12-31", query=query, relevant_metric=metric, scope="consolidated", basis="annual", grade="developer_candidate"))
    return dict(version="business-benchmark-v1.1", clock="2026-11-01T10:00:00+08:00", synthetic=True,
        annotation_status="developer_candidate_not_independent_human_acceptance", split_policy="group_and_company_disjoint",
        real_research_policy="disabled_reuse_prior_record_only", dialogues=dialogues, retrieval_queries=retrieval)


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "src/finagent/resources/business_benchmark.json"
    target.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(target)
