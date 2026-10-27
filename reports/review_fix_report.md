# Báo cáo sửa lỗi theo review độc lập R01–R15 (Session 3, 2026-10-24)

## 0. Provenance — đọc trước khi tin bất kỳ con số nào

- Gói `facebook-intent-independent-review.zip` **không có trên máy này** (đã quét
  toàn bộ `~/workspace`, media library và `~/workspace/fb-intent-review/` — không
  file nào chứa REPORT.md / VERIFICATION.json / probe của reviewer). Người dùng
  xác nhận lúc 12:23: "nó là codebase được zip lại thôi, có sẵn trong máy bạn đó".
- Vì vậy phiên này làm việc từ **mô tả R01–R15 trong tin nhắn 12:19 của người dùng**
  cộng với codebase trên máy. Các probe tái hiện là **tự tái tạo từ mô tả R**
  (`scripts/review_repro.py`, 41 probe), KHÔNG PHẢI script gốc của reviewer, và
  được ghi rõ như vậy trong docstring của script. Quy ước exit ngược với reviewer:
  probe exit 0 = đã sửa xong, exit 1 = còn lỗi tái hiện được.
- **Không gọi model nào trong phiên này.** Ngân sách cộng dồn giữ nguyên
  276/300 calls, ~$0.0854/$2.00 (24 calls còn lại KHÔNG dùng).
- Không gửi Facebook thật, không deploy/merge production. FakeSender vẫn là
  transport duy nhất.

## 1. Đóng băng baseline

- File: `runs/baseline_freeze_pre_reviewfix.json` — SHA-256 đầy đủ của 430 file
  (trừ `__pycache__` và `runs/sandbox.db*`).
- Baseline trước sửa chạy xanh: contract 199/199, unit OK, mutation 11/11 (v1 —
  con số này sau đó được chứng minh là sai, xem R11).
- Baseline reproducer: **32 REPRODUCED / 9 FIXED** (9 là positive counterpart
  và hành vi đã đúng, phải giữ nguyên).

## 2. Bảng R01–R15

| ID | Trạng thái | File / hàm chính | Bằng chứng |
|----|-----------|------------------|------------|
| R01 pending/request ownership, scope, qtype/slot/proposition | FIXED | `app/engine.py::_validate_pending`, `_resolve_ref`, `_validate_ops` | repro R01: 7/7 fixed (cross-pending, mismatch, cross-page, confirm-slot, confirm-"không", reject-answered, topic-tie); regression `tests/unit/test_review_fixes.py::TestReviewPendingLink` |
| R02 tách kiểm chứng khỏi ghi state | FIXED | `app/engine.py::process` (validation phase → business SAVEPOINT; rollback khi REVIEW/BLOCKED) | repro R02: 4/4 fixed; contract `conditional_stock_then_book` giữ requests OPEN dưới REVIEW_AMBIGUOUS (xem §3 về ngoại lệ ambiguity) |
| R03 durable outbox + dispatcher claim + delivery identity | FIXED | `app/state.py` (outbox INTENDED→CLAIMED, `FakeReceiver` riêng connection), `app/engine.py::_dispatch`, `reconcile_unknown` | repro R03: crash-after-effect = 1 effect / 0 SENT row rồi reconcile chốt; double-dispatch = 1 effect; lost_ack giữ UNKNOWN tới khi có ACK muộn |
| R04 HANDOFF scope/lifecycle | FIXED | `app/state.py::handoff_for_scope`, `mark_handoff_overdue`; `app/engine.py::release_handoff`, `_sweep_expired`, `_turn_identity` | repro R04: 5/5 fixed; quá 48h → OVERDUE, không auto-release; release phải gọi rõ ràng + audit; scope theo topic+vehicle/variant (wildcard khi thiếu thông tin) |
| R05 (pending/request quan hệ — cùng nhóm R01) | FIXED | như R01 | pending phải thuộc đúng request, còn OPEN, chưa hết hạn, chưa supersede |
| R06 (CONFIRM đúng loại — cùng nhóm R01) | FIXED | `app/engine.py::_validate_pending` | CONFIRM chỉ cho pending yes_no; CONFIRM trên "không" → REVIEW_CONFIRM_DENIED; CONFIRM khi text mơ hồ → REVIEW_AMBIGUOUS; không có chuyển READY từ semantic CONFIRM suông |
| R07 facts resolver nhất quán | FIXED | `app/facts.py::is_fresh`, `verified_at` semantics; `app/engine.py::_plan` (OTR/loan/stock/promo) | repro R07: 6/6 fixed (fee conflict → HANDOFF, stock 0 → "hết hàng", stock conflict → HANDOFF, verified_at=None không còn vô hạn TTL, stale-at-dispatch → BLOCKED_AT_DISPATCH) |
| R08 preferences đa kênh | FIXED | `app/state.py::contact_blocks` (bảng mới, set semantics), `app/semantics.py::CONTACT_SCOPES` | repro R08: 3/3 fixed (block kênh thứ hai không ghi đè kênh đầu; allow_all validator chấp nhận; opt-in khi đang bị chặn ghi nhận được) |
| R09 schema theo operation + provenance | FIXED | `app/semantics.py::OP_SPEC`, `SLOT_KINDS`, scope/polarity checks; `app/engine.py` provenance phase | repro R09: 3/3 fixed (unknown slot kind reject; out_of_scope START reject; slot kế thừa không tự explicit=True/source=current) |
| R10 claim binding typed | FIXED | `app/renderer.py::claim_check_typed`, wired vào `app/engine.py::process` | repro R10: swapped Creta/Tucson prices bị bắt cả hai chiều; correct mixed vẫn pass |
| R11 mutation harness hợp lệ | FIXED | `tests/mutations/mutation_suite.py` v2 | 17/17 KILLED (v1 báo 11/11 là sai: gồm 1 mutant SQL invalid + 1 mutant SyntaxError + leak scan trong mẫu số); xem §4 |
| R12 scorer mới + chấm lại offline | FIXED | `scripts/scoring_v3.py`, `scripts/score_multiturn_v3.py` (scorer cũ giữ nguyên làm lịch sử) | Rescore §5; exit code: 0 pass / 1 fail / 2 structural |
| R13 resume/cache hash đầy đủ | FIXED | `scripts/cache_key.py`, `scripts/resume.py`, runners, `fmt_context` | repro R13 fixed (key v2 khác nhau theo comment và theo state); legacy checkpoint → cảnh báo + gọi lại, không reuse im lặng |
| R14 budget guard ledger cộng dồn | FIXED | `scripts/budget_ledger.py`, guards trong 2 runner (trước call và trước repair) | Unit `tests/unit/test_review_infra.py::TestBudgetLedger` (append-only, guard chặn vượt calls, unknown cost fail-closed). Chưa kiểm chứng bằng call thật (0 call trong phiên) |
| R15 hạ tầng/clock/policy/NOT_IMPLEMENTED | FIXED | `app/server.py` (WallClock), `app/policy.py`, `app/pg_adapter.py`, `config/policy.yaml`, `compose.yaml` comments, `tests/unit/test_policy.py` | repro R15: 3/3 fixed (server clock khác nhau giữa 2 lần gọi; policy module tồn tại + YAML khớp code; PG ghi nhãn NOT_IMPLEMENTED, connect() ném PostgresNotImplemented) |

Không có mục nào ở trạng thái PARTIAL/BLOCKED/NOT_FIXED. Hai giới hạn còn lại
được ghi ở §6, không phải là "chưa sửa" mà là việc không thể kiểm chứng trong
phạm vi phiên này.

## 3. Thay đổi contract có chủ ý (ghi rõ, không sửa expected lén)

Hai test cũ mâu thuẫn với semantics mới theo đúng yêu cầu R, nên được cập nhật
(cùng với lý do trong comment của test), không phải để "tăng điểm":

1. **Handoff quá SLA không tự release (R04).** Test cũ
   `test_handoff_expiry_and_release` kỳ vọng sweep tự release sau 48h. Contract
   mới: sweep chỉ đánh OVERDUE, bot vẫn BLOCKED_HANDOFF cho tới khi staff gọi
   `release_handoff()` rõ ràng (có audit). Test đã viết lại theo contract mới.
2. **UNKNOWN reconcile bằng receiver ledger (R03).** `lost_ack` cũ không để lại
   trace nên reconcile giữ UNKNOWN tới khi `confirm_delivery`. Thiết kế mới:
   `lost_ack` = kết quả không xác định nên sender KHÔNG ghi effect; reconcile giữ
   UNKNOWN; ACK muộn ghi effect rồi reconcile chốt SENT. Test đã viết lại.
   (Ngoài ra `FakeSender` cũ ghi effect trước rồi báo lost_ack — mâu thuẫn với
   chính định nghĩa "không xác định" — đã sửa.)
3. **Ambiguity giữ START/UPDATE, rollback destructive ops (R02, tinh chỉnh).**
   Lần đầu áp "ambiguity rollback toàn bộ" làm vỡ contract holdout
   `conditional_stock_then_book` (kỳ vọng stock+testdrive ở OPEN, sends=0,
   routing REVIEW_AMBIGUOUS). Quy tắc cuối: sarcasm → rollback toàn bộ (meaning
   không tin cậy); ambiguity → giữ START/UPDATE (câu hỏi làm rõ tham chiếu
   chính các request này), nhưng rollback toàn bộ nếu lượt có op phá hoại
   (CANCEL/CONFIRM/REJECT). Contract trở lại 199/199.

Ba lỗi phát hiện thêm trong lúc sửa (không nằm trong mô tả R, đều có regression):

- `_apply_slot` với `slot.extra` bump version nhiều lần → `save_request` conflict
  → ERROR_ROLLBACK ở mọi op có extra. Sửa: bump đúng 1 lần mỗi `_apply_slot`.
- START không nêu identity wildcard-match request đang mở rồi để UPDATE sau đó
  ghi đè identity của nó (hỏi giá Tucson lại sửa request giá Creta). Sửa: chỉ
  reuse request đang mở khi START tự nêu vehicle/variant.
- START slot-less bị handoff chặn nhầm khi UPDATE sau đó trong lượt nêu xe khác.
  Sửa: kiểm tra handoff scope bằng identity cả lượt (`_turn_identity`).
- `Engine.release_handoff` không commit → process tiếp theo lỗi "cannot start a
  transaction within a transaction". Sửa: release commit ngay (durable).

## 4. Mutation suite v2 — vì sao 11/11 cũ là sai

- Mutant SQL cũ (`drop_state_version_check`) xóa placeholder mà giữ param →
  sqlite `ProgrammingError` (tool error), nhưng suite v1 đếm exit != 0 là DETECTED.
  V2 dùng mutant hợp lệ (xóa cả placeholder lẫn param) → KILLED thật.
- Mutant `ack_is_confirm_all` cũ chèn code sau `if ...: continue` một dòng →
  SyntaxError, cũng bị đếm DETECTED. V2 nhắm đúng nhánh `_apply_ops` → KILLED thật.
- Leak scan cũ nằm trong mẫu số 11/11; v2 báo cáo riêng (no leak).
- V2 thêm: baseline-pass check (check fail sẵn trên code sạch → BASELINE_FAIL,
  không tính vào mẫu số), compile check (→ INVALID), phân loại
  KILLED/SURVIVED/INVALID/BASELINE_FAIL riêng, xóa `__pycache__` cả sau restore.
- V2 thêm 7 mutant cho code mới (pending-link, confirm-slot, handoff-scope,
  stock-zero, fee-conflict, typed-claim, receiver-dedupe) + 9 test regression
  mới (`tests/unit/test_review_fixes.py`, `test_review_infra.py`, `test_policy.py`).
- Kết quả v2: **17/17 KILLED, 0 SURVIVED, 0 INVALID, 0 BASELINE_FAIL**,
  leak scan riêng: no leak. Hai mutant từng SURVIVED ở bản v2 đầu (owner 1 lớp
  bị lớp defense-in-depth còn lại chặn; blind-retry nhắm nhầm nhánh) đã được sửa
  thành mutant đo đúng tính chất hệ thống + test assert cả nội dung (không chỉ
  đếm row, vì INSERT OR REPLACE vẫn ra 1 row).

## 5. Scorer v3 — chấm lại các run cũ OFFLINE (không gọi model)

Scorer cũ giữ nguyên làm lịch sử. Scorer v3 (`scripts/scoring_v3.py`,
`scripts/score_multiturn_v3.py`): không zip(), match request theo identity
(từ chối latest-pick khi còn >1 ứng viên), final-state ảnh hưởng verdict +
exit code, send precision/recall tách riêng, provenance theo comment_id thật
trong run, ASK-necessary theo state trước lượt + đúng request, API/schema
failure tính vào availability và e2e, raw/adjusted/post-hoc tách riêng
(không có file adjudication thì adjusted == raw, post-hoc rỗng).

| Run cũ | Scorer cũ (session 2) | Scorer v3 (raw, không adjudication) |
|--------|----------------------|-------------------------------------|
| mt_dev_v3 (dev, sau Loop 7) | 30/33 turns (final không tính exit) | **26/33 turns**, send precision 28/28, send recall 28/30, final-state 6/10, availability 31/33, exit 1 |
| mt_dev_v2 (dev, trước Loop 7) | 22/33 | **22/33**, precision 29/30, recall 29/30, final 4/10, availability 33/33, exit 1 |
| mt_holdout_v2 (holdout, chạy 1 lần) | raw 17/20, adjudicated 19/20 (post-hoc) | **15/20 raw**, precision 18/18, recall 18/19, final 5/6, availability 20/20, exit 1 |

Đọc đúng các con số này: v3 **nghiêm hơn** scorer cũ theo thiết kế (từ chối
latest-pick khi expected mơ hồ, final-state tính vào verdict, API error tính
vào mẫu số), nên cùng một run cũ chấm thấp hơn — đó KHÔNG PHẢI regression của
engine. Các fail "ambiguous match" (mt_dev_04/06/07, mt_hold_05) là expected của
dataset còn mơ hồ khi nhiều request cùng topic+identity, scorer từ chối đoán —
cần bổ sung identity/status vào expected nếu muốn chấm tiếp, không được sửa
scorer để đoán lại. Các fail "request_ref not found: q_..." ở mt_dev_v3 là lỗi
của model (Luna đưa pending-id làm request_ref), tính vào availability 31/33
đúng theo R12. Output chi tiết: `runs/mt_dev_v3_v3score.json`,
`runs/mt_dev_v2_v3score.json`, `runs/mt_holdout_v2_v3score.json`.
**Không có adjudication mới nào được áp trong phiên này** (post-hoc = 0).

## 6. Giới hạn còn lại (không tuyên bố quá)

1. **PostgreSQL vẫn BLOCKED, và lần này là BLOCKED ở việc viết adapter** (không
   chỉ thiếu Docker): `app/pg_adapter.py::connect()` ném `PostgresNotImplemented`,
   `compose.yaml` ghi rõ service app chạy SQLite và không có Dockerfile.
   Mọi kết quả kèm theo vẫn là SQLite.
2. **Ngữ nghĩa model chưa được kiểm chứng lại** (không gọi Luna/Jev trong phiên
   này theo yêu cầu): prompt chỉ thay đổi tối thiểu ở vocabulary scope
   (`allow_*` cho R08, để model có thể phát ra opt-in) — thay đổi này CHƯA được
   chạy live, cache key v2 sẽ làm checkpoint cũ miss và gọi lại khi chạy tiếp.
   Không tuyên bố auto-ready, không shadow production từ các sửa engine này.
3. **Budget ledger chưa có entry thật**: ledger file chưa tồn tại trước phiên
   này (mọi call cũ đều pre-ledger); guard cộng dồn chỉ có hiệu lực đầy đủ từ
   các run sau, cộng với `--prior-calls/--prior-cost` của multiturn runner.
4. **H2T1 ambiguity over-flag (Loop 9 draft) và các gap holdout cũ** (nhóm
   5/7/8 dev-only, `canon_price_kind` compound, `api_price_otr_0` excerpt) nằm
   ngoài phạm vi R01–R15 nên không sửa trong phiên này.
5. `runs/contract_run.json` và 4 contract checkpoint được tái tạo khi chạy lại
   suite (output làm việc, không phải bằng chứng lịch sử). Mọi run JSON lịch sử
   (api_*, mt_*), reports session 1/2, datasets và gói review đều nguyên vẹn
   theo đối chiếu freeze (§7).

## 7. Đối chiếu với freeze + kết quả kiểm thử cuối

- Đối chiếu `runs/baseline_freeze_pre_reviewfix.json`: các file thay đổi đúng
  phạm vi sửa (app/engine, facts, renderer, semantics, server, state,
  config/policy.yaml, compose.yaml, tests, 2 runner, contract outputs tái tạo).
  Không file report/dataset/run lịch sử nào thay đổi. File mới: app/policy.py,
  app/pg_adapter.py, scripts/cache_key.py, budget_ledger.py, resume.py,
  scoring_v3.py, score_multiturn_v3.py, tests/unit/test_review_fixes.py,
  test_review_infra.py, test_policy.py, scripts/review_repro.py,
  runs/*_v3score.json, reports/review_fix_report.md.
  (`runs/sandbox.db*` đã nằm trong exclusion của freeze; file dev DB này đã
  xóa trước smoke test và server tạo lại khi cần.)
- Reproducer tự tái tạo: **41/41 FIXED, 0 REPRODUCED**
  (`python3 scripts/review_repro.py`).
- Contract: **199/199** (dev 178/178, holdout 21/21; multi_turn 74, multi_user 22,
  error_race 15).
- Unit: **toàn bộ discover OK** (gồm 9 test review-fixes, 11 test infra,
  2 test policy).
- Mutation v2: **17/17 KILLED** + leak scan riêng no leak.
- Recovery (`tests/stateful`): OK. Race (`tests/concurrency`): OK.
- Server smoke (WallClock, port 8765): `/health` ok, POST /event trả
  `answer_price` 699.000.000 đúng, sender fake-only.
- Diff vs baseline: `reports/review_fix_diff.txt` (diff từ source Loop 8 trong
  gói review zip sang source hiện tại, giới hạn app/config/scripts/tests).

## 8. Bàn giao

1. Source candidate mới: `facebook-intent-sandbox-reviewfix.zip` (+ sidecar
   `.sha256`; SHA-256 đầy đủ trong file đính kèm; compose credentials trong
   bản copy xuất được thay bằng placeholder, file gốc trong project không đổi).
2. Diff: `reports/review_fix_diff.txt`.
3. Báo cáo này: `reports/review_fix_report.md`.
4. Scorer v3 outputs: `runs/mt_*_v3score.json`.
5. Không merge/deploy, không gửi Facebook thật, không dùng 24 calls còn lại.
