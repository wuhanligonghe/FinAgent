# 动作与领域路由 v2 交付与验证

2026-11-05，完成前四项改造：更新当前十类别与v2候选标签；将小模型输出拆成动作和数据领域、由宿主映射工具；加入边界对照示例与活动任务的有限投影；对可修复的输出合同错误最多纠正一次，两次请求共用总超时并逐次扣模型调用预算。既有关键词兜底策略保留，工具发现与执行权限继续由宿主约束。

实现见[路由模块](../../src/finagent/intent_routing.py)、[宿主接入](../../src/finagent/harness/host.py)、[评估器](../../src/finagent/acceptance.py)。小模型不更新任务表示，不生成参数，不授予执行权限。research_start/research_followup由后端映射research，memory_write映射memory，automation映射任务相关工具；普通数据查询/分析由data_domains映射，公共任务表示工具始终可见。

[v2候选集](../../evaluation/acceptance_v2/README.md)共140组171轮，校准32组41轮、候选验收108组130轮，仍为pending_independent_review。旧资料库样例换成在线三表查询，新增20组25轮对照；动作金标可声明合理备选。v1数据、冻结包和101/118（85.59%）的原始分类成绩保留，没有重算或覆盖。两个版本的标签、输入和实现均不同，不构成同口径前后对比。

验证结果：

| 检查 | 结果与范围 |
| --- | --- |
| 完整离线回归 | 846项通过；临时Redis与合成HTTP/模型，89.115秒 |
| 最终改动后的相关回归 | 63项通过，含工具发现、动作合同、失败用量、预算与共享超时、v2冻结和旧库隔离 |
| 真实本地Qwen校准 | 41/41轮运行；工具集合38/41（92.68%），支持类别Macro-F1为95.19% |
| 校准动作指标 | 动作39/41（95.12%）；动作与类别同时正确37/41（90.24%） |
| 校准降级与重试 | 降级0/41；1条合同错误经纠正成功，共42次实际请求、输入27325/输出1420 token |
| 最终公开冒烟 | 17/19；是开发样例，不是独立准确率测试 |
| 冻结验证 | v1旧包与v2新包均通过文件摘要校验 |

最后一次公开冒烟仍有两处语义错误：舆情与资金流比较多选market；“只保留技术面”识别为task_control但漏掉market。校准集仍有待办事项错分、预测期限修改或恢复分析时漏保留领域，以及一次analyze/query动作边界不匹配。它们的输出符合结构合同，因此不会自动触发合同纠正；高置信度不能视为语义正确。保留原预测和待审核金标，没有修改金标让这些预测得分。

提示开发过程三次冒烟依次为17/19、18/19、17/19，全部保留；最终报告绑定最后一次源码行为，未挑选最高结果。实际小模型正确率、主模型完成率与独立验收仍不能由本次回归或公开样例推断。尚未运行v2候选验收108组130轮或真实主模型全链路；也未进行生产服务重启。

证据：

- [v2冻结清单](../../.finagent/acceptance_v2/frozen_20261105/manifest.json)，SHA256：`8496a3f1d9e0265eca219d075c5786de897a61640a27d58c9a028e92d55a4ba2`；含迁移记录与空白独立审核表。
- [校准报告](../../.finagent/acceptance_v2/route_calibration_20261105/report.json)、[逐轮预测](../../.finagent/acceptance_v2/route_calibration_20261105/predictions.jsonl)，requests目录逐次记录分类请求与纠正码，不传金标。
- [最终冒烟](../../.finagent/checks/intent_routing_v2_20261105_live_verified.json)；[首次](../../.finagent/checks/intent_routing_v2_20261105_live.json)、[中间版本](../../.finagent/checks/intent_routing_v2_20261105_live_final.json)。
- [完整回归日志](../../.finagent/checks/intent_routing_v2_20261105_regression.log)、[最终相关回归日志](../../.finagent/checks/intent_routing_v2_20261105_targeted.log)。

现有终端/Web进程需重启后才能加载新代码。评测只调用本机分类模型，未运行真实金融操作、研究任务或外部主模型评估。
