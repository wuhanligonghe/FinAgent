"""Default: freeze six tasks. --execute explicitly runs 24 model studies."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from finagent.config import load_settings
from finagent.research.evaluation21 import freeze_matrix, execute_matrix, load_matrix


def main():
    parser = argparse.ArgumentParser(description="v2.1 固定四组对照；默认只冻结，不调用模型")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--source-runs", nargs=2)
    parser.add_argument("--execute", action="store_true", help="执行24次付费模型研究；每次最多80个模型请求")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--token-limit", type=int, default=2000000, help="每个比较研究相同的保守token总额度")
    args = parser.parse_args()
    settings = load_settings()
    if args.inspect:
        print(json.dumps(load_matrix(args.pack), ensure_ascii=False, indent=2))
    elif args.execute:
        print(execute_matrix(settings, args.pack, token_limit=args.token_limit))
    else:
        if not args.source_runs:
            parser.error("冻结需要 --source-runs 公司一研究ID 公司二研究ID")
        m = freeze_matrix(settings, args.source_runs, args.pack)
        print(json.dumps({"input_hash": m["input_hash"], "planned_runs": m["planned_runs"], "executed_runs": 0}))


if __name__ == "__main__":
    main()
