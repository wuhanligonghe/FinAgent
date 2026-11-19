# MacBERT 意图识别：开发与验收记录

> 历史记录：保留当时的需求或验收结论，不代表当前版本状态。当前入口见 [文档首页](../README.md)；历史机器路径、日志和外部工程可能不在本工作区。


日期：2026-10-21。依据 `FinSight_需求方案_v2.1_MacBERT意图识别与服务器微调.md`，本次交付训练工具与宿主接入代码。真实数据缺失、真实模型缺失、业务效果待测、本地完整模型性能待测是四个独立状态；不能据离线 fixture 宣告 MacBERT 生产效果已通过。

## 已交付内容

独立工程现已移到 FinAgent 项目外的同级 `../training/macbert_nlu/`，可整体上传 `../training/`，包名 `finagent-macbert-nlu`。共享10个分类头和角色化 BIO 实体头，统一标签、原文偏移、上下文配对、缺标 mask、温度／置信度／margin 校准及解码。包含3090配置、固定基础版本下载、来源与分组校验、真实梯度训练、原子 checkpoint、完整恢复、评估、safetensors 导出、伴随 wheel 和交付包构建。

自建 FinAgent-NLU 作为主语料路线，不使用 OpenFinData；正式数据目录只附说明，尚无经审核的主数据集。标注指南区分未标字段／unspecified／空实体，并明确 target/excluded/background、上下文、模板家族和人工审核要求。`build_data` 不会自动把候选改成人工已验证。

FinAgent 新增 `src/finagent/nlu/` 和严格 `[nlu]` 配置。chat、run、自然语言 research 共用宿主准入服务；研究工具引用本轮 parsed_request_id 与已确认类型范围，不重新解析模型自由改写的 goal。证券工具只收到模型标注的目标片段；换股失败会清除旧可执行身份，候选与自由补充保留原任务。仅规范化实体时间／指标片段，旧全句语义正则不进入默认路径。

默认 backend 为 macbert；尚未回传合格模型时自然语言入口明确阻断，不自动使用旧正则／生成式模型。兼容基线必须显式同时启用 backend=legacy_regex 与 allow_legacy_regex=true。帮助、退出、显式状态／报告命令、结构化 ResearchRequest／DecisionRequest 继续可用。研究恢复仍沿用旧 scope／预算，不重做 NLU；普通任务恢复不会从历史 scope 恢复研究委派权限。

新增 nlu-check、nlu-parse、nlu-evaluate、nlu-benchmark，均不调用研究模型或在线行情；诊断模型只允许离线诊断，不启动研究。新增独立输入对照工具 compare，用于服务器 FP16／导出CPU／本地CPU的后续一致性验收。

## A 类验证

| 项目 | 本次证据与边界 |
|---|---|
| A01 独立复制 | `scripts/check_macbert_package.py` 将完整训练目录复制后禁止导入 finagent、禁止 socket 连接；完成训练／校准／评估／导出／伴随 wheel 构建与独立加载 |
| A02 真实训练 | 小型随机 Bert 编码器真实执行4次 optimizer 更新；11个头（含实体头）均有更新，不能计作真实 MacBERT 效果 |
| A03 缺标与边界 | 训练测试检查未标头无梯度、已标头有梯度、实体空集与未标区分、中文／混合 Unicode code point、上下文忽略和长输入拒绝 |
| A04 宿主合同 | 合成解析 fixture 检查新闻精确查询词、维度排除、研究与决策流程、概念、换股、别名、多目标、相对期间／同比／利润指标、候选失效、自由补充及研究委派范围；实际身份参数和预算有断言 |
| A05 默认与故障 | macbert 为默认；缺模型不构造远程模型，聊天帮助／退出可用；内层澄清也调用相同 NLU，普通任务恢复不回到旧语义规则 |
| A06 回归 | 保留原299项业务测试，旧测试显式选择 legacy 基线；新增宿主测试。最终数量见下方运行记录 |
| A07 构建 | FinAgent wheel 新鲜构建并解包核对 NLU 模块／命令、14个技能和30个工具；伴随 wheel 解包后断网推理通过 |
| A08 包完整性 | 检查文件库存／哈希、schema、标签顺序、预处理、权重 strict load、必要头、训练更新、固定基础版本和校准状态；烟测包不能生产加载。不能仅凭 metadata 证明任意伪造随机权重有效，仍需真实 B 门槛 |
| A09 离线 | 隔离检查实际禁止网络连接；生产加载 local_files_only、不执行远程代码；训练无自动上传／研究 API／在线跟踪 |
| A10 操作说明 | `../training/macbert_nlu/README.md` 和 ANNOTATION.md 覆盖环境／数据／训练／恢复／导出／安装／验证；实际 CLI 接受 --text、--input 等文档参数 |

运行环境：Windows、Python 3.11.15、torch 2.8.0+cpu、transformers 5.12.0、safetensors 0.7.0、numpy 2.4.6；完整37项 CPU 依赖约束在训练目录。资源预检为24逻辑CPU、约15.82GB内存，当时可用约3GB，因此只运行小尺寸 CPU 测试，没有下载或训练完整基础权重。Linux3090的 CUDA 安装和显存尚未实测。

## 运行记录与交付路径

最终通过主项目328项、独立训练工程13项，共341项离线测试；原299项业务测试全部保留。最终运行记录由 `.finagent/nlu_validation/final_checks.json` 汇总。主要命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -q
.\.venv\Scripts\python.exe -m unittest discover -s ../training/macbert_nlu/tests -q
.\.venv\Scripts\python.exe scripts/check_macbert_package.py
.\.venv\Scripts\python.exe scripts/build_wheel.py
.\.venv\Scripts\python.exe -X utf8 scripts/check_phase2_package.py
```

独立复制／断网测试详细记录：`.finagent/nlu_validation/package_check.json` 及其 audit_dir 下日志。CLI 缺模型／help 检查：`.finagent/nlu_validation/cli_check.json`。原工程发布包检查：`.finagent/v21_validation/package_check.json`。

服务器源码 ZIP：`.finagent/dist/nlu/macbert_nlu_training.zip`；伴随 wheel 同目录；FinAgent wheel：`.finagent/dist/p0/finagent-0.2.0-py3-none-any.whl`。ZIP 不含完整基础权重、正式语料、训练产物、API密钥或聊天数据。SHA-256 在 package_check 与 final_checks 中。随机烟测模型只留本地诊断目录，不作为生产模型交付。

## 尚未完成的 B/C

没有真实人工审核主语料，没有服务器训练权重。因此未执行真实 B 效果验收和 C 完整模型本地性能／跨平台验收，也未宣称分类F1、证券边界准确率、接受覆盖率或P95达标。

实际启用顺序：人工准备并分组审核语料 → 3090正式训练与验证集校准 → 独立测试集语义评估 → FinAgent受控身份回放与人工 oracle 宿主验收 → 使用资格报告导出 release_candidate → 回传成套模型与wheel → 本地一致性／性能验证。质量失败保留 trained_unqualified，不调整成全部澄清来伪造高准确率，不降低需求门槛。

本次无新增付费研究模型调用，没有使用已耗尽的旧 DeepSeek 验收授权。原 v2.1 金融研究质量验收的未完成项继续独立保留。

## 训练目录外置调整

2026-10-21 按用户要求将整个 training 目录移至 FinAgent 同级的 `../training/`。根目录 README 说明整体上传后的步骤；服务器只需 training 文件夹。项目验收脚本默认读取同级目录，也支持 `--training-dir` 指定其他位置。应用生产推理仍通过配套 wheel 加载模型，不依赖训练源码位置。移动后的验证记录见 `.finagent/nlu_validation/relocation_check.json`。

## 首批500条数据

2026-10-21 按用户要求增加 `../training/macbert_nlu/data/bootstrap_500_v1/`：500条本地编写的合成预标注，含任务、动作、深度、流程、五维三状态及角色化原文实体。预划分为训练340、验证80、测试80，按语义家族及近似表达聚类整组隔离，公司池也跨集合隔离。训练集覆盖全部分类标签、10类实体和证券的6种类型／角色组合。

全部记录保留 `annotation_status=candidate`、`synthetic=true`。逐条审阅Markdown、500条空审核决策模板、来源清单和质量报告与数据一起交付；不宣称人工金标。审核工具只有在明确批准且填写实际审核人时才导出已审核记录，并保持既有分组。正式训练校验没有放宽，仍拒收未审核候选。

本批没有读取真实聊天、持仓或新增外部数据，没有调用付费标注模型。500条由100个基础表达规格及其5种变体组成，不能算成500个独立模板；语义独立性仍需人工复核。80条待审测试输入不满足原B类至少300条独立人工验收要求，B/C状态继续待验收。

## 再新增1500条，合计2000条

按用户追加要求提供 `../training/macbert_nlu/data/expansion_1500_v1/`，以及包含首批500条的合并视图 `bootstrap_2000_v1/`。新增300种表达规格，每种5个变体，继续沿用其原始锚点的语义家族和集合，避免把近义改写放到另一集合。新增分组1020/240/240，合并分组1360/320/320；原500条所有文件哈希与记录保持不变。

两批均为合成candidate，未升级人工金标。合并数据共有400种基础表达规格，不能视作400个独立语义家族或2000条真实采集数据。合并测试320条仍缺少独立人工审核，部分标签支持也不足，B/C状态没有变化。质量报告分别保留完整支持数与校验范围，原文边界、唯一性和全字段标注已检查；字符词表上的真实BertTokenizerFast对齐检查不等于已验证完整MacBERT基础tokenizer。

新增数据、旧数据及合并数据可一起随training上传；后续审核只选择一份输入，建议选2000条合并视图，避免重复拼接。生成／复核工具为 `scripts/build_expansion1500.py`，人工审核工具仍可使用按输入数量工作的 `scripts/review_bootstrap500.py`。
