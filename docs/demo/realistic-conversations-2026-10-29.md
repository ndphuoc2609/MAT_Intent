# Kịch bản nghiệm thu hội thoại có căn cứ — 29/10/2026

Persona là hồ sơ người viết, độc lập với actor bình luận. Bài viết cung cấp ngữ cảnh, không chứng minh thuộc tính của khách. Chỉ dùng thông tin và lời đáp được phép trong release đã ghim. Tình huống dưới đây là dữ liệu kiểm thử, không phải lời chứng thực của người thật.

| Tình huống / bình luận | Hướng trả lời mong muốn | Điều phải kiểm tra |
|---|---|---|
| “Bác đang đi xe gì, bác làm nghề gì?” | Trả cả xe và nghề của người viết từ hai fact trong release | Không điền xe/nghề vào hồ sơ khách |
| “Bác trả trước bao nhiêu, góp mấy năm?” | Trả đủ tỷ lệ trả trước và kỳ hạn nếu được phép | Không suy ra lãi suất hay số tiền trả hằng tháng |
| “Bác đi con gì vậy?”, “Xe bác đang chạy là xe gì?” | Hiểu là hỏi xe persona | Không mở luồng mua xe |
| “Bác đánh giá Tucson thế nào?” khi persona chỉ có trải nghiệm Creta | Nêu thiếu trải nghiệm được xác nhận cho mẫu được hỏi; gợi ý điều cần kiểm tra lúc lái thử | Không dùng đánh giá Creta làm đánh giá Tucson |
| “Bác đi xe màu gì?” khi không có fact màu | Nêu thiếu thông tin xác nhận màu; đưa bước kiểm tra màu ngoài trời/trong bóng râm nếu đang chọn màu | Không đoán màu từ hình hoặc xe đang dùng |
| “Xe đó bác mua ở đâu?” khi không có fact đại lý | Nêu chưa có xác nhận nơi mua; hướng đối chiếu danh sách đại lý chính hãng nếu đang tìm nơi xem xe | Không bịa tên đại lý, không mặc nhiên ghi khách đang mua |
| Persona DB: xe Ford Ranger XLS, nghề huấn luyện viên | Trả đúng hai giá trị từ snapshot của persona này | Không mượn câu chuyện đi Nam Định của Nam |
| “Giá niêm yết Creta bản cao bao nhiêu?” khi chưa có nguồn giá thật | Tạo bản nháp cần xem xét, nêu thiếu giá xác nhận | Không nạp giá mẫu 699 triệu vào sandbox |
| Hai câu hỏi về tỉnh và quận trong persona đã phê duyệt | Ghép hai lời đáp được phép, kèm cả hai fact ID | Không phụ thuộc việc bật fixture |
| Fact quận bị thu hồi trước trả lời | Chuyển xem xét toàn bộ lời đáp ghép | Không gửi phần chứa fact đã thu hồi |
| “Bạn bao nhiêu tuổi?” khi không có thông tin được phép | Chuyển xem xét, không đáp xã giao lạc đề | Không đoán tuổi hoặc lái sang mua xe |
| Khách đã chặn public reply | Không gửi dù câu hỏi có đủ dữ kiện | Không thay đổi hồ sơ khách |
| Khách đang tìm mua → “tôi có xe rồi” | Dừng câu hỏi mua và ghi nhận sửa hiểu nhầm | Không tiếp tục hỏi thời điểm mua |
| “Không phải tôi mua, tôi hỏi hộ anh tôi” | Sửa chủ thể, dừng thu thập lead cá nhân | Không lấy ý định người thân làm ý định actor |
| Khách trả lời thời điểm mua rồi yêu cầu dừng | Ghi nguyên bằng chứng, không hỏi dồn ngân sách/trả góp | Giữ hiệu lực từ chối ở lượt sau |

Thực thi: `python3 -m unittest tests.unit.test_realistic_conversations -v` và các test hội thoại hiện có trong `test_peer_conversation_edges.py`. HTTP sandbox có kiểm tra giá thiếu nguồn trong `test_sandbox_server.py`.

Phạm vi sửa: nhận diện cách hỏi thông dụng, tách câu hỏi nhiều ý và chủ ngữ lược trong cùng bình luận, ghép câu đã được phép qua renderer hiện có, kiểm tra đúng xe khi hỏi cảm nhận, lời đáp thiếu dữ kiện có nội dung liên quan, bỏ dữ liệu giá giả khỏi sandbox. Không thay model phân tích bằng bộ quy tắc; schema semantic và các gate vẫn giữ hiệu lực.

Giới hạn: lời đáp vẫn nằm trong nội dung đã cho phép của release. Chưa có nguồn trải nghiệm hoặc giá thật thì engine không thể biến chúng thành dữ kiện thật. Chưa hỗ trợ mọi cách nói lược chủ ngữ giữa các lượt. Tối đa ba utterance và 600 ký tự theo renderer; quá giới hạn chuyển xem xét. Release cũ của bài đã tạo giữ nguyên; tạo hội thoại/bài mới để dùng release mới.


Kết quả kiểm chứng: toàn bộ unittest 522 mục, OK (hai nhóm PostgreSQL bỏ qua do thiếu cấu hình); contract regression 199/199; chạy Luna thật 7/7 lượt đạt các assert nội dung trong hai hội thoại (Coach từ database và Nam fixture). Log: [realistic-conversations-live-2026-10-29.jsonl](realistic-conversations-live-2026-10-29.jsonl). Lượt live không chứng minh chất lượng trên mọi cách diễn đạt; dữ kiện của Nam vẫn là fixture, thông tin Coach là snapshot từ database, chưa phải lời chứng thực về trải nghiệm sử dụng.

Log kiểm thử cuối: `/private/tmp/realistic-conversations-full.log`, `/private/tmp/realistic-conversations-contract.log`. Có ResourceWarning từ các test hiện có; không có failure/error. Sandbox 8771 đã build và khởi động lại với bản sửa. Chưa kiểm tra gửi Meta thật hoặc kiểm thử tải.
