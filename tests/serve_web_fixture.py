"""Browser-test server. All data and model output are synthetic; no API calls."""
from datetime import datetime
from pathlib import Path
import tempfile

import uvicorn

from finagent.config import HarnessSettings, Settings
from finagent.data.contracts import Observation
from finagent.data.evidence import EvidenceStore, write_json
from finagent.harness.host import SessionHost
from finagent.errors import AgentError
from finagent.harness.state import RuntimeStore
from finagent.models import ModelReply
from finagent.research.contracts import ResearchRequest, ResearchScope
from finagent.research.store import ResearchStore
from finagent.runtime import TZ, now
from finagent.web.app import create_app
from finagent.web.store import WebStore
from finagent.web.auth_store import AuthStore
from finagent.web.user_runtime import user_settings
PASSWORD = 'synthetic-test-password-2026'


class SyntheticModel:
    def complete(self, messages, tools, timeout):
        if any(message.get("role") == "user" and "[simulate-connect-error]" in (message.get("content") or '') for message in messages):
            raise AgentError("MODEL_UNAVAILABLE", "模型请求失败（ConnectError）", False)
        return ModelReply({"role": "assistant", "content": "这是合成模型的测试答复，不是真实金融研究。"})


def application(root):
    settings = Settings(data_dir=root, harness=HarnessSettings(workspace=root, memory_enabled=False))
    auth = AuthStore(root / 'auth', settings.auth)
    user = auth.create_user('browser', PASSWORD)
    auth.create_user('other', PASSWORD)
    root = user_settings(settings, user['id']).data_dir
    web = WebStore(root / "web")
    session = web.create_session("合成测试 · 现金流分析")
    scope = ResearchScope(**ResearchRequest(goal="合成现金流研究", symbol="600519.SH", as_of=datetime(2025, 5, 1, tzinfo=TZ), session_id=session["id"]).model_dump())
    research = ResearchStore(root)
    research_id = research.create(scope, settings.research, "browser-test")
    evidence = EvidenceStore(root)
    raw = evidence.save_raw(b"synthetic", {"synthetic": True})
    observation = evidence.save_result(Observation(status="partial", source="合成年报", source_url="https://example.com/report.pdf", snapshot_id=raw, fetched_at=now(),
        data={"blocks": [{"locator": "page:3", "text": "原始披露片段（合成测试，非真实财务数据）"}]}))
    directory = research.directory(research_id)
    report = {"content_status": "partial", "review_status": "issues_found", "outcome_verified": False,
        "report_markdown": "## 01 研究范围与方法\n\n以下内容仅用于浏览器验收，不是真实研究结论。重点核对经营现金流与利润的期间、口径及数据来源。\n\n## 02 盈利与现金流\n\n经营现金流与利润需要结合收款节奏与收入确认进行交叉核对。当前资料不足以形成真实公司的判断。\n\n## 03 风险与证据缺口\n\n缺少完整披露材料，不生成收益预测与投资评级。",
        "artifacts": [{"artifact_id": "artifact_test", "claims": [{"claim_id": "claim_test", "kind": "interpretation", "text": "现金流判断需结合利润兑现与收款节奏（合成示例）。"}], "limitations": ["合成数据不支持真实投资判断。"]}]}
    write_json(directory / "report.json", report)
    write_json(directory / "evidence_manifest.json", {"references": [{"claim": "artifact_test:claim_test", "source": "合成年报", "source_url": "https://example.com/report.pdf", "locator": "page:3", "result_id": observation.result_id, "snapshot_id": raw}]})
    (directory / "report.md").write_text(report["report_markdown"], encoding="utf-8")
    with research.connect() as database:
        database.change("research_runs", {"status":"completed", "content":"partial", "report":str(directory / "report.md")}, id=research_id)
    RuntimeStore(root / "v3").put("work", session["id"], {"id": "work_fixture", "kind": "research", "status": "completed", "resources": {
        "symbol": "600519.SH", "goal": "现金流质量分析（合成测试）", "research_id": research_id, "as_of": scope.as_of.isoformat()},
        "result": {"research_id": research_id, "content_status": "partial", "report_available": True}})
    return create_app(settings, host_factory=lambda settings, session_id: SessionHost(settings, session_id, model=SyntheticModel()))


if __name__ == "__main__":
    import unittest
    from mysql_support import isolated_database
    fixture = unittest.TestCase()
    isolated_database(fixture)
    try:
        with tempfile.TemporaryDirectory(prefix="finsight-browser-") as directory:
            uvicorn.run(application(Path(directory)), host="127.0.0.1", port=8001, proxy_headers=False, timeout_graceful_shutdown=5)
    finally:
        fixture.doCleanups()
