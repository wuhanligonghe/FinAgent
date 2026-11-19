"""Small-corpus memory retrieval: BM25, cached embeddings, and rank fusion."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from hashlib import sha256
import json
import math
import re
from time import monotonic

import httpx

from ..errors import AgentError


def tokens(text):
    """Keep exact Latin identifiers and overlapping Chinese bigrams."""
    result = []
    for word in re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
            if len(word) == 1:
                result.append(word)
        else:
            result.append(word)
    return result


def memory_text(record):
    return record["description"] + "\n" + record["body"]


def keyword_search(request, records):
    query = set(tokens(request))
    documents = [Counter(tokens(memory_text(r))) for r in records]
    if not query or not documents:
        return []
    lengths = [sum(d.values()) for d in documents]
    average = sum(lengths) / len(lengths) or 1
    frequencies = {term: sum(term in d for d in documents) for term in query}
    ranked = []
    for record, document, length in zip(records, documents, lengths):
        score = 0.0
        for term in query:
            tf = document[term]
            if tf:
                idf = math.log(1 + (len(documents) - frequencies[term] + .5) / (frequencies[term] + .5))
                score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * length / average))
        if score > 0:
            ranked.append((record["id"], score))
    return sorted(ranked, key=lambda item: (-item[1], item[0]))


def normalize(vector):
    if (not isinstance(vector, list) or not vector
            or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in vector)):
        raise ValueError("invalid embedding vector")
    norm = math.sqrt(sum(v * v for v in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("zero embedding vector")
    return [v / norm for v in vector]


QWEN3_MEMORY_INSTRUCTION = (
    "Given a user request, retrieve relevant user preferences, project context, "
    "and reference memories that help answer the request."
)


class HTTPEmbedder:
    def __init__(self, settings):
        self.settings = settings
        self.format = settings.embedding_format
        if self.format == "auto":
            name = settings.embedding_model.rsplit("/", 1)[-1].lower()
            self.format = ("embeddinggemma" if name.startswith("embeddinggemma") else
                           "qwen3" if name.startswith("qwen3-embedding") else "plain")
        identity = [
            settings.embedding_backend, settings.embedding_endpoint.rstrip("/"),
            settings.embedding_model, self.format]
        if self.format == "qwen3":
            identity.append(QWEN3_MEMORY_INSTRUCTION)
        self.fingerprint = sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()

    def query_text(self, request):
        if self.format == "embeddinggemma":
            return "task: search result | query: " + request
        if self.format == "qwen3":
            return f"Instruct: {QWEN3_MEMORY_INSTRUCTION}\nQuery:{request}"
        return request

    def document_text(self, record):
        if self.format == "embeddinggemma":
            return "title: " + record["description"] + " | text: " + record["body"]
        return memory_text(record)

    def embed(self, texts, timeout):
        s = self.settings
        headers = {"Content-Type": "application/json"}
        if s.embedding_api_key.get_secret_value():
            headers["Authorization"] = "Bearer " + s.embedding_api_key.get_secret_value()
        body = {"model": s.embedding_model, "input": texts}
        path = "/api/embed" if s.embedding_backend == "ollama" else "/embeddings"
        try:
            # Keep local embeddings independent of the chat/data proxy settings.
            with httpx.Client(trust_env=False, follow_redirects=False) as client:
                response = client.post(s.embedding_endpoint.rstrip("/") + path,
                                       headers=headers, json=body, timeout=timeout)
                response.raise_for_status()
            data = response.json()
            if s.embedding_backend == "ollama":
                vectors = data["embeddings"]
            else:
                rows = sorted(data["data"], key=lambda row: row["index"])
                if [row["index"] for row in rows] != list(range(len(texts))):
                    raise ValueError("invalid embedding indices")
                vectors = [row["embedding"] for row in rows]
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise ValueError("invalid embedding count")
            vectors = [normalize(v) for v in vectors]
            if len({len(v) for v in vectors}) != 1:
                raise ValueError("mixed embedding dimensions")
            return vectors
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # Never include endpoint responses, input text, or credentials.
            raise AgentError("MEMORY_EMBEDDING_UNAVAILABLE", "记忆向量服务不可用，使用关键词检索", True) from exc


class HTTPReranker:
    """Score only the already scoped shortlist through a dedicated rerank API."""
    def __init__(self, settings):
        self.settings = settings

    def rank(self, request, records, timeout):
        if not records:
            return []
        s = self.settings
        headers = {"Content-Type": "application/json"}
        if s.rerank_api_key.get_secret_value():
            headers["Authorization"] = "Bearer " + s.rerank_api_key.get_secret_value()
        body = {"model": s.rerank_model, "query": request[:4000],
                "documents": [memory_text(r) for r in records], "top_n": len(records)}
        try:
            with httpx.Client(trust_env=False, follow_redirects=False) as client:
                response = client.post(s.rerank_endpoint.rstrip("/") + "/rerank",
                    headers=headers, json=body, timeout=timeout)
                response.raise_for_status()
            data = response.json()
            if data.get("model", s.rerank_model) != s.rerank_model:
                raise ValueError("wrong reranker model")
            rows = data["results"]
            if not isinstance(rows, list) or len(rows) != len(records):
                raise ValueError("incomplete rerank response")
            scores, seen = [], set()
            for row in rows:
                index, score = row["index"], row["relevance_score"]
                if (type(index) is not int or not 0 <= index < len(records) or index in seen
                        or isinstance(score, bool) or not isinstance(score, (int, float))
                        or not math.isfinite(score) or not 0 <= score <= 1):
                    raise ValueError("invalid rerank score or index")
                seen.add(index)
                if score >= s.rerank_min_score:
                    scores.append((records[index]["id"], score))
            return [key for key, _ in sorted(scores, key=lambda item: (-item[1], item[0]))]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise AgentError("MEMORY_RERANK_UNAVAILABLE", "记忆重排服务不可用，保留混合召回顺序", True) from exc


class VectorCache:
    def __init__(self, root, user_id, *, database=None, scope=None):
        self.root, self.user_id = root, user_id
        self._database, self._scope = database, scope

    def backend(self):
        from ..storage.database import get_database
        from ..storage.scope import resolve_scope
        if self._database is None:
            self._database = get_database()
        if self._scope is None:
            self._scope = resolve_scope(self._database, self.root)
        return self._database, self._scope

    def remove(self, ids):
        if not ids:
            return
        from ..storage.schema import memory_vectors as t
        try:
            database, scope = self.backend()
            with database.transaction(scope, lock=True) as db:
                db.remove(t.name, memory_user_id=self.user_id, where=[t.c.memory_id.in_(ids)])
            self.last_error = None
        except AgentError as exc:
            # The source JSON/tombstone remains authoritative. A stale cached
            # vector cannot be read without a live, scoped source record.
            if not exc.code.startswith("DATABASE_"):
                raise
            self.last_error = exc.code

    def search(self, request, records, embedder, timeout, min_score):
        deadline = monotonic() + timeout
        def embed(texts):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AgentError("MEMORY_EMBEDDING_UNAVAILABLE", "记忆向量检索超时", True)
            vectors = embedder.embed(texts, timeout=remaining)
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise ValueError("invalid embedding count")
            return [normalize(v) for v in vectors]
        query_text = embedder.query_text(request[:4000]) if hasattr(embedder, "query_text") else request[:4000]
        query = embed([query_text])[0]
        fingerprint = embedder.fingerprint
        fingerprint_hash = sha256(fingerprint.encode()).hexdigest()
        database, scope = self.backend()
        vectors, missing = {}, []
        with database.transaction(scope) as db:
            for record in records:
                value = embedder.document_text(record) if hasattr(embedder, "document_text") else memory_text(record)
                digest = sha256(value.encode()).hexdigest()
                row = db.one("memory_vectors", memory_user_id=self.user_id, memory_id=record["id"], fingerprint_hash=fingerprint_hash, text_hash=digest)
                try:
                    vector = normalize(json.loads(row["vector"])) if row and row["fingerprint"] == fingerprint else None
                except (ValueError, TypeError):
                    vector = None
                if vector is not None and len(vector) == len(query):
                    vectors[record["id"]] = vector
                else:
                    missing.append((record, digest, value))
        # Embedding requests run after the read transaction releases its connection.
        for start in range(0, len(missing), 32):
            batch = missing[start:start + 32]
            embedded = embed([value for _, _, value in batch])
            if any(len(v) != len(query) for v in embedded):
                raise ValueError("embedding dimensions changed")
            with database.transaction(scope, lock=True) as db:
                for (record, digest, _), vector in zip(batch, embedded):
                    vectors[record["id"]] = vector
                    db.upsert("memory_vectors", dict(memory_user_id=self.user_id, memory_id=record["id"],
                        fingerprint_hash=fingerprint_hash, fingerprint=fingerprint, text_hash=digest, dimensions=len(vector), vector=json.dumps(vector)),
                        ["fingerprint", "text_hash", "dimensions", "vector"])
        scores = [(r["id"], sum(a * b for a, b in zip(query, vectors[r["id"]]))) for r in records]
        return sorted((item for item in scores if item[1] >= min_score), key=lambda item: (-item[1], item[0]))


def fuse(keyword, semantic, limit):
    """Reciprocal rank fusion avoids comparing BM25 and cosine score scales."""
    scores = Counter()
    for channel in (keyword, semantic):
        for rank, (key, _) in enumerate(channel[:limit], 1):
            scores[key] += 1 / (60 + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))[:limit]
