# Grounded persona generation

Ngày 30/10/2026. Thiết kế đã được triển khai theo yêu cầu của người dùng; kết quả đo và giới hạn nằm trong [báo cáo demo](../../demo/grounded-generation-implementation-2026-10-30.md).

## Mục tiêu và lựa chọn

Luna viết lời đáp tự nhiên theo persona gắn với bài viết, còn engine giữ quyền quyết định fact nào được cấp, claim nào có căn cứ, có gửi hay không và trạng thái khách được cập nhật ra sao. Người bình luận và persona là hai actor riêng.

Các phương án đã cân nhắc:

- Mở rộng thư viện câu duyệt: ổn định nhưng không nối được hội thoại mở.
- Gọi model viết lại sau khi phân tích: dễ tách trách nhiệm nhưng thêm một lượt model.
- **Hai pha Luna, được chọn:** pha phân tích chọn needs; engine cấp fact; pha viết lời trả `reply_text`, `claims` và `fact_proposals`. Cấu trúc riêng giúp engine có thể cấp dữ kiện đúng chủ đề mà không để model tự quyết định kho persona.

Không thêm dependency. Caller semantic-only cũ vẫn dùng schema và hành vi cũ khi không bật `persona_phase`.

## Persona và nguồn dữ kiện

- Persona của comment lấy từ binding của `(page_id, post_id)`, tức người đã tạo bài là người được mô phỏng. Không hard-code Nam.
- Mọi truy vấn và ghi fact dùng `persona_uuid` của binding. Fact của persona A không được cấp cho B; fact ACTIVE của A dùng chung cho các khách của A.
- Fact đã duyệt lấy từ release hiện hành. Fact Luna tạo nằm riêng trong `persona_generated_facts`, mang `generated=True`, `source_kind='generated'`, `approval='generated'`. Nhánh validator chấp nhận riêng loại này; các kiểm tra fact thường không được nới lỏng.
- Generated facts không vào release, checksum hay bundle đã publish. Mỗi fact có trạng thái PENDING, ACTIVE, DISCARDED hoặc REVOKED và có thể thu hồi.
- Nguồn fixture, database snapshot và generated phải được hiển thị khác nhau. Snapshot DB không tự trở thành trải nghiệm cá nhân đã được xác nhận.

## Hai pha Luna

### Pha 1: phân tích

Schema `persona_analysis_v1` trả semantic v2, `peer_interpretation`, `diagnostics` và `needs {topics, keys}`; không có reply. Cần tách câu hỏi về persona khỏi lời khách nói về chính họ, giữ phủ định/đính chính và không lấy thuộc tính persona gán cho khách.

`diagnostics.intent_confidence` là tự đánh giá chưa hiệu chuẩn của Luna. Intent score Jev là số liệu riêng từ shadow call khi bật; không lấy confidence làm xác suất đúng hoặc dùng điểm để vượt gate.

### Cấp ngữ cảnh

Engine chọn fact theo `needs`, ưu tiên key cụ thể; chỉ mở toàn topic khi pha 1 yêu cầu topic mà không có key cụ thể cho topic đó. Engine thêm fact authored-post được tham chiếu, ký ức đủ điều kiện và danh sách `gaps`. Nếu pha 1 lỗi, cấp toàn bộ fact an toàn và dùng parser semantic hiện có. Context ký được theo persona, release, comment và scope actor.

### Pha 2: viết lời

Schema `persona_turn_v1` trả lời ngắn, claims, tối đa một câu hỏi và tối đa hai `fact_proposals`. Proposal dùng namespace đóng và phải khai báo trong claim bằng `new1`/`new2`. Luna chỉ được đưa chi tiết đời sống mới qua proposal; không tự tạo giá, khuyến mãi, tồn kho, thông số, bảo hành, đại lý, liên hệ hoặc sự kiện về người khác.

Engine kiểm tra proposal về namespace, key trùng, giới hạn hai fact/lượt và hai mươi/ngày/persona, số/giá/liên hệ/nhạy cảm và mốc thời gian. Proposal phải được hỗ trợ bởi câu trả lời đó; không được lấy từ câu hỏi hoặc suy đoán của khách.
`proposal.value` phải xuất hiện nguyên văn trong `reply_text` và claim tương ứng. Cả `reply.fact_ids` lẫn ít nhất một `claims[].fact_ids` phải dẫn đúng `new1`/`new2`; câu có thêm fact đã cấp phải có claim riêng trỏ đúng fact đó, nếu không chi tiết sẽ bị chặn.

## Bài viết do persona tạo

Bài sinh qua `persona_post_v1`, cũng theo needs → fact → proposal. Proposal ở bài thành PENDING trong lúc tạo; chỉ kích hoạt sau khi bài được tạo và binding thành công; lỗi kiểm tra/tạo bài thì discard. Bài lưu text, persona, release và fact IDs đã dùng.

**Đảo ngược có chủ ý:** bài do đúng persona trong binding tự viết là bằng chứng về lời persona đã đăng, nhưng chỉ cho các fact IDs được gắn vào bài. Pha trả lời nhận bài và các fact này; validator chặn claim mâu thuẫn. Comment khách và bài của người thứ ba tiếp tục chỉ là ngữ cảnh, không phải nguồn fact persona. Bài không tự xác nhận fact mới nếu không có proposal được kiểm tra.

## Kiểm tra và gửi

- Validator kiểm source/persona/release, fact IDs, claims, subject, giá trị, phủ định, evidence khách, câu hỏi, kênh public/private, memory eligibility và mâu thuẫn với bài authored. Không dùng text do model sinh làm evidence cho semantic.
- Pha 2 lỗi hoặc reply không qua kiểm tra thì không tự gửi reply đó. Persona có utterance đã duyệt phù hợp có thể dùng làm fallback; nếu không có thì không gửi. Không dùng utterance Nam cho persona khác.
- Fact mới được ghi PENDING cùng outbox INTENDED. Chỉ SENT làm fact ACTIVE; BLOCKED, FAILED hoặc hết TTL làm DISCARD. Precheck tức thời, drain và retry đọc lại binding, block/handoff, fact revocation và context. Unique key giải quyết race giữa hai khách; lượt thua không gửi.
- Sandbox tự gửi nội dung hợp lệ bằng `FakeSender`, không có bước duyệt của người. Đường giao nhận hiện chỉ chạy trong sandbox.
- Không phủ nhận là AI, không giả làm người đang gõ, không xin thông tin liên hệ nơi công khai, không báo giá/nhận cọc/xác nhận lịch hoặc giao dịch. Kill switch, opt-out, handoff, TTL và dedupe tiếp tục có hiệu lực.

## UI, audit và tương thích

Sandbox hiển thị intent, confidence, Jev score khi có, fact được cấp, proposal được nhận/từ chối kèm lý do, status generated và nút thu hồi đúng persona của hội thoại. Audit mỗi lượt lưu persona ID, fact IDs, proposal kết quả, kiểm tra và lý do không gửi. Giữ API semantic-only cho các caller cũ.

## Đánh giá và giới hạn

Bộ 12 hội thoại frozen hiện vẫn là dữ liệu tổng hợp cho Nam; runner cũ/mới cùng dùng FakeSender, ghi attempts, latency, token/cost và trạng thái qua cổng gửi. Không sửa `datasets/` hoặc run cũ chỉ để khớp báo cáo. Chưa có transcript thật và nhãn chấm mù của con người thì chưa thể kết luận chất lượng “như người thật”.

Generated story là **chi tiết tổng hợp do Luna đề xuất**, không phải ký ức đã được xác minh. Kiểm tra chỉ bảo đảm cấu trúc và nhất quán nội bộ với fact/bài được cấp; không bảo đảm sự kiện là thật. Điều này đúng cả khi persona được lấy từ hồ sơ thật. Giao nhận hiện chỉ dùng `FakeSender`.
