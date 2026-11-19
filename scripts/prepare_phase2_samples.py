"""Import selected public official policies; no model calls or private files."""
from datetime import datetime
import json
from pathlib import Path
import re
import sys

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from finagent.config import load_settings
from finagent.data.evidence import EvidenceStore, write_json
from finagent.research.materials import ResearchMaterials
from finagent.research.contracts import PolicyImport
from finagent.runtime import TZ, now

SOURCES = [
    ("capital_market_2024", "https://www.gov.cn/zhengce/content/202404/content_6944877.htm", "资本市场", ["600519.SH", "300750.SZ"]),
    ("equipment_2024", "https://www.gov.cn/zhengce/content/202403/content_6939232.htm", "设备更新", ["300750.SZ"]),
]


def main():
    settings = load_settings()
    directory = settings.data_dir / "phase2_public_samples"
    directory.mkdir(parents=True, exist_ok=True)
    materials = ResearchMaterials(EvidenceStore(settings.data_dir))
    checks = []
    with httpx.Client(timeout=15, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for name, url, keyword, symbols in SOURCES:
            try:
                response = client.get(url)
                response.raise_for_status()
                soup = BeautifulSoup(response.content.decode("utf-8"), "html.parser")
                title = soup.title.get_text(" ", strip=True) if soup.title else ""
                if keyword not in title:
                    raise ValueError("官方页面标题未匹配所选政策")
                meta = soup.find("meta", attrs={"name": re.compile(r"^(firstpublishedtime|PubDate)$", re.I)})
                if not meta:
                    raise ValueError("没有可校验的官方发布时间元数据")
                match = re.search(r"20\d{2}[-/]\d{2}[-/]\d{2}", meta.get("content", ""))
                if not match:
                    raise ValueError("官方发布时间格式未知")
                published = datetime.fromisoformat(match[0].replace("/", "-")).replace(tzinfo=TZ)
                path = directory / (name + ".html")
                path.write_bytes(response.content)
                spec = PolicyImport(title=title, source_url=url, publisher="国务院", published_at=published,
                    status="announced", symbols=symbols, topics=[keyword], version="official-original")
                write_json(directory / (name + ".manifest.json"), spec.model_dump(mode="json"))
                doc = materials.import_policy(path, spec)
                checks.append({"source_url": url, "status": "imported", "title": title, "policy_id": doc["policy_id"], "published_at": published.isoformat()})
            except (httpx.HTTPError, ValueError, UnicodeError) as exc:
                checks.append({"source_url": url, "status": "failed", "reason": type(exc).__name__})
    output = settings.data_dir / "phase2_validation" / ("policies_" + now().strftime("%Y%m%d_%H%M%S") + ".json")
    write_json(output, {"mode": "official_public_import", "checks": checks})
    print(json.dumps({"report": str(output), "checks": checks}, ensure_ascii=False))


if __name__ == "__main__":
    main()
