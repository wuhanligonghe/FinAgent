import json
import re
from urllib.parse import urlsplit

from ..errors import AgentError
from ..research.store import ResearchStore


def safe_url(value):
    try:
        parsed = urlsplit(value or "")
        return value if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


class ReportAdapter:
    def __init__(self, settings, manager):
        self.settings, self.manager = settings, manager

    def load(self, research_id):
        session_id = self.manager.report_owner(research_id)
        store = ResearchStore.from_settings(self.settings)
        snapshot = store.snapshot(research_id)
        directory = store.directory(research_id).resolve()
        if not snapshot.get("report"):
            raise AgentError("REPORT_UNAVAILABLE", "研究报告尚未生成")
        path = directory / "report.json"
        if not path.resolve().is_relative_to(directory) or not path.is_file():
            raise AgentError("REPORT_UNAVAILABLE", "报告结构化产物不可用")
        report = json.loads(path.read_text(encoding="utf-8"))
        manifest_path = directory / "evidence_manifest.json"
        if not manifest_path.resolve().is_relative_to(directory):
            raise AgentError("REPORT_DENIED", "引用路径无效")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        references = manifest.get("references", [])
        citations = [{"id": str(index + 1), "claim": ref.get("claim"), "result_id": ref.get("result_id"),
            "snapshot_id": ref.get("snapshot_id"), "locator": ref.get("locator"), "source": ref.get("source", "原始资料"),
            "source_url": safe_url(ref.get("source_url"))} for index, ref in enumerate(references)]
        findings, limitations = [], []
        for artifact in report.get("artifacts", []):
            for claim in artifact.get("claims", []):
                claim_id = f"{artifact.get('artifact_id') or artifact.get('task_id')}:{claim.get('claim_id')}"
                findings.append({"claim_id": claim_id, "text": claim.get("text", ""),
                    "kind": claim.get("kind"), "citation_ids": [item["id"] for item in citations if item["claim"] == claim_id]})
            limitations.extend(artifact.get("missing", []))
            limitations.extend(artifact.get("limitations", []))
        sections, current = [], None
        for line in report.get("report_markdown", "").splitlines():
            heading = re.match(r"^#{1,3}\s+(.+)$", line)
            if heading:
                current = {"id": f"section-{len(sections) + 1}", "title": heading[1], "markdown": ""}
                sections.append(current)
            else:
                if current is None:
                    current = {"id": "section-1", "title": "研究报告", "markdown": ""}
                    sections.append(current)
                current["markdown"] += line + "\n"
        return {"research_id": research_id, "origin_session_id": session_id,
            "title": f"{snapshot['scope']['symbol']} · 公司深度研究", "scope": snapshot["scope"],
            "execution_status": snapshot["status"], "content_status": report.get("content_status", snapshot.get("content", "unavailable")),
            "review_status": report.get("review_status", snapshot.get("review")), "outcome_verified": report.get("outcome_verified", False),
            "key_findings": findings[:3], "sections": sections, "citations": citations,
            "limitations": list(dict.fromkeys(str(item) for item in [*limitations, *report.get("errors", [])])),
            "charts": [], "markdown": report.get("report_markdown", "")}
