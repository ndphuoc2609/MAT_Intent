# Chia sẻ câu chuyện persona — 29/10/2026

Đã chạy lại đúng sáu lượt người dùng báo với Luna thật trên sandbox 8771. Tạo hội thoại mới để dùng release 11; các bài cũ giữ release đã ghim.

## Hành vi đã đổi

- Khi khách trả lời mẫu xe và trùng xe persona, ưu tiên câu chuyện đã duyệt, có fact và provenance, thay cho “mình cùng tìm hiểu mẫu xe đó”. Nam kể đi làm tuyến Cầu Giấy–Mỹ Đình và cuối tuần về Nam Định. Câu chuyện của Nam không được sao chép sang persona khác vì có liên kết fact riêng.
- Sau khi khách trả lời thời điểm mua, ghi lead nháp nhưng không tự hỏi tiếp trả thẳng/trả góp/ngân sách. Câu hỏi lead chỉ tiếp tục từ việc xác định mẫu xe sang thời điểm mua; không chạy liên tục hết danh sách trường thiếu.
- Nhận xưng hô ở cuối câu như “bác ạ”, “anh nhé”.
- Nhận câu hỏi đánh giá xe, kể cả “đánh gía” sai vị trí dấu. Nam có câu trả lời dài hơn, gồm bối cảnh, điều thích và điều chưa ưng.
- Nhận “Bạn nhắn như AI á” là góp ý về cách nói. Trả lời rõ nhân vật mô phỏng bằng AI và dừng hỏi mua; không phủ nhận vai mô phỏng. Không bỏ qua gate sarcasm: sửa hướng dẫn phân loại thay vì ép gửi khi model còn đánh dấu sarcasm.

## Trích kết quả Luna thật

Khách: **Creta luôn á**

Nam: “Creta giống mình rồi. Mình đi làm tuyến Cầu Giấy–Mỹ Đình, cuối tuần lại về Nam Định. Nên mình hay kể chuyện xe theo hai kiểu đi đó, đi phố hằng ngày và đi xa cuối tuần. Bạn tính khoảng khi nào sẽ mua xe?”

Khách: **Chắc năm nay bác ạ**

Nam: “Vậy mình còn thời gian tìm hiểu trước khi quyết định nhỉ bác.” Không còn pending; lưu thời điểm mua “năm nay” với evidence nguyên câu khách.

Khách: **Bạn nhắn như AI á**

Nam: “Bạn nói đúng, đây là nhân vật mô phỏng bằng AI trong bản demo. Nãy mình hỏi dồn quá, mình dừng hỏi chuyện mua xe nhé.”

Khách: **bác đánh gía xe đang đi như nào**

Nam: “Với nhu cầu đi làm trong phố của mình thì Creta khá vừa vặn, mình thích vị trí ngồi và cảm giác gọn gàng. Cuối tuần đi xa thì điểm mình chưa ưng là tiếng ồn; mình không coi nó là chiếc xe cái gì cũng hay. Nếu chọn lại, mình vẫn muốn tự lái thử cả đoạn phố lẫn đường dài trước khi quyết.”

Phần cảm nhận này là **nội dung kịch bản hư cấu mới tạo cho demo**, có nguồn `fictional_fixture` và approval fixture; không phải đánh giá xe đã được người dùng thực xác nhận. Renderer và kiểm tra thu hồi fact vẫn áp dụng.

## Kiểm thử

- Thêm test tái hiện toàn bộ sáu lượt và xưng hô cuối câu; đã quan sát test đỏ trước khi sửa.
- Toàn bộ unittest: 503 mục, OK, hai nhóm PostgreSQL bỏ qua do không cấu hình database cho lượt này. Không thay migration/schema database.
- Contract regression: 199/199 đạt, không sửa dataset.
- Live Luna: 6/6 lượt đạt các kiểm tra nội dung, lead và pending.
- Log live: `persona-story-live-2026-10-29.jsonl`. Log offline: `/private/tmp/persona-story-full.log`, `/private/tmp/persona-story-contract.log`.

Giới hạn: vẫn chọn từ utterance đã duyệt. Chưa triển khai sinh câu tự do hoặc kiểm thử mọi cách diễn đạt; chưa chạy lại PostgreSQL, Meta thật và kiểm thử tải trong lượt sửa này.
