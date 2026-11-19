# 对照《AI教程.pdf》的代码审视

日期：2026-10-24；代码包版本：0.3.0。本文用于后续逐项讨论修复，本次没有修改业务代码。

依据是上级 Agent 目录中的 [AI教程.pdf](../../../AI教程.pdf)，书名《深入理解 AI Agent：设计原理与工程实践》，李博杰，v2.0，2026-09-23，共 313 个 PDF 页面。下文同时给出印刷页码和 PDF 阅读器页序，避免目录页偏移。

检查范围：CLI/聊天与 NLU 入口、AgentLoop、会话压缩、工具注册、研究与决策执行器、证据审阅、预算、Skills 及研究评估代码，并抽查相关测试和历史验收记录。这是核心路径的静态审视加离线探针，不是全部模块的完整审计；没有调用真实模型、数据接口，也没有重新验收训练模型。

## 总体判断

项目已经落实了教程的不少原则：自建模型—工具反馈循环、原始证据留存、按需 Skills、角色工具权限、共享预算、检查点和可恢复执行。主要差距在于：**反馈能否真正被下一步利用、上下文是否保住用户约束，以及复杂编排是否有质量收益的证据。**

教程 §1.2.4 明确认可固定工作流，不能将“存在固定步骤”直接判为错误。权限、时间截止、证据授权、不可变产物和用户显式选择是应保留的边界；下面区分已复现缺口和待实验验证的架构取舍。

| 编号 | 优先级 | 发现 | 证据强度 |
| --- | --- | --- | --- |
| A1 | P1 | 压缩会丢失中途用户澄清及部分决策过程 | 离线复现 |
| A2 | P1 | 决策角色读取技能后，提交错误可能没有修正预算 | 离线复现预算机制；执行器配置静态核对 |
| A3 | P1 | 普通回复的完成检查没有验证关键事实与答案一致 | 离线复现；属于已有输出策略的质量缺口 |
| A4 | P2 | 无进展检测只识别相邻重复，漏掉交替失败循环 | 离线复现 |
| A5 | P2 | 决策阶段重试没有向新尝试传递上次错误反馈 | 静态确认 |
| A6 | P2 | 固定辩论与多角色链的增益尚缺实验证据 | 架构取舍，不能据此断言质量更差 |
| A7 | P1（评估） | 研究质量评估尚未形成可用于选架构的闭环 | 代码及当前可见记录；不推断外部未提供结果 |

P1 表示建议先讨论；P2 表示随后处理。它们是本次审视的优先级，不沿用旧需求中 P0/P1 的功能分期。

## A1：上下文压缩保住了证据索引，但会丢失中途用户要求

**教程依据：** §2.7.4–2.7.6，印刷 p.70–71 / PDF p.78–79。压缩应保留关键约束、决策理由、失败路径和来源；保留文件不等于让模型掌握当前任务状态。

**代码：** [session.py](../../src/finagent/session.py) `compact_messages`，第 31–61 行；触发点在 [agent.py](../../src/finagent/agent.py) 第 319–326 行。

压缩保留初始 system/user 前缀和最后三个 assistant 组，中间替换成范围与证据索引。索引有工具状态和错误码，但没有归纳中途用户补充、已排除方案、当前发现及下一步。落在被删区间、且没有写进 `ctx.scope` 的用户要求会消失。例如“只比较现金流，不要估值”不是证券或日期字段，不能指望范围状态替它保存。

**本次复现：** 初始目标后加入上述澄清，再加入四条 assistant 消息；调用压缩后 `compacted=true`，澄清文本不在结果中。原日志还在磁盘，但后续模型上下文已经缺失这条要求。

**建议讨论：** 先保证所有仍生效的用户约束都进入可追溯的任务状态，再考虑结构化进展摘要；保持工具消息配对和来源 ID。并非必须增加一个压缩 Agent。

**修复验收：** 强制多次压缩后仍保留最新澄清、否定条件和未完成事项；后续输出遵守要求；历史证据不因摘要取得新权限。

## A2：按需技能与提交修正争用同一小额预算

**教程依据：** §2.5，印刷 p.57–61 / PDF p.65–69；§5.1.5，p.128–129 / PDF p.136–137；§10.2，p.274–275 / PDF p.282–283。工具反馈必须留有修正空间，步骤预算应匹配任务复杂度。

**代码：** [decision_runner.py](../../src/finagent/research/decision_runner.py) 第 60–65 行将多数决策角色限制为两次模型调用，投资组合经理四次，`submit_reserve=1`；[agent.py](../../src/finagent/agent.py) 第 342–346 行在最后预算只暴露 `submit_analysis`；[registry.py](../../src/finagent/tools/registry.py) 第 55–58 行允许最多两次提交尝试。

两次调用中，第一次读取 Skill，第二次才能依据 Skill 提交。第二次若提交参数错误，虽然工具返回了错误，第三次模型调用已没有预算；读取 Skill 后再回读证据或资源也受到限制。“最多一次修正”只是允许次数，不是实际保留了修正机会。

**本次复现：** 用同样的 `model_calls=2`、`submit_reserve=1` 运行真实 AgentLoop，模型依次计划读取技能、错误提交、修正提交。实际停在第二次调用，`MODEL_BUDGET`，提交尝试只有一次。现有 [test_skills.py](../../tests/test_skills.py) 覆盖读取后一次正确提交，没有证明读取后还能修正。

**建议讨论：** 分别预算方法加载、必要回读、首次提交和错误修正；或将可复用方法缓存与实际计费预算协调。增大额度前仍应保留总预算上限，并与 A6 的角色合并实验一起评估。

**修复验收：** 加载技能后首次提交失败，下一轮可依据错误纠正；成功一次提交不消耗额外调用；总预算和取消仍严格生效。

## A3：自然语言结束与事实正确之间缺少验证环节

**教程依据：** §5.1.4，印刷 p.128 / PDF p.136；§7.2–7.3，p.188–190 / PDF p.196–198。应验证用户目标与实际结果，运行状态不能替代结果质量。

**代码：** [terminal.py](../../src/finagent/terminal.py) `read_terminal_response`、`response_records`，第 29–97 行；[agent.py](../../src/finagent/agent.py) 第 424–438、480–486 行。

普通非空文本默认 `completed`。宿主检查可用观测、必需工具、缓存和少数完整性缺口，但不比较正文关键数值与观测是否一致。项目正确地把 `outcome_verified` 设为 false；问题是错误仍可直接呈现给用户，不能从 completed 推出任务答对。

**本次复现：** Mock 行情明确返回价格 10，模型正文回答“合成测试：价格为999元”。真实 AgentLoop 返回 `completed`，`completion_warnings=[]`，`outcome_verified=false`。这是合成输入，不是真实市场价格或真实模型错误率。

**边界：** [回复与结束行为](../features/OUTPUT.md) 说明自然语言输出是已有明确产品选择。不建议退回强制 JSON 或对每句话套校验器。研究路径已有引用核验与语义审阅，也不能说整个项目“完全不验证”。

**建议讨论：** 保持自然语言展示，内部为价格、单位、同比等关键事实保留字段绑定和可验证依据；对可确定的不一致做校验或有限修正。解释和因果判断另用抽样语义评估，避免单纯依靠数字出现匹配。

**修复验收：** 错价、错单位、错公司不能仅因有工具观测就作为正确结果通过质量验收；合理舍入、普通寒暄和有效部分回答仍正常工作。

## A4：无进展检测漏掉交替失败

**教程依据：** §5.1.5，印刷 p.128–129 / PDF p.136–137。故障恢复应识别重复调用模式、失败计数和实际进展，而不只是等待预算耗尽。

**代码：** [agent.py](../../src/finagent/agent.py) 第 397–406 行：签名包含工具名、参数和结果，只与紧邻的 `last_signature` 比较；不同签名会将计数重置为 1。

**本次复现：** 两个不存在的工具 A/B 交替调用六次，每次得到相同类别的拒绝；没有触发 `NO_PROGRESS`，最后为 `MODEL_BUDGET`。总预算仍然阻止无限执行，因此这是提前止损不足，不是无限循环漏洞。

**建议讨论：** 在有限窗口内跟踪调用指纹、错误类别和新证据/范围变化；允许有理由的切源和纠参，识别 A/B/A/B 这类重复失败。

**修复验收：** 无变化的交替失败能提前停止；参数修正后成功、有新资料的再次查询不被误杀。

## A5：跨尝试重试没有携带上次失败反馈

**教程依据：** §5.1.5，印刷 p.128–129 / PDF p.136–137。先区分错误是否值得重试，参数或合同错误需要改变输入或策略。

**代码：** [decision_coordinator.py](../../src/finagent/research/decision_coordinator.py) 第 95–161 行。异常分支记录错误码，除取消、预算和少数状态错误外可进入第二次尝试。下一次调用 `runner.run` 仍传原来的 `question/inputs/bundle`；[decision_runner.py](../../src/finagent/research/decision_runner.py) 会新建上下文和循环，没有把上次拒绝的产物与具体校验错误装入新目标。

一次循环内部已经会反馈工具错误；缺口发生在循环失败后重启整个角色时。这可能只是在相同条件下再次采样，尤其容易与 A2 的预算耗尽叠加。不是说再次采样一定失败，而是它没有利用已经获得的诊断信息。

**建议讨论：** 网络瞬时失败可以有限重试；合同错误保留失败输出、必要的结构化错误及恢复指令；确定性权限错误直接结束或改变合法策略。不要盲目增加尝试次数。

**修复验收：** 故意注入一次合同错误，下一尝试确实收到对应反馈并能改正；取消不重试，不重复不可逆动作，不把无权限证据带入新尝试。

## A6：多角色链的复杂度超前于已证明的收益

**教程依据：** §1.2.2–1.2.4，印刷 p.21–22 / PDF p.29–30；§10.2–10.3，p.274–275 / PDF p.282–283。先从简单方案开始；多 Agent 应通过额外信息、独立反馈或权限隔离提供可测收益。角色差异主要是方法或风格时，可比较 Skill 方案。

**代码：** [workflow.py](../../src/finagent/research/workflow.py) `plan` 第 27–46 行固定构造多空轮次、经理、交易员和三方风险链；[decision_coordinator.py](../../src/finagent/research/decision_coordinator.py) 第 241–260 行按阶段运行；[decision_runner.py](../../src/finagent/research/decision_runner.py) 第 70–105 行给决策角色主要提供已授权证据回读、交接回读、技能和提交工具，并投影此前产物。

专家取数、交叉审阅及最多两个补查任务确实能引入新观测，不能把整条链称为“纯空谈”。较明确的讨论对象是后半段：在冻结材料上增加多空和风险轮次，不自动带来新的外部事实；重复投影此前产物还会增加上下文与串行等待。不同角色也可能发现真实逻辑矛盾，这种收益需要测量。

**判断性质：** 这是待验证的设计取舍，尚不能断言固定链不符合教程，或单 Agent 一定更好。当前显式研究模式是已有产品边界，本次未删除或绕过。

**建议实验：** 用同一批冻结材料、模型与总 token 上限比较单 Agent + Skills、专家并行 + 一次证据审阅、当前一轮决策链、当前两轮决策链。分别统计关键错误、可支持主张、未知项处理、耗时和成本。只保留有稳定收益或有明确隔离需求的角色。

**决策标准：** 先有可重复收益再增加轮次；如果采用按缺口补查或提前停止，也必须遵守用户选择的范围和总预算，不能自行扩大研究授权。

## A7：质量评估仍缺少完成标注、重复采样和架构决策的闭环

**教程依据：** §7.2.2，印刷 p.189 / PDF p.197；§7.4–7.7，p.191–208 / PDF p.199–216；§10.2，p.274 / PDF p.282。区分偶尔成功与稳定交付，按实际任务评估，并用结果选择复杂度。

**代码：** [research/evaluation.py](../../src/finagent/research/evaluation.py) 的 `summarize_runs` 留出 `human_support`、`critical_error`、`support_rate=None` 和 `pending_human_review`；[evaluation21.py](../../src/finagent/research/evaluation21.py) 有六案例、四配置的冻结对照与统一预算，但一次矩阵每个组合执行一次。配套脚本为 [evaluate_phase2.py](../../scripts/evaluate_phase2.py) 与 [evaluate_v21.py](../../scripts/evaluate_v21.py)。

这些入口是正确基础，不是“没有评估”。缺口是当前实现没有在该流程内完成主张标注、评分汇总及同任务重复采样统计；本次可访问的 `.finagent` 中也未找到可用于证明当前架构收益的 comparison/matrix/acceptance JSON。历史文档明确保留真实质量待验收项；外部环境可能另有结果，本次无法确认。

**影响：** 离线协议测试、NLU 的语义帧指标和单次研究成功，分别验证不同问题，不能互相替代，也不能证明两轮辩论值得额外开销。

**建议讨论：** 先补一小组经过人工核验的研究案例与逐项评分规则，再完成上述对照标注。固定每种配置的独立运行次数，保留所有失败，报告单次成功率、连续多次全部成功的比例、样本量和不确定性。暂不需要增加训练或评审 Agent 来制造新的复杂度。

**验收：** 同一输入可以复算评分；不同配置使用相同预算口径；真实与合成结果分开；新增工作流步骤必须指出针对的稳定失败及实测改善。

## 已符合教程、应保留的部分

- AgentLoop 将工具错误作为观测回传，保存配对消息和检查点；上述缺口不否定这一基础。
- 大结果落盘并按 ID 回读，Skills 先发现元数据、按需加载正文和资源，符合渐进式披露。
- 工具白名单、证据授权、截止时间、受限程序与不可变交接是宿主边界，不应为了“更自主”删掉。
- 专家上下文隔离、有界并发、共享预算和 DataBroker 单次并发取数复用具有明确工程用途。
- 代码明确区分执行状态、内容状态和 `outcome_verified`，历史记录也区分真实验证与合成回放。

未使用向量数据库、RL、自动长期记忆或任意代码执行，不单独列为“不符合教程”。这些能力是否需要，应由当前任务和评估决定。NLU 候选模型已获允许使用的事实也不在本次修改范围内。

## 建议下一次讨论顺序

1. 先确定 A1 的“必须保留状态”和 A2 的“最小修正预算”，修复目标最清晰。
2. 讨论 A3：保持自然语言体验的同时，哪些关键结果必须有内部事实绑定。
3. 处理 A4/A5 的停止与恢复反馈，减少无效调用。
4. 完成 A7 的小规模质量基线，再用 A6 的对照结果决定是否精简角色链。

## 本次验证与复现

四个探针均使用现有源码、合成模型和 Mock HTTP，实际观测如下；它们验证控制流缺口，不估计真实模型失败率：

| 探针 | 结果 |
| --- | --- |
| 中途澄清后压缩 | `compacted=true; clarification_retained=false` |
| A/B 未知工具交替失败六次 | `calls=6; error=MODEL_BUDGET` |
| 读取技能、错误提交、计划修正 | `calls=2; submission_attempts=1; repair_called=false` |
| 观测价格 10、正文价格 999 | `status=completed; warnings=[]; outcome_verified=false` |

以下是本次实际使用的复现脚本。保存为 `/tmp/finagent-audit-probes.py`，在项目根目录使用已安装依赖的解释器执行 `PYTHONPATH=src:. python /tmp/finagent-audit-probes.py`。脚本依赖本仓库 `tests.support`，临时数据在结束时清理。

```python
import json, tempfile
from pathlib import Path
from finagent.agent import AgentLoop, RunRequest
from finagent.config import Limits
from finagent.runtime import ExecutionContext
from finagent.session import compact_messages
from finagent.data.evidence import EvidenceStore
from finagent.tools.registry import ToolRegistry, Tool
from finagent.tools.skills import register_skill_tools
from finagent.research.profile import AgentProfile
from finagent.config import StrictModel
from finagent.data.contracts import Observation
from tests.support import ScriptedModel, call_reply, ServiceCase, tencent_body
from finagent.models import ModelReply
import httpx

with tempfile.TemporaryDirectory() as temp:
 store=EvidenceStore(Path(temp)); ctx=ExecutionContext()
 messages=[{'role':'system','content':'system'},{'role':'system','content':'initial'}, {'role':'user','content':'分析公司'}]
 messages += [{'role':'assistant','content':'请补充要求'}, {'role':'user','content':'只比较现金流，不要估值'}]
 messages += [{'role':'assistant','content':str(i)} for i in range(4)]
 compacted, changed=compact_messages(messages,ctx,store)
 print(json.dumps({'probe':'clarification_compaction','compacted':changed,'clarification_retained':'只比较现金流' in json.dumps(compacted,ensure_ascii=False)},ensure_ascii=False))

 model=ScriptedModel(*(call_reply((str(i),'missing_a' if i%2==0 else 'missing_b',{})) for i in range(6)))
 result=AgentLoop(model,ToolRegistry(),store).run(RunRequest('测试',require_data=False), ExecutionContext(Limits(model_calls=6)))
 print(json.dumps({'probe':'alternating_no_progress','calls':result.model_calls,'error':result.error_code}))

 class Submission(StrictModel):
  summary: str
 registry=ToolRegistry(); register_skill_tools(registry, ['bull-thesis'])
 def submit(args,ctx):
  ctx.submitted_analysis={'summary':args.summary}
  return Observation(status='ok',source='host',data={'accepted':True})
 registry.register(Tool('submit_analysis','提交',Submission,submit,False))
 model=ScriptedModel(call_reply(('skill','read_skill',{'name':'bull-thesis'})),call_reply(('bad','submit_analysis',{})),call_reply(('fix','submit_analysis',{'summary':'已修正'})))
 ctx=ExecutionContext(Limits(model_calls=2));ctx.submit_reserve=1
 result=AgentLoop(model,registry,store,profile=AgentProfile('bull','测试')).run(RunRequest('测试',require_data=False),ctx)
 print(json.dumps({'probe':'skill_then_invalid_submit','calls':result.model_calls,'error':result.error_code,'submission_attempts':ctx.submission_attempts,'repair_called':model.index==3}))

case=ServiceCase();case.setUp()
try:
 ctx=ExecutionContext()
 case.handler=lambda _:httpx.Response(200, content=tencent_body(price='10',stamp=ctx.as_of.strftime('%Y%m%d%H%M%S')))
 model=ScriptedModel(call_reply(('quote','get_quote',{'symbol':'600519.SH'})), ModelReply({'role':'assistant','content':'合成测试：价格为999元。'}))
 result=AgentLoop(model,case.registry,case.store).run(RunRequest('查询合成行情'),ctx)
 body=json.loads((result.run_dir/'result.json').read_text())
 print(json.dumps({'probe':'wrong_visible_price','status':result.status,'answer':result.answer,'outcome_verified':body['outcome_verified'],'warnings':body['completion_warnings']},ensure_ascii=False))
finally:case.tearDown()
```
