# Qwen3 意图识别开发与验收记录

> 历史记录：保留当时的需求或验收结论，不代表当前版本状态。当前入口见 [文档首页](../README.md)；历史机器路径、日志和外部工程可能不在本工作区。


日期：2026-10-22。应用版本0.3.0，语义合同nlu_sft_v2.1，部署清单nlu_sft2.bundle.1。对应[专项v2需求](../requirements/FinSight_意图识别需求文档_v2_Qwen3微调.md)，使用方式见[运行说明](../models/NLU.md)。

## 开发结果

| 范围 | 已实施 |
|---|---|
| 默认入口 | config／finagent.toml默认qwen3_ollama；chat、run、request_from_goal和研究委托共用语义结果；MacBERT配置不再接受 |
| 新后端 | nlu/qwen3.py调用Ollama，固定schema、非思考、非流式JSON；校验响应完整性、原文实体、模型tag前后摘要和请求总时限 |
| 训练／推理一致性 | 与独立工程字节相同的prompt、schema；轻量tokenizers本地预检完整输入，加生成预算和32-token余量，不依赖torch或transformers运行 |
| 运行合同 | 生成语义和宿主元数据分开；score可为空、scores={}；记录模型、prompt、schema、部署fingerprint及token用量，不伪造置信度 |
| 宿主准入 | 证券服务确认唯一身份、槽位／维度与范围校验、澄清继承；澄清期间模型内容改变时拒绝继续；诊断输出不能执行 |
| 模式边界 | 普通聊天只问答／查询／专项，显式菜单和CLI控制deep_research／full_decision；模型不授予预算或执行模式 |
| 诊断与验收 | nlu-check／parse／benchmark及新的nlu-evaluate；真实模型预测+受控身份回放，输出语义／宿主指标和独立人工门槛；候选数据不能晋级 |
| 部署身份 | 清单绑定GGUF、Ollama摘要、tokenizer、prompt、schema、预算和应用实现；生产要求匹配质量报告，不能只改quality_status启用 |
| 独立LoRA工程 | train／resume、基座与adapter哈希、merge、GGUF转换、诊断部署包、合格报告绑定及独立解压检查 |
| 历史代码 | MacBERT适配器移出生产源码与wheel；旧适配器／验收代码备份在.finagent/legacy_nlu_sft2，外置旧训练数据和实验保留 |

legacy_regex加显式allow标志只供历史回归；新后端服务、输出或质量失败不会切回该路径，也不会调用其他大模型修复。结构化研究入口无需NLU服务健康。

## 验证结果

以下是工程验证，不能作为训练模型的语义质量成绩。

- 应用全量离线回归：**365项通过**，无跳过；命令`python -m unittest discover -s tests -t . -q`。日志：nlu_sft2_full_tests.log（历史路径：`../../.finagent/nlu_sft2_full_tests.log`；当前工作区未提供）。包括19项新增Qwen3协议／身份／错误／配置测试及既有宿主、聊天、模式和研究链回归。
- 固定模式边界：**18项通过，0错误、0跳过、0真实研究API调用**；报告：mode_boundaries.json（历史路径：`../../.finagent/nlu_sft2_validation/mode_boundaries.json`；当前工作区未提供）。
- 独立训练工程：**10项离线测试通过**；部署包测试使用合成文件及Ollama替身，不生成真实合格模型。
- 真实Qwen tokenizer：2000条全部通过官方模板、前缀token一致性、assistant-only mask及4096-token预算检查；完整序列**762—945 tokens**，监督目标**64—182 tokens**。报告：tokenizer_preflight.json（历史路径：`../../.finagent/nlu_sft2_tokenizer_preflight.json`；当前工作区未提供）。使用本地Transformers 5.12.0／tokenizers 0.22.2做预检，服务器训练依赖范围仍需在目标环境验证。
- 数据结构：2000条保留原1360／320／320拆分及105个表达组；实体位置、源哈希、来源许可、分组和完全相同输入跨集合隔离通过。全部仍为synthetic candidate，不是人工金标。
- 新wheel构建与独立解压验证通过：包含Qwen3后端、prompt和schema，不包含MacBERT适配器，依赖不含torch／transformers／peft／旧伴随包；14类领域角色与30个工具可加载，NLU命令帮助可用。报告：package_check.json（历史路径：`../../.finagent/nlu_sft2_validation/package_check.json`；当前工作区未提供）。
- 独立训练压缩包在不包含FinAgent源码的目录解压并运行测试；结果与哈希见training_package_check.json（历史路径：`../../.finagent/nlu_sft2_validation/training_package_check.json`；当前工作区未提供）。
- 默认未部署模型时，`nlu-check --diagnostic`返回明确的NLU_MODEL_MISSING，未进行远程模型初始化或隐式回退。

## 可交付产物

- FinAgent 0.3.0 wheel（历史路径：`../../.finagent/dist/nlu-sft2/finagent-0.3.0-py3-none-any.whl`；当前工作区未提供）
- [独立服务器训练目录](../../../qwen3_intent/README.md)
- 独立服务器上传ZIP（历史路径：`../../.finagent/nlu_sft2_validation/qwen3_intent_server_v2.zip`；当前工作区未提供）

默认配置已指向新后端，但没有虚构已训练模型、生产manifest或通过的真实质量报告。

## 未完成的真实验收

用户需在服务器按独立README执行LoRA训练、恢复实测、合并、GGUF转换、Ollama导入和F16／量化比较。需补充至少300条独立人工核验输入及oracle，比较旧模型、未微调Qwen3和微调Qwen3的共同语义指标，并验证目标硬件性能。当前没有真实GPU训练结果、生成模型准确率、Ollama实测或热态P95成绩。

工程代码和离线检查已完成；模型训练、质量准入与实际部署是下一阶段。既有证据约束、历史研究scope、预算与显式模式授权继续有效。
