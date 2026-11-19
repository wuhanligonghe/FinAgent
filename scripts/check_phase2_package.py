"""Inspect and smoke-test an extracted wheel without a global installation."""
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import zipfile


def main():
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, help="明确验证新版本 wheel，避免覆盖旧构建")
    parser.add_argument("--output", type=Path, help="保存本次安装包验证报告的新位置")
    args = parser.parse_args()
    wheel = args.wheel.resolve() if args.wheel else max((project / ".finagent/dist/p0").glob("finagent-*.whl"), key=lambda p: p.stat().st_mtime)
    target = project / ".finagent" / ("phase2_install_check_" + uuid4().hex)
    with zipfile.ZipFile(wheel) as package:
        names = package.namelist()
        skills = [n for n in names if n.startswith("finagent/skills/") and n.endswith("SKILL.md")]
        if len(skills) != 14:
            raise RuntimeError("Five analyst and nine decision/quality skills are required")
        if any(name not in names for name in ("finagent/nlu/parser.py", "finagent/nlu/qwen3.py", "finagent/resources/nlu_sft2_schema.json", "finagent/resources/nlu_sft2_system.txt")):
            raise RuntimeError("Qwen3 host adapter or its prompt/schema is missing from the wheel")
        if "finagent/nlu/macbert.py" in names:
            raise RuntimeError("Retired encoder adapter must not be in the production wheel")
        metadata = package.read(next(n for n in names if n.endswith('.dist-info/METADATA'))).decode()
        if any(line.startswith('Requires-Dist: ' + dependency) for line in metadata.splitlines() for dependency in ('torch', 'transformers', 'peft', 'finagent-macbert')):
            raise RuntimeError("Training/encoder dependencies leaked into the runtime wheel")
        if "finagent/research/selection.py" not in names or "finagent/research/menu.py" not in names:
            raise RuntimeError("Explicit research selection is missing from the wheel")
        for name in names:
            if not (target / name).resolve().is_relative_to(target.resolve()):
                raise RuntimeError("Unsafe package entry")
        package.extractall(target)
    code = '''
import json,finagent
import sys
from pathlib import Path
from finagent.analysts.catalog import CATALOG,prompt
from finagent.analysts.decision import prompt as decision_prompt
from finagent.research.workflow import ROLE_TITLES
from finagent.config import Settings
from finagent.data.service import DataService
from finagent.tools.perception import build_registry
s=DataService(Settings(data_dir=Path("smoke-data")))
assert Path(finagent.__file__).resolve().is_relative_to(Path.cwd().resolve())
assert "torch" not in sys.modules and "transformers" not in sys.modules
try:
 r=build_registry(s)
 print(json.dumps({"module":finagent.__file__,"skills":list(CATALOG)+list(ROLE_TITLES),"skill_chars":{**{k:len(prompt(k)) for k in CATALOG},**{k:len(decision_prompt(k)) for k in ROLE_TITLES}},"tools":len(r.tools),"research_entry":"start_stock_research" in r.tools}))
finally:s.close()
'''
    environment = {**os.environ, "PYTHONPATH": str(target), "PYTHONUTF8": "1"}
    result = subprocess.run([sys.executable, "-c", code], cwd=target, env=environment, capture_output=True, text=True, encoding="utf-8", check=True)
    help_result = subprocess.run([sys.executable, "-m", "finagent", "--help"], cwd=target, env=environment, capture_output=True, text=True, encoding="utf-8", check=True)
    if not all(name in help_result.stdout for name in ("nlu-check", "nlu-parse", "nlu-benchmark", "nlu-evaluate")):
        raise RuntimeError("NLU CLI help is incomplete")
    body = {"wheel": str(wheel), "sha256": sha256(wheel.read_bytes()).hexdigest(), "smoke": json.loads(result.stdout),
            "research_help": "research-resume" in help_result.stdout, "nlu_help": True, "nlu_modules": True, "network_calls": 0}
    output = args.output or project / (".finagent/v22_validation/package_check.json" if args.wheel else ".finagent/v21_validation/package_check.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), **body["smoke"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
