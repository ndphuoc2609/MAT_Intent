# REVIEW_GUIDE — facebook-intent-sandbox (code review package)

> Gói này xuất từ `~/workspace/facebook-intent-sandbox/` để review trực tiếp, KHÔNG
> phải bản deploy. Không có code gửi Facebook thật trong `app/`; sender duy nhất là
> `FakeSender` trong cùng process. Mọi giá/ưu đãi trong seed đều là SAMPLE giả lập.

## 0. Phiên bản được xuất, thời gian, cách chạy

- **Revision: candidate cuối Session 2, sau Loop 8.** Đã xác minh bằng hash với
  `runs/baseline_freeze_v8.json`: toàn bộ 21 hash đóng băng đều khớp — 9 file
  `app/*.py`, `config/schema_versions/semantic_v1.json`, 4 dataset
  (`datasets/contract_regression/scenarios.jsonl`,
  `datasets/development/api_cases.jsonl`, `datasets/multiturn/dev.jsonl`,
  `datasets/multiturn/holdout.jsonl`), 7 script scorer/runner. Không có file logic
  nào thay đổi sau freeze v8.
- Thay đổi duy nhất sau freeze v8 (đều là tài liệu, không phải logic):
  `reports/final_report_v2.md`, mục session 2 trong `CHECKPOINT.md`, và text template
  MANIFEST trong `scripts/generate_datasets.py` (đã kiểm chứng: regenerate cho ra
  `scenarios.jsonl` byte-identical với freeze v8). File này (`REVIEW_GUIDE.md`) là
  tài liệu mới thêm vào gói, không sửa logic.
- **Thời gian đóng gói:** 2026-10-24 (giờ Việt Nam, UTC+7).
- **Môi trường đã chạy:** Python 3.12.3, SQLite 3.45.1, stdlib-only
  (`pyproject.toml`: `dependencies = []`, `requires-python >= 3.12`). Không có git
  repo nên revision được định danh bằng SHA-256 (xem `SHA256SUMS.txt` trong gói và
  `runs/baseline_freeze_v8.json`).
- **Cách chạy (offline, không cần credential):**
  ```bash
  cd facebook-intent-sandbox
  python3 scripts/generate_datasets.py          # regenerate 199 contract scenario + API cases
  python3 scripts/run_contract.py               # contract regression
  python3 -m unittest discover -s tests        # unit + recovery + race
  python3 tests/mutations/mutation_suite.py     # mutation harness
  python3 -m app.server                         # HTTP harness 127.0.0.1:8765, fake-only
  ```
  Các lệnh cần Luna thật (`scripts/run_api_eval.py`, `scripts/run_multiturn_eval.py`)
  cần connector `custom.openrouter` đã đăng ký + ngân sách; nhưng mọi số Session 2
  đều tái hiện được KHÔNG cần gọi model mới: `scripts/rescore_v2.py`,
  `scripts/replay_api.py`, `scripts/score_multiturn.py` chỉ đọc run JSON/checkpoint có
  sẵn trong `runs/`.
- **README.md trong gói đã lỗi thời** (ghi 181 scenario, mutation 8/8 — số Session 1).
  Số đúng của revision này: 199 scenario, mutation 11/11 (xem §11).

## 0b. Luồng tổng thể để lần theo (event → ledger)

`app/server.py: do_POST /event` → `app/engine.py: Engine.process()` →
`Engine.context()` (xây context) → semantic đã có (Luna, xem `app/adapters/luna.py:
call_luna`, SYSTEM prompt) → `app/semantics.py: validate_semantic()` →
`Engine._apply_ops()` (ops → state) → `Engine._plan()` từng request (facts qua
`app/facts.py: Facts.query`, claim qua `app/renderer.py: claim_check`) →
tách safe/HANDOFF → dispatch recheck → `Engine._send_reply()` →
`Store` outbox/ledger/audit (`app/state.py`). Harness offline tương đương:
`app/harness.py: make_engine()`, `run_scenario()`.

## 1. HANDOFF hết hạn 48h, release và quyền bot tiếp tục

- `app/engine.py`: `Engine.HANDOFF_TTL_HOURS = 48`; `Engine._sweep_expired()` chạy đầu
  mỗi `process()`: pending quá `expires_at` → EXPIRED (request WAITING_USER → OPEN
  nếu hết pending mở); handoff quá 48h → `Store.release_handoff()` + audit
  `"handoff_expired"`.
- `app/state.py`: `Store.handoff()` (chỉ lấy handoff có `released_at IS NULL`),
  `Store.add_handoff()`, `Store.release_handoff()`. Schema bảng `handoffs` cùng file.
- Điểm cần soi: sau khi handoff hết hạn/release, bot được xử lý lại request đó
  (không còn `BLOCKED_HANDOFF`); ngược lại, request đang bị handoff mà có op mới thì
  `_apply_ops()` trả `BLOCKED_HANDOFF` (trừ op human thứ hai idempotent trong cùng turn).
- Trace: contract `err_expired_pending`, `handoff_lock_blocks_send`
  (`datasets/contract_regression/scenarios.jsonl`, `runs/checkpoints/*.json`).

## 2. Resolve request_ref, topic:<topic>, pending ID, owner check, nhiều request cùng topic

- `app/engine.py: Engine._apply_ops()` → hàm lồng `resolve_ref(ref, op)`:
  (1) `by_ref` trong cùng turn; (2) request đã `touched` trong turn, xếp hạng bằng
  `_slot_score()`; (3) query mọi request của actor+topic theo `rowid DESC`, khớp
  identity bằng `_ident_match()` rồi fallback về request mới nhất cùng topic;
  (4) request id trực tiếp; (5) active requests; (6, Loop 8) **pending id → request
  của pending đó** qua `Store.get_pending()` — dung sai cho model điền pending id
  vào `request_ref` thay vì `topic:<topic>`.
- Owner check ngay sau resolve: `req["actor_id"] != actor` → `REVIEW_OWNER`.
  Ref hoàn toàn lạ → `REVIEW_BAD_REF` (xem contract `err_owner_mismatch`,
  `neg_not_buy` với CANCEL trên topic không có request mở).
- Nhiều request cùng topic: phân biệt bằng identity, không bằng topic (xem §4).
- Trace hai request cùng topic khác xe/phiên bản: `tests/unit/test_loop5.py:
  TestIdentityAndDispatch.test_price_two_vehicles_kept_separate`,
  contract `dup_start_two_vehicles`, multi-turn dev `mt_dev_04`
  (`runs/mt_dev_v3.json`), single-turn `api_dup_start_two_vehicles`
  (`runs/api_dev_v2.json`).

## 3. Ambiguity, CONFIRM/ANSWER/REJECT/CANCEL và mức cam kết của lịch hẹn

- Ambiguity gate: `app/engine.py: Engine.process()` — bất kỳ `uncertainties[]` nào có
  `different_action=true` → routing `REVIEW_AMBIGUOUS`, 0 send, không commit action.
- `Engine._apply_ops()`: `ANSWER_PENDING` (đáp slot cho pending, pending → ANSWERED),
  `CONFIRM_PENDING` (pending → CONFIRMED, testdrive → READY),
  `REJECT_PENDING` (pending → REJECTED, request → OPEN),
  `CANCEL_REQUEST` (pending mở → SUPERSEDED, request → CANCELLED).
- Mức cam kết lịch hẹn (quan trọng, đừng đọc quá): testdrive chỉ có 2 trạng thái
  gửi được — đề xuất `WAITING_USER` với text bắt buộc "(Đây là đề xuất, chưa phải
  lịch đã đặt.)" và `READY` sau confirm với text "nhân viên sẽ xác nhận lại lịch
  (chưa phải lịch đã đặt)". Không có trạng thái "đã đặt lịch thật" trong engine này.
- Vấn đề mở đã biết: sau REJECT_PENDING, `_plan()` đề xuất lại cùng slot thay vì hỏi
  giờ khác (xem `reports/final_report_v2.md` §8 mục 3).
- Trace H2T1 ambiguity: `runs/mt_holdout_v2.json` hội thoại `mt_hold_02` turn 1
  ("9 giờ sáng" → `REVIEW_AMBIGUOUS`, 0 send, hồi phục ở turn "oke" sau đó);
  checkpoint `runs/mt_checkpoints_v2/mt_hold_02_1.json`.

## 4. Identity/dedupe START_REQUEST, webhook dedupe và reopen request

- Identity: `Engine._apply_ops()` → `_op_identity(op)` (vehicle/variant từ slot hoặc
  `slot.extra`) và `_ident_match(req, topic, veh, var)`. START dedupe hai lớp:
  cùng turn (`touched`) và cross-turn (`Store.active_requests()` = status OPEN /
  WAITING_USER / READY). Khác xe/phiên bản → request mới; cùng identity đang mở →
  tái dùng; request COMPLETED/CANCELLED không nằm trong active nên mở mới hợp lệ
  không bị dedupe nhầm (contract `reopen_after_completed`).
- Reopen: `if req["status"] == "COMPLETED" and t in ("UPDATE_SLOT",
  "ANSWER_PENDING")` → status OPEN + version+1 rồi mới `_apply_slot()` (trong
  `_apply_ops()`); đổi vehicle vô hiệu hoá variant/color phụ thuộc
  (`Engine._apply_slot()`).
- Webhook/event dedupe: `Engine.process()` kiểm tra `event_dedupe_key` trong bảng
  `events` → trùng → `DUPLICATE_IGNORED`, không thêm request/effect
  (contract `err_duplicate`).
- Trace: contract `dup_start_two_vehicles`, `dup_start_two_variants_price`,
  `dup_start_stock_two_vehicles`, `reopen_after_completed`, `loop7_reopen_completed_update`.

## 5. Contact preference, mixed request và quyền gửi từng phần

- `Engine._apply_ops()`: `SET_CONTACT_PREFERENCE` — `scope="allow_all"` xoá pref;
  ngược lại `Store.set_pref(page, actor, scope)` (scope `"call"` = đừng gọi).
  Nhiều scope khác nhau trong cùng turn → `REVIEW_PREF_CONFLICT`, không commit pref nào.
- Mixed split trong `Engine.process()`: mỗi request được `_plan()` riêng rồi chia
  `safe` (AUTO_ACTIONS) / `ho` (HANDOFF) / `draft_safe` (human draft do model đề xuất
  nhưng plan auto-safe). Chỉ phần safe được gửi (một reply gộp); phần handoff chỉ
  vào outbox `handoff_draft` (NEEDS_REVIEW) kèm câu báo trong cùng reply.
  Khách yêu cầu người rõ ràng (`human_source=customer_explicit`) hoặc policy →
  HANDOFF toàn phần, không nằm trong safe.
- Quyền gửi từng phần ở dispatch: kill-switch hoặc pref `("all", "public_reply")`
  → `BLOCKED_AT_DISPATCH`, 0 send. Pref `"call"` KHÔNG chặn reply công khai.
- Trace: contract `mixed_pref_price_complaint`, `service_warranty_mixed`,
  multi-turn dev `mt_dev_05` (rút một nhu cầu), `mt_dev_07` (ngừng gọi nhưng vẫn
  nhận thông tin), `runs/mt_dev_v3.json`.

## 6. Facts selection, conflict, TTL/version và kiểm tra lại trước dispatch

- `app/facts.py`: `SEED_FACTS` (18 fact chính + 2 spec; `f_warranty` = 5 năm;
  KHÔNG có fact số điện thoại tư vấn — đó là lý do nhu cầu xin số chỉ ra draft).
  `Facts.query(ftype, vehicle, variant, location, extra, now)` → `(fresh, stale,
  conflict)`: fresh = trong khoảng `valid_from/to` VÀ trong TTL tính từ
  `verified_at` (nếu đặt); fresh sort theo `id` (độc lập thứ tự bản ghi, không dừng
  ở fact expired đầu tiên); conflict = >1 cặp (value, unit) distinct trong fresh.
  `Facts.version` + `touch()` để dispatch recheck.
- `Engine._plan()` ghi provenance vào `req["_used_facts"]` cho từng nhánh (price
  list/OTR gồm cả `calc:otr_v1`, promo, loan, stock, spec theo attribute, dealer,
  warranty, registration). Công thức OTR: `app/facts.py: calc_otr()`
  (list + list×reg% + plate + road).
- Dispatch recheck trong `Engine.process()` (sau khi `dispatch_hook()` chạy):
  facts version đổi → `BLOCKED_STALE_FACTS`; version request safe đổi →
  `BLOCKED_STALE_STATE`; request safe đã có handoff → `BLOCKED_HANDOFF_AT_DISPATCH`.
- Trace: contract `promo_expired`, `facts_conflict`, `handoff_lock_blocks_send`;
  unit `tests/unit/test_loop5.py` (facts đảo thứ tự, TTL hết hạn, conflict).

## 7. Claim binding theo fact/object/attribute/value/unit

- `app/renderer.py: claim_check(text, allowed_numbers, used_facts=None,
  allowed_vehicles=None)`: (1) số trong text phải nằm trong allowed (giữ hỗ trợ
  biểu diễn 2 và 2.0); (2) nếu có `used_facts`: tên xe trong text phải thuộc
  allowed_vehicles (xe của used-facts ∪ request vehicle ∪ compare_vehicle);
  (3) cặp số+đơn vị (đồng/%/năm/L/xe) phải khớp một used-fact (value, unit);
  (4) cặp attribute spec (engine/seats/power_hp) phải khớp attribute của used-fact.
- Chuỗi chứng minh: `response_plan.facts_used` (fact IDs) + `req["_used_facts"]`
  (fact_id → vehicle → variant/location → attribute → value/unit) → claim trong text.
  Claim lỗi → routing `REVIEW_CLAIM`, không gửi.
- Negative tests: `tests/unit/test_loop5.py: TestClaimProvenance` (số Tucson gắn vào
  Creta, đúng số sai đơn vị, số có trong DB nhưng không thuộc facts đã chọn, đúng
  giá sai attribute). Mutation tương ứng: `drop_vehicle_claim_check`.

## 8. UNKNOWN delivery, reconcile và crash/restart

- `app/state.py: FakeSender`: `fail_mode="lost_ack"` → `send()` lưu payload vào
  `pending_deliveries`, trả UNKNOWN; `confirm_delivery(key)` → chuyển sang sent +
  tập `confirmed`. `Engine._send_reply()` map kết quả thật thành outbox SENT/UNKNOWN;
  UNKNOWN → routing `SEND_UNKNOWN`, `outbound_permission=False`, không tính là send.
- `Engine.reconcile_unknown()`: UNKNOWN chỉ → SENT khi `dedupe_key` của payload nằm
  trong `sender.confirmed`; không có xác nhận → giữ UNKNOWN, KHÔNG retry mù.
  Redelivery cùng event → `DUPLICATE_IGNORED` (không gửi trùng).
- Crash/restart: `Store` dùng `BEGIN IMMEDIATE` + optimistic version trên request
  (`version` check khi save) và pending (`update_pending` raise khi version conflict);
  xem `tests/stateful/test_recovery.py`, `tests/concurrency/test_race.py`.
  Lưu ý: crash/restart trên SQLite đã có test; trên PostgreSQL thì chưa (xem §11).

## 9. price_kind, thời gian và đề xuất lại sau khi khách từ chối

- `app/normalize.py: canon_price_kind()` + `PRICE_KINDS` (niêm yết→list, lăn bánh→otr,
  có strip tiền tố "giá "). Thứ tự hỏi trong `Engine._plan()` nhánh price:
  vehicle → price_kind → variant (chỉ hỏi khi conflict nhiều fact giá và chưa có
  variant; Creta có 2 fact nên hỏi, Tucson chỉ 1 fact nên không hỏi) → location
  (chỉ cho OTR).
- Thời gian: `app/normalize.py: parse_time_expression(text, now)` trả
  `("exact", dt) | ("range", (start, end, label, options)) | ("time_only", hour) |
  ("past", dt) | ("vague", reason)`, timezone Asia/Ho_Chi_Minh.
  "thứ ba tuần sau" (từ thứ bảy 24/10/2026) → 27/10; "cuối tuần này" → range thứ 7–
  chủ nhật kèm day options; "sáng mai" → 9h hôm sau; bare "chiều" → time_only rồi
  engine kết hợp với ngày của đề xuất hiện có, nếu không có ngày thì ghi marker
  `datetime_unresolved` và hỏi lại. Giờ mặc định (sáng 9h, chiều 15h, khác 10h)
  CHỈ là đề xuất, text phải ghi rõ chưa phải lịch đã đặt.
- Đề xuất lại sau từ chối: `REJECT_PENDING` → request OPEN → `_plan()` đề xuất lại
  cùng slot (vấn đề mở, §3). Phân biệt với `CANCEL_REQUEST` (dứt hẳn nhu cầu).
  Prompt phân biệt hai trường hợp này: `app/adapters/luna.py` SYSTEM
  (mục CANCEL vs REJECT).
- Trace: contract `time_next_tuesday`, `time_weekend_range`, `time_month_end_vague`,
  `time_change_afternoon`, `loop7_canon_variant_prefix` (canon "Bản tiêu chuẩn").

## 10. Scorer strict/adjusted, post-hoc adjudication và cách tính auto precision

- `scripts/scoring.py`: `routing_acceptable()` = strict (routing == expected) HOẶC
  case khai báo `allowed_routing` HOẶC quy tắc v2: routing == ASK, expected == AUTO,
  và câu hỏi của engine đòi một slot bắt buộc mà semantic không có
  (`ask_necessary()`: map câu hỏi → price_kind/location/vehicle/variant/datetime).
  `rescore()` trả `routing_strict`, `routing_adjusted_v2`, `sent_reply_precision`,
  `effect_precision`, `auto_wrong_ids`. `request_counts()` chỉ trả counts
  (n_expected/n_pred/n_matched theo multiset topic) — counts KHÔNG phải độ đúng
  nội dung từng request.
- Post-hoc adjudication (phải đọc kỹ, đừng lấy số adjusted làm số strict):
  số adjusted 0.972 của Session 1 là post-hoc vì ASK chưa từng là alternative khai
  báo; scorer v2 sửa việc này bằng cách áp MỘT quy tắc versioned cho cả baseline
  lẫn candidate. Hai adjudication khác trong Session 2, đều có bằng chứng trong
  `reports/final_report_v2.md` §5: (a) topic_exact so multiset `['spec','spec']` với
  pred set `['spec']` (engine trả đúng cả hai nhu cầu); (b) `api_time_next_tuesday`
  xung đột nhãn (api kỳ vọng ASK, contract kỳ vọng đề xuất giờ mặc định).
- Auto precision: mẫu số = các turn/case được route AUTO trong tập scored; tử số =
  các turn đó đúng toàn bộ (topic đúng + không có side-effect thừa). Riêng
  `sent_reply_precision` trong scorer cũ đếm theo topic nên một ca có text đúng
  nhưng effect thừa (neg_variant_change bản cũ) vẫn bị tính sai — báo cáo v2 phân
  biệt rõ "text đúng" với "fully-correct auto".
- Multi-turn scorer: `scripts/score_multiturn.py: check_turn()` — per-turn check
  (status, slots subset, sends, routing_contains, response_contains/forbidden,
  has_pending, prefs, ledger purposes) + linkage pending (op tham chiếu pending phải
  là pending đang mở trước lượt) + provenance slot (source_message_id thuộc về
  comment trong hội thoại) + final-state check. HANDOFF/IGNORE không tính là pass
  chung chung; mẫu số 0 ghi N/A (không có trong dữ liệu này).
- Chạy lại số Session 2 không cần model (xem §11).

## 11. Lệnh offline đã xác nhận, output kỳ vọng, phần chưa kiểm chứng

Đã xác nhận trên revision Loop 8 (2026-10-24, Python 3.12.3, SQLite 3.45.1):

| Lệnh | Output kỳ vọng |
|---|---|
| `python3 scripts/generate_datasets.py` | 199 scenario; api dev 109 / holdout 16 |
| `python3 scripts/run_contract.py` | 199/199 (dev 178, holdout 21, multi_turn 74, multi_user 22, error_race 15) |
| `python3 -m unittest discover -s tests` | OK (36 unit Loop 5 trở về trước + 3 Loop 8) |
| `python3 tests/mutations/mutation_suite.py` | PASS 11/11 + leak scan (grep `expected_final` trong `app/` = không thấy) |
| `python3 scripts/rescore_v2.py runs/api_dev_b3.json runs/api_dev_b3_replay2.json` | baseline strict 0.889/adjusted 0.972; replay2 strict 0.917/adjusted 0.972 (cùng scorer v2, 36 scored) |
| `python3 scripts/rescore_v2.py runs/api_dev_v2.json runs/api_dev_v2_replay_loop7.json` | cả hai strict 0.914/adjusted 0.971 (Loop 7 không regression single-turn) |
| `python3 scripts/score_multiturn.py runs/mt_dev_v3.json datasets/multiturn/dev.jsonl` | 30/33 lượt, auto 27/30, pending link 2/2 |
| `python3 scripts/score_multiturn.py runs/mt_holdout_v2.json datasets/multiturn/holdout.jsonl` | 17/20 lượt raw (adjudicated 19/20, xem §12), auto 16/19, pending link 3/3 |
| Server smoke: `python3 -m app.server`, POST /event (fixture semantic giá Creta Cao cấp niêm yết), GET /ledger | 200, routing `answer_price`, text 699.000.000 đồng (SAMPLE); ledger row `reply`/`SENT` |

Chưa kiểm chứng (đừng kết luận ngược lại từ việc thiếu bằng chứng):
- **PostgreSQL: CHƯA CHẠY.** `compose.yaml` (postgres:16 + app) chưa từng được thực
  thi trong VM này (không có Docker/PG binary). Mọi kết quả trên là SQLite
  (BEGIN IMMEDIATE + optimistic version). Transaction/race semantics trên PG cần chạy
  lại suite trên PG trước khi kết luận.
- **Human gold: TRỐNG** (`datasets/human_gold/README.md`). Mọi metric Luna đều là
  nhãn synthetic (contract + scripted multi-turn), không phải human gold.
- **Facebook thật, webhook, kill-switch drill, Facts DB v2:** chưa có. Gate B =
  INSUFFICIENT_EVIDENCE, Gate C = BLOCKED (xem `reports/final_report_v2.md` §7).
- Server smoke dùng fixture semantic (không gọi Luna); server DB runtime
  (`runs/sandbox.db*`) KHÔNG nằm trong gói — reviewer tự tạo lại bằng smoke trên.

## 12. Chỉ dẫn tới trace của 5 ca bắt buộc

1. **service_warranty** — `runs/api_dev_v2.json` (case `api_service_warranty`:
   HANDOFF → AUTO, text 5 năm + draft note); checkpoint
   `runs/api_checkpoints_v2/api_service_warranty.json`; multi-turn
   `runs/mt_dev_v3.json` (`mt_dev_06`, 3 lượt: policy → contact draft → explicit
   human) và `runs/mt_holdout_v2.json` (`mt_hold_05`); contract
   `service_warranty_mixed`; phân tích `reports/final_report_v2.md` §2.
2. **neg_variant_change** — `runs/api_targeted_v2.json` (1 fresh call sau Loop 6:
   chỉ còn stock request, text "Còn Creta Tiêu chuẩn màu Trắng tại Hà Nội");
   checkpoint `runs/api_checkpoints_v2/api_neg_variant_change.json`; bản sai cũ
   trong `runs/api_dev_b3_replay2.json` (buy request thừa → HANDOFF draft, reply
   text thì đúng); phân tích §2–§3 báo cáo v2.
3. **Ca auto sai của Session 1** — chính là neg_variant_change (mục 2); scored AUTO
   25 ca trong `runs/api_dev_b3_replay2.json` (27 raw trừ 2 excluded
   `api_facts_conflict`, `api_promo_expired` — hai ca này AUTO trong replay vì input
   API thiếu điều kiện contract, xem §3 báo cáo v2). Rubric: reply đúng, hại người
   dùng thấp, hại nội bộ trung bình-thấp (draft thừa), không phải lỗi nghiêm trọng.
4. **H2T1 ambiguity** — `runs/mt_holdout_v2.json` hội thoại `mt_hold_02` turn 1
   ("9 giờ sáng" → `REVIEW_AMBIGUOUS`, 0 send; uncertainties
   `different_action=true` dù cả hai cách hiểu cùng dẫn tới đề xuất 09:00 đang chờ);
   checkpoint `runs/mt_checkpoints_v2/mt_hold_02_1.json`; hồi phục ở turn 2 ("oke" →
   `answer_confirmation`) và turn 3 ("Hủy đi" → CANCELLED). Draft sửa Loop 9
   (chưa áp) ở §8 báo cáo v2.
5. **Hai request cùng topic khác xe/phiên bản** — `tests/unit/test_loop5.py:
   TestIdentityAndDispatch.test_price_two_vehicles_kept_separate` (Creta WAITING_USER
   + Tucson COMPLETED, 2 request riêng); contract `dup_start_two_vehicles`
   (spec Creta + Tucson) và `dup_start_two_variants_price`; multi-turn
   `runs/mt_dev_v3.json` (`mt_dev_04`: hai người, hai xe, slot/pending riêng);
   single-turn `runs/api_dev_v2.json` (`api_dup_start_two_vehicles` — lưu ý scorer
   artifact multiset, xem §10).

## 13. Ngân sách và vòng sửa (để reviewer biết giới hạn tái hiện bằng model thật)

- Cộng dồn: 276/300 calls, $0.0854/$2.00 (Session 1: 148/$0.0392;
  Session 2: 128/$0.04618). Còn 24 calls, $1.9146. Vòng sửa: 8/12.
- Model: `openai/gpt-6-luna` qua connector `custom.openrouter` (canonical
  `openai/gpt-6-luna-20260922`, prompt $0.10/1M, completion $0.50/1M).
  Temperature 0.0 (`app/adapters/luna.py: call_luna`). Checkpoint key =
  sha256(input + SYSTEM + schema + MODEL) — resume chỉ khi key khớp
  (`scripts/run_api_eval.py`, `scripts/run_multiturn_eval.py`).
- Budget ledger: xem `runs/baseline_freeze_v8.json` (mục `budget_before_holdout`)
  và tổng kết ở `reports/final_report_v2.md` §1/§9. Mọi retry/repair/judge đều
  tính vào tổng — reviewer chạy thêm model call cần cộng dồn, không reset ngân sách.

## 14. Danh sách redaction và loại trừ (bản xuất ≠ byte-for-byte bản gốc ở 1 file)

- **Redaction nội dung (chỉ trên BẢN SAO trong gói, gốc giữ nguyên):**
  - `compose.yaml`: `POSTGRES_PASSWORD` và `DATABASE_URL` được thay bằng placeholder
    (`change-me-before-use` / URL mẫu với user `fb_sandbox`). Lý do: runtime gắn cờ
    hai giá trị này là secret khi đọc file gốc nên không đưa nguyên văn vào gói.
    Cấu trúc compose, tên service/volume/port không đổi.
- **Không cần redaction nội dung nào khác:** quét toàn bộ cây đã xuất không thấy
  API key/token/cookie/Authorization hay private key; actor ID đã là giả
  (`u1`, `uA`, `uB`, `page_hyundai_sample`); các chuỗi giống số điện thoại trong
  fixture/run JSON là dữ liệu test tổng hợp (dạng số lặp, VD 0999999999); không có
  email thật; `.env.example` chỉ chứa placeholder và 1 đường dẫn local.
- **Loại trừ khỏi gói (không phải redaction):** mọi `__pycache__/` và `*.pyc`;
  `runs/sandbox.db`, `runs/sandbox.db-shm`, `runs/sandbox.db-wal` (DB runtime của
  server smoke — reviewer tự tạo lại, không cần cho review logic).
- **Không tuyên bố:** bản redacted của `compose.yaml` không giống byte-for-byte bản
  gốc; mọi file còn lại trong gói là bản sao nguyên văn (đối chiếu bằng
  `SHA256SUMS.txt`).

---

## Phụ lục Session 3 — Review-fix R01–R15 (2026-10-24, sau Loop 8)

Các anchor trong phần chính vẫn đúng cho Loop 8, nhưng các module sau đã đổi
hình dạng trong session 3 (xem `reports/review_fix_report.md` đầy đủ):

- `Engine.process()` nay có: pref pre-phase → opt-out gates → validation phase
  thuần đọc (`_validate_ops`) → sarcasm gate → business SAVEPOINT → apply →
  ambiguity gate (giữ START/UPDATE, rollback destructive) → plan → claim
  (union + typed) → dispatch precheck → outbox INTENDED → RELEASE+COMMIT →
  dispatcher claim → send ngoài transaction. File: `app/engine.py`.
- Pending checks tập trung ở `_validate_pending` (tồn tại → actor → link
  request → hết hạn → OPEN → qtype → slot/options → yes_no/provenance).
  Resolve tie: `_resolve_ref` — by_ref trước, rồi identity-scored, >1 ứng viên
  thì ambiguous, KHÔNG chọn newest.
- Prefs cũ (`prefs()`/`set_pref()`, single-scope) đã thay bằng bảng
  `contact_blocks` (`Store.blocks/block/unblock`) — set semantics nhiều kênh.
- Facts: `Facts.is_fresh()` + `verified_at=None` nghĩa là "verified lúc tạo
  instance", không phải vô hạn TTL. OTR/loan/stock conflict → HANDOFF; stock 0
  → "hết hàng".
- Claim check typed mới: `app/renderer.py::claim_check_typed` (số trong đoạn về
  xe X phải khớp claim của X, cả variant nếu đoạn nêu variant).
- Mutation suite v2 (`tests/mutations/mutation_suite.py`): phân loại
  KILLED/SURVIVED/INVALID/BASELINE_FAIL, leak scan riêng.
- Scorer v3 (`scripts/scoring_v3.py`, `scripts/score_multiturn_v3.py`): exit
  0/1/2, không zip(), match theo identity, final-state tính verdict.
- Cache key v2 (`scripts/cache_key.py`) + budget ledger (`scripts/budget_ledger.py`)
  + resume remap (`scripts/resume.py`). Server dùng `WallClock`.
- PostgreSQL: `app/pg_adapter.py` ném `PostgresNotImplemented`; `compose.yaml`
  ghi rõ NOT_IMPLEMENTED (không có Dockerfile theo thiết kế).
