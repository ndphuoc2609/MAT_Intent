# Grounded persona generation — triển khai và đo kiểm

Ngày 30/10/2026 · Nhánh `codex/grounded-persona-20261030`. Hội thoại chạy qua `FakeSender` trong sandbox.

## Hành vi đã triển khai

- Persona của bài được lấy từ binding `(page_id, post_id)`. Fact đọc/ghi theo đúng `persona_id`; dữ kiện ACTIVE được dùng chung cho khách của persona đó nhưng không lẫn giữa hai persona.
- Luna chạy hai pha: `persona_analysis_v1` trả semantic, needs, confidence và peer interpretation; engine cấp fact chính xác theo key/topic, fact của bài do persona viết và memory đủ điều kiện; `persona_turn_v1` trả lời kèm claims và tối đa hai proposals.
- Nếu có key cụ thể trong `needs`, engine chỉ cấp key đó thay vì mở rộng cả topic. Pha một lỗi thì cấp toàn bộ fact được phép theo đường fallback hiện có.
- Fact mới nằm ngoài release/checksum, gắn `generated`, có trạng thái PENDING/ACTIVE/DISCARDED/REVOKED và thời hạn PENDING 30 phút. Proposal thành ACTIVE sau FakeSender xác nhận SENT; bị chặn hoặc gửi lỗi thì DISCARD. Có giới hạn hai proposal/lượt, hai mươi/ngày/persona và nút thu hồi trong sandbox.
- Lời đáp phải trích đúng proposal ID ở `reply.fact_ids` và claim. `proposal.value` phải xuất hiện nguyên văn; fact có trong cùng câu cũng phải có claim riêng. Kiểm tra chỉ bảo đảm nhất quán nội bộ, không chứng minh câu chuyện có thật.
- Bài do chính persona viết được dùng làm bằng chứng cho chính các fact IDs gắn với bài. Validator chặn câu đáp mâu thuẫn với bài. Nội dung khách và bài của người khác vẫn không phải nguồn xác nhận fact persona.
- Sandbox tự gửi nội dung hợp lệ qua FakeSender, không có duyệt nháp. UI hiển thị intent/confidence, fact đã cấp, proposals/status và lý do từ chối; generated facts có nhãn và nút thu hồi.
- Caller semantic-only cũ vẫn giữ tương thích khi không bật chế độ hai pha. Không thêm `send_lead` vào `AUTO_ACTIONS`.

## Sáu hội thoại mẫu chạy với Luna thật

Mỗi dòng là một comment độc lập, chạy trên hồ sơ synthetic. Nam là fixture hư cấu; Linh là snapshot DB synthetic, không phải người thật. Bài của mỗi persona ghi “Mình đang dùng [Creta/Tucson]” và context chỉ nhận fact ID chiếc xe đã gắn vào bài. `source_kind=other` ở Linh là nguồn snapshot, không xác nhận rằng chuyện đời sống mới là sự thật.

| # | Persona · comment | Fact được cấp | Proposal | Kết quả gửi qua FakeSender |
|---|---|---|---|---|
| 1 | Nam · “Bạn đang đi xe gì?” | `vehicle.current.model=Hyundai Creta` | Không có | “Mình đang đi Hyundai Creta.” · `OK → GENERATED_REPLY_READY → AUTO_REPLY` |
| 2 | Nam · “Bạn mua xe ở đại lý nào?” | Fact xe Creta gắn với bài | Không có; câu Luna bị chặn `REPLY_TOPIC_MISMATCH` | Fallback: “Mình chưa có thông tin xác nhận đại lý đã bán chiếc xe này. Nếu bạn đang tìm nơi xem xe, nên đối chiếu địa chỉ trong danh sách đại lý chính hãng trước khi đến.” · `APPROVED_FALLBACK_READY → AUTO_REPLY` |
| 3 | Nam · “Bạn đi làm tuyến nào?” | `travel.commute=Cầu Giấy–Mỹ Đình`; fact xe từ bài | Không có | “Mình đi làm tuyến Cầu Giấy–Mỹ Đình.” · `OK → GENERATED_REPLY_READY → AUTO_REPLY` |
| 4 | Linh · “Bạn đang đi xe gì?” | `vehicle.current.model=Hyundai Tucson` | Không có | “Mình đang dùng Hyundai Tucson.” · `OK → GENERATED_REPLY_READY → AUTO_REPLY` |
| 5 | Linh · “Bạn làm nghề gì?” | `occupation.title=Giáo viên`; fact Tucson từ bài | Không có | “Mình là giáo viên.” · `OK → GENERATED_REPLY_READY → AUTO_REPLY` |
| 6 | Linh · “Bạn có kỷ niệm nào đáng nhớ với Tucson không?” | Fact Tucson từ bài | `gen.memory.memorable_tucson_trip` · ACTIVE sau khi gửi | “Mình nhớ nhất là một chuyến đi cuối tuần cùng gia đình.” · `OK → GENERATED_REPLY_READY → AUTO_REPLY` |

Tổng sáu mẫu: **6/6** qua cổng gửi, 12 attempts, p50 **12.75 giây**, p95 **15.50 giây**, chi phí **$0.008224**. Đây là smoke evaluation tổng hợp, không phải đánh giá người dùng thật.

Transcript đầy đủ: `/tmp/persona-live-six-conversations-final.json`.

## A/B trên bộ 12 hội thoại synthetic (48 lượt mỗi bản)

Hai bản chạy tuần tự với cùng bộ dữ liệu đóng băng, cùng model `openai/gpt-6-luna`, Engine và FakeSender. p95 dùng nearest-rank. Chi phí lấy từ provider; không có cost/attempt bị thiếu.

| Chỉ số | Legacy | Hai pha + fact grounded |
|---|---:|---:|
| Qua cổng gửi | 13/48 (27.1%) | 29/48 (60.4%) |
| Lời Luna mới được gửi | — | 7 |
| Fallback persona đã duyệt được gửi | — | 19 |
| Response từ đường engine hiện có | — | 3 |
| Attempts tổng | 48 | 94 |
| Retry attempts | 0 | 0 |
| p50 độ trễ | 5.17 giây | 16.45 giây |
| p95 độ trễ | 10.74 giây | 23.10 giây |
| Chi phí provider | $0.017774 | $0.060449 |
| Lượt không gửi | 35 | 19 |

Bản hai pha tăng số lượt qua cổng gửi **16/48** so với bản legacy trên bộ này; đổi lại độ trễ và chi phí tăng. Trong bản hai pha, 9 lượt không gửi vì `REPLY_TOPIC_MISMATCH`; các lý do khác gồm unresolved, schema/candidate không hợp lệ và handoff. Có 1 proposal ACTIVE, 4 proposal bị DISCARD cùng lời đáp bị chặn, 3 proposal bị từ chối (trong đó có proposal lặp chi tiết từ câu hỏi khách). Không có HTTP retry; hai lượt dừng sau pha phân tích vì schema semantic không qua kiểm tra.

Artifacts chỉ ở `/tmp`: `/tmp/persona-legacy-current.json` và `/tmp/persona-generated-current.json`. Run IDs: `legacy-e62b41f7a7594784a096f15ec76cd6be`, `generated-c0d8e3f32d924604abad5bb233ab443a`.

## Kiểm chứng

- Unit: **607 tests OK** (`python3 -m unittest discover -s tests/unit -q`).
- Integration: **91 tests OK** (`python3 -m unittest discover -s tests/integration -q`).
- `node --check sandbox/static/app.js`, `py_compile` cho các module đã sửa và `git diff --check`: đạt.
- Test phủ migration SQLite 010/011 idempotent, persona isolation, key-scoped facts, proposal policy, generated delivery lifecycle, race, revoke, outbox, bài do persona viết, fallback và lỗi audit.
- Handoff trước đó nêu baseline 559 unit/78 integration; con số này là thông tin lịch sử, không chạy lại trên commit sạch trong lượt này vì workspace đã có thay đổi bẩn từ trước.

## Giới hạn và rủi ro

- Không có `MAT_INTENT_PERSONA_TEST_DSN`, nên PostgreSQL migration chưa được chạy trên DB test thật trong lượt này. SQLite migration đã được kiểm tra.
- Không có transcript khách thật hoặc chấm mù bởi người; số liệu A/B đo routing/gate, không chứng minh hội thoại tự nhiên hơn.
- Generated story là nội dung tổng hợp, có thể sai sự thật dù qua kiểm tra. Auto-send hiện chỉ tới FakeSender.
- Không kiểm thử tải production hoặc chi phí Jev shadow.

## Bổ sung sau lỗi tạo bài

Đã nhập các trường `category`, `role`, `perspective`, `sample_quote` của persona DB vào release và ngữ cảnh tạo bài/trả lời. Thay đổi các trường này tạo release mới; ID nguồn DB không đưa sang model. Câu mẫu hướng dẫn giọng nói và không tự trở thành bằng chứng cho trải nghiệm cá nhân.

Tạo bài cấp các fact hợp lệ khi needs chọn rỗng; prompt phân biệt predicate fact có sẵn với `gen.*`, giới hạn lời thêm không nguồn và có một lần thử lại khi post JSON sai hợp đồng. Giữ nguyên validator nguồn.

Kiểm chứng mới: 608 unit tests và 92 integration tests OK; syntax/compilation/diff check đạt. Hai lần tạo bài bằng Luna thật trên Nam và Linh synthetic qua validator. Sau khi khởi động lại sandbox 8770, tạo bài qua HTTP API trả 201 và validation OK. Số liệu A/B phía trên thuộc phiên bản trước bổ sung này; chưa chạy lại A/B hoặc chấm người cho bổ sung này.
