# 长期记忆与召回

2026-11-15 存储更新：当前源码的认证、Web 元数据、研究账本和向量缓存已改用 MySQL，Redis 与文件继续保留。正式迁移和服务切换已完成；初始化、旧库迁移和实测状态见 [MySQL 存储交付记录](MYSQL_STORAGE.md)。下文历史 SQLite 路径按迁移前版本理解。

当前实现采用“少量稳定偏好常驻 + 用户/项目过滤 + 语义与关键词混合检索 + 专用 Qwen3-Reranker-8B 重排”。CLI 和登录后的 Web 均遵循 `[harness].memory_enabled`、`auto_memory`；这两个开关默认开启。记忆与会话快照分别保存，新会话可以使用同一用户的长期记忆。

当前短期会话检查点、任务和通知已经迁到 Redis，见 [Redis 短期记忆](REDIS_RUNTIME.md)。长期记忆正文继续使用 JSON；向量缓存已独立迁到 MySQL 的 `memory_vectors` 表，详见本页顶部的交付记录。

## 存储和范围

CLI 默认保存位置为 `.finagent/v3/memory/users/local/`；登录后的 Web 为 `.finagent/users/usr_<ID>/v3/memory/users/usr_<ID>/`：

| 文件 | 内容 |
| --- | --- |
| `mem_<ID>.json` | 正文、描述、类型、来源、时间、`user_id`、`project_id`、`core` |
| `MEMORY.md` | 当前用户全部项目的目录索引；检索直接读取 JSON，并在索引和模型调用前过滤项目 |
| MySQL `memory_vectors` 表 | 向量缓存，按记忆 ID、服务/模型指纹、正文摘要区分版本；不重复保存正文 |
| `forgotten.json` / `forgotten_sources.json` | 按项目范围记录内容和来源的删除标记 |
| `archive/` | 整理前的历史记录，不参与自动召回 |

`memory.user_id` 由部署者配置，默认 `local`。`memory.project_id` 为空时，从配置中的工作目录绝对路径生成稳定标识，跨会话保持不变。变更目录后如需保留同一个项目，可明确配置固定项目 ID。已经保存了范围的会话不能用另一个用户或项目恢复，需新建会话。

用户偏好、反馈默认跨项目可见；项目资料和参考信息始终限定当前项目。`memory_write` 支持 `project_scoped=true`，可将某条偏好限制在当前项目。用户和项目来自可信配置，不接受模型指定任意用户/项目 ID。

Web 已启用登录，由服务端账号强制绑定记忆用户与独立数据根目录；配置中的用户 ID 只作为 CLI 操作员范围，不能当作网页登录授权。详见[登录与独立用户工作区](../guides/LOGIN.md)。

旧版本根目录中的记忆由 `local` 用户第一次打开时迁移到当前项目，并写入 `legacy_migration.json`；其他用户不会自动接收旧记录。迁移后的旧偏好先保持在原项目范围，避免扩大可见范围。

## 每轮如何召回

1. 扫描当前用户记录，保留跨项目记录和当前项目记录。其他用户/项目内容不会发送给 Embedding 服务或重排模型。
2. 从标记 `core=true` 的稳定偏好/反馈中，按创建时间由新到旧选择常驻记录，最多 4 条，完整记录合计不超过 1600 字符。常驻不依赖问题相似度。非稳定背景应设置 `core=false`。
3. 对剩余记录执行两路检索：中文重叠双字片段与英文/数字标识的 BM25，以及 Embedding 余弦相似度。语义结果需达到 `semantic_min_score`，默认 0.35；该值是旧 0.6B 模型开发冒烟样例的初始值，升级 8B 后仍需独立校准，不是通用相关性概率，替换模型后应重新校准。此前 EmbeddingGemma 的小样本阈值为 0.26。
4. 用 RRF 融合两路排名，候选最多 20 条。向量按需生成并缓存；正文或模型/服务变化后重新生成。
5. 当前项目配置 `rerank="always"`，对非空候选调用独立 Qwen3-Reranker-8B 评分服务。服务对请求与每条候选计算 yes/no logits 归一化的相关性分数，按分数排序并应用 `rerank_min_score`。`auto` 只在两路候选竞争时调用，`off` 禁用；核心偏好不参与评分。响应的索引、数量和有限分数由宿主校验，不能带入其他用户或项目的记录。
6. 查询召回最多 5 条、合计不超过 8000 字符，与常驻记录一起放入模型上下文的 `memories`。两部分总上限为 9 条、9600 字符，均为字符预算而非精确 token 预算。

仅有常驻偏好或没有匹配候选时，不调用重排模型；`auto` 模式下候选足够少也跳过。专用评分失败时保留融合顺序，不转发给主聊天模型。Embedding 不可用、超时或响应异常时，回退到 BM25，仍保留常驻偏好；不会把故障伪装成语义召回成功。

`memory_recalled` 运行事件记录用户/项目、常驻和最终 ID、两路候选数、`semantic_status`（`ok/disabled/empty/unavailable`）及 `rerank_status`。该事件不记录正文或服务错误响应。

## Embedding 配置

默认使用 Ollama，无需部署向量数据库。需在运行 Ollama 的机器准备模型：

```bash
ollama pull qwen3-embedding:8b
```

```toml
[memory]
user_id = "local"
project_id = ""
embedding_backend = "ollama"
embedding_endpoint = "http://127.0.0.1:11434"
embedding_model = "qwen3-embedding:8b"
embedding_format = "auto"
embedding_timeout_seconds = 30
semantic_min_score = 0.35
rerank = "always"
rerank_backend = "http"
rerank_endpoint = "http://127.0.0.1:11436/v1"
rerank_model = "Qwen/Qwen3-Reranker-8B"
rerank_timeout_seconds = 30
rerank_min_score = 0.5
```

本机已有其他 Embedding 模型时可替换模型名；普通聊天模型不一定支持 Embedding。使用同一模型生成查询与正文向量，缓存会按配置指纹隔离。

首次模型冷加载可能明显慢于驻留后的请求，因此默认 Embedding 预算为 30 秒。可用下方检查脚本的 `--warmup` 单独预热并记录耗时；这不会保证服务重启或模型被卸载后的延迟。

`embedding_format="auto"` 按模型名识别输入格式，支持 Ollama 的 `qwen3-embedding:8b` 和 Hugging Face 的 `Qwen/Qwen3-Embedding-8B` 名称。Qwen 查询使用 `Instruct: <记忆检索任务说明>\nQuery:<用户请求>`，文档保留描述和正文，不添加查询指令。自定义模型别名可显式设置 `embedding_format="qwen3"`。查询任务说明也计入缓存指纹，防止变更任务后复用旧向量；更换模型后，当前范围内的向量按需重新生成，无需删除旧缓存。格式依据 [Qwen3 官方模型说明](https://huggingface.co/Qwen/Qwen3-Embedding-8B#usage)。

兼容旧的 `embeddinggemma` 格式：查询为 `task: search result | query:`，文档为 `title: ... | text: ...`。未知模型使用原文，`embedding_format="plain"` 可显式禁用前缀。格式依据 [EmbeddingGemma 模型说明](https://ai.google.dev/gemma/docs/embeddinggemma/model_card#prompt-instructions)。

使用已有 OpenAI 兼容 Embedding API 时，在 `.env` 配置独立服务和密钥，不自动复用聊天网关或其密钥：

```dotenv
FINAGENT_EMBEDDING_BACKEND=openai
FINAGENT_EMBEDDING_ENDPOINT=https://your-provider.example/v1
FINAGENT_EMBEDDING_MODEL=your-embedding-model
FINAGENT_EMBEDDING_API_KEY=your-embedding-key
```

服务使用标准 `/embeddings`；Ollama 使用 `/api/embed`。非本机地址必须为 HTTPS。连接不跟随重定向，不使用聊天/行情代理。配置为 `embedding_backend="disabled"` 时，仅使用常驻偏好和关键词检索。服务协议参考 [Ollama Embed API](https://docs.ollama.com/api/embed)。

## 专用 8B 重排服务

当前 Embedding 使用 Ollama 的 `qwen3-embedding:8b`，向量为 4096 维。Reranker 使用官方 `Qwen/Qwen3-Reranker-8B` 权重，按官方模板读取查询/候选，计算末位 yes/no logits 的二分类 softmax 分数，不采样聊天答案作为相关性分数。默认采用 bitsandbytes NF4 4-bit 加载，以减少和 Embedding、分类模型并用时的显存占用；参数规模仍是 8B，量化效果需单独评测。

模型依赖放在独立环境，主 Web/CLI 环境只需要 HTTP 客户端：

```bash
.venv/bin/python -m venv .venv-reranker
.venv-reranker/bin/python -m pip install -e '.[reranker]'
.venv-reranker/bin/hf download Qwen/Qwen3-Reranker-8B --local-dir .finagent/models/Qwen3-Reranker-8B
.venv-reranker/bin/python -m finagent.reranker --model-path .finagent/models/Qwen3-Reranker-8B --quantization 4bit
```

服务默认绑定 `127.0.0.1:11436`，`GET /health` 检查实际模型、设备和量化方式，`POST /v1/rerank` 接受 `model/query/documents/top_n`。单请求最多 100 条候选、合计 30000 字符，GPU 推理串行；忙碌或故障返回 503，由宿主回退。默认 `batch_size=1`、`max_length=8192`，单请求预算 25 秒，客户端预算 30 秒。

仓库提供 [finsight-reranker.service](../../deploy/systemd/finsight-reranker.service) 用户服务。安装后可随 Web 启动：

```bash
systemctl --user link "$PWD/deploy/systemd/finsight-reranker.service"
systemctl --user daemon-reload
systemctl --user enable --now finsight-reranker.service
systemctl --user add-wants finsight-web.service finsight-reranker.service
```

独立服务前缀通过 `FINAGENT_RERANK_ENDPOINT` 设置；模型名用 `FINAGENT_RERANK_MODEL`，密钥用 `FINAGENT_RERANK_API_KEY`，阈值/超时可用 `FINAGENT_RERANK_MIN_SCORE`、`FINAGENT_RERANK_TIMEOUT_SECONDS` 覆盖。密钥不复用聊天模型凭证，不进入序列化配置及日志。HTTP 接口不跟随重定向、不读取聊天代理；非本机地址必须为 HTTPS。默认 `rerank_backend="http"`；旧聊天重排只在显式配置 `rerank_backend="chat"` 时使用，不作为故障自动后备。

0.5 是初始 yes/no 分类阈值，不是已校准的业务正确率。换成 8B 后，向量缓存会按模型指纹隔离，旧的 0.6B 向量不会复用，当前范围中的正文按需重新编码；用户/项目过滤、删除墓碑和并发删除复查保持生效。自动记忆提取与整理继续由主模型完成，Embedding 和 Reranker 都不会生成新记忆。

官方参考：[Qwen3-Reranker-8B](https://huggingface.co/Qwen/Qwen3-Reranker-8B)、[Qwen3-Embedding-8B](https://huggingface.co/Qwen/Qwen3-Embedding-8B)。

## 写入、查看和删除

自动发现参照教学版 s09：启用记忆和自动记忆时，每个有真实用户输入且正常完成的主会话回合结束后，都会发起一次独立模型请求，不再要求“记住、以后、偏好”等触发词。发现不等于写入；没有值得新增的信息时返回 `[]`。失败、取消、等待澄清、无新用户输入的后台续跑及子 Agent 不触发这一步。

当前发现请求复用主模型与现有安全过滤、调用预算和用量统计，未单独部署 Qwen 小模型。输入仅含最近 4 条真实用户消息（每条最多 3500 字符），以及当前用户/项目可见的最多 40 条已有记忆目录（合计最多 6000 字符），不把助手回答或工具输出当作记忆来源。模型语义判断偏好、纠正、稳定背景、项目决策和参考线索；候选需要引用真实用户消息来源，临时任务限制、动态行情和凭证仍有规则过滤。提取模型同时判断是否常驻、是否限定当前项目。模型也可通过 `memory_write` 保存有来源的记录。

只有新增记录才触发后续整理；完全重复或已遗忘的记录不算新增。`memory_discovered` 事件仅记录新增 ID，空数组表示本轮完成发现但未新增；异常沿用 `memory_extraction_failed`，不覆盖已完成的业务回答。发现同步执行，通常增加一次模型请求及其等待时间，预算不足时跳过并记录失败。这里的自动抽取是结构化记忆任务，不使用 Qwen3-Embedding；后者只负责召回阶段的向量编码。

CLI：

```text
/memory
/memory search 我承受不了太大亏损
/memory forget mem_<ID>
```

`search` 展示当前范围内的实际检索结果及降级状态，遵循专用 HTTP 重排配置；不会为此调用主聊天模型。

Web API：

```text
GET    /api/v1/sessions/{session_id}/memories
DELETE /api/v1/sessions/{session_id}/memories/{memory_id}
```

Web 查看和删除沿用现有本机访问边界与会话校验；页面尚无独立记忆管理面板。

删除会移除记录和对应向量，保留内容/来源的遗忘标记；标记按用户和项目隔离，不影响其他项目中的独立记录。召回和发送上下文前重新检查记录有效性，旧上下文快照的记忆字段不会再次注入。历史会话原话、回答、摘要、审计归档仍是独立数据，删除长期记忆不等于清除全部历史。

## 多用户、大语料的后续设计（尚未实现）

当前 MySQL `memory_vectors` 表仅缓存向量，并未建立 ANN 索引：每次召回扫描当前用户的 JSON、执行 BM25，并在 Python 中计算当前范围全部记录的余弦相似度。因此它适合每用户少量记忆，不能直接视为大规模向量检索服务。Web 已有第一版账号认证与用户目录隔离，后续向量数据库仍需沿用认证身份，不能信任浏览器或模型任意指定 `user_id`。

如果目标包含大量财报/公告分块、持续导入、多实例和并发检索，应引入带索引的共享检索存储。选择建议：已有 PostgreSQL 且希望简化事务和权限管理时，先评估 [pgvector](https://github.com/pgvector/pgvector)；需要独立扩展向量检索服务时，评估 Milvus。选型以真实语料的过滤后 Recall@K、P95 延迟、导入吞吐和运维成本为依据，不以用户数或固定向量数作为唯一门槛。

接入 Milvus 时建议采用以下边界：

- 私人记忆、租户私有文档与公共金融资料分别组织；同一用户的跨项目偏好与项目限定资料保持现有可见性语义。公共语料可共享索引，避免每用户重复编码。
- 记录至少包含 `tenant_id`、`user_id`、`project_id`、可见范围、原文/分块 ID、内容版本、Embedding 模型及格式版本、删除状态；金融材料另外保存证券、报告期和可信可用时间。
- 用户/租户身份来自服务端认证结果，项目权限来自服务端授权；服务端强制把这些范围和截止时间加入检索条件。BM25、向量召回和原文回读都执行同一权限检查，不能先全库 Top-K 再在应用层简单丢弃越权结果。
- 大量同构租户可采用共享 collection、租户字段过滤及 partition key；有更强隔离需求时评估 collection/database 级隔离。partition key 不是用户鉴权，也不提供该级别的 RBAC。详见 [Milvus 多租户](https://milvus.io/docs/multi_tenancy.md)和[过滤检索](https://milvus.io/docs/filtered-search.md)。
- 权限、记忆/文档状态和版本以事务数据库为准；Milvus 保存可重建的检索索引，原始材料保留在证据存储或对象存储。向量 ID 命中后，回读前再次检查权限和删除状态。通过后台索引任务处理写入、更新、遗忘、失败重试和重建，并避免旧任务复活已删除内容。
- 更换模型应建立独立向量版本，重新编码并验证后切换检索；不同模型或维度的向量不混合计算相似度。保留 BM25 + 向量 + RRF 的组合，以及少量核心偏好直接读取的路径。

本节为架构建议；本次模型切换没有引入或部署 Milvus。

## 离线记忆专项候选集

[memory_v1](../../evaluation/memory_v1/README.md) 提供48条查询和364条合成种子记忆，开发/留出各24条，覆盖检索Precision/Recall、排序MRR/NDCG、生成一致性与必要信息覆盖、错误记忆采用率。输入、种子与gold分文件保存，逐答案审核绑定实际答案和注入记忆；常驻记忆与检索排序分开计分。

本集标签为开发候选、待独立审核；数据准备、校验和评分器测试不是实际记忆效果成绩，也不能沿用关闭记忆的整体候选验收运行。校验不访问模型或真实用户记忆：

```bash
.venv/bin/python scripts/evaluate_memory_benchmark.py validate
```

## 实现和验证

- [memory.py](../../src/finagent/harness/memory.py)：范围过滤、常驻、候选融合与重排、写入、整理、删除和迁移。
- [memory_search.py](../../src/finagent/harness/memory_search.py)：BM25、Embedding/Reranker HTTP 适配、MySQL 向量缓存与 RRF。
- [host.py](../../src/finagent/harness/host.py)：跨会话范围绑定、自动提取、召回及上下文注入。
- [test_memory.py](../../tests/test_memory.py)、[test_web.py](../../tests/test_web.py)：范围隔离、无词面重合的合成语义召回、缓存、故障回退、重排边界、删除并发、自动提取及 Web 跨会话调用。

合成向量测试验证程序行为，不代表真实模型的召回质量；真实模型应另用业务样本评估相关性、遗漏、错误召回和延迟。

可运行 `python scripts/check_memory_recall.py`，在临时目录创建合成记录并调用实际 Embedding 服务，检查语义改写、代码匹配、技术栈查询、无关问题及项目隔离。该小样本检查用于连通和基本行为验证，不构成完整召回质量评测。

可用 `--warmup` 单独记录预热耗时和向量维度，用 `--scores` 输出当前范围内各合成候选的余弦分数，用 `--output <JSON路径>` 保存检查报告。例如：

```bash
python scripts/check_memory_recall.py --warmup --require-rerank --scores --output .finagent/checks/memory_recall_8b_live.json
```

2026-11-04 验证：`python -m unittest discover -s tests -t . -q` 共 700 项通过；本机 Ollama 的 `embeddinggemma` 真实召回检查 6/6 通过，包括两条没有关键词命中的风险偏好改写。实测记录在 `.finagent/checks/memory_recall_live.json`。此检查使用的临时记忆已清理，未写入真实用户记忆库。首次模型加载会增加延迟；这些样本不能证明完整业务场景的召回质量。

2026-11-04 切换至 Ollama `qwen3-embedding:0.6b`，返回 1024 维向量。沿用 0.26 阈值时，预热后的上述 6 条开发样例仅 2 条满足严格召回要求；相似度诊断记录在 `.finagent/checks/memory_recall_qwen3_baseline.json`。以同一开发集把初始阈值调整为 0.35 后，6/6 满足要求，预热后每次完整召回耗时 14–62ms，记录在 `.finagent/checks/memory_recall_qwen3_live.json`。首次冷请求曾超过原 15 秒预算而回退，因此默认预算调整为 30 秒。切换后全量回归 701 项通过（56.313 秒）；沙箱内 Starlette TestClient 启动挂起，改在沙箱外执行完整回归。这是同一小样本上的开发校准，不是独立评测，不能据此宣称优于原模型或证明生产召回质量。

更新代码或配置后，需重启既有 Web 进程，使其重新加载配置与实现。

2026-11-04 每轮发现改造：取消自动抽取的关键词门槛，增加当前范围内的已有记忆目录，空结果、重复内容和遗忘记录不触发整理。完整 Python 回归 721 项通过（65.233 秒），包括无关键词写入、连续回合各发现一次、开关与回合状态、提取异常不覆盖答复、来源校验和输入预算。另用当前配置的主模型在临时目录完成两条合成冒烟检查：无关键词的阅读偏好新增 1 条（1.95 秒），临时三句话限制新增 0 条（0.78 秒）；临时记录已清理。这不是独立质量评测，也没有验证 Qwen3.5-9B 与当前主模型的优劣。

2026-11-05：接入 Ollama `qwen3-embedding:8b`（实测4096维）与官方 `Qwen/Qwen3-Reranker-8B`（CUDA、NF4 4-bit，端口11436）。当前始终对非空候选重排，初始余弦/重排阈值分别0.35/0.5。临时合成记忆完整链路检查6/6通过，四个匹配问题的重排状态均为ok，另外两个无关问题只保留核心偏好；预热后完整召回约29–208ms。记录在 `.finagent/checks/memory_recall_8b_live.json`。样本沿用开发冒烟集，没有据此修改阈值，不是独立质量验收或量化精度评测。

同次验证：全量离线回归771项通过（88.871秒），包含专用重排协议、服务鉴权、异常回退、用户/项目隔离、并发遗忘及新模型缓存隔离测试。GPU服务已由 `finsight-reranker.service` 管理，Web空闲时重启后两项健康检查通过；原 `qwen3.5:9b` 分类服务共存检查通过。部署版本与检查记录见 `.finagent/checks/retrieval_8b_deployment.json`。
