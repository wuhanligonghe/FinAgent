# 本机 Qwen3.5-9B 意图模型接入（2026-10-23）

> 本文是 2026-10-23 的接入与测量快照。文中的服务状态、环境和指标未在本次文档整理中重新验证。当前通用命令见 [运行说明](../guides/RUNNING.md)。


已连接本机 Ollama 0.40.9 和 RTX 3090，模型为
`finagent-intent:qwen3.5-9b-v2-f16`。

按用户明确要求，在 finagent.toml 的 [nlu] 中设置
`allow_unqualified=true`，chat／run 使用当前候选模型识别意图。
模型质量仍为 diagnostic；输出、模型身份、证券确认与研究模式检查继续执行。

在 FinAgent 目录执行：

```bash
../qwen3_intent/scripts/finagent nlu-check
../qwen3_intent/scripts/finagent nlu-parse --text '分析一下中电鑫龙最近的新闻面'
../qwen3_intent/scripts/finagent chat
```

快捷入口使用相邻训练工程的 .venv，固定读取本 FinAgent 的配置。
分析师模型 [model] 当前未配置；NLU 解析已可用，实际问答和研究回复需补齐该配置。

用户级 Ollama 服务已启动并启用：

```bash
systemctl --user status ollama-finagent.service
```

真实候选 validation 共 320 条：合法输出 62.81%，完整语义帧匹配 56.88%，
P95 约 3.98 秒。抽查失败输出发现实体下标偏移；这类输出会被拒收。
应用 368 项离线测试、18 项模式边界回放通过，真实模型宿主冒烟 7/8 通过。
当前识别质量尚未达到正式验收目标。

完整部署、验证结果、候选开关与正式验收步骤见
[训练工程的接入说明](../../../qwen3_intent/FINAGENT_INTEGRATION.md)。
