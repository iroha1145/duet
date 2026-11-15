"""Authenticated, secret-free view of actual persisted provider-call counters."""
import re

from aiohttp import web

from duet_server.providers import clean_usage


def metrics(protocol, usage):
    if protocol == "anthropic":
        return {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_write_tokens": usage.get("cache_creation_input_tokens")}
    details = usage.get("prompt_tokens_details", {})
    return {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
            "cache_read_tokens": details.get("cached_tokens"), "cache_write_tokens": details.get("cache_write_tokens")}


def usage_snapshot(engine, limit=50):
    rows = []
    for session in engine.store.sessions():
        for message in session["messages"]:
            request = message.get("provider_request")
            if message.get("role") != "assistant" or not isinstance(request, dict):
                continue
            protocol = request.get("protocol")
            started = request.get("started_at")
            if protocol not in ("openai", "anthropic") or type(started) not in (int, float):
                continue
            usage = clean_usage(protocol, message.get("usage"))
            status = message.get("status")
            if status not in ("streaming", "complete", "error", "stopped", "interrupted"):
                status = "unknown"
            row = {"session_id": session["id"], "message_id": message["id"], "started_at": started,
                   "protocol": protocol, "model": request.get("model", ""), "profile_id": request.get("profile_id"),
                   "status": status, "usage_reported": bool(usage), "usage": usage or None,
                   "partial_usage": bool(usage) and status != "complete", "metrics": metrics(protocol, usage),
                   "prompt_cache_enabled": request.get("prompt_cache_enabled"),
                   "stream_usage_enabled": request.get("stream_usage_enabled"),
                   "cache_breakpoints": request.get("cache_breakpoints"), "cache_key": None}
            key = request.get("cache_key")
            if isinstance(key, str) and re.fullmatch(r"duet_[0-9a-f]{56}", key):
                row["cache_key"] = key
            rows.append(row)
    rows.sort(key=lambda row: (row["started_at"], row["message_id"]), reverse=True)
    selected = rows[:limit]
    def total(field):
        values = [row["metrics"][field] for row in selected if row["metrics"][field] is not None]
        return sum(values) if values else None
    summary = {"requests": len(selected), "usage_reported": sum(row["usage_reported"] for row in selected),
               "usage_missing": sum(not row["usage_reported"] for row in selected),
               "partial_usage": sum(row["partial_usage"] for row in selected),
               "cache_read_reported": sum(row["metrics"]["cache_read_tokens"] is not None for row in selected),
               "cache_write_reported": sum(row["metrics"]["cache_write_tokens"] is not None for row in selected),
               "cache_read_tokens": total("cache_read_tokens"), "cache_write_tokens": total("cache_write_tokens"),
               "output_tokens": total("output_tokens")}
    return {"requests": selected, "summary": summary, "has_more": len(rows) > limit, "limit": limit}


def register_usage(app, engine=None):
    """Register behind the host's existing account/legacy authentication."""
    async def usage(request):
        if engine is None:
            return web.json_response({"error": {"code": "usage_unavailable"}}, status=503)
        try:
            if set(request.query) - {"limit"}:
                raise ValueError()
            limit = int(request.query.get("limit", "50"))
            if not 1 <= limit <= 200:
                raise ValueError()
        except ValueError:
            return web.json_response({"error": {"code": "invalid_usage_limit"}}, status=400)
        return web.json_response(usage_snapshot(engine, limit), headers={"Cache-Control": "no-store"})
    app.router.add_get("/duet/usage", usage)
    return engine
