# CONTINUATION — 2026-10-26 (supersedes older status below)

- Facebook outbound is off: `.env` has `META_OUTBOUND_ENABLED=false`; Docker
  reports the `meta-services` container as `Created`, with no running container.
  No Facebook send was run in this continuation.
- Intent changes: ambiguity prompt now requires a real state/effect difference;
  ISO datetime slots preserve their local date and time; yes/no accepts common
  short Vietnamese affirmations including “có”; mixed stock replies identify a
  requested vehicle whose inventory has no verified fact without claiming stock.
- Eval runner now persists a transport failure with unknown cost as `unknown_cost`
  instead of recording it as $0. Added a regression for the cross-run spend gate.
- Verification after the fixes: unit suite **225/225**. Contract **190/199**:
  the multi-vehicle reply case now passes. Six remaining fixtures expect sends
  for stock combinations absent from sample facts (current behavior is handoff);
  three expect `REVIEW_UNRESOLVED` where the engine returns `REVIEW_SCOPE` (both
  block state changes). Gold labels were not changed to raise the score.
- Review reproducer: **40 fixed, 1 reproduced / 41**. The remaining case is
  PostgreSQL not implemented (documented by design). Two stale-fact probes were
  false positives because their fact timestamp used wall-clock time while the
  probe froze a prior date; the probe fixtures now use the same timestamp.
- Fresh Luna holdout: `datasets/multiturn/holdout_loop9.jsonl`, output
  `runs/mt_holdout_loop9.json`; 9 successful attempts, $0.003321, raw 7/9 before
  the subsequent engine fixes. Three open-time clarifications were handled with
  no ambiguity flag. The explicit time-change control exposed the ISO parsing
  bug and a “Được, xác nhận lịch đó” pending-confirm issue; both now have
  deterministic regression tests. The live holdout was not rerun after fixes;
  7/9 is not a score for current code. No further model call was made; the
  cumulative ledger reached 293/300 attempts (~$0.0901/$2.00).
- Limits remain: these are synthetic cases and sample facts, not human-labeled
  Facebook conversations. The live holdout was not rerun after the engine fixes;
  the old score must not be presented as a measurement of the current code.

# CHECKPOINT — SESSION 3 (review-fix R01–R15, 2026-10-24, sau session 2)

## Trạng thái cuối session 3 (không gọi model, không dùng 24 calls còn lại)
- Freeze trước sửa: `runs/baseline_freeze_pre_reviewfix.json` (430 file, SHA-256).
  Gói review độc lập không có trên máy — probe tái hiện tự tái tạo từ mô tả R
  (`scripts/review_repro.py`, 41 probe; KHÔNG PHẢI script reviewer gốc).
- Reproducer: **41/41 FIXED, 0 REPRODUCED** (baseline: 32 reproduced / 9 fixed).
- Contract: **199/199** (dev 178/178, holdout 21/21). Unit: **OK toàn bộ**
  (thêm test_review_fixes, test_review_infra, test_policy).
- Mutation suite **v2: 17/17 KILLED** + leak scan riêng (no leak). Lưu ý: 11/11
  của v1 là sai (1 mutant SQL invalid + 1 mutant SyntaxError + leak scan trong
  mẫu số) — xem reports/review_fix_report.md §4.
- Scorer **v3** (`scripts/scoring_v3.py`, `scripts/score_multiturn_v3.py`): rescore
  offline mt_dev_v3 = 26/33, mt_dev_v2 = 22/33, mt_holdout_v2 = 15/20 (raw, không
  adjudication mới). V3 nghiêm hơn scorer cũ nên số thấp hơn không phải
  regression engine — xem báo cáo §5.
- Server smoke (WallClock): /health ok, POST /event answer_price 699.000.000.
- Budget cộng dồn KHÔNG ĐỔI: **276/300 calls, ~$0.0854/$2.00**. Gates B/C không đổi.
- **Vẫn không auto-ready, không shadow production** (sửa engine không chứng minh
  ngữ nghĩa model; prompt allow_* chưa chạy live).
- Báo cáo đầy đủ: `reports/review_fix_report.md`. Diff vs Loop 8:
  `reports/review_fix_diff.txt`. Candidate ZIP: `facebook-intent-sandbox-reviewfix.zip`.

## Thay đổi contract có chủ ý trong session 3 (xem báo cáo §3)
- Handoff quá SLA -> OVERDUE (không tự release); release phải gọi rõ ràng.
- lost_ack không ghi effect; reconcile giữ UNKNOWN tới ACK muộn.
- Ambiguity giữ START/UPDATE, rollback destructive ops; sarcasm rollback toàn bộ.

# CHECKPOINT — SESSION 2 (phiên 2026-10-24, tiếp nối; xem reports/final_report_v2.md)

## Trạng thái cuối session 2
- Contract: **199/199** (dev 178, holdout 21, multi_turn 74, multi_user 22, error_race 15)
- Unit: **OK** (36 + 3 Loop 8) — `python3 -m unittest discover -s tests`
- Mutation: **11/11 + leak scan** (đã sửa 1 lỗi harness pycache làm mutation bị "không phát hiện" giả)
- Single-turn v2 (prompt Loop 6, scorer v2): strict 0.914, adjusted 0.971; auto scorer 21/24,
  adjudicated 24/24 (2 scorer multiset artifact + 1 label conflict, đều có bằng chứng)
- Targeted: **neg_variant_change ĐÃ SỬA** (prompt round 3, 1 fresh call, trả đúng "Creta Tiêu chuẩn màu Trắng")
- Multi-turn dev: vòng 1: 22/33 -> Loop 7 -> vòng 2: **30/33** (còn REVIEW_BAD_REF do Luna điền pending id vào request_ref -> Loop 8)
- Multi-turn holdout (chạy 1 lần, KHÔNG sửa từ lỗi của nó): raw **17/20**, adjudicated 19/20
  (H2T0: expected script sai so với contract; H3T0: câu hỏi variant hợp lệ + final đúng;
  H2T1: 1 lỗi nhỏ thật — Luna flag ambiguous quá đà -> lượt im lặng, hồi phục ở lượt sau)
- Server smoke trên revision cuối (Loop 8): POST /event 200 answer_price, ledger SENT
- Budget cộng dồn: **276/300 calls, $0.0854/$2.00** (session 2: +128 calls). Vòng sửa: 8/12.
- Gates: A = **ENGINEERING_SQLITE_PASS** (SQLite; PostgreSQL BLOCKED — không Docker/PG trong VM);
  B = INSUFFICIENT_EVIDENCE (nhãn synthetic, human_gold vẫn trống);
  C = BLOCKED (chưa link Facebook thật, chưa đủ comment thực tế, chưa Facts DB v2, chưa drill).
- **Không auto-ready, không shadow production.** Holdout KHÔNG bị chuyển thành development
  vì không có thay đổi nào áp dụng từ lỗi của nó; draft sửa ambiguity-flag để ở báo cáo, chưa áp.

## Lỗi mở / bước kế tiếp (theo thứ tự)
1. Áp draft Loop 9 (ambiguity flag chỉ khi hai cách hiểu dẫn tới hiệu ứng khác thật sự) rồi
   chạy holdout MỚI (3-4 hội thoại ngắn, ~10 calls) trước khi kết luận tiếp theo.
2. Cân nhắc canon price_kind closed-set (giá trị lạ -> None để hỏi lại, thay vì lưu raw).
3. REJECT_PENDING đề xuất lại cùng slot sau khi khách từ chối — cần hỏi lại giờ khác.
4. Chạy PostgreSQL thật (compose.yaml) + test race trên PG.
5. Gán nhãn human gold >= 100-200 comment thực tế đã ẩn danh; tạo fresh holdout MỚI chưa từng xem.
6. Sau đó mới xin chạy shadow/suggest — không auto-send.

## Lệnh gần nhất (đều exit 0)
- `python3 scripts/generate_datasets.py` -> 199 scenario
- `python3 scripts/run_contract.py` -> 199/199
- `python3 -m unittest discover -s tests` -> OK
- `python3 tests/mutations/mutation_suite.py` -> PASS 11/11
- `python3 scripts/score_multiturn.py runs/mt_dev_v3.json datasets/multiturn/dev.jsonl` -> 30/33
- `python3 scripts/score_multiturn.py runs/mt_holdout_v2.json datasets/multiturn/holdout.jsonl` -> 17/20

---

# CHECKPOINT — FINAL (phiên 2026-10-24, đã hoàn tất báo cáo)

## Trạng thái cuối
- Project: `~/workspace/facebook-intent-sandbox/` — engine + Luna adapter + fake sender.
- Contract: **183/183** (`python3 scripts/run_contract.py`)
- Unit: **15/15** (`python3 -m unittest discover -s tests`)
- Mutation: **8/8 detected** (`python3 tests/mutations/mutation_suite.py`)
- Replay dev: routing 0.917 / topic 0.972 / recall 1.000, 0 call mới
- Fresh holdout (đo 1 lần, pre-fix): topic 0.958 / routing 0.833 / 2 ERROR — nay đã thành development.
- Budget: **148/300 calls, ~$0.039/$2.00**. Vòng sửa: 4/12.
- Gates: A = ENGINEERING_SANDBOX_PASS (caveat: PG chưa kiểm chứng);
  B = INSUFFICIENT_EVIDENCE (SEMANTIC_EXPLORATORY, không human gold);
  C = BLOCKED (thiếu 200 comment thực tế + monitoring + kill-switch drill).
- **Không auto-ready, không đề xuất shadow production.**

## File cần đọc tiếp (nếu làm tiếp)
- `reports/final_report.md` (báo cáo đầy đủ, 15 trace)
- `reports/failure_analysis.md` (batch 1–3, replay, fresh, Vòng 4)
- `reports/unresolved_cases.jsonl` (6 mục mở)
- `datasets/MANIFEST.md` (tái phân loại holdout — đọc trước khi trích số)

## Lỗi mở / bước kế tiếp (theo thứ tự)
1. service_warranty: Luna vẫn HANDOFF dù fact có sẵn — sửa ở tầng engine, không thêm prompt.
2. neg_variant_change: thừa topic buy — sửa ở tầng engine (buy cần evidence mua rõ ràng).
3. Chạy PostgreSQL thật (compose.yaml) + test race trên PG.
4. Gán nhãn human gold >= 200 comment thực tế đã ẩn danh; tạo fresh holdout MỚI chưa từng xem.
5. Đo API đa lượt (pending-link, negation, testdrive flow) bằng Luna thật.
6. Sau đó mới xin chạy shadow/suggest — không auto-send.

## Lệnh gần nhất (đều exit 0)
- `python3 scripts/generate_datasets.py` -> 183 scenario
- `python3 scripts/run_contract.py` -> 183/183
- `python3 -m unittest discover -s tests` -> 15 OK
- `python3 tests/mutations/mutation_suite.py` -> PASS 8/8
- `python3 scripts/replay_api.py runs/api_dev_b3.json runs/api_dev_b3_replay2.json` -> routing 0.917

---

## Session 4 — Sửa audit độc lập A1–A8 (2026-10-24, trên candidate review-fix)

- Audit mới: `facebook-intent-reviewfix-audit.zip` (SHA-256 `7fad9d9e…892be`),
  gồm residual/infra/resume probes + gói independent review gốc lồng trong.
  Freeze session 4: `runs/baseline_freeze_session4.json` (447 files).
- **0 model call** (ngân sách giữ nguyên 276/300, ~$0.0854/$2.00, còn 24).
- Sửa chính: ambiguity gate khôi phục snapshot + SUPERSEDE pending cũ + basis
  check (`basis_version`/`basis_slots`); choice ANSWER kiểm tra expected_slot;
  REJECT đối xứng CONFIRM; anti re-propose sau reject (bảng proposal_rejections);
  Page isolation qua json_extract page_id + sweep theo (page, actor);
  per-START identity + blocked_idx partial (không abort cả lượt);
  outbox intent trước commit mọi nhánh + `drain_intended` + receiver file-backed
  + claim lease 300s; provenance canonical op-level + evidence phải nằm trong
  chính source claim; cache key bỏ context_text + remap thật trong runner;
  budget guard per-attempt + single-source accounting + cost UNKNOWN.
- Kết quả cuối: unit 99/99, contract 199/199, review_repro 41/41, mutation 17/17,
  residual 1/14 (by-design: lost_ack fixture không có effect), infra 1/3
  (probe mock phớt lờ budget_guard — guard-conformant regression đã viết riêng),
  resume run2 = 0 attempt, server smoke pass, rescore v3 giống hệt (dev 26/33,
  holdout 15/20). Chi tiết: `reports/audit_fix_report.md`.
- Verdict: Engineering SQLite PASS (giới hạn: single-worker lease; mutant mới cho
  guard session 4 chưa làm); PostgreSQL NOT_IMPLEMENTED; Semantic NOT_RETESTED;
  Human gold EMPTY; Production KHÔNG auto-ready, không shadow/suggest.
