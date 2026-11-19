# 业务实验公开快照

这是一份可随仓库展示的精简快照，不包含鉴权头、完整模型上下文、私有运行数据库或模型内部推理字段。来源资料是虚构合成财报。

- [报告](REPORT.md)与[机器可读指标](report.json)、[CSV](metrics.csv)：开发集3组对话、三种方案、34次模型调用；本小样本各3/3成功，不能外推为真实业务准确率100%。
- [候选简历表述](RESUME.md)：当前只生成工程能力表述，不生成独立效果提升声明。
- [推理前清单](inference_manifest.json)与[原冻结数据v1](inference_dataset_v1.json)：保留首次试跑版本，不追改为后来的v1.1。
- [离线重新评分记录](analysis_manifest.json)：明确posthoc分析与源码版本。首次试跑未保存完整推理时源码快照，不声称逐字复现原环境。
- [合成原始表格](synthetic_sources.json)与[单例执行轨迹](sample_traces.json)：可以核查回答中的doc_ID和表格定位。
- [开发检索对照](retrieval_development.json)：扩展词与开发查询来自同一开发阶段，不能视为独立泛化提升。
- [文件摘要](FILES.json)：导出文件的完整性校验。
- [验证记录](validation.json)：540项回归通过、wheel检查和当前留出审核状态。

当前实验框架已补充源码与逐调用输入快照，留出集18组尚待运行前人工标签审核，没有查看留出检索成绩。运行方法、评分范围和版本关系分别见 [实验说明](../../BUSINESS_EXPERIMENTS.md)、[首次结果](../../BUSINESS_EXPERIMENT_RESULTS.md)、[修订记录](../../BUSINESS_EXPERIMENT_REVISIONS.md)。
