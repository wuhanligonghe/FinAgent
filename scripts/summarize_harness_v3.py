"""Summarize execution/cost evidence without treating completed as correctness."""
import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs",nargs="+",type=Path,required=True)
    parser.add_argument("--output",type=Path,default=Path(".finagent/v3_acceptance/model_comparison_summary.json"))
    args=parser.parse_args()
    cases=json.loads((Path(__file__).resolve().parent.parent/"doc/harness_v3_cases.json").read_text())["cases"]
    selected={};all_rows=[]
    for path in args.inputs:
        for line in path.read_text(encoding="utf-8").splitlines():
            row=json.loads(line);row["source_file"]=str(path);all_rows.append(row)
            if row.get("mode") and row.get("case_id") and isinstance(row.get("repeat"),int):
                selected[row["mode"],row["case_id"],row["repeat"]]=row
    modes={}
    missing=[]
    for mode in ["baseline","new","new_hint"]:
        for case in cases:
            for repeat in range(3):
                if (mode,case["id"],repeat) not in selected:missing.append([mode,case["id"],repeat])
        rows=[r for (m,_,_),r in selected.items() if m==mode]
        successful=sum(bool(r.get("statuses")) and all(s in {"completed","partial"} for s in r["statuses"]) for r in rows)
        modes[mode]={"samples":len(rows),"cases_with_normal_runtime_responses":successful,
            "no_recorded_main_response":sum(not r.get("statuses") for r in rows),
            "cases_with_failed_or_cancelled_response":sum(any(s in {"failed","cancelled"} for s in r.get("statuses",[])) for r in rows),
            "clarification_responses":sum(r.get("statuses",[]).count("needs_clarification")+r.get("diagnostics",[]).count("clarification_requested") for r in rows),
            "mean_seconds":round(mean([r["seconds"] for r in rows if "seconds" in r]),3) if any("seconds" in r for r in rows) else None,
            "remote_model_calls":sum(r.get("model_calls",0) for r in rows),"nlu_attempts":sum(r.get("nlu_attempts",0) for r in rows),
            "remote_prompt_tokens":sum(r.get("usage",{}).get("prompt_tokens",0) for r in rows),
            "remote_completion_tokens":sum(r.get("usage",{}).get("completion_tokens",0) for r in rows),
            "implementation_hashes":dict(Counter(r.get("implementation_sha256","unknown") for r in rows)),
            "implementation_changed_during_sample":[[r["case_id"],r["repeat"]] for r in rows if r.get("implementation_stable") is False],
            "critical_error_rate":None,"semantic_completion_rate":None,"human_review":"pending"}
    summary={"schema_version":"finagent.v3.comparison_summary.1","retained_rows":len(all_rows),"selected_samples":len(selected),
             "missing_samples":missing,"modes":modes,"quality_decision":"pending_human_review",
             "selection":"Later input files replace the same mode/case/repeat for corrected fixtures; every original row remains in its source file.",
             "limits":"Execution statuses are not semantic correctness. Synthetic sources are not real market observations. Sequential configuration batches and implementation hashes must be considered before causal claims."}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({"selected_samples":len(selected),"missing_samples":len(missing),"quality_decision":summary["quality_decision"]},ensure_ascii=False))

if __name__=="__main__":main()
