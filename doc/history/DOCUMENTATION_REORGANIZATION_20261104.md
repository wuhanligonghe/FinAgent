# 文档分类与登录方案交付记录

日期：2026-11-04。

本次仅编写登录与用户隔离开发方案、移动说明文档并更新相对链接和索引；未实现登录、修改业务代码、迁移运行数据或改变当前用户权限。

## 分类与移动

| 原路径 | 新路径 |
| --- | --- |
| `doc/PROJECT_DESIGN.md` | [doc/overview/PROJECT_DESIGN.md](../overview/PROJECT_DESIGN.md) |
| `doc/ARCHITECTURE.md` | [doc/overview/ARCHITECTURE.md](../overview/ARCHITECTURE.md) |
| `doc/INPUT_TO_RESULT_GUIDE.md` | [doc/overview/INPUT_TO_RESULT_GUIDE.md](../overview/INPUT_TO_RESULT_GUIDE.md) |
| `doc/PROJECT_DESIGN_CHECKLIST.md` | [doc/overview/PROJECT_DESIGN_CHECKLIST.md](../overview/PROJECT_DESIGN_CHECKLIST.md) |
| `doc/RESUME_PROJECT_HIGHLIGHTS.md` | [doc/overview/RESUME_PROJECT_HIGHLIGHTS.md](../overview/RESUME_PROJECT_HIGHLIGHTS.md) |
| `doc/HARNESS_PARITY.md` | [doc/overview/HARNESS_PARITY.md](../overview/HARNESS_PARITY.md) |
| `doc/RUNNING.md` | [doc/guides/RUNNING.md](../guides/RUNNING.md) |
| `doc/WEB_APP.md` | [doc/guides/WEB_APP.md](../guides/WEB_APP.md) |
| `doc/DEEP_RESEARCH_CHAT.md` | [doc/features/DEEP_RESEARCH_CHAT.md](../features/DEEP_RESEARCH_CHAT.md) |
| `doc/MEMORY.md` | [doc/features/MEMORY.md](../features/MEMORY.md) |
| `doc/TASK_UNDERSTANDING.md` | [doc/features/TASK_UNDERSTANDING.md](../features/TASK_UNDERSTANDING.md) |
| `doc/SKILLS.md` | [doc/features/SKILLS.md](../features/SKILLS.md) |
| `doc/TOOL_DISCOVERY.md` | [doc/features/TOOL_DISCOVERY.md](../features/TOOL_DISCOVERY.md) |
| `doc/OUTPUT.md` | [doc/features/OUTPUT.md](../features/OUTPUT.md) |
| `doc/FACT_CHECKING.md` | [doc/security/FACT_CHECKING.md](../security/FACT_CHECKING.md) |
| `doc/PROMPT_INJECTION_DEFENSE.md` | [doc/security/PROMPT_INJECTION_DEFENSE.md](../security/PROMPT_INJECTION_DEFENSE.md) |
| `doc/INTENT_ROUTING.md` | [doc/models/INTENT_ROUTING.md](../models/INTENT_ROUTING.md) |
| `doc/NLU.md` | [doc/models/NLU.md](../models/NLU.md) |
| `doc/QWEN35_LOCAL_INTEGRATION.md` | [doc/models/QWEN35_LOCAL_INTEGRATION.md](../models/QWEN35_LOCAL_INTEGRATION.md) |
| `doc/PROMPT_ADAPTATION.md` | [doc/models/PROMPT_ADAPTATION.md](../models/PROMPT_ADAPTATION.md) |
| `doc/INDEX_DATA.md` | [doc/data/INDEX_DATA.md](../data/INDEX_DATA.md) |
| `doc/INDUSTRY_RANKING.md` | [doc/data/INDUSTRY_RANKING.md](../data/INDUSTRY_RANKING.md) |
| `doc/SECTOR_DATA_PROBE.md` | [doc/data/SECTOR_DATA_PROBE.md](../data/SECTOR_DATA_PROBE.md) |
| `doc/BUSINESS_EXPERIMENTS.md` | [doc/evaluation/BUSINESS_EXPERIMENTS.md](../evaluation/BUSINESS_EXPERIMENTS.md) |
| `doc/BUSINESS_EXPERIMENT_RESULTS.md` | [doc/evaluation/BUSINESS_EXPERIMENT_RESULTS.md](../evaluation/BUSINESS_EXPERIMENT_RESULTS.md) |
| `doc/BUSINESS_EXPERIMENT_REVISIONS.md` | [doc/evaluation/BUSINESS_EXPERIMENT_REVISIONS.md](../evaluation/BUSINESS_EXPERIMENT_REVISIONS.md) |
| `doc/CURRENT_EVALUATION_RESULTS.md` | [doc/evaluation/CURRENT_EVALUATION_RESULTS.md](../evaluation/CURRENT_EVALUATION_RESULTS.md) |
| `doc/ACCEPTANCE_DATASET.md` | [doc/evaluation/ACCEPTANCE_DATASET.md](../evaluation/ACCEPTANCE_DATASET.md) |
| `doc/TASK_UNDERSTANDING_ACCEPTANCE.md` | [doc/evaluation/TASK_UNDERSTANDING_ACCEPTANCE.md](../evaluation/TASK_UNDERSTANDING_ACCEPTANCE.md) |
| `doc/V3_REBUILD.md` | [doc/history/V3_REBUILD.md](../history/V3_REBUILD.md) |
| `doc/AI_TUTORIAL_REVIEW.md` | [doc/history/AI_TUTORIAL_REVIEW.md](../history/AI_TUTORIAL_REVIEW.md) |

## 实验快照与数据兼容

- `doc/experiments/` 整体移动到 `doc/evaluation/experiments/`。13 个实验快照文件的内容与迁移前逐字节一致，`FILES.json` 保持不变；原 README 的 `../../BUSINESS_*.md` 链接在新目录仍能正确指向评测说明。
- 原记录中的 `public_snapshot: doc/experiments/business_v1/` 是历史发布位置，保留原文；当前入口为[业务实验快照](../evaluation/experiments/business_v1/README.md)。
- `doc` 根目录的 4 个 JSON、`history/phase1_cases_*.json`、版本需求基线及项目根 `evaluation/` 路径保持不变，避免改变脚本和已冻结的评测协议。
- 31 份根目录说明文档按主题归档；不保留旧路径占位文件，后续引用使用[分类索引](../README.md)。
- 整理前临时备份为 `/tmp/finagent-doc-before-auth-20261104.tar.gz`。这是当前机器上的恢复副本，不是长期版本管理或运行数据备份。

## 新增设计

[登录系统与用户隔离](../security/LOGIN_SYSTEM_DESIGN.md)根据[登录页设计图](../../image/FinSight金融研究登录页.png)编写，明确待开发状态、认证合同、每用户数据根、后台任务/SSE、前端账号切换、旧数据迁移及验收条件。

## 本次验证

检查 62 份 Markdown 中的 764 个实际本地链接及 112 组代码围栏，未发现失效目标或未闭合围栏。代码示例中的技能相对链接按示例自身上下文保留，不当作本文档导航重写。13 个实验快照文件与整理前摘要一致，快照清单中的 12 个文件摘要也全部匹配；4 个根目录 JSON 的路径和内容不变。

本次没有修改应用代码，也没有把登录方案中的未来验收用例记为已通过；未为文档移动重复运行业务回归。
