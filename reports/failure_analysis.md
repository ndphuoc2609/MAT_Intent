# Failure analysis (tới batch Luna 2)

## Batch 1 — HỦY (bug, không phải headline)
- 40 ca dev, topic_exact 0.811, routing 0.270.
- Nguyên nhân chính: clock naive trong run_api_eval vs facts aware -> ERROR_ROLLBACK
  hàng loạt (21 ca AUTO->REVIEW). Đã sửa: Facts naive-guard + clock tz-aware.
- Lỗi Luna thật trong batch 1 (3 validation errors): evidence paraphrase (2 ca),
  operations rỗng cho spam (1 ca). Đã sửa bằng prompt + repair 1 lần.

## Batch 2 — sau Vòng sửa 1 (40 ca, 36 scored, 4 excluded engine-only)
- topic_exact 0.944 (34/36), recall 0.972, routing 0.75 (27/36), val_err 0, api_err 0.
- cost $0.0106, calls 41 (gồm 1 repair).
- Nhóm lỗi còn lại (đã phân tích từng ca, xem runs/api_dev_b2.json):
  1. **Pref mâu thuẫn** (pref_no_call_keep_price): Luna sinh 2 pref op (call +
     public_reply) cho "gửi giá tại đây" -> dispatch bị chặn. -> Vòng 2: prompt
     1 pref op + pre-scan conflict -> REVIEW_PREF_CONFLICT.
  2. **Handoff trùng trong cùng turn** (handoff_lock): REPORT_COMPLAINT rồi
     REQUEST_HUMAN cùng request -> BLOCKED_HANDOFF. -> Vòng 2: idempotence
     cùng turn (op human thứ hai bỏ qua, không chặn).
  3. **Thiếu slot** (loan unit, spec compare, price_kind): Luna bỏ unit "triệu",
     gộp "Creta với Tucson" vào compare_vehicle, bỏ price_kind khi câu chỉ nói
     "giá". -> Vòng 2: prompt unit/compare; price_kind thiếu thì ASK là đúng
     (không tính là lỗi engine — scoring route_of đã sửa: mixed->AUTO, pure ask->ASK).
  4. **Service warranty** (defensible): Luna chọn REQUEST_HUMAN vì khách xin số
     tư vấn, trong khi fact bảo hành có sẵn. -> Vòng 2: prompt "trả lời fact nếu
     có, human chỉ khi không có fact / muốn gặp người".
  5. **Buy thừa** (neg_variant_change): đổi variant trong câu tồn kho vẫn thêm
     buy. Lỗi Luna dai dẳng qua cả repair — ghi nhận, không loop vô hạn.
  6. **Artifact scoring** (pref_no_contact_all): fixture cũ gán request social
     cho ca chỉ có pref op. -> Đã sửa generator: requests=[].

## Nhóm lỗi KHÔNG còn sau Vòng 1
- Validation/evidence: 3 -> 0 (repair + prompt verbatim).
- Topic chính: price/promo/loan/dealer/spec đơn nhu cầu gần như đúng hết.

## Batch 3 — sau Vòng sửa 2 (40 ca, 36 scored)
- topic_exact 0.972 (35/36), recall 1.000, routing 0.889 (32/36), val_err 0, api_err 0.
- cost $0.0112, calls 40.
- Lỗi còn lại: service_warranty AUTO->HANDOFF (Luna vẫn chọn human dù fact có
  sẵn — vòng 2 của hướng này, không tiến triển); neg_variant_change thêm buy
  thừa (vòng 2, không tiến triển); 3 AUTO->ASK trong đó 1 bug thật, 2 là ASK
  đúng (thiếu price_kind — fixture artifact).

## Vòng sửa 3 — price_kind canonicalization (0 call, replay batch 3)
- Bug: Luna viết "niêm yết"/"lăn bánh" vào slot price_kind, engine so sánh
  với 'list'/'otr' nên không nhận -> ASK thừa. Sửa canon_slot: niêm yết->list,
  lăn bánh->otr + strip prefix "giá ".
- Bug thứ hai trong cùng fix: _apply_slot loại price_kind khỏi danh sách
  điều kiện -> đã sửa cùng lúc.
- Replay cached semantics qua engine mới (runs/api_dev_b3_replay.json):
  routing 0.917 (33/36), topic 0.972, recall 1.000, dev macro-F1 0.982 (n=36,
  chỉ 1 FP là buy của neg_variant_change).
- pref_inbox_ok và neg_not_tucson AUTO->ASK là ASK ĐÚNG (thiếu price_kind),
  không tính là lỗi Luna — fixture artifact đã ghi nhận.

## Fresh holdout — đo MỘT lần, 24 ca (runs/api_holdout_fresh.json)
- topic_exact 0.958, recall 0.958, routing 0.833, val_err 0, api_err 0,
  calls 24, cost $0.0069.
- Mismatch: fh_loan_docs và fh_spec -> ERROR_ROLLBACK (engine crash, xem
  dưới); fh_testdrive ASK mong đợi nhưng AUTO (bug vague datetime);
  fh_sarcasm topic miss (fixture artifact — expected ['social'] lỏng, routing
  REVIEW của Luna là đúng); fh_no_call AUTO->ASK (ASK đúng, fixture artifact).
- **Đây là số pre-fix duy nhất được trích. Sau khi sửa, tập này đã thành
  development (xem MANIFEST).**

## Vòng sửa 4 — crash dup-start + vague datetime + claim 2.0 (0 call mới)
1. **Dup-start crash (nghiêm trọng, gốc của cả 2 ERROR fresh):** Luna sinh 2
   START_REQUEST cùng topic trong 1 turn (2 câu hỏi con của 1 nhu cầu).
   Engine tạo 2 request object cho cùng request -> _plan chạy 2 lần ->
   xung đột version -> rollback toàn bộ turn. Sửa: START cho topic đã
   touched trong turn tái dùng object cũ. Regression: dup_start_same_turn.
2. **Vague datetime:** parse_relative_datetime match "nay" trong "cuối tuần
   này" -> parse thành hôm nay -> AUTO sai. Sửa: chứa "tuan"/"thang" ->
   None (vague -> engine ASK). Regression: vague_week_testdrive.
3. **Claim 2.0 (phát hiện khi verify fix 1):** fact Tucson có extra engine
   "2.0L" nhưng claim_check chỉ cho phép dạng "2" (float integral) trong khi
   text chứa "2.0" -> REVIEW_CLAIM sai. Sửa: allowed gồm cả str(int) và
   str(float). Sau sửa: spec Tucson trả lời đúng, 1 send, 0 error.
- Sau Vòng 4: contract 183/183, unit 15/15 (thêm recovery test), mutation
  8/8, replay dev không đổi (0.917 routing). Không re-run fresh holdout
  làm headline (đã bị xem).

## Nhóm lỗi còn tồn tại (không sửa thêm trong phiên này)
- service_warranty: Luna vẫn route HANDOFF dù fact bảo hành có sẵn
  (2 vòng prompt không tiến triển — dừng theo quy tắc 3 vòng).
- neg_variant_change: đổi variant trong câu tồn kho vẫn thêm buy thừa
  (2 vòng không tiến triển — dừng).
- Cả hai đều an toàn theo hướng thận trọng (HANDOFF) hoặc chỉ thừa 1 topic
  (buy) trong khi stock vẫn đúng; không có lỗi nghiêm trọng quan sát được
  (không confirm sai, không trộn người, không sai fact, không vi phạm
  opt-out, không gửi trùng).

## Chưa đo được (INSUFFICIENT_EVIDENCE)
- Negation/confirmation/pending-link trên API đơn lượt: contract cover (ack,
  slot ngắn, testdrive flow), API đơn lượt không đo được nhóm này.
- Multi-request recall trên API: chỉ 1 ca multi trong batch 2 (đúng topics,
  routing ASK do testdrive thiếu location — hành vi đúng).
- Human-verified: 0 ca. Mọi số Luna đều là SEMANTIC_EXPLORATORY.
