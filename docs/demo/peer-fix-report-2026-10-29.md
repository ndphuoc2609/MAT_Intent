# Sửa lỗi hội thoại persona — 2026-10-29

Bản sandbox review: http://127.0.0.1:8771/. Tạo hội thoại mới để dùng fixture revision 8; các release và binding trước được giữ nguyên.

- Đính chính chủ thể dạng Luna `UNRESOLVED`: pending mua được đóng; các trường lead ASKED/ANSWERED có nguồn trong đúng thread chuyển DECLINED kèm evidence đính chính. Audit lưu giá trị trước; dữ kiện từ thread khác không bị ghi đè. Phạm vi xử lý là fixture và câu đính chính hẹp được kiểm tra evidence.
- Từ chối dạng Luna `CANCEL_REQUEST`: trường đang hỏi chuyển DECLINED và ghi evidence, đồng thời pending đóng.
- Cách gọi với dấu phẩy được giữ: “Chào bác, tôi có xe rồi” gọi lại bác; câu về người thứ ba vẫn không được dùng để suy cách gọi.
- “Tôi chỉ hỏi hộ thôi” được hiểu là làm rõ, có lời xác nhận riêng; không tạo lead cho người nói.
- Các câu ghi nhận mẫu xe/thời điểm/phương thức thanh toán được chọn theo trường vừa trả lời; lời thoại thuộc release fixture đã duyệt.

Kiểm tra: 424 unit tests, 62 integration tests, `node --check` và `git diff --check` đều đạt. Các test bổ sung bao gồm semantic Luna thực tế, sarcasm và evidence không khớp.

Live: [11 lượt revision 6](peer-fix-live-2026-10-29.json) xác nhận từ chối, xưng hô và phản hồi lead; [4 lượt revision 7](peer-fix-live-v7-2026-10-29.json) xác nhận đính chính và đóng cả hai trường; [2 lượt revision 8](peer-fix-live-v8-2026-10-29.json) xác nhận câu hỏi hộ có phản hồi riêng. Các trường hợp trên dùng dữ liệu hư cấu và model thật; đây là kiểm tra có mục tiêu, không phải benchmark chất lượng hội thoại.

Chưa chạy PostgreSQL integration hoặc một batch hội thoại mới toàn diện cho revision 8. Model chưa có trường phân tích hội thoại chuyên biệt; cách nhận diện ngoài các câu đã hỗ trợ vẫn có thể cần review.
