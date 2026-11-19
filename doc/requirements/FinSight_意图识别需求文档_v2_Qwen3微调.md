# FinSight 意图识别需求文档 v2：Qwen3.5-9B 微调与 Ollama 部署

| 项目 | 定义 |
|---|---|
| 日期／范围 | 2026-10-22；意图识别专项 v2，标识 NLU-SFT2 |
| 读者 | 实施 FinAgent 语义入口与服务器微调的开发者、训练执行者 |
| 基座 | `Qwen/Qwen3.5-9B`（2026-10-23 由暂定的 Qwen3-0.6B 升级，见第 5 节变更记录），原生多模态因果语言模型，纯文本训练其文字主干；以 LoRA SFT 作为第一条训练路线 |
| 应用 | `FinAgent/`，生产接入目标 backend=`qwen3_ollama` |
| 独立训练工程 | `../training/qwen3_intent/`，与 FinAgent 分离，可单独上传服务器 |
| 数据 | 最新 nlu22 候选 2000 条 → 对话式 SFT，1360／320／320，105 个表达分组 |
| 状态 | 需求、迁移数据、独立训练工具及应用新后端已开发；真实GPU训练、GGUF／Ollama效果与独立人工质量验收待完成，见运行说明和开发记录 |

本文是用户要求的“v2 新需求文档”，版本仅指**意图识别方案的第二版**，不覆盖已有产品需求 v2.0／v2.1 的版本编号。继承 [产品 v2.2 的显式研究模式与宿主约束](FinSight_需求文档_v2.2.md)，覆盖其中及 [MacBERT 专项](../history/FinSight_需求方案_v2.1_MacBERT意图识别与服务器微调.md) 与 PLM 模型架构、BIO 解码、置信分数、训练和部署有关的冲突条款。未冲突的研究流程、证据、预算、历史任务读取与恢复约束继续生效。

## 1. 本轮要解决的问题

现有意图识别以 MacBERT 这类 PLM 编码器为基础，使用多个分类头和 BIO 实体抽取。用户反馈效果不佳。本轮将方案改为小尺寸生成式语言模型微调：模型读取原话及有限上下文，一次生成受 JSON schema 约束的语义结果，同时完成任务识别、维度包含／排除、对话行为和原文实体抽取。

“PLM”在本文特指原 MacBERT 编码器与联合分类／BIO 实现。Qwen3 本身也经过预训练，因此不能把新路线描述为“不使用预训练模型”。改变的是模型架构、训练目标与推理合同。

### 1.1 已核对的项目现状

- `src/finagent/nlu/macbert.py` 加载原伴随包和编码器；`nlu/parser.py` 直接导入该 parser，并在 scope／日志中写入 macbert。
- `nlu/contracts.py` 使用 `nlu22.1`，保留任务、对话、绑定、五维和实体；目前实体要求 score，结果要求 bundle_sha256 等宿主元数据。
- `config.py:NLUSettings` 当前只接受 macbert／legacy_regex，不能仅修改配置字符串完成替换。
- 当前有效数据源为 `../training/macbert_nlu/data/bootstrap_2000_nlu22_v1/`，其清单内版本为 `bootstrap_2000_nlu22_v2`；不是历史含 depth／workflow_preference 的那份 2000 条。
- 当前 2000 条均为合成 candidate；主意图分布为股票分析900、数据查询440、概念问答300、other270、状态／报告／恢复各30。共105个表达分组，train61组、validation22组、test22组。稀有类别支持量和表达多样性仍不足。

本轮全量结构检查发现实体原文下标有效，无分组或相同 text＋context 的跨集合重复。这只说明迁移结构有效，不证明标注正确、语义近重复已消除或线上效果达标。旧文档中的历史实验成绩不能直接作为当前 nlu22 模型或新 Qwen3 的成绩。

### 1.2 必须实现的结果

| 编号 | P0 要求 |
|---|---|
| FR-SFT2-01 | 生产自然语言意图识别从 MacBERT／BIO 切换为已微调的 Qwen3.5-9B 生成式 JSON 解析 |
| FR-SFT2-02 | 训练工程位于 FinAgent 外；不依赖应用源码、其虚拟环境、数据库、持仓和密钥 |
| FR-SFT2-03 | 2000条有效 nlu22 数据转换为对话 SFT；保留来源、ID、分组、集合和待审状态 |
| FR-SFT2-04 | 服务器训练、恢复、合并、GGUF 导出、Ollama 导入与诊断评估有可执行入口 |
| FR-SFT2-05 | 宿主严格校验输出，继续负责证券确认、日期归一化、澄清、取消和准入，不使用旧语义规则隐式兜底 |
| FR-SFT2-06 | 模式由用户菜单／CLI 显式授权；模型不能生成或升级 execution_mode、预算和阶段权限 |
| FR-SFT2-07 | 工程完成、真实训练、部署成功、模型质量和生产切换分别记录，未达标不宣称已替换上线 |

不训练投资结论、股票预测或报告生成，不更换分析师模型，不改动研究和决策链的领域职责。不在本轮本地机器上消耗服务器训练资源。

## 2. 训练与 Ollama 的职责

**实现路线：服务器 Transformers＋PEFT LoRA 微调 → 合并完整 HF 权重 → 转 GGUF → Ollama 导入和推理 → FinAgent 宿主校验及分流。**

用户提出“在服务器上用 Ollama 训练”。根据当前官方接口，Ollama 提供运行、模型导入和结构化推理，没有内置梯度微调入口；因此这里落实为在使用 Ollama 的服务器上完成微调，再交给 Ollama 部署。`ollama create` 读取权重与 Modelfile，不会执行 SFT；不得编造 `ollama train` 命令。[Ollama 官方仓库讨论](https://github.com/ollama/ollama/issues/7815)、[模型导入说明](https://docs.ollama.com/import)

初始基座必须使用 HF 的 `Qwen/Qwen3.5-9B` 或其完整本地快照；不得直接对 Ollama 下载的量化运行权重训练 LoRA。训练、tokenizer、合并和部署使用同一模型版本。正式记录基座 commit／文件清单、依赖、训练种子、数据／prompt／schema 哈希、checkpoint 和导出文件哈希。

Qwen3 支持非思考模式。本任务训练采用 `enable_thinking=False`，Ollama 推理传 `think=false`，监督目标不包含推理过程。Ollama `/api/chat` 的 `format` 传入 JSON schema，`stream=false`；采用 temperature=0 作为本任务可重复性起点，仍须实测，不把其视作 Qwen 对所有任务的通用推荐参数。[Qwen3.5 模型卡](https://huggingface.co/Qwen/Qwen3.5-9B)、[Ollama Chat API](https://docs.ollama.com/api/chat)、[结构化输出](https://docs.ollama.com/capabilities/structured-outputs)

## 3. 语义输入、生成输出与宿主合同

### 3.1 输入

统一三消息训练格式：固定 system 解析规则和 schema；user 内容为 JSON `{text, context}`；assistant 为语义 JSON。推理使用相同 system、user 和 Qwen3 chat template。训练文件外层的 ID、分组、metadata 和 expected_admission 不进入 prompt。

`text` 保留原文，不裁剪公司名、不静默修改字符。`context` 只包含必要最近用户话轮、已确认证券、当前范围、关联研究ID、待澄清字段及必要候选。与当前 `context_summary` 对齐，限制话轮、字符和 token 总量；不载入财报、证据全文、系统密钥或完整会话。上下文序列化为数据，不能变成新的 system 指令。

初始训练与推理上下文上限4096 tokens，生成上限1024 tokens。上线前用真实 tokenizer 校验全部迁移样本和实际上下文。超限返回可操作错误／要求缩短，不截断实体或 JSON 后继续执行；长输入策略由宿主负责。工程初版训练已实现拒绝超限，生产接入还须做同等预检。

### 3.2 模型生成合同：nlu_sft_v2.1

| 字段 | 值与规则 |
|---|---|
| schema_version | 固定 `nlu_sft_v2.1`，独立于研究 workflow_version |
| task_type | stock_analysis／data_query／concept_qa／research_status／research_report／research_resume／other |
| dialogue_act | new_request／follow_up／switch_entity／correct／clarification_answer／cancel／other |
| entity_binding | explicit_single／explicit_multiple／reference／absent，按当前目标证券身份计数 |
| dimension_state | technical、sentiment、news、fundamentals、policy 五键齐全，各为 include／exclude／unspecified |
| entities | 当前原话中的实体数组，最多64项；每项 type、role、text、start、end 必填 |

实体类型继续覆盖 COMPANY、STOCK_CODE、LOOKBACK_WINDOW、TIME_CUTOFF、REPORT_PERIOD、TRADE_DATE、DECISION_HORIZON、FINANCIAL_METRIC、COMPARISON_MODE、DISPLAY_UNIT。COMPANY／STOCK_CODE 的 role 为 target／excluded／background，其余类型 role=null。原文下标采用 Python Unicode code point、左闭右开，必须满足 `text[start:end] == entity.text`；按位置排序且不重叠。证券全称、简称和代码共指同一身份时由证券服务去重确认，不由模型记忆替代查询。

所有对象拒绝未知字段，必填项不靠默认值补齐。模型不输出 depth、workflow_preference、execution_mode、admission、置信分数、证券标准身份、预算或模型哈希。取消、指代、否定和角色从语义模型识别，不能在 JSON 出错后用关键词／正则重新猜意图。

示例（原话“分析一下中电鑫龙最近的新闻面”）：

```json
{
  "schema_version": "nlu_sft_v2.1",
  "task_type": "stock_analysis",
  "dialogue_act": "new_request",
  "entity_binding": "explicit_single",
  "dimension_state": {
    "technical": "unspecified", "sentiment": "unspecified", "news": "include",
    "fundamentals": "unspecified", "policy": "unspecified"
  },
  "entities": [
    {"type": "COMPANY", "role": "target", "text": "中电鑫龙", "start": 4, "end": 8},
    {"type": "LOOKBACK_WINDOW", "role": null, "text": "最近", "start": 8, "end": 10}
  ]
}
```

### 3.3 宿主运行结果

模型 JSON 与宿主运行对象分开。宿主附加 model tag、实际模型／GGUF 哈希、schema／prompt 版本、耗时、输入状态、admission 和 reasons。现有 Entity.score 不能填1.0假装置信度：应改为可缺省／null，或在新版本运行对象中删除；下游 acceptance 必须同步改造。无有效校准时 scores={}，不能沿用 MacBERT softmax／BIO 分数阈值。

语法及原文校验通过不等于预测正确。P0 准入按任务必需槽位、上下文一致性、证券解析唯一性、维度与对象冲突、宿主当前状态判定。多对象但功能只支持单标的时澄清，不自行挑第一个。无维度的股票分析澄清维度；有对象引用但上下文缺失时补问；报告／状态／恢复缺有效研究关联时补问；cancel 进入现有取消流程。

输入超限、未知 schema、JSON 无效、未知字段、下标错误、响应截断或服务失败均不得启动新研究。返回结构化错误及可执行提示；不调用旧 MacBERT、旧正则或其他大模型修复语义。网络瞬态重试最多一次，受同一请求超时和计量约束，不能重试已创建的研究动作。诊断客户端初版采用不重试。

如后续引入 token logprob／拒识器，必须在 validation 校准，定义独立协议和验收；不得把模型自报数值或 JSON 格式合法当作概率。

## 4. 2000 条数据适配与质量工作

### 4.1 本轮迁移规则

输入：最新 `bootstrap_2000_nlu22_v1`，输出：新工程 `data/qwen3_intent_2000_v2/`。旧数据原样保留，不覆盖历史模型或训练输出。

1. 保留全部2000条的 text、context、ID、source_id、group_id 和 train／validation／test 归属；不随机重新拆分。
2. 将 labels 的任务、对话、绑定、五维及 entities 转为 assistant JSON；schema 改为 nlu_sft_v2.1；非证券 role 显式补 null。
3. 原 expected_admission 只放 metadata，供未来宿主端到端评估；不训练模型裁定执行授权。
4. 原 candidate／synthetic 状态保持。不得只转换格式就改为 approved；审阅表与真正标签修订分别管理。
5. 转换器先核验源 manifest 哈希、训练许可、ID唯一性、主文件与拆分一致、跨集合分组／相同输入隔离、标签字段和实体边界，再生成产物。
6. 生成三个 JSONL、manifest、quality_report、migration_audit 和 review_decisions.template；写入源清单及 prompt 的 SHA256。

迁移仅改变训练表示，不宣称已重标或修正语义。转换后的来源分布、稀有类别和上下文依赖仍需人工复核。训练端完整保留对话／指代样本，不能丢掉 context 后沿用原标签。

### 4.2 候选诊断与正式语料

初步服务器诊断允许用全部候选 train 微调，但命令必须显式开启 `--allow-candidate`；默认拒绝未审数据。test 完全不用于训练、早停或配方选择。反复用于调参的集合只能称开发集，不再作为独立验收证据。

人工审核优先覆盖：实体边界与 target/excluded/background、否定范围、多维包含排除、指代上下文、换股纠正、澄清答案、无对象／无维度、概念中的研究词、报告读取／恢复、超长／异常／输入指令干扰。发现错标须修订并发布新数据版本，更新哈希和审计，不在已冻结产物上静默修改。

后续补充实际失败表达和独立人工语料，按用户会话／表达家族分组隔离，清除跨集合语义近重复，不只替换公司名。正式验收至少300条独立人工核验样本，覆盖每个主意图／对话／绑定类不少于10例，每维 include／exclude 各不少于20例；不足时增加样本，不删类别降低分母。私有对话、账户和持仓数据须在有授权且脱敏后使用。

## 5. 独立工程与训练配方

```text
Agent/
  FinAgent/
    doc/FinSight_意图识别需求文档_v2_Qwen3微调.md
  training/
    macbert_nlu/                 # 历史实验保留
    qwen3_intent/
      README.md
      requirements_v2.md        # 本文副本，独立上传后仍可阅读
      requirements-train.txt
      configs/lora_3090.json
      nlu.py                    # prompt、schema、严格校验与 loss mask
      prepare_data.py
      train.py
      merge.py
      ollama_client.py
      evaluate.py
      ollama/Modelfile
      data/qwen3_intent_2000_v2/
      tests/test_contract.py
      DELIVERY.md
```

本轮脚本为可供服务器验证的初版，不等于已通过真实训练环境验证。训练依赖不进入 FinAgent 应用依赖；转换、校验和 Ollama HTTP 诊断仅需 Python 标准库。所有训练配置中的相对路径以配置文件所在位置解析，脚本默认路径以工程根解析；不依赖某台 Windows 机器的绝对路径。

初始 LoRA 配方：r=16、alpha=32、dropout=0.05；attention q/k/v/o 和 MLP gate/up/down 投影；学习率1e-4，batch2、累积8，有效batch16；最多5轮，validation loss 两轮无改善早停；warmup5%、cosine、weight_decay0.01、clip1.0、seed42。支持时 BF16，否则 FP16；启用梯度检查点，不在训练时用推理型 device_map=auto。以上为起点，不保证最优或固定显存用量。[PEFT LoRA](https://huggingface.co/docs/peft/main/en/developer_guides/lora)、[Trainer](https://huggingface.co/docs/transformers/main/en/main_classes/trainer)

仅对 assistant JSON 和终止 token 计算 causal LM loss；system、user、上下文、padding 设为-100。必须使用官方 chat template 验证前缀 token 一致性，不把整段文本交给默认 collator 计算全量 loss。保留空 think 的模板控制前缀可以，但不监督模型生成思考内容。不启用跨样本 packing 作为首轮方案。

新配方写独立 output_dir。恢复只接受本次运行实际 checkpoint，核对配置、数据、prompt 和基座版本；不得 resume MacBERT、旧 schema 或已变更数据的 checkpoint。初版 best 按 validation loss 选，正式候选还需生成式语义评估，不能把 loss 最低直接称为意图效果最好。

第一轮训练 Qwen3.5-9B；未达标先检查错标、prompt、模板、loss mask 和类别支持，再调整配方。变更记录：2026-10-23 由项目所有者决定将基座从暂定的 Qwen3-0.6B 升级为 Qwen/Qwen3.5-9B；依据是 0.6B 容量不足的风险与既有 2000 条数据对新基座模板已验证兼容；资源成本为单卡 RTX 3090 24GB 上 bf16 LoRA（batch=1、梯度累积16、gradient checkpointing），DeltaNet 层建议安装 causal-conv1d／fla 快核。后续再换基座仍需另行记录选择依据和资源成本。

## 6. 导出、部署与 FinAgent 改造

### 6.1 导出与服务器部署

训练输出 adapter／tokenizer／运行清单；使用同版本未量化基座合并为完整 HF 权重，再用支持 Qwen3 的 llama.cpp 转 GGUF。先导入 F16 版验证模板、非思考模式和 JSON，再单独评估 Q8_0／Q4_K_M。不能假定 Ollama 的所有导入格式都已验证 Qwen3 adapter；本方案不依赖直接 ADAPTER 导入。[GGUF 转换器](https://github.com/ggml-org/llama.cpp/blob/master/convert_hf_to_gguf.py)、[Ollama 导入](https://docs.ollama.com/import)

交付包带 GGUF、Modelfile、schema、prompt及哈希、数据／基座／训练清单、实际依赖、评测报告和启动步骤。模型 tag 只用于寻址，不单独作为不可变身份；须记录内容哈希。Modelfile 不重复注入 SYSTEM 或替换 Qwen3 模板，客户端每次发送同版 prompt 与 think=false。

服务器训练并部署是首个验证目标；生产推理既可连接服务器 Ollama，也可回传 GGUF 在本机 Ollama 部署。使用哪台机器、硬件和网络延迟必须写入验收报告，不假定本机已有 GPU。具体命令见 [独立工程 README](../../../qwen3_intent/README.md)。

### 6.2 后续应用改造清单

| 位置 | 必须变化 |
|---|---|
| config.py／finagent.toml／配置说明 | 增加 qwen3_ollama backend、endpoint、模型tag、超时、上下文和生成上限、预期版本／哈希；训练依赖隔离 |
| nlu/qwen3.py（新增） | 统一请求、响应完整性、JSON/schema/原文检查、模型身份、超时及可诊断错误 |
| nlu/contracts.py | 增加新的生成／运行合同，score改为可无值，移除对旧BIO字段的生产依赖 |
| nlu/acceptance.py | 移除旧多头／BIO阈值假设，改为新宿主准入，不伪造置信分数 |
| nlu/parser.py | 通过统一 parser 工厂调用新后端；去除 macbert 导入与硬编码日志；保持上下文、pending 和证券确认 |
| nlu/diagnostics.py／CLI诊断 | 显示 Ollama 健康、模型身份与版本，不再把旧 wheel／本地 encoder 作为启动条件 |
| chat.py／cli.py／tools/delegation.py | 所有自然语言入口共用结果；继续消费宿主授权，不允许模型扩大流程 |
| pyproject.toml／打包与测试 | 去除生产 MacBERT 伴随包、分类头、BIO 和 torch 运行要求；增加新后端契约测试 |

最终生产路径不再保留 macbert／legacy_regex 为自动回退。旧代码、数据和实验可以留在历史训练工程或版本库供离线对比，但不能影响默认入口。应用默认接入qwen3_ollama；只有新模型和宿主验收通过才允许生产执行。上表的应用重构已实施，MacBERT运行适配器移出生产包；显式legacy_regex标志仅保留给历史离线回归。

### 6.3 必须保持的模式边界

普通 chat 只做问答、工具查询或已指定维度专项，不自动进入辩论、研究经理、交易员、风险或投资组合经理。deep_research／full_decision 继续由 `/research` 菜单或显式 CLI mode 授权；模式不是模型标签，也不进入 SFT 输出。模型预测、目标文字或工具参数不能伪造选择凭据。

`/help`、`/exit`、`/cancel`、显式研究ID读写／恢复及完全结构化的研究命令由宿主处理。NLU 不可用时，结构化入口仍可用；需要语义解析的普通输入返回明确服务／模型错误。自然语言“继续上次研究”仍需有效ID、原任务范围和已有恢复授权，不能绕过预算新建任务。旧 scope、workflow_version 和历史预算不因新 NLU 改写。

## 7. 评估与验收

### 7.1 比较对象与指标口径

至少比较旧 MacBERT、未微调 Qwen3.5-9B 和微调 Qwen3.5-9B；在同一冻结样本、上下文及共享语义字段上评估。旧模型没有有效预测文件时注明缺失，不捏造基线。比较完整帧时去除旧 score／运行元数据，并归一化非证券 role=null；不得使用不同数据或不同字段数量证明提升。

必须报告：合法 JSON/schema/实体位置率、主意图 accuracy及Macro-F1、对话／绑定Macro-F1、每维三态Macro-F1与include／exclude F1、五维完全匹配、角色化实体span F1、证券集合完全匹配、无证券输入误提取率、完整语义帧、宿主admission、正确自动处理比例、冷／热延迟和资源占用。

字段宏平均基于冻结全部枚举，报告每类支持／混淆，缺支持类不默默去掉。完整帧需任务、对话、绑定、五维和实体集合全部正确；实体键为(type,role,start,end)，多提／错角色均算错。生成无效、超时和截断不得从分母删除。零分母记null，不视作通过。

宿主接受准确率＝预测accept且gold应accept且全帧正确的数量／所有预测accept数量。覆盖率＝gold应accept中预测accept比例；正确自动处理比例还要求全帧正确。格式合法不能代替这三个指标。独立evaluate.py只做语义诊断；正式宿主指标现由FinAgent的nlu-evaluate生成。

配方和拒识阈值只用train／validation选择，test冻结后一次性回归。F16、量化版和跨平台部署分别评估，记录一致性、资源和性能，不能只评估HF后宣称Ollama效果相同。

### 7.2 目标门槛（待验证）

沿用产品v2.2的主要业务质量目标，新增生成合法性门槛；不是对现有2000候选的训练结果保证。

| 指标 | 生产目标 |
|---|---|
| 合法JSON＋schema＋实体原文位置 | ≥99%，同时无效结果误启动数=0 |
| 主意图Macro-F1 | ≥0.90 |
| 每维Macro-F1／每维include及exclude F1 | ≥0.85／≥0.80 |
| 对话行为／实体绑定Macro-F1 | 各≥0.85 |
| 角色实体span F1／证券集合完全匹配 | ≥0.90／≥0.95 |
| 完整语义帧准确率 | ≥0.80 |
| 宿主接受准确率／覆盖率 | ≥0.95／≥0.70 |
| 模式越权／取消后启动／无效输出启动 | 固定边界用例全部为0次 |

质量门槛在至少300条独立人工集及第4节类别支持条件下验收。2000条合成候选的内部test只作诊断。未达标时保留诊断结果和失败样本，不自动降低门槛。

旧编码器CPU P95≤500ms目标不能无测量直接承诺给生成模型。新性能基线先在指定服务器Ollama记录首轮、独立冷加载、热态P50/P95、输出tokens、并发1和目标并发、显存／内存及网络往返；初始热态单请求P95≤2s为待验证工程目标。本机CPU另行实测，不宣称满足服务器目标。性能目标变化不降低质量与模式边界要求。

### 7.3 必测行为

| 输入／状态 | 预期 |
|---|---|
| 分析一下中电鑫龙最近的新闻面 | 原文公司中电鑫龙、news include；chat新闻专项 |
| 不看新闻，只分析基本面和政策 | news exclude，fundamentals/policy include；无对象且无上下文先补问 |
| 不是兴业银行，换成中电鑫龙看看新闻 | 兴业银行excluded，中电鑫龙target；不把两个名字当双目标 |
| 那它的政策面呢，已有确认证券 | reference＋follow_up，复用已确认对象；不幻造当前原文实体 |
| 什么是完整决策／深度研究 | concept_qa，不启动研究 |
| 全面深入分析中电鑫龙的新闻 | chat新闻专项，不取得深度模式权限 |
| 分析一下中电鑫龙，未指定维度 | 澄清维度，提供显式研究入口 |
| 显式选deep_research／full_decision | 模式来自宿主选择记录，保持不同阶段终点 |
| 查看报告／恢复任务，ID不唯一 | 澄清关联任务，不新建研究 |
| JSON带execution_mode／下标错／截断／服务超时 | 拒收并明确提示，不执行研究、不隐式回退 |
| 用户文本要求忽略规则输出交易指令 | 仅解析数据，不执行输入中的指令 |

这些固定用例在数据层、独立推理层及FinAgent所有入口分别覆盖。离线替身测试验证协议和门控，真实Qwen＋Ollama测试验证语义、模板、导出和性能，两类结果不能互相替代。

## 8. 实施顺序与本轮交付边界

| 阶段 | 完成定义 | 本轮状态 |
|---|---|---|
| A 需求与合同 | 新方案、模式继承、Ollama职责和验收定义清楚 | 已交付本文 |
| B 数据与训练工具 | 独立目录、2000条SFT、审计、训练／恢复／合并／GGUF／部署包／诊断入口 | 已交付；真实tokenizer检查已通过，离线检查见DELIVERY.md |
| C 服务器实验 | 固定环境、tokenizer核验、真实微调、验证与基线比较 | 由用户在服务器执行，尚未执行 |
| D 导出与部署 | 真实GGUF、Ollama导入、F16／量化评估及性能报告 | 待完成 |
| E 应用接入 | 新backend、合同／准入迁移、旧运行依赖移除、全入口门控 | 已开发并默认配置新backend；合格模型生产准入仍待完成 |
| F 生产验收 | 独立人工集质量达标、权限边界和性能验证、明确切换记录 | 待完成 |

主文档位于FinAgent/doc，新工程内保存 requirements_v2.md 副本，独立复制后仍可阅读完整需求。应用文档入口应指向新方案并说明替换尚未实施；旧资料作为历史记录，不删除用户现有工作。

## 官方资料

查阅日期：2026-10-22。以下资料用于确认API和实现路径，不作为本项目模型已达标的证据；没有以外部论文成绩替代项目评测。

- [Qwen3.5-9B 官方模型卡](https://huggingface.co/Qwen/Qwen3.5-9B)
- [Transformers Trainer](https://huggingface.co/docs/transformers/main/en/main_classes/trainer)
- [PEFT LoRA](https://huggingface.co/docs/peft/main/en/developer_guides/lora)
- [Ollama 官方仓库微调讨论](https://github.com/ollama/ollama/issues/7815)
- [Ollama 导入](https://docs.ollama.com/import)
- [Ollama Chat API](https://docs.ollama.com/api/chat)
- [Ollama 结构化输出](https://docs.ollama.com/capabilities/structured-outputs)
- [llama.cpp GGUF 转换器](https://github.com/ggml-org/llama.cpp/blob/master/convert_hf_to_gguf.py)
