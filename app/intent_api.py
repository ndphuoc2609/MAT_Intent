"""Standalone, authenticated HTTP API for MAT Intent; no Meta/channel imports."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .adapters.jev import JevOpenRouter, intent_questions, validate_answers
from .adapters.luna import PINNED_MODEL, call_luna
from .context import fmt_context
from .engine import Engine, WallClock
from .facts import Facts
from .pg_adapter import connect as connect_postgres
from .semantics import upgrade_semantic
from .state import EventPayloadConflict, FakeSender, IntentLeaseLost, Store

API_VERSION = "1"
SCHEMA_VERSION = "2"
MAX_BODY_BYTES = 128 * 1024
MAX_TEXT_CHARS = 20_000
LEASE_SECONDS = 180
_EVENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_JEV_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="intent-jev-shadow")
_JEV_SLOTS = threading.BoundedSemaphore(2)


class BudgetLedgerWriteFailure(Exception):
    """A provider attempt happened, but its durable cost record is uncertain."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_message(value, label, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, dict) or set(value) != {"id", "actor_id", "text"}:
        raise ValueError(f"{label} must contain exactly id, actor_id, text")
    for name in ("id", "actor_id"):
        part = value[name]
        if not isinstance(part, str) or not part.strip() or len(part) > 256:
            raise ValueError(f"{label}.{name} must be a non-empty string of at most 256 characters")
    if not isinstance(value["text"], str) or len(value["text"]) > MAX_TEXT_CHARS:
        raise ValueError(f"{label}.text must be a string of at most {MAX_TEXT_CHARS} characters")
    return value


def validate_request(data):
    allowed = {"api_version", "event_id", "event_time", "page_id", "post", "thread_id",
               "root", "parent", "current"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("request must be an object with only documented fields")
    if data.get("api_version", API_VERSION) != API_VERSION:
        raise ValueError("api_version must be '1'")
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise ValueError("event_id must be 1-128 ASCII letters, digits, dot, underscore, colon, or hyphen")
    page_id, thread_id = data.get("page_id"), data.get("thread_id")
    if not isinstance(page_id, str) or not page_id.strip() or len(page_id) > 256:
        raise ValueError("page_id must be a non-empty string of at most 256 characters")
    if not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 256:
        raise ValueError("thread_id must be a non-empty string of at most 256 characters")
    post = data.get("post")
    if (not isinstance(post, dict) or set(post) - {"id", "text", "vehicles"} or
            not {"id", "text"}.issubset(post)):
        raise ValueError("post must contain id, text, and optional vehicles")
    if not isinstance(post.get("id"), str) or not post["id"].strip() or len(post["id"]) > 256:
        raise ValueError("post.id must be a non-empty string of at most 256 characters")
    if not isinstance(post.get("text"), str) or len(post["text"]) > MAX_TEXT_CHARS:
        raise ValueError(f"post.text must be a string of at most {MAX_TEXT_CHARS} characters")
    vehicles = post.get("vehicles", [])
    if (not isinstance(vehicles, list) or len(vehicles) > 64 or
            any(not isinstance(v, str) or not v.strip() or len(v) > 80 for v in vehicles)):
        raise ValueError("post.vehicles must be a list of at most 64 short strings")
    root = _validate_message(data.get("root"), "root")
    parent = _validate_message(data.get("parent"), "parent", optional=True)
    current = _validate_message(data.get("current"), "current")
    if root["id"] != thread_id:
        raise ValueError("thread_id must equal root.id")
    if parent and parent["id"] == root["id"] and (
            parent["actor_id"] != root["actor_id"] or parent["text"] != root["text"]):
        raise ValueError("parent must match root when parent.id equals root.id")
    if current["id"] == thread_id and (current["actor_id"] != root["actor_id"] or
                                        current["text"] != root["text"]):
        raise ValueError("current must match root when current.id equals thread_id")
    if current["id"] != thread_id and current["id"] == (parent or {}).get("id"):
        raise ValueError("current.id cannot equal parent.id")
    if parent and parent["id"] != thread_id and parent["id"] == current["id"]:
        raise ValueError("parent.id cannot equal current.id")
    event_time = data.get("event_time")
    if event_time is not None:
        if not isinstance(event_time, str) or len(event_time) > 64:
            raise ValueError("event_time must be an ISO timestamp string")
        try:
            datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("event_time must be an ISO timestamp string") from exc
    text_size = sum(len(x["text"]) for x in (post, root, parent, current) if x)
    if text_size > 50_000:
        raise ValueError("combined post and message text exceeds 50000 characters")
    return {**data, "api_version": API_VERSION, "post": {**post, "vehicles": vehicles}}


def _json_value(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _result_status(routing):
    routes = [str(x) for x in (routing or [])]
    if any(x.startswith("ERROR_") for x in routes):
        return "PROCESSING_ERROR"
    if any("CONFLICT" in x for x in routes):
        return "CONTEXT_CONFLICT"
    if any(x.startswith("REVIEW") for x in routes):
        return "REVIEW"
    if any("HANDOFF" in x for x in routes):
        return "HANDOFF"
    if any("WAIT" in x for x in routes):
        return "WAIT"
    if any("STOP" in x or "CANCEL" in x for x in routes):
        return "STOP"
    return "PROCESSED"


class IntentService:
    """Owns persistence, inference, engine policy, and readiness checks."""

    def __init__(self, *, database_url=None, api_key=None, model_call=None,
                 model=None, budget_path=None, store=None, facts=None,
                 jev_adapter=None, jev_shadow_enabled=None, trace_path=None):
        self.api_key = api_key if api_key is not None else os.environ.get("INTENT_API_KEY", "")
        self.model = model or os.environ.get("INTENT_LUNA_MODEL", PINNED_MODEL)
        self.model_call = model_call or call_luna
        self.live_model = self.model_call is call_luna
        self.budget_path = budget_path or os.environ.get("INTENT_BUDGET_LEDGER_PATH", "runs/budget_ledger.jsonl")
        self.model_timeout = _positive_env_int("INTENT_MODEL_TIMEOUT_SECONDS", 45)
        self.model_max_tokens = _positive_env_int("INTENT_MODEL_MAX_TOKENS", 1200)
        self.lease_seconds = max(_positive_env_int("INTENT_API_LEASE_SECONDS", LEASE_SECONDS),
                                 self.model_timeout * 9 + 90)
        self.budget_max_calls = _env_int("INTENT_MODEL_MAX_CALLS")
        self.budget_max_cost = _env_float("INTENT_MODEL_MAX_COST_USD")
        self.budget_reserve = _env_float("INTENT_MODEL_RESERVE_USD")
        from scripts import budget_ledger
        self.budget_prior_calls = budget_ledger.SHARED_PRIOR_CALLS
        self.budget_prior_cost = budget_ledger.SHARED_PRIOR_COST_USD
        self.jev_adapter = jev_adapter or JevOpenRouter()
        self.jev_shadow_enabled = (os.environ.get("INTENT_JEV_SHADOW_ENABLED", "false").lower() == "true"
                                   if jev_shadow_enabled is None else bool(jev_shadow_enabled))
        self.jev_budget_path = os.environ.get("INTENT_JEV_BUDGET_LEDGER_PATH", self.budget_path)
        self.jev_max_calls = _env_int("INTENT_JEV_MODEL_MAX_CALLS")
        self.jev_max_cost = _env_float("INTENT_JEV_MODEL_MAX_COST_USD")
        self.jev_reserve = _env_float("INTENT_JEV_MODEL_RESERVE_USD")
        self.trace_path = trace_path or os.environ.get("INTENT_TRACE_PATH", "runs/intent_trace.jsonl")
        self.worker_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self.database_reason = None
        self.store = store
        if store is None:
            dsn = database_url if database_url is not None else os.environ.get("MAT_INTENT_DATABASE_URL", "")
            if not dsn:
                self.database_reason = "MAT_INTENT_DATABASE_URL is not configured"
            else:
                try:
                    self.store = connect_postgres(dsn)
                except Exception as exc:
                    self.database_reason = _safe_database_error(exc)
        if self.store is not None:
            if not hasattr(self.store.conn, "raw"):
                self.store.conn.execute("""CREATE TABLE IF NOT EXISTS intent_api_events (
                    event_id TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL, page_id TEXT NOT NULL,
                    post_id TEXT NOT NULL, thread_root_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_until TEXT,
                    result TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    seq INTEGER PRIMARY KEY AUTOINCREMENT)""")
            self.engine = Engine(self.store, facts or Facts([]), FakeSender(), WallClock())
        else:
            self.engine = None

    def close(self):
        if self.store is not None:
            self.store.conn.close()

    def readiness(self):
        reasons = []
        if not self.api_key:
            reasons.append("INTENT_API_KEY is not configured")
        if self.store is None:
            reasons.append(self.database_reason or "PostgreSQL is unavailable")
        else:
            try:
                with self.store.lock:
                    self.store.conn.execute("SELECT 1 FROM intent_api_events LIMIT 1").fetchone()
            except Exception as exc:
                reasons.append(_safe_database_error(exc))
        if self.live_model:
            if not os.environ.get("OPENROUTER_API_KEY", "").strip():
                reasons.append("OPENROUTER_API_KEY is not configured")
            if (self.budget_max_calls is None or self.budget_max_calls <= 0 or
                    not _valid_budget_value(self.budget_max_cost) or
                    not _valid_budget_value(self.budget_reserve) or
                    self.budget_max_cost <= 0 or self.budget_reserve <= 0 or
                    self.budget_reserve > self.budget_max_cost):
                reasons.append("INTENT_MODEL_MAX_CALLS, INTENT_MODEL_MAX_COST_USD, and INTENT_MODEL_RESERVE_USD are required")
            else:
                try:
                    from scripts import budget_ledger
                    calls, cost, unknown, _ = budget_ledger.totals(self.budget_path)
                    calls += self.budget_prior_calls
                    cost += self.budget_prior_cost
                    if unknown:
                        reasons.append("model budget ledger contains unknown or invalid entries")
                    elif calls >= self.budget_max_calls:
                        reasons.append("model call budget is exhausted")
                    elif cost + self.budget_reserve > self.budget_max_cost:
                        reasons.append("model cost budget cannot cover one reserved call")
                except Exception:
                    reasons.append("model budget ledger cannot be verified")
        if self.jev_shadow_enabled:
            if (not os.environ.get("OPENROUTER_API_KEY", "").strip() or
                    self.jev_max_calls is None or self.jev_max_calls <= 0 or
                    not _valid_budget_value(self.jev_max_cost) or self.jev_max_cost <= 0 or
                    not _valid_budget_value(self.jev_reserve) or self.jev_reserve <= 0 or
                    self.jev_reserve > self.jev_max_cost):
                reasons.append("Jev shadow requires OPENROUTER_API_KEY and INTENT_JEV_MODEL_MAX_CALLS, "
                               "INTENT_JEV_MODEL_MAX_COST_USD, INTENT_JEV_MODEL_RESERVE_USD")
            else:
                try:
                    from scripts import budget_ledger
                    calls, cost, unknown, _ = budget_ledger.totals(self.jev_budget_path)
                    calls += self.budget_prior_calls
                    cost += self.budget_prior_cost
                    if unknown:
                        reasons.append("Jev budget ledger contains unknown or invalid entries")
                    elif calls >= self.jev_max_calls:
                        reasons.append("Jev call budget is exhausted")
                    elif cost + self.jev_reserve > self.jev_max_cost:
                        reasons.append("Jev cost budget cannot cover one reserved call")
                except Exception:
                    reasons.append("Jev budget ledger cannot be verified")
        return {"ok": not reasons, "reasons": reasons}

    def health(self):
        return {"ok": True, "service": "mat-intent", "api_version": API_VERSION}

    def _claim(self, request):
        digest = hashlib.sha256(_canonical(request).encode()).hexdigest()
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=self.lease_seconds)).isoformat()
        with self.store.lock:
            self.store.conn.execute("BEGIN IMMEDIATE")
            try:
                self.store.conn.execute(
                    "INSERT INTO intent_api_events(event_id,payload_hash,page_id,post_id,thread_root_id,actor_id,"
                    "comment_id,payload,status,attempts,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?, 'PENDING',0,?,?) ON CONFLICT(event_id) DO NOTHING",
                    (request["event_id"], digest, request["page_id"], request["post"]["id"],
                     request["thread_id"], request["current"]["actor_id"], request["current"]["id"],
                     _canonical(request), now_text, now_text))
                lock_clause = " FOR UPDATE" if hasattr(self.store.conn, "raw") else ""
                row = self.store.conn.execute(
                    "SELECT payload_hash,status,lease_until,result FROM intent_api_events WHERE event_id=?" +
                    lock_clause, (request["event_id"],)).fetchone()
                if not row or row[0] != digest:
                    self.store.conn.rollback()
                    return "conflict", None
                status, expires, result = row[1], row[2], row[3]
                if status in ("COMPLETED", "REVIEW"):
                    self.store.conn.commit()
                    return "replay", json.loads(result) if result else None
                if status == "PROCESSING" and expires:
                    try:
                        live = datetime.fromisoformat(expires) > now
                    except ValueError:
                        live = False
                    if live:
                        self.store.conn.commit()
                        return "processing", None
                    uncertain = self._base_result(request["event_id"], "REVIEW", None,
                        {"routing": ["REVIEW_INFERENCE_OUTCOME_UNKNOWN"], "state_changes": [], "state_after": None},
                        {"reason": "expired inference lease; event was not replayed"})
                    self.store.conn.execute(
                        "UPDATE intent_api_events SET status='REVIEW',result=?,last_error=?,lease_owner=NULL,"
                        "lease_until=NULL,updated_at=? WHERE event_id=?",
                        (_canonical(uncertain), "inference_outcome_unknown", now_text, request["event_id"]))
                    self.store.conn.commit()
                    return "replay", uncertain
                attempt_token = f"{self.worker_id}:{uuid.uuid4().hex}"
                self.store.conn.execute(
                    "UPDATE intent_api_events SET status='PROCESSING',attempts=attempts+1,lease_owner=?,"
                    "lease_until=?,last_error=NULL,updated_at=? WHERE event_id=?",
                    (attempt_token, lease_until, now_text, request["event_id"]))
                self.store.conn.commit()
                return "claimed", attempt_token
            except Exception:
                self.store.conn.rollback()
                raise

    def _finish(self, event_id, attempt_token, status, result=None, error=None, *, allow_expired=False):
        now = datetime.now(timezone.utc).isoformat()
        with self.store.lock:
            live_clause = "" if allow_expired else " AND lease_until>?"
            cur = self.store.conn.execute(
                "UPDATE intent_api_events SET status=?,result=?,last_error=?,lease_owner=NULL,lease_until=NULL,"
                "updated_at=? WHERE event_id=? AND status='PROCESSING' AND lease_owner=?" + live_clause,
                (status, _canonical(result) if result is not None else None, error, now,
                 event_id, attempt_token, *([] if allow_expired else [now])))
            self.store.conn.commit()
            return cur.rowcount == 1

    def _persist_context_messages(self, request, event, attempt_token):
        messages = [dict(request["root"], role="root")]
        if request.get("parent") and request["parent"]["id"] != request["root"]["id"]:
            messages.append(dict(request["parent"], role="parent"))
        with self.store.lock:
            self.store.conn.execute("BEGIN IMMEDIATE")
            try:
                self.store.assert_intent_lease(request["event_id"], attempt_token)
                for message in messages:
                    if message["id"] == event["comment_id"]:
                        continue
                    rows = self.store.conn.execute(
                        "SELECT actor_id,text,payload FROM events WHERE comment_id=? AND retracted=0 "
                        "AND json_extract(payload, '$.page_id')=? AND json_extract(payload, '$.post_id')=? "
                        "AND json_extract(payload, '$.thread_root_id')=? ORDER BY rowid DESC LIMIT 1",
                        (message["id"], event["page_id"], event["post_id"],
                         event["thread_root_id"])).fetchone()
                    if rows:
                        try:
                            existing = json.loads(rows[2] or "{}")
                        except (TypeError, ValueError):
                            existing = {}
                        if (rows[0] != message["actor_id"] or rows[1] != message["text"] or
                                any(existing.get(key) != event.get(key) for key in
                                    ("page_id", "post_id", "thread_root_id"))):
                            raise EventPayloadConflict("context message conflicts with stored thread history")
                        stored_semantic = self._stored_message_semantic(event, message)
                        if stored_semantic:
                            message["semantic"] = stored_semantic
                        continue
                    context_event = {
                        "page_id": event["page_id"], "post_id": event["post_id"],
                        "thread_root_id": event["thread_root_id"], "comment_id": message["id"],
                        "parent_id": message.get("parent_id", event["thread_root_id"]
                            if message["role"] == "parent" else None),
                        "event_time": message.get("event_time"),
                        "actor_id": message["actor_id"], "actor_role": "user",
                        "event_type": "context", "content_version": 1, "text": message["text"],
                    }
                    dedupe = "context:" + ":".join((event["page_id"], event["post_id"],
                                                      event["thread_root_id"], message["id"]))
                    self.store.insert_event(dedupe, context_event, message["actor_id"])
                self.store.assert_intent_lease(request["event_id"], attempt_token)
                self.store.conn.commit()
            except Exception:
                self.store.conn.rollback()
                raise
        return [{"message_id": message["id"], "actor_id": message["actor_id"],
                 "text": message["text"], "role": message["role"],
                 **({"semantic": message["semantic"]} if message.get("semantic") else {}),
                 "page_id": event["page_id"], "post_id": event["post_id"],
                 "thread_root_id": event["thread_root_id"],
                 "parent_id": message.get("parent_id", event["thread_root_id"]
                     if message["role"] == "parent" else None),
                 "event_time": message.get("event_time")} for message in messages]

    def _stored_message_semantic(self, event, message):
        row = self.store.conn.execute(
            "SELECT result FROM intent_api_events WHERE comment_id=? AND actor_id=? AND page_id=? "
            "AND post_id=? AND thread_root_id=? AND status IN ('COMPLETED','REVIEW') "
            "ORDER BY seq DESC LIMIT 1", (message["id"], message["actor_id"], event["page_id"],
                event["post_id"], event["thread_root_id"])).fetchone()
        try:
            from .semantics import validate_semantic
            semantic = json.loads(row[0]).get("semantic") if row and row[0] else None
            if isinstance(semantic, dict) and not validate_semantic(semantic, message["text"], []):
                return semantic
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return None

    @staticmethod
    def _base_result(event_id, status, semantic, engine_result, trace):
        routing = engine_result.get("routing") or []
        reason = trace.get("reason") or next((r for r in routing if r.startswith(("REVIEW", "HANDOFF", "WAIT", "STOP"))), None)
        return {
            "api_version": API_VERSION, "event_id": event_id, "status": status,
            "semantic_version": SCHEMA_VERSION if semantic else None,
            "semantic": _json_value(semantic),
            "decision": {"routing": list(routing), "reason": reason,
                         "outbound_effects_enabled": False},
            "state_changes": _json_value(engine_result.get("state_changes") or []),
            "state_after": _json_value(engine_result.get("state_after")),
            "trace": _json_value(trace), "idempotent_replay": False,
        }

    def process(self, raw_request):
        try:
            request = validate_request(raw_request)
        except ValueError as exc:
            return 422, {"api_version": API_VERSION, "status": "INVALID_REQUEST", "error": str(exc)}
        if self.store is None or self.engine is None:
            return 503, {"api_version": API_VERSION, "status": "NOT_READY",
                         "reasons": self.readiness()["reasons"]}
        try:
            claim, existing = self._claim(request)
        except Exception:
            return 503, {"api_version": API_VERSION, "status": "DATABASE_UNAVAILABLE"}
        if claim == "conflict":
            return 409, {"api_version": API_VERSION, "event_id": request["event_id"],
                         "status": "EVENT_ID_CONFLICT", "error": "event_id was already used with a different payload"}
        if claim == "processing":
            return 202, {"api_version": API_VERSION, "event_id": request["event_id"], "status": "PROCESSING"}
        if claim == "replay":
            if not isinstance(existing, dict):
                existing = {"api_version": API_VERSION, "event_id": request["event_id"],
                            "status": "REVIEW", "semantic": None,
                            "decision": {"routing": ["REVIEW_RESULT_UNAVAILABLE"],
                                         "reason": "stored event result is unavailable",
                                         "outbound_effects_enabled": False},
                            "state_changes": [], "state_after": None,
                            "trace": {"reason": "missing persisted result"},
                            "idempotent_replay": True}
            else:
                existing = dict(existing)
                existing["idempotent_replay"] = True
            return (409 if existing.get("status") == "CONTEXT_CONFLICT" else 200), existing
        attempt_token = existing

        def finish(status, result=None, error=None, *, allow_expired=False):
            return self._finish(request["event_id"], attempt_token, status, result, error,
                                allow_expired=allow_expired)

        def lease_lost_response():
            return 409, {"api_version": API_VERSION, "event_id": request["event_id"],
                         "status": "WORKER_LEASE_LOST", "error": "event attempt lost its lease"}

        readiness = self.readiness()
        if not readiness["ok"]:
            response = {"api_version": API_VERSION, "event_id": request["event_id"],
                        "status": "MODEL_UNAVAILABLE", "semantic": None,
                        "decision": {"routing": ["MODEL_UNAVAILABLE"], "reason": "service is not ready",
                                     "outbound_effects_enabled": False},
                        "state_changes": [], "state_after": None, "trace": {"model_requested": self.model,
                        "schema_version": SCHEMA_VERSION, "prompt_version": "luna_system_v2",
                        "cost_usd": None, "cost_unknown": True}, "reasons": readiness["reasons"],
                        "idempotent_replay": False}
            if not finish("PENDING", response, "model_or_service_not_ready"):
                return lease_lost_response()
            return 503, response

        event = {
            "page_id": request["page_id"], "post_id": request["post"]["id"],
            "thread_root_id": request["thread_id"], "comment_id": request["current"]["id"],
            "parent_id": request["parent"]["id"] if request.get("parent") else None,
            "actor_id": request["current"]["actor_id"], "actor_role": "user",
            "event_type": "create", "content_version": 1, "text": request["current"]["text"],
            "event_dedupe_key": request["event_id"], "event_time": request.get("event_time"),
        }
        postcard = {"id": request["post"]["id"], "page_id": request["page_id"],
                    "text": request["post"]["text"], "vehicles": request["post"]["vehicles"]}
        started = time.perf_counter()
        try:
            trusted_messages = self._persist_context_messages(request, event, attempt_token)
            semantic = None
            model_trace = None
            engine_result = None
            committed_response = None
            for inference_attempt in range(3):
                snapshot = self._prepare_context(event, postcard, trusted_messages, attempt_token)
                model_context = fmt_context(snapshot)
                if inference_attempt == 0:
                    self._start_jev_shadow(request, event, model_context)
                model_result = self._call_model(request, postcard, event, model_context)
                model_trace = model_result.pop("_intent_trace", {})
                if not model_result.get("ok"):
                    trace = {**model_trace, "pipeline_latency_ms": round((time.perf_counter() - started) * 1000, 3)}
                    response = self._base_result(request["event_id"], "MODEL_UNAVAILABLE", None,
                        {"routing": ["MODEL_UNAVAILABLE"], "state_changes": [], "state_after": None},
                        {**trace, "reason": "model budget blocked" if trace.get("budget_blocked")
                         else "semantic model unavailable"})
                    if not finish("PENDING", response, "model_unavailable"):
                        return lease_lost_response()
                    return 503, response
                semantic = upgrade_semantic(model_result.get("semantic"))
                if not isinstance(semantic, dict) or semantic.get("schema_version") != SCHEMA_VERSION:
                    response = self._base_result(request["event_id"], "REVIEW", None,
                        {"routing": ["REVIEW_SCHEMA"], "state_changes": [], "state_after": None},
                        {**model_trace, "reason": "semantic schema is invalid"})
                    if not finish("REVIEW", response, "review_schema"):
                        return lease_lost_response()
                    return 200, response

                def persist_engine_result(engine_result):
                    nonlocal committed_response
                    routes = engine_result.get("routing", [])
                    status = _result_status(routes)
                    if "REVIEW_STALE_CONTEXT" in routes or status in ("PROCESSING_ERROR", "CONTEXT_CONFLICT"):
                        return
                    semantic_out = engine_result.get("semantic")
                    semantic_out = upgrade_semantic(semantic_out) if semantic_out else None
                    trace = {**model_trace,
                        "model_requested": model_result.get("model_requested", self.model),
                        "model_effective": model_result.get("model_effective", self.model),
                        "schema_version": SCHEMA_VERSION,
                        "prompt_version": model_result.get("prompt_version", "luna_system_v2"),
                        "state_snapshot_version": hashlib.sha256(snapshot["state_token"].encode()).hexdigest()[:24],
                        "routing": routes,
                        "review_reason": next((r for r in routes if r.startswith("REVIEW")), None),
                        "pipeline_latency_ms": round((time.perf_counter() - started) * 1000, 3)}
                    response = self._base_result(request["event_id"], status, semantic_out,
                                                 engine_result, trace)
                    final_state = "REVIEW" if status == "REVIEW" else "COMPLETED"
                    now = datetime.now(timezone.utc).isoformat()
                    cur = self.store.conn.execute(
                        "UPDATE intent_api_events SET status=?,result=?,last_error=NULL,lease_owner=NULL,"
                        "lease_until=NULL,updated_at=? WHERE event_id=? AND status='PROCESSING' "
                        "AND lease_owner=? AND lease_until>?",
                        (final_state, _canonical(response), now, request["event_id"], attempt_token, now))
                    if cur.rowcount != 1:
                        raise IntentLeaseLost("intent lease lost before atomic result commit")
                    committed_response = (200, response)

                engine_result = self.engine.process(
                    event, postcard, semantic, expected_state_token=snapshot["state_token"],
                    trusted_context_messages=trusted_messages,
                    attempt_token=attempt_token,
                    attempt_result_callback=persist_engine_result,
                    trusted_context_texts=[request["post"]["text"], request["root"]["text"],
                                           request["parent"]["text"] if request.get("parent") else ""],
                    trusted_source_ids={request["post"]["id"]: {
                        "text": request["post"]["text"], "actor_id": request["page_id"], "role": "post",
                        "vehicles": request["post"]["vehicles"]}})
                if "REVIEW_STALE_CONTEXT" not in engine_result.get("routing", []):
                    break
                if inference_attempt == 2:
                    break
            if committed_response is not None:
                return committed_response
            semantic_out = engine_result.get("semantic") if engine_result else None
            semantic_out = upgrade_semantic(semantic_out) if semantic_out else None
            status = _result_status(engine_result.get("routing"))
            routes = engine_result.get("routing", [])
            trace = {**model_trace, "model_requested": model_result.get("model_requested", self.model),
                     "model_effective": model_result.get("model_effective", self.model),
                     "schema_version": SCHEMA_VERSION, "prompt_version": model_result.get("prompt_version", "luna_system_v2"),
                     "state_snapshot_version": hashlib.sha256(snapshot["state_token"].encode()).hexdigest()[:24],
                     "routing": routes,
                     "review_reason": next((r for r in routes if r.startswith("REVIEW")), None),
                     "pipeline_latency_ms": round((time.perf_counter() - started) * 1000, 3)}
            response = self._base_result(request["event_id"], status, semantic_out, engine_result, trace)
            if status == "PROCESSING_ERROR":
                response["decision"]["reason"] = "engine state transaction rolled back"
                response["trace"]["error_code"] = "ENGINE_ROLLBACK"
                if not finish("PENDING", response, "engine_rollback"):
                    return lease_lost_response()
                return 503, response
            if status == "CONTEXT_CONFLICT":
                response["decision"]["reason"] = "engine rejected conflicting event context"
                if not finish("REVIEW", response, "engine_event_conflict"):
                    return lease_lost_response()
                return 409, response
            final_state = "REVIEW" if status == "REVIEW" else "COMPLETED"
            if not finish(final_state, response):
                return lease_lost_response()
            return 200, response
        except EventPayloadConflict:
            response = {"api_version": API_VERSION, "event_id": request["event_id"],
                "status": "CONTEXT_CONFLICT", "semantic": None,
                "decision": {"routing": ["CONTEXT_CONFLICT"],
                    "reason": "root or parent conflicts with stored thread history",
                    "outbound_effects_enabled": False}, "state_changes": [],
                "state_after": None, "trace": {"reason": "stored context identity conflict"},
                "idempotent_replay": False}
            if not finish("REVIEW", response, "context_conflict"):
                return lease_lost_response()
            return 409, response
        except BudgetLedgerWriteFailure:
            response = self._base_result(request["event_id"], "REVIEW", None,
                {"routing": ["REVIEW_BUDGET_LEDGER"], "state_changes": [], "state_after": None},
                {"reason": "model call outcome could not be recorded in the budget ledger",
                 "schema_version": SCHEMA_VERSION, "prompt_version": "luna_system_v2",
                 "cost_usd": None, "cost_unknown": True})
            if not finish("REVIEW", response, "budget_ledger_write_failed"):
                return lease_lost_response()
            return 503, response
        except IntentLeaseLost:
            response = {"api_version": API_VERSION, "event_id": request["event_id"],
                "status": "REVIEW", "semantic": None,
                "decision": {"routing": ["REVIEW_LEASE_LOST"],
                    "reason": "event attempt lost its live lease before state commit",
                    "outbound_effects_enabled": False}, "state_changes": [],
                "state_after": None, "trace": {"reason": "attempt fencing rejected stale worker"},
                "idempotent_replay": False}
            if finish("REVIEW", response, "lease_lost", allow_expired=True):
                return 409, response
            return lease_lost_response()
        except Exception as exc:
            # Persist only the exception class: the provider/db error may contain user text or credentials.
            response = {"api_version": API_VERSION, "event_id": request["event_id"],
                         "status": "PROCESSING_ERROR", "error_code": type(exc).__name__,
                         "error": "event could not be completed safely",
                         "decision": {"routing": ["PROCESSING_ERROR"],
                            "reason": "event could not be completed safely",
                            "outbound_effects_enabled": False},
                         "state_changes": [], "state_after": None,
                         "trace": {"reason": "processing exception"},
                         "idempotent_replay": False}
            if not finish("PENDING", response, type(exc).__name__):
                return lease_lost_response()
            return 503, response

    def _call_model(self, request, postcard, event, context):
        reservations = []
        budget_denials = []
        event_hash = hashlib.sha256((request["page_id"] + "\x1f" + request["post"]["id"] +
            "\x1f" + request["thread_id"] + "\x1f" + request["current"]["actor_id"] +
            "\x1f" + request["event_id"]).encode()).hexdigest()
        def guard():
            from scripts import budget_ledger
            reservation_id = f"intent:{event_hash}:{uuid.uuid4().hex}"
            allowed, reason = budget_ledger.reserve(self.budget_path, reservation_id=reservation_id,
                max_calls=self.budget_max_calls, max_cost=self.budget_max_cost,
                reserve_cost_usd=self.budget_reserve, prior_calls=self.budget_prior_calls,
                prior_cost=self.budget_prior_cost)
            if allowed:
                reservations.append(reservation_id)
            else:
                budget_denials.append(reason)
            return allowed
        started = time.perf_counter()
        try:
            result = self.model_call(postcard["text"], event["text"], context=context,
                timeout=self.model_timeout, max_tokens=self.model_max_tokens,
                model=self.model, schema_version=SCHEMA_VERSION, budget_guard=guard)
            if not isinstance(result, dict):
                result = {"ok": False, "attempts": 0, "error": "invalid model adapter response"}
        except Exception as exc:
            result = {"ok": False, "attempts": 0, "error": type(exc).__name__}
        attempts = result.get("attempts") if type(result.get("attempts")) is int else 0
        cost = result.get("cost_usd_est")
        cost_known = type(cost) in (int, float) and cost >= 0 and not result.get("cost_unknown", True)
        trace = {"event_hash": event_hash, "adapter": "luna", "model_requested": self.model,
                 "model_effective": result.get("model_effective", self.model),
                 "schema_version": result.get("schema_version", SCHEMA_VERSION),
                 "prompt_version": result.get("prompt_version", "luna_system_v2"),
                 "model_latency_ms": round((time.perf_counter() - started) * 1000, 3),
                 "usage": {key: value for key, value in (result.get("usage") or {}).items()
                           if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and type(value) is int},
                 "cost_usd": float(cost) if cost_known else None, "cost_unknown": not cost_known,
                 "attempts": attempts}
        if attempts:
            from scripts import budget_ledger
            total_cost = cost if cost_known else None
            if result.get("ok") and attempts > 1:
                if result.get("retry_cost_unknown", True):
                    total_cost = None
                elif total_cost is not None:
                    total_cost += result.get("retry_cost_usd_est", 0) or 0
            try:
                budget_ledger.record(self.budget_path, run_id=f"intent:{event_hash}:{uuid.uuid4().hex}",
                    kind="call", attempts=attempts, cost_usd_est=total_cost,
                    model=result.get("model_effective", self.model), status=None if result.get("ok") else "failed")
            except Exception as exc:
                raise BudgetLedgerWriteFailure() from exc
        if reservations:
            from scripts import budget_ledger
            try:
                budget_ledger.settle_reservations(self.budget_path, reservations)
            except Exception as exc:
                raise BudgetLedgerWriteFailure() from exc
        if budget_denials:
            trace["budget_blocked"] = True
        result["_intent_trace"] = trace
        return result

    def _start_jev_shadow(self, request, event, context):
        if not self.jev_shadow_enabled or not _JEV_SLOTS.acquire(blocking=False):
            return False
        event_hash = hashlib.sha256((request["page_id"] + "\x1f" + request["post"]["id"] +
            "\x1f" + request["thread_id"] + "\x1f" + request["current"]["actor_id"] +
            "\x1f" + request["event_id"]).encode()).hexdigest()

        def run():
            reservations = []
            result = {}
            ledger_recorded = False
            try:
                def guard():
                    from scripts import budget_ledger
                    rid = f"intent-jev:{event_hash}:{uuid.uuid4().hex}"
                    allowed, _ = budget_ledger.reserve(self.jev_budget_path, reservation_id=rid,
                        max_calls=self.jev_max_calls, max_cost=self.jev_max_cost,
                        reserve_cost_usd=self.jev_reserve, prior_calls=self.budget_prior_calls,
                        prior_cost=self.budget_prior_cost)
                    if allowed:
                        reservations.append(rid)
                    return allowed
                questions = intent_questions()
                result = self.jev_adapter.decide({"context": context}, questions,
                    timeout=min(self.model_timeout, 10), budget_guard=guard)
                if not isinstance(result, dict):
                    result = {"ok": False, "error": "invalid Jev adapter response", "attempts": 0}
                answers = {}
                if result.get("ok"):
                    try:
                        answers = validate_answers(result.get("answers"), questions)
                    except (TypeError, ValueError):
                        result["ok"] = False
                        result["error"] = "invalid Jev answer schema"
                attempts = result.get("attempts") if type(result.get("attempts")) is int else 0
                if attempts:
                    from scripts import budget_ledger
                    cost = result.get("cost_usd_est")
                    known = _valid_budget_value(cost) and not result.get("cost_unknown", True)
                    budget_ledger.record(self.jev_budget_path, run_id=f"jev:{event_hash}:{uuid.uuid4().hex}",
                        kind="shadow", attempts=attempts, cost_usd_est=float(cost) if known else None,
                        model=result.get("model_effective", "typesafe/jev-1.14"),
                        status=None if result.get("ok") else "failed")
                    ledger_recorded = True
                trace = {"event_hash": event_hash, "adapter": "jev_decisions", "shadow": True,
                    "model_requested": "typesafe/jev-1.14",
                    "model_effective": result.get("model_effective", "typesafe/jev-1.14"),
                    "provider": result.get("provider"), "schema_version": "jev_intent_decisions_v1",
                    "prompt_version": "jev_intent_questions_v1", "question_version": "jev_intent_questions_v1",
                    "question_keys": sorted(intent_questions()),
                    "decisions": {key: {"type": answer.get("type"), "probability": answer.get("noul")}
                                  for key, answer in answers.items() if isinstance(answer, dict)},
                    "usage": {key: value for key, value in (result.get("usage") or {}).items()
                              if key in {"input_tokens", "output_tokens", "total_tokens"} and type(value) is int},
                    "latency_ms": result.get("latency_ms"),
                    "cost_usd": result.get("cost_usd_est") if not result.get("cost_unknown", True) else None,
                    "cost_unknown": result.get("cost_unknown", True), "attempts": result.get("attempts", 0),
                    "routing": ["SHADOW_COMPLETED" if result.get("ok") else "SHADOW_FAILED"]}
                from scripts.model_trace import append
                append(self.trace_path, trace)
            except Exception:
                pass
            finally:
                if reservations and (not result.get("attempts") or ledger_recorded):
                    try:
                        from scripts import budget_ledger
                        budget_ledger.settle_reservations(self.jev_budget_path, reservations)
                    except Exception:
                        pass
                _JEV_SLOTS.release()
        try:
            _JEV_POOL.submit(run)
            return True
        except RuntimeError:
            _JEV_SLOTS.release()
            return False

    def _prepare_context(self, event, postcard, messages, attempt_token):
        now = datetime.now(timezone.utc).isoformat()
        with self.store.lock:
            self.store.conn.execute("BEGIN IMMEDIATE")
            try:
                self.store.assert_intent_lease(event["event_dedupe_key"], attempt_token)
                self.store.lock_scope(event)
                self.engine._sweep_expired(event, now)
                snapshot = self.engine.context(event, postcard, messages)
                self.store.assert_intent_lease(event["event_dedupe_key"], attempt_token)
                self.store.conn.commit()
                return snapshot
            except Exception:
                self.store.conn.rollback()
                raise

    def get_event(self, event_id):
        if self.store is None:
            return None
        with self.store.lock:
            row = self.store.conn.execute(
                "SELECT status,result,attempts,last_error,created_at,updated_at FROM intent_api_events WHERE event_id=?",
                (event_id,)).fetchone()
        if not row:
            return None
        return {"api_version": API_VERSION, "event_id": event_id, "status": row[0],
                "result": json.loads(row[1]) if row[1] else None, "attempts": row[2],
                "last_error": row[3], "created_at": row[4], "updated_at": row[5]}

    def get_thread(self, page_id, post_id, thread_id, actor_id):
        if self.store is None:
            return None
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT event_id,status,result,created_at FROM intent_api_events WHERE page_id=? AND post_id=? "
                "AND thread_root_id=? AND actor_id=? ORDER BY seq LIMIT 100",
                (page_id, post_id, thread_id, actor_id)).fetchall()
        return {"api_version": API_VERSION, "page_id": page_id, "post_id": post_id,
                "thread_id": thread_id, "actor_id": actor_id,
                "events": [{"event_id": r[0], "status": r[1],
                            "result": json.loads(r[2]) if r[2] else None, "created_at": r[3]} for r in rows]}


def _safe_database_error(exc):
    name = type(exc).__name__
    if name in {"ModuleNotFoundError", "ImportError"}:
        return "PostgreSQL driver is unavailable; install the postgres extra"
    if name == "RuntimeError" and "schema" in str(exc).lower():
        return "PostgreSQL schema is not current; run python -m app.pg_adapter migrate"
    return "PostgreSQL is unavailable or not migrated"


def _env_int(name):
    try:
        value = os.environ.get(name)
        return int(value) if value is not None and value.strip() else None
    except ValueError:
        return None


def _env_float(name):
    try:
        value = os.environ.get(name)
        return float(value) if value is not None and value.strip() else None
    except ValueError:
        return None


def _valid_budget_value(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _positive_env_int(name, default):
    value = _env_int(name)
    return value if value is not None and value > 0 else default


def create_server(service=None, host=None, port=None):
    service = service or IntentService()
    host = host or os.environ.get("INTENT_API_HOST", "127.0.0.1")
    port = int(port if port is not None else os.environ.get("INTENT_API_PORT", "8780"))

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, data):
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if not service.api_key:
                self._send(503, {"api_version": API_VERSION, "status": "NOT_READY",
                                 "reasons": ["INTENT_API_KEY is not configured"]})
                return False
            supplied = self.headers.get("Authorization", "")
            token = supplied[7:] if supplied.startswith("Bearer ") else ""
            if not hmac.compare_digest(token, service.api_key):
                self._send(401, {"api_version": API_VERSION, "status": "UNAUTHORIZED"})
                return False
            return True

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                return self._send(200, service.health())
            if parsed.path == "/ready":
                ready = service.readiness()
                return self._send(200 if ready["ok"] else 503, {"api_version": API_VERSION, **ready})
            if not self._authorized():
                return
            if parsed.path.startswith("/v1/events/"):
                record = service.get_event(unquote(parsed.path.split("/", 3)[3]))
                return self._send(200, record) if record else self._send(404, {"status": "NOT_FOUND"})
            if parsed.path.startswith("/v1/threads/"):
                thread_id = unquote(parsed.path.split("/", 3)[3])
                query = parse_qs(parsed.query)
                values = [query.get(key, [""])[0] for key in ("page_id", "post_id", "actor_id")]
                if not all(values):
                    return self._send(400, {"api_version": API_VERSION, "status": "INVALID_SCOPE",
                                            "error": "page_id, post_id, and actor_id are required"})
                return self._send(200, service.get_thread(values[0], values[1], thread_id, values[2]))
            return self._send(404, {"api_version": API_VERSION, "status": "NOT_FOUND"})

        def do_POST(self):
            if self.path != "/v1/intent":
                return self._send(404, {"api_version": API_VERSION, "status": "NOT_FOUND"})
            if not self._authorized():
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send(400, {"api_version": API_VERSION, "status": "INVALID_LENGTH"})
            if size < 1:
                return self._send(400, {"api_version": API_VERSION, "status": "EMPTY_BODY"})
            if size > MAX_BODY_BYTES:
                return self._send(413, {"api_version": API_VERSION, "status": "PAYLOAD_TOO_LARGE"})
            self.connection.settimeout(5)
            try:
                raw = self.rfile.read(size)
                data = json.loads(raw)
            except (TimeoutError, OSError):
                return self._send(408, {"api_version": API_VERSION, "status": "REQUEST_TIMEOUT"})
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._send(400, {"api_version": API_VERSION, "status": "INVALID_JSON"})
            status, result = service.process(data)
            return self._send(status, result)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.intent_service = service
    return server


def main():
    server = create_server()
    server.serve_forever()


if __name__ == "__main__":
    main()
