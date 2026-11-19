"""Text extraction for policy imports; no company-report library."""
from pathlib import Path
import re
from bs4 import BeautifulSoup
from ..errors import AgentError


def parse_material(path: Path):
    """Preserve page/table/cell positions; no invented PDF page numbers."""
    blocks, cells = [], {}

    def table(rows, prefix):
        for r, row in enumerate(rows, 1):
            for c, value in enumerate(row, 1):
                cells[f"{prefix}/row:{r}/col:{c}"] = str(value or "").strip()
        blocks.append({"locator": prefix, "text": "\n".join(" | ".join(str(v or "") for v in row) for row in rows), "rows": rows})

    if path.suffix.lower() in (".html", ".htm"):
        soup = BeautifulSoup(path.read_text(encoding="utf-8-sig"), "html.parser")
        for hidden in soup(["script", "style"]):
            hidden.decompose()
        for i, item in enumerate(soup.find_all("table"), 1):
            # Row/column spans require manual reconstruction, not guessed columns.
            if item.select('[rowspan]:not([rowspan="1"]), [colspan]:not([colspan="1"])'):
                blocks.append({"locator": f"table:{i}", "text": item.get_text(" ", strip=True), "pending": "merged cells"})
            else:
                table([[v.get_text(" ", strip=True) for v in row.find_all(["td", "th"])] for row in item.find_all("tr")], f"table:{i}")
        text = soup.get_text("\n", strip=True)
    elif path.suffix.lower() == ".pdf":
        try:
            import pdfplumber
        except ImportError as exc:
            raise AgentError("PDF_DEPENDENCY", "请安装 finagent[pdf] 以读取文本PDF和表格") from exc
        with pdfplumber.open(path) as pdf:
            for p, page in enumerate(pdf.pages, 1):
                blocks.append({"locator": f"page:{p}", "text": page.extract_text() or ""})
                for i, rows in enumerate(page.extract_tables(), 1):
                    table(rows, f"page:{p}/table:{i}")
        text = "\n".join(b["text"] for b in blocks if re.fullmatch(r"page:\d+", b["locator"]))
    elif path.suffix.lower() in (".txt", ".md"):
        text = path.read_text(encoding="utf-8-sig")
    else:
        raise AgentError("UNSUPPORTED_DOCUMENT", "只接受文本PDF、UTF-8 HTML/Markdown/TXT；不导入训练答案字段")
    for i in range(0, len(text), 1200):
        blocks.append({"locator": f"text:{i}:{min(i+1200,len(text))}", "text": text[i:i+1200]})
    if not text.strip():
        raise AgentError("UNPARSEABLE_DOCUMENT", "未提取到文字；扫描件应先人工核验或另行OCR")
    return blocks, cells

