import re

from ..presentation import failure_answer


def failure_diagnostic(reason, retryable):
    code = reason.partition(":")[0]
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", code):
        code = "EXECUTION_FAILED"
    transport = re.search(r"\b(ConnectError|ConnectTimeout|ReadTimeout|WriteTimeout|PoolTimeout|ProxyError|RemoteProtocolError)\b", reason)
    http_status = re.search(r"\bHTTP ([1-5][0-9]{2})\b", reason)
    safe_reason = transport[1] if transport else f"HTTP {http_status[1]}" if http_status else ""
    return {"code": code, "transport_error": transport[1] if transport else None,
        "http_status": int(http_status[1]) if http_status else None, "retryable": bool(retryable),
        "hint": failure_answer(code, reason=safe_reason)}
