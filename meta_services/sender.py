"""At-most-once Page reply adapter for the Engine outbox."""
from __future__ import annotations

from datetime import datetime, timezone


class MetaDeliveryReceiver:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.conn.execute("""CREATE TABLE IF NOT EXISTS meta_delivery_attempts(
                dedupe_key TEXT PRIMARY KEY, status TEXT NOT NULL, remote_id TEXT,
                updated_at TEXT NOT NULL)""")
            store.conn.commit()

    def begin(self, key):
        now = datetime.now(timezone.utc).isoformat()
        with self.store.lock:
            cur = self.store.conn.execute(
                "INSERT OR IGNORE INTO meta_delivery_attempts VALUES (?,?,NULL,?)",
                (key, "ATTEMPTING", now))
            self.store.conn.commit()
            return cur.rowcount == 1

    def finish(self, key, status, remote_id=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE meta_delivery_attempts SET status=?, remote_id=?, updated_at=? WHERE dedupe_key=?",
                (status, remote_id, now, key))
            self.store.conn.commit()

    def get(self, key):
        with self.store.lock:
            row = self.store.conn.execute(
                "SELECT status, remote_id FROM meta_delivery_attempts WHERE dedupe_key=?", (key,)
            ).fetchone()
        return row

    def has_effect(self, key):
        row = self.get(key)
        return bool(row and row[0] == "SENT" and row[1])


class MetaSender:
    # Graph has no idempotency key: recovery never replays an old intent.
    retry_intended = False
    retry_claimed_after_restart = False

    def __init__(self, graph, store, outbound_enabled):
        self.graph = graph
        self.outbound_enabled = outbound_enabled
        self.receiver = MetaDeliveryReceiver(store)

    def send(self, payload):
        if not self.outbound_enabled():
            return {"status": "failed", "error": "Meta outbound is disabled"}
        key = payload.get("dedupe_key")
        comment_id = payload.get("comment_id")
        text = payload.get("text")
        if not all(isinstance(value, str) and value for value in (key, comment_id, text)):
            return {"status": "failed", "error": "Invalid reply payload"}
        if not self.receiver.begin(key):
            status, remote_id = self.receiver.get(key) or ("UNKNOWN", None)
            if status == "SENT":
                return {"status": "sent", "id": remote_id, "duplicate_delivery": True}
            return {"status": "UNKNOWN" if status != "FAILED" else "failed"}
        try:
            response = self.graph.reply_to_comment(comment_id, text)
            remote_id = response.get("id") if isinstance(response, dict) else None
            if not isinstance(remote_id, str) or not remote_id:
                self.receiver.finish(key, "UNKNOWN")
                return {"status": "UNKNOWN"}
            try:
                self.receiver.finish(key, "SENT", remote_id)
            except Exception:
                # Meta confirmed the reply; the durable ATTEMPTING row still blocks replay.
                return {"status": "UNKNOWN"}
            return {"status": "sent", "id": remote_id}
        except Exception as error:
            unknown = bool(getattr(error, "outcome_unknown", not isinstance(error, ValueError)))
            status = "UNKNOWN" if unknown else "FAILED"
            try:
                self.receiver.finish(key, status)
            except Exception:
                pass
            return {"status": "UNKNOWN" if unknown else "failed"}
