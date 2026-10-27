"""Mutation suite v2 (R11): mỗi mutation phải bị phát hiện bởi suite hiện có.
Khác v1 ở:
- Check baseline trước: check nào đã FAIL trên code sạch thì mutant đó là
  BASELINE_FAIL (không tính vào mẫu số, suite FAIL rõ lý do) thay vì bị
  tính là DETECTED/SURVIVED một cách vô nghĩa.
- Mutant phải compile + import được: lỗi cú pháp/import/binding (VD mutant
  SQL cũ xóa placeholder mà không xóa param -> sqlite ProgrammingError) là
  INVALID, không phải KILLED. Chỉ exit != 0 do test/check fail thật mới là
  KILLED.
- Leak scan báo cáo riêng, không nằm trong mẫu số mutant.
- Xóa __pycache__ cả trước check lẫn sau restore (pyc header chỉ lưu mtime
  giây + size nên mutant cùng size trong cùng 1 giây có thể bị tin nhầm).
Chạy: python3 tests/mutations/mutation_suite.py
"""
from __future__ import annotations
import hashlib, os, shutil, subprocess, sys, py_compile
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def h(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()
def run(cmd, cwd=ROOT):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr

# (tên, file, [(old, new), ...], check_cmd) — check phải FAIL khi mutation còn.
U = [sys.executable, "-m", "unittest"]
MUTATIONS = [
    ("drop_handoff_override_complaint", "app/engine.py",
     [('if req["status"] == "HANDED_OFF":\n            return (["Yêu cầu này đã được chuyển cho nhân viên xử lý."], nums, "HANDOFF")',
       'if False and req["status"] == "HANDED_OFF":\n            return (["Yêu cầu này đã được chuyển cho nhân viên xử lý."], nums, "HANDOFF")'),
      ('return (["Phần khiếu nại đã được chuyển cho nhân viên xử lý."], nums, "HANDOFF")',
       'return (["Cảm ơn bạn!"], nums, "social_ack")')],
     [sys.executable, "scripts/run_contract.py", "--split", "dev", "--only", "complaint_pure"]),
    # Owner isolation có 2 lớp (_resolve_ref + _apply_ops): mutant 1 lớp bị
    # lớp còn lại chặn nên SURVIVED một cách đúng đắn — mutant phải drop cả
    # 2 lớp mới đo được tính chất hệ thống (uB không được sửa request của uA).
    ("drop_owner_check", "app/engine.py",
     [('if r["actor_id"] != actor: return None, "owner", None',
       'if False and r["actor_id"] != actor: return None, "owner", None'),
      ('if req["actor_id"] != actor:',
       'if False and req["actor_id"] != actor:')],
     [sys.executable, "scripts/run_contract.py", "--split", "dev", "--only", "err_owner_mismatch"]),
    # Mutant SQL HỢP LỆ (v1 xóa placeholder mà giữ param -> binding error,
    # bị tính DETECTED oan): xóa cả placeholder lẫn param version.
    ("drop_state_version_check", "app/state.py",
     [("UPDATE requests SET status=?, version=?, slots=?, source_events=?, desired_outcomes=?, updated_at=? WHERE id=? AND version=?",
       "UPDATE requests SET status=?, version=?, slots=?, source_events=?, desired_outcomes=?, updated_at=? WHERE id=?"),
      ('req.get("updated_at", ""), req["id"], req["version"] - 1))',
       'req.get("updated_at", ""), req["id"]))')],
     U + ["tests.unit.test_core.TestEngine.test_version_conflict_detected"]),
    ("allow_expired_fact", "app/facts.py",
     [("return vf <= now <= vt", "return True")],
     [sys.executable, "scripts/run_contract.py", "--split", "dev", "--only", "promo_expired"]),
    # Mutant HỢP LỆ (v1 chèn code sau `if ...: continue` một dòng -> SyntaxError):
    # nhắm đúng nhánh SOCIAL_ACK trong _apply_ops.
    ("ack_is_confirm_all", "app/engine.py",
     [('            if t == "SOCIAL_ACK":\n                result["routing"].append("SOCIAL_ACK")\n                continue',
       '            if t == "SOCIAL_ACK":\n                result["routing"].append("SOCIAL_ACK")\n                for _p in self.store.open_pending_for(actor):\n                    _p["status"] = "CONFIRMED"; _p["version"] += 1; self.store.update_pending(_p)\n                continue')],
     [sys.executable, "scripts/run_contract.py", "--split", "dev", "--only", "ack_not_confirm"]),
    ("blind_retry_unknown_outbox", "app/engine.py",
     [('if row and row[0] == "SENT": return  # không ghi đè row đã SENT',
       'if False and row and row[0] == "SENT": return  # không ghi đè row đã SENT'),
      ('if row and row[0] == "SENT": continue',
       'if False and row and row[0] == "SENT": continue')],
     U + ["tests.unit.test_core.TestEngine.test_outbox_dedupe_no_blind_retry"]),
    ("any_safe_instead_of_all_safe", "app/engine.py",
     [('if ho and not safe:', 'if False and ho and not safe:')],
     [sys.executable, "scripts/run_contract.py", "--split", "dev", "--only", "complaint_pure"]),
    ("drop_vehicle_claim_check", "app/renderer.py",
     [('if norm(v) in nt and v not in allowed_vehicles:',
       'if False and norm(v) in nt and v not in allowed_vehicles:')],
     U + ["tests.unit.test_loop5.TestClaimProvenance.test_tucson_number_in_creta_reply"]),
    # Test cũ đổi cả valid_to nên freshness recheck vẫn chặn dù version check
    # bị drop (defense in depth tốt, nhưng không giết được mutant này) -> check
    # mới chỉ bump version, fact vẫn fresh.
    ("drop_dispatch_facts_recheck", "app/engine.py",
     [('if self.facts.version != plan_facts_version:',
       'if False and self.facts.version != plan_facts_version:')],
     U + ["tests.unit.test_review_fixes.TestReviewFactsVersionRecheck.test_dispatch_blocked_on_facts_version_bump_only"]),
    ("drop_identity_match", "app/engine.py",
     [('if veh is not None and tv is not None and veh != tv: return False',
       'if False and veh is not None and tv is not None and veh != tv: return False')],
     U + ["tests.unit.test_loop5.TestIdentityAndDispatch.test_price_two_vehicles_kept_separate"]),
    # Mutant cho code mới (R01/R04/R07/R10/R03).
    ("drop_pending_link_check", "app/engine.py",
     [('if pend["request_id"] != req["id"]: return "REVIEW_BAD_PENDING"',
       'if False and pend["request_id"] != req["id"]: return "REVIEW_BAD_PENDING"')],
     U + ["tests.unit.test_review_fixes.TestReviewPendingLink.test_pending_of_other_request_rejected"]),
    ("allow_confirm_slot_question", "app/engine.py",
     [('if pend["qtype"] != "yes_no": return "REVIEW_BAD_PENDING"',
       'if False and pend["qtype"] != "yes_no": return "REVIEW_BAD_PENDING"')],
     U + ["tests.unit.test_review_fixes.TestReviewPendingLink.test_confirm_slot_question_rejected"]),
    ("drop_handoff_scope_start", "app/engine.py",
     [('if topic and self.store.handoff_for_scope(page, actor, topic, veh, var,',
       'if False and topic and self.store.handoff_for_scope(page, actor, topic, veh, var,'),
      ('if topic and self.store.handoff_for_scope(page, actor, topic, _veh, _var,',
       'if False and topic and self.store.handoff_for_scope(page, actor, topic, _veh, _var,')],
     U + ["tests.unit.test_review_fixes.TestReviewHandoffScope.test_same_vehicle_blocked_other_vehicle_allowed"]),
    ("drop_stock_zero_check", "app/engine.py",
     [('if all((f["value"] or 0) <= 0 for f in fl):',
       'if False and all((f["value"] or 0) <= 0 for f in fl):')],
     U + ["tests.unit.test_review_fixes.TestReviewFacts.test_stock_zero_not_available"]),
    ("drop_fee_conflict_check", "app/engine.py",
     [('if fconf or pconf or dconf:', 'if False and (fconf or pconf or dconf):')],
     U + ["tests.unit.test_review_fixes.TestReviewFacts.test_otr_fee_conflict_handoff"]),
    ("drop_typed_claim_check", "app/renderer.py",
     [('    claims = [c for c in (claims or [])',
       '    return []\n    claims = [c for c in (claims or [])')],
     U + ["tests.unit.test_review_fixes.TestReviewTypedClaim.test_swapped_prices_rejected"]),
    ("drop_receiver_dedupe", "app/state.py",
     [('        if not is_new:', '        if False and not is_new:')],
     U + ["tests.unit.test_review_fixes.TestReviewReceiverDedupe.test_redelivery_same_key_one_effect"]),
]

INVALID_MARKERS = ("ProgrammingError", "OperationalError", "SyntaxError",
                   "ImportError", "ModuleNotFoundError", "IndentationError")

def _clear_pycache():
    for base, dirs, files in os.walk(ROOT):
        if "__pycache__" in base:
            shutil.rmtree(base, ignore_errors=True)

def _classify(code, out):
    if code == 0:
        return "SURVIVED"
    # unittest failure cũng in traceback của assertion — chỉ coi là INVALID
    # khi marker là lỗi hệ thống (DB/import/cú pháp), không phải AssertionError.
    if "AssertionError" in out:
        return "KILLED"
    for m in INVALID_MARKERS:
        if m in out:
            return "INVALID"
    return "KILLED"

def main():
    results = []
    for name, rel, pairs, cmd in MUTATIONS:
        path = os.path.join(ROOT, rel)
        orig, orig_hash = open(path).read(), h(path)
        mutated = orig
        for old, new in pairs:
            assert old in mutated, f"mutation anchor not found: {name} in {rel}: {old[:60]}"
            mutated = mutated.replace(old, new, 1)
        # 1. Baseline: check phải PASS trên code sạch, nếu không mutant này
        # không đánh giá được.
        _clear_pycache()
        bcode, bout = run(cmd)
        if bcode != 0:
            print(f"BASELINE_FAIL {name} (check exit={bcode} on clean tree)")
            results.append((name, "BASELINE_FAIL", bcode)); continue
        try:
            open(path, "w").write(mutated)
            _clear_pycache()
            # 2. Compile: mutant hỏng cú pháp là INVALID.
            try:
                py_compile.compile(path, doraise=True)
            except Exception as e:
                print(f"INVALID {name} (compile: {type(e).__name__})")
                results.append((name, "INVALID", -1)); continue
            code, out = run(cmd)
            status = _classify(code, out)
            print(f"{status} {name} (check exit={code})")
            results.append((name, status, code))
        finally:
            open(path, "w").write(orig)
            assert h(path) == orig_hash, f"restore failed for {rel}"
            _clear_pycache()
    # Leakage scan: báo cáo RIÊNG, không nằm trong mẫu số mutant.
    code, out = run(["grep", "-r", "expected_final", "app/"])
    leak_ok = code != 0
    print(f"{'DETECTED (no leak)' if leak_ok else 'LEAK FOUND'} label_leakage_scan (separate)")
    killed = sum(1 for _, s, _ in results if s == "KILLED")
    denom = sum(1 for _, s, _ in results if s in ("KILLED", "SURVIVED"))
    bad = [(n, s) for n, s, _ in results if s != "KILLED"]
    print(f"mutation suite: {'PASS' if not bad else 'FAIL'} "
          f"(killed {killed}/{denom} valid; non-killed: {bad})")
    return 0 if not bad and leak_ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
