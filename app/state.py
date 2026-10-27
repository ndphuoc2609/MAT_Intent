"""State store SQLite (process riêng). Transaction: ingest+state+outbox+audit
trong một BEGIN IMMEDIATE; optimistic version check trong cùng transaction.
PostgreSQL semantics (SERIALIZABLE/row-lock) KHÔNG được kiểm chứng ở đây —
compose.yaml là bản chạy PG thật, cần chạy lại suite trên PG trước khi kết luận.
"""
from __future__ import annotations
import json, sqlite3, threading, uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(dedupe_key TEXT PRIMARY KEY, comment_id TEXT,
  actor_id TEXT, text TEXT, payload TEXT, retracted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, page_id TEXT, actor_id TEXT,
  topic TEXT, status TEXT, version INTEGER, slots TEXT, source_events TEXT,
  desired_outcomes TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY, request_id TEXT, actor_id TEXT,
  qtype TEXT, expected_slot TEXT, options TEXT, proposition TEXT,
  issued_at TEXT, expires_at TEXT, version INTEGER, status TEXT,
  basis_version INTEGER, basis_slots TEXT);
CREATE TABLE IF NOT EXISTS outbox(dedupe_key TEXT PRIMARY KEY, request_id TEXT,
  purpose TEXT, version INTEGER, status TEXT, payload TEXT, created_at TEXT,
  claimed_at TEXT, claimed_by TEXT);
CREATE TABLE IF NOT EXISTS proposal_rejections(request_id TEXT, fingerprint TEXT,
  created_at TEXT, PRIMARY KEY(request_id, fingerprint));
CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY, comment_id TEXT, kind TEXT,
  payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS contact_prefs(page_id TEXT, actor_id TEXT, scope TEXT,
  PRIMARY KEY(page_id, actor_id));
CREATE TABLE IF NOT EXISTS contact_blocks(page_id TEXT, actor_id TEXT, channel TEXT,
  PRIMARY KEY(page_id, actor_id, channel));
CREATE TABLE IF NOT EXISTS handoffs(request_id TEXT PRIMARY KEY, owner TEXT, reason TEXT,
  lock_version INTEGER, acquired_at TEXT, released_at TEXT, overdue INTEGER DEFAULT 0);
"""

class Store:
    def __init__(self, path=":memory:"):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA); self.conn.commit()
        # Migration cho DB file đã tồn tại từ trước (CREATE TABLE IF NOT EXISTS
        # không thêm cột): handoffs.overdue, pending.basis_*, outbox.claimed_*.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(handoffs)").fetchall()}
        if "overdue" not in cols:
            self.conn.execute("ALTER TABLE handoffs ADD COLUMN overdue INTEGER DEFAULT 0")
        pcols = {r[1] for r in self.conn.execute("PRAGMA table_info(pending)").fetchall()}
        if "basis_version" not in pcols:
            self.conn.execute("ALTER TABLE pending ADD COLUMN basis_version INTEGER")
        if "basis_slots" not in pcols:
            self.conn.execute("ALTER TABLE pending ADD COLUMN basis_slots TEXT")
        ocols = {r[1] for r in self.conn.execute("PRAGMA table_info(outbox)").fetchall()}
        if "claimed_at" not in ocols:
            self.conn.execute("ALTER TABLE outbox ADD COLUMN claimed_at TEXT")
        if "claimed_by" not in ocols:
            self.conn.execute("ALTER TABLE outbox ADD COLUMN claimed_by TEXT")
        self.conn.commit()
    # -- helpers trong lock, dùng transaction của caller
    def get_request(self, rid):
        r = self.conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r: return None
        return dict(id=r[0], page_id=r[1], actor_id=r[2], topic=r[3], status=r[4],
                    version=r[5], slots=json.loads(r[6]), source_events=json.loads(r[7]),
                    desired_outcomes=json.loads(r[8]))
    def active_requests(self, page_id, actor_id):
        rows = self.conn.execute(
            "SELECT * FROM requests WHERE page_id=? AND actor_id=? AND status IN ('OPEN','WAITING_USER','READY') ORDER BY rowid DESC",
            (page_id, actor_id)).fetchall()
        out = []
        for r in rows:
            out.append(dict(id=r[0], topic=r[3], status=r[4], version=r[5],
                            slots=json.loads(r[6]), desired_outcomes=json.loads(r[8])))
        return out
    def save_request(self, req):
        self.conn.execute(
            "UPDATE requests SET status=?, version=?, slots=?, source_events=?, desired_outcomes=?, updated_at=? WHERE id=? AND version=?",
            (req["status"], req["version"], json.dumps(req["slots"], ensure_ascii=False),
             json.dumps(req["source_events"], ensure_ascii=False),
             json.dumps(req["desired_outcomes"], ensure_ascii=False),
             req.get("updated_at", ""), req["id"], req["version"] - 1))
        if self.conn.execute("SELECT changes()").fetchone()[0] == 0:
            raise RuntimeError(f"state version conflict for request {req['id']}")
    def insert_request(self, req):
        self.conn.execute(
            "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (req["id"], req["page_id"], req["actor_id"], req["topic"], req["status"],
             req["version"], json.dumps(req["slots"], ensure_ascii=False),
             json.dumps(req["source_events"], ensure_ascii=False),
             json.dumps(req["desired_outcomes"], ensure_ascii=False),
             req.get("created_at", ""), req.get("updated_at", "")))
    def get_pending(self, pid):
        r = self.conn.execute("SELECT * FROM pending WHERE id=?", (pid,)).fetchone()
        if not r: return None
        basis_slots = {}
        if len(r) > 12 and r[12]:
            try: basis_slots = json.loads(r[12])
            except Exception: basis_slots = {}
        return dict(id=r[0], request_id=r[1], actor_id=r[2], qtype=r[3], expected_slot=r[4],
                    options=json.loads(r[5]), proposition=r[6], issued_at=r[7],
                    expires_at=r[8], version=r[9], status=r[10],
                    basis_version=r[11] if len(r) > 11 else None, basis_slots=basis_slots)
    def open_pending_for(self, actor_id, request_id=None, page_id=None):
        # A3: khi có page_id, lọc pending qua request của đúng Page đó (join),
        # không chỉ theo actor — sweep/context không được chạm scope Page khác.
        if page_id is None:
            rows = self.conn.execute(
                "SELECT * FROM pending WHERE actor_id=? AND status='OPEN'", (actor_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT p.* FROM pending p JOIN requests r ON r.id=p.request_id "
                "WHERE p.actor_id=? AND p.status='OPEN' AND r.page_id=?", (actor_id, page_id)).fetchall()
        out = [self.get_pending(r[0]) for r in rows]
        return [p for p in out if request_id is None or p["request_id"] == request_id]
    def insert_pending(self, p):
        self.conn.execute("INSERT INTO pending VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], p["request_id"], p["actor_id"], p["qtype"], p["expected_slot"],
             json.dumps(p["options"], ensure_ascii=False), p["proposition"], p["issued_at"],
             p["expires_at"], p["version"], p["status"], p.get("basis_version"),
             json.dumps(p.get("basis_slots") or {}, ensure_ascii=False)))
    def update_pending(self, p):
        self.conn.execute("UPDATE pending SET status=?, version=? WHERE id=? AND version=?",
                          (p["status"], p["version"], p["id"], p["version"] - 1))
        if self.conn.execute("SELECT changes()").fetchone()[0] == 0:
            raise RuntimeError(f"pending version conflict {p['id']}")
    # -- contact blocks: tập hợp nhiều kênh bị chặn (R08). Một kênh bị chặn
    # không ghi đè mất kênh đã chặn trước đó; mở lại từng kênh hoặc tất cả.
    def blocks(self, page_id, actor_id):
        rows = self.conn.execute(
            "SELECT channel FROM contact_blocks WHERE page_id=? AND actor_id=?",
            (page_id, actor_id)).fetchall()
        return frozenset(r[0] for r in rows)
    def block(self, page_id, actor_id, channel):
        self.conn.execute("INSERT OR IGNORE INTO contact_blocks VALUES (?,?,?)",
                          (page_id, actor_id, channel))
    def unblock(self, page_id, actor_id, channel):
        self.conn.execute("DELETE FROM contact_blocks WHERE page_id=? AND actor_id=? AND channel=?",
                          (page_id, actor_id, channel))
    def clear_blocks(self, page_id, actor_id):
        self.conn.execute("DELETE FROM contact_blocks WHERE page_id=? AND actor_id=?",
                          (page_id, actor_id))
    def handoff(self, request_id):
        r = self.conn.execute("SELECT * FROM handoffs WHERE request_id=? AND released_at IS NULL",
                              (request_id,)).fetchone()
        return dict(request_id=r[0], owner=r[1], reason=r[2], overdue=bool(r[6]) if len(r) > 6 else False) if r else None
    def add_handoff(self, request_id, owner, reason, lock_version, now):
        self.conn.execute("INSERT OR REPLACE INTO handoffs VALUES (?,?,?,?,?,NULL,0)",
                          (request_id, owner, reason, lock_version, now))
    def release_handoff(self, request_id, now):
        self.conn.execute("UPDATE handoffs SET released_at=? WHERE request_id=?",
                          (now, request_id))
    def mark_handoff_overdue(self, request_id):
        """Quá SLA -> OVERDUE (escalation nội bộ), KHÔNG release cho bot (R04)."""
        self.conn.execute("UPDATE handoffs SET overdue=1 WHERE request_id=? AND released_at IS NULL",
                          (request_id,))
    def handoff_for_scope(self, page_id, actor_id, topic, vehicle=None, variant=None):
        """Handoff đang hiệu lực cùng scope (page, actor, topic, vehicle/variant).
        None ở một phía = wildcard (thiếu thông tin thì coi là cùng scope cho
        an toàn — bot không gửi chen khi chưa phân biệt được). Trả về handoff
        row + request tương ứng, hoặc None khi scope thực sự độc lập."""
        rows = self.conn.execute(
            "SELECT h.request_id FROM handoffs h JOIN requests r ON r.id=h.request_id "
            "WHERE h.released_at IS NULL AND r.page_id=? AND r.actor_id=? AND r.topic=?",
            (page_id, actor_id, topic)).fetchall()
        for (rid,) in rows:
            req = self.get_request(rid)
            if not req: continue
            rv = (req["slots"].get("vehicle") or {}).get("value") if isinstance(req["slots"].get("vehicle"), dict) else req["slots"].get("vehicle")
            rvar = (req["slots"].get("variant") or {}).get("value") if isinstance(req["slots"].get("variant"), dict) else req["slots"].get("variant")
            if vehicle is not None and rv is not None and vehicle != rv: continue
            if variant is not None and rvar is not None and variant != rvar: continue
            return dict(request_id=rid, owner="staff", topic=topic, vehicle=rv, variant=rvar)
        return None
    # -- outbox durable (R03): intent -> claim -> gửi -> ghi kết quả.
    def insert_outbox_intended(self, dedupe_key, request_id, purpose, version, payload, now):
        self.conn.execute("INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,?,?,?)",
                          (dedupe_key, request_id, purpose, version, "INTENDED",
                           json.dumps(payload, ensure_ascii=False), now, None, None))
    def get_outbox(self, dedupe_key):
        r = self.conn.execute("SELECT * FROM outbox WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if not r: return None
        return dict(dedupe_key=r[0], request_id=r[1], purpose=r[2], version=r[3],
                    status=r[4], payload=json.loads(r[5]), created_at=r[6],
                    claimed_at=r[7] if len(r) > 7 else None,
                    claimed_by=r[8] if len(r) > 8 else None)
    def claim_outbox(self, dedupe_key, worker_id=None, now_iso=None):
        """Claim nguyên tử: chỉ INTENDED -> CLAIMED mới thành công, kèm
        claimed_at/claimed_by làm lease (A5): worker khác thấy CLAIMED còn
        lease thì không được reset mù; hết lease drain mới được reclaim.
        Dispatcher thứ hai claim cùng key sẽ nhận False (không gửi trùng)."""
        cur = self.conn.execute(
            "UPDATE outbox SET status='CLAIMED', claimed_at=?, claimed_by=? "
            "WHERE dedupe_key=? AND status='INTENDED'",
            (now_iso, worker_id, dedupe_key))
        return cur.rowcount == 1
    def reclaim_outbox(self, dedupe_key, worker_id=None, now_iso=None):
        """Reclaim một row CLAIMED đã hết lease (drain recovery): chỉ
        CLAIMED -> CLAIMED (đổi owner/lease) mới thành công."""
        cur = self.conn.execute(
            "UPDATE outbox SET claimed_at=?, claimed_by=? "
            "WHERE dedupe_key=? AND status='CLAIMED'",
            (now_iso, worker_id, dedupe_key))
        return cur.rowcount == 1
    # -- proposal rejections (A2): đề xuất đã bị từ chối thì không đề xuất lại
    # y hệt; fingerprint = slot values của request lúc bị từ chối.
    def add_proposal_rejection(self, request_id, fingerprint, now):
        self.conn.execute("INSERT OR IGNORE INTO proposal_rejections VALUES (?,?,?)",
                          (request_id, fingerprint, now))
    def rejected_fingerprints(self, request_id):
        return {r[0] for r in self.conn.execute(
            "SELECT fingerprint FROM proposal_rejections WHERE request_id=?", (request_id,)).fetchall()}
    def set_outbox_status(self, dedupe_key, status):
        self.conn.execute("UPDATE outbox SET status=? WHERE dedupe_key=?", (status, dedupe_key))
    def audit(self, comment_id, kind, payload, now):
        self.conn.execute("INSERT INTO audit VALUES (?,?,?,?,?)",
                          (str(uuid.uuid4()), comment_id, kind,
                           json.dumps(payload, ensure_ascii=False, default=str), now))
    def ledger(self):
        return [dict(dedupe_key=r[0], request_id=r[1], purpose=r[2], status=r[4],
                     payload=json.loads(r[5]))
                for r in self.conn.execute("SELECT * FROM outbox ORDER BY created_at").fetchall()]

class FakeReceiver:
    """Phía nhận bền vững, TÁCH KHỎI transaction DB của engine (R03). Mỗi effect
    được commit ngay trên connection riêng: một effect đã xảy ra không biến mất
    chỉ vì engine DB rollback. Dedupe theo delivery key: cùng key chỉ tạo tối
    đa 1 effect dù dispatcher retry hay redelivery (at-most-one effect; KHÔNG
    hứa exactly-once vì transport không có bảo đảm tương ứng)."""
    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self.conn.execute("CREATE TABLE IF NOT EXISTS effects(dedupe_key TEXT PRIMARY KEY, payload TEXT, created_at TEXT)")
        self.conn.commit()
    def record(self, payload):
        """Trả True nếu đây là effect mới, False nếu key đã có (không tạo thêm)."""
        from datetime import datetime, timezone
        cur = self.conn.execute("INSERT OR IGNORE INTO effects VALUES (?,?,?)",
                                (payload["dedupe_key"], json.dumps(payload, ensure_ascii=False),
                                 datetime.now(timezone.utc).isoformat()))
        self.conn.commit()
        return cur.rowcount == 1
    def has_effect(self, dedupe_key):
        return self.conn.execute("SELECT 1 FROM effects WHERE dedupe_key=?", (dedupe_key,)).fetchone() is not None
    def effect_count(self, dedupe_key=None):
        if dedupe_key is None:
            return self.conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM effects WHERE dedupe_key=?", (dedupe_key,)).fetchone()[0]

class FakeSender:
    """Sender duy nhất trong sandbox. Không có code gửi Facebook thật trong repo
    ngoài connector đọc (facebook_cli) — sender này không import nó.
    Luồng: send() ghi effect vào FakeReceiver TRƯỚC (bền vững, riêng biệt),
    rồi mới trả kết quả — TRỪ fail_mode="lost_ack": ACK mất nghĩa là kết quả
    gửi KHÔNG XÁC ĐỊNH, nên KHÔNG ghi effect (ghi effect rồi báo UNKNOWN sẽ
    khiến reconcile kết luận SENT từ bằng chứng mà phía gửi chưa từng có).
    lost_ack -> trả UNKNOWN, KHÔNG retry mù; Engine.reconcile_unknown() giữ
    UNKNOWN cho tới khi receiver xác nhận có effect (confirm_delivery là
    đường ACK muộn: lúc đó effect mới được ghi)."""
    def __init__(self, fail_mode=None, receiver=None):
        self.sent = []; self.fail_mode = fail_mode
        self.receiver = receiver or FakeReceiver()
        self.pending_deliveries = {}; self.confirmed = set()
    def send(self, payload):
        key = payload["dedupe_key"]
        if self.fail_mode == "lost_ack":
            # Kết quả không xác định: KHÔNG ghi effect, chỉ giữ pending để
            # confirm_delivery sau này có thể chốt.
            self.pending_deliveries[key] = payload
            return {"status": "UNKNOWN", "id": key}
        is_new = self.receiver.record(payload)
        if not is_new:
            # Redelivery cùng key: không tạo effect mới, trả như đã gửi.
            return {"status": "sent_to_fake_ledger", "id": key, "duplicate_delivery": True}
        self.sent.append(payload); return {"status": "sent_to_fake_ledger", "id": key}
    def confirm_delivery(self, key):
        """Xác nhận từ phía nhận (mô phỏng carrier ACK đến muộn): lúc này
        effect mới được ghi vào receiver, reconcile sau đó chốt SENT."""
        if key in self.pending_deliveries:
            payload = self.pending_deliveries.pop(key)
            self.receiver.record(payload)
            self.sent.append(payload); self.confirmed.add(key)
            return True
        return False
