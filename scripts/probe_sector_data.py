"""Live sector feasibility probe using FinAgent's existing HTTP/evidence layer.

Run: PYTHONPATH=src .venv/bin/python scripts/probe_sector_data.py
This does not register production tools or change the persistent source whitelist.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
import re

from finagent.data.contracts import DailyBar
from finagent.config import load_settings
from finagent.data.endpoints import ALLOWED_HOSTS
from finagent.data.evidence import EvidenceStore, write_json
from finagent.data.http import SourceHTTP
from finagent.data.providers.common import number, source_result
from finagent.errors import AgentError
from finagent.runtime import ExecutionContext, TZ, now


LIST_URL = "https://79.push2.eastmoney.com/api/qt/clist/get"
QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
PROBE_HOSTS = {"79.push2.eastmoney.com", "push2.eastmoney.com", "push2his.eastmoney.com"}
FIELDS = "f2,f3,f4,f5,f6,f12,f13,f14,f20,f104,f105,f124"
COMMON = {"pz": 100, "po": 0, "np": 1, "fltt": 2, "invt": 2,
          "fid": "f12", "fields": FIELDS, "ut": "bd1d9ddb04089700cf9c27f6f7426281"}


def timestamp(value, cutoff):
    value = number(value)
    if value is None or value <= 0:
        return None
    stamp = datetime.fromtimestamp(value, TZ)
    if stamp > cutoff:
        raise AgentError("AFTER_CUTOFF", "板块来源时间晚于探测截止时间")
    return stamp.isoformat()


class SectorProbe:
    def __init__(self, root):
        self.store = EvidenceStore(root)
        self.http = SourceHTTP(self.store, timeout=10, retries=1, proxy_url=load_settings().data_proxy_url)
        self.ctx = ExecutionContext()
        self.results = []

    def get(self, name, url, params):
        raw = self.http.get("eastmoney_sector_probe_" + name, url, self.ctx,
                            params=params, headers={"Referer": "https://quote.eastmoney.com/"})
        body = raw.json()
        if not isinstance(body, dict) or body.get("rc") != 0 or not isinstance(body.get("data"), dict):
            raise AgentError("INVALID_RESPONSE", "板块接口未返回成功的数据对象")
        return raw, body["data"]

    def pages(self, kind, fs):
        rows, snapshots, total = {}, [], None
        for page in range(1, 21):
            raw, data = self.get(kind, LIST_URL, {**COMMON, "fs": fs, "pn": page})
            snapshots.append(raw.snapshot_id)
            page_total = int(data["total"])
            if total is not None and page_total != total:
                raise AgentError("DATA_CONFLICT", "分页期间总数变化，不能声明完整覆盖")
            total = page_total
            entries = data["diff"]
            entries = list(entries.values()) if isinstance(entries, dict) else entries
            if not isinstance(entries, list) or not entries:
                raise AgentError("INCOMPLETE_COVERAGE", "板块分页为空，未取得完整目录")
            for entry in entries:
                code = entry["f12"]
                if code in rows:
                    raise AgentError("DATA_CONFLICT", "分页重复代码，无法确认完整覆盖")
                if kind in ("industry", "concept"):
                    if not re.fullmatch(r"BK\d{4,6}", code) or entry.get("f13") != 90:
                        raise AgentError("IDENTITY_MISMATCH", "板块代码或市场标识无效")
                    symbol = code
                else:
                    # Keep vendor market IDs, including unsupported stock markets.
                    # Do not turn a Beijing constituent into a Shanghai/Shenzhen stock.
                    symbol = f"{entry['f13']}.{code}"
                rows[code] = {"code": code, "name": entry["f14"], "vendor_symbol": symbol,
                              "price": number(entry.get("f2")),
                              "vendor_change_percent": number(entry.get("f3")),
                              "data_time": timestamp(entry.get("f124"), self.ctx.as_of),
                              "snapshot_id": raw.snapshot_id}
            print(f"{kind}: page {page}, {len(rows)}/{total}", flush=True)
            if len(rows) == total:
                result = source_result(raw, None, {"kind": "sector_" + kind,
                    "classification": "eastmoney", "rows": list(rows.values()), "total": total,
                    "coverage_complete": True, "historical_membership": False})
                result.snapshot_ids = snapshots
                result.status = "partial"
                result.warnings = ["当前供应商分类/成分，不证明历史成员、历史修订或最终收盘状态"]
                saved = self.store.save_result(result)
                self.results.append({"test": kind, "ok": True, "count": total,
                                     "result_id": saved.result_id, "snapshot_ids": snapshots})
                return list(rows.values())
            if len(rows) > total:
                raise AgentError("DATA_CONFLICT", "返回条数超出供应商总数")
        raise AgentError("INCOMPLETE_COVERAGE", "超过20页探测上限，目录仍未完整")

    def board(self, entry, kind, start, end):
        code, name = entry["code"], entry["name"]
        raw, data = self.get("quote", QUOTE_URL, {"secid": "90." + code, "fltt": 2,
            "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f124,f169,f170"})
        if data.get("f57") != code or data.get("f58") != name:
            raise AgentError("IDENTITY_MISMATCH", "板块行情名称/代码与目录不一致")
        quote = source_result(raw, code, {"kind": "sector_quote", "name": name,
            "classification": "eastmoney", "sector_type": kind,
            "price": number(data.get("f43")), "previous_close": number(data.get("f60")),
            "open": number(data.get("f46")), "high": number(data.get("f44")),
            "low": number(data.get("f45")), "volume": number(data.get("f47")),
            "amount_cny": number(data.get("f48")),
            "vendor_change_percent": number(data.get("f170")),
            "catalog_data_time": entry["data_time"]}, currency="CNY",
            data_time=timestamp(data.get("f124"), self.ctx.as_of),
            units={"price": "index_point", "volume": "vendor_sector_volume", "amount_cny": "CNY"},
            warnings=["供应商板块行情快照；最终收盘状态未独立核验；涨跌幅为供应商字段，未由程序复算"])
        if quote.data["price"] is None or quote.data["price"] <= 0:
            raise AgentError("INVALID_RESPONSE", "板块行情缺少正数价格")
        if quote.data_time is None:
            quote.warnings.append("快照接口未提供有效数据时间；catalog_data_time仅为另份目录记录的时间")
        quote.status = "partial"
        quote.snapshot_ids.append(entry["snapshot_id"])
        quote = self.store.save_result(quote)
        self.results.append({"test": kind + "_quote", "ok": True, "code": code, "name": name,
                             "result_id": quote.result_id, "data_time": quote.data_time,
                             "catalog_data_time": entry["data_time"]})

        raw, data = self.get("history", HISTORY_URL, {"secid": "90." + code,
            "klt": 101, "fqt": 0, "beg": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
            "lmt": 1000, "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"})
        if data.get("code") != code or data.get("name") != name:
            raise AgentError("IDENTITY_MISMATCH", "板块日线名称/代码与目录不一致")
        bars = []
        for index, line in enumerate(data["klines"]):
            fields = line.split(",")
            day = date.fromisoformat(fields[0])
            if not start <= day <= end:
                raise AgentError("INVALID_DATE_RANGE", "板块日线超出请求日期")
            if day >= self.ctx.as_of.astimezone(TZ).date():
                continue
            bar = DailyBar(date=day, open=fields[1], close=fields[2], high=fields[3],
                           low=fields[4], volume_shares=fields[5], amount_cny=fields[6])
            bars.append({**bar.model_dump(mode="json"), "locator": f"$.data.klines[{index}]"})
        if not bars or [b["date"] for b in bars] != sorted({b["date"] for b in bars}):
            raise AgentError("INVALID_RESPONSE", "板块日线为空或日期重复/乱序")
        history = source_result(raw, code, {"kind": "sector_history", "name": name, "bars": bars},
            currency="CNY", data_time=bars[-1]["date"], price_basis="unadjusted",
            units={"price": "index_point", "volume_shares": "vendor_sector_volume", "amount_cny": "CNY"},
            warnings=["仅验证实际返回日线的OHLCV；不声明任意历史覆盖或供应商最终完整性"])
        history.status = "partial"
        history = self.store.save_result(history)
        self.results.append({"test": kind + "_history", "ok": True, "code": code, "name": name,
                             "count": len(bars), "first": bars[0]["date"], "last": bars[-1]["date"],
                             "result_id": history.result_id})
        members = self.pages(kind + "_members", "b:" + code + " f:!50")
        self.results[-1].update(code=code, name=name)
        return len(members)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--industry", default="半导体")
    parser.add_argument("--concept", default="人工智能")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    current = now()
    start, end = args.start or current.date() - timedelta(days=30), args.end or current.date()
    if start > end or end > current.date() or (end - start).days > 365:
        parser.error("日期窗口跨度不超过一年，起日不晚于止日，止日不在未来")
    root = args.output_dir or Path(".finagent/sector_probe") / current.strftime("%Y%m%d_%H%M%S")
    added_hosts = PROBE_HOSTS - ALLOWED_HOSTS
    ALLOWED_HOSTS.update(added_hosts)
    probe = SectorProbe(root)
    try:
        for kind, query, fs in (("industry", args.industry, "m:90 t:2 f:!50"),
                                ("concept", args.concept, "m:90 t:3 f:!50")):
            try:
                entries = probe.pages(kind, fs)
                selected = [entry for entry in entries if entry["name"] == query or entry["code"] == query]
                if len(selected) != 1:
                    raise AgentError("NEEDS_CLARIFICATION", "板块未唯一匹配；请使用目录中的完整名称或代码")
                probe.board(selected[0], kind, start, end)
            except (AgentError, ValueError, KeyError, TypeError, IndexError) as exc:
                code = exc.code if isinstance(exc, AgentError) else "INVALID_RESPONSE"
                probe.results.append({"test": kind, "query": query, "ok": False, "error": code})
                print(f"{kind}: failed ({code})", flush=True)
    finally:
        probe.http.close()
        ALLOWED_HOSTS.difference_update(added_hosts)
    report = {"tested_at": current.isoformat(), "mode": "live_feasibility_probe",
              "production_tools_registered": False, "probe_only_hosts": sorted(PROBE_HOSTS),
              "classification": "eastmoney", "start": str(start), "end": str(end),
              "results": probe.results, "requests": probe.http.request_log,
              "source_documentation": "https://akshare.akfamily.xyz/data/stock/stock_board.html"}
    write_json(root / "report.json", report)
    print("REPORT_PATH=" + str(root / "report.json"), flush=True)
    return 0 if all(item["ok"] for item in probe.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
