# Kiểm thử hội thoại demo với Luna — 29/10/2026

Đã chạy năm kịch bản với Luna thật trong sandbox. Nhân vật được tạo thành bản chụp **kịch bản hư cấu**, có thông tin xe/nghề lấy từ bản ghi đã chọn; đây không phải sự chấp thuận của nhân vật ngoài đời. Không gửi tin nhắn ra Facebook.

## 1. Coach Tuấn Gym — hỏi xe, muốn mua, sau đó nói đã có xe

- Khách: tập gym bác đi bằng xe gì
- Coach: Mình đang đi Ford Ranger XLS.
- Khách: tôi cũng tính mua
- Coach: Bác cũng đang tính mua xe à, mình cùng trao đổi nhé. Bác đang nghiêng về mẫu xe nào?
- Khách: Creta
- Coach: Mình cùng tìm hiểu mẫu xe đó nhé bác. Bác tính khoảng khi nào sẽ mua xe?
- Khách: tôi có xe rồi
- Coach: À, bác có xe rồi. Mình hiểu nhầm là bác đang tìm mua, xin lỗi nhé.

Sau câu “Creta”, hồ sơ nháp ghi `interested_vehicle = Creta` với evidence từ câu khách. Khi khách nói có xe, luồng hỏi mua dừng. Trong một lần chạy lại, Luna trả `UNRESOLVED` ở lượt cuối khiến bot không phản hồi; đã bổ sung xử lý và test hồi quy cho đúng biến thể đó.

## 2. Thầy Vũ Đình Tuấn — hỏi thông tin cá nhân

- Khách: anh đang đi xe gì
- Thầy: Mình đang đi Honda Civic RS.
- Khách: anh làm nghề gì
- Thầy: Mình làm Giảng viên Đại học Bách Khoa.
- Khách: cảm ơn anh
- Thầy: Có gì đâu anh, mình cùng trao đổi thôi.

Luna từng phân loại câu hỏi nghề nghiệp là ngoài phạm vi; đã giới hạn xử lý bổ sung cho câu hỏi nghề nghiệp rõ ràng thuộc release hư cấu.

## 3. Tuấn Tech — thời điểm mua và từ chối hỏi tiếp

- Khách: bác đi bằng xe gì
- Tuấn: Mình đang đi Hyundai Tucson Turbo 2024.
- Khách: tôi cũng tính mua xe
- Tuấn: Hay đó bác, mua xe cũng nên cân nhắc kỹ. Bác đang nghiêng về mẫu xe nào?
- Khách: tháng sau
- Tuấn: Vậy mình còn thời gian tìm hiểu trước khi quyết định nhỉ bác.
- Khách: đừng hỏi nữa
- Tuấn: Ok bác, mình không hỏi thêm nữa nhé.

Câu “tháng sau” được ghi là thời điểm, không bị nhầm thành tên xe. Khi khách bảo dừng, không tiếp tục hỏi.

## 4. Chủ tiệm sửa xe — hỏi đại lý và hỏi hộ

- Khách: bác đang đi xe gì
- Tuấn: Mình đang đi Suzuki Swift 2020.
- Khách: xe đó bác mua ở đâu
- Tuấn: Mình không rõ thông tin đại lý, bác đang tìm ở khu nào?
- Khách: tôi chỉ hỏi hộ thôi
- Tuấn: Ừ, mình cùng bác tìm hiểu để tham khảo nhé.

Đây là kết quả chạy lại sau khi bổ sung câu trả lời đã duyệt cho đại lý chưa có dữ liệu; hệ thống không tự bịa tên đại lý.

## 5. Nhân vật khảo sát — có xe và muốn đổi xe

- Khách: anh đang đi xe gì
- Nhân vật: Mình đang đi Toyota Hilux hai cầu.
- Khách: tôi có xe rồi
- Nhân vật: Vậy là anh đã có xe rồi nhỉ.
- Khách: tôi muốn đổi xe
- Nhân vật: À, anh có xe rồi mà vẫn muốn đổi, nghe cũng hợp lý. Anh đang nghiêng về mẫu xe nào?
- Khách: Tucson
- Nhân vật: Mình cùng tìm hiểu mẫu xe đó nhé anh. Anh tính khoảng khi nào sẽ mua xe?

## Dữ liệu đối chiếu

- `five-persona-final-2026-10-29.jsonl`: 18 lượt của năm kịch bản, gồm ngữ nghĩa, routing, pending và hồ sơ lead.
- `five-persona-v9-check-2026-10-29.jsonl`: bảy lượt chạy lại Coach và thợ máy; có ghi lại lỗi `UNRESOLVED` được phát hiện sau đó.
- `five-persona-v9-recheck-2026-10-29.jsonl`: bảy lượt chạy lại sau sửa lỗi cuối; cả bảy đều có phản hồi. Lượt “tôi có xe rồi” lần này được Luna phân loại `UPDATE_SLOT`; nhánh `UNRESOLVED` được xác minh bằng test hồi quy với đúng ngữ nghĩa đã ghi nhận trước đó.
- Khi nâng phiên bản release, bản chụp persona cũ và mới phải cùng tồn tại; đã sửa lỗi trùng tên nội bộ gây HTTP 500 và thêm test hồi quy.
- Kiểm thử tự động: 430 unit và 65 tích hợp đạt.

Giới hạn: tập câu trả lời vẫn là utterance đã duyệt, vì vậy các câu nằm ngoài phạm vi có thể chuyển duyệt thay vì trả lời tự động. Model có thể phân loại khác nhau giữa các lần chạy; năm kịch bản không chứng minh mọi cuộc hội thoại đều tự nhiên.
