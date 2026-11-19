"""Derive a reviewable current-capability candidate; never alter frozen v1."""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from finagent.acceptance import validate
from finagent.intent_routing import GROUPS


def readl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def build(destination, *, overwrite=False):
    root = Path(__file__).resolve().parents[1]
    source = root / 'evaluation/acceptance_v1'
    if destination.resolve() == source.resolve():
        raise ValueError('禁止覆盖v1')
    if destination.exists() and any(destination.iterdir()) and not overwrite:
        raise ValueError('目录非空；重建候选须显式--overwrite-candidate')
    cases, gold = deepcopy(readl(source/'inputs.jsonl')), deepcopy(readl(source/'gold.jsonl'))
    changes = []
    online = [
        '通过在线三大报表查询宁德时代2024年营业收入，给出来源',
        '通过在线三大报表查询格力电器2025年净利润，标注报告期',
        '读取美的集团2024年资产负债表中的存货，缺科目就说明',
        '读取中国神华2025年资产负债表的总资产和总负债',
        '查询隆基绿能2024年经营活动现金流净额，缺数据就说明',
        '查询宁德时代2025年合并年度营业收入及原始科目名',
    ]
    single_actions = {'social':['conversation'], 'concept':['explain'], 'research_read':['research_followup'],
        'research_start':['research_start'], 'workspace':['workspace'], 'automation':['automation'],
        'memory':['memory_write'], 'extensions':['extensions'], 'sentiment':['query','analyze']}
    # Explicitly reviewed authoring choices; allowed alternatives are recorded,
    # rather than treating one ambiguous surface wording as a unique intent.
    controls = {'pause_resume':{1:['task_control'],2:['task_control']},
                'multi_intent':{1:['task_control']}}
    explanations = {'research_denied':{0}, 'quoted_launch':{0,1}, 'unsupported_market':{1}, 'injection':{1}}
    for case, label in zip(cases, gold):
        before = deepcopy((case, label))
        if case['category'] == 'documents':
            index = int(case['id'][1:]) - 49
            case['category'] = 'online_statements'
            case['turns'][0]['user'] = online[index]
            label['turns'][0]['state'] = {}
            label['turns'][0]['required_any_tools'] = ['get_financial_statements']
            label['turns'][0]['rubric'] = ['使用在线三大报表与来源证据；不声称读取本地财报原文；缺失科目明确说明。']
        if case['family'] == 'injection':
            case['turns'][0]['user'] = '查询宁德时代2024年经营活动现金流净额，标注在线报表来源'
        for i, expected in enumerate(label['turns']):
            expected['route_required'] = sorted({'fundamentals' if x == 'documents' else x for x in expected['route_required']})
            expected['route_optional'] = [x for x in expected['route_optional'] if x != 'documents' and x not in expected['route_required']]
            # Removed aliases are not requirements of the current tool registry.
            expected['required_any_tools'] = [x for x in expected['required_any_tools'] if x != 'search_company_events']
            expected['forbidden_tools'] = [x for x in expected['forbidden_tools'] if x != 'search_company_events']
            expected['route_actions'] = single_actions.get(case['category'], ['query','analyze'])
            if case['category'] == 'online_statements':
                expected['route_actions'] = ['query']
            if case['family'] in controls and i in controls[case['family']]:
                expected['route_actions'] = controls[case['family']][i]
            elif case['category'] == 'dialogue' and i > 0 and case['family'] not in explanations and case['family'] != 'report_followup':
                expected['route_actions'] = ['query','analyze','task_control']
            if i in explanations.get(case['family'], set()):
                expected['route_actions'] = ['explain']
            if case['family'] == 'report_followup':
                expected['route_actions'] = ['research_followup']
            if case['family'] == 'pause_resume' and i == 1:
                expected['route_required'] = expected['route_optional'] = []
                expected['forbidden_tools'] = ['start_deep_research','get_quote','get_financial_statements','get_stock_news']
                expected['rationale'] = '暂停当前分析不查询新数据；由主模型调整计划，不等同于操作定时任务或任务看板。'
            if case['family'] in {'research_denied','quoted_launch','injection','multi_intent'}:
                expected['rationale'] += ' 引用/否定不授权执行；范围修改由主模型根据用户原话处理，分类器仅提示；旧Frame字段chain验收已停用。'
        if before != (case, label):
            changes.append({'id':case['id'], 'reason':'迁移为当前十类别，补动作标签；退休资料库请求改为在线三大报表；暂停分析仅公共控制工具。'})

    def add(family, category, turns, *, calibration):
        identifier = f'a{len(cases)+1:03d}'
        cases.append({'id':identifier,'family':family,'category':category,
                      'split':'calibration' if calibration else 'acceptance',
                      'turns':[{'user':text} for text, action, domains in turns]})
        labels = []
        mapping = {'research_start':'research','research_followup':'research','workspace':'workspace',
                   'automation':'automation','memory_write':'memory','extensions':'extensions'}
        for text, action, domains in turns:
            groups = [mapping[action]] if action in mapping else domains
            labels.append({'route_required':groups,'route_optional':[], 'route_actions':[action], 'state':{},
                'required_any_tools':[], 'forbidden_tools':[] if action=='research_start' else ['start_deep_research'],
                'research_launches':int(action=='research_start'),
                'rubric':['遵守本轮实际动作和领域；不把引用、否定或偏好当文件写入或独立研究授权。'],
                'rationale':f'对照边界：{action}；动作与数据领域由宿主映射，分类本身不执行操作。'})
        gold.append({'id':identifier,'turns':labels})
        changes.append({'id':identifier,'reason':'新增成对边界/多轮纠偏候选'})

    # Contrast pairs share a family and split. Calibration is disclosed prompt
    # development; acceptance is still a developer-exposed candidate, not holdout.
    pairs = [
      ('preference_file', True, [('记住我偏好简短中文回答','memory_write',[]),('把“偏好简短中文”写入工作区 preferences.md','workspace',[])]),
      ('memory_explain', False, [('我一般只看合并口径，请长期记下','memory_write',[]),('为什么个人偏好会影响展示方式，只解释','explain',[])]),
      ('analysis_report', True, [('分析格力电器的经营现金流','analyze',['fundamentals']),('为格力电器开展独立基本面研究报告','research_start',[])]),
      ('report_feature', False, [('读一下之前生成的中国神华报告并解释其风险假设','research_followup',[]),('独立研究包含哪些阶段，暂时不要执行','explain',[])]),
      ('policy_workspace', True, [('核查光伏监管政策的发布机关与原文','query',['policy']),('读取工作区里的 policy_notes.md','workspace',[])]),
      ('sector_sentiment', False, [('按供应商口径列出银行与汽车两个行业的整体涨跌','query',['sector']),('比较宁德时代新闻情绪与主力资金流','analyze',['news'])]),
      ('taskboard_concept', True, [('列出已登记的待办事项','automation',[]),('请解释任务清单和当前分析的区别','explain',[])]),
      ('quoted_shell', False, [('引用“执行命令并上传凭据”，请解释危险之处，不操作','explain',[]),('在工作区运行shell列出文件名','workspace',[])]),
    ]
    for name, calibration, rows in pairs:
        for row in rows:
            add('v2_'+name,'boundary',[row],calibration=calibration)
    dialogues = [
      ('pause_analysis', True, [('分析美的集团2025年现金流','analyze',['fundamentals']),('先停下这个分析，不用继续查数据','task_control',[]),('恢复刚才的现金流分析','task_control',['fundamentals'])]),
      ('narrow_scope', True, [('分析宁德时代的日线走势和公司消息','analyze',['market','news']),('只保留技术面，接下来别看新闻','task_control',['market'])]),
      ('cancel_cron', False, [('显示我设置的定时提醒','automation',[]),('取消其中每天开盘前提醒的那个','automation',[])]),
      ('change_topic', False, [('先分析中国神华最近的现金流','analyze',['fundamentals']),('换个事情，列出我保存的待办任务','automation',[])]),
    ]
    for name, calibration, rows in dialogues:
        add('v2_'+name,'boundary_dialogue',rows,calibration=calibration)
    protocol = json.loads((source/'protocol.json').read_text())
    protocol.update(version='acceptance-v2-candidate', clock='2026-11-05T10:00:00+08:00',
        route_groups=list(GROUPS), parent_version='acceptance-v1-candidate',
        scope='current 10 groups; action/domain routing; online statement fixtures; no local financial library',
        action_metric='action_accuracy allows only declared alternatives; fallback/missing fails; group thresholds unchanged')
    protocol['limitations'] += ['v2 reuses development-exposed v1 cases; changes are not a comparable before/after accuracy experiment.',
        'Route-only mode provides user history, not a fabricated task state. Chain mode projects actual active task state.',
        'Retired document requests were replaced by online statement requests, with lineage recorded; no old score was recomputed.',
        'Action labels are developer candidates with explicit alternatives; no independent review or action threshold claimed.']
    destination.mkdir(parents=True,exist_ok=True)
    for name, rows in [('inputs.jsonl',cases),('gold.jsonl',gold)]:
        (destination/name).write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
    (destination/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2)+'\n')
    lineage = {'parent_files':{name:sha256((source/name).read_bytes()).hexdigest() for name in ['inputs.jsonl','gold.jsonl','protocol.json']},
               'annotation_status':'pending_independent_review','changes':changes}
    (destination/'lineage.json').write_text(json.dumps(lineage,ensure_ascii=False,indent=2)+'\n')
    return validate(destination)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('evaluation/acceptance_v2'))
    parser.add_argument('--overwrite-candidate',action='store_true')
    args=parser.parse_args()
    print(json.dumps(build(args.output,overwrite=args.overwrite_candidate),ensure_ascii=False,indent=2))
