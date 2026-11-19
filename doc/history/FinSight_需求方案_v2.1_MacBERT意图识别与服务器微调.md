# FinSight 需求方案 v2.1：MacBERT 意图识别与服务器微调

> 历史记录：保留当时的需求或验收结论，不代表当前版本状态。当前入口见 [文档首页](../README.md)；历史机器路径、日志和外部工程可能不在本工作区。


日期：2026-10-21。范围标识：`NLU21`。读者：负责 FinAgent 开发与训练工具交付的 Agent。

本文是 v2.1 的意图理解专项需求，补充[现有 v2.1 决策链需求](../requirements/FinSight_需求文档_v2.1.md)，不覆盖、不重编号其研究流程和验收结论。本文状态为**待实施的开发基线**，不表示模型已训练或意图识别已完成替换。用户输入中的“MaxBERT”统一解释为 **MacBERT**。

用户要求交付两个部分：一个可单独复制到 RTX 3090 服务器的微调工程，以及使用该工程导出模型的 FinAgent 本地意图理解入口。开发者可以先完成工具和接入代码；用户在服务器完成训练、回传模型后，才具备真实模型上线验收的条件。

## 📋 1. 目标、现状与范围

### 1.1 要解决的问题

当前请求“分析一下中电鑫龙最近的新闻面”被送往证券名称搜索的查询词是“一下中电鑫龙最近”。专项分析路由命中了新闻分析师，但名称裁剪失败。只训练意图分类器而保留原名称裁剪，不能解决这个问题。

现有入口涉及：

| 位置 | 当前职责 | 本次改造要求 |
|---|---|---|
| `src/finagent/research/routing.py` | 关键词／正则判断 direct、specialist、deep 和 pipeline | 默认自然语言入口改为消费 PLM 解析结果 |
| `src/finagent/research/entry.py` | 名称裁剪、截止时间、期限及研究请求构造 | 移除默认路径中的语义正则裁剪；保留类型、范围和预算验证 |
| `src/finagent/session.py` | 名称／代码扫描、换股／指代、期间和指标继承 | 拆分会话状态读取与语义解析；MacBERT 路径不能先调用旧规则决定研究对象 |
| `src/finagent/chat.py` | 聊天分流、交互澄清、研究启动 | 一轮统一解析，澄清答案进入同一待完成请求 |
| `src/finagent/cli.py` | 单次请求、研究命令、会话入口 | 与聊天共享解析服务，避免同一句话在不同入口产生不同结果 |
| `src/finagent/tools/delegation.py` | 受控研究工具及显式参数交接 | 复用已解析范围和显式类型参数，不能再从 goal 裁剪公司名 |
| `src/finagent/data/service.py` | `resolve_entity` 和证据登记 | 继续负责名称到证券身份的确认，不由模型记忆替代 |

### 1.2 必须达到的目标

- **FR-NLU21-01**：交付可独立复制、安装、训练、恢复、评估、导出的 `../training/macbert_nlu/` 工程。
- **FR-NLU21-02**：以 `hfl/chinese-macbert-base` 为基线，一个共享编码器联合学习任务分类、分析维度、对话行为及实体片段。
- **FR-NLU21-03**：FinAgent 的默认自然语言入口使用本地已微调模型，包括公司与时间提取；不额外调用生成式大模型识别意图。
- **FR-NLU21-04**：保持证券身份验证、历史截止、预算、旧研究只读／恢复和 v2.1 完整决策链的原有约束。
- **FR-NLU21-05**：将训练工具完成、接入完成、模型完成训练和真实效果达标分别记录，禁止用合成模型测试冒充上线验收。

### 1.3 范围边界

本次不训练投资结论、行情预测或报告生成模型，不更换分析师使用的生成式模型，不调整决策链质量门。MacBERT 的结果是用户请求的语义解析，不属于金融证据。

`/help`、`/exit`、`/cancel`、带明确 ID 的状态／报告命令，以及严格格式的命令行选项仍由宿主处理。代码格式、日期归一化、枚举映射、权限和预算检查可以使用确定性代码；不得再用关键词规则决定整句意图、裁剪公司名或判断自然语言否定范围。

本文要求生产 backend 为 `macbert`。如保留旧规则，只能用于明确选择的基线评测或显式回退配置；不得成为模型缺失、低置信度或异常时的隐式回退。首次无训练模型的安装不能声称“已经切换成功”。

## 🧭 2. 总体方案与统一合同

### 2.1 训练与应用的职责

```mermaid
flowchart LR
    accTitle: MacBERT Training And Deployment
    accDescr: 服务器用允许训练的数据微调模型，评估导出后复制到本地，FinAgent 解析请求并验证证券身份后进入现有研究流程。
    train_data[可训练数据与标签] --> server_train[3090 微调与验证]
    server_train --> model_bundle[版本化模型包]
    model_bundle --> local_parse[本地 CPU 语义解析]
    user_input[用户原话与有限上下文] --> local_parse
    local_parse --> admission{宿主校验}
    admission -->|明确| entity_lookup[证券身份解析]
    admission -->|有歧义| clarification[保留请求并澄清]
    entity_lookup --> existing_flow[现有问答或研究流程]
```

### 2.2 输入合同

定义 `NLUInput`，至少包含 `text`、最近必要的用户话轮、当前已确认证券、当前研究类型及规范化范围、是否正在澄清及待填字段。上下文只携带意图相关的摘要和确认状态，不加载完整财报、证据正文、API 密钥或整个历史记录。

原始 `text` 必须保留。用于模型的空白／Unicode 处理必须有原文偏移映射，不能导致实体位置错位。当前话轮与上下文用明确分隔符编码，序列分类使用上下文，实体输出默认只指向当前话轮；指代继承另由 `entity_binding` 表达。

### 2.3 输出合同

定义严格的 `NLUResult`；未知字段拒收，所有枚举、维度顺序、offset 和分数定义随 schema 版本冻结。

| 字段 | 标签或内容 | 处理原则 |
|---|---|---|
| `schema_version` | `nlu21.1` | 与研究 workflow_version 分开 |
| `task_type` | `stock_analysis`、`data_query`、`concept_qa`、`research_status`、`research_report`、`research_resume`、`other` | 状态／报告读取和恢复不能变成新研究 |
| `dialogue_act` | `new_request`、`follow_up`、`switch_entity`、`correct`、`clarification_answer`、`cancel`、`other` | 用于追问、换股、纠正和澄清状态机 |
| `depth` | `specialist`、`deep`、`unspecified` | 未明确不等于模型无把握 |
| `workflow_preference` | `research_only`、`full_decision`、`unspecified` | 允许表达只要报告或完整辩论决策 |
| `dimension_state` | 五专家分别为 `include`、`exclude`、`unspecified` | 支持多维度及“不要新闻，只看基本面” |
| `entity_binding` | `explicit_single`、`explicit_multiple`、`reference`、`absent` | 按当前目标证券计数，不把被否定或背景实体算成目标 |
| `entities` | 当前原文中的实体片段及类型 | 每项包含 `text`、`start`、`end`、`type`、`score`；证券实体还必须包含 `role` |
| `scores` | 各任务头的原始分数与可用的校准分数 | 不把 softmax 或置信分数直接当真实正确率 |
| `model_id`、`bundle_sha256` | 当前模型版本与清单哈希 | 一轮和一份日志必须可追溯 |
| `input_status` | `ok`、`too_long`、`invalid` | 禁止静默截断后执行高成本研究 |

`start`／`end` 使用 Python 原始字符串的 Unicode code point 下标、左闭右开；必须满足 `text[start:end] == entity.text`。模型只输出 logits，以上结构由共享解码器构造，不调用 `generate()` 拼 JSON。

实体类型首期至少覆盖 `COMPANY`、`STOCK_CODE`、`LOOKBACK_WINDOW`、`TIME_CUTOFF`、`REPORT_PERIOD`、`TRADE_DATE`、`DECISION_HORIZON`、`FINANCIAL_METRIC`、`COMPARISON_MODE`、`DISPLAY_UNIT`。COMPARISON_MODE 标注“同比／环比／与上一年比较”等比较短语；REPORT_PERIOD 可为多个片段，宿主依据这些片段建立比较期间，不重新扫描整句寻找年份。显式风险偏好／持仓背景通过类型参数继续支持；若增加自然语言识别，必须增加对应标签、样本和测试，不得从用户语气猜测偏好或持仓。

COMPANY／STOCK_CODE 的 `role` 为 `target`、`excluded` 或 `background`。例如“不是兴业银行，换成中电鑫龙看看新闻”应提取 excluded=兴业银行、target=中电鑫龙，`entity_binding=explicit_single`。将角色编码到 BIO 标签（如 `B-COMPANY_TARGET`／`B-COMPANY_EXCLUDED`），共享解码器再拆为 type 和 role；禁止在解码后用否定正则猜角色。多个提及归并、证券解析和对象计数只使用 target，excluded 用于防止误选，background 不触发换股。

以下仅展示语义字段；生产结果的模型身份、分数和 input_status 由真实加载与推理填写：

```json
{
  "schema_version": "nlu21.1",
  "task_type": "stock_analysis",
  "dialogue_act": "new_request",
  "depth": "specialist",
  "workflow_preference": "unspecified",
  "dimension_state": {
    "technical": "unspecified",
    "sentiment": "unspecified",
    "news": "include",
    "fundamentals": "unspecified",
    "policy": "unspecified"
  },
  "entity_binding": "explicit_single",
  "entities": [
    {"type": "COMPANY", "role": "target", "text": "中电鑫龙", "start": 4, "end": 8},
    {"type": "LOOKBACK_WINDOW", "text": "最近", "start": 8, "end": 10}
  ]
}
```

### 2.4 标签到现有流程的映射

| 解析结果 | 宿主行为 |
|---|---|
| `concept_qa`／`other` | 普通 AgentLoop，不因出现“深度研究”“新闻”等词自动启动研究 |
| `data_query` | 普通工具查询；证券、期间和指标由规范化范围传入 |
| `stock_analysis`＋`specialist` | `ResearchRequest`，pipeline=research，使用明确包含且未排除的维度 |
| `stock_analysis`＋`deep`＋`research_only` | 原深度研究报告流程，不进入决策链 |
| `stock_analysis`＋`deep`＋其他流程偏好 | 默认 `DecisionRequest`，沿用 v2.1 多空与风险讨论 |
| `stock_analysis`＋`full_decision` | 解析为 deep；若同时明确 specialist 或排除决策，进入澄清，不能静默覆盖 |
| `research_status`／`research_report` | 读取明确关联的研究 ID，ID 不唯一时澄清 |
| `research_resume` | 调用原恢复入口，保留旧 scope、版本和剩余预算；不得新建来绕过限制 |

专项请求没有可确定维度、任务需要公司但对象缺失且不能继承、或解析字段互相矛盾时，必须澄清。深度研究未指定维度时默认五维；明确排除维度时从五维移除，全部排除则澄清。普通“分析一下某公司”但未明确深度／维度时，询问希望专项还是完整研究，不能凭默认值启动昂贵流程。

显式 `research` 子命令的 depth/pipeline 默认行为沿用现有 CLI，普通聊天使用本节映射；实现必须区分“用户显式提供的选项”和 argparse 默认值，避免默认 deep 压过模型识别到的专项请求。

## 📦 3. 可独立复制的服务器工程

### 3.1 必须交付的目录

以下为开发后应出现的结构，当前需求文档不表示这些文件已经实现：

```text
../training/macbert_nlu/
  README.md
  pyproject.toml
  requirements-train.lock.txt
  requirements-runtime.lock.txt
  configs/3090_joint.yaml
  configs/labels.json
  data/README.md
  data/examples/smoke.jsonl
  data/manifests/
  src/finagent_macbert_nlu/
    contracts.py
    modeling.py
    preprocessing.py
    decoding.py
    dataset.py
    train.py
    evaluate.py
    calibrate.py
    export.py
    predict.py
    validate_data.py
    package.py
  tests/
  scripts/train_3090.sh
  scripts/check_environment.py
```

配置中的数据、checkpoint、模型基础权重、输出路径均相对配置文件所在目录解析，不依赖启动 cwd、个人 Windows 盘符或 FinAgent 的 `.env`。README 给出 Linux bash 训练与 Windows PowerShell 安装／推理命令。

训练目录现已移出 FinAgent，位于同级 `../training/`。可整体上传 `training` 文件夹；进入其中的 `macbert_nlu` 后可以独立运行；不得 import `finagent`、`tests.support`，或读取主项目 SQLite、聊天、持仓、密钥。示例数据必须标记 `synthetic=true`、`purpose=smoke_only`，不得用它达成业务指标或发布为生产模型。

### 3.2 共享代码与分发方式

训练目录提供可构建的伴随包 `finagent-macbert-nlu`，导入名为 `finagent_macbert_nlu`。模型结构、tokenizer 输入构造、标签顺序、BIO 对齐、解码和校准逻辑在此包中保持一份实现，服务器与本地使用同一版本。

FinAgent 的 `src/finagent/nlu/` 仅实现宿主适配、状态、规范化、证券确认与范围准入，不复制另一个多任务模型实现。训练交付同时构建伴随 wheel；回传到本地的是模型目录和该 wheel。模型包 manifest 必须声明所需伴随包版本。

FinAgent 主 wheel 不内嵌大权重、不自动从未发布的 PyPI 包名安装伴随包。README 明确先从本地 wheel 安装伴随包，再安装 FinAgent。训练依赖与运行依赖分开，普通状态／帮助命令不能因未安装 torch 而 import 失败。选择 macbert 后缺依赖以 `NLU_DEPENDENCY_MISSING` 明确报错。

### 3.3 环境与复现

建议服务器环境为 Linux、Python 3.11、RTX 3090 24 GB；MacBERT-base 的标准结构适合作为单卡全参数微调基线。[模型配置](https://huggingface.co/hfl/chinese-macbert-base/raw/main/config.json)、[3090 规格](https://www.nvidia.com/en-us/geforce/graphics-cards/30-series/rtx-3090-3090ti/)

开发 Agent 必须选择并锁定实际验证过的 PyTorch／CUDA wheel、Transformers、tokenizers、safetensors、accelerate、指标依赖版本，记录 Linux 训练和 Windows CPU 推理的安装方式。不能把此文档中的建议配置宣称为服务器已验证；也不能只列无限制的 latest。

`check_environment.py` 输出 GPU 型号、可用显存、驱动／CUDA／Python／依赖版本、CPU／内存和可用磁盘；检查精度支持。CPU 冒烟允许显式启动，正式服务器训练默认要求检测到 CUDA。运行不得自动上传 Hub、开启在线实验跟踪或发送原始数据。

## 🧾 4. 数据、标签与划分

### 4.1 数据使用策略

**FR-NLU21-06**：默认训练入口只接收具有明确来源与可训练用途记录的本地 JSONL，不自动收集个人会话，不自动调用外部大模型生成训练集。

| 数据 | 本期默认用途 | 必须记录的限制 |
|---|---|---|
| FinAgent 场景自建标注集 | 首版主训练、验证和项目验收 | 自行编写种子、按场景扩展并人工核对；来源和生成方式真实记录 |
| 用户明确提供并同意用于训练的实际请求 | 后续补充训练及独立验收 | 只取请求与必要上下文，先去除个人敏感内容，禁止默认扫描整个会话库 |
| MASSIVE `zh-CN` | 首版非必需；后续可选辅助训练对照 | CC BY 4.0、保留署名与版本；原始意图／槽位不等于 FinAgent 标签 |
| MINDS-14 `zh-CN` | 首版非必需；后续可选金融客服辅助训练 | CC BY 4.0、使用转写文本；银行业务与股票研究的标签差异 |

依据：[MASSIVE 官方数据卡](https://huggingface.co/datasets/AmazonScience/massive)、[MINDS-14 官方数据卡](https://huggingface.co/datasets/PolyAI/minds14)。本表是本工程的数据准入要求，不替代对具体版本授权材料的核对。

按用户最新决定，OpenFinData 不再纳入本期训练、验证、评测或下载流程。首版不依赖其他开源语料即可开发和训练；先验证自建场景集的效果，再决定是否引入通用辅助数据。

项目主训练数据尚未就绪时，Agent 必须交付格式、校验器、标注指南和 smoke 样本，并将真实训练状态记为待数据就绪；不得凭几条测试句训练出“已完成”的生产模型。Agent 可以在用户要求构建数据时编写候选样本，但不能把自身生成或另一模型复核标记为 human_verified。

### 4.2 JSONL 数据合同

每条记录至少包含 `id`、`group_id`、`source_id`、`text`、可选上下文、`labels`、`entities`、`annotation_status`。主训练集和项目验收集要求 `annotation_status=human_verified`。人工确认的辅助来源标签也可按来源规则保留。

```json
{
  "id": "finagent_news_0001",
  "group_id": "news_phrase_family_001",
  "source_id": "finagent_manual_v1",
  "text": "分析一下中电鑫龙最近的新闻面",
  "context": {"confirmed_symbol": null, "pending_slot": null},
  "labels": {
    "task_type": "stock_analysis",
    "dialogue_act": "new_request",
    "depth": "specialist",
    "workflow_preference": "unspecified",
    "entity_binding": "explicit_single",
    "dimension_state": {
      "technical": "unspecified", "sentiment": "unspecified",
      "news": "include", "fundamentals": "unspecified", "policy": "unspecified"
    }
  },
  "entities": [
    {"type": "COMPANY", "role": "target", "text": "中电鑫龙", "start": 4, "end": 8},
    {"type": "LOOKBACK_WINDOW", "text": "最近", "start": 8, "end": 10}
  ],
  "annotation_status": "human_verified"
}
```

这是格式样例，不等于已经收集了真实训练集。缺少某任务标注用 null／缺字段和 loss mask 表示，不能将其补成 `other`、`exclude` 或 `unspecified`。`unspecified` 的含义是标注者确认用户未表达该要求；实体标注缺失不等于确认整句为 O。

来源 manifest 至少记录来源链接／作者、版本／commit、许可说明、允许用途、文件 SHA-256、转换脚本版本、人工审核状态和是否合成。数据转换要保留原始 ID，去掉选择题提示和答案字母后才能讨论类别映射；不允许直接把 A/B/C/D 当固定意图类。

### 4.3 样本覆盖和划分

首期主集应覆盖普通问答、数据查询、专项、深度报告、完整决策、五维组合及排除、概念解释、否定、换股、纠正、指代、历史截止、财报期间、比较指标、研究过程查看／恢复、范围外请求和多公司请求。

建议先编写300—500条种子样本，确认标注合同后扩充到约2,000—3,000条人工核对主样本；这是可调整的启动计划，不作为效果保证。先做一版直接微调基线，不把通用辅助训练作为前置条件。数据不足时保持待验收状态，不能用大量近重复模板补足门槛。

以2,500条为例，目标划分为train约1,700、validation约400、test约400，按组可略有调整；最终测试不能少于300条，并满足8.3的类别与关键边界覆盖。数据较少时先保障独立验证／验收样本，再决定训练规模，不能机械按比例使测试样本低于门槛。

按 `group_id` 划分，同一对话、模板家族、种子及其改写、公司替换版本和近重复内容不能跨集合；应在扩写前分配组和集合，而不是生成后随机按行切分。另留未出现在训练集中的公司简称／别名，用于验证名称边界和未知名称泛化。划分一次冻结，保存文件哈希。

阈值与模型选择只使用 validation；最终项目验收问题与答案不得参与训练、早停、阈值选择或反复调参。首次验收失败后，可分析失败形成开发／回归样例，但该集合不再是下一版的未见测试集；必须保留历史失败结果，另外冻结独立新验收集，不能只换掉失败题来提高原分数。

如后续启用辅助集，使用独立来源标签头或明确审核的映射，缺失的 FinAgent 任务头不计算 loss。必须比较“直接用主集微调”与“辅助训练后主集微调”，保留验证集表现更好的方案，不默认辅助训练一定提升效果。

### 4.4 自建数据的具体构建流程

1. 编写场景矩阵，以 task_type 为主轴；以五维选择／排除、研究深度、流程偏好、实体角色、对话行为、时间／指标表达为交叉覆盖维度。各维度不能只出现一种固定措辞。
2. 编写300—500条种子，包含规范表达、口语、最小差异对和必要上下文；检查标签是否互相矛盾。“分析一下”不能统一标成deep，“新闻”出现不能统一标成news分析。
3. 在扩展前冻结模板家族、对话组和集合。验证／测试另写表达方式，保留一批新公司／别名；不得把同一句只换股票名后分进测试集。
4. 训练侧扩充同义表达、语序、语气词、合理错别字、跨维组合、否定与纠正。只替换股票名不计作新增语义覆盖。Agent生成必须记录 `authored_by=agent`、`synthetic=true`，待人审；外部API批量生成不是默认步骤。
5. 每条标注所有已定义句级字段、证券片段及target/excluded/background角色、时间／指标／比较关系和必要上下文；用字符串切片自动检查offset。生成候选的标签也需复核，不能因为由模板生成就自动认定正确。
6. 人工复核标签与实体位置，优先检查关键边界和验收集；有条件时增加独立复核，分歧通过书面规则裁决。只有一名审核者时如实记录，不虚构多人一致意见。不允许未经人审的数据进入正式主集或冒充最终验收集。
7. 去重、组间泄漏检查、每类统计、长度／截断检查和来源核对通过后，输出train／validation／test及manifest。文本事实只用于表达用户请求，不需要采集研报正文或验证财务预测来构建NLU数据。
8. 训练后将错误分为意图、深度／流程、维度否定、实体边界／角色、上下文和范围规范化问题；优先补充对应的新训练家族，再在验证集调整，最终用未见验收集检验。

建议2,500条样本的主任务配额如下，配额是数据构建计划，不是已存在样本：

| 主任务 | 目标条数 | 例子 |
|---|---:|---|
| stock_analysis | 1,000 | 新闻专项、五维组合、深度报告、完整决策、排除辩论 |
| data_query | 500 | 行情、财报期间、归母净利润、同比、历史截止 |
| concept_qa | 300 | 概念解释、包含分析词但不要求研究的问句 |
| research_status／research_report／research_resume | 各100，共300 | 带必要研究上下文的过程查看、报告读取、恢复 |
| other | 400 | 闲聊、无关任务和范围外表达 |

对话行为、否定、公司角色、时间与五维维度作为跨主任务的交叉标签，不按上述配额再次累计数量。测试集仍按8.3单独保障覆盖，不能只按总体主任务比例抽样。

## 🧠 5. 模型与 3090 训练要求

### 5.1 模型实现

**FR-NLU21-07**：以同一个 MacBERT 编码器接任务头：task_type、dialogue_act、depth、workflow_preference、entity_binding；五维采用每维 include／exclude／unspecified 三分类；实体采用含证券角色的 BIO token classification。首期不强制 CRF。

联合意图与实体学习的设计参考 DIET，但本工程是基于 MacBERT 的独立实现，不引入 Rasa 运行依赖。[DIET 原始论文](https://arxiv.org/abs/2004.09936)

分类使用有标注样本的交叉熵，实体使用有效 token 的交叉熵，总 loss 为可配置加权和。各头按有效样本／token 数归一化；某 batch 没有该头标注时跳过，避免 NaN 或将缺标当负例。

使用 fast tokenizer 的 offset mapping；special token、padding、上下文 token 的实体 loss 为 ignore_index。对切成多个 subword 的实体定义一致的 BIO 对齐和拼接规则；训练与推理共用代码。实体不能跨当前话轮边界、不能用 tokenizer 字符串拼接代替原文截取。非法 I 标签序列需按确定性策略修复并记录，或拒收，不得无说明吞掉。

### 5.2 截断与上下文

**FR-NLU21-08**：先移除最旧的上下文，优先保留完整当前输入。当前输入仍超模型包约定的 max_length 时返回 `input_status=too_long`，由宿主要求缩短／拆分；不得截掉尾部“不用辩论”后直接启动完整链。

训练记录若目标实体或关键指令因截断丢失，默认校验失败或明确排除并统计；不允许悄悄当成有效标签。原研究请求的 12,000 字符上限不能被误称为 MacBERT 能完整理解的输入长度；类型化程序请求无需经过自然语言模型。

### 5.3 建议起步配置

| 配置 | 建议值 | 要求 |
|---|---|---|
| 基础模型 | `hfl/chinese-macbert-base` | 锁定 revision 或本地快照和哈希 |
| 训练方式 | 全参数联合微调 | 另提供冻结编码器模式用于基线 |
| 最大序列长度 | 128 | 验证上下文需求后可改256，重新校准并记录 |
| 单卡 batch size | 16 | OOM 时显式调小并记录 |
| 梯度累积 | 2 | 有效 batch 为32；最后不足累积步正确处理 |
| 精度 | FP16 混合精度 | 启用梯度缩放；FP32 冒烟可配置 |
| 编码器学习率 | `2e-5` | 新任务头学习率可独立配置，默认同值 |
| 优化器 | AdamW | weight_decay 默认0.01 |
| epoch | 最多5，配合早停 | 依据 validation，不依据最终测试集 |
| warmup_ratio | 0.1 | 配置可改，记录实际步数 |
| seed | 42 | 保存所有随机状态及数据顺序 |

以上是开发起点，不是实测显存／耗时承诺。OOM 处理不得自动缩短序列或改变数据标签；需要变化时更新配置并重新验证。

### 5.4 训练、恢复与模型选择

**FR-NLU21-09**：训练脚本支持配置文件、离线基础模型路径、随机种子、输出目录、显式 resume、freeze_encoder、CPU smoke；默认不上传模型、不调用任何研究大模型 API。

checkpoint 保存权重、优化器、scheduler、混合精度 scaler、global_step、epoch、RNG 状态、数据划分／来源／配置哈希。恢复前检查这些版本；数据或标签改变时禁止伪装成同一训练的继续。训练意外中断可以恢复，不能重置步数并覆盖原最佳模型。

每个 epoch 输出各任务指标、各类支持数、loss、学习率、显存峰值和耗时。最佳模型按 FinAgent 主验证集完整标注子集的语义 frame exact match 选择；早停 patience 默认2。frame 包含分类字段、五维状态和原文实体边界／角色。辅助来源与部分标注只单独报指标，不以其较少字段的 exact match 抬高主任务选模分数。主验证集没有完整标注子集时，正式选模失败，smoke 可单独运行。

模型选择不能只看 overall accuracy。评估至少包含 Macro-F1、五维 exact match、实体 span exact match／F1及证券角色正确率、任务完整解析率、混淆矩阵、各来源指标和失败样例。标签不足时标为未覆盖，不将缺失类别计作自动正确；每个必需任务头必须实际获得有效训练标注并被更新，不能用辅助集训练了编码器而未训练业务头的模型发布。

### 5.5 校准与拒识

**FR-NLU21-10**：在 validation 上校准分类分数和接受阈值，至少同时考虑 top1 分数、top1/top2 差值、必要实体可靠性和跨头一致性。低分、冲突、多实体无法归并、未覆盖类型、越界范围一律产生明确的澄清／拒识原因。

`thresholds.json` 记录校准方法、验证集哈希、每头／每维阈值、必要实体条件、校准前后指标、接受覆盖率和错误率。阈值不可在最终测试集上搜索；不能通过全部澄清获得虚假的高准确率。分布外输入必须有专门样本，不依赖 softmax 数值作唯一判断。

## 💾 6. 导出、本地加载与模型身份

### 6.1 版本化交付包

**FR-NLU21-11**：导出目录至少包含：

```text
finagent-macbert-nlu-<model_id>/
  manifest.json
  config.json
  model.safetensors
  tokenizer.json
  tokenizer_config.json
  special_tokens_map.json
  vocab.txt
  labels.json
  preprocessing.json
  thresholds.json
  training_summary.json
  evaluation.json
  provenance.json
  checksums.sha256
```

manifest 包含模型 ID、schema／架构／预处理／伴随包版本、基础模型 revision、各任务头与实体类型、max_length、训练和验证数据指纹、训练步数、生产／smoke 标记、质量状态及有效载荷文件的校验信息。包内不包含原始私有训练集、服务器绝对路径凭证或任意可执行 Python 代码。

哈希规则固定为：manifest 的 payload_hashes 覆盖除 `manifest.json`、`checksums.sha256` 外的全部模型载荷；`bundle_sha256` 是 manifest 以排序键、无多余空白、UTF-8 编码序列化后的 SHA-256，不回写进 manifest 自身。`checksums.sha256` 可列出 manifest 文件和所有载荷的文件字节哈希，但不包含自身，避免循环引用。manifest 同时记录包外伴随 wheel 的文件名／版本／哈希，伴随 wheel 不包含本模型目录。

单独发布所需伴随 wheel 及其 SHA-256，避免训练代码和本地模型结构漂移。多任务结构不能仅以 `AutoModelForSequenceClassification` 加载后静默忽略其他头。

### 6.2 载入约束

本地默认 CPU、eval 模式和 inference_mode，进程内只初始化一次；模型切换需要显式重载并记录。必须检查 manifest、schema、标签、预处理和权重匹配，strict load，禁止 ignore_mismatched_sizes 后用随机头继续。

使用 safetensors 载入训练交付权重，`local_files_only=True`，`trust_remote_code=False`。缺文件、篡改、版本不匹配、未训练／smoke 包以明确错误阻断生产自然语言入口；禁止联网自动拉基础 MacBERT 来冒充训练模型。单独的开发 smoke 命令可读 smoke 包，但不得启动真实研究。

随机头的校验边界必须真实：strict load 防止缺头后随机初始化；训练摘要登记每头有效标注数、更新步数和初始化／训练后参数摘要，导出验证 checkpoint 与模型参数一致。仅凭权重或自报 metadata 无法证明一个恶意伪造的模型经过有效训练；实际能力由B类独立评测证明，不能宣传存在“识别任意随机权重”的通用检查。

如模型完成训练但质量未达标，标记 `trained_unqualified`，允许离线评估与诊断，生产准入仍失败；不得把训练步数大于0等同于可用。只有完成约定评估并导出 `release_candidate` 的模型能进入正式本地验收。

### 6.3 跨平台一致性

服务器导出前、导出后重新加载、Windows 本地 CPU 加载必须使用同一批输入对照。分类标签、维度和实体边界一致；logits 按记录的容差比较，不能要求跨设备逐 bit 相同。

首期以 PyTorch CPU 为必需路径。ONNX／INT8 为后续优化，不是首期开发完成前置条件；如实现，必须通过独立的精度与拒识回归，不自动沿用未量化结果和阈值。

## 🔌 7. FinAgent 集成行为

### 7.1 模块、配置与诊断

**FR-NLU21-12**：新增 `src/finagent/nlu/`，建议至少包含 `contracts.py`、`parser.py`、`macbert.py`、`normalization.py`、`admission.py`。`parse_request` 返回一次解析及准入决定；`request_from_goal` 接受已解析结果，不能重复加载／推理或再次裁剪名称。

新增严格的 `[nlu]` 设置，开发后示例：

```toml
[nlu]
backend = "macbert"
model_dir = "models/finagent-macbert-nlu"
device = "cpu"
cpu_threads = 4
local_files_only = true
allow_legacy_regex = false
store_raw_text = false
```

model_dir 相对主配置文件解析；阈值和 max_length 来自模型包，默认不得在本地悄悄覆盖。诊断显示实际 backend、model_id、模型状态、模型路径和依赖情况。新增只执行本地解析的 `finagent nlu-parse --text ...`、模型检查的 `finagent nlu-check`、性能验证的 `finagent nlu-benchmark --input ...`，均不得调用研究大模型或在线证券源。

缺依赖、缺模型、模型不兼容分别为 `NLU_DEPENDENCY_MISSING`、`NLU_MODEL_MISSING`、`NLU_MODEL_INCOMPATIBLE`；质量不达标、过长、推理失败也使用独立代码。聊天保持可读的错误提示，仍允许帮助／退出／状态命令；自然语言请求不能默默进入旧规则或生成式模型。

### 7.2 证券身份与换股

**FR-NLU21-13**：模型提取的 target COMPANY／STOCK_CODE 只作为身份解析输入；证券查询必须复用现有市场限制、唯一匹配、候选确认和证据登记。新闻搜索最终使用规范化证券身份，不能使用整句或包含“一下／最近”的裁剪词。被否定的证券不能因与已确认上下文相同就重新继承。

优先处理当前话轮明确对象；其次在 `entity_binding=reference` 且上下文唯一时继承已确认对象；没有当前对象和可靠指代时，才使用启动默认证券。CLI `--symbol` 在普通聊天中的既有“默认对象而非强制锁定”语义保持不变。严格结构化 API 调用可直接传 symbol；若同时提供与正文明确对象冲突的类型化 scope，应返回范围冲突，不能静默覆盖。

换股或纠正时先清除旧证券身份／研究关联的可执行绑定，再解析新对象；新名称解析失败必须澄清，不能退回旧公司查新闻。可以保留已确认的显示偏好和用户明确沿用的期间，但不能继承旧证据为本轮新证据。

多公司提及可能是同一证券的简称／全称，不应机械拒绝；先按合法解析路径归一化，仍是多个证券且当前研究只支持单标的时澄清。不得自动选择第一个。

### 7.3 日期、指标和范围归一化

**FR-NLU21-14**：宿主仅规范化模型标记的时间／指标片段及显式类型参数，不能重新扫描原句来改变模型意图或猜测公司边界。所有归一化结果记录来源是 explicit、context 或 default。

- 新闻“最近”默认沿用7天，政策沿用90天；无明确窗口也使用现有默认值。明确“最近两周”转14天，超合同范围要求用户缩小，不静默 clamp。
- 证据 cutoff 默认本轮 now（Asia/Shanghai）；显式历史截止应用到全部取证工具，日期仅指定到日时按现有截止规范处理。当日截止不得被设到未来。
- `REPORT_PERIOD`、`TRADE_DATE`、`TIME_CUTOFF` 和 `DECISION_HORIZON` 必须区分；“未来三个月”不能变成证据截止，“去年年报”不能被解释成新闻窗口。
- 财报年度／季度／半年报、同比比较、归母／合并净利润、显示单位沿用原合同；比较方式来自 COMPARISON_MODE，期间来自 REPORT_PERIOD。明确的同比可按规范化期间生成上年同口径期间；同比和环比不能互换，无法规范化或期间缺失则澄清，不继承冲突的旧范围。
- 只有输出了可靠指代／承接关系，才沿用上轮范围；历史研究查看／恢复按已存 scope，不按当前 now 重写。

### 7.4 澄清与取消

**FR-NLU21-15**：待澄清状态保存原始请求、已确认字段、待填字段、候选及其规范化身份、model_id。用户补充内容按该字段和当前话轮重新解析，不再往 goal 追加“；研究对象：”后交给正则读取。

若补充是候选序号，宿主可以按当前已展示候选确定性绑定；过期序号／不同候选列表必须拒绝。自由文本补充仍经模型和实体解析，保留原始新闻／专项／深度要求。用户输入新的完整任务或明确换股时，取消旧待填状态并展示新的准入结果；输入取消则不启动研究。

### 7.5 所有入口与历史兼容

**FR-NLU21-16**：chat、普通单次 run、自然语言 research 入口共享同一个解析服务。一次宿主接纳生成仅在当前执行上下文有效的 `parsed_request_id`，关联原始用户请求、NLUResult 和冻结的规范化 scope；工具委派必须引用它并传类型化参数，宿主比较参数与该 scope，不重复解析模型生成的 goal 来检测语义冲突。

`start_stock_research` 的研究目标使用宿主保存的原始用户请求或宿主从已接纳字段生成的摘要。模型传入的自由 goal 不能替换研究对象、范围、深度或被排除维度，也不能扩充本轮用户授权。缺少关联或类型化参数冲突返回 `RESEARCH_SCOPE_MISMATCH`／原有范围错误。由用户发起的新自由文本任务作为新根请求允许一次解析，不能以“工具补充说明”为名解析第二个不同目标。旧程序通过完整类型化 ResearchRequest／DecisionRequest 调用仍沿用原路径。

旧 `ResearchRequest`／`DecisionRequest`、研究 scope 哈希和研究 ID 不因更换 NLU 自动重写。不要把新字段塞进旧 scope 来改变序列化；NLU 元数据可作为新建请求的旁路审计记录。研究 runner 和恢复流程继续使用已冻结 scope，不重新运行 NLU 改写历史任务。

旧正则模块可以暂时保留用于显式 baseline，但默认入口不得调用其语义判断。历史单元测试可显式指定 baseline 或注入 NLU fixture，旧证券切换、截止、预算和决策链行为断言不得删除。不能让伪模型长期成为生产默认值。

### 7.6 本地日志与调用边界

默认记录 model_id、backend、耗时、规范化字段来源、接受／澄清／拒绝原因及错误码。原始用户文本默认不额外落 NLU 训练日志，显式开启后才保存；已有会话日志遵守原配置。不得自动上传会话去训练或自动回传服务器。

NLU 本地推理与研究模型用量分开：本地一次前向不能记成付费 LLM 调用，也不能给研究预算新增免费次数。实体解析的真实来源请求继续计账。每条自然语言请求正常只前向一次；一次澄清补充允许一次新的前向，不能在多层入口重复执行。

## 🧪 8. 验收、性能与完成判定

### 8.1 必测的场景合同

| 场景 | 必需行为 |
|---|---|
| 分析一下中电鑫龙最近的新闻面 | news 专项；COMPANY 精确为“中电鑫龙”，窗口7天；身份工具只收到该名称 |
| 看看这家公司的新闻和基本面 | 两维；有唯一有效上下文时继承，否则澄清 |
| 不要新闻，只看基本面 | news=exclude、fundamentals=include，不能因出现新闻而选中新闻分析师 |
| 深入研究贵州茅台，不用多空辩论 | deep＋research_only，不启动决策链 |
| 对贵州茅台做完整研究和多空、风险讨论 | deep＋full_decision，进入 v2.1 原链 |
| 什么是多空辩论／新闻面分析是什么意思 | 概念解释，不查股、不启动研究 |
| 不是兴业银行，换成中电鑫龙看看新闻 | 兴业银行=excluded、中电鑫龙=target、explicit_single；解析新对象，不查询旧证券，不误报多公司 |
| 那它最近有什么利空 | 唯一上下文＋新闻分析；无上下文时澄清 |
| 截至2025-05-08分析某公司最近新闻 | 历史 cutoff；取证均不越界，不能换成当前新闻 |
| 看去年年报的归母净利润同比 | REPORT_PERIOD、FINANCIAL_METRIC、COMPARISON_MODE分别提取；期间／指标／上年同期范围准确，不解释成新闻窗口 |
| 贵州茅台和宁德时代一起研究 | 单标的范围限制下澄清，不偷偷选第一家 |
| 候选序号／公司简称补充 | 绑定当前有效候选，保留原分析任务和待填状态 |
| 长输入末尾有“不要辩论” | 完整处理或明确 too_long；不能丢掉否定后启动完整链 |
| 看刚才研究的进度／继续上次研究 | 分别读取或恢复明确 ID，不新建、重置预算或改变历史截止 |
| 模型缺失／缺失头或显式未训练包／标签顺序篡改 | 明确拒绝生产加载，不能伪装成低置信度或切回规则；任意伪造随机权重的能力问题按6.2与B类评估处理 |

### 8.2 代码与工具验收 A

无需用户提供服务器凭证或真实训练权重即可完成：

| AC | 验收标准 |
|---|---|
| A01 | 独立复制训练目录到临时工作区后，不依赖 FinAgent 源码运行数据校验、CPU smoke、评估和导出检查 |
| A02 | 真实小尺寸随机初始化编码器能完成一次前向／反向、checkpoint 保存恢复；明确 smoke，不计模型质量 |
| A03 | 多任务缺标 loss mask、生效的梯度、BIO offset、中文及混合字符、上下文和截断均有测试 |
| A04 | 用轻量 NLU fixture 验证8.1全部宿主行为，断言实际调用的 identity 参数、scope、pipeline 与预算 |
| A05 | 默认生产入口无语义正则回退、无额外 LLM 意图调用；缺模型命令保持可诊断 |
| A06 | 原有测试回归通过；改变注入接口可以更新测试装配，不得移除原有业务断言 |
| A07 | 独立构建并解包安装 FinAgent wheel、伴随 wheel；资源、CLI、无依赖帮助路径和相对模型路径通过 |
| A08 | 模型损坏、缺失头、显式未训练／smoke 包、错误 schema、基础 revision／预处理不兼容均拒收；训练与导出头参数一致性可查，不能仅靠 metadata 宣称识别所有随机模型 |
| A09 | 断网时本地推理不尝试 HTTP；训练默认没有 Hub 上传、外部模型调用或在线跟踪 |
| A10 | 文档的训练、恢复、导出、本地检查命令与实际 CLI help 一致，来源准入和缺数据状态真实 |

### 8.3 模型与业务效果验收 B

用户在3090上训练并回传模型后执行，不可用 fixture 代替：

- 至少300条独立人工核对的项目验收输入，与 train／validation 按组隔离；覆盖新公司、上下文、组合维度、范围外输入。每个首期必须支持的 task_type、dialogue_act 标签至少20条，五维状态与必要实体类型分别报告支持数；缺少必要类别时补充样本，不用未覆盖宏平均掩盖。
- 至少100条关键边界，否定、换股／纠正、历史截止、概念解释和澄清各至少20条；这些样例错误进入昂贵流程、错误继承证券或越过 cutoff 的次数必须为0。
- task_type Macro-F1 ≥0.95；研究深度／流程偏好和五维状态分别报告，五维 exact match ≥0.95；显式公司提及的 span exact match ≥0.98；其他实体 span F1 ≥0.95。
- 默认阈值下，设 N 为全部非歧义可执行业务样本，A 为其中自动接受的样本，C 为其中完整字段／实体／角色／范围均正确的自动接受样本。自动处理准确率 C/A ≥0.95、接受覆盖率 A/N ≥0.90，同时报告自动正确比例 C/N。A=0不能通过；拒识和澄清单独计数，不当成自动处理正确。
- 范围外检测召回率 ≥0.95，分母为人工标注的范围外输入，命中定义为正确路由 other 且不启动金融取证／研究，或明确拒识。对需要澄清的歧义样本，另报正确澄清率及误启动数，不能将错误接受从报告中排除。
- 端到端验收终点是正确的身份解析参数、准入结果与研究／普通查询范围；受控证券工具可回放，但必须使用真实微调模型。在线行情与研究报告金融质量继续按原 v2.1 验收，不能由本指标替代。
- 样本不足或某必要标签没有足够测试支持，状态为“未覆盖／待补样本”，不能把指标记为达标。关键字段须报告分母、每类支持数及失败样例。

本节是产品验收目标，不是预估训练效果。若不达标，提交失败分析及真实指标，继续补数据／改模型；禁止靠修改阈值到全部澄清、挑选样例或降低质量门宣布完成。

### 8.4 性能与跨平台验收 C

使用用户本地 CPU；记录 CPU、内存、线程、系统、依赖版本、模型哈希及输入 token 长度。模型常驻，先20次预热，再对至少500条预先固定输入测 P50／P95、吞吐和进程峰值内存；冷启动单独报告。

建议目标为128 token、batch=1、4个CPU线程时 warm P95 ≤500ms。该值是待实测的产品目标，不是已验证承诺；若本地硬件不同，先记录实际结果并明确性能验收是否通过，不能换更强设备后宣称用户本地达标。金融取数网络耗时不混入纯 NLU 延迟，也不能把整个研究任务说成因此达到该延迟。

服务器 FP16 验证、导出后 CPU 推理、本地 CPU 推理对同一组输入的关键决策必须一致；接近阈值导致的差异需要列出并修复。量化版本如有，单独报告。

### 8.5 完成状态

| 状态 | 可以声明 | 不可声明 |
|---|---|---|
| A通过，真实数据／模型未提供 | 训练工具与接入代码已交付，等待服务器训练 | 默认意图理解已具备可用效果 |
| 已在服务器训练，B未通过 | 已产生真实微调模型，效果待改进 | 意图识别已替换上线成功 |
| A、B通过，C未通过 | 功能与质量通过，本地性能待优化 | 用户本地性能达标 |
| A、B、C全部通过 | 本专项交付完成，可使用 MacBERT 生产入口 | 原 v2.1 其他未完成金融验收自动完成 |

首次交付必须写 `doc/MacBERT意图识别_开发与验收记录.md`，记录每项 AC、真实环境、产物路径和未完成条件。没有服务器训练权限不妨碍完成 A；不得为凑齐 B/C 在本地偷偷启动长时间 GPU 训练或新增付费模型研究。

## 🛠️ 9. 开发顺序与操作合同

### 9.1 实施顺序

1. **M-NLU21-1：合同与数据**。冻结标签、输入／输出／数据 schema、标注指南、manifest 和分组划分；建立旧规则基线。
2. **M-NLU21-2：独立训练工程**。实现模型、部分标注 loss、3090 配置、校验、CPU smoke、checkpoint 恢复、评估和校准。
3. **M-NLU21-3：模型交付**。实现 safetensors 导出、manifest、校验、伴随 wheel 和统一推理解码，验证复制后可独立使用。
4. **M-NLU21-4：FinAgent 接入**。改造所有自然语言入口、会话状态、实体／时间范围及澄清状态机，完成A类回归；缺模型时如实阻断自然语言入口。
5. **M-NLU21-5：服务器训练**。用户提供合格数据，在3090训练；开发 Agent 提供命令与诊断，不假设能登录服务器。
6. **M-NLU21-6：本地验收**。回传完整模型包及伴随 wheel，执行B/C；达标后更新启用说明与验收记录。

### 9.2 开发后必须支持的命令

以下定义 CLI 的期望合同；实现完成后须用实际 help 和运行记录核对，当前不是已可执行命令。

服务器在独立训练目录中：

```bash
python -m venv .venv
source .venv/bin/activate
# 按 README 先安装锁定的 CUDA 版 torch，再安装本工程的训练依赖。
python -m pip install -e '.[train]'
python scripts/check_environment.py
python -m finagent_macbert_nlu.validate_data --config configs/3090_joint.yaml
python -m finagent_macbert_nlu.train --config configs/3090_joint.yaml
python -m finagent_macbert_nlu.train --config configs/3090_joint.yaml --resume outputs/run/checkpoint-last
python -m finagent_macbert_nlu.calibrate --checkpoint outputs/run/best --split validation
python -m finagent_macbert_nlu.evaluate --checkpoint outputs/run/best --split test
python -m finagent_macbert_nlu.export --checkpoint outputs/run/best --output exports/model
python -m finagent_macbert_nlu.package --model exports/model --output exports/delivery
```

真实模型训练前必须有数据并通过来源／字段校验。`--resume` 是训练中断后的替代命令，不是每次正常训练后必须再执行一次。`best`、`checkpoint-last` 为逻辑入口，具体目录由实现固定，不得让用户猜文件名。

`package` 输出伴随 wheel、完整模型目录／压缩包、SHA-256 以及评估报告。不得静默覆盖同名生产 model_id；打包命令也不能把不合格模型升级为 release_candidate。

本地 Windows，在 FinAgent 目录使用已有虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m pip install <回传的伴随wheel完整路径>
# 将模型目录放到配置的 models/finagent-macbert-nlu，并启用 [nlu]。
.\.venv\Scripts\python.exe -m finagent nlu-check
.\.venv\Scripts\python.exe -m finagent nlu-parse --text "分析一下中电鑫龙最近的新闻面"
.\.venv\Scripts\python.exe -m finagent nlu-benchmark --input <固定验收输入JSONL路径>
```

### 9.3 Agent 交付清单

- 独立训练目录、锁定依赖、数据格式与人工标注指南、来源准入与防泄漏校验。
- 共享模型／预处理／解码伴随包，3090 训练配置、恢复、指标、校准与导出工具。
- FinAgent 统一 NLU 模块、严格配置、所有入口接入、证券／范围／澄清状态机。
- 新增测试与保留的旧回归，随机／合成测试清楚标识。
- 数据缺失、模型缺失、效果未达标和本地性能未测的独立状态。
- 安装／训练／复制／本地启动说明，以及逐条验收记录。

Agent 执行本文开发时，应先读取仓库当前状态与相关约束，保留用户修改；先完成A类交付，再等待用户训练产物。不能把“没有微调模型”当成停止编写训练代码和接入代码的理由，也不能以文档完成代替代码完成。
