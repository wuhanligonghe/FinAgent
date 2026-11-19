# 原版 Qwen 9B 工具意图分类

主对话（终端与 FinSight Web 共用 `SessionHost`）现支持：

`用户原话 → 精确闲聊规则 → Qwen3.5-9B 动作/数据领域分类 → 宿主映射工具类别 → 按加载模式披露工具 → 主模型执行`

默认项目配置已开启 `[intent]`。这是对原版模型的带边界示例提示推理，不训练模型，不加载 LoRA，不依赖旧 NLU 的 `manifest.json`、tokenizer 或微调权重。旧 `[nlu]` 仍关闭；两条路径独立。

工具加载由 `[model].tool_loading` 决定：`eager` 按下文筛选实际 schema；当前项目配置的 `deferred` 使用分类作为发现建议，保持工具目录稳定，通过 `search_tools` 或 `select_tool_groups` 的结构化引用按需加载。执行权限与角色过滤均保留。协议、缓存和兼容配置见[工具渐进披露与缓存](../features/TOOL_DISCOVERY.md)。

当前本机配置使用独立实例 `http://127.0.0.1:11435`，用户服务名 `finagent-intent-ollama.service`；下方11434是常规 Ollama 安装示例。独立实例通过项目现有代理完成官方模型下载。该服务由 `systemd-run --user` 启动，属于当前用户的临时服务，重启机器后需重新启动：

```bash
systemd-run --user --unit=finagent-intent-ollama \
  --property=WorkingDirectory=/home/hxj/Agent/FinAgent \
  /home/hxj/Agent/FinAgent/.venv/bin/python \
  /home/hxj/Agent/FinAgent/scripts/serve_intent_ollama.py \
  --binary /home/hxj/ollama-bin/bin/ollama
```

## 启动模型

在具备运行模型条件的机器安装 Ollama 后：

```bash
ollama serve                         # 已有服务时跳过
ollama pull qwen3.5:9b
ollama run qwen3.5:9b                # 首次预热，完成后退出交互
```

标准模型名称见 [Ollama 官方模型页](https://ollama.com/library/qwen3.5:9b)。本项目调用 [Ollama /api/chat](https://docs.ollama.com/api/chat)，使用 JSON schema、`think=false`、`temperature=0`、非流式输出与驻留缓存。无需安装 Python ollama/torch/transformers。9B 是相对主模型的分类用途，不保证任意机器都能低延迟推理；首次加载或 CPU 推理可能超过默认 8 秒，此时使用规则兜底。可先预热或按实测调大超时。

```toml
[intent]
enabled = true
endpoint = "http://127.0.0.1:11434"
model = "qwen3.5:9b"
timeout_seconds = 8
context_tokens = 8192
output_tokens = 256
max_input_chars = 4000
confidence_threshold = 0.65
```

也可在 `.env` 覆盖 `FINAGENT_INTENT_ENABLED`、`FINAGENT_INTENT_ENDPOINT`、`FINAGENT_INTENT_MODEL`。远端服务的 endpoint 填 Ollama 根地址。模型服务连接不继承市场数据代理或主模型代理。修改配置后重启已有应用进程。

## 分类输出与工具选择

模型只输出 `action`、可多选的 `data_domains`、`confidence`；宿主映射兼容字段 `intent/tool_groups`，增加 `source`、实际请求模型名、耗时、全部尝试用量、`model_calls`、`diagnostics` 与降级原因。`confidence` 是模型自报值，尚未校准。类别为：

| 类别 | 用途 |
|---|---|
| market | 股票/指数快照、历史日线、涨跌、技术指标 |
| fundamentals | 公司资料、财报、财务证据与计算 |
| news | 个股新闻、事件、资金流、情绪 |
| policy | 宏观新闻、政策检索与原文 |
| sector | 行业板块整体强弱与排名 |
| research | 独立研究发起、状态、列表和已有报告读取 |
| workspace | 工作区文件、Shell、导出 |
| automation | 任务、队友、定时、工作流 |
| memory | 有用户来源的长期记忆 |
| extensions | MCP 和动态注册的其他工具 |

例如“茅台去年净利润和最近股价”应选 `fundamentals` 与 `market`；“上面报告的风险再解释一下”可选 `research`。类别仅决定初始工具展示；不启动任何操作，不生成业务工具参数。分类和金融执行都记入当前请求预算；闲聊规则不消耗模型调用。

动作包括conversation、explain、query、analyze、research_start、research_followup、workspace、automation、memory_write、extensions、task_control、other。query/analyze必须有数据领域；conversation/explain与专用能力动作不得夹带数据领域。研究启动/追问统一映射research，偏好保存映射memory，任务清单映射automation；暂停分析可不选数据工具，公共任务表示工具仍可用，修改为只看技术面则保留market。输出合同禁止重复领域、额外字段、非有限置信度和类型强制转换。

提示词提供成对边界：偏好与文件、普通分析与独立研究、已有报告与概念、当前分析的暂停/继续与定时任务、政策原文与工作区、引用与实际执行。小模型只给发现提示；主模型负责理解需求并更新TODO，宿主负责领域参数、证据和权限检查。

每次真实用户输入（包括运行中的追加输入）重新分类。分类上下文使用之前最近三条用户/助手文本（每条尾部最多600字符）、当前证券、待澄清问题，以及实际活动任务的类型、状态和领域提示；不传原始工具响应、任务ID、业务参数或完整任务记录。任务领域通过既有目标/指标/范围的关键词保守投影，仅是提示。分类不改写用户原话或语义任务。输入过长时保留业务原话、跳过分类推理，按完整原话使用规则；UTF-8 字节上界保守预留上下文，避免依赖本地 tokenizer。

澄清、证券解析、证据回读、Skills 和任务理解工具始终保留。主模型可用 `select_tool_groups` 扩展类别，下一轮取得工具定义；调用未选择类别的工具会返回补选提示。现有角色 `tool_filter` 和权限仍然检查，补选不能扩大角色权限。后台通知续跑不继承上次用户的类别筛选，隔离子执行器保留其角色工具范围。

只有完整匹配的问候、感谢或能力介绍才直接命中闲聊规则；“你好，查一下茅台股价”继续调用 Qwen。其他请求先用 Qwen；内容JSON、schema、跨字段约束或截断错误最多纠正一次；重试只收到宿主白名单错误码和字段路径，不回灌无效响应。模型身份、响应协议、连接、超时错误不重试。两次尝试共用分类总超时，每次请求都扣当前模型调用预算，全部可得用量均登记，包括最后降级的尝试。仍失败或输入过长时按既有关键词策略多选类别。未知请求开放全部类别；模型低置信度或返回 `other` 时同样开放全部类别。兜底规则可能多选或漏选，主模型可补选；它不会将“报告”等关键词视为启动研究的授权。

## 诊断与验证

```bash
.venv/bin/finagent intent-parse '茅台去年净利润和最近股价'
.venv/bin/finagent intent-parse '那它呢' --history '比较茅台和五粮液的现金流'
.venv/bin/finagent intent-parse '今天哪个行业最强' --rules-only
.venv/bin/python -m unittest tests.test_intent_routing
.venv/bin/python scripts/check_intent_live.py
```

`source=qwen` 才表示完成真实模型分类；`source=fallback` 和 `fallback_reason` 说明降级，不能将其作为 Qwen 成功证据。分类写入会话的 `intent_classified` 事件；补选写入 `tool_groups_selected`，全部可得分类尝试用量记为 `usage.intent`。不单独记录分类原话，原始用户输入沿用项目既有会话记录。

离线测试覆盖协议、降级、多标签、上下文、工具可见性和执行边界。它们不代表真实 Qwen 分类准确率或推理速度；正式使用前应在目标机器上用实际金融请求验证。已有语义任务校验、证据约束和深度研究授权检查继续生效。

`check_intent_live.py` 先预热再检查19个固定样例（含活动任务提示），将分类来源、类别、耗时和结果写入 `.finagent/checks/intent_live.json`。这是开发冒烟测试，不能作为独立分类准确率评测。既有业务对照实验和任务对话回放显式关闭新分类入口，以维持原实验协议。

如果已有 Ollama 缺少下载代理，可用 `scripts/serve_intent_ollama.py --binary /path/to/ollama` 启动独立本机实例（默认端口11435），继承项目 `model.proxy_url` 作为下载的 HTTPS 代理，权重存入 `.finagent/intent-ollama-models/`。将 `[intent].endpoint` 改为该端点；主模型、数据服务仍使用各自配置。

2026-11-03 本机验证：完整离线回归610项通过；固定实验配置隔离后，相关38项再次通过。RTX 3090 上官方 `qwen3.5:9b` 十条冒烟样例全部匹配预期（1条规则、9条真实模型分类），模型预热后耗时中位数299ms、最大324ms；原始记录位于 `.finagent/checks/intent_live.json`。这些数字仅描述本次短样例、串行、预热运行，不含冷启动，不代表并发延迟或独立准确率评测。

2026-11-05 改造对应[当前能力候选集v2](../../evaluation/acceptance_v2/README.md)：140组171轮，十类别及动作标签，待独立审核。v1数据与历史分数保留。新增合同/重试/预算和上下文测试不代表真实分类准确率。

本次实际结果与残留错误见[路由v2验证报告](../evaluation/ROUTING_V2_RESULTS.md)：41轮校准工具集合38/41、动作39/41，最终公开冒烟17/19；这些开发结果不构成独立验收。历史2026-11-03的10/10冒烟仅属于当时版本。
