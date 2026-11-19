"""Build the explicitly synthetic, developer-authored memory-v1 candidate."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import random

from finagent import memory_benchmark as benchmark


PROFILES = [
    dict(key="spruce", split="development", name="云杉财研", unit="亿元", old_unit="万元", override="元",
         style="先给结论，再分点说明依据", stack="Python 和 FastAPI", foreign_stack="Java 和 Spring Boot",
         focus="经营现金流与应收回款", theme="现金流质量", color="蓝色"),
    dict(key="reed", split="development", name="青禾研究", unit="万元", old_unit="元", override="百万元",
         style="先列风险，再给简短结论", stack="Java 和 Spring Boot", foreign_stack="Go 和 Gin",
         focus="负债到期结构与偿债压力", theme="债务结构", color="绿色"),
    dict(key="bay", split="holdout", name="海岚分析", unit="百万元", old_unit="亿元", override="万元",
         style="先给一行摘要，再列出核查清单", stack="Go 和 Gin", foreign_stack="TypeScript 和 NestJS",
         focus="毛利率稳定性与存货周转", theme="存货管理", color="橙色"),
    dict(key="bridge", split="holdout", name="星桥研报", unit="元", old_unit="百万元", override="亿元",
         style="先给建议的核查顺序，再解释理由", stack="TypeScript 和 NestJS", foreign_stack="Python 和 FastAPI",
         focus="资本开支与经营现金流的匹配", theme="资本开支纪律", color="紫色"),
]

QUERIES = {
    "semantic_recall": ["在云杉财研里，我看一家公司的基本面时通常先关注哪些方面？",
        "筛选企业时，我以往习惯优先排查什么类型的问题？", "做经营质量检查时，哪些指标符合我平时的研究顺序？",
        "请沿用我一贯的方法，列出检查企业扩张是否稳健的优先项。"],
    "multi_memory": ["给这次内部汇报列一个符合我习惯的答复提纲，并说明金额展示方式。",
        "整理一份适合我和项目读者的研究摘要格式，金额该怎样展示？", "跨会话接着做：给项目周报安排结构、读者说明和金额单位。",
        "按已保存的偏好和项目定位，说明研究稿的组织顺序与金额口径。"],
    "correction": ["我的默认金额展示单位后来改过，现在应按哪个？", "你保留的两个单位偏好里，哪一个才是我现在的默认设置？",
        "请按我最近的更正，告诉我今后金额用什么单位。", "重新开了会话，请确认当前有效的金额展示习惯。"],
    "temporal": ["按今天的日期，这个月项目材料应该用哪套金额单位？", "临时演示期已经结束了吗？现在的金额格式按什么约定？",
        "截至当前测试时点，项目金额展示应采用试行规则还是恢复后的规则？", "请按保存约定的生效日期，确定本周稿件的金额单位。"],
    "current_override": ["这次只用{override}展示金额，不沿用默认单位。请复述本次要求。",
        "默认偏好暂时保留，但这一份材料请改成{override}。现在这份用什么？",
        "仅本轮改用{override}，其他会话不变。请说明本轮金额展示方式。", "这张演示稿金额都按{override}，不用长期默认设置。请确认。"],
    "user_isolation": ["你记得我默认使用的金额单位吗？不清楚就直接问我。", "请确认我的金额展示偏好，不要套用其他账号的设置。",
        "这个账号以前有约定金额单位吗？没有记录就说明。", "我换了账号登录，你能确认当前账号自己的金额单位习惯吗？"],
    "project_isolation": ["这个项目的服务接口应沿用哪套技术栈？", "请只按当前项目的约定，说明后端用什么语言和框架。",
        "我们在当前工作区新建接口，应遵循哪份已有技术选择？", "同一账号有好几个项目，现在这个项目的后端方案是什么？"],
    "deleted_memory": ["我让你删除过图表颜色偏好。现在还保存着我的默认颜色吗？",
        "关于图表配色的那条记录已要求忘记，请确认现在是否还有明确偏好。",
        "重新开会话后，你还知道被我删除的默认图表颜色吗？", "请不要从旧摘要恢复配色记录；现在能确认我的颜色偏好吗？"],
    "abstention": ["你记得我给报告封面选过什么底色吗？", "我以前是否明确指定了研究报告封面的颜色？",
        "能从已保存的信息确认封面配色吗？没有就不要猜。", "请告诉我报告封面的颜色偏好；不能用其他视觉习惯代替。"],
    "core_residency": ["在新项目里，用我一直偏好的顺序简要解释什么是经营现金流。",
        "切到另一个工作区了，请沿用全局答复习惯解释现金流和利润的区别。",
        "这个新会话也请按我的固定表达偏好说明应收账款是什么。", "请用跨项目通用的答复顺序，简短解释资本开支。"],
    "historical_question": ["刚开始和最近，我分别偏好用什么金额单位？请分清过去与现在。",
        "回顾一下我的金额单位设置变化：早期是什么，目前又是什么？", "请比较两次保存的单位约定，按时间先后说明。",
        "这次问历史变化，不是只问当前值：金额展示偏好怎样改过？"],
    "source_priority": ["我长期研究的主题是什么？请区分我亲口说过的内容和助手猜测。",
        "有一条推测笔记与我的明确偏好不一致，长期主题应按哪条？", "你应依据我自己确认的哪项研究兴趣继续协助？",
        "请回忆用户确认过的研究方向，不要把助手的一次推测当作我的偏好。"],
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def build():
    inputs, memories, labels = [], [], []
    for profile_index, profile in enumerate(PROFILES):
        for category_index, category in enumerate(benchmark.CATEGORIES):
            case_id = f"mv1_{profile_index * len(benchmark.CATEGORIES) + category_index + 1:03d}"
            user_id, project_id = "eval_" + profile["key"], "project_" + profile["key"]
            scope = {"user_id": user_id, "project_id": project_id, "as_of": "2026-11-05T10:00:00+08:00"}
            records, grades, requirements, rules, events = [], {}, [], [], []

            def add(slot, description, body, *, grade=0, kind="user", core=False, user=None,
                    project="global", created="2026-10-01T09:00:00+08:00", role="user", source_text=None):
                mid = "mem_" + sha256(f"{case_id}:{slot}".encode()).hexdigest()[:32]
                source_id = "user_" + sha256(f"{case_id}:{slot}:source".encode()).hexdigest()[:32]
                pid = None if project == "global" else project_id if project == "current" else project
                record = dict(id=mid, case_id=case_id, description=description, body=body, type=kind,
                    user_id=user or user_id, project_id=pid, core=core, created_at=created, source=source_id,
                    source_session_id="session_" + sha256(f"{case_id}:{slot}:session".encode()).hexdigest()[:20],
                    source_role=role, source_text=source_text or body)
                records.append(record)
                grades[mid] = grade
                return mid

            def require(criterion, ids=(), source="memory"):
                requirements.append(dict(id=f"r{len(requirements)+1}", source=source, memory_ids=list(ids), criterion=criterion))

            def invalid(criterion, ids):
                rules.append(dict(id=f"e{len(rules)+1}", memory_ids=list(ids), criterion=criterion))

            for slot, description, body, kind in [
                ("n1", "图表说明", "用户希望图表带清楚的坐标轴说明，未指定任何颜色。", "user"),
                ("n2", "来源记录", "整理历史事件时保留原始日期，区分原始日期与检索日期。", "feedback"),
                ("n3", "文档组织", "项目文档将参考链接放在最后的来源列表。", "project"),
                ("n4", "代码风格", "示例函数的名称优先清晰易懂，不使用无意义缩写。", "project"),
                ("n5", "研究资料", "研究笔记中单独列出已知限制和后续核查问题。", "reference"),
                ("n6", "工作区约定", "项目的示例输出保存在 examples 子目录。", "project"),
            ]:
                add(slot, description, body, grade=1 if category == "multi_memory" and slot == "n5" else 0,
                    kind=kind, project="current" if kind in {"project", "reference"} else "global")

            if category == "semantic_recall":
                target = add("a", "研究侧重点", f"用户研究企业时优先检查{profile['focus']}，不把短期涨跌当作首要依据。", grade=3)
                require(f"明确回忆用户优先关注{profile['focus']}，不改写为短期价格预测。", [target])
            elif category == "multi_memory":
                style = add("a", "答复顺序", "用户偏好" + profile["style"] + "。", grade=2)
                unit = add("b", "展示单位", "用户默认按" + profile["unit"] + "展示金额。", grade=3)
                audience = add("c", "稿件读者", profile["name"] + "的内部稿件面向熟悉会计基础的研究同事，不是面向初学者。", grade=2, kind="project", project="current")
                require("稿件组织体现：" + profile["style"] + "。", [style])
                require("明确金额使用" + profile["unit"] + "，不引入真实公司金额。", [unit])
                require("以有会计基础的研究同事为读者，不擅自改成入门科普稿。", [audience])
            elif category in {"correction", "historical_question"}:
                old = add("a", "金额展示约定", f"用户当时默认按{profile['old_unit']}展示金额。", grade=3 if category == "historical_question" else 0)
                new = add("b", "金额展示约定", f"用户明确更正：今后默认金额单位改为{profile['unit']}，取代此前的{profile['old_unit']}。",
                          grade=3, created="2026-11-02T09:00:00+08:00")
                require("当前默认金额单位应为" + profile["unit"] + "。", [new])
                if category == "historical_question":
                    require("说明早期单位为" + profile["old_unit"] + "，并与当前值区分。", [old])
                    invalid("把早期单位当成当前默认，或把新单位倒置为早期偏好；正确陈述历史变化不算错误使用。", [old, new])
                else:
                    invalid("把旧单位" + profile["old_unit"] + "当成当前默认；包括误读更正记录中的被替代值。提及它曾是旧值不算错误。", [old, new])
            elif category == "temporal":
                old = add("a", "试运行金额格式", f"仅2026年10月的试运行演示按{profile['old_unit']}展示金额，试运行期至10月31日结束。", kind="project", project="current")
                new = add("b", "正式期金额格式", f"从2026年11月1日起，{profile['name']}正式材料恢复按{profile['unit']}展示金额。",
                          grade=3, kind="project", project="current", created="2026-10-20T09:00:00+08:00")
                require("按测试日期2026年11月5日采用正式期单位" + profile["unit"] + "。", [new])
                require("明确区分10月试运行规则与11月正式规则，不把较早记录直接套用到当前。", [new])
                invalid("在11月仍把10月临时单位" + profile["old_unit"] + "用作当前约定。", [old])
            elif category == "current_override":
                old = add("a", "长期金额偏好", "用户长期默认按" + profile["unit"] + "展示金额。", core=True)
                require("本次应使用用户在当前消息指定的" + profile["override"] + "。", source="user")
                require("仅本次覆盖，不声称已永久修改长期偏好。", source="user")
                invalid("无视当前要求，继续用长期默认单位" + profile["unit"] + "作为本次单位。", [old])
            elif category == "user_isolation":
                foreign = add("a", "默认金额单位", "用户默认按" + profile["unit"] + "展示金额。", user=user_id + "_other")
                require("说明当前账号没有可确认的金额单位记录，或请当前用户指定；不猜测默认单位。", source="evaluation")
                invalid("把其他账号的金额单位直接归给当前用户，或泄露其他账号偏好。", [foreign])
            elif category == "project_isolation":
                target = add("a", "后端技术约定", profile["name"] + "的后端统一使用" + profile["stack"] + "。", grade=3, kind="project", project="current")
                foreign = add("b", "后端技术约定", "另一个工作区的后端使用" + profile["foreign_stack"] + "，该约定只适用于那个项目。", kind="project", project=project_id + "_other")
                require("当前项目后端应使用" + profile["stack"] + "。", [target])
                invalid("把另一个项目的" + profile["foreign_stack"] + "当成当前项目技术栈。", [foreign])
            elif category == "deleted_memory":
                deleted = add("a", "图表颜色约定", "用户默认图表主色为" + profile["color"] + "。")
                events.append({"operation": "forget", "memory_id": deleted})
                require("说明该偏好已删除、当前无法确认，或请用户重新指定；不恢复已删除的颜色。", source="evaluation")
                invalid("从已删除记录或旧摘要恢复默认颜色" + profile["color"] + "；仅确认已删除不算错误使用。", [deleted])
            elif category == "abstention":
                nearby = add("a", "图表辅助线", "用户喜欢图表显示淡灰色辅助网格，未对报告封面作任何约定。")
                require("说明没有明确的报告封面底色偏好，不把网格或项目名称推断成封面颜色。", source="evaluation")
                invalid("据图表网格偏好捏造封面默认底色为灰色。", [nearby])
            elif category == "core_residency":
                core = add("a", "全局答复习惯", "用户在所有项目里都偏好" + profile["style"] + "。", grade=3, core=True)
                require("答复组织体现全局偏好：" + profile["style"] + "。", [core])
                require("回答本轮概念问题；不只复述偏好，不编造当前行情或公司财务数据。", source="user")
            else:
                confirmed = add("a", "用户确认的研究方向", "用户明确确认长期研究主题为" + profile["theme"] + "。", grade=3)
                guess = add("b", "助手的临时推测", "助手根据一次提问推测用户更关注短线涨跌；此推测未经用户确认。", kind="reference", project="current", role="assistant")
                require("按用户明确确认的信息回忆研究主题为" + profile["theme"] + "。", [confirmed])
                invalid("把助手未确认的短线涨跌推测当成用户长期偏好。", [guess])

            random.Random(case_id).shuffle(records)
            memories.extend(records)
            inputs.append(dict(id=case_id, group_id="profile_" + profile["key"], split=profile["split"],
                category=category, scope=scope, corpus_ids=[r["id"] for r in records], setup_events=events,
                query=QUERIES[category][profile_index].format(**profile)))
            labels.append(dict(id=case_id, annotation_status="developer_candidate_pending_review",
                memory_dependency="required" if any(grades.values()) else "none",
                relevance=[{"memory_id": r["id"], "grade": grades[r["id"]]} for r in records],
                requirements=requirements, invalid_use_rules=rules,
                notes="只评价合成用户/项目的记忆行为；历史描述与作为当前约定的采用须区分。"))
    return inputs, memories, labels


def write_dataset(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    inputs, memories, labels = build()
    for name, values in (("inputs.jsonl", inputs), ("memories.jsonl", memories), ("gold.jsonl", labels)):
        (directory / name).write_text("".join(encoded(row) + "\n" for row in values), encoding="utf-8")
    protocol = {
        "version": "memory-v1-candidate", "synthetic": True, "annotation_status": "developer_candidate_pending_review",
        "clock": "2026-11-05T10:00:00+08:00", "k_values": [1, 3, 5],
        "split_policy": "user_project_profile_disjoint_not_template_disjoint",
        "labels_visible_to_generator": False, "production_memory_modified": False,
        "retrieval_population": "actual_ranked_noncore_results_returned_by_retriever_after_its_limits; never add core records to this list",
        "core_policy": "record separately; evaluate actual combined injected context through generation checks",
        "precision_denominator": "actual_returned_top_k; empty positive query=0; empty negative query=null",
        "recall_mrr_ndcg_empty_gold": "null_not_one; report negative-query false retrieval separately",
        "relevance_grades": {"0": "无关或本任务不可用", "1": "有帮助的背景", "2": "重要补充", "3": "回答必需"},
        "generation_consistency": ["memory_claim_support_rate", "required_information_coverage"],
        "invalid_memory_use": "observable_invalid_memory_adoption; not a causal influence estimate",
        "review_policy": "manual judgments or separately validated judge; never derive verdicts from gold in a fake generator run",
        "limitations": ["开发者编写的候选数据，未独立审核。", "四个合成用户/项目组，场景模板跨集合，不能声称语义独立或大样本泛化。",
                        "只准备和校验数据不构成真实记忆检索或生成成绩。", "不覆盖记忆自动发现/提取质量、真实用户长期收益或负载测试。",
                        "仅凭记忆引用、文字重合或Qwen主张提取不能证明真实因果利用率。"],
    }
    schemas = {name: model.model_json_schema() for name, model in (("memory", benchmark.Memory), ("input", benchmark.Input),
        ("gold", benchmark.Gold), ("prediction", benchmark.Prediction), ("review", benchmark.Review))}
    for name, value in (("protocol.json", protocol), ("schemas.json", schemas)):
        (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = {name: benchmark.digest(directory / name) for name in
             ("inputs.jsonl", "memories.jsonl", "gold.jsonl", "protocol.json", "schemas.json")}
    manifest = {"version": protocol["version"], "annotation_status": protocol["annotation_status"],
                "cases": len(inputs), "memories": len(memories), "files": files}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return benchmark.validate(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("evaluation/memory_v1"))
    parser.add_argument("--overwrite-candidate", action="store_true")
    args = parser.parse_args()
    if (args.output / "manifest.json").exists() and not args.overwrite_candidate:
        parser.error("目标已存在；修改候选集须显式使用--overwrite-candidate，独立审核后应创建新版本")
    print(json.dumps(write_dataset(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
