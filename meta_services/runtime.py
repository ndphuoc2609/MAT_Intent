"""Durable comment intake and the existing intent engine for one Meta Page."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.adapters.luna import MODEL, call_luna
from app.adapters.jev import JevOpenRouter, intent_questions
from app.adapters.decision_fast_path import social_ack_candidate, social_ack_eligible
from app.normalize import VEHICLES, norm
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
                 database_url=None,
                 budget_path="runs/meta_services_budget.jsonl", model_call=None,
                 trace_path="runs/meta_services_trace.jsonl", jev_shadow_enabled=None,
                 jev_adapter=None, jev_budget_path=None,
                 jev_fast_path_enabled=None, jev_social_threshold=None):
        self.graph = graph
        self.page_id = graph.page_id
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.service_api_key = service_api_key
        self.outbound_enabled = bool(outbound_enabled)
        self.budget_path = str(budget_path)
        self.trace_path = str(trace_path)
        self.jev_budget_path = str(jev_budget_path or budget_path)
        self.max_model_calls = _env_int("META_MODEL_MAX_CALLS")
        self.max_model_cost = _env_float("META_MODEL_MAX_COST_USD")
        self.model_reserve = _env_float("META_MODEL_RESERVE_USD")
        self.jev_max_calls = _env_int("META_JEV_MAX_CALLS")
        if self.jev_max_calls is None: self.jev_max_calls = self.max_model_calls
        self.jev_max_cost = _env_float("META_JEV_MAX_COST_USD")
        if self.jev_max_cost is None: self.jev_max_cost = self.max_model_cost
        self.jev_reserve = _env_float("META_JEV_RESERVE_USD")
        self.jev_shadow_enabled = (os.environ.get("META_JEV_SHADOW_ENABLED", "false").lower() == "true"
                                   if jev_shadow_enabled is None else bool(jev_shadow_enabled))
        self.jev_fast_path_enabled = (
            os.environ.get("META_JEV_FAST_PATH_ENABLED", "false").lower() == "true"
            if jev_fast_path_enabled is None else bool(jev_fast_path_enabled))
        self.jev_social_threshold = (_env_float("META_JEV_SOCIAL_THRESHOLD")
                                     if jev_social_threshold is None else jev_social_threshold)
        self.jev_adapter = jev_adapter or JevOpenRouter()
        self.model_call = model_call or call_luna
        if database_url:
            from app.pg_adapter import connect as connect_postgres
            self.store = connect_postgres(database_url)
        else:
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
            database_url=os.environ.get("META_DATABASE_URL") or None,
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
        started = time.perf_counter()
        reservations = []

        def guard():
            reservation_id = f"meta:{event['comment_id']}:{uuid.uuid4().hex}"
            allowed, _ = budget_ledger.reserve(
                self.budget_path, reservation_id=reservation_id,
                max_calls=self.max_model_calls, max_cost=self.max_model_cost,
                reserve_cost_usd=self.model_reserve)
            if allowed: reservations.append(reservation_id)
            return allowed

        result = self.model_call(
            post, event["text"], parent=parent, context=context,
            budget_guard=guard)
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        result["_trace"] = {"model_requested": MODEL,
            "model_effective": result.get("model_effective", MODEL), "adapter": "luna",
            "schema_version": "1", "prompt_version": "luna_system_v1",
            "latency_ms": (time.perf_counter() - started) * 1000,
            "usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                      if type(usage.get(k)) is int},
            "cost_usd": result.get("cost_usd_est"),
            "cost_unknown": result.get("cost_unknown", True), "attempts": result.get("attempts", 0)}
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
            budget_ledger.settle_reservations(self.budget_path, reservations)
        return result

    def _start_jev_shadow(self, event, context):
        if not self.jev_shadow_enabled:
            return
        event_hash = hashlib.sha256(event["comment_id"].encode()).hexdigest()

        def run():
            reservations = []
            def guard():
                reservation_id = f"jev:{event_hash}:{uuid.uuid4().hex}"
                allowed, _ = budget_ledger.reserve(
                    self.jev_budget_path, reservation_id=reservation_id,
                    max_calls=self.jev_max_calls, max_cost=self.jev_max_cost,
                    reserve_cost_usd=self.jev_reserve)
                if allowed: reservations.append(reservation_id)
                return allowed
            result = self.jev_adapter.decide(context, intent_questions(), timeout=8,
                                             budget_guard=guard)
            if result.get("attempts"):
                budget_ledger.record(self.jev_budget_path, run_id=event_hash, kind="shadow",
                    attempts=result["attempts"], cost_usd_est=result.get("cost_usd_est"),
                    model=result.get("model_effective", "typesafe/jev-1.14"),
                    status=None if result.get("ok") else "failed")
                budget_ledger.settle_reservations(self.jev_budget_path, reservations)
            from scripts.model_trace import append
            append(self.trace_path, {"event_hash": event_hash,
                "model_requested": "typesafe/jev-1.14",
                "model_effective": result.get("model_effective", "typesafe/jev-1.14"),
                "provider": result.get("provider"), "adapter": "jev_decisions",
                "schema_version": "intent_decisions_v1", "prompt_version": "jev_intent_questions_v1",
                "latency_ms": result.get("latency_ms"), "usage": result.get("usage", {}),
                "cost_usd": result.get("cost_usd_est"), "cost_unknown": result.get("cost_unknown", True),
                "attempts": result.get("attempts", 0), "shadow": True,
                "routing": ["SHADOW_COMPLETED" if result.get("ok") else "SHADOW_FAILED"]})
        threading.Thread(target=run, name="jev-shadow", daemon=True).start()

    def _try_jev_fast_path(self, event, context):
        threshold = self.jev_social_threshold
        if (not self.jev_fast_path_enabled or not social_ack_eligible(event.get("text")) or
                type(threshold) not in (int, float) or not 0 <= threshold <= 1):
            return None
        event_hash = hashlib.sha256(event["comment_id"].encode()).hexdigest()
        reservations = []
        def guard():
            reservation_id = f"jev-fast:{event_hash}:{uuid.uuid4().hex}"
            allowed, _ = budget_ledger.reserve(
                self.jev_budget_path, reservation_id=reservation_id,
                max_calls=self.jev_max_calls, max_cost=self.jev_max_cost,
                reserve_cost_usd=self.jev_reserve)
            if allowed: reservations.append(reservation_id)
            return allowed
        result = self.jev_adapter.decide(context, {"social": intent_questions()["social"]},
                                         timeout=8, budget_guard=guard)
        if result.get("attempts"):
            budget_ledger.record(self.jev_budget_path, run_id=event_hash, kind="fast_path",
                attempts=result["attempts"], cost_usd_est=result.get("cost_usd_est"),
                model=result.get("model_effective", "typesafe/jev-1.14"),
                status=None if result.get("ok") else "failed")
            budget_ledger.settle_reservations(self.jev_budget_path, reservations)
        semantic = social_ack_candidate(event["text"],
            (result.get("answers") or {}).get("social"), threshold) if result.get("ok") else None
        result["semantic"] = semantic
        result["_trace"] = {"event_hash": event_hash, "model_requested": "typesafe/jev-1.14",
            "model_effective": result.get("model_effective", "typesafe/jev-1.14"),
            "provider": result.get("provider"), "adapter": "jev_decisions",
            "schema_version": "1" if semantic else "intent_decisions_v1",
            "prompt_version": "jev_social_fast_path_v1", "latency_ms": result.get("latency_ms"),
            "usage": result.get("usage", {}), "cost_usd": result.get("cost_usd_est"),
            "cost_unknown": result.get("cost_unknown", True), "attempts": result.get("attempts", 0),
            "routing": ["FAST_PATH_ACCEPTED" if semantic else "FAST_PATH_FALLBACK"]}
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

        post_text = post.get("message") or ""
        parent_text, parent_author = post_text, self.page_id
        parent_comment = None
        if event["parent_id"] != event["post_id"]:
            parent_comment = self.graph.get_comment(event["parent_id"])
            parent_object = parent_comment.get("object") or {}
            if parent_object.get("id") not in (None, event["post_id"]):
                return {"routing": ["IGNORED_PARENT_POST_MISMATCH"]}
            parent_text = parent_comment.get("message") or parent_text
            parent_author = (parent_comment.get("from") or {}).get("id")

        root_comment = parent_comment
        root_author = parent_author
        root_text = parent_text
        if event["thread_root_id"] not in (event["post_id"], event["comment_id"], event["parent_id"]):
            root_comment = self.graph.get_comment(event["thread_root_id"])
            root_object = root_comment.get("object") or {}
            if root_object.get("id") not in (None, event["post_id"]):
                return {"routing": ["IGNORED_ROOT_POST_MISMATCH"]}
            root_text = root_comment.get("message") or ""
            root_author = (root_comment.get("from") or {}).get("id")

        event = {**event, "actor_role": "user", "event_dedupe_key": event["comment_id"] + ":1"}
        post_text = post_text[:12000]
        post_vehicles = sorted({canonical for alias, canonical in VEHICLES.items()
                                if re.search(r"(?<!\w)" + re.escape(norm(alias)) + r"(?!\w)", norm(post_text))})
        postcard = {"id": event["post_id"], "page_id": self.page_id,
                    "text": post_text, "title": post.get("title") or "Facebook Page post",
                    "vehicles": post_vehicles,
                    "topic": "", "persona_default": "tư vấn viên",
                    "published_at": post.get("created_time")}
        trusted_messages = []
        if root_comment and event["thread_root_id"] not in (event["post_id"], event["comment_id"]):
            trusted_messages.append({"message_id": event["thread_root_id"], "actor_id": root_author,
                "page_id": self.page_id, "post_id": event["post_id"],
                "thread_root_id": event["thread_root_id"], "role": "root", "text": root_text})
        if parent_comment and event["parent_id"] not in (event["post_id"], event["comment_id"]):
            trusted_messages.append({"message_id": event["parent_id"], "actor_id": parent_author,
                "page_id": self.page_id, "post_id": event["post_id"],
                "thread_root_id": event["thread_root_id"], "role": "parent", "text": parent_text})
        context = self.engine.context(event, postcard, trusted_messages)
        context["recent_bot_replies"] = self._bot_replies(event["actor_id"])
        from scripts.run_multiturn_eval import fmt_context
        model_context = fmt_context(context)
        fast_path = self._try_jev_fast_path(event, model_context)
        trace_rows = [fast_path["_trace"]] if fast_path else []
        if fast_path and fast_path.get("semantic"):
            model = {"ok": True, "semantic": fast_path["semantic"], "attempts": fast_path.get("attempts", 0),
                     "model_effective": fast_path.get("model_effective", "typesafe/jev-1.14"),
                     "_trace": fast_path["_trace"]}
        else:
            if fast_path is None:
                self._start_jev_shadow(event, model_context)
            model = self._call_model(event, postcard["text"], "", model_context)
            trace_rows.append(model.get("_trace"))
        semantic = model.get("semantic") if model.get("ok") else None
        trusted_context = [postcard["text"], root_text, parent_text, *context["recent_bot_replies"]]
        source_ids = {event["post_id"]: {"text": postcard["text"], "actor_id": self.page_id, "role": "post"}}
        for message in trusted_messages:
            source_ids[message["message_id"]] = message
        result = self.engine.process(event, postcard, semantic,
                                     trusted_context_texts=trusted_context,
                                     trusted_source_ids=source_ids,
                                     expected_state_token=context["state_token"],
                                     trusted_context_messages=trusted_messages)
        result["_model_traces"] = [trace for trace in trace_rows if trace]
        return result

    def process_next(self):
        job = self.inbox.claim()
        if job is None:
            return False
        comment_id, event, attempt = job
        started = time.perf_counter()
        try:
            result = self._process_event(event)
            model_traces = result.pop("_model_traces", [])
            trace_write_failed = False
            if model_traces:
                from scripts.model_trace import append
                for model_trace in model_traces:
                    try:
                        append(self.trace_path, {**model_trace,
                            "event_hash": hashlib.sha256(comment_id.encode()).hexdigest(),
                            "routing": model_trace.get("routing", result.get("routing", [])),
                            "review_reasons": [x for x in result.get("routing", []) if x.startswith("REVIEW")],
                            "pipeline_latency_ms": (time.perf_counter() - started) * 1000,
                            "shadow": False})
                    except OSError:
                        trace_write_failed = True
            self.inbox.finish(comment_id, "DONE", result={
                "routing": result.get("routing", []),
                "outbound_permission": result.get("outbound_permission", False),
                "response_text": result.get("response_text"),
                "trace_write_failed": trace_write_failed,
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
                "INSERT OR IGNORE INTO meta_manual_writes "
                "(idempotency_key,fingerprint,status,result,created_at,updated_at) "
                "VALUES (?,?, 'ATTEMPTING',NULL,?,?)",
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


def _env_int(name):
    try:
        value = os.environ.get(name)
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _env_float(name):
    try:
        value = float(os.environ[name])
        return value if value >= 0 else None
    except (KeyError, TypeError, ValueError):
        return None
