"""Persist the complete offline regression result, including failure counts."""
import io
import json
from time import perf_counter
import unittest

from finagent import __version__
from finagent.config import load_settings
from finagent.data.evidence import write_json
from finagent.provenance import package_digest
from finagent.runtime import now


def main():
    started = perf_counter()
    output = io.StringIO()
    suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
    result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)
    settings = load_settings()
    report = {"mode": "offline_development_regression", "checked_at": now().isoformat(), "version": __version__,
              "implementation_sha256": package_digest(), "tests_run": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
              "passed": result.wasSuccessful(), "elapsed_seconds": round(perf_counter()-started, 3),
              "notice": "包含合成HTTP和替身模型；不能替代真实模型评估或冻结留出集。"}
    write_json(settings.data_dir / "checks/p0_offline_tests.json", report)
    (settings.data_dir / "checks/p0_offline_tests.txt").write_text(output.getvalue(), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() else 2


if __name__ == "__main__":
    raise SystemExit(main())
