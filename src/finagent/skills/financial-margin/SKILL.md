---
name: financial-margin
description: 计算和解释净利率、营业利润率及其跨期百分点变化。用于已明确利润口径且有可比收入的财务分析。
---

# 利润率与变化

先明确利润指全部净利润、归母净利润还是营业利润，对齐分子分母的期间、币种与合并范围。使用 get_financial_statements 返回的利润和营业收入 fact_ids，经 generate_program/execute_program 执行，不能在理由或摘要中自行心算。

目的 net_margin 用利润/收入，operating_margin 用营业利润/收入；对应利润率为 profit/revenue*100。margin_change 按“本期利润、本期收入、上期利润、上期收入”传入事实，结果是两期利润率之差，单位为百分点，不能标成相对增长百分比。

分母非正、期间不可比或缺少原始事实时停止该项计算，报告缺口。涉及扣非、毛利率或特殊行业口径时，按需读取 [适用边界](references/metric-boundaries.md)，不得把工具未核验的业务公式当作标准利润率结果。
