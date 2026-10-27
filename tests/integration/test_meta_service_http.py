import hashlib
import hmac
import io
import json
import tempfile
import unittest
import urllib.parse
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

from meta_services.runtime import MetaRuntime
from meta_services.graph import MetaGraphError
from meta_services.server import MetaRequestHandler


class FakeGraph:
    page_id = "123"

    def __init__(self):
        self.replies = []
        self.posts = []
        self.comment_reads = 0

    def list_posts(self, limit=25, after=None):
        return {"data": [{"id": "123_456", "message": "Creta"}], "paging": {}}

    def list_comments(self, post_id, limit=25, after=None):
        return {"data": [{"id": "456_789", "message": "Giá?"}], "paging": {}}

    def get_post(self, post_id):
        return {"id": post_id, "message": "Creta", "from": {"id": "123"}}

    def get_comment(self, comment_id):
        self.comment_reads += 1
        if comment_id == "999_999":
            raise MetaGraphError("not found", status=404)
        return {"id": comment_id, "message": "Giá?", "from": {"id": "999"},
                "object": {"id": "123_456", "from": {"id": "123"}}}

    def reply_to_comment(self, comment_id, message):
        self.replies.append((comment_id, message))
        return {"id": f"reply_{len(self.replies)}"}

    def publish_post(self, message, link=None):
        self.posts.append((message, link))
        return {"id": f"123_{len(self.posts)}"}


class MetaServiceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.graph = FakeGraph()
        self.runtime = MetaRuntime(
            self.graph, app_secret="app-secret", verify_token="verify-token",
            service_api_key="private-api-key", outbound_enabled=True,
            db_path=Path(self.temp.name) / "meta.db",
            budget_path=Path(self.temp.name) / "budget.jsonl",
            trace_path=Path(self.temp.name) / "trace.jsonl",
            model_call=lambda *args, **kwargs: {"ok": False, "attempts": 0},
        )
        self.addCleanup(self._close)

    def _close(self):
        self.runtime.close()

    def request(self, path, *, method="GET", body=None, auth=True, headers=None):
        request_headers = dict(headers or {})
        if auth:
            request_headers["Authorization"] = "Bearer private-api-key"
        data = b"" if body is None else json.dumps(body).encode()
        if method == "POST":
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(data))
        message = Message()
        for key, value in request_headers.items():
            message[key] = value
        handler = object.__new__(MetaRequestHandler)
        handler.server = SimpleNamespace(runtime=self.runtime)
        handler.path = path
        handler.command = method
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.headers = message
        handler.rfile = io.BytesIO(data)
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 0)
        handler.close_connection = True
        handler._headers_buffer = []
        getattr(handler, f"do_{method}")()
        response = handler.wfile.getvalue()
        response_headers, content = response.split(b"\r\n\r\n", 1)
        lines = response_headers.split(b"\r\n")
        status = int(lines[0].split()[1])
        self.last_response_headers = {
            name.decode().lower(): value.decode()
            for line in lines[1:]
            if b":" in line
            for name, value in [line.split(b":", 1)]
        }
        return status, content

    def test_openapi_docs_and_local_swagger_assets_are_available(self):
        status, body = self.request("/openapi.json", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("application/json", self.last_response_headers["content-type"])
        spec = json.loads(body)
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertIn("/health", spec["paths"])
        self.assertIn("/webhooks/meta", spec["paths"])
        self.assertIn("/api/posts", spec["paths"])
        self.assertIn("/api/posts/{post_id}/comments", spec["paths"])
        self.assertIn("/api/comments/{comment_id}/reply", spec["paths"])
        self.assertIn("/api/inbox", spec["paths"])
        self.assertIn("/api/reviews", spec["paths"])
        self.assertNotIn("security", spec["paths"]["/health"]["get"])
        self.assertIn("security", spec["paths"]["/api/posts"]["get"])
        self.assertIn("400", spec["paths"]["/api/comments/{comment_id}"]["get"]["responses"])
        self.assertEqual(spec["components"]["securitySchemes"]["BearerAuth"]["scheme"], "bearer")
        self.assertIn("string", spec["components"]["schemas"]["Error"]["properties"]["status"]["type"])

        status, body = self.request("/docs", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("text/html", self.last_response_headers["content-type"])
        self.assertIn(b"swagger-ui", body)
        for path, expected_type in (
            ("/static/swagger-ui.css", "text/css"),
            ("/static/swagger-ui-bundle.js", "javascript"),
            ("/static/swagger-initializer.js", "javascript"),
        ):
            status, body = self.request(path, auth=False)
            self.assertEqual(status, 200, path)
            self.assertIn(expected_type, self.last_response_headers["content-type"])
            self.assertTrue(body)

        _, initializer = self.request("/static/swagger-initializer.js", auth=False)
        self.assertIn(b"/openapi.json", initializer)

        status, _ = self.request("/static/../.env", auth=False)
        self.assertEqual(status, 404)

    def test_admin_page_and_assets_are_local_and_leave_api_protected(self):
        status, body = self.request("/admin", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("text/html", self.last_response_headers["content-type"])
        self.assertLess(body.index(b'id="notice"'), body.index(b'id="dashboard"'))
        self.assertIn(b"/static/admin.js", body)
        self.assertIn(b"/static/admin.css", body)

        status, script = self.request("/static/admin.js", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("javascript", self.last_response_headers["content-type"])
        self.assertIn(b"/api/posts", script)
        self.assertIn(b"/api/inbox", script)
        self.assertIn(b"/api/reviews", script)
        self.assertIn(b"textContent", script)
        self.assertNotIn(b"innerHTML", script)
        self.assertNotIn(b"localStorage", script)
        self.assertNotIn(b"sessionStorage", script)

        status, _ = self.request("/static/admin.css", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("text/css", self.last_response_headers["content-type"])
        status, _ = self.request("/api/inbox?limit=1", auth=False)
        self.assertEqual(status, 401)

    def test_disabled_outbound_blocks_reply_and_publish_without_graph_writes(self):
        self.runtime.outbound_enabled = False
        headers = {"Idempotency-Key": "reply-disabled-1"}
        status, body = self.request("/api/comments/456_789/reply", method="POST",
                                    body={"message": "Trả lời thử"}, headers=headers)
        self.assertEqual(status, 423)
        self.assertEqual(json.loads(body)["error"], "outbound_disabled")
        headers = {"Idempotency-Key": "post-disabled-1"}
        status, body = self.request("/api/posts", method="POST",
                                    body={"message": "Bài thử"}, headers=headers)
        self.assertEqual(status, 423)
        self.assertEqual(json.loads(body)["error"], "outbound_disabled")
        self.assertEqual(self.graph.replies, [])
        self.assertEqual(self.graph.posts, [])

    def test_health_is_public_but_comment_api_requires_service_key(self):
        status, body = self.request("/health", auth=False)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["outbound_enabled"] is False)
        status, _ = self.request("/api/posts", auth=False)
        self.assertEqual(status, 401)
        status, body = self.request("/api/posts")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"][0]["id"], "123_456")

    def test_webhook_handshake_and_signature_are_enforced(self):
        query = urllib.parse.urlencode({"hub.mode": "subscribe", "hub.verify_token": "verify-token",
                                        "hub.challenge": "challenge-123"})
        status, body = self.request("/webhooks/meta?" + query, auth=False)
        self.assertEqual((status, body), (200, b"challenge-123"))
        payload = {"object": "page", "entry": [{"id": "123", "changes": [{
            "field": "feed", "value": {"item": "comment", "verb": "add",
                "comment_id": "456_789", "post_id": "123_456", "parent_id": "123_456",
                "from": {"id": "999"}, "message": "Giá?"}
        }]}]}
        raw = json.dumps(payload).encode()
        signature = hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
        status, body = self.request("/webhooks/meta", method="POST", auth=False, body=payload,
                                    headers={"X-Hub-Signature-256": "sha256=" + signature})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["queued"], 1)
        status, _ = self.request("/webhooks/meta", method="POST", auth=False, body=payload,
                                 headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
        self.assertEqual(status, 403)

    def test_manual_write_requires_idempotency_and_never_duplicates(self):
        headers = {"Idempotency-Key": "reply-123456"}
        body = {"message": "Mình đã nhận thông tin."}
        status, first = self.request("/api/comments/456_789/reply", method="POST", body=body, headers=headers)
        self.assertEqual(status, 200)
        status, second = self.request("/api/comments/456_789/reply", method="POST", body=body, headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(second)["duplicate"])
        self.assertEqual(self.graph.comment_reads, 1)
        self.assertEqual(len(self.graph.replies), 1)

    def test_page_post_requires_idempotency_key(self):
        status, _ = self.request("/api/posts", method="POST", body={"message": "Bài viết"})
        self.assertEqual(status, 400)
        status, body = self.request("/api/posts", method="POST", body={"message": "Bài viết"},
                                    headers={"Idempotency-Key": "post-123456"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], "123_1")
        self.assertEqual(self.graph.posts, [("Bài viết", None)])

    def test_comment_lookup_error_returns_sanitized_http_response(self):
        status, body = self.request("/api/comments/999_999/reply", method="POST",
                                    body={"message": "Trả lời"},
                                    headers={"Idempotency-Key": "reply-errors-1"})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body)["error"], "meta_graph_error")
        self.assertEqual(len(self.graph.replies), 0)


if __name__ == "__main__":
    unittest.main()
