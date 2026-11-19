# 日线背离判定口径 daily-divergence-v1

## 指标与数据

- 仅接收当前任务 get_market_data 的原始证据绑定日线；使用同一币种、单位和未复权价格。需要连续交易日且有成交，已知公司行动窗口暂停比较。未知公司行动不会被自动修复。
- MACD：DIF=EMA12(close)-EMA26(close)，DEA=EMA9(DIF)，柱线=2×(DIF-DEA)。EMA 用首个收盘价初始化，DEA 初始为零；柱线的两倍缩放与部分平台不同，符号和相对变化不变。
- RSI14：以前14次涨跌的简单均值初始化，之后采用 Wilder 平滑 (前均值×13+当期变化)/14；连续不涨不跌约定为50，没有下跌且有上涨为100。
- 前60条为预热要求，从索引59开始才比较锚点；最少样本还需覆盖枢轴间隔及右侧确认。初始数据窗口变化可能改变 EMA/RSI 数值。

## 枢轴与配对

默认价格 low/high 必须严格低于/高于左侧与右侧各两条日线才算确认枢轴，平台式相同高低点不纳入。本工具采用价格枢轴日对应指标值，不单独寻找另一个日期的指标极值。

同类枢轴相隔默认至少5条、最多60条日线。分别比较最近已确认枢轴与间隔内最近合格前枢轴；窗口末端右侧不足的极值另作候选。不为找背离跳过最近合格枢轴任意挑更早点。

底背离要求第二价格低点更低，同时对应指标值更高；顶背离反向。DIF/柱线底背离要求两值均小于零，顶背离要求两值均大于零；RSI 不强制超买超卖阈值。指标差异需大于数值容差0.00000001，该容差不是有效性评分。

## 时点、确认与失效

枢轴确认最早发生在第二枢轴右侧交易日齐备之时，而非第二枢轴当天。工具只读取目标截止日前的数据；candidate 与 confirmed 还受数据最终状态约束，供应商最终状态未知时不确认结构。

价格确认单独观察：底背离后收盘越过两低点之间的最高 high，顶背离后收盘跌破两高点之间的最低 low。它不等于保证反转，历史参考水平也不是建议执行价格。价格确认日期与枢轴确认日期可能不同，应分别报告。

第二低点后又出现更低 low，或第二高点后又出现更高 high，该组信号标记 invalidated。新的候选结构另行比较，不能将旧结构保留为持续有效。

上述是本项目可复现的筛查口径，没有统一的背离划分标准；未检出只适用于本口径和当前窗口。隐性背离、跨波段面积、缠论结构和跨周期共振不在本版计算范围。

## 方法参考

- [Fidelity MACD](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/macd)：指标公式及与价格背离的解释。
- [Fidelity RSI](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/RSI)：价格高低点与 RSI 不确认的背离。
- [StockCharts MACD-Histogram](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/macd-histogram)：柱线与 DIF、信号线的关系。本项目明确采用两倍柱线缩放。
