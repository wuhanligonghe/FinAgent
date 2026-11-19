"""Session-stable tool definitions and trusted, client-executed discovery."""
from copy import deepcopy
import re

from ..intent_routing import COMMON_TOOLS, GROUPS, GROUP_TOOLS
from .legacy_session import RETIRED_TOOLS


CORE_TOOLS = {"search_tools", "select_tool_groups", "ask_user"}


def historical_references(messages):
    names = set()
    for message in messages:
        references = list(message.get("_tool_references", []))
        for block in message.get("_anthropic_content", []):
            if block.get("type") == "tool_search_tool_result" and isinstance(block.get("content"), dict):
                references.extend(block["content"].get("tool_references", []))
        names.update(ref["tool_name"] for ref in references if isinstance(ref, dict)
                     and ref.get("type") == "tool_reference" and isinstance(ref.get("tool_name"), str))
    return names


def definitions(host):
    """Keep definitions (including loaded schemas) stable across turns/restarts.

    Retired definitions remain available to expand historical tool references;
    execution always checks the live registry and role permissions separately.
    """
    catalog = host.state.setdefault("_tool_catalog", {})
    tools = host.security.protect_tools(host.pool.schemas())
    for tool in tools:
        name = tool["function"]["name"]
        if host.tool_filter is None or name in host.tool_filter:
            catalog.setdefault(name, deepcopy(tool))
    referenced = historical_references(host.messages)
    permitted = [deepcopy(t) for name, t in catalog.items()
                 if name not in RETIRED_TOOLS
                 and (host.tool_filter is None or name in host.tool_filter or name in referenced)]
    # A narrow worker without a discovery tool must be able to call its tools.
    searchable = any(t["function"]["name"] in {"search_tools", "select_tool_groups"} for t in permitted)
    for tool in permitted:
        name = tool["function"]["name"]
        historical_only = name not in host.pool.tools or (host.tool_filter is not None and name not in host.tool_filter)
        tool["defer_loading"] = historical_only or (searchable and name not in CORE_TOOLS)
    return host.security.protect_tools(permitted)


def search(host, query, limit):
    query = query.strip().lower()
    terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", query))
    pairs = {query[i:i+2] for i in range(len(query)-1)
             if all("\u4e00" <= c <= "\u9fff" for c in query[i:i+2])}
    groups = {g for g, description in GROUPS.items() if g in terms or query in description.lower()}
    group_names = set().union(*(GROUP_TOOLS[g] for g in groups)) if groups else set()
    if "extensions" in groups:
        known = set().union(*GROUP_TOOLS.values(), COMMON_TOOLS, CORE_TOOLS)
        group_names.update(set(host.pool.tools) - known)
    ranked = []
    for tool in definitions(host):
        name = tool["function"]["name"]
        if name not in host._visible_tool_names() or name in CORE_TOOLS:
            continue
        description = tool["function"]["description"].lower()
        score = (100 if query == name.lower() else 0) + (60 if name in group_names else 0)
        score += 30 if query in name.lower() else 0
        score += 20 if query in description else 0
        score += sum(5 for term in terms if term in name.lower() or term in description)
        score += sum(1 for pair in pairs if pair in description)
        if score:
            ranked.append((-score, name))
    names = [name for _, name in sorted(ranked)[:limit]]
    return {"discovered_tools": names, "query": query,
            "tool_groups": GROUPS if not names else {},
            "message": "工具已在本次结果中加载，执行权限仍由宿主检查。" if names else "未找到工具；可改用工具名或类别名搜索。"}


def result_message(host, call, output, message):
    """Only host discovery results may create protocol-level references."""
    if call.name not in {"search_tools", "select_tool_groups"} or not isinstance(output, dict) or output.get("error"):
        return message
    deferred = {t["function"]["name"] for t in definitions(host) if t["defer_loading"]}
    live = host._visible_tool_names()
    names = list(dict.fromkeys(name for name in output.get("discovered_tools", [])
                               if isinstance(name, str) and name in deferred and name in live))
    if names:
        message["_tool_references"] = [{"type": "tool_reference", "tool_name": name} for name in names]
    return message
