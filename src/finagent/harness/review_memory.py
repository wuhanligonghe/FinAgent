"""Exact evidence retained once in the review prompt, independent of its audit log."""
from __future__ import annotations

import json

from .review_evidence import encode, resolve_pointer, pointer_part


class ReviewEvidenceMemory:
    def __init__(self):
        self.records = {}
        self.searches = {}

    def _ancestor(self, evidence_id, path):
        for (key, parent), record in self.records.items():
            if key == evidence_id and path.startswith(parent + "/") and self._complete(record):
                value = json.loads(record["parts"][0][1])
                return encode(resolve_pointer(value, path[len(parent):]))
        return None

    @staticmethod
    def _complete(record):
        parts = record["parts"]
        return len(parts) == 1 and parts[0][0] == 0 and len(parts[0][1]) == record["total_chars"]

    def add(self, evidence_id, path, offset, text, total_chars):
        ancestor = self._ancestor(evidence_id, path)
        if ancestor is not None:
            if ancestor[offset:offset + len(text)] != text:
                raise ValueError("Immutable evidence fragments disagree")
            return 0
        key = (evidence_id, path)
        record = self.records.setdefault(key, {"total_chars": total_chars, "parts": []})
        if record["total_chars"] != total_chars:
            raise ValueError("Immutable evidence length changed")
        before = sum(len(part) for _, part in record["parts"])
        merged = []
        for start, part in sorted([*record["parts"], (offset, text)]):
            if not part:
                continue
            if merged and start <= merged[-1][0] + len(merged[-1][1]):
                old_start, old_text = merged[-1]
                overlap = min(len(part), old_start + len(old_text) - start)
                if old_text[start - old_start:start - old_start + overlap] != part[:overlap]:
                    raise ValueError("Immutable evidence fragments disagree")
                merged[-1] = (old_start, old_text + part[overlap:])
            else:
                merged.append((start, part))
        record["parts"] = merged
        return sum(len(part) for _, part in merged) - before

    def add_value(self, evidence_id, path, value):
        text = encode(value)
        return self.add(evidence_id, path, 0, text, len(text))

    def complete(self, evidence_id, path):
        record = self.records.get((evidence_id, path))
        return bool(record and self._complete(record)) or self._ancestor(evidence_id, path) is not None

    def fragments(self, evidence_ids):
        return [text for (key, _), record in self.records.items() if key in evidence_ids
                for _, text in record["parts"]]

    def read_loaded(self, evidence_id, path, offset, length):
        """Return only a contiguous range already displayed to this reviewer."""
        record = self.records.get((evidence_id, path))
        ancestor = self._ancestor(evidence_id, path)
        if ancestor is not None:
            if offset >= len(ancestor):
                raise LookupError("Citation offset is outside the loaded value")
            return ancestor[offset:offset + length]
        if record is not None:
            end = min(record["total_chars"], offset + length)
            for start, text in record["parts"]:
                if start <= offset < end <= start + len(text):
                    return text[offset - start:end - start]
        raise LookupError("Citation range has not been loaded")

    def citation_paths(self, limit=128):
        """Bounded, source-paired paths actually present in the displayed state."""
        direct, descendants = {}, {}
        def visit(key, path, value):
            if not isinstance(value, (dict, list)) or len(encode(value)) <= 240:
                descendants.setdefault(key, []).append(path)
            if isinstance(value, dict):
                for name, child in value.items():
                    visit(key, path + "/" + pointer_part(name), child)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(key, path + "/" + str(index), child)
        for (key, path), record in self.records.items():
            if self._complete(record):
                value = json.loads(record["parts"][0][1])
                visit(key, path, value)
                if not isinstance(value, (dict, list)) or record["total_chars"] <= 240:
                    direct.setdefault(key, []).append(path)
            else:
                direct.setdefault(key, []).append(path)
        result = {}
        for key in dict.fromkeys([*direct, *descendants]):
            explicit = list(dict.fromkeys(reversed(direct.get(key, []))))[:limit]
            remaining = [p for p in dict.fromkeys(descendants.get(key, [])) if p not in explicit]
            available = limit - len(explicit)
            if len(remaining) > available:
                head = available // 2
                remaining = remaining[:head] + (remaining[-(available - head):] if available > head else [])
            result[key] = explicit + remaining
        return {key: paths for key, paths in result.items() if paths}

    def view(self):
        items = []
        for (key, path), record in self.records.items():
            if self._ancestor(key, path) is not None:
                continue
            complete = self._complete(record)
            item = {"evidence_id": key, "path": path, "complete_value": complete,
                    "total_chars": record["total_chars"]}
            if complete:
                item["value"] = json.loads(record["parts"][0][1])
            else:
                item["segments"] = [{"offset": start, "text": text} for start, text in record["parts"]]
                item["next_offset"] = (len(record["parts"][0][1])
                                       if record["parts"] and record["parts"][0][0] == 0 else 0)
            items.append(item)
        return items
