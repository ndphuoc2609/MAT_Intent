# Báo cáo cuối — facebook-intent-sandbox (phiên 2026-10-24)

Project: `~/workspace/facebook-intent-sandbox/` (Python stdlib, SQLite sandbox,
fake sender duy nhất, không có code gửi Facebook thật trong `app/`).

## Lệnh đã chạy và xác nhận (2026-10-24, lần cuối sau Vòng sửa 4)
- `python3 scripts/generate_datasets.py` — 183 scenario, 23 family
- `python3 scripts/run_contract.py` — **183/183 pass**
  (dev 163/163, holdout gốc 20/20; multi-turn 70, multi-user 22, error/race 15)
- `python3 -m unittest discover -s tests` — **15/15 pass**
  (14 unit + recovery crash/restart)
- `python3 tests/mutations/mutation_suite.py` — **PASS 8/8 detected**
- `python3 scripts/replay_api.py runs/api_dev_b3.json runs/api_dev_b3_replay2.json`
  — deterministic, routing 0.917 / topic 0.972 / recall 1.000 (0 call mới)
- Server smoke: `python3 app/server.py` (127.0.0.1:8765) — POST /event -> answer,
  GET /ledger chỉ fake — pass (chạy trước Vòng 4, không thay đổi server).

## Bảng cổng nghiệm thu (phân biệt offline / API thật / tổng hợp / human)
| Cổng | Kết luận | Căn cứ |
|---|---|---|
| A — Engineering sandbox | **ENGINEERING_SANDBOX_PASS (có caveat)** | 183/183 contract, 15/15 unit, mutation 8/8, replay deterministic, recovery crash/restart pass, không leak cross-user, không fact hết hạn lọt ra, không send khi bị chặn, không duplicate effect trong test tương ứng. Caveat: PostgreSQL **chưa kiểm chứng** (VM không có Docker/PG; sandbox chạy SQLite, compose.yaml chưa từng chạy) — semantics transaction/race trên PG là chưa chứng minh. Holdout gốc đã thành development (xem MANIFEST). |
| B — Semantic & nghiệp vụ | **INSUFFICIENT_EVIDENCE** (chỉ SEMANTIC_EXPLORATORY) | Không có human gold (0 ca). Số Luna đều trên nhãn tổng hợp, n nhỏ, đánh giá đơn lượt. Dev replay: macro-F1 0.982, topic 0.972, recall 1.000, routing strict 0.917 (0.972 nếu tính ASK đúng) — trên ngưỡng số học 0.90 nhưng **không đủ điều kiện tuyên bố Gate B**. Fresh holdout pre-fix: topic 0.958, routing 0.833, 2 ERROR (đã sửa sau đó, tập đã thành development). Self-completion: 24/27 = 0.889 nhưng mẫu số < 30 -> INSUFFICIENT. Auto precision: 24/25 = 0.96 < 0.99, n nhỏ. Negation/confirmation/pending-link trên API: chưa đo (chỉ fixture). Không có lỗi nghiêm trọng quan sát được (không confirm sai, không trộn người, không sai fact, không vi phạm opt-out, không gửi trùng). |
| C — Sẵn sàng xin chạy shadow thực tế | **BLOCKED** | Thiếu: 200 comment/turn thực tế đã ẩn danh + người kiểm tra, monitoring, kill-switch drill trên dữ liệu thật, credential scope, kế hoạch tiếp nhận HANDOFF. Chỉ có labeling pack 60 ca chưa gán nhãn. **Không đề xuất kích hoạt shadow/suggest production trong phiên này.** |

## Baseline / candidate (cùng Engine, cùng input, paired)
| Candidate | Tập / mẫu số | topic_exact | recall | routing | calls | cost |
|---|---|---|---|---|---|---|
| Engine + fixture (contract) | 183 scenario | — (fixture) | — | 183/183 pass | 0 | $0 |
| Luna batch 1 | dev 40 | 0.811 | 0.800 | 0.270 | 40 | ~$0.0098 |
| **Batch 1 HỦY — không phải headline**: bug clock naive vs facts aware -> 21 ERROR_ROLLBACK. Đã sửa Facts naive-guard + clock tz-aware. | | | | | | |
| Luna batch 2 (sau Vòng 1: prompt + repair + scoring) | dev 36 scored (40 chạy, 4 engine-only loại) | 0.944 | 0.972 | 0.750 | 41 | $0.0106 |
| Luna batch 3 (sau Vòng 2: pref-conflict pre-scan, handoff idempotence, prompt unit/compare) | dev 36 scored | 0.972 | 1.000 | 0.889 | 40 | $0.0112 |
| Batch 3 replay (sau Vòng 3: price_kind canonicalization) | dev 36, 0 call mới | 0.972 | 1.000 | 0.917 | 0 | $0 |
| Fresh holdout — đo MỘT lần, pre-fix | 24 ca mới | 0.958 | 0.958 | 0.833 | 24 | $0.0069 |
| Sau Vòng 4 (dup-start, vague week, claim 2.0) | offline 183/183, unit 15/15, mutation 8/8 | — | — | — | 0 | $0 |

Cải thiện có bằng chứng: routing dev 0.270 (batch 1, bug) -> 0.750 -> 0.889
-> 0.917 (replay). Hồi quy: không — replay dev không đổi sau Vòng 4,
contract tăng 181 -> 183 do 2 regression mới, đều pass.
Model: Luna `openai/gpt-6-luna`, canonical `openai/gpt-6-luna-20260922`,
giá $0.10/1M prompt + $0.50/1M completion (theo /v1/models ngày 2026-10-24).

## 15 trace tiêu biểu (ưu tiên lỗi còn tồn tại và ranh giới khó)
1. **Giá lăn bánh (thành công):** "Creta Cao cấp lăn bánh Hà Nội?" ->
   answer_price, 1 send, `804,440,000 đồng` = 699,000,000 list + 12% + 20,000,000
   biển + 1,560,000 phí đường bộ, formula otr_v1. Request COMPLETED.
2. **Testdrive full flow (thành công, 4 lượt):** đăng ký Creta HN chiều mai ->
   propose 25/10 15:00; "Đừng hủy lịch, chỉ đổi sang sáng mai" -> propose lại
   25/10 09:00 (không hủy); "oke" -> xác nhận đề xuất (không nói đã đặt lịch);
   "Hủy lịch lái thử này" -> CANCELLED. Ledger 4 reply SENT, không trùng.
3. **Dup-start crash — lỗi nghiêm trọng đã sửa (fresh fh_spec):** Luna sinh 2
   START_REQUEST cùng topic "spec" trong 1 turn -> engine tạo 2 request ->
   xung đột version -> ERROR_ROLLBACK, 0 send. Sau Vòng 4: tái dùng object
   đã touched -> answer_spec, 1 send, request COMPLETED, 0 error.
4. **Vague datetime — đã sửa (fresh fh_testdrive):** "cuối tuần này" bị parse
   thành hôm nay do match "nay" -> AUTO sai. Sau Vòng 4: chứa "tuan"/"thang"
   -> None -> engine ASK ngày cụ thể. Regression vague_week_testdrive pass.
5. **Claim 2.0 — đã sửa (phát hiện khi verify fix 3):** fact Tucson extra
   engine "2.0L" nhưng claim_check chỉ cho "2" -> REVIEW_CLAIM sai.
   Sau sửa allowed gồm cả str(int) và str(float): "Thông số mẫu Tucson:
   engine 2.0L, seats 5, power_hp 156 (SAMPLE)", 1 send.
6. **service_warranty — LỖI CÒN TỒN TẠI:** khách xin số tư vấn bảo hành, fact
   bảo hành có sẵn; Luna vẫn REQUEST_HUMAN thay vì answer_service.
   An toàn theo hướng thận trọng (HANDOFF, không gửi sai), nhưng mất auto.
   2 vòng prompt không tiến triển -> dừng theo quy tắc 3 vòng.
7. **neg_variant_change — LỖI CÒN TỒN TẠI:** "Không lấy bản cao nữa, lấy
   bản tiêu chuẩn, còn màu trắng không?" -> Luna thêm buy thừa ngoài stock
   đúng. Stock vẫn đúng, chỉ thừa 1 topic (FP duy nhất trong dev macro-F1).
   2 vòng không tiến triển -> dừng.
8. **Pref mâu thuẫn — đã sửa (Vòng 2):** "gửi giá tại đây" sinh 2 pref op
   (call + public_reply) -> dispatch bị chặn. Sau pre-scan: conflict ->
   REVIEW_PREF_CONFLICT, không gửi sai kênh. Batch 3 ca này pass.
9. **Khiếu nại + hỏi giá (mixed, ranh giới):** phần giá trả lời an toàn,
   phần khiếu nại vào NEEDS_REVIEW draft cho người xử lý; không bị IGNORE
   lấn át, không gửi phản hồi tự do về khiếu nại.
10. **Duplicate webhook:** cùng event_dedupe_key gửi lại -> DUPLICATE_IGNORED,
    0 send thêm, ledger không tăng. Recovery test: crash/restart với DB file
    -> redelivery vẫn DUPLICATE_IGNORED, request COMPLETED bền vững.
11. **Sarcasm:** khen ngược / emoji cười -> sarcasm present/uncertain ->
    REVIEW, không auto trả lời nghiêm túc. Fresh fh_sarcasm: routing REVIEW
    đúng; topic miss chỉ là fixture artifact (expected ['social'] lỏng).
12. **Promo hết hạn:** fact promo quá valid_to -> không dùng giá mẫu cũ,
    route HANDOFF/ASK thay vì trả lời giá hết hạn. Contract facts group pass.
13. **Owner mismatch:** comment của người B trả lời pending của người A ->
    REVIEW, không điền slot/pending của người khác. Contract multiuser pass.
14. **Multi-request 1 câu:** giá + trả góp + lái thử -> 3 request riêng,
    mỗi request có slot/pending riêng, không đánh COMPLETED khi còn thiếu
    (testdrive thiếu location -> ASK đúng, không phải lỗi).
15. **Đổi xe làm vô hiệu variant cũ:** đổi Tucson -> Creta trong câu tồn kho
    -> variant/facts của Tucson bị revalidate, không mang sang Creta.
    Contract stock group pass.

## Cái gì có tác dụng / không có tác dụng
- Có tác dụng: Facts naive-guard + clock tz-aware (xóa 21 ERROR); repair 1 lần
  cho lỗi validation (val_err 3 -> 0); engine pre-scan pref-conflict và
  handoff idempotence (xóa 2 nhóm lỗi không cần sửa prompt); price_kind
  canonicalization (routing 0.889 -> 0.917 qua replay, 0 call); dup-start
  reuse + vague-datetime guard + claim float form (xóa 2 ERROR fresh).
- Không có tác dụng (đã dừng): prompt thêm cho service_warranty (2 vòng vẫn
  HANDOFF) và cho neg_variant_change (2 vòng vẫn thừa buy). Hướng tiếp theo
  nên là sửa ở tầng engine (ví dụ: service fact có sẵn -> ép answer trước
  khi chấp nhận REQUEST_HUMAN; buy chỉ khi có evidence mua rõ ràng), không
  phải thêm prompt nữa — nhưng đó là Vòng 5 trở đi, chưa làm trong phiên này.

## Ngân sách thực dùng (giới hạn: 300 calls / $2.00 / 12 vòng sửa)
- Calls: batch1 40 + chẩn đoán 3 + batch2 41 + batch3 40 + fresh 24 = **148**
- Cost: ~$0.0098 + $0.0007 + $0.0106 + $0.0112 + $0.0069 = **~$0.039**
- Vòng sửa: 4 (Vòng 1 tz/prompt, Vòng 2 pref/handoff, Vòng 3 canon,
  Vòng 4 crash/datetime/claim). Phần cost unknown: không có — mọi call đều
  có usage trả về; checkpoint từng ca trong runs/api_checkpoints/ (dev
  checkpoints cũ đã xóa trước batch 3 theo thiết kế resume).
- Không có call nào tới Facebook thật, không tạo lead/lịch/đặt cọc thật.

## Kết luận phân tầng
- **Đủ căn cứ xin code review:** engine + gate + outbox + facts + renderer
  (183/183 contract, mutation 8/8, recovery pass, trace đầy đủ). Đây là
  phần engineering, không phải tuyên bố model hiểu tiếng Việt tốt.
- **Chỉ exploratory:** mọi số Luna (dev replay + fresh pre-fix) — nhãn tổng
  hợp, n nhỏ, đơn lượt, holdout đã bị xem. Dùng để định hướng sửa, không
  dùng để tuyên bố Gate B.
- **Thiếu trước production:** (1) PostgreSQL thật + test race trên PG;
  (2) human gold >= 200 comment/turn thực tế đã ẩn danh, split theo family,
  kèm fresh holdout MỚI chưa từng xem; (3) đo negation/confirmation/
  pending-link và multi-request trên API đa lượt; (4) sửa 2 lỗi còn tồn tại
  ở tầng engine (service_warranty, neg_variant_change) hoặc chấp nhận
  HANDOFF/thừa topic như hành vi có chủ đích; (5) monitoring, kill-switch
  drill, kế hoạch tiếp nhận HANDOFF, credential scope — rồi mới xin chạy
  shadow/suggest, tuyệt đối không auto-send production từ trạng thái này.
