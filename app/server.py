"""API/event harness backed by the configured PostgreSQL database.
POST /event  {event, postcard, semantic} -> process result
GET  /ledger -> fake ledger
GET  /health -> ok
Chạy: MAT_INTENT_DATABASE_URL=<PostgreSQL URL> python3 -m app.server
"""
from __future__ import annotations
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
from .harness import make_engine
from .engine import WallClock
from .pg_adapter import connect

database_url = os.environ.get("MAT_INTENT_DATABASE_URL") or os.environ.get("DB_NEON")
if not database_url:
    raise RuntimeError("MAT_INTENT_DATABASE_URL or DB_NEON is required")
ENG = make_engine(store=connect(database_url), clock=WallClock())
# A5: server restart phải drain intent tồn đọng (INTENDED/CLAIMED hết lease)
# từ lần chạy trước thay vì để chúng kẹt cho tới khi có event trùng lặp.
try:
    _drained = ENG.drain_intended()
    if _drained:
        print(f"server recovery: drained {_drained}")
except Exception as _e:  # recovery không được chặn server khởi động
    print(f"server recovery: drain failed ({_e})")

class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        if self.path != "/event": return self._send({"error": "not found"}, 404)
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0))
        res = ENG.process(data["event"], data["postcard"], data.get("semantic"))
        self._send({k: v for k, v in res.items() if k != "audit"})
    def do_GET(self):
        if self.path == "/ledger": return self._send(ENG.store.ledger())
        if self.path == "/health": return self._send({"ok": True, "sender": "fake-only"})
        return self._send({"error": "not found"}, 404)
    def log_message(self, *a): pass

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8765), H).serve_forever()
