> 2026-11-03 更新：新增独立 `[intent]` 入口，以未微调 Qwen3.5-9B 和规则选择工具类别，详见[工具意图分类](INTENT_ROUTING.md)。本文仅描述旧微调 NLU。
>
> v3 更新（2026-10-30）：默认 `[nlu].enabled=false`。主 Agent 不调用旧 prepare 准入，NLU 仅通过 SessionHost 提供可纠正建议；模型缺失、超时或分类错误不禁止只读查询。下文部署/训练/旧验收说明继续供独立诊断使用，不能当作 v3 的执行权限合同。

# Qwen3 意图识别运行说明

应用版本0.3.0，意图合同nlu_sft_v2.1。本文保留部署与验收流程；2026-10-23 已完成本机候选模型接入和部分真实诊断，结果见 [本机接入记录](QWEN35_LOCAL_INTEGRATION.md)。正式独立质量验收仍未完成。旧MacBERT不再是生产依赖或默认入口。

## 当前运行方式

`finagent.toml`的`[nlu]`现为`qwen3_ollama`。它独立于用于分析师／问答的`[model]`配置，可连接本机或服务器Ollama。正式验收模式要求本地部署清单、匹配tokenizer、固定模型摘要及通过验收的质量报告；当前本机另有用户明确启用的 `allow_unqualified=true` 候选运行例外，见接入记录；这不等于正式质量验收通过。没有模型时普通自然语言会明确报错。帮助、显式研究菜单、完全结构化的研究命令、报告读取和已授权恢复继续可用。

```toml
[nlu]
backend = "qwen3_ollama"
endpoint = "http://127.0.0.1:11434"
model = "finagent-intent:qwen3.5-9b-v2-f16"
model_dir = "models/finagent-qwen3-intent"
timeout_seconds = 30
context_tokens = 4096
output_tokens = 1024
max_input_chars = 12000
expected_schema = "nlu_sft_v2.1"
store_raw_text = false
```

相对路径按配置文件所在目录解析。可用`FINAGENT_NLU_ENDPOINT`、`FINAGENT_NLU_MODEL`、`FINAGENT_NLU_MODEL_DIR`和`FINAGENT_NLU_EXPECTED_DIGEST`覆盖；进程环境优先于本地.env。不要把Ollama地址填入分析师模型的base_url来代替NLU配置。

## 训练、导出和回传

独立工程位于[qwen3_intent](../../../qwen3_intent/README.md)，可单独上传服务器。训练使用LoRA SFT，不由Ollama执行梯度更新。

```bash
# 在独立训练工程内，安装服务器CUDA环境与requirements-train.txt后：
python train.py --config configs/lora_3090.json --allow-candidate
python merge.py --run outputs/qwen35-9b-intent-v2-candidate --base ../models/Qwen3.5-9B --output outputs/merged-v2
python export_gguf.py --merged outputs/merged-v2 --llama-cpp /path/to/llama.cpp --output outputs/finagent-intent-v2-f16.gguf
cp ollama/Modelfile outputs/Modelfile
cd outputs
ollama create finagent-intent:qwen3.5-9b-v2-f16 -f Modelfile
cd ..
python package_bundle.py create --merged outputs/merged-v2 --gguf outputs/finagent-intent-v2-f16.gguf --output outputs/deployment-v2
```

首次2000条全部是待审candidate，`--allow-candidate`仅代表诊断实验。训练会记录实际依赖、基座文件哈希、模板验证、loss长度和最佳checkpoint；合并前核验adapter和基座，GGUF转换记录llama.cpp commit和文件哈希。`package_bundle.py`核对Ollama tag的FROM blob与本地GGUF内容，防止把另一个同名模型装入清单。

把deployment-v2复制到配置的model_dir；推理机无需训练依赖、MacBERT wheel、PyTorch或Transformers，只需正常安装FinAgent及轻量tokenizers依赖。目标Ollama需要导入包内model.gguf及Modelfile，并使用相同tag；若Ollama版本使模型摘要发生变化，须重新绑定并验证部署，不能手改摘要绕过校验。

## 诊断与正式启用

```bash
python -m finagent nlu-check --diagnostic --model-dir /path/to/deployment-v2
python -m finagent nlu-parse --diagnostic --model-dir /path/to/deployment-v2 --text "分析一下中电鑫龙最近的新闻面"
```

诊断预测固定`diagnostic_only=true`、`admission=reject`，不能交给研究执行。模型只返回语义，宿主附加模型身份和状态；实体score为空、scores={}，不编造置信度。

上线前，使用当前应用生成模式边界报告，再对独立人工集运行宿主验收。该过程调用真实Qwen3，但证券确认使用人工oracle的受控回放，不执行研究或在线证券查询。

```bash
python scripts/evaluate_v22_boundaries.py --output .finagent/nlu_sft2_validation/mode_boundaries.json
python -m finagent nlu-evaluate --model-dir /path/to/deployment-v2 --test /path/to/independent_test.jsonl --oracle /path/to/oracle.jsonl --mode-boundaries .finagent/nlu_sft2_validation/mode_boundaries.json --output /path/to/qualification.json
# 仅当报告真实通过全部门槛时，在训练工程内：
python package_bundle.py qualify --bundle /path/to/deployment-v2 --report /path/to/qualification.json
python -m finagent nlu-check --model-dir /path/to/deployment-v2
```

独立测试文件采用相同SFT格式；metadata需`annotation_status=approved`或`human_verified`、`synthetic=false`、`purpose=independent_acceptance`。oracle逐ID对应，annotation_status=human_verified，每条提供clock、identity_replay与完整expected：outcome、symbol、mode、pipeline、analysts、identity_queries、news_days、policy_days、as_of_date、scope_constraints；无动作字段仍写null／空数组。outcome为accept／clarify／reject。示例：

```json
{"id":"example-concept","annotation_status":"human_verified","clock":"2026-10-22T12:00:00+08:00","identity_replay":{},"expected":{"outcome":"accept","symbol":null,"mode":"direct","pipeline":null,"analysts":[],"identity_queries":[],"news_days":7,"policy_days":90,"as_of_date":"2026-10-22","scope_constraints":{}}}
```

证券回放示例为`identity_replay={"中电鑫龙":{"selected":{"symbol":"002298.SZ"}}}`；歧义时提供candidates。测试输入、人工语义和oracle须由独立人员核验，不能用模型预测自动填写。2000条候选即使全部拟合，也不能通过独立人工集门槛。

质量报告绑定实际模型内容、tokenizer、prompt、schema、token预算及当前应用实现。代码、部署文件或模型改变后必须重新验收。修改quality_status字符串不足以启用生产。

## 性能与错误

```bash
python -m finagent nlu-benchmark --diagnostic --model-dir /path/to/deployment-v2 --samples /path/to/500_fixed_samples.jsonl --output /path/to/benchmark.json
```

性能报告分别记录首轮、20轮预热后的热态延迟、token数量、错误及Ollama驻留/显存信息。不会为了测冷启动主动卸载共享模型；首轮是否冷加载从初始驻留和load_duration判断。热态P95≤2秒是待实测目标，诊断性能通过不等于质量通过。

常见错误：NLU_MODEL_MISSING（缺部署清单／模型tag）、NLU_MODEL_UNQUALIFIED（未通过验收）、NLU_MODEL_CHANGED（tag换内容）、NLU_MODEL_INCOMPATIBLE（哈希或版本不匹配）、NLU_INPUT_TOO_LONG（预算不足）、NLU_INVALID_OUTPUT（JSON／实体错误）、NLU_TEMPLATE_MISMATCH（模板开销异常）、NLU_TIMEOUT／NLU_SERVICE_UNAVAILABLE（服务故障）。均不会启动研究或隐式切回旧方法。

输入使用完整Qwen tokenizer预检，预留生成空间与32-token模板余量；不截断公司或末尾否定。服务端prompt_eval_count可能受前缀缓存影响，较小计数不视为截断；超出预检开销则拒收。诊断和日志默认不保存原话，token及模型身份用于追溯。

普通chat仍只做问答、查询和指定维度专项，深度研究／完整决策继续使用显式菜单或`research --mode`。`legacy_regex`加显式allow标志仅留给历史离线回归，不是模型故障回退；macbert配置不再接受。
