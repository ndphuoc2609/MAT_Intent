"""Bounded, ordered context serialization shared by service and eval runner."""
from __future__ import annotations

import json

CONTEXT_TOKEN_LIMIT = 12_000
CONTEXT_CHAR_LIMIT = CONTEXT_TOKEN_LIMIT * 3


def _over_budget(serialized):
    # shortcut: UTF-8 bytes/3 estimates tokens; use a provider tokenizer when one is available.
    return len(serialized.encode("utf-8")) > CONTEXT_TOKEN_LIMIT * 3


def fmt_context(ctx, *, prior_customer_turns=None):
    """Drop old low-priority messages whole and keep required context structured."""
    def model_safe(value):
        if isinstance(value, dict):
            return {key: model_safe(item) for key, item in value.items()
                    if key not in {"state_token", "source_state_token"}}
        if isinstance(value, list):
            return [model_safe(item) for item in value]
        return value

    recent = list(ctx.get("recent") or [])
    bot = list(ctx.get("recent_bot_replies") or [])
    prior = list(prior_customer_turns or [])
    scope = ctx.get("scope")
    actor = scope[3] if isinstance(scope, (list, tuple)) and len(scope) > 3 else None
    if not isinstance(actor, str) or not actor:
        current = next((m for m in ctx.get("messages", []) if isinstance(m, dict) and
                        m.get("role") == "current" and isinstance(m.get("actor_id"), str) and
                        m["actor_id"]), None)
        actor = current.get("actor_id") if current else None
    recent_texts = {m.get("text") for m in recent if isinstance(m, dict) and
                    isinstance(m.get("text"), str) and
                    m.get("role") == "recent" and actor is not None and m.get("actor_id") == actor}
    prior = [text for text in prior if not isinstance(text, str) or text not in recent_texts]
    messages = list(ctx.get("messages") or [])
    if not any(m.get("role") in ("root", "parent", "root_parent")
               for m in messages if isinstance(m, dict)):
        messages += [{"role": "parent", "text": text} for text in ctx.get("parent_texts", [])
                     if isinstance(text, str)]
    payload = model_safe({
        "format": "mat_intent_context_v1", "postcard": ctx.get("postcard") or {},
        "messages": messages, "recent": recent,
        "state_sources": [{key: value for key, value in source.items() if key != "request_id"}
                          for source in ctx.get("state_sources") or []],
        "active_requests": ctx.get("active_requests") or [], "pending": ctx.get("pending") or [],
        "contact_blocks": ctx.get("contact_blocks") or [], "prior_customer_turns": prior,
        "recent_bot_replies": [{"text": x, "verified_fact": False} for x in bot],
    })
    recent = payload["recent"]
    prior = payload["prior_customer_turns"]
    encode = lambda: json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while _over_budget(encode()) and recent:
        recent.pop()
    while _over_budget(encode()) and prior:
        prior.pop(0)
    while _over_budget(encode()) and bot:
        bot.pop(0)
        payload["recent_bot_replies"] = [{"text": x, "verified_fact": False} for x in bot]
    if _over_budget(encode()):
        raise ValueError("required post/root/parent/current/state context exceeds budget")
    return encode()
