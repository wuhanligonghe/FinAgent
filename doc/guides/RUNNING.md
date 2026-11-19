# FinSight v3 运行与开发

2026-11-15 存储更新：当前源码的认证、Web 元数据、研究账本和向量缓存已改用 MySQL，Redis 与文件继续保留。正式迁移和服务切换已完成；初始化、旧库迁移和实测状态见 [MySQL 存储交付记录](../features/MYSQL_STORAGE.md)。下文历史 SQLite 路径按迁移前版本理解。

FinSight 在 learn-claude-code s01–s17 的 Harness 上扩展金融工具、证据、记忆和 Skills。默认是持续会话，深度研究单独启动。能力来源见 [对照表](../overview/HARNESS_PARITY.md)，迁移及验收见 [重建记录](../history/V3_REBUILD.md)。

## 安装与模型

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[calendar]'
finagent --help
```

Python 3.11+。PDF 导入可安装 `.[pdf]`；旧微调 Qwen 建议服务可安装 `.[nlu]`。项目已开启原版 Qwen3.5-9B 工具分类，模型安装、超时与诊断见[工具意图分类](../models/INTENT_ROUTING.md)。该入口无需 NLU 模型包或额外 Python 依赖；Ollama 不可用时使用规则兜底继续主流程。

会话运行层现以 Redis 为主存储。启动 CLI/Web 前准备 Redis，可在另一终端运行 `.venv/bin/python scripts/serve_runtime_redis.py`；本机已部署 `finsight-redis.service`。配置、首次旧数据导入和恢复规则见 [Redis 短期记忆](../features/REDIS_RUNTIME.md)。`--help` 等不创建会话的命令不要求模型或 Redis 已连接。

在 `finagent.toml` 的 `[model]` 或环境变量中配置 `FINAGENT_BASE_URL`、`FINAGENT_MODEL`、`FINAGENT_API_KEY`。配置目录 `.env` 的优先级低于已有环境变量；文件内容仅按字面值读取，不执行 Shell。支持 Chat Completions、Anthropic Messages，具体供应商需要真实联通验证。

`[nlu].enabled=false` 是默认值。开启后仅提供可纠正的理解建议；超时、模型缺失或预测失效记录诊断，不拒绝主模型查询。`nlu-*` 仍可独立诊断，其旧业务准入指标不代表 v3 主 Agent 质量。

`[harness].workspace` 是文件与 Shell 工作区，默认以配置目录为基准；数据目录、凭证和宿主策略不向通用文件工具开放。Shell 沙箱不可用时，只开放受限只读命令，任意命令会明确失败。配置 MCP 只接受宿主登记的 argv，不接受模型自带启动脚本。

## 持续会话

Web 登录独立于终端聊天。首次使用网页前先创建账号；账号、邀请码和旧数据迁移见[登录指南](LOGIN.md)。终端普通聊天入口保持原样。

```bash
finagent
finagent --session cashflow
finagent --resume cashflow
finagent --config /absolute/path/finagent.toml
finagent tools
```

`run`、`chat` 和普通 `resume RUN_ID` 已移除。无需选择问答/单步/专项模式；直接输入目标，后续追问、公司比较、更正和产物继续保存在同一会话。普通分析由主模型负责，必要时用 Skills、子 Agent 或团队。

命令：

- `/help`、`/exit`、`/cancel`：帮助、退出、取消当前工作。
- `/new 名称`、`/sessions`、`/resume 名称`：管理会话。
- `/compact`：归档并整理上下文，保留有效约束和回读路径。
- `/goal 条件`：设置目标并立即工作；`/goal` 查看，`/goal clear` 清除。没有 Goal 的普通对话不会额外调用判断器。
- `/tasks`、`/skills`、`/memory`、`/memory forget ID`：查看任务、技能、长期记忆或删除记忆。
- `/memory search 问题`：调试当前用户/项目范围的混合召回，查看 Embedding 降级状态；配置见 [长期记忆与召回](../features/MEMORY.md)。
- `/sources`、`/details`：来源、对象、时间、使用量和诊断。

后台完成、定时触发、队友消息会自动进入所属会话。用户可在模型执行中输入新要求，执行器在下个工具/模型边界接收；Ctrl+C 取消当前工作，空闲时退出。权限审批由终端协调者接收，后台任务不能抢占输入。多个进程不能同时打开同一会话。

聊天终端会显示主 Agent 的实际工具调用进度：`[工具] 调用 get_quote（symbol=600519.SH）`，随后显示返回状态、耗时及可用的来源/数据时间。部分数据、空结果和失败会分别标明；参数只预览有限字段，省略凭证、代码、文件正文和大段原始结果。模型调用 `todo_write` 时会显示 `[Todo]` 列表及状态更新；是否创建 Todo 仍由模型决定。如果本轮没有工具调用，终端会明确显示 `[工具] 本轮未调用工具。`；这不代表重新查询或核验过数据。隔离子任务内部和独立深度研究沿用各自的记录方式。

## 独立深度研究

```bash
finagent research --mode deep_research --symbol 600519.SH --goal '分析现金流质量'
finagent research --mode full_decision --symbol 600519.SH --goal '完整研究'
finagent research-status RESEARCH_ID
finagent research-report RESEARCH_ID
finagent research-trace RESEARCH_ID --stage risk
finagent research-resume RESEARCH_ID
```

可以在对话里直接说“给刚才这家公司做一份深度研究报告，重点分析现金流质量”。主模型使用 `start_deep_research` 发起独立后台任务，继承当前公司、截止时间、有效用户约束与研究窗口；无法唯一确认公司时先解析名称或澄清。启动结果包含独立预算和工作ID，可继续普通聊天。普通“深入看看”、功能咨询或报告追问不会因此自动启动研究。

聊天内 `/research 分析现金流质量` 同样可继承当前公司；也保留完整写法 `/research --mode deep_research --symbol 600519.SH 分析现金流`。单独输入 `/research` 查看模式和参数。`deep_research` 到研究经理收口，`full_decision` 继续交易研究与风险讨论；两者没有真实下单能力。

`/research-list` 列出本会话研究；`/research-status` 省略ID时查看最近发起的研究，也接受启动时返回的工作ID。后台阶段进度显示为 `[深度研究] ...`。完成通知唤醒所属会话后，主模型读取实际报告并给摘要；可继续追问“报告为什么这样判断”。`/research-report` 可省略ID或传工作ID，显式读过另一份报告后，后续追问沿用那份报告。长报告在模型工具中分页读取。

独立研究锁定自己的模式、证券、cutoff、预算与产物；聊天切换公司不影响它。报告读取明确返回原始范围，不把旧报告当实时事实。普通“深入看看”不会自动启动该流程。

同一用户启动请求的工具重试复用已有任务，不重复消耗一份研究预算；会话重启保留关联。`/cancel` 取消当前工作及本会话后台任务，原线程不能跨进程继续运行；需要恢复时使用 `/research-resume RESEARCH_ID`，仍受原研究范围和剩余预算约束。自然语言入口由主模型选择工具，宿主不按关键词自动切换普通分析流程。实现和验证见 [主对话与独立研究衔接](../features/DEEP_RESEARCH_CHAT.md)。

## 存储、复用与恢复

Redis 按数据根目录生成命名空间，保存会话检查点、任务图、后台记录、定时记录及带确认的通知，不再读写 `v3/runtime.sqlite3`。`v3/sessions/<id>/transcript.jsonl` 留存原始 transcript，outputs 保存大结果/归档；长期记忆仍保存在 `v3/memory/`，Workflow journal/快照位于 `v3/workflows/<session>/`。Redis 连接失败明确报错，旧 SQLite 仅由离线迁移读取，完整说明见 [Redis 短期记忆](../features/REDIS_RUNTIME.md)。

新会话保存完整实际对话，按需压缩，不截成四轮。静态财报在相同版本/期间/cutoff 下可以复用；行情检查时效。知道证据 ID 不等于获得读取权限。恢复不会自动重放执行状态未知的工具，旧进程线程标记 interrupted。

旧 `.finagent/evidence`、`results`、`runs`、`sessions`、`research` 和研究 SQLite 保留。`finagent evidence ID` 及研究查看命令可继续使用；财报 `library` 和 `import-document` 命令已移除；`legacy-run ID` 查看旧未完成运行。旧实现/协议不兼容时不冒充可恢复。

## 验证

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_harness_v3
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .
```

离线回归不代表金融研究质量已提升。真实模型对照应固定模型、来源/预算口径，保留全部失败与未核验项，详见重建需求与验收记录。

真实模型对照入口如下，会产生模型 API 费用；冻结的合成金融来源不访问真实行情，主模型和可选建议模型真实执行。该脚本不属于普通聊天的运行依赖。输出文件采用新文件名，避免覆盖交付记录。

```bash
.venv/bin/python scripts/evaluate_harness_v3.py \
  --modes baseline new new_hint --repeats 3 --parallel 3 \
  --baseline-archive .finagent/v3_acceptance/baseline_v2_source.tar.gz \
  --output .finagent/v3_acceptance/model_comparison_rerun.jsonl
.venv/bin/python scripts/summarize_harness_v3.py \
  --inputs .finagent/v3_acceptance/model_comparison_rerun.jsonl \
  --output .finagent/v3_acceptance/model_comparison_rerun_summary.json
```

现有记录的人工质量复核仍未完成；最后一次模型服务检查返回 HTTP 402，重新运行前需先恢复可用服务。验收状态与限制见 [重建记录](../history/V3_REBUILD.md)。
