# v3 重建与迁移记录

日期：2026-10-30。对应 [需求方案](../requirements/FinSight_v3.0_Agent_Harness重建需求方案.md)。

## 基线与保护

- 核验参考清单中 57 个文件；保留 MIT 来源与项目原有许可证/NOTICE。
- 重构前源码、测试、配置已打包，交付副本在 [baseline_v2_source.tar.gz](../../.finagent/v3_acceptance/baseline_v2_source.tar.gz)，用于迁移比较；不包含 `.env`，不是生产运行依赖。
- 初始本机 411 项测试有 2 个失败、25 个错误，主要错误为缺少可选交易日历；该依赖已经补齐。
- v3 存储在 `.finagent/v3/`，原 `evidence/`、`results/`、旧 sessions、runs、research 和研究 SQLite 不被迁移覆盖。
- 当前工作区没有可用 Git 历史，变更检查依赖旧源码归档和文件清单，不宣称已创建提交。

## P0–P5 实施内容

| 阶段 | 工作包与落点 | 验证 |
| --- | --- | --- |
| P0 | D00–D02：源码基线、旧测试运行、来源映射、旧实现备份 | 参考文件 hash；基线日志 |
| P1 | D03–D05：统一 Kernel、动态工具池、provider、持久 Session Host | 持续追问、协议配对、默认入口 |
| P2 | D06–D08：NLU 默认关闭/可选 hint、持续任务状态、多对象证据、单主模型 | A01–A13、旧金融/消息回归 |
| P3 | D09–D12：Skills、压缩、记忆、任务、后台、cron、团队、worktree、stdio MCP、Workflow、Goal | A14–A27、跨模块运行 |
| P4 | D13–D14：Research Service、角色统一 Kernel、旧记录只读/兼容、独立预算 | 两种研究模式、trace、恢复、原始模型错误传播 |
| P5 | D15–D17：移除 run/chat 路由、更新配置/文档/依赖、安装检查和质量入口 | 完整回归、构建、真实模型检查；结果单列 |

## 有意改变的旧行为与测试

不是恢复全部 v2 行为再加开关：

- `tests/test_chat.py` 改为持续会话测试。旧“每条输入独立 RunResult/独立证据/重新预算”的断言由会话继承、澄清预算续接、取消和协议测试替代。
- 默认 CLI 的测试改为无 `chat` 子命令；`run/chat/resume` 旧命令检查迁移提示。PTY 中文编辑测试保留并使用新入口。
- NLU 的两条“概念标签拒绝金融工具”断言改为“建议不拦截工具”；训练解析、实体、候选、指纹与诊断测试继续保留。
- 股票切换测试改为检查同一会话保留先前证据、正确切换焦点和按各自输入计算，不再要求清空前一家证据。
- 非相邻 A/B/A/B 失败测试改为有限窗口止损。
- 研究角色请求重试从一次计费改为有界重试实际计费；默认决策角色预算增加到四次，默认总模型预算 128、保留池 64，测试按当前预算检查分池。
- 旧固定回放 E01 的收盘复盘必需工具在 fixture 中显式声明，取代公共 loop 的关键词推断；原计算/覆盖检查不放宽。
- 普通业务代码不再调用旧 NLU prepare/research_route；这些历史语义合同仍供训练诊断与旧验收入口使用，不是新版准入。

## 验证记录

最终源码完整回归 **433 项全部通过**，耗时 34.244 秒，见 [日志](../../.finagent/v3_acceptance/full_tests.log)。其中 `test_harness_v3.py` 有 32 项跨模块测试，覆盖持续会话、纠参、预算、压缩恢复、后台通知、团队认领、真实 Git worktree、真实 stdio MCP、Workflow、Goal 与记忆等。模型和金融数据的离线替身均标记 synthetic。

从全新 staging 构建 `finagent-3.0.0-py3-none-any.whl`，再安装到独立临时目录，以 Python `-I` 启动验证。实际导入路径指向该安装目录，包内容摘要与交付源码一致；发现 **64 个工具、19 个 Skills**，持续会话可提交并完成问候。在禁止导入 `finagent.nlu` 和 `tokenizers` 的条件下通过，说明默认主线没有强制 NLU 依赖。安装包包含 LICENSE、NOTICE 和 THIRD_PARTY_NOTICES；`pip check` 通过。见 [安装核验](../../.finagent/v3_acceptance/install_verification.json) 和 [构建日志](../../.finagent/v3_acceptance/wheel_build.log)。本轮执行环境为 Linux、Python 3.13；没有声称逐个验证全部支持的 Python 版本和操作系统。

57 个参考文件在交付前再次逐个校验，均与固定清单一致；运行时不导入相邻项目。逐章行为映射见 [HARNESS_PARITY.md](../overview/HARNESS_PARITY.md)。新状态单独存储，旧源码归档和旧数据保留；未完成的旧运行仅兼容查看，不假装能够无损重放。

### 真实模型验证与未决验收

使用原有配置的 `deepseek-flash`，没有替换主模型或修改凭证。首先完成问候、承接追问、查询最新可得行情的三条连续真实调用；结果与当时工具时间保存在 [live_smoke.json](../../.finagent/v3_acceptance/live_smoke.json)。这是连通和工具使用检查，没有逐句人工核验全部金融叙述。

另完成 20 个用例 × 3 种配置 × 3 次重复的 **180 条真实模型对照记录**：旧源码、v3 默认关闭 NLU、v3 开启建议。主模型真实调用，行情/财报来源使用冻结的合成数据；该对照不代表真实市场研究质量。每次用户输入最多 20 次模型调用，个案总额度乘输入数，NLU 尝试也计入额度；输出上限 1200 tokens、来源请求总上限 24。保留全部原始输出、耗时、模型调用、用量、失败与实现摘要，见 [原始记录](../../.finagent/v3_acceptance/model_comparison.jsonl) 和 [主批摘要](../../.finagent/v3_acceptance/model_comparison_primary_summary.json)。

修正合成证券搜索响应格式后，对 C07/C08/C15 补跑 27 条，见 [修正批次](../../.finagent/v3_acceptance/model_comparison_fixture_v2.jsonl)。[合并摘要](../../.finagent/v3_acceptance/model_comparison_summary.json) 保留 207 条输入记录，选择 180 个配置/用例/重复组合，修正批次覆盖相同组合的统计；两个原始文件均保留。

这些数据不能作为最终版本智能程度提升的定论：部分样本在开发过程中发生实现变化，文件记录了对应摘要；配置批次顺序执行；来源是合成数据；人工语义完成率和严重错误率均待复核，保持 `null`，没有用执行成功状态代替质量评分。旧路径经常在进入主模型之前结束，耗时和调用量也不能直接解释为效率更高。

补测后服务连续失败，一次独立极小请求确认返回 **HTTP 402**，见 [服务检查](../../.finagent/v3_acceptance/model_service_check.json)。此前部分失败仅保存一般错误，不能把它们逐条归因为 402。没有继续自动消耗调用，也没有进行付款或凭证变更。最终版本的完整同版本真实对照及人工质量签收仍待完成；代码重建、离线回归、打包和隔离安装已完成。机器可读交付记录见 [release_report.json](../../.finagent/v3_acceptance/release_report.json)。
