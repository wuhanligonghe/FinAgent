"""Prepare, verify, run and adjudicate the independently reviewable suite."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate","prepare","verify","route","chain","adjudicate"))
    parser.add_argument("--source",type=Path,default=Path("evaluation/acceptance_v2"))
    parser.add_argument("--bundle",type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--config",default="finagent.toml")
    parser.add_argument("--split",choices=("calibration","acceptance"),default="calibration")
    parser.add_argument("--candidate",action="store_true",help="未审核候选诊断，不能算独立验收")
    parser.add_argument("--review",type=Path)
    parser.add_argument("--case",action="append",dest="cases")
    parser.add_argument("--max-model-calls",type=int,default=100)
    args=parser.parse_args()
    if args.bundle and args.action not in {"prepare","validate"}:
        sys.path.insert(0,str(args.bundle.resolve()/"implementation"))
    from finagent import acceptance as a
    from finagent.config import load_settings
    try:
        if args.action=="validate": result=a.validate(args.source)
        elif args.action=="prepare":
            if not args.output: parser.error("prepare需要output")
            result=a.prepare(args.source,args.output,load_settings(args.config))
        elif args.action=="verify":
            if not args.bundle: parser.error("需要bundle")
            manifest=a.verify(args.bundle)
            result={"verified":True,"coverage":manifest["coverage"]}
        elif args.action=="adjudicate":
            if not args.output or not args.bundle: parser.error("需要bundle和output运行目录")
            result=a.adjudicate(args.bundle,args.output,args.review)
        else:
            if not args.output or not args.bundle: parser.error("需要bundle和output")
            result=a.run(args.bundle,args.output,load_settings(args.config),mode=args.action,
                split=args.split,candidate=args.candidate,review_path=args.review,
                case_ids=args.cases,max_calls=args.max_model_calls)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,OSError) as exc:
        print(str(exc),file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
