---
name: technical-divergence
description: 计算和解释日线 MACD、RSI 顶底背离，区分候选、枢轴确认和失效。用于背离、日线底部或顶部动量结构研究。
---

# 日线背离结构

使用已确认任务窗口的 get_market_data 日线结果，调用 calculate_technical_divergence(result_id)。只有 get_daily_bars 时先取得工具支持的市场序列，不能传模型自填指标。默认复用已有窗口，不扩大冻结范围。

工具同时计算 MACD 的 DIF/DEA/柱线及 RSI，比较同一价格枢轴日期的指标值。先检查 outcome、missing 和 data_finality，再读 signals/comparisons；参数、公式、预热及枢轴规则见按需参考 [结构判定口径](references/structure-rules.md)。

- 底背离：价格低点降低，指标低点方向抬高；顶背离：价格高点抬高，指标高点方向降低。仅使用工具 matched_indicators，不把 MACD、RSI 同源信号作为独立来源重复计权。
- status=candidate 表示右侧枢轴尚未确认或供应商最终状态未知；confirmed 仅表示本口径的背离结构可确认，仍需分别说明 price_confirmation 是否出现。invalidated 表示后续已知价格越过该结构锚点，不能当成当前有效背离。
- 价格/指标两处锚点日期、数值、pivot_confirmation_date、周期及失效条件须可追溯到计算。不能在第二低点/高点当天就声称已知道未来右侧确认；不能凭日线推断周线、分钟或多周期共振。
- outcome=not_detected 仅表示当前算法与窗口未检出，不等于排除全部背离；unavailable 表示无从判断，不能写成无背离或中性。

在现有报告长度与主张上限内优先交付：周期与方向、结构状态、关键锚点依据、价格确认和失效观察。通过当前任务提供的合同或回复方式交付，calculation_refs 引用实际计算 result_id。背离不等于反转，更不自动产生买卖建议、目标价或胜率；未实现的隐性背离、缠论笔/中枢及柱面积不能补写成已计算。
