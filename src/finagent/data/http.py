from __future__ import annotations

from dataclasses import dataclass
import json
from time import monotonic
from urllib.parse import urlparse

import httpx

from ..errors import AgentError
from ..runtime import ExecutionContext, now
from .endpoints import ALLOWED_HOSTS
from .evidence import EvidenceStore

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}


@dataclass
class RawResponse:
    content: bytes
    source: str
    url: str
    fetched_at: str
    snapshot_id: str
    encoding: str

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, errors="strict")

    def json(self):
        return json.loads(self.content)


class SourceHTTP:
    def __init__(self, store: EvidenceStore, timeout=12.0, retries=1, client: httpx.Client | None = None, *, proxy_url=None):
        self.store, self.timeout, self.retries = store, timeout, retries
        self.client = client or httpx.Client(follow_redirects=False, proxy=proxy_url)
        self.request_log: list[dict] = []

    def close(self):
        self.client.close()

    def get(self, source: str, url: str, ctx: ExecutionContext, *, params=None, encoding="utf-8", headers=None) -> RawResponse:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password:
            raise AgentError("SOURCE_DENIED", "数据请求只能访问登记的 HTTPS 来源")
        for attempt in range(self.retries + 1):
            operation = ctx.source_request()
            started = monotonic()
            record = {"source": source, "url": url, "attempt": attempt + 1}
            self.request_log.append(record)
            ctx.source_events.append(record)
            try:
                with self.client.stream("GET", url, params=params, headers={**HEADERS, **(headers or {})},
                                        timeout=ctx.request_timeout(self.timeout)) as response:
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        ctx.check()
                        size += len(chunk)
                        if size > 4 * 1024 * 1024:
                            raise AgentError("RESPONSE_TOO_LARGE", "来源响应超过 4 MiB 上限")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    fetched = now().isoformat()
                    meta = {"source": source, "url": str(response.request.url), "fetched_at": fetched,
                            "encoding": encoding, "http_status": response.status_code}
                    key = self.store.save_raw(content, meta)
                    ctx.evidence_ids.add(key)
                    record.update(snapshot_id=key, status=response.status_code, fetched_at=fetched)
                    if response.status_code != 200:
                        raise AgentError("HTTP_ERROR", f"{source} 返回 HTTP {response.status_code}",
                                         response.status_code in (408, 429, 500, 502, 503, 504))
                    ctx.check()
                    return RawResponse(content, source, str(response.request.url), fetched, key, encoding)
            except httpx.RequestError as exc:
                hint = "；请检查数据源网络或 FINAGENT_DATA_PROXY 配置" if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)) else ""
                error = AgentError("SOURCE_UNAVAILABLE", f"{source} 请求失败（{type(exc).__name__}）{hint}", True)
            except AgentError as exc:
                error = exc
            finally:
                record["elapsed_seconds"] = round(monotonic() - started, 3)
                if operation and ctx.research_budget is not None:
                    ctx.research_budget.settle(operation, state="finished" if record.get("status") == 200 else "failed")
            record["error"] = error.code
            if not error.retryable or attempt == self.retries:
                raise error
            ctx.cancelled.wait(min(0.25 * (attempt + 1), ctx.check()))
            ctx.check()
        raise AssertionError("unreachable")
