"""Explicit terminal configuration; no language model runs while drafting."""
from datetime import datetime
from time import monotonic

from ..errors import AgentError
from ..console import read_input
from ..runtime import ExecutionContext, now
from .contracts import ROLES
from .selection import MODES, SelectedRequest, SelectedScope, config_for, select


def menu(settings, *, read=None, write=print, symbol=None, as_of=None, session_id=None):
    read = read or read_input
    began = monotonic()
    source_used = 0

    def ask(prompt, default=""):
        answer = read(prompt).strip()
        if monotonic() - began > 900:
            raise AgentError("SELECTION_EXPIRED", "配置已过期，请重新打开 /research")
        if answer.lower() in ("0", "取消", "/cancel", "/exit", "/quit"):
            return None
        return answer or default

    try:
        while True:
            write("请选择：1. 深度研究报告（多空辩论、研究经理裁决）  2. 完整研究与决策（另含交易方案、风险讨论、最终决策）  0. 返回聊天")
            choice = ask("选择 > ")
            if choice is None:
                return None
            if choice not in ("1", "2"):
                write("请选择当前菜单中的 1、2 或 0。")
                continue
            mode = "deep_research" if choice == "1" else "full_decision"
            query = ask("股票名称或代码 > ", symbol or "")
            if query is None:
                return None
            if not query:
                write("需要明确股票。")
                continue
            from ..data.symbols import normalize_symbol
            ctx = ExecutionContext(settings.limits, as_of=as_of or now())
            ctx.source_requests = source_used
            try:
                selected = normalize_symbol(query)
            except (ValueError, AgentError):
                from ..data.service import DataService
                service = DataService(settings)
                try:
                    result = service.resolve_entity(query, ctx)
                    service.store.save_result(result)
                    selected = result.data.get("selected", {}).get("symbol")
                    candidates = result.data.get("candidates", [])
                    if not selected and candidates:
                        for i, item in enumerate(candidates, 1):
                            write(f"{i}. {item.get('name', '')} {item['symbol']}")
                        answer = ask("候选序号 > ")
                        if answer is None:
                            return None
                        if answer.isdecimal() and 1 <= int(answer) <= len(candidates):
                            selected = candidates[int(answer) - 1]["symbol"]
                    if not selected:
                        write("未能唯一确认股票，请重新配置。")
                        continue
                finally:
                    source_used = ctx.source_requests
                    service.close()
            value = ask("分析维度（technical sentiment news fundamentals policy；回车全部） > ", "全部")
            if value is None:
                return None
            names = {"技术": "technical", "情绪": "sentiment", "新闻": "news", "基本面": "fundamentals", "政策": "policy"}
            roles = list(ROLES) if value in ("全部", "all") else [names.get(x, x) for x in value.replace("，", " ").replace(",", " ").split()]
            if not roles or len(set(roles)) != len(roles) or set(roles) - set(ROLES):
                write("分析维度无效，请重新配置。")
                continue
            cutoff = ask("证据截止（带时区 ISO 时间；回车使用当前时间） > ", ctx.as_of.isoformat())
            debate = ask("多空轮次（1—3；回车使用配置） > ", str(settings.decision.debate_rounds))
            if cutoff is None or debate is None:
                return None
            risk = horizon = profile = position = None
            if mode == "full_decision":
                risk = ask("风险轮次（1—3） > ", str(settings.decision.risk_rounds))
                horizon = ask("决策期限 > ", "未来 1—3 个月")
                profile = ask("风险偏好（unspecified/aggressive/balanced/conservative） > ", "unspecified")
                position = ask("持仓背景（unknown/not_held/held） > ", "unknown")
                if any(v is None for v in (risk, horizon, profile, position)):
                    return None
            options = dict(mode=mode, symbol=selected, source="terminal_menu", analysts=roles, as_of=cutoff,
                debate_rounds=int(debate), risk_rounds=int(risk) if risk else None, horizon=horizon,
                risk_profile=profile, position_context=position, session_id=session_id,
                resolution_source_requests=ctx.source_requests)
            draft = SelectedScope(goal=MODES[mode], symbol=selected, as_of=datetime.fromisoformat(cutoff), analysts=roles,
                execution_mode=mode, selection_id="0" * 32, selection_source="terminal_menu", debate_rounds=int(debate),
                risk_rounds=int(risk) if risk else 0, decision_horizon=horizon or "未来 1—3 个月",
                risk_profile=profile or "unspecified", position_context=position or "unknown")
            cfg = config_for(draft, settings)
            from .workflow import validate_config
            validate_config(draft, cfg)
            write(f"模式：{MODES[mode]}；股票：{selected}；维度：{'、'.join(roles)}；截止：{cutoff}；多空 {debate} 轮；风险 {risk or '不执行'}；模型上限 {cfg.model_calls}，已用 0；名称查询 {ctx.source_requests} 次。流程通过选项修改，自由文本不改变模式。")
            answer = ask("操作（开始／修改／取消） > ")
            if answer is None:
                return None
            if answer == "开始":
                return select(settings, **options)
            write("返回配置；没有启动研究。")
    except (EOFError, KeyboardInterrupt):
        write("已取消研究配置，返回普通聊天。")
        return None
    except ValueError as exc:
        raise AgentError("INVALID_RESEARCH_CONFIG", "配置格式无效，请检查日期、轮次和枚举，重新打开 /research") from exc
