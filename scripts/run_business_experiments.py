"""Prepare a frozen experiment or run budgeted real-model comparisons."""
import argparse
from datetime import datetime
from pathlib import Path

from finagent.config import load_settings
from finagent.experiments.runner import ARMS, analyze, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run", "analyze"], nargs="?", default="prepare")
    parser.add_argument("--input", type=Path, help="analyze读取原实验目录，不再请求模型")
    parser.add_argument("--config", default="finagent.toml")
    parser.add_argument("--split", choices=["development", "holdout"], default="development")
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--max-model-calls", type=int, default=72)
    parser.add_argument("--soft-token-limit", type=int, default=500000)
    parser.add_argument("--review-file", type=Path)
    parser.add_argument("--retrieval", choices=["keyword", "bm25_expansion"], default="keyword")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(".finagent/business_experiments") / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    try:
        if args.command == "analyze":
            if not args.input:
                parser.error("analyze需要--input")
            report = analyze(args.input, output)
        else:
            report = run(load_settings(args.config), output, live=args.command == "run", split=args.split, case_ids=args.cases,
                     arms=args.arms, repeats=args.repeats, seed=args.seed, max_calls=args.max_model_calls,
                     soft_token_limit=args.soft_token_limit, review_file=args.review_file, retrieval_strategy=args.retrieval)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"命令 {args.command}，原实验模式 {report['mode']}，完成 {report['completed_jobs']}/{report['planned_jobs']}，新增真实深度研究 0")
    print(output.resolve() / "REPORT.md")
    return 1 if report["stopped_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
