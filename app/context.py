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
