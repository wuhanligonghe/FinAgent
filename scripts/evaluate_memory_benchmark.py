"""Validate memory-v1 data or score supplied predictions and answer reviews offline."""
import argparse
import json
from pathlib import Path

from finagent.memory_benchmark import score, validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "score"))
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/memory_v1"))
    parser.add_argument("--split", choices=("development", "holdout"), default="development")
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "validate":
            result = validate(args.dataset)
        else:
            if not args.predictions:
                parser.error("score需要真实预测文件--predictions；不会用金标伪造模型输出")
            result = score(args.dataset, args.predictions, split=args.split, review_path=args.review)
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
