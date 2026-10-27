# Manifest

- contract scenarios: 199
- families (23): price, promo, loan, stock, spec, dealer, service, registration, multi, testdrive, buy, complaint, social, negation, contact, sarcasm, scope, multiuser, ack, conditional, parser, facts, takeover
- holdout families (index%4==3): complaint, conditional, registration, sarcasm, stock
- api dev/holdout: 109/16
- label_source: synthetic_contract (KHÔNG phải human gold)
- human_gold: TRỐNG (xem datasets/human_gold/README.md)

## Session 2 — version notes (giữ trong template này để khỏi bị ghi đè)
- err_expired_pending: expected_final WAITING_USER -> OPEN (Loop 5: sweep hết hạn pending là hành vi đúng).
- service_warranty_mixed (Loop 5): tách 2 nhu cầu — chính sách (trả lời 5 năm) + xin số tư vấn (không có fact số -> handoff draft). Scenario contract cũ cùng text chỉ model nhu cầu chính sách: thiếu sót phủ nhãn, giữ nguyên.
- api_time_next_tuesday — XUNG ĐỘT NHÃN: api kỳ vọng ASK, contract Loop 5 kỳ vọng đề xuất 27/10 lúc 10:00 (giờ mặc định = đề xuất). Hành vi engine theo contract; báo riêng mỗi lần chấm, chưa đổi expected.
- api_dup_start_same_turn / api_dup_start_two_vehicles — scorer artifact: topic_exact so multiset ['spec','spec'] với pred set ['spec']; engine trả đúng cả hai nhu cầu.
- scorer v2: 0.972 session 1 là post-hoc; v2 chỉ chấp nhận ASK khi engine hỏi đúng slot bắt buộc mà semantic không có.
- Multi-turn: dev 10 / holdout 6 hội thoại, tách TRƯỚC khi chạy, nhãn synthetic (không phải human gold). Holdout đã chạy 1 lần (mt_holdout_v2, raw 17/20, adjudicated 19/20) và KHÔNG bị chuyển thành development vì không có thay đổi nào áp từ lỗi của nó; draft Loop 9 ở reports/final_report_v2.md §8, chưa áp.
