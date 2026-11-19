# Trading 参考提示词适配记录

本次将相邻 Trading 项目 `tradingagents/agents/` 中的研究方法适配到 FinAgent。运行时继续从本项目的资源文件加载提示词，不读取或导入 Trading。

## 角色对应

| Trading 参考文件（相对 agents/） | FinAgent 方法资源（相对 src/finagent/） | 补充重点 |
| --- | --- | --- |
| analysts/market_analyst.py | skills/technical-analysis/SKILL.md | 趋势、量价、波动、基准强弱与指标有效性 |
| analysts/fundamentals_analyst.py | skills/fundamentals-analysis/SKILL.md | 成长来源、盈利质量、资产负债与可比估值 |
| analysts/news_analyst.py | skills/news-events/SKILL.md | 事件去重、时间线、预期差与公司传导 |
| analysts/social_media_analyst.py | skills/market-sentiment/SKILL.md | 资金、量价、热度、新闻标签及信号背离 |
| analysts/policy_analyst.py | skills/policy-impact/SKILL.md | 政策层次、有效状态、适用条件与业务映射 |
| researchers/bull_researcher.py | research/prompts/bull.md | 成长与催化支持链、空头回应及失效条件 |
| researchers/bear_researcher.py | research/prompts/bear.md | 下行路径、多头假设检验及风险反证 |
| managers/research_manager.py | research/prompts/research_manager.md | 逐项论点裁决、评级含义与条件计划 |
| trader/trader.py | research/prompts/trader.md | 评级到行动的转换、前提、触发与复核 |
| risk_mgmt/aggressive_debator.py | research/prompts/risk_aggressive.md | 机会成本、催化兑现及上行假设压力测试 |
| risk_mgmt/conservative_debator.py | research/prompts/risk_conservative.md | 下行、退出约束、治理与证据脆弱点 |
| risk_mgmt/neutral_debator.py | research/prompts/risk_neutral.md | 统一时间口径、定位分歧与接受边界 |
| managers/portfolio_manager.py | research/prompts/portfolio_manager.md | 全风险覆盖、关键反对意见及最终方向一致性 |
| quality_gate.py | research/prompts/quality_gate.md | 数据时效、覆盖和语义支持的逐问题收口 |

当前将通用方法与独立研究角色分开：8个通用分析技能和1个 `deep-research` 入口技能继续采用 YAML 元数据与按需读取；多空、经理、交易与风险方法迁入研究模块，由执行器直接加载本角色正文。交叉审阅与旧版综合报告方法分别保留在 `research/prompts/review.md`、`synthesis.md`，不再是独立技能。

`analysts/methods.py` 保留固定角色职责与分析师技能映射；技术分析师角色提示词版本为 `1.3`，其他分析师为 `1.2`，新决策产物的 `role_prompt_version` 为 `2.1.3`。合同与工作流版本维持原值，既有产物不重写。运行机制和扩展方式见 [Skills 机制](../features/SKILLS.md)。

## 适配原则

- 参考项目的详细检查框架用于选择最重要的问题，不能变成必须填满的数据清单。继续遵守现有模型预算、专家正文与主张上限，并优先处理指定补查问题。
- 工具名称与能力以 FinAgent 实际授予的 schema 为准。技术分析采用现有均线、量价及基准计算，不要求调用参考项目的指标工具；财务比率仍由绑定事实的程序计算。
- 保留冻结范围、原始证据引用、交接哈希、结构化提交与弃权传播。参考项目的自由文本、最终交易口号及强制表态不迁入提交合同。
- 不照搬散户占比、统一 PE/PEG 阈值、政策价格托底或固定媒体排名。交易规则须由授权材料核验证券类别及历史有效期，不把参考提示词中的规则数字硬编码为事实。
- Trading 的 hot_money_tracker 与 lockup_watcher 在 FinAgent 尚无独立对应角色，本次不新增对应金融分析角色或金融取证工具。资金与热度方法在情绪角色中适配；解禁、减持及治理风险只在现有授权披露主张支持时交由新闻、基本面、多空与风险角色讨论。
- 质量审阅依据证据支持与真实缺口，不采用参考项目按篇幅、表格和缺失项数量评分的机制。质量门只收口宿主已登记的问题，门控仍由宿主计算。

## 验证边界

原提示词增强通过131项研究专项回归。Skills 运行机制迁移另增发现、分阶段读取、权限、任务恢复和原生执行器测试；当前验证结果见 [Skills 机制](../features/SKILLS.md)。研究专项回归命令如下：

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
  tests.test_research tests.test_research_v2 tests.test_research_v21 \
  tests.test_research_v22 tests.test_v21_audit tests.test_research_failures
```

离线验证用于确认调用与合同兼容，不能证明真实模型的研究质量已提升；实际质量仍需使用相同研究案例进行模型对比和人工审阅。
