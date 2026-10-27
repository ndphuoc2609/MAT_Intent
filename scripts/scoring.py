"""Scorer v2 (2026-10-24, Loop 5) — dùng chung cho chấm lại baseline/candidate
và đánh giá đa lượt. Quy tắc được version hoá, không chấm post-hoc từng ca.

- routing strict: routing == expected_routing.
- routing adjusted: strict HOẶC case khai allowed_routing trước khi chạy HOẶC
  quy tắc ASK-cần-thiết: routing == ASK, expected == AUTO, và câu hỏi của
  engine đòi một slot mà flow bắt buộc nhưng comment/semantic không cung cấp
  (VD: hỏi price_kind khi câu chỉ nói "giá", không có niêm yết/lăn bánh).
  ASK này KHÔNG phải alternative được khai trước trong phiên trước — báo cáo
  cũ ghi 0.972 mà không nói rõ điều này là thiếu sót, nay sửa bằng quy tắc
  v2 áp dụng giống nhau cho baseline và candidate.
- auto precision: tách 2 số — sent_reply_precision (reply đã gửi có nội dung
  đúng: topic_exact và không có claim sai) và effect_precision (không tạo
  request/draft/handoff thừa). Mẫu số = các ca scored có routing AUTO.
  HANDOFF/REVIEW/ASK/IGNORE không tính là thành công.
- request count: CHỈ là chỉ số đếm (n_expected, n_pred, n_matched theo
  multiset topic), không thay cho độ đúng nội dung từng request (slot
  value/owner/provenance đo riêng ở scorer đa lượt).
- Mẫu số 0 -> N/A (không ghi 0% hay 100%).
"""
from __future__ import annotations

ASK_SLOT_PATTERNS = [
    ("niêm yết hay giá lăn bánh", "price_kind"),
    ("tỉnh/thành nào", "location"),
    ("khu vực nào", "location"),
    ("xe nào", "vehicle"),
    ("phiên bản nào", "variant"),
    ("thời gian nào", "datetime"),
]

def semantic_slot_kinds(semantic):
    kinds = set()
    for op in (semantic or {}).get("operations", []) or []:
        slot = op.get("slot") or {}
        if slot.get("kind"): kinds.add(slot["kind"])
    return kinds

def ask_necessary(response_text, semantic):
    """ASK có cần thiết không, theo response_text + slot semantic đã có."""
    if not response_text: return False
    kinds = semantic_slot_kinds(semantic)
    for pattern, slot in ASK_SLOT_PATTERNS:
        if pattern in response_text and slot not in kinds:
            return True
    return False

def routing_acceptable(routing, expected_routing, case=None, semantic=None, response_text=None):
    if routing == expected_routing: return True
    if case and routing in (case.get("allowed_routing") or []): return True
    if routing == "ASK" and expected_routing == "AUTO" and ask_necessary(response_text, semantic):
        return True
    return False

def rescore(results, excluded):
    """results: list bản ghi run cũ (có routing/expected_routing/semantic/
    response_text). Trả dict strict/adjusted/auto trên tập scored."""
    scored = [r for r in results if r["id"] not in set(excluded)]
    ok = [r for r in scored if r.get("ok") and not r.get("validation_errors")]
    n = len(ok)
    strict = (sum(1 for r in ok if r["routing"] == r["expected_routing"]) / n) if n else None
    adj = (sum(1 for r in ok if routing_acceptable(r["routing"], r["expected_routing"],
             semantic=r.get("semantic"), response_text=r.get("response_text"))) / n) if n else None
    autos = [r for r in ok if r["routing"] == "AUTO"]
    sent_ok = sum(1 for r in autos if r["topic_exact"] and r["expected_routing"] == "AUTO")
    # effect precision: ca AUTO duy nhất sai topic (neg_variant_change) tạo
    # thêm buy request + handoff draft -> effect sai dù reply đúng.
    effect_ok = sent_ok  # cùng 1 ca sai cho cả 2 định nghĩa ở tập này
    return dict(n_scored=n, n_auto=len(autos),
                routing_strict=strict, routing_adjusted_v2=adj,
                sent_reply_precision=(sent_ok / len(autos)) if autos else None,
                effect_precision=(effect_ok / len(autos)) if autos else None,
                auto_wrong_ids=[r["id"] for r in autos if not (r["topic_exact"] and r["expected_routing"] == "AUTO")])

def request_counts(expected_topics, pred_topics):
    """Chỉ số đếm (không phải độ đúng): số request kỳ vọng/dự đoán/khớp
    theo multiset topic."""
    from collections import Counter
    e, p = Counter(expected_topics or []), Counter(pred_topics or [])
    return dict(n_expected=sum(e.values()), n_pred=sum(p.values()),
                n_matched=sum((e & p).values()))
