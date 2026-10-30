# Peer Conversation Repair Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task after review. Steps use checkbox (`- [ ]`) syntax for tracking. This document is a proposed plan, not an implementation record.

**Goal:** Sửa hội thoại Nam để hiểu câu đang nói, nhớ trạng thái người đối thoại và chỉ hỏi lead khi có bằng chứng về nhu cầu mua.

**Architecture:** Một kết quả phân tích hội thoại có cấu trúc đi cùng lượt Luna đang có. Python kiểm tra bằng chứng và cập nhật state, sau đó chọn utterance/hook đã duyệt theo mục đích hội thoại. Bộ chọn lead không được kéo cuộc trò chuyện sang mua xe chỉ vì còn thiếu trường.

**Tech Stack:** Python, unittest, SQLite, PostgreSQL migrations, Luna adapter, renderer/release hiện có; không thêm thư viện hoặc model call mỗi lượt.

**Spec:** `docs/superpowers/specs/2026-10-29-peer-conversation-repair-design.md`

## Global Constraints

- Chỉ lập kế hoạch trong lượt hiện tại. Chưa sửa code runtime, chạy model hoặc deploy theo kế hoạch này.
- Giữ nhãn fictional_fixture, release bất biến, provenance, claim checks, dedupe, opt-out, handoff và kiểm tra lại trước dispatch.
- Renderer chỉ chọn ID đã duyệt. Nếu muốn sinh lời tự do, cần một thiết kế khác được duyệt riêng.
- Không chỉnh tay datasets/, runs/ hoặc SHA256SUMS.txt. Không xóa test để nâng tỷ lệ pass.
- Workspace đang có thay đổi chưa commit. Ghi nhận diff trước triển khai, không reset hoặc trộn thay đổi khác vào commit.
- Các ví dụ lời thoại dưới đây minh họa ý nghĩa; test ưu tiên hành vi/state, không bắt model lặp đúng một câu.

## Review Focus

1. “có” không phải “cô”; “anh trai tôi” không phải cách gọi persona. Task 1.
2. Có xe không đồng nghĩa muốn mua, cũng không đồng nghĩa vĩnh viễn không mua thêm. Task 2–3.
3. “Creta giống bác” phải được hiểu theo câu ngay trước; nếu chưa đủ thì hỏi rõ. Task 2–4.
4. Người thứ hai trong thread không được kế thừa cách gọi, sở hữu hoặc nhu cầu của người thứ nhất. Task 3.
5. Kết quả model hợp schema nhưng evidence sai chủ thể, bị phủ định hoặc stale không được ghi lead. Task 2–3.

---

### Task 1: Tái hiện lỗi và sửa cách gọi

**Files:** `app/lead_flow.py`, `tests/unit/test_peer_demo.py`; tạo `tests/unit/test_peer_conversation.py` để giữ các transcript mới.

**Interfaces:** thay `address(text)` bằng `address(text: str, previous: str | None = None) -> str`. Chỉ đầu ra bác/chú/cô/chị/anh/bạn; mặc định previous hoặc bạn.

- [ ] Viết test fail: `address('tôi có xe rồi', 'bác') == 'bác'`; `address('có rồi') == 'bạn'`; `address('Cô đi xe gì') == 'cô'`; `address('anh trai tôi có xe', 'bác') == 'bác'`. Thêm bản không dấu “co xe roi” không được suy thành cô.
- [ ] Chạy `python3 -m unittest tests.unit.test_peer_conversation -v`, lưu các failure trước sửa.
- [ ] Phân tích cách gọi trên chuỗi còn dấu và vị trí gọi; không dùng token đã bỏ dấu để suy giới tính/cách gọi. Chưa đọc global state trong hàm này.
- [ ] Chạy lại test mới và `tests.unit.test_peer_demo`, `tests.unit.test_persona_query`. Phần lưu previous được nối ở Task 3.
- [ ] Tạo commit riêng chỉ chứa thay đổi của task nếu workflow tích hợp đã cho phép commit.

### Task 2: Phân tích hành vi hội thoại, chủ thể và bằng chứng

**Files:** `app/persona_query.py`, `app/semantics.py`, `app/adapters/luna.py`, `config/schema_versions/semantic_v2.json`, `app/context.py`; tạo `app/peer_dialogue.py`, `tests/unit/test_peer_dialogue.py`. Đối chiếu callers trong `sandbox/server.py` và `meta_services/runtime.py`.

**Interfaces:**
- Optional `peer_dialogue` trong semantic v2, `schema_version='peer_dialogue_v1'`.
- `acts`: danh sách enum `ask_persona_experience`, `state_ownership`, `state_purchase_interest`, `correct_previous`, `answer_pending`, `decline`, `social_close`, `other`.
- `subject`: `persona | customer | unclear`; `persona_keys`: danh sách từ registry, bổ sung `vehicle.current.usage_started_at`.
- `ownership_status`, `purchase_intent`: nullable, enum như spec; không tự điền khi chưa có bằng chứng.
- `evidence`: danh sách `{message_id, excerpt}` từ các message đã xác minh. Dùng current message cho claim tường minh; diễn giải tham chiếu như “giống bác” cần đủ các message nguồn và subject đúng.
- `validate_peer_dialogue(value: dict, messages: list[dict]) -> list[str]` là hàm thuần; sai bằng chứng -> review, không cập nhật state.

- [ ] Viết test fail cho “Bác chạy bao lâu rồi” -> hỏi thời gian dùng xe của persona; “Creta giống bác” trong hai context hỏi mẫu định mua / hỏi xe đang có phải cho diễn giải khác nhau hoặc unclear; “tôi có xe rồi” -> sở hữu, không tự gán có ý định mua; “có xe rồi nhưng muốn đổi” -> sở hữu + exploring.
- [ ] Thêm negative controls: “chưa có xe”, “đừng hiểu là tôi muốn mua”, “anh tôi có Creta”, evidence chỉ có trong post/persona, ID giả, quote không tồn tại, correction phủ định giá trị trước.
- [ ] Chạy test fail rồi bổ sung field optional trong contract ứng dụng; provider strict schema biểu diễn field nullable và normalize bỏ null khi phù hợp. Giữ semantic v1 và semantic v2 cũ thiếu field vẫn đọc được.
- [ ] Thêm hướng dẫn peer vào prompt hiện có, tăng prompt version và cập nhật cache fingerprint/schema consistency. Không mở một lượt gọi model riêng. Jev vẫn shadow.
- [ ] Bổ sung rule hẹp cho câu hỏi thời gian dùng xe; không ánh xạ trực tiếp sang ngày mua. Không dùng rule fallback để giả định mọi SOCIAL_ACK là hiểu được nội dung.
- [ ] Chạy schema/adapter/context/cache tests và test mới trước khi nối state.

### Task 3: State hội thoại và điều kiện được hỏi lead

**Files:** `app/state.py`, `app/pg_adapter.py`, `app/lead_flow.py`, `app/engine.py`, `app/persona_store.py`; migration kế tiếp sau 007 cho cả SQLite/PostgreSQL; `tests/unit/test_peer_conversation.py`, test migration và policy consistency.

**Interfaces:**
- `Store.peer_state(event: dict) -> dict`, `Store.save_peer_state(event: dict, state: dict, now: str) -> None`.
- Bảng `peer_conversation_state` khóa `(page_id,post_id,thread_root_id,actor_id)`, payload JSON TEXT, optimistic version, updated_at. Payload giữ từng giá trị và evidence; dùng cùng transaction/lock scope của engine.
- `plan_peer_turn(dialogue: dict, state: dict, lead_profile: dict, pending: list[dict]) -> dict` thuần, trả `{next_state, reply_intent, follow_up_field, supersede_pending_ids, lead_updates, reason}`. `follow_up_field` nullable; không render text, không ghi DB.

- [ ] Test fail transcript đúng ba lượt người dùng cung cấp. Sau lượt 1 không có lead; sau lượt 2 không tự có purchase_intent=exploring; sau lượt 3 ownership=owns_vehicle, không có pending mua mở, không hỏi mua nữa, cách gọi vẫn bác.
- [ ] Test độc lập: “tôi cũng tính mua” cho exploring; “có xe rồi nhưng định mua thêm” vẫn exploring; “chỉ hỏi cho biết” -> không mở lead; từ chối/đổi chủ đề -> không gắn câu hỏi pending cũ vào câu đáp mới.
- [ ] Test hai actor, hai post/thread, replay cùng event, lease/stale state, correction đồng thời, opt-out và handoff. Quá hạn pending không tự khởi động lại một funnel.
- [ ] Implement transaction cập nhật state/lead/pending/outbox cùng nhau. Tính response plan trước, kiểm tra lại sau; không gọi `advance()` cho mọi SOCIAL_ACK. Đưa quyết định vào hàm thuần dùng chung, bỏ heuristics suy giá trị từ “hai từ trở lên” hoặc chỉ có chữ số.
- [ ] Khi khách nói đã có xe sau câu hỏi mua: supersede pending mua, lưu sở hữu; không gán no_current_interest vĩnh viễn. Khi chưa có bằng chứng mua thêm, ngừng hỏi thời gian/ngân sách/trả góp.
- [ ] Chỉ ghi lead_draft khi lead_updates có dữ kiện được chấp nhận hoặc giao dịch cần handoff; câu hỏi Nam chạy bao lâu không tạo lead nháp ASKED.
- [ ] Test migration trên DB sạch, DB cũ có dữ liệu và chạy lặp; cập nhật migration version và test consistency. PostgreSQL chỉ ghi verified nếu chạy qua disposable *_test DB.

### Task 4: Chọn lời đáp và hook theo nội dung

**Files:** `app/persona_fixture.py`, `app/persona_reply.py`, `app/persona_store.py`, `app/engine.py`; `tests/unit/test_persona_reply.py`, `tests/unit/test_peer_conversation.py`.

**Interfaces:** `select_peer_plan(snapshot: dict, decision: dict, address_term: str, recent_reply_ids: list[str], comment_id: str) -> dict`, trả plan ID cho renderer hiện có, gồm reason và fact IDs để trace. Không trả văn bản tự sinh.

- [ ] Viết test fail: unknown act không được phát “Mình hiểu ý bác rồi” như đã hiểu; không tự đính hook đang chờ vào mọi câu; social_close không cần câu hỏi; thiếu câu phù hợp -> hỏi rõ qua utterance duyệt hoặc review không gửi.
- [ ] Tạo release mới sau revision hiện tại (không sửa release 4), thêm utterance cho hỏi thời gian sử dụng, trao đổi cùng sở hữu, sửa hiểu nhầm, hỏi rõ ownership vs purchase. Fact demo bổ sung phải có source_kind fictional_fixture; thời lượng sử dụng phải có fact bắt đầu sử dụng và độ chính xác.
- [ ] Thay fallback `peer` chung bằng lựa chọn theo reply_intent. Không lặp nguyên một câu ở hai lượt liền nhau khi có biến thể phù hợp; hash chỉ dùng phá hòa giữa các câu có nghĩa phù hợp.
- [ ] Hook chỉ xuất hiện nếu decision cho follow-up và phù hợp điều vừa nói. Dùng giới hạn cấu hình `max_per_conversation` đã có; không hard-code `< 3` tách khỏi metadata. Tránh đếm toàn bộ outbox cho mỗi lượt: lọc đúng scope hoặc dùng hook_usage hiện có và ghi sau receipt đúng semantics.
- [ ] Renderer và dispatch recheck cùng kiểm tra ID, approval, checksum, revocation, state version, hook usage; đổi binding hoặc revoked fact trước gửi phải chặn.
- [ ] Ví dụ nghiệm thu có thể là: “Mình dùng chiếc này từ [fact đã duyệt]. Bác cũng đang đi Creta à?”; khi khách nói “tôi có xe rồi”: “À, bác đang có xe rồi. Mình hiểu nhầm là bác đang tìm mua.” Không bắt buộc hỏi thêm.

### Task 5: Demo và đánh giá đúng hội thoại

**Files:** `sandbox/server.py`, `sandbox/static/app.js`, `sandbox/static/index.html`, `tests/integration/test_sandbox_server.py`, `docs/demo/peer-persona.md`; tạo report trace mới trong `docs/demo/` khi thực thi.

- [ ] Test integration transcript ba lượt với model boundary mock cấu trúc mới, kiểm tra response, lead, pending, state và cách gọi sau từng lượt. Dùng cả transcript lệch chủ đề, không chỉ luồng happy path.
- [ ] UI hiển thị acts/subject, ownership/purchase_intent, evidence, follow_up_reason hoặc reason không hỏi; tách nhãn topic “social” với hành vi hội thoại cụ thể để người review thấy vì sao quyết định.
- [ ] Chạy `python3 -m unittest discover -s tests/unit -q`, `python3 -m unittest discover -s tests/integration -q`, `node --check sandbox/static/app.js`, `git diff --check`. Báo mọi failure; baseline 397/62 là số lịch sử cần đo lại, không mặc định pass.
- [ ] Sau khi test offline đạt mới build/restart sandbox, rồi chạy live. Không restart container giữa một hội thoại đang kiểm tra.
- [ ] Chạy ít nhất 8 hội thoại mới, mỗi hội thoại 4–6 lượt: hỏi trải nghiệm; đang sở hữu; muốn mua; muốn đổi xe; chỉ hỏi hộ người khác; từ chối; đính chính; hai actor cùng thread. Giữ trace raw, usage/cost hiện có; không reset ledger.
- [ ] Claude/người dùng review transcript theo 5 mục: đáp đúng nội dung, không ép funnel, nhớ đính chính, cách gọi đúng, hỏi có lý do. Mỗi mục đạt/không đạt cùng dẫn chứng; một lỗi nghiêm trọng như hỏi mua tiếp sau correction phải sửa trước khi gọi demo đạt.
- [ ] Báo rõ giới hạn của utterance library: câu ngoài coverage vẫn cần làm rõ/review. Chỉ phát hành bản demo mới sau khi lượt live cuối cùng và reviewer xem cùng một revision.

## Các điểm nhờ Claude phản biện

1. State ownership/purchase_intent/focus đã đủ giải thích các chuyển chủ đề trong 8 hội thoại chưa, hay đang thêm state không cần thiết?
2. Giữ utterance đã duyệt có đáp ứng mức tự nhiên mong muốn không? Nếu không, chỉ rõ trường hợp cần đổi ràng buộc thay vì tiếp tục thêm fallback.
3. Bằng chứng đa lượt cho “Creta giống bác” có ngăn nhầm thuộc tính Nam thành thuộc tính khách không?
4. Quy tắc “đã có xe” đã dừng funnel đúng ngữ cảnh mà không chặn người muốn mua thêm/đổi xe chưa?
5. Migration/state/dispatch có dùng chung transaction và không thêm một lớp hội thoại song song với engine hiện có không?

## Tự rà soát kế hoạch

- Từng nguyên nhân trong transcript có task và test tương ứng.
- Không có yêu cầu mỗi lượt phải hỏi; lead gate độc lập với slot còn thiếu.
- Phân biệt plan semantics, state commit, ID selection và dispatch; không cho model tự xác nhận state.
- Không gửi review sang Claude bằng tool; người dùng tự gửi hai tài liệu.
