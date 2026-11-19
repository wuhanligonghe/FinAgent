"""Host-owned plan, immutable handoffs, reference closure and decision eligibility."""
from __future__ import annotations

import json
import re
from uuid import uuid4
from ..errors import AgentError
from .contracts import digest
from .decision_contracts import SCHEMAS, ItemRef
from .review import number_tokens, numeric_content, unmatched_numbers

ROLE_PROMPT_VERSION = "2.1.3"

ROLE_TITLES = {"bull": "多头研究员", "bear": "空头研究员", "research_manager": "研究经理",
    "trader": "交易员", "risk_aggressive": "激进风险分析师", "risk_conservative": "保守风险分析师",
    "risk_neutral": "中性风险分析师", "portfolio_manager": "投资组合经理", "quality_gate": "质量门收口"}


def access(role, scope=None):
    if role == "research_manager" and getattr(scope, "execution_mode", None) == "deep_research":
        return "final"
    return "final" if role == "portfolio_manager" else "downstream" if role in ROLE_TITLES and role != "quality_gate" else False


def plan(scope):
    nodes = [{"id": r, "role": r, "depends_on": []} for r in scope.analysts]
    nodes += [{"id": "review", "role": "review", "depends_on": list(scope.analysts)},
              {"id": "quality_gate", "role": "quality_gate", "depends_on": ["review"]}]
    previous = "quality_gate"
    for round_ in range(1, scope.debate_rounds + 1):
        for side in ("bull", "bear"):
            key = f"{side}_{round_}"
            nodes.append({"id": key, "role": side, "round": round_, "depends_on": [previous]})
            previous = key
    nodes.append({"id": "research_manager", "role": "research_manager", "depends_on": [previous]})
    if getattr(scope, "execution_mode", None) == "deep_research":
        return nodes
    nodes.append({"id": "trader", "role": "trader", "depends_on": ["research_manager"]})
    previous = "trader"
    for round_ in range(1, scope.risk_rounds + 1):
        for role in ("risk_aggressive", "risk_conservative", "risk_neutral"):
            key = f"{role}_{round_}"
            nodes.append({"id": key, "role": role, "round": round_, "depends_on": [previous]})
            previous = key
    nodes.append({"id": "portfolio_manager", "role": "portfolio_manager", "depends_on": [previous]})
    return nodes


def validate_config(scope, cfg):
    deep = getattr(scope, "execution_mode", None) == "deep_research"
    logical = len(scope.analysts) + (5 if deep else 7) + 2 * scope.debate_rounds + (0 if deep else 3 * scope.risk_rounds)
    downstream = 8 * scope.debate_rounds + (4 if deep else 12 * scope.risk_rounds + 12)
    upstream = len(scope.analysts) * cfg.expert_model_calls + 4 + 2 * cfg.followup_model_calls + 4
    if cfg.max_attempts < logical or cfg.model_calls < upstream + downstream:
        raise AgentError("INVALID_DECISION_CONFIG", "轮数、必需角色与模型／尝试预算不相容，请增加预算或显式调整轮数")
    if cfg.model_calls - cfg.reserve_model < upstream or cfg.reserve_model < downstream:
        raise AgentError("INVALID_DECISION_CONFIG", "阶段保留池不能容纳配置的角色上限")
    if cfg.tool_calls < len(scope.analysts) * cfg.expert_tool_calls + cfg.reserve_tool or cfg.wall_seconds <= cfg.reserve_seconds:
        raise AgentError("INVALID_DECISION_CONFIG", "工具或时间阶段保留配置不相容")


def seal(body, scope, run, task, role, attempt, inputs):
    value = {**body, "schema_version": "2.1", "workflow_version": scope.workflow_version, "research_id": run,
             "scope_hash": scope.scope_hash, "task_id": task, "attempt_id": attempt, "role": role,
             "role_prompt_version": ROLE_PROMPT_VERSION, "artifact_id": uuid4().hex,
             "input_artifact_refs": [ref(a) for a in inputs],
             "content_status": body.get("content_status", "complete")}
    from ..runtime import now
    value["created_at"] = now().isoformat()
    value["artifact_hash"] = digest(value)
    return value


def verify_seal(value, scope):
    if value.get("scope_hash") != scope.scope_hash or value.get("artifact_hash") != digest({k: v for k, v in value.items() if k != "artifact_hash"}):
        raise AgentError("ARTIFACT_CHANGED", "交接产物范围或哈希不匹配")


def ref(artifact, item="$", **extra):
    return {"artifact_id": artifact["artifact_id"], "artifact_hash": artifact["artifact_hash"], "item_id": item, **extra}


def items(artifact):
    result = {"$": artifact}
    for key, identity in (("claims", "claim_id"), ("arguments", "argument_id"), ("risk_arguments", "argument_id")):
        for item in artifact.get(key, []):
            if item[identity] in result:
                raise AgentError("DUPLICATE_ITEM", "条目ID重复")
            result[item[identity]] = item
    return result


def references(value):
    if isinstance(value, dict):
        if {"artifact_id", "artifact_hash", "item_id"} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


def resolve(reference, artifacts):
    r = reference.model_dump() if isinstance(reference, ItemRef) else reference
    a = next((a for a in artifacts if a["artifact_id"] == r["artifact_id"] and a["artifact_hash"] == r["artifact_hash"]), None)
    if a is None or r["item_id"] not in items(a):
        raise AgentError("INVALID_ARTIFACT_REFERENCE", "引用未授权、版本不符或条目不存在")
    return a, items(a)[r["item_id"]]


def text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        if {"artifact_id", "artifact_hash", "item_id"} <= value.keys():
            return
        for key, child in value.items():
            if key not in {"argument_id", "side", "perspective", "decision_status", "rating", "action", "round", "turn_index", "issue_id"}:
                yield from text_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from text_values(child)


def validate_output(role, payload, artifacts, scope, task, bundle):
    args = SCHEMAS[role].model_validate(payload)
    body = args.model_dump(mode="json")
    for a in artifacts:
        verify_seal(a, scope)
    for r in references(body):
        resolve(r, artifacts)
    # Numeric support is drawn only from referenced claims/arguments and scope,
    # never arbitrary numbers elsewhere in another report.
    selected = [resolve(r, artifacts)[1] for r in references(body) if r["item_id"] != "$"]
    support = number_tokens(numeric_content(selected) + numeric_content(scope.model_dump(mode="json")))
    missing_numbers = unmatched_numbers(" ".join(text_values(body)), support)
    if missing_numbers:
        raise AgentError("UNSUPPORTED_DECISION_NUMBER", "未获引用支持的数字：" + "、".join(str(n) for n in sorted(missing_numbers)[:12]) + "；请补充真实主张引用，或删除相应数字，不得自行估算")
    prose = " ".join(text_values(body))
    if re.search(r"(?:目标价|止损价|止盈价|买入价|仓位|股数)[^。；\n]{0,18}\d|(?:买入|卖出)\s*\d+\s*股", prose):
        raise AgentError("EXECUTION_PARAMETERS_DENIED", "不接纳目标价、止损或仓位执行参数")
    if body.get("decision_status") == "abstained":
        for match in re.finditer(r"(?:建议|应当|应该|立即)(?:积极|直接|立即)?(?:买入|卖出|增持|减持|加仓|清仓|建仓)|\b(?:recommend|should)\s+(?:buy|sell)\b", prose, re.I):
            before = prose[max(0, match.start() - 8):match.start()]
            if not re.search(r"不|未|不能|无法|不应|not\s*$", before, re.I):
                raise AgentError("NARRATIVE_DECISION_CONFLICT", "暂不判断产物不能在文字中给出买卖建议")
    if re.search(r"已确认本方案(?:可以|可)执行|已确认交易可行|保证可成交", prose):
        raise AgentError("EXECUTION_FEASIBILITY_UNKNOWN", "本流程没有执行可行性确认，不得宣称已确认成交或执行")
    if role in ("bull", "bear") or role.startswith("risk_"):
        if body.get("horizon") is not None and body["horizon"] != scope.decision_horizon:
            raise AgentError("HORIZON_MISMATCH", "讨论的判断期限必须与冻结范围一致")
        risk = role.startswith("risk_")
        previous = [a for a in artifacts if a.get("role", "").startswith("risk_")] if risk else [a for a in artifacts if a.get("role") in ("bull", "bear")]
        expected = len(previous) + 1
        width = 3 if risk else 2
        order = ("risk_aggressive", "risk_conservative", "risk_neutral") if risk else ("bull", "bear")
        if body["turn_index"] != expected or body["round"] != (expected - 1) // width + 1 or role != order[(expected - 1) % width] or body.get("perspective", body.get("side")) != role:
            raise AgentError("TURN_ORDER", "角色、轮次或发言序号不符合宿主计划")
        if task != f"{role}_{body['round']}":
            raise AgentError("TURN_ORDER", "任务身份不匹配")
        opponents = [a for a in previous if a["role"] != role]
        key = "risk_arguments" if risk else "arguments"
        targets = {(a["artifact_id"], c["argument_id"]) for a in opponents for c in a.get(key, [])}
        if opponents:
            if targets:
                if not body["responses"]:
                    raise AgentError("RESPONSE_REQUIRED", "必须回应已存在的对方论点")
                for response in body["responses"]:
                    r = response["target"]
                    if (r["artifact_id"], r["item_id"]) not in targets:
                        raise AgentError("INVALID_RESPONSE_TARGET", "不能回应未来、同方或非论点条目")
            elif not body["no_response_reason"] or not body["no_response_refs"] or any(r["item_id"] != "$" or r["artifact_id"] not in {a["artifact_id"] for a in opponents} for r in body["no_response_refs"]):
                raise AgentError("RESPONSE_REQUIRED", "无论点时须关联对方弃权发言并说明")
        if risk:
            proposal, _ = resolve(body["proposal_ref"], artifacts)
            if proposal["role"] != "trader" or body["proposal_ref"]["item_id"] != "$":
                raise AgentError("PROPOSAL_MISMATCH", "必须审查同一有效交易员方案")
        return body
    if role == "quality_gate":
        issues = {i["issue_id"] for i in bundle["issues"]}
        if {r["issue_id"] for r in body["resolutions"]} != issues or len(body["resolutions"]) != len(issues):
            raise AgentError("ISSUE_RESOLUTION_MISMATCH", "收口须覆盖原问题且不能新增问题")
        for r in body["resolutions"]:
            if r["state"] != "open" and not r["claim_refs"]:
                raise AgentError("RESOLUTION_EVIDENCE_REQUIRED", "解决或撤回问题必须有可回读依据")
        return body
    if body["horizon"] != scope.decision_horizon:
        raise AgentError("HORIZON_MISMATCH", "判断期限必须与冻结范围一致")
    if body["decision_status"] == "decided" and (bundle["gate_status"] != "pass" or not bundle.get("debate_complete")):
        raise AgentError("DECISION_REQUIRES_ABSTENTION", "受限研究或未完成辩论必须暂不判断")
    if role == "research_manager":
        argument_refs = {(a["artifact_id"], c["argument_id"]) for a in artifacts if a.get("role") in ("bull", "bear") for c in a.get("arguments", [])}
        ruled = {(r["target"]["artifact_id"], r["target"]["item_id"]) for r in body["issue_rulings"]}
        if argument_refs != ruled:
            raise AgentError("RULING_MISSING", "研究经理必须逐项裁决已有主要论点")
    elif role == "trader":
        previous, _ = resolve(body["plan_ref"], artifacts)
        if previous["role"] != "research_manager" or body["plan_ref"]["item_id"] != "$":
            raise AgentError("PLAN_MISMATCH", "方案必须引用研究经理产物")
        if previous["decision_status"] == "abstained" and body["decision_status"] != "abstained":
            raise AgentError("DECISION_REQUIRES_ABSTENTION", "前序弃权必须沿链保留")
    elif role == "portfolio_manager":
        p, _ = resolve(body["plan_ref"], artifacts)
        t, _ = resolve(body["proposal_ref"], artifacts)
        if p["role"] != "research_manager" or t["role"] != "trader" or body["plan_ref"]["item_id"] != "$" or body["proposal_ref"]["item_id"] != "$":
            raise AgentError("PLAN_MISMATCH", "最终裁决必须关联经理与方案")
        risks = {a["artifact_id"] for a in artifacts if a.get("role", "").startswith("risk_")}
        if {r["artifact_id"] for r in body["risk_refs"]} != risks or any(r["item_id"] != "$" for r in body["risk_refs"]):
            raise AgentError("RISK_COVERAGE_MISSING", "最终裁决须覆盖所有有效风险发言")
        if body["decision_status"] == "decided" and (not bundle.get("risk_complete") or p["decision_status"] != "decided" or t["decision_status"] != "decided" or not bundle["decision_eligible"]):
            raise AgentError("DECISION_REQUIRES_ABSTENTION", "关键证据、风险轮次或前序方向不满足决策条件")
        targets = {r["target"]["artifact_id"] for r in body["accepted_rejected_views"]}
        opposition = {a["artifact_id"] for a in artifacts if a.get("role") in ("bear", "risk_conservative") and (a.get("arguments") or a.get("risk_arguments"))}
        if opposition and not targets & opposition:
            raise AgentError("OPPOSITION_MISSING", "最终裁决必须处置至少一项关键反对意见")
        if ((body["rating"] in ("Buy", "Overweight") and body["action"] == "Sell") or
                (body["rating"] in ("Sell", "Underweight") and body["action"] == "Buy")):
            raise AgentError("DIRECTION_CONFLICT", "同一判断期限下评级与方向相反")
    if body["decision_status"] == "decided" and not body["claim_refs"]:
        raise AgentError("DECISION_EVIDENCE_REQUIRED", "方向判断必须引用主张")
    return body
