"""Offline memory benchmark contracts and scoring; no network or model calls."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import Field

from .config import StrictModel
from .nlu.semantic import strict_json


CATEGORIES = ("semantic_recall", "multi_memory", "correction", "temporal", "current_override",
              "user_isolation", "project_isolation", "deleted_memory", "abstention",
              "core_residency", "historical_question", "source_priority")


class Scope(StrictModel):
    user_id: str
    project_id: str
    as_of: str


class Memory(StrictModel):
    id: str = Field(pattern=r"^mem_[a-f0-9]{32}$")
    case_id: str
    description: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=4000)
    type: Literal["user", "feedback", "project", "reference"]
    user_id: str
    project_id: str | None
    core: bool
    created_at: str
    source: str
    source_session_id: str
    source_role: Literal["user", "assistant"]
    source_text: str


class SetupEvent(StrictModel):
    operation: Literal["forget"]
    memory_id: str


class Input(StrictModel):
    id: str
    group_id: str
    split: Literal["development", "holdout"]
    category: Literal[CATEGORIES]
    scope: Scope
    corpus_ids: list[str] = Field(min_length=6, max_length=30)
    setup_events: list[SetupEvent]
    query: str = Field(min_length=1, max_length=2000)


class Grade(StrictModel):
    memory_id: str
    grade: int = Field(ge=0, le=3)


class Requirement(StrictModel):
    id: str
    source: Literal["memory", "user", "evaluation"]
    memory_ids: list[str]
    criterion: str = Field(min_length=1)


class InvalidUseRule(StrictModel):
    id: str
    memory_ids: list[str] = Field(min_length=1)
    criterion: str = Field(min_length=1)


class Gold(StrictModel):
    id: str
    annotation_status: Literal["developer_candidate_pending_review"]
    memory_dependency: Literal["required", "none"]
    relevance: list[Grade]
    requirements: list[Requirement] = Field(min_length=1)
    invalid_use_rules: list[InvalidUseRule]
    notes: str


class Prediction(StrictModel):
    id: str
    dataset_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    memory_enabled: bool
    retrieved_ids: list[str]
    core_ids: list[str]
    injected_ids: list[str]
    status: Literal["completed", "failed", "cancelled"]
    answer: str


class RequirementJudgment(StrictModel):
    id: str
    passed: bool
    evidence_quote: str


class ClaimJudgment(StrictModel):
    quote: str = Field(min_length=1)
    verdict: Literal["supported", "contradicted", "unverifiable"]
    memory_ids: list[str]


class InvalidUse(StrictModel):
    rule_id: str
    memory_id: str
    quote: str = Field(min_length=1)


class AnswerJudgment(StrictModel):
    id: str
    answer_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    complete: bool
    requirements: list[RequirementJudgment]
    memory_claims: list[ClaimJudgment]
    invalid_uses: list[InvalidUse]


class Review(StrictModel):
    reviewer: str = Field(min_length=1)
    reviewed_at: str
    independent_of_authoring: bool
    judgments: list[AnswerJudgment]


def read_json(path):
    return strict_json(Path(path).read_text(encoding="utf-8"))


def rows(path, model):
    return [model.model_validate(strict_json(line), strict=True).model_dump()
            for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def answer_digest(answer):
    return sha256(answer.encode()).hexdigest()


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("时间必须带时区")
    return result


def unique(values, label):
    if len(values) != len(set(values)):
        raise ValueError("重复的" + label)


def in_scope(memory, scope):
    return memory["user_id"] == scope["user_id"] and memory["project_id"] in (None, scope["project_id"])


def load_dataset(directory, verify=True):
    directory = Path(directory)
    inputs = rows(directory / "inputs.jsonl", Input)
    memories = rows(directory / "memories.jsonl", Memory)
    labels = rows(directory / "gold.jsonl", Gold)
    for collection, key in ((inputs, "id"), (memories, "id"), (labels, "id")):
        unique([r[key] for r in collection], key)
    by_id, gold = {m["id"]: m for m in memories}, {g["id"]: g for g in labels}
    if {r["id"] for r in inputs} != set(gold):
        raise ValueError("输入与标签必须一一对应")
    groups, users, projects, queries, referenced = {}, {}, {}, {}, set()
    for case in inputs:
        scope, label = case["scope"], gold[case["id"]]
        timestamp(scope["as_of"])
        for key, mapping in ((case["group_id"], groups), (scope["user_id"], users), (scope["project_id"], projects)):
            if key in mapping and mapping[key] != case["split"]:
                raise ValueError("用户、项目或场景组跨开发/留出集合")
            mapping[key] = case["split"]
        normalized_query = "".join(case["query"].split())
        if normalized_query in queries:
            raise ValueError("重复的查询原文")
        queries[normalized_query] = case["id"]
        ids = set(case["corpus_ids"])
        unique(case["corpus_ids"], "记忆池ID")
        if not ids <= by_id.keys():
            raise ValueError("记忆池引用不存在的记忆")
        referenced.update(ids)
        deleted = set()
        for event in case["setup_events"]:
            if event["memory_id"] not in ids or not in_scope(by_id[event["memory_id"]], scope):
                raise ValueError("删除事件必须属于当前用户/项目记忆池")
            deleted.add(event["memory_id"])
        grades = {g["memory_id"]: g["grade"] for g in label["relevance"]}
        if len(grades) != len(label["relevance"]) or set(grades) != ids:
            raise ValueError("相关度必须完整覆盖记忆池且无重复")
        for key in ids:
            memory = by_id[key]
            if memory["case_id"] != case["id"]:
                raise ValueError("记忆池混入其他测试实例")
            if timestamp(memory["created_at"]) > timestamp(scope["as_of"]):
                raise ValueError("种子记忆晚于测试时点")
            if grades[key] and (not in_scope(memory, scope) or key in deleted):
                raise ValueError("其他用户/项目或已删除记忆不能标为正相关")
            if memory["core"] and memory["type"] not in {"user", "feedback"}:
                raise ValueError("常驻记忆类型无效")
        unique([r["id"] for r in label["requirements"]], "必需信息检查ID")
        unique([r["id"] for r in label["invalid_use_rules"]], "错误使用检查ID")
        for requirement in label["requirements"]:
            if not set(requirement["memory_ids"]) <= ids:
                raise ValueError("必需信息来源不在记忆池")
            if requirement["source"] == "memory" and (not requirement["memory_ids"] or any(
                    grades[mid] == 0 for mid in requirement["memory_ids"])):
                raise ValueError("必需记忆信息须引用相关记忆")
        for rule in label["invalid_use_rules"]:
            if not set(rule["memory_ids"]) <= ids:
                raise ValueError("错误使用规则引用不存在的记忆")
        if (label["memory_dependency"] == "none") != (not any(grades.values())):
            raise ValueError("记忆依赖标签与相关度不一致")
    if referenced != set(by_id):
        raise ValueError("存在未被使用的种子记忆")
    protocol = read_json(directory / "protocol.json")
    if protocol.get("labels_visible_to_generator") is not False or protocol.get("synthetic") is not True:
        raise ValueError("本候选集必须明确合成数据且隔离生成输入与金标")
    if any(timestamp(case["scope"]["as_of"]) != timestamp(protocol["clock"]) for case in inputs):
        raise ValueError("样例与协议冻结时点不一致")
    if (not protocol.get("k_values") or any(type(k) is not int or k < 1 for k in protocol["k_values"])
            or len(set(protocol["k_values"])) != len(protocol["k_values"])):
        raise ValueError("排序截断k必须为不重复的正整数")
    if set(CATEGORIES) != {c["category"] for c in inputs}:
        raise ValueError("场景覆盖不完整")
    if any({c["category"] for c in inputs if c["split"] == split} != set(CATEGORIES)
           for split in ("development", "holdout")):
        raise ValueError("开发和留出集合都必须覆盖全部场景类别")
    if verify:
        manifest = read_json(directory / "manifest.json")
        if (manifest.get("cases") != len(inputs) or manifest.get("memories") != len(memories)
                or manifest.get("version") != protocol["version"]
                or manifest.get("annotation_status") != protocol["annotation_status"]):
            raise ValueError("冻结清单规模或版本不匹配")
        for filename, expected in manifest["files"].items():
            if Path(filename).name != filename or digest(directory / filename) != expected:
                raise ValueError("冻结文件摘要不匹配：" + filename)
        if set(manifest["files"]) != {"inputs.jsonl", "memories.jsonl", "gold.jsonl", "protocol.json", "schemas.json"}:
            raise ValueError("冻结清单缺失文件")
    return inputs, by_id, gold, protocol


def validate(directory):
    cases, memories, gold, _ = load_dataset(directory)
    return {"status": "valid_candidate_not_acceptance", "cases": len(cases), "memories": len(memories),
            "splits": dict(Counter(c["split"] for c in cases)),
            "categories": dict(Counter(c["category"] for c in cases)),
            "annotation_status": "developer_candidate_pending_review"}


def retrieval_metrics(ranked, relevance, k):
    selected = ranked[:k]
    relevant = {key for key, grade in relevance.items() if grade > 0}
    hits = sum(key in relevant for key in selected)
    precision = hits / len(selected) if selected else (0.0 if relevant else None)
    recall = hits / len(relevant) if relevant else None
    positions = [i for i, key in enumerate(selected, 1) if key in relevant]
    dcg = sum((2 ** relevance.get(key, 0) - 1) / math.log2(i + 2) for i, key in enumerate(selected))
    ideal = sum((2 ** grade - 1) / math.log2(i + 2) for i, grade in enumerate(sorted(relevance.values(), reverse=True)[:k]))
    return {"precision": precision, "recall": recall,
            "mrr": 1 / positions[0] if positions else (0.0 if relevant else None),
            "ndcg": dcg / ideal if ideal else None}


def mean(values):
    available = [v for v in values if v is not None]
    return sum(available) / len(available) if available else None


def score(directory, predictions_path, *, split="holdout", review_path=None):
    cases, memories, gold, protocol = load_dataset(directory)
    cases = [c for c in cases if c["split"] == split]
    case_index = {c["id"]: c for c in cases}
    predictions = rows(predictions_path, Prediction)
    unique([p["id"] for p in predictions], "预测ID")
    if any(p["id"] not in case_index for p in predictions):
        raise ValueError("预测包含不属于所选集合的样例")
    predicted = {p["id"]: p for p in predictions}
    for p in predictions:
        if p["dataset_manifest_sha256"] != digest(Path(directory) / "manifest.json"):
            raise ValueError("预测绑定的数据集摘要不匹配，不能沿用旧版本成绩")
        if not p["memory_enabled"]:
            raise ValueError("记忆评估必须实际启用记忆，不能复用关闭记忆的候选验收运行")
        pool = set(case_index[p["id"]]["corpus_ids"])
        for field in ("retrieved_ids", "core_ids", "injected_ids"):
            unique(p[field], field)
            if not set(p[field]) <= pool:
                raise ValueError("预测记忆ID不在该样例记忆池")
        if set(p["retrieved_ids"]) & set(p["core_ids"]):
            raise ValueError("常驻和检索通道不可重复计数")
        if any(memories[key]["core"] for key in p["retrieved_ids"]) or any(not memories[key]["core"] for key in p["core_ids"]):
            raise ValueError("常驻与检索通道标记不匹配")
        if not set(p["injected_ids"]) <= set(p["retrieved_ids"] + p["core_ids"]):
            raise ValueError("注入记忆不属于实际检索/常驻结果")
    review = Review.model_validate(read_json(review_path), strict=True).model_dump() if review_path else None
    judgments = {}
    if review:
        timestamp(review["reviewed_at"])
        unique([j["id"] for j in review["judgments"]], "答案审核ID")
        for j in review["judgments"]:
            if j["id"] not in predicted:
                raise ValueError("答案审核没有对应预测")
            p, label = predicted[j["id"]], gold[j["id"]]
            if not j["complete"] or j["answer_sha256"] != answer_digest(p["answer"]):
                raise ValueError("审核未完成或答案摘要不匹配")
            requirements = {r["id"]: r for r in label["requirements"]}
            unique([r["id"] for r in j["requirements"]], "审核必需信息ID")
            if {r["id"] for r in j["requirements"]} != set(requirements):
                raise ValueError("必需信息审核未完整覆盖")
            for r in j["requirements"]:
                if r["evidence_quote"] and r["evidence_quote"] not in p["answer"]:
                    raise ValueError("审核引文不在实际答案")
                if r["passed"] and (p["status"] != "completed" or not r["evidence_quote"]):
                    raise ValueError("失败/空答复不能通过必需信息检查")
                requirement = requirements[r["id"]]
                if r["passed"] and requirement["source"] == "memory" and not set(requirement["memory_ids"]) <= set(p["injected_ids"]):
                    raise ValueError("必需记忆未实际注入，不能记为已使用该记忆信息")
            for c in j["memory_claims"]:
                if c["quote"] not in p["answer"] or not set(c["memory_ids"]) <= set(p["injected_ids"]):
                    raise ValueError("主张审核引用不在答案或实际注入的记忆中")
                if c["verdict"] == "supported" and not c["memory_ids"]:
                    raise ValueError("记忆支持的主张必须有实际注入的来源")
                deleted = {e["memory_id"] for e in case_index[p["id"]]["setup_events"]}
                if c["verdict"] == "supported" and any(not in_scope(memories[mid], case_index[p["id"]]["scope"])
                        or mid in deleted for mid in c["memory_ids"]):
                    raise ValueError("其他用户/项目或已删除记忆不能作为有效支持来源")
            rules = {r["id"]: r for r in label["invalid_use_rules"]}
            for misuse in j["invalid_uses"]:
                if (misuse["rule_id"] not in rules or misuse["memory_id"] not in rules[misuse["rule_id"]]["memory_ids"]
                        or misuse["memory_id"] not in p["injected_ids"] or misuse["quote"] not in p["answer"]):
                    raise ValueError("错误记忆采用须对应既定规则、实际注入记忆及答案引文")
            judgments[j["id"]] = j
    retrieval = []
    for case in cases:
        p, label = predicted.get(case["id"]), gold[case["id"]]
        grades = {g["memory_id"]: g["grade"] for g in label["relevance"] if not memories[g["memory_id"]]["core"]}
        ranked = p["retrieved_ids"] if p else []
        retrieval.append({"id": case["id"], "missing": p is None,
                          "has_relevant_retrieval": any(grades.values()),
                          "returned_any": bool(ranked),
                          "at_k": {str(k): retrieval_metrics(ranked, grades, k) for k in protocol["k_values"]}})
    metrics = {str(k): {metric: mean(r["at_k"][str(k)][metric] for r in retrieval)
                       for metric in ("precision", "recall", "mrr", "ndcg")} for k in protocol["k_values"]}
    reviewed = list(judgments.values())
    claims = [claim for j in reviewed for claim in j["memory_claims"]]
    requirements = [r for j in reviewed for r in j["requirements"]]
    probes = [j for j in reviewed if gold[j["id"]]["invalid_use_rules"]]
    generation = {"status": "not_evaluated" if not reviewed else "complete" if len(reviewed) == len(cases) else "partial",
        "reviewed": len(reviewed), "planned": len(cases),
        "memory_claim_support_rate": sum(c["verdict"] == "supported" for c in claims) / len(claims) if claims else None,
        "required_information_coverage": sum(r["passed"] for r in requirements) / len(requirements) if requirements else None,
        "invalid_memory_adoption_rate": sum(bool(j["invalid_uses"]) for j in reviewed) / len(reviewed) if reviewed else None,
        "invalid_memory_adoption_rate_on_probes": sum(bool(j["invalid_uses"]) for j in probes) / len(probes) if probes else None,
        "memory_claims": len(claims), "requirements": len(requirements), "error_probes_reviewed": len(probes)}
    negative = [r for r in retrieval if not r["has_relevant_retrieval"] and not r["missing"]]
    return {"kind": "candidate_memory_evaluation_not_independent_acceptance", "split": split,
            "dataset_manifest_sha256": digest(Path(directory) / "manifest.json"),
            "predictions_sha256": digest(predictions_path), "planned": len(cases), "predicted": len(predicted),
            "missing": [c["id"] for c in cases if c["id"] not in predicted],
            "failed_or_cancelled": sum(p["status"] != "completed" for p in predictions),
            "retrieval": metrics, "retrieval_records": retrieval, "generation": generation,
            "empty_gold_false_retrieval_rate": mean(int(r["returned_any"]) for r in negative),
            "review_sha256": digest(review_path) if review_path else None,
            "independent_acceptance_passed": False}
