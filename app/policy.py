"""Policy — NGUỒN DUY NHẤT engine đọc trực tiếp (code, không phải YAML).

`config/policy.yaml` chỉ là bản mirror cho người đọc; test
`tests/unit/test_policy.py` bắt buộc hai nơi khớp nhau (giá trị + auto allowlist
+ gate order). Không sửa YAML rồi mong runtime đổi theo, và ngược lại: mọi thay
đổi policy phải sửa ở đây trước, rồi đồng bộ YAML + chạy test consistency.

PostgreSQL adapter: NOT_IMPLEMENTED (xem app/pg_adapter.py) — không phải chỉ
thiếu môi trường kiểm thử.
"""
from __future__ import annotations

# Các action được phép gửi tự động (không cần người duyệt).
AUTO_ACTIONS = frozenset({
    "answer_price", "answer_promo", "answer_loan_info", "answer_stock",
    "answer_spec", "answer_dealer", "ask_clarification", "social_ack",
    "propose_test_drive_slot", "answer_confirmation",
})

# SLA handoff: quá số giờ này handoff chuyển OVERDUE (escalation nội bộ),
# KHÔNG tự release cho bot tiếp quản (xem Engine._sweep_expired).
HANDOFF_SLA_HOURS = 48

# TTL của một pending (câu hỏi đang chờ khách trả lời).
PENDING_TTL_HOURS = 24

# Thứ tự gate trong Engine.process (tài liệu hoá, thứ tự thực thi trong code
# phải khớp: ingest -> sweep -> pref pre-phase -> validation -> risk gates ->
# business savepoint -> plan -> claim -> dispatch recheck -> dispatcher).
GATE_ORDER = (
    "kill_switch_and_environment",
    "opt_out_per_channel",
    "handoff_lock",
    "dedupe_and_state_version",
    "schema_evidence_provenance",
    "complaint_legal_human_route",
    "sarcasm_or_ambiguous_action",
    "slots_facts_no_conflict",
    "response_validated_and_auto_allowlist",
    "recheck_before_dispatch",
)

# Các kênh liên hệ có thể bị chặn (block set, không ghi đè lẫn nhau).
# "all" = chặn mọi kênh. Các scope mở lại: allow_call / allow_public_reply /
# allow_inbox / allow_all (xóa block tương ứng).
BLOCKABLE_CHANNELS = frozenset({"call", "public_reply", "inbox", "all"})
UNBLOCK_SCOPES = frozenset({"allow_call", "allow_public_reply", "allow_inbox", "allow_all"})
