# Taxonomy & boundaries — semantic v1 (mới, không bắt buộc giữ taxonomy cũ)

## Request topics (12 nhu cầu §1 -> topic)
price, promo, loan, stock, spec, dealer, testdrive, buy, service, complaint,
registration, social. `spam` và `out_of_scope` KHÔNG phải request topic:
chúng là scope_assessment, để tránh biến spam thành một "nhu cầu" được xử lý.

## Operations allowlist (model chỉ được ĐỀ XUẤT, engine mới commit)
START_REQUEST, UPDATE_SLOT, ANSWER_PENDING, CONFIRM_PENDING, REJECT_PENDING,
CANCEL_REQUEST, SET_CONTACT_PREFERENCE, REQUEST_HUMAN, REPORT_COMPLAINT,
SOCIAL_ACK, UNRESOLVED.
Model KHÔNG được sinh code/SQL/URL/tool name. Bool không phải probability.
Sarcasm chỉ nhận present/absent/uncertain.

## Ranh giới bắt buộc
- PostCard là bối cảnh, không phải bằng chứng khách muốn mua / đã chọn xe.
  Post nhiều xe + "xe này" chưa rõ tham chiếu -> unresolved, hỏi lại.
- "Đừng gọi" chỉ chặn call, không hủy nhu cầu báo giá. "Không liên hệ nữa" chặn all.
- "Đừng hủy lịch, chỉ đổi sang chiều" = UPDATE_SLOT, không phải CANCEL_REQUEST.
- "Cho số tư vấn bảo hành" = service/dealer contact, không gán buy.
- Người bình luận quảng cáo vay = spam, không phải khách hỏi trả góp.
- "Xe đẹp nhưng chưa có nhu cầu" = khen + no-buy-intent, không mở flow chốt sale.
- "oke/ừ/đc/👍" không có pending = SOCIAL_ACK; có pending yes/no thì chỉ xác
  nhận đúng proposition đó, đúng người, đúng state_version, chưa hết hạn.
- Khiếu nại / tranh chấp cọc / yêu cầu gặp người -> HANDOFF, không gửi tự do.
- Sarcasm present/uncertain, hoặc 2 cách hiểu dẫn tới action khác nhau -> REVIEW.
- Không fact / fact hết hạn / xung đột -> ASK/HANDOFF/REVIEW, không bịa giá.
- Không tuyên bố exactly-once với transport ngoài; sender ở đây là FakeSender
  trong cùng process, dedupe bằng outbox key + ledger.

## Nguồn gốc tái sử dụng
- Bài học từ sandbox cũ intent_v53 (goals/.../files/sandbox/intent_v53):
  gate replay, expired-fact bug, slot normalisation. KHÔNG copy code/taxonomy
  cũ: schema, gate và engine ở đây viết mới theo prompt này.
- Luna calls OpenRouter directly with `OPENROUTER_API_KEY`; model `openai/gpt-6-luna`.
