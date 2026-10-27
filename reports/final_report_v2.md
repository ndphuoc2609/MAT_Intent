# BÁO CÁO BÀN GIAO — SESSION 2: KIỂM CHỨNG ENGINE + ĐA LƯỢT (2026-10-24)

Tiếp nối `reports/final_report.md` (session 1). Không xóa dữ liệu/run cũ; mọi run mới
đều có run ID riêng; checkpoint gắn với input + prompt + schema + model version.

## 1. Revision, manifest, ngân sách cộng dồn

- Freeze: `runs/baseline_freeze_v5.json` (trước mọi sửa session 2) → v6 (sau Loop 5+6)
  → v7 (sau Loop 7) → v8 (sau Loop 8, candidate cuối). Mỗi freeze lưu sha256 (16 hex)
  của code, prompt (`app/adapters/luna.py` SYSTEM), schema `semantic_v1`, policy yamls,
  datasets, scorer scripts.
- Contract: 199 scenario (196 + 3 Loop 7). API cases: dev 109, holdout 16.
  Multi-turn: dev 10 hội thoại (`datasets/multiturn/dev.jsonl`), holdout 6
  (`datasets/multiturn/holdout.jsonl`), tách TRƯỚC khi chạy; script/expected không
  vào inference (runner chỉ đọc `text`).
- Ngân sách (CỘNG DỒN, không phải ngân sách mới):
  - Session 1: 148 calls / ~$0.0392, 4/12 vòng sửa.
  - Session 2: single-turn v2: 41 ($0.01338) + targeted: 1 fresh ($0.00056)
    + multi-turn dev vòng 1: 33 ($0.01140) + vòng 2: 33 ($0.01306)
    + holdout: 20 ($0.00778) = 128 calls / $0.04618.
  - **Tổng: 276/300 calls, $0.0854/$2.00. Còn: 24 calls, $1.9146. Vòng sửa: 8/12.**
- Môi trường: không git repo, không Docker, không PostgreSQL trong VM (xác minh lại
  session 2). Mọi kết luận "PASS" dưới đây là trên SQLite trừ khi ghi rõ khác.

## 2. Kết luận contract cho hai ca tranh luận

### service_warranty — nhãn cũ THIẾU PHỦ, engine cũ LÀM MẤT ĐÁP ÁN (đã sửa)
- Input API: "Cho số tư vấn bảo hành, Creta bảo hành mấy năm?" = HAI nhu cầu:
  (1) chính sách bảo hành — trả lời được (fact `f_warranty` = 5 năm);
  (2) số tư vấn bảo hành — KHÔNG có fact số/contact nào trong seed → không trả lời được.
- Phân loại nguồn chuyển người: model đề xuất vì thiếu thông tin (model_proposed),
  KHÔNG phải khách yêu cầu người rõ ràng, KHÔNG phải policy bắt buộc.
- Hành vi cũ: REQUEST_HUMAN làm HANDOFF toàn lượt → đáp án 5 năm bị mất, 0 send.
- Sửa (Loop 5 engine + Loop 6 prompt): model_proposed + plan auto-safe → gửi phần
  safe + ghi handoff draft kèm câu báo trong cùng reply; khách yêu cầu người rõ ràng
  vẫn HANDOFF toàn phần. Prompt bắt buộc slot `service_topic` khi xác định được.
- Kiểm chứng: single-turn v2 routing HANDOFF → **AUTO**, text "Thời gian bảo hành
  mẫu là 5 năm (SAMPLE). Phần cần người xử lý (Chưa có thông tin số tư vấn bảo hành...";
  multi-turn dev D6 3/3, holdout H5 3/3 (policy → contact draft → explicit human HANDOFF).
- Scenario contract cũ cùng text chỉ model nhu cầu chính sách: ghi nhận là thiếu sót
  phủ nhãn, scenario cũ giữ nguyên; scenario mới `service_warranty_mixed` bổ sung.

### neg_variant_change — expected ĐÚNG, Luna sai, prompt round 3 sửa được
- Input: "Không lấy bản cao nữa, lấy bản tiêu chuẩn, còn màu trắng không?"
  (fresh state, postcard Creta 2026). Không có request mua nào đang mở trước đó.
- Expected (khai báo từ trước, không đổi): topics `['stock']` duy nhất, final
  `[(stock, COMPLETED)]`, 1 send — cấm cả nhãn buy, request mua và CTA mua.
- Lỗi cũ: Luna đọc "lấy bản tiêu chuẩn" thành buy intent → tạo buy request →
  HANDED_OFF + NEEDS_REVIEW draft (reply text về tồn kho thì đúng).
- Sửa (Loop 6 prompt, round 3 tích lũy cho hướng này — session 1 đã thử 2 round):
  "lấy bản X trong câu hỏi tồn kho/giá = chọn variant cho chính request đó, buy chỉ
  khi nói rõ mua/đặt cọc/chốt mua".
- Kiểm chứng targeted (1 fresh call, checkpoint v2): ops chỉ còn stock request
  (variant=tiêu chuẩn) + stock COMPLETED, text **"Còn Creta Tiêu chuẩn màu Trắng
  tại Hà Nội"** — đúng cả variant lẫn màu. Ca auto sai cuối cùng của session 1 đã đóng.
- Lưu ý: kế hoạch session 1 ghi "sửa ở tầng engine, không thêm prompt" cho cả hai ca;
  bằng chứng session 2 cho thấy lỗi nằm ở tầng semantic của Luna (thiếu slot / đọc
  sai intent), nên prompt + engine-split mới là sửa đúng chỗ — đã kiểm chứng bằng
  live call, không sửa expected.

## 3. Trace đầy đủ của ca auto sai (24/25, session 1)

- Nguồn: `runs/api_dev_b3_replay2.json`, scored AUTO = 25 (27 raw trừ 2 excluded
  `api_facts_conflict`, `api_promo_expired` — hai ca này trong replay chỉ AUTO vì
  input API thiếu điều kiện contract: Facts() mặc định có promo còn hạn và không
  conflict, nên trả lời là đúng với input đó; expectation HANDOFF của chúng thuộc
  fixture contract có fact override).
- Ca sai duy nhất: `api_neg_variant_change` (xem §2). Phân loại rubric:
  reply đã gửi ĐÚNG; hại người dùng THẤP; hại nội bộ TRUNG BÌNH-THẤP (draft buy
  thừa = nhiễu triage cho nhân viên); KHÔNG phải lỗi nghiêm trọng (không xác nhận
  sai, không sai fact, không trộn người, không vi phạm opt-out, không duplicate).
- Trạng thái: TỒN TẠI ở baseline v5, ĐÃ SỬA ở Loop 6 (kiểm chứng targeted, §2).

## 4. Thay đổi engine, regression, mutation

### Loop 5 (engine hardening, vòng 5/12)
- `normalize.py`: `parse_time_expression` thật (thứ ba tuần sau, ngày 3/11, cuối tuần
  này → range + day options, cuối tháng → vague, sáng mai → 9h, bare buổi → time_only,
  ngày quá khứ → past), timezone Asia/Ho_Chi_Minh, clock kiểm soát được; bỏ guard
  chặn mọi chuỗi tuần/tháng; giờ mặc định chỉ là đề xuất.
- `engine.py`: dedupe START theo identity (owner, topic, vehicle, variant) — không
  dedupe chỉ theo topic; hai xe / hai phiên bản giữ đủ request; request đã hoàn tất
  không bị dedupe nhầm khi mở mới hợp lệ. Mixed split cho human model_proposed (§2).
  Datetime range/time_only/past/vague → slot dict/marker + hỏi lại (không set sai).
  Dispatch recheck: kill-switch/pref/facts version/state version/handoff-at-dispatch
  → BLOCKED_* (không gửi plan cũ). `_send_reply` theo kết quả thật (SENT/UNKNOWN).
  `_sweep_expired`: pending quá hạn → EXPIRED, handoff > 48h tự release.
  `reconcile_unknown()`: UNKNOWN chỉ → SENT khi sender đã confirm (không retry mù).
- `facts.py`: query lọc TTL khi có verified_at, trả fresh sort theo id (độc lập thứ tự
  bản ghi, không dừng ở fact expired đầu tiên), conflict = >1 (value, unit) distinct;
  `Facts.version` + `touch()`; deep copy seed (sửa bug test làm bẩn SEED_FACTS chung).
- `state.py`: `active_requests` ORDER BY rowid DESC; `FakeSender(fail_mode="lost_ack")`
  + `confirm_delivery`.
- `renderer.py`: `claim_check` theo provenance (fact ID → object → attribute →
  value/unit → claim) + check vehicle thuộc used-facts; giữ hỗ trợ 2 và 2.0.
- Scenarios mới (13, không sửa scenario cũ trừ 1 version note): dup_start hai xe /
  hai variant / hai tồn kho, reopen_after_completed, 7 scenario thời gian,
  service_warranty_mixed, mixed_pref_price_complaint.
- Version note duy nhất: `err_expired_pending` expected_final WAITING_USER → OPEN
  (sweep hết hạn là hành vi đúng; routing REVIEW_PENDING_EXPIRED không đổi).

### Loop 6 (prompt, vòng 6/12)
- `luna.py` SYSTEM: bắt buộc `service_topic`; `human_source` (customer_explicit chỉ
  khi nói rõ gặp người; "cho số tư vấn" = model_proposed); quy tắc variant (§2).
- `run_api_eval.py`: `--ckdir` + `cache_key` (sha256 SYSTEM+schema+MODEL) cho checkpoint;
  resume chỉ khi key khớp; checkpoint cũ không bị xóa hay dùng ké.

### Loop 7 (engine + prompt, vòng 7/12) — từ multi-turn dev vòng 1 (22/33)
- Nguyên nhân (có bằng chứng từ semantic): canon variant thiếu "ban tieu chuan"
  (chỉ có "ban cao") → fact miss → HANDOFF; Luna nhầm "còn màu trắng không?" thành
  UPDATE color lên price; "hủy lái thử" thành REJECT_PENDING rồi đề xuất lại;
  "đừng gọi nữa, gửi giá tại đây" thành START price mới; follow-up stock/testdrive
  không mang vehicle/variant từ context và hay mất color slot.
- Sửa engine: `canon_variant` strip tiền tố "ban " (thử nguyên chuỗi trước để giữ
  "ban cao"); stock answer nêu màu của fact khi mọi fact khớp cùng một màu.
- Sửa prompt: stock-vs-price color, CANCEL vs REJECT (REJECT chỉ cho trả lời "không"
  trước câu hỏi yes/no), "vẫn lấy/giữ X" không START mới, cụm kênh chỉ
  SET_CONTACT_PREFERENCE, mang vehicle/variant từ context, "màu X" luôn thành
  color slot.
- Script dev D8 sửa công khai: turn 0/1 không nêu price_kind nên hành vi đúng là ASK
  loại giá (không phải COMPLETED như script ban đầu viết).
- Kiểm chứng: contract 199/199, unit OK, mutation 11/11 (sau khi sửa harness, xem dưới),
  multi-turn dev vòng 2: **30/33**.

### Loop 8 (engine tolerance + prompt, vòng 8/12) — từ multi-turn dev vòng 2
- Nguyên nhân: Luna điền pending id (`q_...`) vào `request_ref` thay vì `topic:...`
  ở 2 lượt ("oke" xác nhận, "lăn bánh" trả lời) → `REVIEW_BAD_REF`, 0 send. Pending id
  thì đúng (linkage 2/2), chỉ sai trường ref.
- Sửa engine: `resolve_ref` map pending id → request của nó (pending xác định duy
  nhất một request; owner check phía sau vẫn bắt pending của người khác; ref hoàn
  toàn lạ vẫn REVIEW_BAD_REF). Sửa prompt: request_ref LUÔN là `topic:<topic>`.
- Kiểm chứng 0 call: 3 unit test Loop 8 (answer/confirm bằng pending-id ref pass,
  ref lạ vẫn BAD_REF); contract 199/199; unit OK.

### Mutation harness — lỗi harness, không phải lỗi engine
- Sau Loop 7, suite báo 10/11 (`drop_identity_match` "không bị phát hiện").
  Điều tra: header pyc chỉ lưu mtime (giây) + size; hai mutant khác nhau viết trong
  cùng một giây và cùng size (+10 ký tự "if False and") khiến Python tin nhầm
  bytecode cũ của mutant trước (chứa logic identity gốc) → false negative.
- Sửa harness: xóa `__pycache__` trước mỗi check. Sau sửa: **11/11 + leak scan**,
  kiểm chứng lại trên revision cuối.

### Regression cuối (revision Loop 8)
- `python3 scripts/generate_datasets.py` → 199 scenario, api dev 109 / holdout 16
- `python3 scripts/run_contract.py` → **199/199** (dev 178, holdout 21, multi_turn 74,
  multi_user 22, error_race 15)
- `python3 -m unittest discover -s tests` → **OK** (36 + 3 Loop 8)
- `python3 tests/mutations/mutation_suite.py` → **PASS 11/11 + leak scan**
- Replay single-turn v2 qua engine Loop 7: giống hệt v2 (strict 0.914, adjusted 0.971)
  → Loop 7 không gây regression single-turn.

## 5. Kết quả model thật nhiều lượt

### Single-turn v2 (prompt Loop 6, scorer v2, so sánh cùng scorer)
- Baseline b3 (36 scored): strict 32/36 = 0.889, adjusted 35/36 = 0.972, auto 23/24.
- Replay2 (engine Loop 3): strict 33/36 = 0.917, adjusted 35/36, auto 24/25.
- V2 (Loop 6): strict 32/35 = 0.914, adjusted 34/35 = 0.971, auto theo scorer 21/24.
  3 ca auto "sai" theo scorer đều đã adjudicate: 2 scorer artifact (topic_exact so
  multiset `['spec','spec']` với pred set `['spec']` trong khi engine trả đúng cả hai
  nhu cầu) + 1 label conflict (`api_time_next_tuesday`: api kỳ vọng ASK, contract
  Loop 5 kỳ vọng đề xuất 27/10 lúc 10:00 — hành vi engine theo contract).
  Ca validation duy nhất: `api_price_otr_0` (Luna trích evidence không có trong comment).
- Scorer v2 (đã sửa post-hoc của session 1): ASK chỉ được chấp nhận khi engine hỏi
  đúng slot bắt buộc mà semantic không có (price_kind/location/vehicle/variant/
  datetime); 0.972 session 1 là post-hoc vì ASK chưa từng là alternative khai báo.
  Hai ca ASK-necessary còn lại (pref_inbox_ok, neg_not_tucson) hỏi price_kind thật sự
  thiếu → chấp nhận được, nhưng phải ghi rõ là adjusted, không phải strict.

### Multi-turn dev — vòng 1: 22/33 lượt pass, auto 21/30, pending link 6/6,
### provenance 0 vi phạm, duplicate effects 0
- Lỗi thật (đã sửa ở Loop 7, §4): canon variant, stock-vs-price color, CANCEL vs
  REJECT, pref-only START thừa, context carryover. Script D8 sai (đã sửa công khai).

### Multi-turn dev — vòng 2 (Loop 7): 30/33 lượt pass, auto 27/30, pending link 2/2
- 8/10 hội thoại pass toàn bộ (D1, D4, D5, D6, D7, D8, D9, D10).
- Còn lại: D2 turn "oke" + D3 turn "lăn bánh" → REVIEW_BAD_REF (pending id trong
  request_ref, §4 Loop 8). D3 hồi phục ở lượt sau (UPDATE location → hỏi lại loại
  giá đúng) — khả năng phục hồi tốt, không mất state.
- Không crash = không đồng nghĩa state đúng: mọi lượt pass đều kiểm tra status,
  slots, sends, routing, ledger purposes và provenance (source_message_id thuộc về
  comment trong hội thoại).

### Multi-turn holdout — chạy 1 lần duy nhất (Loop 8): raw 17/20, auto 16/19,
### pending link 3/3, duplicate 0, provenance 0
- H1, H4, H5, H6: pass toàn bộ (giá→variant→tồn kho; hai người cùng thread; bảo hành
  3 nhu cầu; postcard-ref + promo điều kiện).
- H2 turn 0: expected script sai — "sáng thứ hai tuần sau" theo contract phải đề xuất
  26/10 lúc 09:00 (sáng = 9h mặc định), không phải ASK; hành vi đúng contract.
- H2 turn 1 ("9 giờ sáng"): lỗi nhỏ THẬT — Luna flag `different_action` quá đà
  ("cập nhật giờ" vs "xác nhận đề xuất") dù cả hai cùng dẫn tới đề xuất 09:00 đang
  chờ → engine REVIEW_AMBIGUOUS, lượt im lặng, 0 send; hội thoại hồi phục hoàn toàn
  ở lượt "oke" + "hủy" sau đó. Tác động: THẤP (1 lượt im lặng, không sai state/send).
- H3 turn 0: expected ghi cứng cụm "niêm yết hay giá lăn bánh" trong khi engine hỏi
  variant ("Bạn muốn xem phiên bản nào ạ?") — câu hỏi hợp lệ và cần thiết hơn
  (Creta có 2 fact giá, chưa có variant thì chưa báo giá được); Luna có tạo thêm
  buy draft thừa từ "Muốn mua Creta" (draft only, không gửi); final vẫn đúng
  (699M COMPLETED ở turn 2). Adjudicated: hành vi chấp nhận được.
- **Adjudicated holdout: 19/20 lượt đúng về thực chất, 1 lỗi nhỏ (H2T1) có hồi phục.**
- Holdout KHÔNG bị chuyển thành development: không có thay đổi nào áp dụng từ lỗi
  của nó. Draft sửa ambiguity-flag (Loop 9, §8) chưa áp — nếu áp thì phải chạy
  holdout MỚI trước khi kết luận tiếp theo.

## 6. Smoke cuối cùng (revision Loop 8)
- `python3 -m app.server` (127.0.0.1:8765) + POST /event (fixture semantic, giá
  Creta Cao cấp niêm yết) → 200, routing `answer_price`,
  text "Giá niêm yết Creta Cao cấp là 699.000.000 đồng (giá mẫu SAMPLE).";
  GET /ledger → row `smoke_final` purpose `reply` status `SENT`.
- Smoke session 2 đầu phiên (revision Loop 5+6) cũng pass tương tự; smoke cuối này
  chạy lại trên đúng revision cuối dù `server.py` không đổi.

## 7. Verdict theo đúng môi trường

- **ENGINEERING_SQLITE_PASS** — contract 199/199, unit OK, mutation 11/11, smoke pass,
  multi-turn dev 30/33 + holdout adjudicated 19/20, tất cả trên SQLite + FakeSender.
- **PostgreSQL: BLOCKED** — không Docker, không binary PG trong VM; `compose.yaml`
  chưa từng được thực thi; semantics transaction/race trên PG chưa được chứng minh
  (không giả vờ đã chứng minh).
- **Gate B: INSUFFICIENT_EVIDENCE** — mọi nhãn đều synthetic (contract + scripted
  multi-turn); `human_gold` vẫn trống; không tự tuyên bố semantic production-ready.
- **Gate C: BLOCKED** — chưa link Facebook thật, chưa có comment thực tế đã gán nhãn
  (≥100), chưa Facts DB v2, chưa webhook/kill-switch drill.
- **KHÔNG auto-ready, KHÔNG shadow production.** Hệ thống chỉ xin code review
  phần engine + chạy suggest-mode có người duyệt (khi đủ điều kiện Gate B/C).

## 8. Lỗi còn tồn tại, tác động, bước tiếp theo

1. **Ambiguity flag quá đà (H2T1, tác động THẤP)** — Luna đặt `different_action=true`
   khi hai cách hiểu cùng dẫn tới một trạng thái. Draft Loop 9 (CHƯA ÁP):
   "different_action=true CHỈ khi hai cách hiểu dẫn tới trạng thái/hiệu ứng khác nhau
   thật sự (gửi khác nhau, hủy khác nhau); nếu cùng dẫn tới một trạng thái thì chọn
   cách hiểu khả dĩ nhất, different_action=false." Sau khi áp: chạy holdout MỚI
   (3-4 hội thoại ngắn, ~10 calls) trước khi kết luận tiếp theo.
2. **price_kind lưu giá trị raw không chuẩn (tác động THẤP)** — VD "lăn bánh hay
   niêm yết" được lưu làm slot thay vì hỏi lại. Cân nhắc: price_kind là closed set
   {list, otr} → giá trị lạ nên về None để engine hỏi lại (không áp trong session này
   vì holdout đã chạy; cần test regression nếu áp).
3. **REJECT_PENDING đề xuất lại cùng slot (tác động THẤP, tiềm ẩn)** — sau khi khách
   từ chối đề xuất, engine plan lại cùng slot thay vì hỏi giờ khác. Chưa sửa (nằm
   ngoài lỗi quan sát session 2 sau khi CANCEL được dùng đúng chỗ).
4. **Fact số tư vấn bảo hành không tồn tại (by design)** — nhu cầu xin số chỉ có thể
   ra handoff draft, không trả lời được; cần Facts DB v2 với contact facts thật.
5. **Xung đột nhãn `api_time_next_tuesday` (chưa có quyết định)** — api kỳ vọng ASK,
   contract kỳ vọng đề xuất giờ mặc định. Giữ nguyên expected, báo riêng mỗi lần chấm;
   cần Phước quyết: giữ quy tắc "giờ mặc định = đề xuất" thì cập nhật expected api.
6. **PostgreSQL chưa kiểm chứng; human gold trống; holdout thiếu nhóm 5/7/8**
   (rút một nhu cầu, ngừng gọi, khiếu nại chỉ có ở dev) — holdout sau cần bổ sung.

## 9. Lệnh đã thực sự xác nhận (đều exit 0, trừ ghi chú)
- `python3 scripts/rescore_v2.py runs/api_dev_b3.json runs/api_dev_b3_replay2.json`
  → paired scorer v2 (baseline 0.889/0.972, replay2 0.917/0.972)
- `python3 scripts/run_api_eval.py --input datasets/development/api_cases.jsonl --limit 40 --stratified --out runs/api_dev_v2.json --ckdir runs/api_checkpoints_v2`
  → 41 fresh calls, strict 0.914 / adjusted 0.971
- `python3 scripts/run_api_eval.py --input datasets/development/api_targeted_v2.jsonl --out runs/api_targeted_v2.json --ckdir runs/api_checkpoints_v2`
  → 1 fresh call (neg_variant_change FIXED)
- `python3 scripts/run_multiturn_eval.py --input datasets/multiturn/dev.jsonl --out runs/mt_dev_v2.json --ckdir runs/mt_checkpoints_v2 --prior-calls 190 --prior-cost 0.05314`
  → vòng 1: 33 calls, 22/33
- `python3 scripts/run_multiturn_eval.py --input datasets/multiturn/dev.jsonl --out runs/mt_dev_v3.json --ckdir runs/mt_checkpoints_v2 --prior-calls 223 --prior-cost 0.0645`
  → vòng 2: 33 calls, 30/33
- `python3 scripts/run_multiturn_eval.py --input datasets/multiturn/holdout.jsonl --out runs/mt_holdout_v2.json --ckdir runs/mt_checkpoints_v2 --prior-calls 256 --prior-cost 0.0776`
  → holdout: 20 calls, 17/20 raw
- `python3 scripts/score_multiturn.py <run> <conversations>` → các số multi-turn trên
- `python3 scripts/generate_datasets.py` → 199 scenario
- `python3 scripts/run_contract.py` → 199/199
- `python3 -m unittest discover -s tests` → OK
- `python3 tests/mutations/mutation_suite.py` → PASS 11/11 + leak scan
- Server smoke: `python3 -m app.server` + POST /event + GET /ledger → 200 / SENT
