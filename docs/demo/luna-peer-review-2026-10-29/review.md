# Review live — Nam Creta revision 5, 2026-10-29

Kết luận: ba lượt lỗi gốc đã sửa được qua Luna thật. Bản này chưa đạt mục tiêu trò chuyện ngang hàng tự nhiên: vẫn có lỗi đính chính/state và lời đáp khuôn mẫu. Đề nghị giữ ở demo review, chưa đánh dấu nghiệm thu hội thoại.

## Phạm vi và bằng chứng

- Build và chạy container `persona-review-sandbox-1` tại http://127.0.0.1:8771/; release fixture revision 5.
- 8 hội thoại, 35 lượt, 36 Luna attempts (một lượt phải repair). Known Luna cost: $0.013476025; không có lượt Luna cost unknown. Số này không gồm Jev shadow; usage chung vẫn đi qua ledger hiện có, không reset.
- 418 unit test pass khi cho phép HTTP loopback; 62 integration test pass. Lần unit chạy trong sandbox lỗi một test do quyền bind socket, lần chạy đủ quyền đã pass.
- Không thay source runtime trong đợt review; SHA-256 trước/sau giống nhau, lưu ở [manifest.json](manifest.json).
- [Transcript đọc được](transcript.md), [raw JSONL](transcripts.jsonl). Mỗi case dùng hội thoại mới; case two_actors dùng chung thread và thay actor_id từng lượt.
- Lead/pending/response được quan sát trực tiếp qua API. Peer state nội bộ chưa được endpoint trả ra, nên không khẳng định đã kiểm tra trực tiếp mọi cột peer state trong live.

## Findings cần gửi Claude

### 1. Cao — đính chính người mua bị review, lead và pending cũ vẫn giữ

Case `correction`, lượt 1: “Mình cũng tính mua Creta” tạo interested_vehicle=Creta và pending purchase_timeline.

Lượt 2: “Không phải tôi mua, tôi hỏi hộ anh tôi”. Luna trả scope=in_scope, operations=[UNRESOLVED], reason: lời đính chính không trả lời câu hỏi thời điểm mua. Engine trả REVIEW_UNRESOLVED, không gửi lời sửa hiểu nhầm. Pending purchase_timeline vẫn OPEN và lead Creta vẫn ANSWERED như thông tin của người nói. Đến lượt 4 vẫn còn pending này.

Nguyên nhân: đường review operation thoát trước `_peer_plan`/`_apply_peer_plan`. Test đính chính hiện đưa SOCIAL_ACK giả nên không phủ tình huống Luna trả UNRESOLVED.

Đề nghị: thêm test integration bằng chính semantic trong raw trace. Thiết kế thứ tự xử lý correction có evidence sau schema/risk validation để được cập nhật chủ thể/state và supersede pending đúng cách; không mở bypass cho schema, sarcasm, provenance hoặc opt-out. Giữ lịch sử evidence gốc nhưng đánh dấu thông tin đã bị đính chính để không xuất lead sai người. Đây là phần model dialogue contract trong kế hoạch còn thiếu.

### 2. Vừa — từ chối đã dừng hỏi nhưng hồ sơ vẫn ASKED

Case `decline`, lượt 2: “không cần đâu, đừng hỏi nữa”. Luna sinh CANCEL_REQUEST với request_ref=topic:social. Engine đóng request/pending trước peer planner. Nam nói “Được bạn, vậy mình dừng ở đây nhé”, pending hết nhưng lead interested_vehicle vẫn ASKED, evidence=null, source vẫn là lượt hỏi trước. Không thành DECLINED và không ghi evidence từ chối như test SOCIAL_ACK.

Đề nghị: test với CANCEL_REQUEST thật; áp dụng quyết định từ chối nhất quán trong cùng transaction, giữ evidence câu từ chối và status DECLINED. Không chỉ sửa lời đáp.

### 3. Vừa — “Chào bác, tôi có xe rồi” mất cách gọi bác

Case `owner`, lượt 1 được đáp “À, bạn có xe rồi ạ.” `address_explicit('Chào bác, tôi có xe rồi')` tái hiện trả None tại source hiện tại. Cụm “tôi” sau dấu phẩy bị coi như dấu hiệu người thứ ba, nên bác bị bỏ qua. Các lượt sau nói “Bác đang đi xe gì” mới phục hồi cách gọi.

Đề nghị: phân biệt ranh giới dấu câu/vế câu trước khi loại đại từ sở hữu, thêm control “anh trai tôi có xe” vẫn không được nhận là xưng hô.

### 4. Vừa — “hỏi hộ thôi” bị hiểu thành kết thúc hội thoại

Case `third_party`: lượt 1 “Anh tôi tính mua Creta” không mở lead nhưng im lặng với REVIEW_PERSONA_NO_PEER_UTTERANCE. Lượt 2 “Tôi chỉ hỏi hộ thôi” được đáp “Được bạn, vậy mình dừng ở đây nhé.” Từ “thôi” mang nghĩa giới hạn “chỉ ... thôi”, không phải yêu cầu ngừng nói chuyện.

Nguyên nhân: `_DECLINE` match “thoi” ở cuối câu mà không xét “chỉ ... thôi”.

Đề nghị: test cặp “Tôi chỉ hỏi hộ thôi” / “Thôi nhé, đừng hỏi nữa”; không chuyển hỏi hộ thành lead của người nói, nhưng cần đáp hoặc hỏi rõ phù hợp thay vì đóng hội thoại.

### 5. Chất lượng — câu ack mới vẫn có tính máy móc và lặp

Case `buying`: “Creta” -> “Vậy hả bác, mình nghe rồi”; “tháng sau” -> “Ừ bác, mình hiểu rồi”; “trả góp” -> “Vậy hả bác, mình nghe rồi”. Case switch cũng dùng các câu tương tự. Những câu này không phản hồi nội dung cụ thể vừa nghe. Tránh lặp ở hai lượt liền nhau không giải quyết việc lặp cách lượt và thiếu phản ứng theo ý nghĩa.

Đề nghị: Claude đánh giá lại giới hạn thư viện utterance. Chọn response intent theo nội dung/uncertainty trước; nếu cần tham chiếu dữ kiện khách vừa nói, thiết kế template đã duyệt với biến được ràng buộc evidence. Không coi việc thay “Mình hiểu ý” bằng “Vậy hả” là đã giải quyết chất lượng. Không nới sang model viết tự do mà chưa thiết kế/duyệt phạm vi.

## Kết quả theo hội thoại

| Case | Quan sát |
|---|---|
| original | Đáp thời gian dùng xe; hỏi rõ đã có hay định mua; nhận đã có xe, giữ bác, không lead/pending. Ba lỗi gốc đã đi đúng. |
| owner | Không ép lead; câu chào dài mất bác; câu màu xe vẫn dừng ở “không rõ” do fixture chưa có fact. |
| buying | Ghi Creta, tháng sau, trả góp đúng evidence; dừng sau ba câu hỏi. Lời ack khuôn mẫu. |
| switch | Đã có xe vẫn được mở exploring khi muốn đổi; ghi Tucson, cuối năm nay; giữ nguyên lời “chưa quyết định trả góp hay trả thẳng” trong giá trị finance, không gán chắc là trả góp. Lượt cuối repair một lần. |
| third_party | Không tạo lead sai cho người nói; im lặng ở lượt đầu, hiểu nhầm “chỉ hỏi hộ thôi” là dừng. |
| decline | Ngừng hỏi, pending đóng; status/evidence từ chối chưa cập nhật đúng. |
| correction | Không xử lý được đính chính trong semantic UNRESOLVED; giữ pending và lead cũ. |
| two_actors | Quan sát lead/pending của b không xuất hiện ở a; a giữ bác khi nói có xe, b tiếp tục Creta/tháng sau với cách gọi bạn. Lời ack vẫn khuôn mẫu. |

## Thứ tự sửa đề nghị

1. Thêm regression từ semantic live cho correction/decline, chốt lại thứ tự planner với engine gates.
2. Sửa cách gọi có dấu phẩy và nghĩa “chỉ ... thôi”.
3. Hoàn thiện model dialogue contract và response intent; giữ validation/approval hiện có.
4. Chạy lại những case lỗi với cùng input rồi một batch diễn đạt mới. Không sửa fixture/test để biến expected thành hành vi đang sai.

Chưa kiểm thử PostgreSQL thật hoặc UI bằng browser trong đợt này. Đây là 8 hội thoại tổng hợp có chủ đích, không phải bằng chứng chất lượng trên người dùng thật.
