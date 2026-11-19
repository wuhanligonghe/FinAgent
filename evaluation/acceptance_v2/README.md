# 当前能力候选验收集 v2

140组、171轮：校准32组41轮，候选验收108组130轮。当前生产工具为十类，不含退休的 `documents`。状态为 `pending_independent_review`，开发者编写且使用过旧版失败案例，不能称为独立标注、未见测试或已验收。

相对[v1](../acceptance_v1/README.md)：保留原120个ID；旧资料库的6条输入改成在线三大报表查询，删除 `documents` 必要/可选标签；“暂停分析”仅需公共任务控制工具，不再要求新的财务查询；新增20组25轮边界对照与多轮范围修改。原版输入、金标、冻结包及85.59%的历史成绩均保留，v2与v1不能直接当作同口径准确率对比。[lineage.json](lineage.json)记录父文件SHA256与逐组改动，重建脚本不会写入v1。

模型只读 `inputs.jsonl` 原话与历史，不读 `gold.jsonl`。路由模式使用用户历史，不虚构主模型答复或金标任务状态；全链路模式从实际宿主状态投影活动任务类型、领域和状态。金融接口使用冻结合成HTTP，v2不再向旧财报库导入合成原文。研究执行仍为stub，不评估报告正文质量。

金标增加 `route_actions`，允许明确列出的歧义备选动作。`action_accuracy`统计动作匹配；`action_and_groups_match`同时要求动作与工具集合正确。降级或缺失均失败，保留计划轮数作分母。十类别的集合匹配、Precision/Recall/F1与原阈值继续统计；动作指标暂无独立批准的验收阈值。

```bash
.venv/bin/python scripts/evaluate_acceptance_suite.py validate
.venv/bin/python scripts/evaluate_acceptance_suite.py prepare --output .finagent/acceptance_v2/frozen
.venv/bin/python scripts/evaluate_acceptance_suite.py route --bundle .finagent/acceptance_v2/frozen --output .finagent/acceptance_v2/route_calibration --max-model-calls 82
# 未独立审核的验收部分只能以candidate运行
.venv/bin/python scripts/evaluate_acceptance_suite.py route --bundle .finagent/acceptance_v2/frozen --output .finagent/acceptance_v2/route_candidate --split acceptance --candidate --max-model-calls 260
```

冻结时一并保存源码、配置摘要、数据、迁移记录和空白审核表，禁止覆盖旧运行。每次实际分类请求（含纠正）分别计预算和日志；输入日志中没有金标。只需显式指定 `--source evaluation/acceptance_v1` 即可继续冻结v1源数据；复现历史成绩必须使用其原冻结源码。

生成脚本：`scripts/build_acceptance_v2.py`。非空目录必须显式 `--overwrite-candidate`；该选项只用于重建开发候选，不能用于修改已冻结证据。人工审核规则见[ANNOTATION_GUIDE.md](ANNOTATION_GUIDE.md)。
