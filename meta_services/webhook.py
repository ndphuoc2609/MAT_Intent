"""Meta Page webhook verification and minimal comment-event extraction."""
from __future__ import annotations

import hashlib
import hmac
import re


_GRAPH_ID = re.compile(r"[0-9]+(?:_[0-9]+)*\Z")


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    if not isinstance(body, bytes) or not isinstance(header, str) or not app_secret:
        return False
    if not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", header):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def extract_comment_events(payload, page_id: str) -> list[dict]:
    """Return only new customer comments belonging to the configured Page."""
    if not isinstance(payload, dict) or payload.get("object") != "page":
        return []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return []
    events = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("id") != page_id:
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != "feed":
                continue
            value = change.get("value")
            if not isinstance(value, dict) or value.get("item") != "comment" or value.get("verb") != "add":
                continue
            comment_id, post_id = value.get("comment_id"), value.get("post_id")
            author = value.get("from")
            actor_id = author.get("id") if isinstance(author, dict) else value.get("sender_id")
            text = value.get("message")
            if (not isinstance(comment_id, str) or not _GRAPH_ID.fullmatch(comment_id)
                    or not isinstance(post_id, str) or not _GRAPH_ID.fullmatch(post_id)
                    or not isinstance(actor_id, str) or not _GRAPH_ID.fullmatch(actor_id)
                    or actor_id == page_id or not isinstance(text, str)
                    or not text.strip() or len(text) > 10000):
                continue
            parent_id = value.get("parent_id")
            if not isinstance(parent_id, str) or not _GRAPH_ID.fullmatch(parent_id):
                parent_id = post_id
            thread_root_id = value.get("thread_root_id") or value.get("root_comment_id")
            if not isinstance(thread_root_id, str) or not _GRAPH_ID.fullmatch(thread_root_id):
                thread_root_id = comment_id if parent_id == post_id else parent_id
            events.append({
                "page_id": page_id,
                "actor_id": actor_id,
                "post_id": post_id,
                "comment_id": comment_id,
                "parent_id": parent_id,
                "thread_root_id": thread_root_id,
                "text": text,
                "event_type": "create",
                "content_version": 1,
            })
    return events
