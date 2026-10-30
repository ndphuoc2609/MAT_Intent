# Thiết kế đề xuất: hội thoại persona theo nội dung và trạng thái người nói

Trạng thái: đề xuất để người dùng gửi Claude review; chưa triển khai.

## Mục tiêu

Nam trả lời đúng điều người đối thoại đang nói và nhớ các điều họ đã đính chính. Thu lead chỉ khi có nhu cầu mua thể hiện trong hội thoại. Một lượt có thể không hỏi gì; nếu hỏi thì tối đa một câu và câu đó phải liên quan đến điều vừa nói.

## Bằng chứng và nguyên nhân

Transcript người dùng cung cấp:
1. “Bác chạy bao lâu rồi” bị đáp “Mình hiểu ý bác rồi” và hỏi mẫu xe.
2. “Creta giống bác” bị dùng để tiếp tục hỏi thời điểm mua.
3. “tôi có xe rồi” bị gọi là “cô” và tiếp tục hỏi thời điểm mua.

Đối chiếu code:
- `lead_flow.address()` bỏ dấu rồi dò từng từ: “có” -> `co` -> “cô”. Đây là lỗi xác định cách gọi, không phải Luna đoán giới tính.
- `persona_query._deterministic()` chưa có câu hỏi thời gian sử dụng “chạy bao lâu”.
- `Engine._peer_reply()` gán mọi câu không khớp vào `peer`; fixture `peer` chỉ có câu “Mình hiểu ý … rồi”.
- `Engine.process()` gọi `advance()` với gần như mọi SOCIAL_ACK của fixture; `advance()` chọn trường đầu tiên chưa có trong FIELDS, không có điều kiện nhu cầu mua.
- `_peer_reply()` lấy câu hỏi từ pending đang mở ngay cả khi khách vừa đổi chủ đề hoặc sửa thông tin.
- Hồ sơ chỉ có các trường lead; chưa biểu diễn “đã có xe”, “không có nhu cầu hiện tại”, trạng thái hỏi người dùng xe ngang hàng và câu đính chính.
- Luna/Jev nhãn social không đủ để phân biệt hỏi kinh nghiệm, nói sở hữu, trả lời pending hay đính chính. Jev shadow chỉ so sánh, không được biến thành trọng tài.

## Quyết định thiết kế đề xuất

Giữ renderer chỉ chọn utterance/hook đã duyệt. Bổ sung phân tích hành vi hội thoại có cấu trúc trong lượt Luna hiện có; Python quyết định state và chọn ID. Không thêm model call cho mỗi lượt. Rule chỉ xử lý các trường hợp ngôn ngữ hẹp, chắc chắn; không dùng regex mở rộng để đoán toàn bộ ý nghĩa.

Các phương án đã cân nhắc:
- Thêm regex và nhiều câu đồng nghĩa: sửa được vài ví dụ, vẫn thiếu quan hệ người nói/người được hỏi và state nhu cầu mua.
- Cho model tự viết toàn bộ câu trả lời: linh hoạt hơn nhưng thay đổi ràng buộc đã có về utterance được duyệt, tăng phạm vi kiểm tra claim; không chọn trong đề xuất này.
- **Đề xuất:** model trích xuất hành vi hội thoại, bằng chứng và chủ thể; state machine điều tiết lead; renderer chọn câu đã duyệt. Cách này giữ cơ chế kiểm tra hiện có nhưng độ phong phú vẫn phụ thuộc bộ câu đã duyệt. Không cam kết hội thoại tự do ngang model sinh văn bản.

## Contract cần đạt

- Phân biệt câu hỏi về Nam với thông tin của người bình luận.
- “Bác chạy bao lâu rồi”: nhận là hỏi thời gian dùng xe; trả từ fact có nguồn và thời điểm tương ứng, hoặc hỏi rõ “Bác hỏi mình dùng chiếc Creta này bao lâu rồi à?”. Không mở lead.
- Fact mua tháng 03/2024 không tự chứng minh thời điểm bắt đầu sử dụng. Chỉ tính thời lượng khi có fact ngày/tháng bắt đầu dùng, độ chính xác và ngày hiện tại của event. Có thể tạo thêm fact hư cấu được ghi nguồn riêng cho demo.
- “Creta giống bác” đứng một mình chưa chứng minh nhu cầu mua. Nếu không rõ đang sở hữu hay định mua thì hỏi đúng điểm này, không hỏi tài chính/thời điểm.
- “Tôi có xe rồi”: cập nhật trạng thái sở hữu, dừng câu hỏi mua đang chờ; không tự suy ra dòng xe hoặc từ bỏ mọi ý định mua vĩnh viễn. “Có xe rồi nhưng định đổi xe” vẫn có nhu cầu đổi xe.
- Cách gọi: nhận xưng hô có dấu trong vai trò gọi người đối thoại; “có”, “cổ”, “cơ” không thành “cô”. Giữ cách gọi trước đó của cùng actor/thread khi lượt mới không có cách gọi rõ; mặc định “bạn”. Không suy tuổi/giới tính.
- 0 hoặc 1 câu hỏi/lượt. Không bắt buộc hỏi sau cảm ơn, chào kết thúc, câu từ chối hay khi nội dung đã đủ.
- Hook dựa trên mục đích hội thoại và điều vừa nói; thiếu trường lead không tự tạo quyền hỏi.
- Không tạo lead_draft chỉ vì câu chuyện đang tiếp diễn. Chỉ ghi bản nháp khi có dữ kiện lead được chấp nhận hoặc chuyển giao liên quan.
- Tôn trọng opt-out, handoff, version, TTL, dedupe, bằng chứng và revocation. Giữ fixture được gắn nhãn hư cấu.

## State tối thiểu

Phạm vi (page_id, post_id, thread_root_id, actor_id):
- address_term: cách gọi có bằng chứng hoặc null.
- ownership_status: unknown | owns_vehicle | no_vehicle.
- purchase_intent: unknown | no_current_interest | exploring | transaction_requested.
- conversation_focus: persona_experience | ownership_exchange | purchase_exploration | other.
- Mỗi giá trị có source_message_id(s), excerpt(s), updated_at; không điền bằng thuộc tính Nam.

Ownership và purchase_intent độc lập. Dữ kiện mâu thuẫn phải được đính chính có bằng chứng hoặc hỏi rõ; không chọn giá trị chỉ vì field còn trống.

Lead chỉ bắt đầu/tiếp tục khi purchase_intent=exploring và khách không vừa đổi chủ đề/đính chính/từ chối. transaction_requested đi handoff. owns_vehicle đơn độc không kích hoạt lead. Khách có xe rồi sau câu hỏi mua phải dừng pending mua cho đến khi có bằng chứng muốn mua/đổi thêm.

## Nghiệm thu sản phẩm

Chấm cả hội thoại liên tiếp và state, không chỉ số câu hỏi hoặc chuỗi từ cấm. Reviewer đọc transcript ẩn tên phiên bản để chấm: đáp đúng nội dung, không suy diễn nhu cầu, nhớ sửa sai, cách gọi nhất quán, câu hỏi có lý do. Bất kỳ hỏi mua tiếp sau “tôi có xe rồi” khi không có ý định mua thêm là fail.
