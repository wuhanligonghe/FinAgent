---
name: financial-growth
description: 使用可比在线三表字段计算同比或绝对变化。用于收入、利润等同指标跨期比较；不用于市场价格涨跌计算。
---

# 财务增长比较

先确认公司、指标、合并范围和期间，以截止前可用的在线三表为准。用 get_financial_statements 查询可比期，选择返回的 fact_id，必要时用 read_evidence 核对接口字段；不能将累计数与单季数混比，不能重复处理宿主已解析的相对年份。

使用授予的 generate_program/execute_program，选择目的 growth 或 absolute_change。fact_ids 按本期、上期顺序传入，程序绑定接口字段，不填模型估算值。同比公式为 (current-prior)/prior*100；上期非正时普通增长率不成立，应比较绝对变化并说明盈亏状态。

发现报告期、单位或合并范围差异时，按需读取 [可比性说明](references/comparability.md)。只有输入可比且执行成功后才解释变化，引用计算记录及接口证据；缺资料则保留缺口，不从百分比反算未披露金额。
