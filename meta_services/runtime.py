"""Durable comment intake and the existing intent engine for one Meta Page."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.adapters.luna import MODEL, call_luna
from app.engine import Engine, WallClock
from app.facts import Facts
from app.state import Store
from scripts import budget_ledger

from .graph import MetaGraphClient
from .graph import MetaGraphError
from .sender import MetaSender
from .webhook import extract_comment_events


_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z")


class CommentInbox:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.conn.execute("""CREATE TABLE IF NOT EXISTS meta_inbox(
                comment_id TEXT PRIMARY KEY, event TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, last_error TEXT, result TEXT,
                next_attempt_at TEXT)""")
            columns = {row[1] for row in store.conn.execute("PRAGMA table_info(meta_inbox)")}
            if "next_attempt_at" not in columns:
                store.conn.execute("ALTER TABLE meta_inbox ADD COLUMN next_attempt_at TEXT")
            store.conn.execute("UPDATE meta_inbox SET status='PENDING' WHERE status='PROCESSING'")
            store.conn.execute("""CREATE TABLE IF NOT EXISTS meta_manual_writes(
                idempotency_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)""")
            store.conn.commit()

    def enqueue(self, event):
        now = datetime.now(timezone.utc).isoformat()
        with self.store.lock:
            cur = self.store.conn.execute(
                "INSERT OR IGNORE INTO meta_inbox "
                "(comment_id,event,status,attempts,created_at,updated_at,last_error,result,next_attempt_at) "
                "VALUES (?,?, 'PENDING',0,?,?,NULL,NULL,NULL)",
                (event["comment_id"], json.dumps(event, ensure_ascii=False), now, now))
            self.store.conn.commit()
            return cur.rowcount == 1

    def claim(self):
        with self.store.lock:
            self.store.conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(timezone.utc).isoformat()
            row = self.store.conn.execute(
                "SELECT comment_id,event,attempts FROM meta_inbox WHERE status='PENDING' "
                "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY rowid LIMIT 1", (now,)
            ).fetchone()
            if not row:
                self.store.conn.commit()
                return None
            self.store.conn.execute(
                "UPDATE meta_inbox SET status='PROCESSING', attempts=attempts+1, updated_at=?, "
                "next_attempt_at=NULL WHERE comment_id=?",
                (now, row[0]))
            self.store.conn.commit()
            return row[0], json.loads(row[1]), row[2] + 1

    def finish(self, comment_id, status, *, error=None, result=None, retry_after=0):
        now = datetime.now(timezone.utc).isoformat()
        next_attempt = ((datetime.now(timezone.utc) + timedelta(seconds=retry_after)).isoformat()
                        if status == "PENDING" and retry_after else None)
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE meta_inbox SET status=?, updated_at=?, last_error=?, result=?, next_attempt_at=? "
                "WHERE comment_id=?",
                (status, now, error, json.dumps(result, ensure_ascii=False) if result is not None else None,
                 next_attempt, comment_id))
            self.store.conn.commit()

    def list(self, limit=50):
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT comment_id,event,status,attempts,created_at,updated_at,last_error,result,next_attempt_at "
                "FROM meta_inbox ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [dict(comment_id=r[0], event=json.loads(r[1]), status=r[2], attempts=r[3],
                     created_at=r[4], updated_at=r[5], last_error=r[6],
                     result=json.loads(r[7]) if r[7] else None, next_attempt_at=r[8]) for r in rows]


class MetaRuntime:
    def __init__(self, graph, *, app_secret, verify_token, service_api_key,
                 outbound_enabled=False, db_path="runs/meta_services.sqlite3",
                 budget_path="runs/meta_services_budget.jsonl", model_call=None):
        self.graph = graph
        self.page_id = graph.page_id
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.service_api_key = service_api_key
        self.outbound_enabled = bool(outbound_enabled)
        self.budget_path = str(budget_path)
        self.model_call = model_call or call_luna
        if db_path != ":memory:":
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
        self.store = Store(db_path)
        self.sender = MetaSender(self.graph, self.store, lambda: self.outbound_enabled)
        self.engine = Engine(self.store, Facts([]), self.sender, WallClock())
        self.inbox = CommentInbox(self.store)
        self._stop = threading.Event()
        self._thread = None
        self._recover_outbox()

    @classmethod
    def from_env(cls):
        graph = MetaGraphClient.from_env()
        return cls(
            graph,
            app_secret=os.environ.get("META_APP_SECRET", ""),
            verify_token=os.environ.get("META_WEBHOOK_VERIFY_TOKEN", ""),
            service_api_key=os.environ.get("META_SERVICE_API_KEY", ""),
            outbound_enabled=os.environ.get("META_OUTBOUND_ENABLED", "false").lower() == "true",
        )

    def _recover_outbox(self):
        # Reconcile only confirmed Meta receipts; no old Facebook write is replayed.
        self.engine.reconcile_unknown()
        with self.store.lock:
            self.store.conn.execute("UPDATE outbox SET status='BLOCKED' WHERE status='INTENDED'")
            self.store.conn.execute("UPDATE outbox SET status='UNKNOWN' WHERE status='CLAIMED'")
            self.store.conn.commit()

    def enqueue_webhook(self, payload):
        queued = 0
        for event in extract_comment_events(payload, self.page_id):
            if not event["post_id"].startswith(self.page_id + "_"):
                continue
            queued += int(self.inbox.enqueue(event))
        return queued

    def _bot_replies(self, actor_id):
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT json_extract(o.payload,'$.text') FROM outbox o "
                "WHERE o.purpose='reply' AND o.status='SENT' AND EXISTS ("
                "SELECT 1 FROM events e WHERE e.comment_id=json_extract(o.payload,'$.comment_id') "
                "AND e.actor_id=? AND json_extract(e.payload,'$.page_id')=?) "
                "ORDER BY o.created_at DESC LIMIT 5", (actor_id, self.page_id)).fetchall()
        return [row[0] for row in reversed(rows) if isinstance(row[0], str)]

    def _call_model(self, event, post, parent, context):
        inflight = 0

        def guard():
            nonlocal inflight
            allowed, _ = budget_ledger.guard(
                self.budget_path, max_calls=300, max_cost=2.00,
                est_attempts=inflight + 1, est_cost=0.01 * (inflight + 1))
            if allowed:
                inflight += 1
            return allowed

        result = self.model_call(
            post, event["text"], parent=parent, context=context,
            budget_guard=guard)
        attempts = result.get("attempts", 0)
        if attempts:
            cost = result.get("cost_usd_est") if not result.get("cost_unknown", True) else None
            if result.get("ok") and attempts > 1:
                if result.get("retry_cost_unknown", True):
                    cost = None
                elif cost is not None:
                    cost += result.get("retry_cost_usd_est", 0) or 0
            budget_ledger.record(
                self.budget_path, run_id=f"meta:{event['comment_id']}", kind="call",
                attempts=attempts, cost_usd_est=cost, model=result.get("model_effective", MODEL),
                status=None if result.get("ok") else "failed")
        return result

    def _process_event(self, event):
        comment = self.graph.get_comment(event["comment_id"])
        author = comment.get("from") or {}
        if comment.get("id") != event["comment_id"] or author.get("id") != event["actor_id"]:
            return {"routing": ["IGNORED_CANONICAL_MISMATCH"]}
        if event["actor_id"] == self.page_id:
            return {"routing": ["PAGE_SELF_IGNORED"]}
        comment_text = comment.get("message")
        if not isinstance(comment_text, str) or not comment_text.strip() or len(comment_text) > 10000:
            return {"routing": ["IGNORED_EMPTY_OR_LONG_COMMENT"]}
        event = {**event, "text": comment_text}

        post = self.graph.get_post(event["post_id"])
        owner = post.get("from") or {}
        if (post.get("id") != event["post_id"] or
                (owner.get("id") != self.page_id if owner.get("id")
                 else not event["post_id"].startswith(self.page_id + "_"))):
            return {"routing": ["IGNORED_OUT_OF_PAGE"]}
        comment_object = comment.get("object") or {}
        if comment_object.get("id") and comment_object["id"] != event["post_id"]:
            return {"routing": ["IGNORED_COMMENT_POST_MISMATCH"]}

        parent_text = post.get("message") or ""
        if event["parent_id"] != event["post_id"]:
            parent = self.graph.get_comment(event["parent_id"])
            parent_object = parent.get("object") or {}
            if parent_object.get("id") not in (None, event["post_id"]):
                return {"routing": ["IGNORED_PARENT_POST_MISMATCH"]}
            parent_text = parent.get("message") or parent_text

        event = {**event, "actor_role": "user", "event_dedupe_key": event["comment_id"] + ":1"}
        postcard = {"id": event["post_id"], "page_id": self.page_id,
                    "text": (post.get("message") or "")[:12000],
                    "title": "Facebook Page post", "vehicles": [],
                    "topic": "", "persona_default": "tư vấn viên",
                    "published_at": post.get("created_time")}
        context = self.engine.context(event, postcard)
        context["recent_bot_replies"] = self._bot_replies(event["actor_id"])
        if parent_text:
            context["external_parent_text"] = parent_text[:5000]
        model_context = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        if len(model_context) > 18000:
            model_context = model_context[-18000:]
        model = self._call_model(event, postcard["text"], parent_text[:5000], model_context)
        semantic = model.get("semantic") if model.get("ok") else None
        trusted_context = [postcard["text"], parent_text, *context["recent_bot_replies"]]
        return self.engine.process(event, postcard, semantic,
                                   trusted_context_texts=trusted_context,
                                   trusted_source_ids={event["post_id"]: postcard["text"]})

    def process_next(self):
        job = self.inbox.claim()
        if job is None:
            return False
        comment_id, event, attempt = job
        try:
            result = self._process_event(event)
            self.inbox.finish(comment_id, "DONE", result={
                "routing": result.get("routing", []),
                "outbound_permission": result.get("outbound_permission", False),
                "response_text": result.get("response_text"),
            })
        except MetaGraphError as error:
            retryable = error.status is None or error.status in (429, 500, 502, 503, 504) or error.code == 4
            if retryable and attempt < 3:
                self.inbox.finish(comment_id, "PENDING", error="MetaGraphError",
                                  retry_after=min(2 ** attempt, 16))
                return True
            self.inbox.finish(comment_id, "FAILED", error="MetaGraphError")
        except Exception as error:
            # Error detail is intentionally omitted from persistent logs (tokens and customer text).
            self.inbox.finish(comment_id, "FAILED", error=type(error).__name__)
        return True

    def start_worker(self):
        if self._thread and self._thread.is_alive():
            return

        def run():
            while not self._stop.is_set():
                try:
                    progressed = self.process_next()
                except Exception:
                    progressed = False
                if not progressed:
                    self._stop.wait(1)

        self._thread = threading.Thread(target=run, name="meta-comment-worker", daemon=True)
        self._thread.start()

    def stop_worker(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def list_reviews(self, limit=50):
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT dedupe_key,payload,created_at FROM outbox "
                "WHERE status='NEEDS_REVIEW' ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(id=r[0], payload=json.loads(r[1]), created_at=r[2]) for r in rows]

    def list_inbox(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return self.inbox.list(limit)

    def claim_manual_write(self, key, operation, target, payload):
        if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
            raise ValueError("Idempotency-Key must be 8-128 safe characters")
        fingerprint = hashlib.sha256(json.dumps(
            [operation, target, payload], ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with self.store.lock:
            cur = self.store.conn.execute(
                "INSERT OR IGNORE INTO meta_manual_writes VALUES (?,?, 'ATTEMPTING',NULL,?,?)",
                (key, fingerprint, now, now))
            if cur.rowcount == 1:
                self.store.conn.commit()
                return {"claimed": True}
            row = self.store.conn.execute(
                "SELECT fingerprint,status,result FROM meta_manual_writes WHERE idempotency_key=?", (key,)
            ).fetchone()
            self.store.conn.commit()
        if row[0] != fingerprint:
            raise ValueError("Idempotency-Key was already used for a different action")
        if row[1] == "SENT" and row[2]:
            return {"claimed": False, "status": "SENT", "result": json.loads(row[2])}
        return {"claimed": False, "status": row[1]}

    def finish_manual_write(self, key, status, result=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE meta_manual_writes SET status=?,result=?,updated_at=? WHERE idempotency_key=?",
                (status, json.dumps(result, ensure_ascii=False) if result is not None else None, now, key))
            self.store.conn.commit()

    def close(self):
        self.stop_worker()
        self.store.conn.close()
