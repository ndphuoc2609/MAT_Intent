"""Reproducer cho các nhóm lỗi R01-R15 (mô tả review bằng văn bản).

LƯU Ý NGUỒN GỐC — ĐỌC TRƯỚC KHI DÙNG SỐ LIỆU:
- Gói review gốc của reviewer (REPORT.md / VERIFICATION.json / script tái hiện)
  KHÔNG có trên máy này (đã quét toàn bộ workspace, không zip nào chứa chúng).
  Các probe dưới đây do tác giả sandbox TỰ TÁI TẠO từ mô tả R01-R15 bằng văn bản
  trong yêu cầu sửa, KHÔNG phải script gốc của reviewer.
- Quy ước exit NGƯỢC với reviewer: exit 0 = mọi probe đã FIXED (không còn tái
  hiện); exit 1 = còn ít nhất một probe REPRODUCED. Không được lấy exit 0 của
  script reviewer cũ (tái hiện được lỗi) làm kết quả hệ thống đạt.
- Không gọi model, không gửi Facebook thật; chỉ dùng fixture semantic + FakeSender.

Chạy: python3 scripts/review_repro.py [--only R01] [--list]
"""
from __future__ import annotations
import json, os, sys, tempfile
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.harness import make_engine
from app.engine import Clock, Engine
from app.facts import Facts, SEED_FACTS
from app.state import Store, FakeSender
from app.renderer import claim_check

T0 = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
PC1 = {"topic": "Creta 2026", "vehicles": ["Creta"], "persona_default": "tư vấn viên"}
PC2 = {"topic": "Nhiều xe Hyundai", "vehicles": ["Creta", "Tucson"], "persona_default": "tư vấn viên"}

def ev(cid, actor, text, page="page1", parent="root"):
    return dict(page_id=page, post_id="post_1", thread_root_id="root", comment_id=cid,
                parent_id=parent, actor_id=actor, actor_role="user", event_type="create",
                content_version=1, text=text, event_dedupe_key=f"{cid}:1")

def sem(ops, reqs=None, sarcasm="absent", unc=None):
    return dict(schema_version="1", sentiment="NEU", sarcasm=sarcasm, scope="in_scope",
                requests=reqs or [dict(topic="price", desired_outcome="price", polarity="positive")],
                operations=ops, uncertainties=unc or [])

def op(opname, ref=None, slot=None, pid=None, scope=None, reason=None, text="x"):
    d = dict(op=opname, evidence_excerpt=text)
    if ref is not None: d["request_ref"] = ref
    if slot is not None: d["slot"] = slot
    if pid is not None: d["pending_id"] = pid
    if scope is not None: d["scope"] = scope
    if reason is not None: d["reason"] = reason
    return d

def slot(kind, value, explicit=True):
    return dict(kind=kind, value=value, explicit=explicit)

def reqs_of(eng, actor=None):
    rows = eng.store.conn.execute("SELECT id, page_id, topic, status, slots, actor_id FROM requests ORDER BY rowid").fetchall()
    out = [dict(id=r[0], page_id=r[1], topic=r[2], status=r[3], slots=json.loads(r[4]), actor_id=r[5]) for r in rows]
    return [r for r in out if actor is None or r["actor_id"] == actor]

def pends_of(eng, actor=None):
    rows = eng.store.conn.execute("SELECT id, request_id, qtype, expected_slot, status, proposition, actor_id FROM pending ORDER BY rowid").fetchall()
    out = [dict(id=r[0], request_id=r[1], qtype=r[2], expected_slot=r[3], status=r[4], proposition=r[5], actor_id=r[6]) for r in rows]
    return [p for p in out if actor is None or p["actor_id"] == actor]

def start_price(eng, actor, text, cid, pc=PC1, slots_ops=()):
    ops = [op("START_REQUEST", ref="topic:price", text=text)]
    ops += list(slots_ops)
    return eng.process(ev(cid, actor, text), pc, sem(ops))

def ask_pending(eng, actor):
    """Trả về pending OPEN đầu tiên của actor (đi qua plan ask)."""
    ps = [p for p in pends_of(eng, actor) if p["status"] == "OPEN"]
    return ps[0] if ps else None

# ---------------- R01 ----------------
def p_r01_cross_pending_confirm():
    # B xác nhận pending của A (CONFIRM không kiểm tra actor của pending).
    eng = make_engine(clock=Clock(T0))
    start_price(eng, "uA", "giá Creta", "c1", slots_ops=[op("UPDATE_SLOT", ref="topic:price", slot=slot("price_kind", "niêm yết"), text="giá Creta")])
    # A hỏi testdrive -> pending yes_no
    eng.process(ev("c2", "uA", "đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"), PC1,
                sem([op("START_REQUEST", ref="topic:testdrive", text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"),
                     op("UPDATE_SLOT", ref="topic:testdrive", slot=slot("location", "Hà Nội"), text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"),
                     op("UPDATE_SLOT", ref="topic:testdrive", slot=slot("datetime", "sáng thứ hai tuần sau"), text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau")],
                    reqs=[dict(topic="testdrive", desired_outcome="testdrive", polarity="positive")]))
    pa = ask_pending(eng, "uA")
    # B tự tạo testdrive request của mình rồi CONFIRM bằng pending của A.
    eng.process(ev("c3", "uB", "đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"), PC1,
                sem([op("START_REQUEST", ref="topic:testdrive", text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"),
                     op("UPDATE_SLOT", ref="topic:testdrive", slot=slot("location", "Hà Nội"), text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"),
                     op("UPDATE_SLOT", ref="topic:testdrive", slot=slot("datetime", "sáng thứ hai tuần sau"), text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau")],
                    reqs=[dict(topic="testdrive", desired_outcome="testdrive", polarity="positive")]))
    res = eng.process(ev("c4", "uB", "oke"), PC1,
                      sem([op("CONFIRM_PENDING", ref="topic:testdrive", pid=pa["id"], text="oke")],
                          reqs=[dict(topic="testdrive", desired_outcome="testdrive", polarity="positive")]))
    pa_after = [p for p in pends_of(eng, "uA") if p["id"] == pa["id"]][0]
    bug = (pa_after["status"] == "CONFIRMED")  # pending của A bị B xác nhận
    return bug, f"pending A status={pa_after['status']} routing={res['routing']}"

def p_r01_pending_request_mismatch():
    # Cùng actor, pending của request này nhưng request_ref trỏ request khác.
    eng = make_engine(clock=Clock(T0))
    start_price(eng, "u1", "giá Creta", "c1")
    p = ask_pending(eng, "u1")  # pending price_kind của request price
    eng.process(ev("c2", "u1", "đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"), PC1,
                sem([op("START_REQUEST", ref="topic:testdrive", text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"),
                     op("UPDATE_SLOT", ref="topic:testdrive", slot=slot("location", "Hà Nội"), text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"),
                     op("UPDATE_SLOT", ref="topic:testdrive", slot=slot("datetime", "sáng thứ hai tuần sau"), text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau")],
                    reqs=[dict(topic="testdrive", desired_outcome="testdrive", polarity="positive")]))
    res = eng.process(ev("c3", "u1", "Hà Nội"), PC1,
                      sem([op("ANSWER_PENDING", ref="topic:testdrive", pid=p["id"], slot=slot("location", "Hà Nội"), text="Hà Nội")],
                          reqs=[dict(topic="testdrive", desired_outcome="testdrive", polarity="positive")]))
    p_after = [x for x in pends_of(eng, "u1") if x["id"] == p["id"]][0]
    bug = (p_after["status"] == "ANSWERED" and "REVIEW_BAD_PENDING" not in res["routing"])
    return bug, f"pending price status={p_after['status']} routing={res['routing']}"

def p_r01_cross_page():
    # Cùng actor id ở hai Page: pending page1 bị dùng ở page2 qua đường
    # pending-id fallback (Loop 8): ref chính là pending id, không phải topic.
    eng = make_engine(clock=Clock(T0))
    start_price(eng, "u1", "giá Creta", "c1", pc=PC1)
    p = ask_pending(eng, "u1")
    res = eng.process(ev("c2", "u1", "niêm yết", page="page2"), PC1,
                      sem([op("ANSWER_PENDING", ref=p["id"], pid=p["id"], slot=slot("price_kind", "niêm yết"), text="niêm yết")]))
    p_after = [x for x in pends_of(eng, "u1") if x["id"] == p["id"]][0]
    bug = (p_after["status"] == "ANSWERED")
    return bug, f"pending status={p_after['status']} routing={res['routing']} (cross-page pending accepted = bug)"

def p_r01_confirm_slot_question():
    # CONFIRM_PENDING cho câu hỏi chọn phiên bản/slot (không phải yes_no):
    # pending phải giữ OPEN, lượt phải REVIEW (qtype không phù hợp).
    eng = make_engine(clock=Clock(T0))
    start_price(eng, "u1", "giá Creta", "c1")
    p = ask_pending(eng, "u1")  # choice price_kind
    res = eng.process(ev("c2", "u1", "oke"), PC1,
                      sem([op("CONFIRM_PENDING", ref="topic:price", pid=p["id"], text="oke")]))
    p_after = [x for x in pends_of(eng, "u1") if x["id"] == p["id"]][0]
    bug = (p_after["status"] == "CONFIRMED" and "REVIEW" not in " ".join(res["routing"]))
    return bug, f"pending status={p_after['status']} routing={res['routing']} (CONFIRM on choice question = bug)"

def p_r01_confirm_negative():
    # Semantic CONFIRM cho câu "không" -> không được READY.
    eng = make_engine(clock=Clock(T0))
    eng.process(ev("c1", "u1", "đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"), PC1,
                sem([op("START_REQUEST", ref="topic:testdrive", text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"),
                     op("UPDATE_SLOT", ref="topic:testdrive", slot=slot("location", "Hà Nội"), text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"),
                     op("UPDATE_SLOT", ref="topic:testdrive", slot=slot("datetime", "sáng thứ hai tuần sau"), text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau")],
                    reqs=[dict(topic="testdrive", desired_outcome="testdrive", polarity="positive")]))
    p = ask_pending(eng, "u1")
    res = eng.process(ev("c2", "u1", "không"), PC1,
                      sem([op("CONFIRM_PENDING", ref="topic:testdrive", pid=p["id"], text="không")],
                          reqs=[dict(topic="testdrive", desired_outcome="testdrive", polarity="positive")]))
    r = [x for x in reqs_of(eng, "u1") if x["topic"] == "testdrive"][0]
    bug = (r["status"] == "READY")
    return bug, f"testdrive status={r['status']} routing={res['routing']}"

def p_r01_reject_answered():
    # REJECT_PENDING lên pending đã ANSWERED -> không được lật thành REJECTED.
    eng = make_engine(clock=Clock(T0))
    start_price(eng, "u1", "giá Creta", "c1")
    p = ask_pending(eng, "u1")
    eng.process(ev("c2", "u1", "niêm yết"), PC1,
                sem([op("ANSWER_PENDING", ref="topic:price", pid=p["id"], slot=slot("price_kind", "niêm yết"), text="niêm yết")]))
    res = eng.process(ev("c3", "u1", "không"), PC1,
                      sem([op("REJECT_PENDING", ref="topic:price", pid=p["id"], text="không")]))
    p_after = [x for x in pends_of(eng, "u1") if x["id"] == p["id"]][0]
    bug = (p_after["status"] == "REJECTED")
    return bug, f"pending status={p_after['status']} routing={res['routing']}"

def p_r01_topic_tie():
    # Hai request cùng topic khác xe, ref topic:price + UPDATE không có slot
    # phân biệt: engine cũ chọn request mới nhất (Tucson) trong im lặng.
    eng = make_engine(clock=Clock(T0))
    start_price(eng, "u1", "giá Creta", "c1", pc=PC2,
                slots_ops=[op("UPDATE_SLOT", ref="topic:price", slot=slot("vehicle", "Creta"), text="giá Creta")])
    start_price(eng, "u1", "giá Tucson", "c2", pc=PC2,
                slots_ops=[op("UPDATE_SLOT", ref="topic:price", slot=slot("vehicle", "Tucson"), text="giá Tucson")])
    before = [(r["id"], json.dumps(r["slots"], sort_keys=True)) for r in reqs_of(eng, "u1")]
    res = eng.process(ev("c3", "u1", "niêm yết"), PC2,
                      sem([op("UPDATE_SLOT", ref="topic:price", slot=slot("price_kind", "niêm yết"), text="niêm yết")]))
    after = [(r["id"], json.dumps(r["slots"], sort_keys=True)) for r in reqs_of(eng, "u1")]
    mutated = before != after
    ambiguous = any(x in res["routing"] for x in ("REVIEW_REF_AMBIGUOUS", "REVIEW_AMBIGUOUS"))
    bug = mutated and not ambiguous
    return bug, f"mutated={mutated} ambiguous={ambiguous} routing={res['routing']} (silent newest-pick = bug)"

def p_r01_valid_confirm():
    # Positive: xác nhận hợp lệ vẫn READY.
    eng = make_engine(clock=Clock(T0))
    eng.process(ev("c1", "u1", "đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"), PC1,
                sem([op("START_REQUEST", ref="topic:testdrive", text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"),
                     op("UPDATE_SLOT", ref="topic:testdrive", slot=slot("location", "Hà Nội"), text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau"),
                     op("UPDATE_SLOT", ref="topic:testdrive", slot=slot("datetime", "sáng thứ hai tuần sau"), text="đặt lái thử Creta ở Hà Nội sáng thứ hai tuần sau")],
                    reqs=[dict(topic="testdrive", desired_outcome="testdrive", polarity="positive")]))
    p = ask_pending(eng, "u1")
    res = eng.process(ev("c2", "u1", "oke"), PC1,
                      sem([op("CONFIRM_PENDING", ref="topic:testdrive", pid=p["id"], text="oke")],
                          reqs=[dict(topic="testdrive", desired_outcome="testdrive", polarity="positive")]))
    r = [x for x in reqs_of(eng, "u1") if x["topic"] == "testdrive"][0]
    ok = (r["status"] == "READY" and res.get("sends"))
    return (not ok), f"valid confirm: status={r['status']} sends={len(res.get('sends', []))} (must stay working)"

# ---------------- R02 ----------------
def p_r02_cancel_sarcasm():
    eng = make_engine(clock=Clock(T0))
    start_price(eng, "u1", "giá Creta", "c1")
    eng.process(ev("c2", "u1", "hủy giá đi, xe dở ẹc"), PC1,
                sem([op("CANCEL_REQUEST", ref="topic:price", text="hủy giá đi, xe dở ẹc")], sarcasm="uncertain"))
    r = [x for x in reqs_of(eng, "u1") if x["topic"] == "price"][0]
    bug = (r["status"] == "CANCELLED")
    return bug, f"price status={r['status']} (REVIEW_SARCASM must not commit cancel)"

def p_r02_cancel_ambiguous():
    eng = make_engine(clock=Clock(T0))
    start_price(eng, "u1", "giá Creta", "c1")
    eng.process(ev("c2", "u1", "hủy giá"), PC1,
                sem([op("CANCEL_REQUEST", ref="topic:price", text="hủy giá")],
                    unc=[dict(alternatives=["hủy giá", "giữ giá"], different_action=True)]))
    r = [x for x in reqs_of(eng, "u1") if x["topic"] == "price"][0]
    bug = (r["status"] == "CANCELLED")
    return bug, f"price status={r['status']} (REVIEW_AMBIGUOUS must not commit cancel)"

def p_r02_partial_bad_ref():
    eng = make_engine(clock=Clock(T0))
    start_price(eng, "u1", "giá Creta", "c1")
    res = eng.process(ev("c2", "u1", "Hà Nội"), PC1,
                      sem([op("UPDATE_SLOT", ref="topic:price", slot=slot("location", "Hà Nội"), text="Hà Nội"),
                           op("ANSWER_PENDING", ref="topic:price", pid="q_nope123", slot=slot("price_kind", "niêm yết"), text="Hà Nội")]))
    r = [x for x in reqs_of(eng, "u1") if x["topic"] == "price"][0]
    bug = (r["slots"].get("location") is not None)
    return bug, f"location slot persisted={r['slots'].get('location')} routing={res['routing']} (partial turn must not persist)"

def p_r02_partial_bad_pending_other():
    eng = make_engine(clock=Clock(T0))
    start_price(eng, "u1", "giá Creta", "c1")
    start_price(eng, "u1", "giá Tucson", "c2", pc=PC2,
                slots_ops=[op("UPDATE_SLOT", ref="topic:price", slot=slot("vehicle", "Tucson"), text="giá Tucson")])
    res = eng.process(ev("c3", "u1", "Hà Nội"), PC2,
                      sem([op("UPDATE_SLOT", ref="topic:price", slot=slot("location", "Hà Nội"), text="Hà Nội"),
                           op("ANSWER_PENDING", ref="topic:price", pid="q_nope123", slot=slot("price_kind", "niêm yết"), text="Hà Nội")]))
    rs = reqs_of(eng, "u1")
    bug = any(r["slots"].get("location") is not None for r in rs)
    return bug, f"some request got location persisted, routing={res['routing']}"

def p_r02_mixed_safe_kept():
    # Positive: price safe + complaint handoff trong một lượt: price vẫn gửi, complaint draft.
    eng = make_engine(clock=Clock(T0))
    res = eng.process(ev("c1", "u1", "giá Creta bản cao và khiếu nại tiền cọc"), PC1,
                      sem([op("START_REQUEST", ref="topic:price", text="giá Creta bản cao và khiếu nại tiền cọc"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("price_kind", "niêm yết"), text="giá Creta bản cao và khiếu nại tiền cọc"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("variant", "Cao cấp"), text="giá Creta bản cao và khiếu nại tiền cọc"),
                           op("REPORT_COMPLAINT", ref="topic:complaint", reason="deposit", text="giá Creta bản cao và khiếu nại tiền cọc")],
                          reqs=[dict(topic="price", desired_outcome="price", polarity="positive"),
                                dict(topic="complaint", desired_outcome="complaint", polarity="negative")]))
    ok = bool(res.get("sends")) and any(x["topic"] == "price" and x["status"] == "COMPLETED" for x in reqs_of(eng, "u1"))
    return (not ok), f"mixed safe kept: sends={len(res.get('sends', []))} routing={res['routing']} (must stay working)"

# ---------------- R03 ----------------
class LedgerSender(FakeSender):
    """Sender mô phỏng receiver ledger bền vững RIÊNG (sống qua restart, commit
    ngay khi effect xảy ra). crash=True: effect đã ghi nhưng raise trước khi
    engine lưu kết quả -> engine rollback toàn bộ transaction."""
    def __init__(self, ledger, crash=False):
        super().__init__()
        self.ledger = ledger
        self.crash = crash
    def send(self, payload):
        self.ledger.append(payload["dedupe_key"])  # effect bền vững, không dedupe (baseline)
        if self.crash:
            raise RuntimeError("crash after receiver effect, before result persisted")
        return {"status": "sent_to_fake_ledger", "id": payload["dedupe_key"]}

def _price_sem(text):
    return sem([op("START_REQUEST", ref="topic:price", text=text),
                op("UPDATE_SLOT", ref="topic:price", slot=slot("price_kind", "niêm yết"), text=text),
                op("UPDATE_SLOT", ref="topic:price", slot=slot("variant", "Cao cấp"), text=text)])

def p_r03_crash_after_effect():
    receiver = []
    eng = make_engine(clock=Clock(T0))
    eng.sender = LedgerSender(receiver, crash=True)
    eng.process(ev("c1", "u1", "giá Creta bản cao"), PC1, _price_sem("giá Creta bản cao"))
    # Restart: engine mới trên cùng store + cùng receiver ledger, xử lý lại event
    # (ingest đã rollback nên event không bị dedupe chặn).
    eng2 = Engine(eng.store, eng.facts, LedgerSender(receiver, crash=False), eng.clock)
    eng2.process(ev("c1", "u1", "giá Creta bản cao"), PC1, _price_sem("giá Creta bản cao"))
    rows = eng2.store.ledger()
    bug = (len(receiver) == 2 and len([r for r in rows if r["status"] == "SENT"]) == 1)
    return bug, f"receiver effects={len(receiver)} sent rows={len([r for r in rows if r['status']=='SENT'])} (2 effects 1 row = bug)"

def p_r03_double_dispatch_same_key():
    # Hai lần gửi cùng delivery key phải cho tối đa 1 effect (receiver dedupe).
    receiver = []
    class CountingSender(FakeSender):
        def send(self, payload):
            receiver.append(payload["dedupe_key"])
            return super().send(payload)
    eng = make_engine(clock=Clock(T0))
    eng.sender = CountingSender()
    payload = dict(dedupe_key="c1:reply", text="x", comment_id="c1")
    eng._send_reply(ev("c1", "u1", "x"), [], "reply", "x", T0.isoformat(), payload)
    eng._send_reply(ev("c1", "u1", "x"), [], "reply", "x", T0.isoformat(), payload)
    bug = (len(receiver) == 2)
    return bug, f"receiver got {len(receiver)} effects for same key (must be 1)"

def p_r03_unknown_no_blind_retry():
    # Positive: UNKNOWN không có bằng chứng delivery -> reconcile giữ UNKNOWN, không retry.
    eng = make_engine(clock=Clock(T0))
    eng.sender = FakeSender(fail_mode="lost_ack")
    eng.process(ev("c1", "u1", "giá Creta bản cao"), PC1,
                sem([op("START_REQUEST", ref="topic:price", text="giá Creta bản cao"),
                     op("UPDATE_SLOT", ref="topic:price", slot=slot("price_kind", "niêm yết"), text="giá Creta bản cao"),
                     op("UPDATE_SLOT", ref="topic:price", slot=slot("variant", "Cao cấp"), text="giá Creta bản cao")]))
    done = eng.reconcile_unknown()
    rows = eng.store.ledger()
    ok = all(r["status"] == "UNKNOWN" for r in rows if r["purpose"] == "reply") and len(eng.sender.sent) == 0
    return (not ok), f"unknown kept: rows={[(r['status']) for r in rows]} sent={len(eng.sender.sent)} (must stay working)"

# ---------------- R04 ----------------
def _handoff_price(eng, actor):
    # Handoff gắn với request giá CRETA (có slot vehicle): scope của nó là
    # price/Creta, không phải toàn bộ topic price. Handoff trên request không
    # nêu xe (vehicle=None) là wildcard theo thiết kế và sẽ chặn cả topic.
    start_price(eng, actor, "giá Creta", "h1",
                slots_ops=[op("UPDATE_SLOT", ref="topic:price", slot=slot("vehicle", "Creta"), text="giá Creta")])
    eng.process(ev("h2", actor, "gặp nhân viên xử lý giá"), PC1,
                sem([op("REQUEST_HUMAN", ref="topic:price", reason="staff", text="gặp nhân viên xử lý giá")]))

def p_r04_start_bypasses_handoff():
    eng = make_engine(clock=Clock(T0))
    _handoff_price(eng, "u1")
    res = eng.process(ev("h3", "u1", "giá Creta"), PC1,
                      sem([op("START_REQUEST", ref="topic:price", text="giá Creta"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("price_kind", "niêm yết"), text="giá Creta"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("variant", "Cao cấp"), text="giá Creta")]))
    n_price = len([r for r in reqs_of(eng, "u1") if r["topic"] == "price"])
    bug = (n_price == 2 or bool(res.get("sends")))
    return bug, f"price requests={n_price} sends={len(res.get('sends', []))} (new id bypassing handoff = bug)"

def p_r04_independent_scope_allowed():
    # Positive: request thực sự độc lập (xe khác) vẫn được xử lý khi có handoff.
    eng = make_engine(clock=Clock(T0))
    _handoff_price(eng, "u1")
    res = eng.process(ev("h3", "u1", "giá Tucson bản cao"), PC2,
                      sem([op("START_REQUEST", ref="topic:price", text="giá Tucson bản cao"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("vehicle", "Tucson"), text="giá Tucson bản cao"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("price_kind", "niêm yết"), text="giá Tucson bản cao"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("variant", "Cao cấp"), text="giá Tucson bản cao")]))
    ok = bool(res.get("sends"))
    return (not ok), f"independent scope sends={len(res.get('sends', []))} (must stay working)"

def p_r04_overdue_not_released():
    eng = make_engine(clock=Clock(T0))
    _handoff_price(eng, "u1")
    eng.clock.advance(hours=49)
    eng.process(ev("h3", "u1", "giá Creta"), PC1,
                sem([op("START_REQUEST", ref="topic:price", text="giá Creta")]))
    ho = eng.store.handoff([r for r in reqs_of(eng, "u1") if r["topic"] == "price"][0]["id"])
    bug = (ho is None)
    return bug, f"handoff after 49h: {'RELEASED (bug)' if ho is None else 'still active (OVERDUE expected)'}"

def p_r04_restart_keeps_handoff():
    # Positive: restart (file-backed store) không mất handoff.
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "s.db")
    eng = make_engine(db=db, clock=Clock(T0))
    _handoff_price(eng, "u1")
    rid = [r for r in reqs_of(eng, "u1") if r["topic"] == "price"][0]["id"]
    eng2 = make_engine(db=db, clock=Clock(T0))
    ok = eng2.store.handoff(rid) is not None
    return (not ok), f"handoff survives restart: {ok} (must stay working)"

def p_r04_valid_release_continues():
    eng = make_engine(clock=Clock(T0))
    _handoff_price(eng, "u1")
    rid = [r for r in reqs_of(eng, "u1") if r["topic"] == "price"][0]["id"]
    eng.store.release_handoff(rid, T0.isoformat())
    eng.store.conn.commit()  # release ngoài process() cần commit tay (store không tự commit)
    # Sau release hợp lệ: request vẫn HANDED_OFF? Engine cho tiếp tục khi handoff đã release:
    # hành vi tối thiểu: START mới cùng scope không còn bị chặn bởi handoff cũ.
    res = eng.process(ev("h3", "u1", "giá Creta"), PC1,
                      sem([op("START_REQUEST", ref="topic:price", text="giá Creta"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("price_kind", "niêm yết"), text="giá Creta"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("variant", "Cao cấp"), text="giá Creta")]))
    ok = (eng.store.handoff(rid) is None)
    return (not ok), f"release works: handoff gone={ok} (must stay working)"

# ---------------- R07 ----------------
def p_r07_otr_fee_conflict():
    facts = Facts(facts=SEED_FACTS + [
        dict(id="f_fee_reg_hn_alt", type="reg_rate", vehicle=None, variant=None, location="Hà Nội",
             value=10, unit="percent", source="SAMPLE", valid_from="2026-01-01",
             valid_to="2026-12-31", ttl_hours=8760, version=1)])
    eng = make_engine(clock=Clock(T0), facts=facts)
    res = eng.process(ev("c1", "u1", "giá lăn bánh Creta bản cao ở Hà Nội"), PC1,
                      sem([op("START_REQUEST", ref="topic:price", text="giá lăn bánh Creta bản cao ở Hà Nội"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("price_kind", "lăn bánh"), text="giá lăn bánh Creta bản cao ở Hà Nội"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("variant", "Cao cấp"), text="giá lăn bánh Creta bản cao ở Hà Nội"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("location", "Hà Nội"), text="giá lăn bánh Creta bản cao ở Hà Nội")]))
    bug = bool(res.get("sends")) and "HANDOFF" not in res["routing"]
    return bug, f"otr conflict sends={len(res.get('sends', []))} routing={res['routing']} text={res.get('response_text')}"

def p_r07_stock_zero():
    facts = Facts(facts=[dict(id="f_stock_zero", type="stock", vehicle="Creta", variant="Cao cấp",
                              location="Hà Nội", value=0, unit="xe", extra={"color": "Trắng"},
                              source="SAMPLE", valid_from="2026-01-01", valid_to="2026-12-31",
                              ttl_hours=24, version=1)])
    eng = make_engine(clock=Clock(T0), facts=facts)
    res = eng.process(ev("c1", "u1", "còn Creta bản cao màu trắng không"), PC1,
                      sem([op("START_REQUEST", ref="topic:stock", text="còn Creta bản cao màu trắng không"),
                           op("UPDATE_SLOT", ref="topic:stock", slot=slot("variant", "Cao cấp"), text="còn Creta bản cao màu trắng không"),
                           op("UPDATE_SLOT", ref="topic:stock", slot=slot("color", "Trắng"), text="còn Creta bản cao màu trắng không")],
                          reqs=[dict(topic="stock", desired_outcome="stock", polarity="positive")]))
    bug = (res.get("response_text") or "").startswith("Còn")
    return bug, f"stock=0 text={res.get('response_text')}"

def p_r07_stock_conflict():
    facts = Facts(facts=[
        dict(id="f_stock_a", type="stock", vehicle="Creta", variant="Cao cấp", location="Hà Nội",
             value=1, unit="xe", extra={"color": "Trắng"}, source="SAMPLE",
             valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=24, version=1),
        dict(id="f_stock_b", type="stock", vehicle="Creta", variant="Cao cấp", location="Hà Nội",
             value=0, unit="xe", extra={"color": "Trắng"}, source="SAMPLE",
             valid_from="2026-01-01", valid_to="2026-12-31", ttl_hours=24, version=1)])
    eng = make_engine(clock=Clock(T0), facts=facts)
    res = eng.process(ev("c1", "u1", "còn Creta bản cao màu trắng không"), PC1,
                      sem([op("START_REQUEST", ref="topic:stock", text="còn Creta bản cao màu trắng không"),
                           op("UPDATE_SLOT", ref="topic:stock", slot=slot("variant", "Cao cấp"), text="còn Creta bản cao màu trắng không"),
                           op("UPDATE_SLOT", ref="topic:stock", slot=slot("color", "Trắng"), text="còn Creta bản cao màu trắng không")],
                          reqs=[dict(topic="stock", desired_outcome="stock", polarity="positive")]))
    bug = bool(res.get("sends")) and (res.get("response_text") or "").startswith("Còn")
    return bug, f"stock conflict text={res.get('response_text')} routing={res['routing']}"

def p_r07_ttl_none_infinite():
    # Match fact verification time to this frozen clock, then let the 24h TTL expire.
    eng = make_engine(clock=Clock(T0), facts=Facts(verified_at=T0))
    eng.clock.advance(hours=49)
    res = eng.process(ev("c1", "u1", "còn Creta bản cao màu trắng không"), PC1,
                      sem([op("START_REQUEST", ref="topic:stock", text="còn Creta bản cao màu trắng không"),
                           op("UPDATE_SLOT", ref="topic:stock", slot=slot("variant", "Cao cấp"), text="còn Creta bản cao màu trắng không"),
                           op("UPDATE_SLOT", ref="topic:stock", slot=slot("color", "Trắng"), text="còn Creta bản cao màu trắng không")],
                          reqs=[dict(topic="stock", desired_outcome="stock", polarity="positive")]))
    bug = (res.get("response_text") or "").startswith("Còn")
    return bug, f"49h later stock text={res.get('response_text')} (None must not mean infinite TTL)"

def p_r07_stale_between_plan_dispatch():
    eng = make_engine(clock=Clock(T0), facts=Facts(verified_at=T0))
    def hook():
        eng.clock.advance(hours=49)  # fact hết TTL sau plan, version không đổi
    eng.dispatch_hook = hook
    res = eng.process(ev("c1", "u1", "còn Creta bản cao màu trắng không"), PC1,
                      sem([op("START_REQUEST", ref="topic:stock", text="còn Creta bản cao màu trắng không"),
                           op("UPDATE_SLOT", ref="topic:stock", slot=slot("variant", "Cao cấp"), text="còn Creta bản cao màu trắng không"),
                           op("UPDATE_SLOT", ref="topic:stock", slot=slot("color", "Trắng"), text="còn Creta bản cao màu trắng không")],
                          reqs=[dict(topic="stock", desired_outcome="stock", polarity="positive")]))
    bug = bool(res.get("sends"))
    return bug, f"stale-at-dispatch sends={len(res.get('sends', []))} routing={res['routing']}"

def p_r07_valid_fact_answered():
    # Positive: fact hợp lệ vẫn trả lời trực tiếp.
    eng = make_engine(clock=Clock(T0))
    res = eng.process(ev("c1", "u1", "giá niêm yết Creta bản cao"), PC1,
                      sem([op("START_REQUEST", ref="topic:price", text="giá niêm yết Creta bản cao"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("price_kind", "niêm yết"), text="giá niêm yết Creta bản cao"),
                           op("UPDATE_SLOT", ref="topic:price", slot=slot("variant", "Cao cấp"), text="giá niêm yết Creta bản cao")]))
    ok = bool(res.get("sends")) and "699.000.000" in (res.get("response_text") or "")
    return (not ok), f"valid fact answered: sends={len(res.get('sends', []))} text={res.get('response_text')} (must stay working)"

# ---------------- R08 / R09 ----------------
def p_r08_multi_channel_prefs():
    eng = make_engine(clock=Clock(T0))
    eng.process(ev("c1", "u1", "đừng gọi nữa"), PC1,
                sem([op("SET_CONTACT_PREFERENCE", scope="call", text="đừng gọi nữa")]))
    eng.process(ev("c2", "u1", "đừng trả lời công khai"), PC1,
                sem([op("SET_CONTACT_PREFERENCE", scope="public_reply", text="đừng trả lời công khai")]))
    blocks = eng.store.blocks("page1", "u1") if hasattr(eng.store, "blocks") else ({eng.store.prefs("page1", "u1")} if eng.store.prefs("page1", "u1") else set())
    bug = not ({"call", "public_reply"} <= set(blocks))
    return bug, f"blocks={blocks} (second block must not overwrite first)"

def p_r08_allow_all_reachable():
    eng = make_engine(clock=Clock(T0))
    eng.process(ev("c1", "u1", "đừng gọi nữa"), PC1,
                sem([op("SET_CONTACT_PREFERENCE", scope="call", text="đừng gọi nữa")]))
    from app.semantics import validate_semantic
    errs = validate_semantic(sem([op("SET_CONTACT_PREFERENCE", scope="allow_all", text="cho phép lại")]), "cho phép lại", [])
    bug = bool(errs)
    return bug, f"validator errs for allow_all: {errs} (engine branch exists but validator rejects it)"

def p_r08_optin_while_blocked():
    eng = make_engine(clock=Clock(T0))
    eng.process(ev("c1", "u1", "đừng trả lời công khai"), PC1,
                sem([op("SET_CONTACT_PREFERENCE", scope="public_reply", text="đừng trả lời công khai")]))
    eng.process(ev("c2", "u1", "cho phép lại"), PC1,
                sem([op("SET_CONTACT_PREFERENCE", scope="allow_all", text="cho phép lại")]))
    blocks = eng.store.blocks("page1", "u1") if hasattr(eng.store, "blocks") else ({eng.store.prefs("page1", "u1")} if eng.store.prefs("page1", "u1") else set())
    bug = bool(blocks)
    return bug, f"blocks after opt-in while blocked: {blocks} (opt-in must be recordable)"

def p_r09_unknown_slot_kind():
    from app.semantics import validate_semantic
    errs = validate_semantic(sem([op("UPDATE_SLOT", ref="topic:price", slot=dict(kind="mystery_kind", value="x"), text="x")]), "x", [])
    bug = not errs
    return bug, f"unknown slot kind errs: {errs} (must be rejected)"

def p_r09_start_out_of_scope():
    from app.semantics import validate_semantic
    s = sem([op("START_REQUEST", ref="topic:price", text="x")])
    s["scope"] = "out_of_scope"
    errs = validate_semantic(s, "x", [])
    bug = not errs
    return bug, f"out_of_scope START errs: {errs} (must be rejected)"

def p_r09_extra_explicit_and_source():
    # slot.extra bị gán explicit=True + source thành comment hiện tại ("oke").
    eng = make_engine(clock=Clock(T0))
    start_price(eng, "u1", "giá Creta", "c1")
    eng.process(ev("c2", "u1", "oke"), PC1,
                sem([op("UPDATE_SLOT", ref="topic:price", slot=dict(kind="price_kind", value="niêm yết", explicit=False, extra={"vehicle": "Creta"}), text="oke")]))
    r = [x for x in reqs_of(eng, "u1") if x["topic"] == "price"][0]
    veh = r["slots"].get("vehicle", {})
    bug = (veh.get("explicit") is True) or (veh.get("source_message_id") == "c2" and veh.get("explicit") is False)
    return bug, f"inherited vehicle slot: {veh} (must not auto-claim explicit=True/source=current 'oke')"

# ---------------- R10 ----------------
def p_r10_swapped_prices():
    used = [dict(fact_id="f_list_creta_high", vehicle="Creta", variant="Cao cấp", location=None, attribute="list_price", value=699_000_000, unit="VND"),
            dict(fact_id="f_list_tucson_high", vehicle="Tucson", variant="Cao cấp", location=None, attribute="list_price", value=899_000_000, unit="VND")]
    text = "Giá niêm yết Creta Cao cấp là 899.000.000 đồng, Tucson Cao cấp là 699.000.000 đồng (giá mẫu SAMPLE)."
    try:
        from app.renderer import claim_check_typed
        errs = claim_check_typed(text, used)
    except ImportError:
        errs = claim_check(text, [699_000_000, 899_000_000], used, {"Creta", "Tucson"})
    bug = not errs
    return bug, f"swapped prices errs: {errs} (union checker accepts the swap = bug)"

def p_r10_correct_mixed_passes():
    used = [dict(fact_id="f_list_creta_high", vehicle="Creta", variant="Cao cấp", location=None, attribute="list_price", value=699_000_000, unit="VND"),
            dict(fact_id="f_list_tucson_high", vehicle="Tucson", variant="Cao cấp", location=None, attribute="list_price", value=899_000_000, unit="VND")]
    text = "Giá niêm yết Creta Cao cấp là 699.000.000 đồng, Tucson Cao cấp là 899.000.000 đồng (giá mẫu SAMPLE)."
    try:
        from app.renderer import claim_check_typed
        errs = claim_check_typed(text, used)
    except ImportError:
        errs = claim_check(text, [699_000_000, 899_000_000], used, {"Creta", "Tucson"})
    ok = not errs
    return (not ok), f"correct mixed errs: {errs} (must stay working)"

# ---------------- R11-R15 (headline checks) ----------------
def p_r11_sql_mutant_invalid():
    # Sau sửa: mutation suite v2 dùng mutant SQL HỢP LỆ (xóa cả placeholder
    # lẫn param) và phân loại INVALID riêng. Probe kiểm tra mutant
    # drop_state_version_check trong suite v2 phải là KILLED, không phải INVALID.
    import subprocess
    r = subprocess.run([sys.executable, "tests/mutations/mutation_suite.py"],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    bug = ("KILLED drop_state_version_check" not in out)
    return bug, f"mutation suite v2 drop_state_version_check killed={'KILLED drop_state_version_check' in out} (suite exit={r.returncode})"

def p_r12_scorer_exit_and_zip():
    # Scorer cũ: final-state fail chỉ in, exit luôn 0; zip() bỏ turn thiếu.
    import subprocess
    run = dict(new_calls=0, cumulative_calls=0, new_cost_usd=0, cumulative_cost_usd=0, budget_hit=False,
               conversations=[dict(id="mt_dev_01", group="g", turns=[dict(turn=0, actor="u1", text="x", semantic={}, validation_errors=[], routing=["answer_price"], response_text="ok", sends_n=1, requests=[], pendings=[], prefs=None, ledger_purposes_added=[], errors=[], open_pendings_before=[])])])
    conv = [dict(id="mt_dev_01", group="g", turns=[dict(actor="u1", text="x", expected={}), dict(actor="u1", text="y", expected={})], final=dict(requests=[dict(topic="price", status="COMPLETED")]))]
    d = tempfile.mkdtemp()
    rp, cp = os.path.join(d, "run.json"), os.path.join(d, "conv.jsonl")
    json.dump(run, open(rp, "w")); open(cp, "w").write(json.dumps(conv[0], ensure_ascii=False) + "\n")
    # Sau sửa: scorer v3 (scorer cũ giữ nguyên làm lịch sử) phải exit != 0:
    # structural (1 run turn vs 2 dataset turns) -> exit 2.
    r = subprocess.run([sys.executable, "scripts/score_multiturn_v3.py", rp, cp], cwd=ROOT, capture_output=True, text=True)
    bug = (r.returncode == 0)
    return bug, f"scorer v3 exit={r.returncode} (final-state fail + dropped turn must affect exit code)"

def p_r13_cache_key_ignores_input():
    # Sau sửa: cache key v2 (scripts/cache_key.py) hash cả comment + state.
    from scripts.cache_key import full_cache_key
    from app.adapters.luna import SYSTEM, MODEL
    schema = open(os.path.join(ROOT, "config", "schema_versions", "semantic_v1.json")).read()
    pc = {"topic": "Creta", "vehicles": ["Creta"]}
    k1 = full_cache_key(comment="giá Creta", parent="", postcard=pc, state_fp="fp1",
                        system=SYSTEM, schema=schema, model=MODEL)
    k2 = full_cache_key(comment="giá Tucson", parent="", postcard=pc, state_fp="fp1",
                        system=SYSTEM, schema=schema, model=MODEL)
    k3 = full_cache_key(comment="giá Creta", parent="", postcard=pc, state_fp="fp2",
                        system=SYSTEM, schema=schema, model=MODEL)
    bug = (k1 == k2) or (k1 == k3)
    return bug, f"cache key v2 differs by comment={k1 != k2} and by state={k1 != k3}"

def p_r15_server_frozen_clock():
    from app.server import ENG
    t1 = ENG.clock.now(); t2 = ENG.clock.now()
    bug = (t1 == t2)
    return bug, f"server clock now() twice equal: {t1} == {t2} (server must use wall clock)"

def p_r15_policy_module_missing():
    bug = not os.path.exists(os.path.join(ROOT, "app", "policy.py"))
    return bug, "policy.yaml cites app/policy.py as the single source, but the module does not exist"

def p_r15_pg_not_implemented():
    # Sau sửa (theo đúng R15): PG được GHI NHÃN NOT_IMPLEMENTED rõ ràng, không
    # phải "làm như đã có". Probe kiểm tra nhãn, không đòi Dockerfile giả.
    import glob
    has_adapter = os.path.exists(os.path.join(ROOT, "app", "pg_adapter.py"))
    compose = open(os.path.join(ROOT, "compose.yaml")).read()
    labeled = "NOT_IMPLEMENTED" in compose and "PostgreSQL adapter" in compose
    try:
        from app import pg_adapter
        raises = False
        try:
            pg_adapter.connect("postgresql://example/db")
        except pg_adapter.PostgresNotImplemented:
            raises = True
    except Exception:
        raises = False
    bug = (not has_adapter) or (not labeled) or (not raises)
    return bug, f"pg_adapter={has_adapter} compose_labeled_NOT_IMPLEMENTED={labeled} connect_raises={raises} (no Dockerfile by design; PG is labeled NOT_IMPLEMENTED, not claimed)"

PROBES = [
    ("R01", "cross_pending_confirm", p_r01_cross_pending_confirm),
    ("R01", "pending_request_mismatch", p_r01_pending_request_mismatch),
    ("R01", "cross_page_pending", p_r01_cross_page),
    ("R01", "confirm_slot_question", p_r01_confirm_slot_question),
    ("R01", "confirm_negative", p_r01_confirm_negative),
    ("R01", "reject_answered", p_r01_reject_answered),
    ("R01", "topic_tie_no_pick", p_r01_topic_tie),
    ("R01", "valid_confirm_kept", p_r01_valid_confirm),
    ("R02", "cancel_sarcasm_no_commit", p_r02_cancel_sarcasm),
    ("R02", "cancel_ambiguous_no_commit", p_r02_cancel_ambiguous),
    ("R02", "partial_bad_ref", p_r02_partial_bad_ref),
    ("R02", "partial_bad_pending_other", p_r02_partial_bad_pending_other),
    ("R02", "mixed_safe_kept", p_r02_mixed_safe_kept),
    ("R03", "crash_after_effect", p_r03_crash_after_effect),
    ("R03", "double_dispatch_same_key", p_r03_double_dispatch_same_key),
    ("R03", "unknown_no_blind_retry", p_r03_unknown_no_blind_retry),
    ("R04", "start_bypasses_handoff", p_r04_start_bypasses_handoff),
    ("R04", "independent_scope_allowed", p_r04_independent_scope_allowed),
    ("R04", "overdue_not_released", p_r04_overdue_not_released),
    ("R04", "restart_keeps_handoff", p_r04_restart_keeps_handoff),
    ("R04", "valid_release_continues", p_r04_valid_release_continues),
    ("R07", "otr_fee_conflict", p_r07_otr_fee_conflict),
    ("R07", "stock_zero", p_r07_stock_zero),
    ("R07", "stock_conflict", p_r07_stock_conflict),
    ("R07", "ttl_none_infinite", p_r07_ttl_none_infinite),
    ("R07", "stale_between_plan_dispatch", p_r07_stale_between_plan_dispatch),
    ("R07", "valid_fact_answered", p_r07_valid_fact_answered),
    ("R08", "multi_channel_prefs", p_r08_multi_channel_prefs),
    ("R08", "allow_all_reachable", p_r08_allow_all_reachable),
    ("R08", "optin_while_blocked", p_r08_optin_while_blocked),
    ("R09", "unknown_slot_kind", p_r09_unknown_slot_kind),
    ("R09", "start_out_of_scope", p_r09_start_out_of_scope),
    ("R09", "extra_explicit_and_source", p_r09_extra_explicit_and_source),
    ("R10", "swapped_prices", p_r10_swapped_prices),
    ("R10", "correct_mixed_passes", p_r10_correct_mixed_passes),
    ("R11", "sql_mutant_invalid", p_r11_sql_mutant_invalid),
    ("R12", "scorer_exit_and_zip", p_r12_scorer_exit_and_zip),
    ("R13", "cache_key_ignores_input", p_r13_cache_key_ignores_input),
    ("R15", "server_frozen_clock", p_r15_server_frozen_clock),
    ("R15", "policy_module_missing", p_r15_policy_module_missing),
    ("R15", "pg_not_implemented", p_r15_pg_not_implemented),
]

def main():
    only = None
    if "--only" in sys.argv: only = sys.argv[sys.argv.index("--only") + 1]
    if "--list" in sys.argv:
        for g, n, _ in PROBES: print(f"{g} {n}")
        return 0
    reproduced, fixed = [], []
    for g, name, fn in PROBES:
        if only and only not in (g, name): continue
        try:
            bug, detail = fn()
        except Exception as e:
            bug, detail = True, f"PROBE ERROR: {type(e).__name__}: {e}"
        (reproduced if bug else fixed).append((g, name))
        print(f"[{'REPRODUCED' if bug else 'FIXED'}] {g} {name} :: {detail}")
    print(f"\nreproduced={len(reproduced)} fixed={len(fixed)} total={len(reproduced) + len(fixed)}")
    return 1 if reproduced else 0

if __name__ == "__main__":
    raise SystemExit(main())
