"""Real executor + SYNTHETIC fixtures. No network, credentials or model charges."""
import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / "src"), str(root)]
from finagent.config import Settings
from finagent.research.coordinator import ResearchCoordinator
from finagent.research.decision_contracts import DecisionRequest
from finagent.runtime import now
from tests.research21_support import native_factory


def main():
    parser = argparse.ArgumentParser(description="v2.1 真实执行器的合成模型／数据演示，不是金融验收")
    parser.add_argument("--limited", action="store_true")
    args = parser.parse_args()
    target = root / ".finagent" / "v21_demo" / uuid4().hex
    settings = Settings(data_dir=target)
    state = ResearchCoordinator(settings, runner_factory=native_factory(settings, {"policy"} if args.limited else ())).run(
        DecisionRequest(goal="SYNTHETIC：合成流程演示，非真实行情或投资建议", symbol="600519.SH", as_of=now()), progress=lambda s: print(s, file=sys.stderr))
    print(json.dumps({"synthetic": True, "network_calls": 0, "report": state["report"], "research_id": state["id"],
                      "content_status": state["content"], "model_attempts_scripted": state["budget"]["calls"].get("model", 0)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
