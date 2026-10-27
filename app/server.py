"""API/event harness (stdlib http.server, không FastAPI để chạy offline không cần pip).
POST /event  {event, postcard, semantic} -> process result
GET  /ledger -> fake ledger
GET  /health -> ok
Chạy: python3 -m app.server  (port 8765, DB runs/sandbox.db)
"""
from __future__ import annotations
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
from .harness import make_engine
from .engine import WallClock

# R15: server dùng WallClock (giờ thật mỗi lần gọi). Clock đóng băng chỉ dùng
# cho test; server cũ dùng Clock(datetime.now()) nên mọi now() bằng nhau.
ENG = make_engine(db=os.environ.get("SANDBOX_DB_PATH", "./runs/sandbox.db"),
                  clock=WallClock())
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
