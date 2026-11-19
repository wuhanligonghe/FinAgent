"""Small host-side intent guard; explicit research requests have priority."""
import re

ALIASES = {"technical": ("技术面", "技术分析"), "sentiment": ("情绪", "舆情"), "news": ("新闻", "消息面"),
           "fundamentals": ("基本面", "财务分析"), "policy": ("政策",)}


def research_route(goal):
    if re.search(r"什么是|是什么意思|定义|如何使用|怎么用|[“\"].*(?:深入|深度|辩论).*?[”\"]", goal):
        return "direct", [], "概念或引用内容"
    if re.search(r"(?:不要|不用|不需要)(?:进行|做)?\s*(?:深度研究|深入研究|深入分析)", goal):
        return "direct", [], "用户否定深入研究"
    if re.search(r"(?:不要|不用|不需要).*辩论", goal) and not re.search(r"深入分析|深度研究|深入研究|五维|完整研究报告", goal):
        return "direct", [], "概念、否定或引用内容"
    roles = [role for role, names in ALIASES.items() if any(name in goal for name in names)]
    deep = bool(re.search(r"深入分析|深度研究|深入研究|五个维度|五维|完整研究报告|多空辩论|完整投资研究|完整.*风险讨论", goal))
    if deep:
        return "specialist", roles, "兼容入口也只做专项；缺维度须澄清，深度研究由菜单选择"
    if roles and re.search(r"分析|看看|研究|只看|如何|怎么样", goal):
        return "specialist", roles, "用户请求专项分析"
    return "direct", [], "普通问答或工具查询"


def pipeline_from_goal(goal):
    if re.search(r"(?:不要|不用|不需要|不做|无需).*(?:辩论|交易方案|决策)|只(?:要|做).*研究报告", goal):
        return "research"
    return "trading"
