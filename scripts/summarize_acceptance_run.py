"""Merge disjoint candidate shards without converting them to acceptance."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from finagent.acceptance import digest, jsonl, read, route_metrics, write


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--route',type=Path,required=True)
    parser.add_argument('--chain',type=Path,action='append',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('output must be empty')
    cases=[c for c in jsonl(args.bundle/'inputs.jsonl') if c['split']=='acceptance']
    gold={g['id']:g for g in jsonl(args.bundle/'gold.jsonl')}
    expected_keys=[(c['id'],i) for c in cases for i in range(len(c['turns']))]
    rows=[]
    calls=0
    usage=Counter()
    shard_ids=set()
    for folder in args.chain:
        report=read(folder/'report.json')
        if report['bundle_sha256']!=digest(args.bundle/'manifest.json') or not report['candidate'] or report['mode']!='chain':
            raise ValueError('incompatible shard')
        if digest(folder/'predictions.jsonl')!=report['predictions_sha256']:
            raise ValueError('prediction digest mismatch')
        if shard_ids & set(report['selected_cases']):
            raise ValueError('overlapping shards')
        shard_ids.update(report['selected_cases'])
        rows.extend(jsonl(folder/'predictions.jsonl'))
        calls+=report['model_calls']
        usage.update(report['usage'])
    actual={(r['id'],r['turn']):r for r in rows}
    if len(actual)!=len(rows) or set(actual)!=set(expected_keys):
        raise ValueError('incomplete or duplicate chain predictions')
    categories=defaultdict(lambda:Counter(planned=0,passed=0))
    fields=defaultdict(Counter)
    failures=[]
    successful_dialogues=0
    for case in cases:
        turns=[actual[(case['id'],i)] for i in range(len(case['turns']))]
        successful_dialogues+=int(all(r['automatic_pass'] for r in turns))
        for r in turns:
            categories[case['category']]['planned']+=1
            categories[case['category']]['passed']+=int(r['automatic_pass'])
            for check in r['checks']:
                fields[check['field']]['total']+=1
                fields[check['field']]['passed']+=int(check['passed'])
            if not r['automatic_pass']:
                failures.append({'id':r['id'],'turn':r['turn'],'user':r['user'],'status':r.get('status'),
                    'reason':r.get('reason'),'failed_checks':[c for c in r['checks'] if not c['passed']]})
    route_report=read(args.route/'report.json')
    route_rows=jsonl(args.route/'predictions.jsonl')
    if route_report['bundle_sha256']!=digest(args.bundle/'manifest.json') or digest(args.route/'predictions.jsonl')!=route_report['predictions_sha256']:
        raise ValueError('route evidence mismatch')
    route_index={(r['id'],r['turn']):r for r in route_rows}
    if set(route_index)!=set(expected_keys):
        raise ValueError('incomplete routing results')
    expected=[gold[c]['turns'][i] for c,i in expected_keys]
    routes=route_metrics([route_index[key]['route'] for key in expected_keys],expected)
    critical=[{'id':r['id'],'turn':r['turn'],'check':check} for r in rows for check in r['checks']
              if check['field'] in {'forbidden_tools','research_launches'} and not check['passed']]
    summary={'candidate':True,'independent_acceptance_passed':False,'bundle_sha256':digest(args.bundle/'manifest.json'),
        'dialogues':len(cases),'turns':len(rows),'route':routes,
        'chain_automatic_turn_success':sum(r['automatic_pass'] for r in rows)/len(rows),
        'chain_automatic_dialogue_success':successful_dialogues/len(cases),
        'chain_dialogues_passed':successful_dialogues,'chain_turns_passed':sum(r['automatic_pass'] for r in rows),
        'runtime_status':dict(Counter(r.get('status') for r in rows)),
        'chain_categories':dict(categories),'chain_field_checks':dict(fields),'critical_violations':critical,
        'chain_model_calls':calls,'chain_token_usage':dict(usage),'failures':failures,
        'answer_review_status':'pending','financial_data':'frozen_synthetic','per_turn_model_limit':10,
        'evidence':{'route':str(args.route),'shards':[str(p) for p in args.chain]}}
    args.output.mkdir(parents=True,exist_ok=True)
    write(args.output/'summary.json',summary)
    lines=['# 候选集评测结果','',f"98组118轮；分类集合匹配率 {routes['exact_match']:.2%}，Macro-F1 {routes['macro_f1_supported']:.2%}。",'',
           f"全链路自动检查：{summary['chain_turns_passed']}/{len(rows)}轮，{successful_dialogues}/{len(cases)}组对话。",'',
           '这些是未独立审核的候选标签、冻结合成金融数据、每轮10次模型调用下的结果。自然语言答案未独立人工验收，不能将自动检查通过率视为金融回答正确率。','',
           '| 类别 | Precision | Recall | F1 |','|---|---:|---:|---:|']
    for group,m in routes['per_class'].items():lines.append(f"| {group} | {m['precision']:.2%} | {m['recall']:.2%} | {m['f1']:.2%} |")
    lines+=['','## 全链路失败','']
    for f in failures:lines.append(f"- {f['id']} 第{f['turn']+1}轮：{f['user']}；{f['reason'] or ', '.join(c['field'] for c in f['failed_checks'])}")
    (args.output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k not in {'failures','critical_violations','chain_field_checks','chain_categories','route'}},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
