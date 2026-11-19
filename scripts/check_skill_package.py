"""Verify the nine skills and internal research methods in an isolated wheel."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


SMOKE = r'''
import json
from pathlib import Path
import sys
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import finagent
from finagent.analysts.methods import ANALYSIS_SKILLS, CHAT_SKILLS
from finagent.analysts.decision import prompt
from finagent.config import Settings, HarnessSettings
from finagent.harness.host import SessionHost
from finagent.harness.kernel import Ledger
from finagent.models import ToolCall
from finagent.research.role_prompts import ROLE_PROMPTS, role_method
from finagent.skill_library import SkillLibrary
assert Path(finagent.__file__).resolve().is_relative_to(root)
library = SkillLibrary()
assert len(library.skills) == 9 and set(library.skills) == set(CHAT_SKILLS)
assert len(ANALYSIS_SKILLS) == 8 and 'deep-research' in library.skills
assert len(ROLE_PROMPTS) == 11
for role in ROLE_PROMPTS:
    assert role_method(role)
    if role not in ('review', 'synthesis'):
        assert role_method(role) in prompt(role)
settings = Settings(data_dir=root / 'smoke-data',
    harness=HarnessSettings(workspace=root, memory_enabled=False, auto_memory=False))
host = SessionHost(settings, 'skill-package-check')
try:
    assert set(host.finance.skill_library.skills) == set(CHAT_SKILLS)
    required = {'start_deep_research', 'list_research', 'research_status', 'read_research_report'}
    assert required <= host.pool.tools.keys()
    result = host.pool.execute(ToolCall('guide', 'read_skill', json.dumps({'name': 'deep-research'})),
        host.ctx, Ledger(settings.limits))
    assert result['status'] == 'ok' and result['data']['instructions']
    assert host.ctx.source_requests == 0
    print(json.dumps({'isolated_import': True, 'skills': sorted(library.skills),
        'internal_roles': sorted(ROLE_PROMPTS), 'deep_research_tools': sorted(required),
        'guide_loaded': True, 'source_requests': host.ctx.source_requests}))
finally:
    host.close()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    project = Path(__file__).resolve().parents[1]
    temporary_root = project / ".finagent"
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="skill_package_check_", dir=temporary_root) as folder:
        target = Path(folder).resolve()
        with zipfile.ZipFile(wheel) as package:
            names = package.namelist()
            skills = [n for n in names if n.startswith("finagent/skills/") and n.endswith("/SKILL.md")]
            methods = [n for n in names if n.startswith("finagent/research/prompts/") and n.endswith(".md")]
            references = [n for n in names if n.startswith("finagent/skills/") and "/references/" in n and n.endswith(".md")]
            if (len(skills), len(methods), len(references)) != (9, 11, 3):
                raise RuntimeError("Wheel must contain 9 skills, 11 internal role methods and 3 references")
            for name in names:
                if not (target / name).resolve().is_relative_to(target):
                    raise RuntimeError("Unsafe wheel entry")
            package.extractall(target)
        result = subprocess.run([sys.executable, "-I", "-c", SMOKE, str(target)],
                                cwd=target, capture_output=True, text=True, check=True)
        report = {"wheel": str(wheel), "sha256": sha256(wheel.read_bytes()).hexdigest(),
                  "skill_count": len(skills), "role_method_count": len(methods),
                  "reference_count": len(references), "smoke": json.loads(result.stdout)}
    output = args.output or wheel.parent / "skill_package_check.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output.resolve()), **report}, ensure_ascii=False))


if __name__ == "__main__":
    main()
