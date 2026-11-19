"""Freeze task packs or explicitly execute 18 paid model ablation runs."""
import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from finagent.config import load_settings
from finagent.data.evidence import write_json
from finagent.research.evaluation import freeze_packs, FrozenPack, run_variant, summarize_runs, VARIANTS


def main():
    parser = argparse.ArgumentParser(description="默认仅冻结输入；--execute执行18个真实模型对照研究，会消耗API配额")
    parser.add_argument("--source-run")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    if not args.execute:
        if not args.source_run:
            parser.error("冻结需要--source-run")
        manifest = freeze_packs(settings, args.source_run, args.pack)
        print(json.dumps({"packs": len(manifest["packs"]), "input_hash": manifest["input_hash"], "executed": False}))
        return
    pack, records = FrozenPack(args.pack), []
    for case in pack.manifest["packs"]:
        for variant in VARIANTS:
            target = settings.data_dir / "phase2_evaluation" / (case["name"] + "_" + variant + "_" + uuid4().hex)
            pack.prepare(target)
            scoped = settings.model_copy(update={"data_dir": target})
            records.append((variant, run_variant(scoped, pack, case, variant)))
            write_json(settings.data_dir / "phase2_evaluation" / "comparison.json", {"input_hash": pack.manifest["input_hash"], **summarize_runs(records)})
    print(json.dumps({"runs": len(records), "quality_status": "pending_human_review"}))


if __name__ == "__main__":
    main()
