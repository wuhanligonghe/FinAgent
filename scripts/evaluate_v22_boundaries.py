"""Run offline authority and execution replay tests; never calls research APIs."""
import argparse
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',default='.finagent/v22_validation/mode_boundaries.json')
    a=p.parse_args()
    suite=unittest.defaultTestLoader.loadTestsFromName('tests.test_research_v22')
    log=io.StringIO()
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    from finagent.provenance import package_digest
    from finagent.data.evidence import write_json
    output=Path(a.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.with_suffix('.log').write_text(log.getvalue(),encoding='utf-8')
    body={'schema_version':'execution22.boundaries.1','implementation':package_digest(),
          'cases':result.testsRun,'errors':len(result.errors)+len(result.failures),
          'skipped':len(result.skipped),'passed':result.wasSuccessful() and not result.skipped and result.testsRun>=14,
          'research_api_calls':0,'evidence':'Synthetic local executor replay; not real financial/model acceptance'}
    write_json(output,body);print(json.dumps(body,ensure_ascii=False))
    return 0 if body['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
