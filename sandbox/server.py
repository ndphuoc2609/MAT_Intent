"""Local, live Luna sandbox. Business decisions and simulated sends belong to Engine."""
from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from app.adapters.luna import MODEL, call_luna
from app.engine import WallClock
from app.harness import make_engine
from app.semantics import validate_semantic
from scripts import budget_ledger
from scripts.run_multiturn_eval import fmt_context

STATIC = Path(__file__).with_name("static")
PROVIDER_LOCK = threading.RLock()
CONTEXT_CHAR_LIMIT = 40000
REPAIR_CHAR_LIMIT = 10000
# Conservative bound for the capped prompt and the adapter's current 1,200-token output/rates.
ATTEMPT_COST_RESERVE = 0.05
POSTS = [
    {"id": "creta", "page_id": "sample-hyundai", "title": "Hyundai Creta", "text": "Hyundai Creta: tìm hiểu phiên bản Tiêu chuẩn và Cao cấp tại Hà Nội.", "vehicles": ["Creta"]},
    {"id": "tucson", "page_id": "sample-hyundai", "title": "Hyundai Tucson", "text": "Hyundai Tucson Cao cấp: thông tin xe và lái thử tại Hà Nội.", "vehicles": ["Tucson"]},
    {"id": "compare", "page_id": "sample-hyundai", "title": "Creta và Tucson", "text": "Cùng tìm hiểu Hyundai Creta và Tucson. Bạn quan tâm xe nào?", "vehicles": ["Creta", "Tucson"]},
]


def required_text(data, key, maximum=10000):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} must be non-empty text (maximum {maximum} characters)")
    return value.strip()


class Sandbox:
    def __init__(self, ledger_path=None):
        self.ledger_path = ledger_path or budget_ledger.DEFAULT_PATH
        self.conversations = {}
        self.lock = PROVIDER_LOCK

    def create(self, data):
        actor = required_text(data, "actor_id", 200)
        if data.get("post_id"):
            post = next((dict(p) for p in POSTS if p["id"] == data["post_id"]), None)
            if post is None:
                raise ValueError("unknown seeded post_id")
        else:
            post = dict(id=uuid.uuid4().hex, page_id=required_text(data, "page_id", 200),
                        text=required_text(data, "post_text"), title="Manual post")
            vehicles = data.get("vehicles", [])
            if not isinstance(vehicles, list) or len(vehicles) > 20 or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 100 for v in vehicles):
                raise ValueError("vehicles must be a list of vehicle names")
            post["vehicles"] = [v.strip() for v in vehicles]
        cid = uuid.uuid4().hex
        engine = make_engine(clock=WallClock())
        conversation = dict(id=cid, page_id=post["page_id"], actor_id=actor, thread_id="root-" + cid,
                            post=post, engine=engine, history=[], counter=0)
        conversation["postcard"] = dict(topic="vehicle", vehicles=post["vehicles"])
        root = self.event(conversation, post["text"], conversation["thread_id"])
        root.update(actor_id="page-self:" + cid, actor_role="page_self", parent_id=None)
        engine.process(root, conversation["postcard"], None)
        self.conversations[cid] = conversation
        return self.state(conversation)

    @staticmethod
    def event(conversation, text="", comment_id=None):
        return dict(page_id=conversation["page_id"], actor_id=conversation["actor_id"],
                    post_id=conversation["post"]["id"], thread_root_id=conversation["thread_id"],
                    parent_id=conversation["thread_id"], comment_id=comment_id or "preview",
                    actor_role="user", event_type="create", content_version=1, text=text)

    def state(self, conversation):
        engine = conversation["engine"]
        context = engine.context(self.event(conversation), conversation["postcard"])
        with engine.store.lock:
            outbox = [dict(dedupe_key=r[0], purpose=r[1], status=r[2]) for r in
                      engine.store.conn.execute("SELECT dedupe_key,purpose,status FROM outbox ORDER BY rowid")]
        return dict(id=conversation["id"], page_id=conversation["page_id"], actor_id=conversation["actor_id"],
                    thread_id=conversation["thread_id"], post=conversation["post"], history=conversation["history"],
                    active_requests=context["active_requests"], pending=context["pending"],
                    outbox=outbox, fake_sends=engine.sender.sent, sample_facts=True, simulated_send=True)

    def budget(self, inflight=0):
        return budget_ledger.guard(self.ledger_path, max_calls=300, max_cost=2.00,
                                   prior_calls=276, prior_cost=.0854,
                                   est_attempts=inflight + 1, est_cost=ATTEMPT_COST_RESERVE * (inflight + 1))

    @staticmethod
    def prior_customer_turns(conversation, text, existing=()):
        existing = set(existing)
        return list(dict.fromkeys(turn["text"] for turn in conversation["history"]
                                  if isinstance(turn.get("text"), str) and turn["text"].strip()
                                  and turn["text"] != text and turn["text"] not in existing))[-5:]

    def model(self, conversation, text, context, repair_note=""):
        model_context = fmt_context(context)
        existing = [row["text"] for row in context["recent"]] + context["parent_texts"]
        customer_turns = self.prior_customer_turns(conversation, text, existing)
        if customer_turns:
            model_context += "\n\nPrior customer turns:\n" + "\n".join(customer_turns)
        replies = [turn["response_text"] for turn in conversation["history"]
                   if turn.get("outbound_permission") is True and
                   any(send.get("status") == "sent_to_fake_ledger" for send in turn.get("sends", [])
                       if isinstance(send, dict)) and
                   isinstance(turn.get("response_text"), str)][-5:]
        if replies:
            model_context += "\n\nPrior bot replies (conversational context, not verified facts):\n" + "\n".join(replies)
        if len(model_context) > CONTEXT_CHAR_LIMIT:
            model_context = "[older conversation context truncated]\n" + model_context[-CONTEXT_CHAR_LIMIT:]
        inflight = 0
        def guard():
            nonlocal inflight
            allowed, _ = self.budget(inflight)
            if allowed:
                inflight += 1
            return allowed
        result = call_luna(conversation["post"]["text"], text,
                           parent="\n".join(context["parent_texts"])[-20000:], context=model_context,
                           repair_note=repair_note[-REPAIR_CHAR_LIMIT:], budget_guard=guard)
        attempts = result.get("attempts", 0)
        if attempts:
            cost = result.get("cost_usd_est") if not result.get("cost_unknown", True) else None
            # Retries before a successful response have no reported usage.
            if result.get("ok") and attempts > 1:
                retry_unknown = result.get("retry_cost_unknown", True)
                budget_ledger.record(self.ledger_path, run_id=conversation["id"], kind="retry",
                                     attempts=attempts - 1,
                                     cost_usd_est=None if retry_unknown else result.get("retry_cost_usd_est"),
                                     model=MODEL, status="failed")
                attempts = 1
            budget_ledger.record(self.ledger_path, run_id=conversation["id"],
                                 kind="repair" if repair_note else "call", attempts=attempts,
                                 cost_usd_est=cost, model=result.get("model_effective", MODEL),
                                 status=None if result.get("ok") else "failed")
        return result

    def turn(self, conversation, data):
        text = required_text(data, "text")
        conversation["counter"] += 1
        event = self.event(conversation, text, f"{conversation['id']}-{conversation['counter']}")
        engine = conversation["engine"]
        context = engine.context(event, conversation["postcard"])
        engine_processed = False
        turn = dict(text=text, ok=False, semantic=None, routing=[], response_text=None,
                    sends=[], errors=[], attempts=0, cost_usd_est=0.0, cost_unknown=False)
        allowed, reason = self.budget()
        if not allowed:
            turn.update(error=reason, routing=["BLOCKED_BUDGET"])
        else:
            for iteration in range(2):
                if iteration:
                    allowed, reason = self.budget()
                    if not allowed:
                        turn.update(error=reason, routing=["BLOCKED_BUDGET"])
                        break
                api = self.model(conversation, text, context, "; ".join(turn["errors"]) if iteration else "")
                attempts = api.get("attempts", 0)
                turn["attempts"] += attempts
                if attempts:
                    unknown = api.get("cost_unknown", True) or (api.get("ok") and attempts > 1)
                    turn["cost_unknown"] |= unknown
                    turn["cost_usd_est"] = (None if turn["cost_unknown"] else
                                             turn["cost_usd_est"] + api.get("cost_usd_est", 0))
                if not api.get("ok"):
                    turn.update(error=api.get("error") or "Model request failed", routing=["REVIEW_MODEL_ERROR"])
                    break
                turn["semantic"] = api.get("semantic")
                turn["errors"] = validate_semantic(turn["semantic"], text,
                    [r["text"] for r in context["recent"]] + context["parent_texts"] +
                    self.prior_customer_turns(conversation, text,
                        [r["text"] for r in context["recent"]] + context["parent_texts"]))
                if turn["errors"]:
                    turn.update(error="Model semantics failed validation", routing=["REVIEW_SCHEMA"])
                    continue
                result = engine.process(event, conversation["postcard"], turn["semantic"])
                engine_processed = not any(route.startswith("ERROR_") for route in result["routing"])
                turn.update({k: result[k] for k in ("routing", "response_text", "sends", "errors", "outbound_permission", "state_changes")})
                turn["ok"] = not any(route.startswith(("REVIEW", "BLOCKED", "ERROR_")) for route in turn["routing"])
                turn.pop("error", None)
                break
        if not engine_processed:
            engine.process(event, conversation["postcard"], None)
        conversation["history"].append(turn)
        return dict(self.state(conversation), turn=turn)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def local_host(self):
        try:
            host = urlsplit("//" + self.headers.get("Host", "").strip())
        except ValueError:
            return False
        name = (host.hostname or "").rstrip(".").lower()
        if not host.netloc or host.path or host.query or host.fragment or host.username or host.password:
            return False
        if name == "localhost" or name.endswith(".localhost"):
            return True
        try:
            return ipaddress.ip_address(name).is_loopback
        except ValueError:
            return False

    def reply(self, status, body, content_type="application/json; charset=utf-8"):
        payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if not self.local_host():
            self.reply(403, dict(error="Local host required; open the app with localhost or 127.0.0.1"))
            return
        path = urlsplit(self.path).path
        if path == "/health":
            self.reply(200, dict(ok=True, model=MODEL, configured=bool(os.environ.get("OPENROUTER_API_KEY", "").strip()), simulated_send=True))
        elif path == "/api/posts":
            self.reply(200, dict(posts=POSTS, sample_facts=True))
        elif path in ("/", "/static/app.js", "/static/style.css"):
            name = {"/": "index.html", "/static/app.js": "app.js", "/static/style.css": "style.css"}[path]
            mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
            self.reply(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")
        elif path.startswith("/api/conversations/"):
            cid = path.removeprefix("/api/conversations/")
            with self.server.app.lock:
                conversation = self.server.app.conversations.get(cid)
                self.reply(200, self.server.app.state(conversation)) if conversation else self.reply(404, dict(error="Conversation not found"))
        else:
            self.reply(404, dict(error="Not found"))

    def do_POST(self):
        try:
            if not self.local_host():
                self.reply(403, dict(error="Local host required; open the app with localhost or 127.0.0.1"))
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValueError("Content-Type must be application/json")
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self.reply(403, dict(error="Cross-origin requests refused"))
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("JSON body must be between 1 and 65536 bytes")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            path = urlsplit(self.path).path
            with self.server.app.lock:
                if path == "/api/conversations":
                    self.reply(201, self.server.app.create(data))
                    return
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "turn":
                    conversation = self.server.app.conversations.get(parts[2])
                    if conversation:
                        self.reply(200, self.server.app.turn(conversation, data))
                        return
                self.reply(404, dict(error="Conversation not found"))
        except (ValueError, UnicodeError, RecursionError) as exc:
            self.reply(400, dict(error=str(exc)))
        except Exception:
            self.reply(500, dict(error="Sandbox request failed; no automatic retry. Inspect server setup."))


def create_server(host="127.0.0.1", port=8766, ledger_path=None):
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = Sandbox(ledger_path)
    return server


def main():
    server = create_server(os.environ.get("SANDBOX_HOST", "127.0.0.1"), int(os.environ.get("SANDBOX_PORT", "8766")))
    print(f"Live Luna sandbox: http://{server.server_address[0]}:{server.server_address[1]} (sample facts, FakeSender only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
