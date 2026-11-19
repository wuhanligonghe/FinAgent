"""Check the Windows launcher from the user's package subdirectory, offline."""
import json
import os
from pathlib import Path
import subprocess
import sys

from finagent.config import load_settings, find_config
from finagent.data.evidence import write_json
from finagent.runtime import now


def main():
    project = Path(__file__).resolve().parent.parent
    working = project / "src/finagent"
    launcher = project / ".venv/Scripts/finagent.exe"
    original = Path.cwd()
    try:
        os.chdir(working)
        settings = load_settings()
        selected = find_config()
        checks = []
        for arguments in ([], ["chat"]):
            process = subprocess.run([str(launcher), *arguments], input="/exit\n", text=True, encoding="utf-8",
                                     capture_output=True, cwd=working, timeout=20)
            checks.append({"arguments": arguments, "exit_code": process.returncode,
                           "entered_chat": "FinSight 终端交互已启动" in process.stdout,
                           "exited_chat": "已退出 FinSight" in process.stdout,
                           "stdout": process.stdout, "stderr": process.stderr})
        report = {"checked_at": now().isoformat(), "python": sys.executable, "python_version": sys.version.split()[0],
                  "working_directory": str(working), "launcher": str(launcher), "configuration": str(selected),
                  "model_key_configured": bool(settings.model.api_key.get_secret_value()), "data_dir": str(settings.data_dir),
                  "checks": checks, "network_or_model_calls": 0}
        report["passed"] = selected == project / "finagent.toml" and all(
            item["exit_code"] == 0 and item["entered_chat"] and item["exited_chat"] for item in checks)
        write_json(project / ".finagent/checks/startup_repair.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 2
    finally:
        os.chdir(original)


if __name__ == "__main__":
    raise SystemExit(main())
