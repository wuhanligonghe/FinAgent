"""Calendar period parsing for evidence checks; no conversation task state."""
from datetime import date
import calendar
import re


def resolve_period(raw, anchor, basis=None):
    """Parse calendar periods against the caller's date; do not store task state."""
    text = raw.strip().replace(" ", "")
    explicit = re.search(r"(20\d{2})年?", text)
    year = int(explicit[1]) if explicit else anchor.year - (2 if "前年" in text else 1 if "去年" in text else 0)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        end = date.fromisoformat(text)
        return {"raw": raw, "start": None, "end": end.isoformat(), "basis": basis or "unspecified", "anchor": anchor.isoformat()}
    quarter = None
    if text in ("上季度", "上个季度", "上一季度"):
        year, quarter = (anchor.year - 1, 4) if anchor.month <= 3 else (anchor.year, (anchor.month - 1) // 3)
    else:
        match = re.search(r"(?:第|前)?([一二三四1234])季(?:度|报)?|[Qq]([1-4])", text)
        if match:
            quarter = int(match[2]) if match[2] else "一二三四".index(match[1]) + 1 if match[1] in "一二三四" else int(match[1])
    if quarter:
        inferred = "cumulative" if "季报" in text or "前" in text else "single_quarter" if "第" in text or text in ("上季度", "上个季度", "上一季度") else None
        chosen = basis or inferred
        month = quarter * 3
        start = date(year, month - 2 if chosen == "single_quarter" else 1, 1)
        return {"raw": raw, "start": start.isoformat(), "end": date(year, month, calendar.monthrange(year, month)[1]).isoformat(),
                "basis": chosen or "unspecified", "anchor": anchor.isoformat()}
    if "上半年" in text or "半年报" in text or "中报" in text:
        start, end, chosen = date(year, 1, 1), date(year, 6, 30), "cumulative"
    elif re.fullmatch(r"20\d{2}(?:年|年度|年年报|年报)?|去年|前年|今年|去年度|去年全年", text):
        start, end, chosen = date(year, 1, 1), date(year, 12, 31), "annual"
    else:
        return None
    return {"raw": raw, "start": start.isoformat(), "end": end.isoformat(), "basis": basis or chosen, "anchor": anchor.isoformat()}

