from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .agent import AgentLoop, RunRequest
from .config import load_settings
from .data.evidence import write_json
from .data.service import DataService
from .data.symbols import normalize_symbol
from .demo import run_demo
from .errors import AgentError
from .models import CompatibleChatModel, LazyChatModel
from .runtime import ExecutionContext, now
from .tools.perception import build_registry


def emit(body):
    print(json.dumps(body, ensure_ascii=False, indent=2, default=str, allow_nan=False))


def main(argv=None, *, settings_override=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="FinSight 自研金融研究 Agent")
    parser.add_argument("--config", help="配置文件；默认向父目录寻找finagent.toml，相对运行路径以配置所在目录为准")
    commands = parser.add_subparsers(dest="command", required=True)
    intent = commands.add_parser("intent-parse", help="仅诊断原版Qwen的工具类别分类，不调用业务工具")
    intent.add_argument("text")
    intent.add_argument("--history", action="append", default=[], help="之前的话轮，可重复，最多保留最近三条")
    intent.add_argument("--symbol")
    intent.add_argument("--rules-only", action="store_true", help="仅检查无模型兜底规则")
    research = commands.add_parser("research", help="五维深度研究或指定分析师专项研究")
    research.add_argument("goal", nargs="?", default="深入研究并给出最终研判")
    research.add_argument("--goal", dest="named_goal")
    research.add_argument("--mode", choices=("deep_research", "full_decision"), help="显式选择研究报告或完整决策")
    research.add_argument("--pipeline", choices=("research", "trading"))
    research.add_argument("--debate-rounds", type=int, choices=(1, 2, 3))
    research.add_argument("--risk-rounds", type=int, choices=(1, 2, 3))
    research.add_argument("--horizon")
    research.add_argument("--risk-profile", choices=("unspecified", "aggressive", "balanced", "conservative"))
    research.add_argument("--position-context", choices=("unknown", "not_held", "held"))
    research.add_argument("--symbol", help="研究证券；可省略并从目标中的明确名称解析")
    research.add_argument("--depth", choices=("deep", "specialist"))
    research.add_argument("--analysts", nargs="+", choices=("technical", "sentiment", "news", "fundamentals", "policy"))
    research.add_argument("--as-of", help="带时区的历史截止时间")
    research.add_argument("--market-window", type=int, default=120)
    for action in ("research-status", "research-report", "research-resume", "research-trace"):
        sub = commands.add_parser(action, help="查看研究状态、报告或显式恢复")
        sub.add_argument("research_id")
        if action == "research-trace":
            sub.add_argument("--stage", choices=("all", "debate", "risk", "research_manager", "trader", "portfolio_manager", "quality_gate"), default="all")
    for action in ("import-policy", "import-research-signal"):
        sub = commands.add_parser(action, help="导入带来源的真实政策原文或市场信号，不调用模型")
        sub.add_argument("path")
        sub.add_argument("--manifest", required=True)
    commands.add_parser("tools", help="查看感知工具合同，无需模型密钥")
    accept = commands.add_parser("nlu-evaluate", help="真实模型加受控证券回放的离线业务验收，不执行研究")
    accept.add_argument("--model-dir")
    accept.add_argument("--endpoint")
    accept.add_argument("--model")
    accept.add_argument("--test", required=True)
    accept.add_argument("--oracle", required=True)
    accept.add_argument("--output", required=True)
    accept.add_argument("--mode-boundaries", help="evaluate_v22_boundaries.py 生成的当前实现模式边界报告")
    for action in ("nlu-check", "nlu-parse", "nlu-benchmark"):
        sub = commands.add_parser(action, help="Qwen3/Ollama 检查、JSON解析和性能测量；不执行金融研究")
        sub.add_argument("--model-dir")
        sub.add_argument("--endpoint")
        sub.add_argument("--model")
        sub.add_argument("--diagnostic", action="store_true", help="仅离线诊断允许未达标/烟测模型，不能启动研究")
        if action == "nlu-parse":
            sub.add_argument("text", nargs="?")
            sub.add_argument("--text", dest="named_text")
            sub.add_argument("--context", default="{}")
        if action == "nlu-benchmark":
            sub.add_argument("--samples", "--input", dest="samples", required=True, help="固定 JSONL 样本，每条含 text 和可选 context，至少500条")
            sub.add_argument("--output", default=".finagent/nlu_benchmark.json")
    tool = commands.add_parser("tool", help="直接调用一个工具，不调用模型")
    tool.add_argument("name")
    tool.add_argument("--args", default="{}", help="JSON参数")
    tool.add_argument("--as-of", help="带时区的截止时间")
    market_importer = commands.add_parser("import-market", help="显式导入有来源的行情JSON回放；完整标记必须存在原始记录中")
    market_importer.add_argument("path")
    market_importer.add_argument("--manifest", required=True)
    resume = commands.add_parser("legacy-run", help="只读查看旧运行记录；新版会话使用 --resume")
    resume.add_argument("run_id")
    session = commands.add_parser("session", help="读取/删除范围记忆或显式设置/删除长期偏好")
    session.add_argument("name")
    session.add_argument("--forget", action="store_true")
    session.add_argument("--preference", choices=("display_unit", "watchlist"))
    session.add_argument("--value", help="不传value则删除指定偏好")
    commands.add_parser("demo", help="合成模型/数据的离线演示，明确不算真实验收")
    evidence = commands.add_parser("evidence", help="人工查看证据；Agent只能读取本任务登记ID")
    evidence.add_argument("id")
    evidence.add_argument("--offset", type=int, default=0)
    probe = commands.add_parser("probe", help="低频顺序检查所选股票的数据源，不调用模型")
    probe.add_argument("--symbol", default="600519.SH")
    evaluate = commands.add_parser("evaluate", help="阶段一固定回放、在线数据与真实模型验收（分别记录）")
    evaluate.add_argument("--mode", choices=("replay", "online", "model", "audit", "all"), default="replay")
    evaluate.add_argument("--cases", nargs="+", choices=("D1", "D2", "D3", "D4", "D5"), help="只重测指定真实模型演示，既有失败记录保留")
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    service = model = None
    try:
        settings = settings_override or load_settings(args.config)
        if args.command == "intent-parse":
            from .intent_routing import IntentRouter, fallback_route, routing_context, social_rule
            context = routing_context([{"role": "user", "content": t} for t in args.history], args.symbol)
            if args.rules_only:
                result = social_rule(args.text) or fallback_route(args.text, context)
            else:
                result = IntentRouter(settings.intent).route(args.text, context)
            emit(result.model_dump(mode="json"))
            return 0
        if args.command == "nlu-evaluate":
            from .nlu.acceptance import evaluate as evaluate_nlu
            from .nlu.diagnostics import configured
            local = configured(args, settings)
            report = evaluate_nlu(local, local.nlu.model_dir, args.test, args.oracle, args.output, mode_boundaries=args.mode_boundaries)
            emit({k: v for k, v in report.items() if k != "cases"})
            return 0 if report["host_gates_passed"] else 2
        if args.command.startswith("nlu-"):
            from .nlu.diagnostics import run_command
            emit(run_command(args, settings))
            return 0
        if args.command.startswith("research-"):
            from .research.coordinator import ResearchCoordinator
            from .research.store import ResearchStore
            store = ResearchStore.from_settings(settings)
            if args.command == "research-trace":
                from .research.decision_reporting import read_trace
                emit(read_trace(store, args.research_id, args.stage))
                return 0
            if args.command == "research-status":
                emit(store.snapshot(args.research_id))
                return 0
            if args.command == "research-report":
                row = store.get(args.research_id)
                if not row["report"]:
                    raise AgentError("REPORT_UNAVAILABLE", "研究尚无报告")
                report_path = Path(row["report"]).resolve()
                if not report_path.is_relative_to(store.directory(args.research_id).resolve()):
                    raise AgentError("REPORT_DENIED", "报告路径无效")
                print(report_path.read_text(encoding="utf-8"))
                return 0
            from .research.selection import resume_selected
            result = resume_selected(settings, args.research_id, progress=lambda text: print(text, file=sys.stderr))
            emit(result)
            return 130 if result["status"] == "cancelled" else 0 if result["content"] == "complete" else 2
        if args.command in ("import-policy", "import-research-signal"):
            from .research.materials import ResearchMaterials, SignalImport
            from .research.contracts import PolicyImport
            from .data.evidence import EvidenceStore
            materials = ResearchMaterials(EvidenceStore(settings.data_dir))
            manifest = Path(args.manifest).read_text(encoding="utf-8-sig")
            body = materials.import_policy(args.path, PolicyImport.model_validate_json(manifest)) if args.command == "import-policy" else materials.import_signal(args.path, SignalImport.model_validate_json(manifest))
            emit({k: v for k, v in body.items() if k not in ("data", "blocks")})
            return 0
        if args.command == "research":
            from .research.entry import request_from_goal
            from .research.coordinator import ResearchCoordinator
            from .research.selection import select, MODES
            from .research.menu import menu
            if args.depth is not None or args.pipeline is not None:
                raise AgentError("MODE_CONFLICT" if args.mode else "MODE_REQUIRED", "--depth/--pipeline 已弃用；请使用 --mode，专项分析请用 run 或聊天")
            if args.mode is None:
                if not sys.stdin.isatty():
                    raise AgentError("MODE_REQUIRED", "非交互研究必须指定 --mode deep_research 或 full_decision")
                request = menu(settings, symbol=args.symbol, as_of=datetime.fromisoformat(args.as_of) if args.as_of else None)
                if request is None:
                    return 0
            else:
                if not args.symbol:
                    raise AgentError("SYMBOL_REQUIRED", "显式研究请提供 --symbol 股票代码；股票名称可在 /research 菜单中确认")
                request = select(settings, mode=args.mode, source="cli_flag", symbol=args.symbol,
                    goal=args.named_goal or args.goal, as_of=args.as_of, analysts=args.analysts,
                    debate_rounds=args.debate_rounds, risk_rounds=args.risk_rounds, horizon=args.horizon,
                    risk_profile=args.risk_profile, position_context=args.position_context, market_window=args.market_window)
            from .research.selection import config_for, SelectedScope
            cfg = config_for(SelectedScope(**request.model_dump()), settings)
            print(f"{MODES[request.execution_mode]}；{request.symbol}；维度 {'、'.join(request.analysts)}；截止 {request.as_of.isoformat()}；模型上限 {cfg.model_calls}；自由文本不改变所选流程。", file=sys.stderr)
            result = ResearchCoordinator(settings).run(request, progress=lambda text: print(text, file=sys.stderr))
            emit(result)
            return 130 if result["status"] == "cancelled" else 0 if result["content"] == "complete" else 2
        if args.command == "evaluate":
            from .evaluation import run_replay, run_online, run_models, run_audit
            reports = []
            if args.mode in ("replay", "all"):
                reports.append(run_replay(settings))
            if args.mode in ("online", "model", "all"):
                online = run_online(settings)
                reports.append(online)
                if args.mode in ("model", "all") and online["passed"]:
                    reports.append(run_models(settings, online, args.cases))
            if args.mode in ("audit", "all"):
                reports.append(run_audit(settings))
            emit({"passed": all(r["passed"] for r in reports), "reports": [
                {k: v for k, v in r.items() if k not in ("cases", "checks", "attempts", "latest_demonstrations")} for r in reports]})
            return 0 if all(r["passed"] for r in reports) else 2
        if args.command == "demo":
            result = run_demo(settings.data_dir)
            emit(result.__dict__)
            return 0
        service = DataService(settings)
        registry = build_registry(service)
        if args.command == "import-market":
            from .market import MarketImport, import_market
            spec = MarketImport.model_validate_json(Path(args.manifest).read_text(encoding="utf-8-sig"))
            value = import_market(Path(args.path), spec, service.store)
            emit(value.model_dump(mode="json"))
            return 0
        if args.command == "session":
            from .session import Conversation
            conversation = Conversation(service.store, args.name)
            if args.forget:
                conversation.forget()
            elif args.preference:
                conversation.preference(args.preference, args.value)
            emit(conversation.state)
            return 0
        if args.command == "legacy-run":
            import re
            if not re.fullmatch(r"[a-f0-9]{32}",args.run_id):
                raise AgentError("INVALID_RUN_ID","旧运行ID必须为32位小写十六进制")
            directory=(service.store.root/"runs"/args.run_id).resolve()
            if not directory.is_relative_to((service.store.root/"runs").resolve()):
                raise AgentError("INVALID_RUN_ID","旧记录路径越界")
            state=json.loads((directory/"state.json").read_text(encoding="utf-8"))
            result_path=directory/"result.json"
            emit({"state": state,"result":json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None,
                  "note": "旧运行记录仅供查看；新会话使用 finagent --resume SESSION_ID"})
            return 0
        if args.command == "tools":
            emit(registry.schemas())
            return 0
        if args.command == "evidence":
            if args.offset < 0:
                raise AgentError("INVALID_OFFSET", "offset不得小于零")
            emit(service.store.read(args.id, args.offset))
            return 0
        cutoff = datetime.fromisoformat(args.as_of) if getattr(args, "as_of", None) else now()
        ctx = ExecutionContext(settings.limits, as_of=cutoff, cutoff_explicit=bool(getattr(args, "as_of", None)))
        if args.command == "tool":
            result = registry.execute(args.name, args.args, ctx)
            result = service.store.save_result(result)
            emit(result.model_dump(mode="json"))
            return 2 if result.status == "error" else 0
        if args.command == "probe":
            symbol = normalize_symbol(args.symbol)
            end = now().date() - timedelta(days=1)
            checks = [("get_quote", {"symbol": symbol, "source": "tencent"}),
                      ("get_quote", {"symbol": symbol, "source": "sina"}),
                      ("get_daily_bars", {"symbol": symbol, "start": (end - timedelta(days=35)).isoformat(), "end": end.isoformat()}),
                      ("get_financial_statements", {"symbol": symbol, "statement": "income", "limit": 1}),
                      ("get_company_profile", {"symbol": symbol}),
                      ("get_stock_news", {"symbol": symbol, "start": (end - timedelta(days=14)).isoformat(), "end": now().date().isoformat()})]
            records = []
            for name, parameters in checks:
                context = ExecutionContext(settings.limits)
                result = service.store.save_result(registry.execute(name, parameters, context))
                records.append({"tool": name, "parameters": parameters, "result": result.model_dump(mode="json"), "source_requests": context.source_requests})
            path = settings.data_dir / "checks" / (now().strftime("%Y%m%d_%H%M%S") + ".json")
            write_json(path, {"checked_at": now().isoformat(), "checks": records, "requests": service.http.request_log})
            emit({"report": str(path), "checks": [{"tool": r["tool"], "parameters": r["parameters"], "status": r["result"]["status"], "error": r["result"]["error"], "snapshot_id": r["result"]["snapshot_id"]} for r in records]})
            return 2 if any(r["result"]["status"] == "error" for r in records) else 0
        raise AgentError("UNKNOWN_COMMAND", "未识别的维护命令")
    except (AgentError, ValidationError, ValueError, OSError) as exc:
        if isinstance(exc, AgentError):
            emit({"status": "error", "code": exc.code, "message": exc.message})
        else:
            emit({"status": "error", "code": "CONFIG_OR_INPUT_ERROR", "message": "配置、输入或本地文件无效（" + type(exc).__name__ + "）"})
        return 2
    except KeyboardInterrupt:
        emit({"status": "cancelled", "message": "用户中断运行"})
        return 130
    finally:
        if model:
            model.close()
        if service:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
