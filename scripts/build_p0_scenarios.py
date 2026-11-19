"""Versioned synthetic development matrix generator; no real market claims."""
import json
from datetime import date, timedelta
from pathlib import Path
from finagent.data.evidence import write_json

entries = json.loads(Path("src/finagent/resources/securities.json").read_text(encoding="utf-8"))
chinese = []
for entry in entries:
    for kind, question in enumerate([f"查询{entry['name']}2024年报营业收入", f"换成{entry['name']}", "那上一年呢"]):
        period = ["2024-12-31", "2023-12-31", "2022-12-31"][kind]
        company = entry["symbol"] if kind < 2 else "600519.SH"
        chinese.append({"id": f"CN{len(chinese)+1:02}", "prior_symbol": "600519.SH", "prior_period": "2023-12-31",
                        "question": question, "expected": {"symbol": company, "report_period": period, "evidence_count": 0}})
for entry in entries:
    chinese.append({"id": f"CN{len(chinese)+1:02}", "prior_symbol": entry["symbol"], "prior_period": "2024-06-30",
                    "question": "该公司那上一年呢", "expected": {"symbol": entry["symbol"], "report_period": "2023-06-30", "evidence_count": 0}})
market = []
for start, count, previous in [("2024-02-09", 10, "2024-02-08"), ("2024-04-04", 4, "2024-04-03"),
                               ("2024-05-01", 5, "2024-04-30"), ("2024-10-01", 7, "2024-09-30")]:
    for index in range(count):
        day = (date.fromisoformat(start) + timedelta(days=index)).isoformat()
        market.append({"id": f"MK{len(market)+1:02}", "requested": day, "as_of": day + "T23:59:59+08:00",
                       "expected": {"trade_date": previous, "session_ended": True}})
for stamp, ended in [("09:00:00", False), ("12:00:00", False), ("14:59:59", False), ("15:01:00", True)]:
    market.append({"id": f"MK{len(market)+1:02}", "requested": "2024-04-30", "as_of": "2024-04-30T" + stamp + "+08:00",
                   "expected": {"trade_date": "2024-04-30", "session_ended": ended}})
write_json(Path("doc/p0_scenarios.json"), {"version": "p0-development-v1", "synthetic": True,
                                        "chinese_multiturn": chinese, "market_calendar": market})
