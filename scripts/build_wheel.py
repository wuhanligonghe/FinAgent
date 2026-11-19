"""Build from a fresh source staging directory, independent of stale build/lib.

Copied workspaces can have generated files newer than their actual sources.
Never let setuptools' timestamp-based incremental copy select that old code.
"""
from pathlib import Path
import argparse
import shutil
import subprocess
import sys
from uuid import uuid4


def main():
    project = Path(__file__).resolve().parent.parent
    stage = project / ".finagent" / ("package_build_" + uuid4().hex)
    stage.mkdir(parents=True)
    for name in ("pyproject.toml", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES"):
        shutil.copyfile(project / name, stage / name)
    shutil.copytree(project / "src/finagent", stage / "src/finagent", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=project / ".finagent/dist/p0")
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", str(stage), "--no-deps", "--no-build-isolation",
                    "--wheel-dir", str(output)], check=True, cwd=stage)
    print("Fresh source staging: " + str(stage))


if __name__ == "__main__":
    main()
