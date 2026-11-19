"""Read-only, scoped review catalog with exact projections and provenance aliases."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

from ..errors import AgentError


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def pointer_part(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def resolve_pointer(value, path):
    if path == "":
        return value
    if not path.startswith("/") or len(path) > 1000:
        raise AgentError("REVIEW_PATH_INVALID", "使用JSON Pointer字段路径，不接受文件路径或URL")
    try:
        for token in path[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not token.isdecimal():
                    raise KeyError(token)
                value = value[int(token)]
            elif isinstance(value, dict):
                value = value[token]
            else:
                raise KeyError(token)
        return value
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AgentError("REVIEW_PATH_INVALID", "证据中不存在该JSON Pointer字段") from exc


def _stable_metadata(value):
    if isinstance(value, dict):
        return {k: _stable_metadata(v) for k, v in value.items()
                if k not in {"snapshot_id", "fetched_at", "retrieved_at", "request_id", "elapsed_ms"}}
    if isinstance(value, list):
        return [_stable_metadata(v) for v in value]
    return value


class ReviewEvidenceCatalog:
    def __init__(self, packet):
        self.entries, self.aliases, self.observations, self.fingerprints = {}, {}, {}, {}
        fingerprints = {}
        for original in packet["evidence"]:
            entry = deepcopy(original)
            key = entry["evidence_id"]
            if key in self.aliases:
                raise AgentError("ANSWER_REVIEW_INPUT", "证据目录存在重复ID")
            semantics = {k: v for k, v in entry.items()
                         if k not in {"evidence_id", "provenance", "fetched_at", "cache_hit", "snapshots"}}
            semantics["source_metadata"] = _stable_metadata(entry.get("snapshots", []))
            digest = sha256(encode(semantics).encode()).hexdigest()
            canonical = fingerprints.setdefault(digest, key)
            self.fingerprints[canonical] = digest
            self.aliases[key] = canonical
            observation = {k: v for k, v in entry.items() if k != "body"}
            self.observations.setdefault(canonical, []).append(observation)
            self.entries.setdefault(canonical, entry)
        for key, entry in self.entries.items():
            entry["aliases"] = [alias for alias, canonical in self.aliases.items() if canonical == key]

    def get(self, evidence_id):
        canonical = self.aliases.get(evidence_id)
        if canonical is None:
            raise AgentError("REVIEW_EVIDENCE_DENIED", "仅允许读取本次审核目录内的证据ID")
        return self.entries[canonical]

    def manifest(self):
        result = []
        for key, entry in self.entries.items():
            body = entry["body"]
            related = [other for other, value in self.entries.items()
                       if other != key and value.get("tool") == entry.get("tool")
                       and value.get("symbol") == entry.get("symbol")]
            result.append({k: deepcopy(entry.get(k)) for k in (
                "evidence_id", "aliases", "tool", "symbol", "source", "source_url", "data_time",
                "currency", "units", "status", "warnings", "representation")} | {
                    "body_fields": list(body) if isinstance(body, dict) else [],
                    "collections": {"/body/" + pointer_part(k): len(v) for k, v in body.items()
                                    if isinstance(v, list)} if isinstance(body, dict) else {},
                    "body_characters": len(encode(body)), "related_evidence_ids": related,
                    "observations": [{"evidence_id": o["evidence_id"], "fetched_at": o.get("fetched_at"),
                                      "snapshot_ids": [s.get("snapshot_id") for s in o.get("snapshots", [])]}
                                     for o in self.observations[key]],
                    "read_path": "/body", "full_body_loaded": False})
        return result

    def initial_view(self, limit, max_entries=None):
        """Copy exact small fields, exposing every skipped collection as a pointer."""
        views, remaining = [], limit
        allowance = max(300, min(2400, limit // max(1, len(self.entries))))
        ordered_entries = sorted(self.entries.items(), key=lambda pair: (
            0 if pair[1].get("tool") == "get_quote" else
            1 if pair[1].get("tool", "").startswith("calculate_") or pair[1].get("tool") == "execute_program" else 2))
        if max_entries is not None:
            ordered_entries = ordered_entries[:max_entries]
        for key, entry in ordered_entries:
            fields = []
            used = 0
            full_item = {"path": "/body", "value": entry["body"]}
            if (entry.get("tool") == "get_quote" or entry.get("tool", "").startswith("calculate_")
                    or entry.get("tool") == "execute_program") and len(encode(full_item)) <= min(8000, remaining):
                views.append({"evidence_id": key, "fields": [deepcopy(full_item)], "full_body_loaded": True})
                remaining -= len(encode(full_item))
                continue

            def add(path, value):
                nonlocal used, remaining
                item = {"path": path, "value": value}
                size = len(encode(item))
                if size <= min(remaining, allowance - used):
                    fields.append(deepcopy(item))
                    used += size
                    remaining -= size

            body = entry["body"]
            if len(encode({"path": "/body", "value": body})) <= allowance:
                add("/body", body)
            elif isinstance(body, dict):
                # Metadata and calculated results precede bulky source arrays.
                ordered = sorted(body, key=lambda k: (
                    0 if not isinstance(body[k], (dict, list)) else
                    1 if k in {"metrics", "latest_indicators", "missing", "limitations", "scope",
                               "parameters", "formulas", "input_result_ids"} else 2))
                for name in ordered:
                    value = body[name]
                    path = "/body/" + pointer_part(name)
                    if isinstance(value, dict):
                        for field, item in value.items():
                            add(path + "/" + pointer_part(field), item)
                    elif isinstance(value, list):
                        if name == "bars":
                            for i in range(max(0, len(value) - 2), len(value)):
                                add(path + "/" + str(i), value[i])
                        elif name in {"reports", "facts"}:
                            for i, record in enumerate(value[:2]):
                                if isinstance(record, dict):
                                    for field, item in record.items():
                                        if field == "metrics" and isinstance(item, dict):
                                            for metric, amount in item.items():
                                                add(path + f"/{i}/metrics/" + pointer_part(metric), amount)
                                        else:
                                            add(path + f"/{i}/" + pointer_part(field), item)
                        else:
                            add(path, value)
                    else:
                        add(path, value)
            views.append({"evidence_id": key, "fields": fields,
                          "full_body_loaded": any(f["path"] == "/body" for f in fields)})
        return views

    def read(self, evidence_id, path="/body", offset=0, limit=6000):
        entry = self.get(evidence_id)
        # Alias metadata remains individually addressable after body deduplication.
        original = next(o for o in self.observations[entry["evidence_id"]]
                        if o["evidence_id"] == evidence_id)
        value = resolve_pointer({**original, "body": entry["body"]}, path)
        text = encode(value)
        offset = max(0, offset)
        end = min(len(text), offset + limit)
        result = {"evidence_id": entry["evidence_id"], "requested_id": evidence_id, "path": path,
                "offset": offset, "text": text[offset:end], "total_chars": len(text),
                "next_offset": end if end < len(text) else None,
                "complete_value": offset == 0 and end == len(text),
                "coverage_note": "这是所请求字段的指定字符片段；未展示部分不能推断为不存在。长内容可使用search_review_evidence定位。"}
        if offset == 0 and end < len(text):
            start = max(end, len(text) - 200)
            result["tail_preview"] = {"offset": start, "text": text[start:]}
        return result

    def contains_quote(self, evidence_id, quote):
        entry = self.get(evidence_id)
        observation = next(o for o in self.observations[entry["evidence_id"]] if o["evidence_id"] == evidence_id)
        source = {**observation, "body": entry["body"]}
        if quote in encode(source):
            return True
        stack = [source]
        while stack:
            value = stack.pop()
            if isinstance(value, str) and quote in value:
                return True
            if isinstance(value, dict):
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
        return False

    def search(self, evidence_id, query, path="/body", offset=0, max_hits=5):
        if not query.strip() or len(query) > 200:
            raise AgentError("REVIEW_QUERY_INVALID", "搜索词须为1—200个字符，按字面匹配")
        entry = self.get(evidence_id)
        observation = next(o for o in self.observations[entry["evidence_id"]] if o["evidence_id"] == evidence_id)
        text = encode(resolve_pointer({**observation, "body": entry["body"]}, path))
        query = query.strip()
        terms = list(dict.fromkeys(query.split())) or [query]
        anchor = query if query in text else terms[0]
        all_present = all(term in text for term in terms)
        hits, cursor = [], max(0, offset)
        while all_present and len(hits) < max_hits:
            index = text.find(anchor, cursor)
            if index < 0:
                break
            start, end = max(0, index - 100), min(len(text), index + len(anchor) + 100)
            hits.append({"offset": index, "snippet_offset": start, "text": text[start:end]})
            cursor = index + len(anchor)
        more = bool(hits) and text.find(anchor, cursor) >= 0
        return {"evidence_id": entry["evidence_id"], "path": path, "hits": hits,
                "query_terms": terms, "match_mode": "all_terms_in_field",
                "total_chars": len(text), "next_offset": cursor if more else None}
