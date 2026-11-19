"""User-facing wording and progress; evidence and audit records remain untouched."""
from __future__ import annotations

import json
import re


def failure_answer(code: str, *, has_evidence: bool = False, correction_attempts: int | None = None,
                   reason: str = "") -> str:
    messages = {
        "CANCELLED": "已取消这次查询。",
        "FINISH_CHECK_FAILED": "这次结果没有通过核验，还不能作为可靠答复。",
        "FACT_CHECK_FAILED": "我不知道如何用现有资料确认这份答复中的关键事实；核验未通过，暂不提供可能错误的数字或结论。",
        "CLAIM_EXTRACTION_FAILED": "这次答复的主张提取未能完成，暂不交付未经完整检查的草稿。请稍后重试。",
        "ANSWER_REVIEW_INPUT": "本次回答或相关证据超过审核范围，暂不交付草稿。请缩小问题范围后重试。",
        "ANSWER_REVIEW_TIMEOUT": "本次回答的证据审核超时，暂不交付草稿。请稍后重试。",
        "ANSWER_REVIEW_UNAVAILABLE": "证据审核服务暂不可用，暂不交付草稿。请稍后重试。",
        "ANSWER_REVIEW_INVALID": "证据审核未返回完整有效的结果，暂不交付草稿。请稍后重试。",
        "ANSWER_REVIEW_INCOMPLETE": "现有材料不足以完成证据审核，暂不交付草稿。请补充资料或缩小问题范围。",
        "ANSWER_REVIEW_EVIDENCE": "部分审核资料无法读取或来源校验未通过，暂不交付草稿。已保留诊断记录。",
        "ANSWER_REVIEW_CAPACITY": "审核上下文容量不足，暂不交付草稿。完整资料和取证记录已保留。",
        "ANSWER_REVIEW_BUDGET": "本次证据审核已达到处理预算，尚未完成核验。已保留资料和审核进度。",
        "ANSWER_REVIEW_NO_PROGRESS": "证据审核连续补查未取得新信息，已停止重复查询。草稿未交付，资料和诊断已保留。",
        "MODEL_BUDGET": "这次分析已达到处理次数上限，暂时无法继续核对。",
        "TOOL_BUDGET": "这次查询已达到工具调用上限，暂时无法继续补查。",
        "SOURCE_BUDGET": "这次数据查询已达到请求上限，暂时无法继续补查。",
        "TIME_BUDGET": "这次查询用时超过了上限，暂时没能完成。",
        "NO_PROGRESS": "重复查询没有取得新的信息，我先停在这里。",
        "MODEL_TRUNCATED": "这次回复没有完整生成，还不能给出可靠结论。",
        "EMPTY_RESPONSE": "模型完成了处理，但没有返回答复，请重试或更换模型。",
        "MODEL_PROTOCOL_ERROR": "这次模型返回的内容无法正常处理，没能完成答复。",
        "MODEL_UNAVAILABLE": "暂时无法连接模型服务，请稍后再试。",
        "MODEL_NOT_CONFIGURED": "尚未配置模型，请在 .env 中设置 FINAGENT_BASE_URL 和 FINAGENT_MODEL。",
        "MODEL_KEY_MISSING": "缺少模型 API 密钥，请配置 finagent.toml 中 api_key_env 指定的环境变量（默认 FINAGENT_API_KEY）。",
        "INVALID_MODEL_URL": "模型 API 地址无效，请检查 FINAGENT_BASE_URL；远程服务需使用不含凭证和查询参数的 HTTPS 地址。",
        "MODEL_HTTP_ERROR": "模型服务拒绝了请求，请检查 API 配置和服务商账户状态。",
        "INTERNAL_ERROR": "这次处理遇到了内部问题，没能完成答复。",
    }
    answer = messages.get(code, "这次查询没有顺利完成，暂时无法给出可靠结论。")
    if code == "MODEL_UNAVAILABLE":
        transport = re.search(r"\b(ConnectError|ConnectTimeout|ReadTimeout|WriteTimeout|PoolTimeout|ProxyError|RemoteProtocolError)\b", reason)
        if transport:
            kind = transport[1]
            if kind == "ProxyError":
                advice = "请检查模型代理是否启动、端口是否正确。可在 .env 中设置 FINAGENT_MODEL_PROXY。"
            elif kind in {"ConnectError", "ConnectTimeout"}:
                advice = "请检查后端网络；若需要代理，可在 .env 中设置 FINAGENT_MODEL_PROXY，保存后重启后端。"
            elif kind == "ReadTimeout":
                advice = "模型服务未在请求时限内返回，请稍后重试，或检查模型请求超时配置。"
            else:
                advice = "请检查网络和代理连接，或稍后重试。"
            answer = f"模型连接未完成（{kind}），未能生成答复。{advice}"
    if code == "MODEL_HTTP_ERROR":
        # Extract only the numeric status, never echo provider payloads or credentials.
        status = re.search(r"\bHTTP ([1-5][0-9]{2})\b", reason)
        if status:
            number = int(status[1])
            advice = {
                400: "请检查模型名称、请求参数和接口协议是否匹配。",
                401: "API 鉴权失败，请检查密钥是否有效及是否属于当前服务商。",
                402: "通常与付费或额度限制有关，请检查服务商账户的余额、额度及计费状态，或更换可用的模型配置。",
                403: "请求被拒绝，请检查账户权限和模型访问权限。",
                404: "请检查 FINAGENT_BASE_URL、FINAGENT_MODEL 和接口协议是否正确。",
                429: "请求受到限流或配额限制，请稍后重试并检查服务商配额。",
            }.get(number, "模型服务暂时异常，请稍后重试。" if number >= 500
                  else "请检查 API 配置和服务商账户状态。")
            answer = f"模型服务返回 HTTP {number}，未能生成答复。{advice}"
    if code == "FINISH_CHECK_FAILED" and correction_attempts is not None:
        answer += f" 已尝试修正 {correction_attempts} 次，仍未解决核验问题；可用 /details 查看具体原因。"
    if has_evidence:
        answer += " 已取得的资料保存在本次运行记录中。"
    return answer


def result_body(result):
    return json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))


def format_answer(result) -> str:
    """Use short citation labels in chat while keeping exact IDs in the saved answer."""
    body = result_body(result)
    if not (body.get("final_protocol_verified") or body.get("terminal_response_validated")):
        return result.answer
    labels = {key: f"[来源{index}]" for index, key in enumerate(body["cited_evidence_ids"], 1)}
    labels.update({key: f"[计算{index}]" for index, key in enumerate(body["cited_calculation_ids"], 1)})
    return re.sub(r"(?:raw_[a-f0-9]{64}|result_[a-f0-9]{32})",
                  lambda match: labels.get(match[0], match[0]), result.answer)


def format_sources(result, store) -> str:
    body = result_body(result)
    verified = body.get("final_protocol_verified")
    if not (verified or body.get("terminal_response_validated")):
        return "这次没有可展示的答复来源。可用 /details 查看运行记录。"
    evidence = body["cited_evidence_ids"] or body.get("available_evidence_ids", [])
    calculations = body["cited_calculation_ids"] or body.get("available_calculation_ids", [])
    if not evidence and not calculations:
        return "上一条回复没有引用外部数据或程序计算。"
    values = [store.load_result(key) for key in body["result_tools"]]
    lines = [] if verified else ["以下为本次取得的来源与计算记录，不代表正文已逐句核验。"]
    for index, key in enumerate(evidence, 1):
        value = next((value for value in values if key in [value.snapshot_id, *value.snapshot_ids]
                      and value.source_url), None)
        if value is None:
            value = next((value for value in values if key in [value.snapshot_id, *value.snapshot_ids]), None)
        source = (value.source or "原始资料") if value else "原始资料"
        lines.append(f"[来源{index}] {source}")
        if value:
            stamp = value.data_time or (value.fetched_at.isoformat() if value.fetched_at else None)
            if stamp:
                lines.append("时间：" + stamp)
            if value.source_url:
                lines.append(value.source_url)
        lines.append("证据：" + key)
    lines.extend(f"[计算{index}] {key}" for index, key in enumerate(calculations, 1))
    return "\n".join(lines)


class ProgressReporter:
    """Report observed stages once, without tool payloads or private reasoning."""

    STAGES = {
        "get_sector_ranking": ("industry", "正在读取行业分类与涨跌幅，并核对数据时效。"),
        "resolve_entity": ("identity", "我先确认一下你说的是哪家公司。"),
        "resolve_task_scope": ("scope", "我先核对这次要看的期间和指标口径。"),
        "get_quote": ("market", "正在查询行情，同时核对数据时间。"),
        "get_daily_bars": ("market", "正在查询行情，同时核对数据时间。"),
        "get_market_data": ("market", "正在查询行情，同时核对数据时间。"),
        "get_benchmark_data": ("benchmark", "我再取对应的基准行情，方便对照。"),
        "get_financial_statements": ("financial", "正在查对应期间的财务数据。"),
        "get_company_profile": ("profile", "正在核对公司的基本资料。"),
        "get_stock_news": ("events", "正在查相关消息，并核对发布时间。"),
        "execute_program": ("calculation", "正在用取得的数据核算结果。"),
        "calculate_market_metrics": ("calculation", "正在用取得的行情核算涨跌数据。"),
        "calculate_closing_review": ("calculation", "正在核算行情指标，并检查数据是否完整。"),
        "calculate_technical_divergence": ("divergence", "正在核对日线背离结构、确认时间及失效条件。"),
    }

    def __init__(self, write=None):
        self.write = write
        self.seen = set()

    def __call__(self, event):
        if self.write is None:
            return
        kind = event["kind"]
        message = None
        if kind == "tool_started":
            stage = self.STAGES.get(event["name"])
            if stage:
                key, message = stage
            else:
                return
        elif kind == "tool_result" and event["result"]["status"] in ("empty", "error"):
            if event["name"] == "ask_user":
                return
            key = "data_gap"
            message = "这次查询暂时没有返回可用数据。"
        elif kind == "finish_rejected":
            if not event.get("will_retry", True):
                return
            attempt = event.get("correction_attempt", 0) + 1
            key = ("verification", attempt)
            message = f"答复还没通过核验，我正在修正（第 {attempt} 次）。"
        else:
            return
        if key in self.seen:
            return
        self.seen.add(key)
        try:
            self.write(message)
        except Exception:
            # A disconnected display must not change execution or evidence.
            self.write = None
