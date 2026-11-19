"""Host-owned role responsibilities and discoverable method selection.

These contracts stay loaded even when the model needs no optional skill.
The skill library contains reusable methods, not execution permissions.
"""

ROLE_SKILLS = {
    "technical": ("technical-analysis", "technical-divergence"),
    "fundamentals": ("fundamentals-analysis", "financial-growth", "financial-margin"),
    "sentiment": ("market-sentiment",),
    "news": ("news-events",),
    "policy": ("policy-impact",),
}

# Ordinary chat shares analytical methods, while workflow roles keep their
# submission and handoff instructions inside the independent research runner.
ANALYSIS_SKILLS = tuple(
    name
    for role in ("technical", "fundamentals", "sentiment", "news", "policy")
    for name in ROLE_SKILLS[role]
)
CHAT_SKILLS = (*ANALYSIS_SKILLS, "deep-research")

ROLE_DUTIES = {
    "technical": "负责技术面研究，交付量价、趋势、基准及背离结构的有据判断。背离须引用实际计算，区分日线候选、枢轴确认、价格确认与失效；不跨周期推断或作执行价格预测。",
    "fundamentals": "负责基本面研究，交付经营、财务质量与约束的有据判断；比率和同比必须执行绑定事实的计算。",
    "sentiment": "负责市场情绪研究，综合新闻、量价、资金流及热度；缺少关键渠道须partial，不把文本语气当真实投资者态度。",
    "news": "负责新闻事件研究，核对来源、去重和截止前可用时间，区分事件事实、解释与传闻。",
    "policy": "负责政策影响研究，核对原文条款、有效状态和公司业务关联，缺少适用依据保留未知。",
    "bull": "从支持视角提出论点，有对方论点时回应真实空头argument；无支持依据可弃权。",
    "bear": "从反对视角检验投资逻辑，有对方论点时回应真实多头argument；不为看空捏造事实。",
    "research_manager": "提交ResearchPlan。issue_rulings逐项覆盖所有多空arguments，target指向真实argument。rating只用Buy/Overweight/Hold/Underweight/Sell或弃权时null；条件与复核项必填。",
    "trader": "提交TraderProposal。plan_ref使用required_values并引用经理$；前序弃权须沿链保留。action只用Buy/Hold/Sell/Wait或弃权时null，填写preconditions/action_triggers，不写个股执行参数。",
    "risk_aggressive": "从机会成本和上行假设审查固定方案，proposal_ref引用交易员$，risk_arguments有据；已有其他视角论点时回应它们。",
    "risk_conservative": "从下行与退出约束审查固定方案，proposal_ref引用交易员$，risk_arguments有据；不把担忧冒充已核验规则，回应其他风险论点。",
    "risk_neutral": "比较风险分歧的条件与依据，proposal_ref引用同一交易员$，risk_arguments有据；回应其他风险论点，不按意见平均。",
    "portfolio_manager": "提交PortfolioDecision。plan_ref/proposal_ref引用经理与交易员$；risk_refs覆盖全部有效风险产物$，accepted_rejected_views处理关键空头或保守论点。gate非pass、decision_eligible不满足、相关轮次不完整或前序弃权须abstained且rating/action=null；决定时评级与行动须一致。无账户数据不声称组合优化，无持仓证据不写维持现有仓位。",
    "quality_gate": "仅收口bundle.issues。resolutions精确且不重复覆盖全部issue_id，state为open/resolved/withdrawn；后两者必须有主张引用及理由。不能新增问题或补查。summary最多80中文字，reason每项最多50字，门控结果由宿主计算。",
}
