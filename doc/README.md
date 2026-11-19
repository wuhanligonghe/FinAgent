# FinSight / FinAgent 文档

更新日期：2026-11-15。产品名 FinSight，Python 包 `finagent`，当前代码版本 3.1.0。

## 快速入口

| 你要做什么 | 从这里开始 |
| --- | --- |
| 使用或维护登录与用户隔离 | [登录使用指南](guides/LOGIN.md)、[登录系统开发方案](security/LOGIN_SYSTEM_DESIGN.md)，视觉参考为[现有登录页设计图](../image/FinSight金融研究登录页.png) |
| 启动或排查项目 | [运行说明](guides/RUNNING.md)、[Web 工作台](guides/WEB_APP.md) |
| 理解整体设计 | [项目设计](overview/PROJECT_DESIGN.md)、[输入到结果的完整链路](overview/INPUT_TO_RESULT_GUIDE.md)、[代码结构](overview/ARCHITECTURE.md) |
| 维护 MySQL 业务存储 | [运行与交付状态](features/MYSQL_STORAGE.md)、[迁移方案](requirements/MySQL存储迁移方案.md) |
| 查看记忆与工具机制 | [统一工具注册](features/UNIFIED_TOOLS.md)、[工具渐进披露与缓存](features/TOOL_DISCOVERY.md)、[Redis 短期记忆](features/REDIS_RUNTIME.md)、[长期记忆](features/MEMORY.md)、[任务理解](features/TASK_UNDERSTANDING.md) |
| 评估当前效果与限制 | [评测目录](evaluation/README.md)、[当前候选集实测](evaluation/CURRENT_EVALUATION_RESULTS.md) |
| 整理项目介绍与准备面试 | [面试准备指南](overview/INTERVIEW_PREPARATION.md)、[项目特点与简历说明](overview/RESUME_PROJECT_HIGHLIGHTS.md)、[详细设计清单](overview/PROJECT_DESIGN_CHECKLIST.md) |

## 按主题浏览

| 目录 | 内容 |
| --- | --- |
| [overview/](overview/README.md) | 项目设计、架构、输入输出链路、设计清单、能力来源和项目介绍 |
| [guides/](guides/README.md) | 安装运行、模型与代理配置、Web 启动和操作 |
| [features/](features/README.md) | 记忆、任务理解、工具发现、Skills、研究交接和回复行为 |
| [security/](security/README.md) | 登录与用户隔离开发方案、事实核验、注入与泄露防护 |
| [models/](models/README.md) | 工具意图分类、旧微调 NLU、本地接入和提示词适配 |
| [data/](data/README.md) | 金融来源、指数、行业排名、接口探测和数据迁移 |
| [evaluation/](evaluation/README.md) | 验收集、业务实验、结果与修订；`experiments/` 保存实验快照 |
| [requirements/](requirements/README.md) | 各版本需求、意图识别专项和参考源码基线 |
| [history/](history/README.md) | 历史需求、阶段验收、v3 重建、代码审视和文档迁移记录 |

## 阅读边界

当前行为以源码和生效配置为准；设计方案、历史记录和评测结果分别描述目标、当时实现和特定测试条件，不能相互替代。Web 已实现第一版账号登录与独立用户目录，默认仍只监听本机；组织共享、多实例和向量数据库扩容属于后续建议。

需求按 v2.0 → v2.1 → v2.2 → v3.0 继承；[v3.1 更新](requirements/FinSight_v3.1_统一工具注册更新方案.md)统一聊天与研究工具注册。旧需求继续用于理解独立研究与决策链，不从版本号推定质量已验收。历史微调 Qwen 与当前工具分类、Embedding 分别是独立路径，按对应文档和配置理解。

金融数据文档保留原有来源、时间戳和单位限制；未提供市场时间的行业返回不能视为实时行情。测试结果也不自动代表真实业务效果。

## 机器读取样本与实验快照

以下四个 JSON 由脚本或示例命令直接引用，因此保留在原位置；它们是数据文件，不是散落的说明文档。

| 文件 | 用途 |
| --- | --- |
| [public_samples.json](public_samples.json) | 公开材料准备清单 |
| [announcement_sample.json](announcement_sample.json) | 公告导入元数据示例 |
| [p0_scenarios.json](p0_scenarios.json) | P0 开发回放样例 |
| [harness_v3_cases.json](harness_v3_cases.json) | Harness v3 检查样例 |

业务实验快照整体归入 [evaluation/experiments/business_v1/](evaluation/experiments/business_v1/README.md)，其中所有文件内容和摘要清单保持不变；记录中的旧发布路径按其生成时间理解。根目录下的 `evaluation/acceptance_v1/` 及历史 JSON 数据路径不变。

新增文档应放入对应主题目录，并更新该目录索引；首页保留少量常用入口。新的评测结果记录日期、协议和限制，冻结实验文件不通过整理文档改写。

本次整理的移动关系、校验范围和兼容说明见[文档分类迁移记录](history/DOCUMENTATION_REORGANIZATION_20261104.md)。
