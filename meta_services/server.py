"""HTTP API + signed Meta Page webhook for the `meta-services` sidecar."""
from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .graph import MetaGraphError
from .openapi import OPENAPI_DOCUMENT
from .runtime import MetaRuntime
from .webhook import verify_signature


MAX_BODY = 1_048_576
STATIC_DIR = Path(__file__).with_name("static")
STATIC_ASSETS = {
    "/docs": ("docs.html", "text/html; charset=utf-8"),
    "/admin": ("admin.html", "text/html; charset=utf-8"),
    "/static/swagger-ui-bundle.js": ("swagger-ui-bundle.js", "application/javascript; charset=utf-8"),
    "/static/swagger-ui.css": ("swagger-ui.css", "text/css; charset=utf-8"),
    "/static/swagger-initializer.js": ("swagger-initializer.js", "application/javascript; charset=utf-8"),
    "/static/admin.js": ("admin.js", "application/javascript; charset=utf-8"),
    "/static/admin.css": ("admin.css", "text/css; charset=utf-8"),
}


class ResourceNotFound(Exception):
    pass


class MetaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, runtime):
        self.runtime = runtime
        super().__init__(address, MetaRequestHandler)


class MetaRequestHandler(BaseHTTPRequestHandler):
    server_version = "MetaServices/1.0"
    sys_version = ""

    @property
    def runtime(self):
        return self.server.runtime

    def log_message(self, format, *args):
        # Request paths and query strings can contain customer identifiers.
        return

    def _reply(self, status, body=b"", content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("chunked request bodies are not accepted")
        length = self.headers.get("Content-Length")
        if not length or not length.isdigit():
            raise ValueError("Content-Length is required")
        size = int(length)
        if size > MAX_BODY:
            raise OverflowError("request body too large")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError("incomplete request body")
        return raw

    def _json_body(self):
        raw = self._body()
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("request body must be valid JSON") from None
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return raw, value

    def _authorized(self):
        key = self.runtime.service_api_key
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if not key or not token or not hmac.compare_digest(token, key):
            self._reply(401, {"error": "unauthorized"})
            return False
        return True

    def _outbound_allowed(self):
        if not self.runtime.outbound_enabled:
            self._reply(423, {"error": "outbound_disabled"})
            return False
        return True

    def _graph_error(self, error):
        body = {"error": "meta_graph_error", "status": error.status, "code": error.code}
        if error.outcome_unknown:
            body["outcome_unknown"] = True
        self._reply(502, body)

    def _post_owned_by_page(self, post_id):
        post = self.runtime.graph.get_post(post_id)
        owner = post.get("from") or {}
        expected_owner = owner.get("id")
        return (post.get("id") == post_id and
                (expected_owner == self.runtime.page_id if expected_owner
                 else post_id.startswith(self.runtime.page_id + "_")))

    def _comment_and_post(self, comment_id):
        comment = self.runtime.graph.get_comment(comment_id)
        if comment.get("id") != comment_id:
            return None
        obj = comment.get("object") or {}
        post_id = obj.get("id")
        if not isinstance(post_id, str) or not post_id.startswith(self.runtime.page_id + "_"):
            return None
        if not self._post_owned_by_page(post_id):
            return None
        return comment, post_id

    def _manual_write(self, key, operation, target, payload, action):
        try:
            claim = self.runtime.claim_manual_write(key, operation, target, payload)
        except ValueError as error:
            self._reply(400, {"error": str(error)})
            return
        if not claim["claimed"]:
            if claim.get("status") == "SENT":
                result = dict(claim["result"])
                result["duplicate"] = True
                self._reply(200, result)
            else:
                self._reply(409, {"error": "idempotency_key_already_used",
                                  "status": claim.get("status", "UNKNOWN")})
            return
        try:
            result = action()
            self.runtime.finish_manual_write(key, "SENT", result)
            self._reply(200, result)
        except MetaGraphError as error:
            status = "UNKNOWN" if error.outcome_unknown else "FAILED"
            try:
                self.runtime.finish_manual_write(key, status)
            except Exception:
                pass
            self._graph_error(error)
        except ResourceNotFound:
            try:
                self.runtime.finish_manual_write(key, "FAILED")
            except Exception:
                pass
            self._reply(404, {"error": "comment_not_found"})
        except ValueError as error:
            try:
                self.runtime.finish_manual_write(key, "FAILED")
            except Exception:
                pass
            self._reply(400, {"error": str(error)})
        except Exception:
            try:
                self.runtime.finish_manual_write(key, "UNKNOWN")
            except Exception:
                pass
            self._reply(502, {"error": "write_outcome_unknown", "outcome_unknown": True})

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/openapi.json":
            self._reply(200, OPENAPI_DOCUMENT)
            return
        static = STATIC_ASSETS.get(url.path)
        if static:
            filename, content_type = static
            try:
                body = (STATIC_DIR / filename).read_bytes()
            except OSError:
                self._reply(500, {"error": "static_asset_unavailable"})
                return
            self._reply(200, body, content_type)
            return
        if url.path == "/health":
            with self.runtime.store.lock:
                pending = self.runtime.store.conn.execute(
                    "SELECT COUNT(*) FROM meta_inbox WHERE status='PENDING'").fetchone()[0]
                failed = self.runtime.store.conn.execute(
                    "SELECT COUNT(*) FROM meta_inbox WHERE status='FAILED'").fetchone()[0]
            self._reply(200, {"ok": True, "page_configured": bool(self.runtime.page_id),
                              "openrouter_configured": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
                              "outbound_enabled": self.runtime.outbound_enabled,
                              "pending_comments": pending, "failed_comments": failed})
            return
        if url.path == "/webhooks/meta":
            query = parse_qs(url.query, keep_blank_values=True)
            mode = query.get("hub.mode", [""])[0]
            token = query.get("hub.verify_token", [""])[0]
            challenge = query.get("hub.challenge", [""])[0]
            if (mode == "subscribe" and self.runtime.verify_token and challenge
                    and hmac.compare_digest(token, self.runtime.verify_token)):
                self._reply(200, challenge.encode(), "text/plain; charset=utf-8")
            else:
                self._reply(403, {"error": "webhook_verification_failed"})
            return
        if not url.path.startswith("/api/"):
            self._reply(404, {"error": "not_found"})
            return
        if not self._authorized():
            return
        parts = [unquote(part) for part in url.path.split("/") if part]
        query = parse_qs(url.query, keep_blank_values=True)
        try:
            if parts == ["api", "posts"]:
                limit = int(query.get("limit", ["25"])[0])
                after = query.get("after", [None])[0]
                self._reply(200, self.runtime.graph.list_posts(limit, after))
                return
            if len(parts) == 4 and parts[:2] == ["api", "posts"] and parts[3] == "comments":
                if not self._post_owned_by_page(parts[2]):
                    self._reply(404, {"error": "post_not_found"})
                    return
                limit = int(query.get("limit", ["25"])[0])
                after = query.get("after", [None])[0]
                self._reply(200, self.runtime.graph.list_comments(parts[2], limit, after))
                return
            if len(parts) == 3 and parts[:2] == ["api", "comments"]:
                found = self._comment_and_post(parts[2])
                if not found:
                    self._reply(404, {"error": "comment_not_found"})
                    return
                comment, post_id = found
                self._reply(200, {**comment, "post_id": post_id})
                return
            if parts == ["api", "reviews"]:
                limit = int(query.get("limit", ["50"])[0])
                if not 1 <= limit <= 100:
                    raise ValueError("limit must be between 1 and 100")
                self._reply(200, {"data": self.runtime.list_reviews(limit)})
                return
            if parts == ["api", "inbox"]:
                limit = int(query.get("limit", ["50"])[0])
                self._reply(200, {"data": self.runtime.list_inbox(limit)})
                return
            self._reply(404, {"error": "not_found"})
        except MetaGraphError as error:
            self._graph_error(error)
        except (ValueError, TypeError) as error:
            self._reply(400, {"error": str(error)})

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path == "/webhooks/meta":
            try:
                raw = self._body()
            except OverflowError:
                self._reply(413, {"error": "request_too_large"})
                return
            except ValueError as error:
                self._reply(400, {"error": str(error)})
                return
            signature = self.headers.get("X-Hub-Signature-256", "")
            if not verify_signature(raw, signature, self.runtime.app_secret):
                self._reply(403, {"error": "invalid_webhook_signature"})
                return
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._reply(400, {"error": "invalid_json"})
                return
            queued = self.runtime.enqueue_webhook(payload)
            self._reply(200, {"ok": True, "queued": queued,
                              "outbound_enabled": self.runtime.outbound_enabled})
            return
        if not url.path.startswith("/api/"):
            self._reply(404, {"error": "not_found"})
            return
        if not self._authorized() or not self._outbound_allowed():
            return
        try:
            _, body = self._json_body()
        except OverflowError:
            self._reply(413, {"error": "request_too_large"})
            return
        except ValueError as error:
            self._reply(400, {"error": str(error)})
            return

        parts = [unquote(part) for part in url.path.split("/") if part]
        key = self.headers.get("Idempotency-Key", "")
        if len(parts) == 4 and parts[:2] == ["api", "comments"] and parts[3] == "reply":
            comment_id = parts[2]
            message = body.get("message")
            if not isinstance(message, str) or not message.strip() or "\x00" in message or len(message) > 10000:
                self._reply(400, {"error": "message must contain 1-10000 characters"})
                return

            def reply():
                if not self._comment_and_post(comment_id):
                    raise ResourceNotFound()
                return self.runtime.graph.reply_to_comment(comment_id, message)

            self._manual_write(key, "reply", comment_id, {"message": message},
                               reply)
            return
        if parts == ["api", "posts"]:
            message, link = body.get("message"), body.get("link")
            if not isinstance(message, str) or not message.strip() or "\x00" in message or len(message) > 63000:
                self._reply(400, {"error": "message must contain 1-63000 characters"})
                return
            if link is not None:
                try:
                    parsed = urlsplit(link) if isinstance(link, str) else None
                    valid_link = (parsed is not None and parsed.scheme == "https" and bool(parsed.hostname)
                                  and not parsed.username and not parsed.password
                                  and not any(c.isspace() or ord(c) < 32 for c in link))
                except ValueError:
                    valid_link = False
                if not valid_link:
                    self._reply(400, {"error": "link must be an HTTPS URL without credentials"})
                    return
            self._manual_write(key, "publish", self.runtime.page_id,
                               {"message": message, "link": link},
                               lambda: self.runtime.graph.publish_post(message, link))
            return
        self._reply(404, {"error": "not_found"})


def create_server(runtime, host="127.0.0.1", port=8767):
    return MetaHTTPServer((host, port), runtime)


def main():
    runtime = MetaRuntime.from_env()
    host = os.environ.get("META_SERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("META_SERVICE_PORT", "8767"))
    server = create_server(runtime, host, port)
    runtime.start_worker()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.close()


if __name__ == "__main__":
    main()
