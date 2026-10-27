# Human gold

Thư mục này hiện chưa có nhãn tiếng Việt do người gán. Không đưa contract,
AI-reviewed hoặc synthetic labels vào đây và không báo chúng là human gold.

## Gán nhãn

Tạo `gold.jsonl`, mỗi dòng là một comment đã được người gán nhãn:

```json
{"id":"opaque-001","thread_id":"thread-opaque","actor_id":"actor-opaque","timestamp":"2026-10-27T09:00:00+07:00","label_source":"human","labels":["price","stock"],"slots":{"vehicle":"Creta","color":"Trắng"},"action":"REVIEW","risk_group":"normal","auto_processed":false}
```

`labels` là multi-label từ taxonomy trong `app/semantics.py`; `slots` chỉ
chứa giá trị được người gán xác nhận; `action` là quyết định policy mong đợi.
Gán `risk_group` là `high` cho các ca nhạy cảm (opt-out, hủy, khiếu nại,
handoff, pending/A-B/đồng ý); `auto_processed` mô tả kết quả engine khi chấm,
không phải mong đợi của nhãn.

Tách theo thời gian, đồng thời gom các dòng có cùng actor hoặc thread vào cùng
fold để tránh leakage:

```sh
python3 scripts/evaluate_human_gold.py split --gold datasets/human_gold/gold.jsonl --out-dir datasets/human_gold/splits
python3 scripts/evaluate_human_gold.py score --gold datasets/human_gold/splits/test.jsonl --predictions runs/predictions.jsonl
```

Scorer từ chối mọi dòng không có `label_source: human`. Báo cáo gồm intent
macro-F1/recall, slot precision/recall/accuracy, action correctness, critical
errors, coverage, pipeline latency p50/p95 và cost/request; tách riêng high-risk
và auto-processed. `reports/human_labeling_pack.jsonl` là hàng chờ gán nhãn
chưa được xác nhận, không phải gold.
