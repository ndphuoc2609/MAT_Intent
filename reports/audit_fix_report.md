# Báo cáo sửa audit độc lập — Session 4 (A1–A8)

- Candidate gốc: `facebook-intent-sandbox-reviewfix.zip`, SHA-256
  `865a4d4259a4e5182e19310a4f70d99dc30102282aa75700a6bef1a0e4609599`
  (freeze `runs/baseline_freeze_session4.json`, 447 files).
- Audit mới: `facebook-intent-reviewfix-audit.zip`, SHA-256
  `7fad9d9e8a07cdc5626f3677afd6ba48fbfd90837f366290265a9649bdd892be`
  (gồm `REVIEWFIX_AUDIT.md`, `VERIFICATION.json`, `residual_probes.py`,
  `infra_probes.py`, `resume_probe.py` và gói independent review gốc lồng bên trong).
- Môi trường: Python 3.12.3, SQLite 3.45.1, không Docker, không PostgreSQL.
- **0 model call trong phiên này** (ngân sách lịch sử giữ nguyên 276/300 calls,
  ~$0.0854/$2.00, còn 24). Mọi kiểm chứng API dùng mock/fixture.
- Không gửi Facebook thật, không deploy, không merge production.
- Probe gốc của reviewer KHÔNG bị sửa; adapter/test bổ sung tách riêng
  (`tests/unit/test_review_audit.py`, `tests/unit/test_review_infra2.py`).

## 0. Lưu ý về exit code của probe

Probe audit exit 0 chỉ có nghĩa script chạy xong, KHÔNG có nghĩa hệ thống pass.
Verdict đọc từ từng trường `residual_bug` / `control_pass` / assertion thực chất.

## 1. Bảng A1–A8: baseline → fix → test → trạng thái cuối → giới hạn

| ID | Baseline (bản review-fix) | Bản sửa (file chính) | Test chứng minh | Trạng thái cuối | Giới hạn còn lại |
|----|---------------------------|----------------------|-----------------|-----------------|------------------|
| A1 | UPDATE mơ hồ (TP.HCM, different_action=true) ghi đè slot live trong khi pending yes/no vẫn đề xuất Hà Nội; "oke" sau đó xác nhận đề xuất HN nhưng request READY mang TP.HCM. Opt-in trong lượt sarcasm=uncertain xóa block trước risk gate. | `app/engine.py`: ambiguity gate mới — op destructive → rollback toàn lượt (giữ nguyên session 3); op non-destructive → khôi phục snapshot request cũ (version+1) + SUPERSEDE pending yes/no cũ + audit `ambiguous_restore`; request mới giữ làm placeholder OPEN. Pending lưu `basis_version`/`basis_slots`; CONFIRM/REJECT kiểm tra basis, lệch → `REVIEW_STALE_PROPOSITION`. Pref phase: chỉ block áp ngay, allow hoãn tới sau validation + sarcasm + ambiguity gate, trong savepoint business. | residual `ambiguous_update_commits_live_slot`, `stale_proposition_confirmed_after_ambiguous_update`, `uncertain_optin_clears_blocks_before_review` đều false; `TestA1Ambiguity` 6 tests (gồm positive: update rõ ràng → đề xuất mới → confirm được; opt-in rõ ràng khi bị block vẫn được). | **FIXED** | Ambiguity vẫn chặn lượt mơ hồ bằng REVIEW, không tự suy đoán ý khách. |
| A2 | ANSWER choice chỉ kiểm tra value ∈ options, không kiểm tra expected_slot (pending hỏi price_kind, đáp location="list" vẫn ghi). REJECT chấp nhận yes_no()=None ("Cảm ơn" → REJECTED). Sau reject hợp lệ, `_plan` đề xuất lại đúng combo vừa bị từ chối. | `app/engine.py`: `_validate_pending` — choice ANSWER yêu cầu `slot.kind == expected_slot` VÀ value ∈ options; REJECT đối xứng CONFIRM (yn True → `REVIEW_CONFIRM_DENIED`, yn None → `REVIEW_AMBIGUOUS`). `_plan` testdrive: fingerprint slot hiện tại ∈ `rejected_fingerprints` → hỏi datetime khác thay vì re-propose. Bảng mới `proposal_rejections`. | residual `choice_answer_writes_wrong_slot`, `reject_pending_accepts_non_negative_text` false; `TestA2PendingEvidence` 4 tests (gồm positive choice đúng slot; reject "không" → hỏi giờ mới, trả lời giờ mới → đề xuất mới). | **FIXED** | — |
| A3 | `context()` recent chỉ lọc actor → Page B thấy text Page A; parent lookup không kiểm tra page; `_sweep_expired` không giới hạn page → event Page B hết hạn pending Page A. | `app/engine.py`: recent query thêm `json_extract(payload,'$.page_id')=?` (bảng events KHÔNG có cột page_id — page nằm trong JSON payload; lần sửa đầu dùng cột page_id đã gây lỗi và đã sửa lại); parent chỉ đưa vào context khi payload parent cùng page (fail-closed); `_sweep_expired` giới hạn (page, actor), handoff-overdue thêm `r.page_id=?`. | residual `context_reads_other_page` (recent=[]), `page_B_event_expires_page_A_pending` (pending Page A vẫn OPEN) false; `TestA3PageIsolation` 2 tests (gồm positive: event Page A vẫn hết hạn pending Page A). | **FIXED** | Housekeeping quét nhiều scope (nếu có) phải tách khỏi quyền đọc/sửa của event — hiện không có quét nào như vậy. |
| A4 | Hai START cùng `topic:price` (Creta + Tucson): `_turn_identity` lấy xe CUỐI (Tucson) áp cho cả hai handoff check → giá Creta gửi dù đang HANDOFF Creta price. | `app/engine.py`: `_start_identity(ops, ref, this_op)` — đúng một START mang ref → identity của START đó (+ UPDATE sau); nhiều START → `_op_identity(this_op)` riêng từng op. START/req bị handoff → `blocked_idx` (bỏ qua phần đó, không abort cả lượt); phần safe vẫn gửi + câu thông báo chung không nêu tên xe bị khóa. | residual `mixed_same_ref_bypasses_creta_handoff` false (routing `BLOCKED_HANDOFF` + `answer_price`, text chỉ có Tucson); `TestA4MultiRequestHandoff` 3 tests, cả hai thứ tự Creta→Tucson và Tucson→Creta, sends==1, không tạo request Creta mới, positive không-handoff trả cả hai xe. | **FIXED** | Câu thông báo phần bị khóa cố tình chung chung (không nêu tên xe) vì oracle của probe coi tên xe bị khóa xuất hiện trong text gửi là lỗi. |
| A5 | (a) INTENDED commit rồi crash trước dispatch → restart + redelivery = DUPLICATE_IGNORED, intent kẹt vĩnh viễn, không drain. (b) Nhánh social + disambiguation commit ingest trước khi có intent bền vững. (c) `make_engine(db=file)` tạo FakeReceiver `:memory:` → restart mất bằng chứng delivery. CLAIMED không có lease. | `app/engine.py` + `app/harness.py` + `app/server.py`: `_outbox_intended` ghi TRƯỚC commit ở mọi nhánh gửi (social, disambiguation, business). `drain_intended()` xử lý INTENDED và CLAIMED hết lease; claim/reclaim nguyên tử theo worker_id; version lệch → BLOCKED_STALE_STATE, không gửi; kill_switch → trả về INTENDED. `process()` đường DUPLICATE gọi `drain_intended`, giữ routing `["DUPLICATE_IGNORED"]` (test recovery cũ yêu cầu). `reconcile_unknown` lease-aware: có effect → SENT; CLAIMED còn lease, chưa effect → giữ CLAIMED; hết lease → INTENDED; UNKNOWN chưa effect → giữ UNKNOWN. File DB → receiver mặc định `db + ".receiver"` (bền vững qua restart); server khởi động drain best-effort. `CLAIM_LEASE_SECONDS = 300`. | residual `committed_intent_stuck_after_restart`, `social_reply_intent_missing_after_crash`, `receiver_evidence_lost_on_default_restart`, `control_actual_lost_ack_reconciles` đều false/pass; `TestA5OutboxRestart` 7 tests (gồm cả hai khả năng UNKNOWN: chưa delivery giữ UNKNOWN; đã effect nhưng mất ACK → SENT, effect vẫn 1; lease chưa hết giữ CLAIMED, +6 phút → INTENDED; stale state bị chặn). | **FIXED** (trong phạm vi SQLite + single-worker lease) | Không tuyên bố exactly-once ngoài phạm vi đã chứng minh: chỉ một worker drain tại một thời điểm theo lease; nhiều worker đồng thời chưa được kiểm chứng tải thật. `lost_ack_fixture_omits_delivered_effect` vẫn `residual_bug=true` — THEO THIẾT KẾ (xem §3). |
| A6 | Validator kiểm tra `op.source_message_id` nhưng `_apply_slot` đọc `slot.source_message_id` → source giả (NONEXISTENT_SOURCE) vẫn được lưu, đáp án vẫn gửi. Nested slot field không allowlist. Evidence chỉ kiểm tra chung chung, không kiểm tra trong chính source được claim. | `app/engine.py`: op-level source là canonical; slot-level nếu có phải bằng op-level, nếu chỉ có slot-level thì validate riêng; source phải tồn tại dạng event, cùng page, cùng actor (hoặc parent/root/"root"); **evidence_excerpt phải là substring của text của chính source đó**, nếu không → `REVIEW_PROVENANCE`. `_apply_slot` dùng canonical đã validate cho slot chính, extras và down_payment_unit. `app/semantics.py`: op.source ≠ slot.source → lỗi; allowlist key slot; extra value phải scalar. | residual `slot_source_id_bypasses_validation` false; `TestA6Provenance` 5 tests (slot source giả; source người khác; source Page khác; evidence không nằm trong source claim; positive source op hợp lệ và slot-only hợp lệ, source được ghi đúng). | **FIXED** | — |
| A7 | Cache key chứa `context_text` (fmt_context nhúng UUID request/pending) → engine mới luôn cache-miss; helper remap trong `scripts/resume.py` tồn tại nhưng runner không gọi. | `scripts/cache_key.py`: `full_cache_key` không còn nhận/gồm `context_text` (state_fp đã bao phủ state logic). `scripts/resume.py`: `try_remap_cached()` (lọc pending OPEN hai phía, map request/pending theo logical key, `remap_semantic`). `scripts/run_multiturn_eval.py`: checkpoint hit → remap thật với reqs/pendings hiện tại; remap fail → warning + gọi lại (tính là miss); checkpoint ghi `state_snapshot`. | resume probe: run2 `mocked_adapter_attempts`=0 (trước: 1), `resume_diverged: false`; `TestA7Resume` 2 tests (reuse 0 attempt + routes/final state logic bằng nhau; đổi input → invalidate, gọi lại). | **FIXED** | Không bỏ state khỏi cache key để ép hit (state_fp vẫn trong key). |
| A8 | Guard chỉ chạy mỗi lần gọi wrapper; adapter retry (tối đa 3) chạy dưới guard (prior=299, cap=300 → 302, budget_hit=False). Guard đọc lại ledger sau khi ghi rồi cộng thêm counter của run → double count (cap=2 dừng sau 1 call). Response thiếu usage → cost 0.0. | `app/adapters/luna.py`: `call_luna(..., budget_guard=None)` — guard gọi TRƯỚC mỗi HTTP attempt kể cả retry; bị từ chối → `ok=False, error="budget guard denied before HTTP attempt", attempts=<số attempt thực tế>`. Cost chỉ known khi usage có CẢ `prompt_tokens` và `completion_tokens`, nếu không `cost_usd_est=None, cost_unknown=True`. `scripts/run_multiturn_eval.py` + `run_api_eval.py`: baseline ledger đọc MỘT lần trước vòng conv; `_make_hook()` guard per-attempt (inflight counter; từ chối khi baseline+prior+run+inflight+1 > max_calls hoặc unknown-cost); guard truyền vào cả call chính và repair; `record()` có `status` (failed/unknown_cost/completed). `scripts/budget_ledger.py`: `breakdown()` tách attempted/completed/failed/unknown_cost/actual_cost_usd. | infra `ledger_and_run_double_count` false (mock_attempts=2, cumulative=2, turns_run=2), `missing_usage_treated_as_zero_cost` false (cost=null); `TestA8Budget` 4 tests (guard-conformant prior=299 → cumulative đúng 300; cap=2 → đúng 2 attempt; thiếu usage → cost None + unknown; breakdown tách status). | **FIXED cho adapter đúng contract; PARTIAL theo oracle probe gốc** | Infra probe `retry_attempts_exceed_budget` vẫn 302 — vì fake_call của probe patch bỏ adapter và phớt lờ `budget_guard`, nên accounting trung thực không thể "unspend" 3 attempt mà mock tự báo. Worst-case reservation (từ chối khi còn < 3 attempt) đã BỊ LOẠI vì vi phạm yêu cầu tường minh của user: "cap=2 phải cho phép đúng hai attempt hợp lệ". Spec của user cao hơn oracle probe; audit cũng nói một số probe nên thành regression với contract đã sửa, không dùng nguyên si làm CI. |

## 2. Trace đầy đủ (verbatim từ probe trên bản fixed copy)

### 2.1 Stale confirmation (A1)
```
{"probe": "ambiguous_update_commits_live_slot", "residual_bug": false,
 "routing": ["REVIEW_AMBIGUOUS"], "live_location": "Hà Nội",
 "pending_proposition": "Xác nhận đề xuất lái thử Creta tại Hà Nội lúc 25/10 lúc 09:00?",
 "pending_status": "SUPERSEDED"}
{"probe": "stale_proposition_confirmed_after_ambiguous_update", "residual_bug": false,
 "routing": ["REVIEW_BAD_PENDING"],
 "confirmed_proposition": "Xác nhận đề xuất lái thử Creta tại Hà Nội lúc 25/10 lúc 09:00?",
 "request_location": "Hà Nội"}
```
Đọc: lượt mơ hồ → REVIEW, slot live giữ Hà Nội, pending cũ bị SUPERSEDED;
lượt "oke" sau đó không xác nhận được đề xuất cũ (REVIEW_BAD_PENDING),
request không READY, không gửi gì.

### 2.2 Cross-Page (A3)
```
{"probe": "context_reads_other_page", "residual_bug": false, "recent": []}
{"probe": "page_B_event_expires_page_A_pending", "residual_bug": false,
 "routing": ["SOCIAL_ACK", "social_ack"], "page_A_pending_status": "OPEN"}
```
Đọc: context Page B trống (không mang text Page A); event Page B không hết hạn
pending Page A (vẫn OPEN). Positive control: event Page A vẫn hết hạn pending
Page A (test `TestA3PageIsolation`).

### 2.3 Multi-car HANDOFF (A4)
```
{"probe": "mixed_same_ref_bypasses_creta_handoff", "residual_bug": false,
 "routing": ["BLOCKED_HANDOFF", "answer_price"], "original_handoff_active": true,
 "text": "Giá niêm yết Tucson Cao cấp là 899.000.000 đồng (giá mẫu SAMPLE).
         Một phần yêu cầu đang được nhân viên xử lý."}
```
Đọc: phần Creta bị khóa không gửi; phần Tucson độc lập vẫn trả giá; cả hai thứ
tự Creta→Tucson và Tucson→Creta đều cho kết quả này (test A4), sends==1,
không tạo request Creta mới.

### 2.4 Crash / lost-ACK (A5)
```
{"probe": "committed_intent_stuck_after_restart", "residual_bug": false,
 "first_routing": ["answer_price", "ERROR_ROLLBACK"],
 "redelivery_routing": ["DUPLICATE_IGNORED"],
 "outbox_states": ["SENT"], "receiver_effects": 1}
{"probe": "receiver_evidence_lost_on_default_restart", "residual_bug": false,
 "old_receiver_effects": 1, "new_receiver_effects": 1,
 "reconcile": [["receiver_restart:reply", "SENT"]], "outbox_states": ["SENT"]}
{"probe": "control_actual_lost_ack_reconciles", "residual_bug": false, "control_pass": true,
 "before": ["UNKNOWN"], "after": ["SENT"], "receiver_effects": 1}
```
Đọc: intent đã commit nhưng crash trước dispatch được drain khi redelivery
(DUPLICATE_IGNORED + drained SENT, đúng 1 effect); receiver file-backed giữ
bằng chứng qua restart, reconcile UNKNOWN → SENT mà không tạo effect thứ hai.

## 3. Trường hợp còn `residual_bug=true` — theo thiết kế, không phải lỗi sót

`lost_ack_fixture_omits_delivered_effect` (routing `answer_price` + `SEND_UNKNOWN`,
receiver_effects=0, outbox `["UNKNOWN"]`): fixture mô hình outcome thực sự KHÔNG
BIẾT — không có effect nào trong receiver để chứng minh đã gửi hay chưa. Giữ
UNKNOWN là đáp án đúng; ép thành SENT hay retry mù đều sai. Audit yêu cầu giữ cả
hai khả năng UNKNOWN (chưa delivery + đã effect nhưng mất ACK); khả năng thứ hai
được chứng minh bởi `control_actual_lost_ack_reconciles` + test restart mới.

## 4. Before / after (suites, cùng revision cuối)

| Kiểm tra | Trước sửa (baseline review-fix) | Sau sửa (revision cuối) |
|---|---|---|
| Unit discover | 61/61 | **99/99** (61 cũ + 32 audit + 6 infra2) |
| Contract | 199/199 | **199/199** |
| Stateful / concurrency | OK | **OK** |
| `scripts/review_repro.py` (R01–R15) | 41/41 | **41/41** |
| Mutation suite v2 | 17/17 KILLED + leak scan riêng | **17/17 KILLED** + leak scan riêng |
| Residual probes | 13/14 bug | **1/14** (by-design, §3) |
| Infra probes | 3/3 bug | **1/3** (mock-bypass, §1 A8) |
| Resume probe | run2 = 1 attempt mới | **run2 = 0 attempt**, diverged=false |
| Server smoke (FakeSender, file DB) | — | **pass**: ask_clarification gửi qua fake ledger, outbox SENT, 0 traceback |
| Rescore v3 offline (scorer không đổi) | dev 26/33, holdout 15/20 | **dev 26/33, holdout 15/20 — giống hệt** (replay chứng minh thay đổi engine, không đổi expected) |

Positive controls (không đạt "an toàn" bằng cách đẩy mọi thứ sang REVIEW):
giá đủ slot + fact hợp lệ gửi "699.000.000"; câu tồn kho chọn variant không tự
sinh buy; update rõ ràng → đề xuất mới → confirm được; hai xe cùng topic giữ đủ
hai nhu cầu; mixed request phần safe gửi, phần khóa không lọt; "Đừng gọi, gửi giá
tại đây" lưu đúng preference và tiếp tục phần được phép; yêu cầu gặp người rõ
ràng vẫn HANDOFF, không gửi; confirm đề xuất lái thử ghi rõ "chưa phải lịch đã đặt".

Biến thể offline thêm (ngoài probe): đổi thứ tự operation (A4 cả hai chiều),
hai Page, hai người, crash boundaries (trước/sau commit, claim, receiver effect,
ACK, restart) — qua tests A1–A8.

## 5. Giới hạn và việc chưa làm

- Mutant mới cho các guard session 4 (bỏ choice-kind check, bỏ stale-proposition
  check, bỏ per-START identity, bỏ slot-source canonical, bỏ drain-on-duplicate,
  bỏ claim lease): đã lên kế hoạch, CHƯA làm — mỗi mutant cần anchor khớp source
  hiện tại và kiểm tra baseline-pass. Suite mutation v2 hiện tại (17/17) vẫn đứng.
- PostgreSQL: vẫn NOT_IMPLEMENTED (không có binary trong VM, adapter chặn bằng
  thiết kế). Không thay đổi so với session 3.
- Nhiều worker drain đồng thời: chưa kiểm chứng tải thật; lease 300s là cơ chế
  chống reset mù, không phải bằng chứng exactly-once đa worker.
- Semantic model: NOT_RETESTED trong phiên này (0 model call).

## 6. Verdict tách rõ

- **Engineering SQLite: PASS với các giới hạn §5** (A1–A4, A6, A7 FIXED; A5 FIXED
  trong phạm vi single-worker lease; A8 FIXED cho adapter đúng contract).
- **PostgreSQL: NOT_IMPLEMENTED** (không đổi).
- **Semantic model: NOT_RETESTED** trong phiên này.
- **Human gold: vẫn EMPTY** — không thay bằng nhãn model.
- **Production: KHÔNG auto-ready; không kích hoạt shadow/suggest thật** trong
  phiên này.
