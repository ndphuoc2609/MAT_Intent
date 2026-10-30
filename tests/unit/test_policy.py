"""Consistency test: config/policy.yaml phải mirror đúng app/policy.py (R15).
Code là nguồn duy nhất engine đọc; YAML chỉ là bản đọc cho người. Test này
bắt mọi lệch nhau (gate order, auto allowlist, TTL/SLA) thay vì để hai nơi
drift im lặng.
"""
import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app import policy

def _load_yaml(path):
    # Parser YAML tối giản cho file policy (không cần PyYAML): đọc list/dict
    # nông với key: value và list "- item".
    out, cur = {}, None
    for raw in open(path):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - "):
            out.setdefault(cur, []).append(line[4:].strip())
            continue
        if ":" in line and not line.startswith(" "):
            key, val = line.split(":", 1)
            cur = key.strip()
            val = val.split("#")[0].strip()
            if val:
                try: val = int(val)
                except ValueError: pass
                out[cur] = val
            else:
                out.setdefault(cur, [])
    return out

class TestPolicyConsistency(unittest.TestCase):
    def test_persona_answer_is_an_engine_action(self):
        self.assertIn("answer_persona", policy.AUTO_ACTIONS)

    def test_yaml_mirrors_code(self):
        y = _load_yaml(os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "config", "policy.yaml"))
        self.assertEqual(list(y["gate_order"]), list(policy.GATE_ORDER))
        self.assertEqual(set(y["auto_allowed_actions"]), set(policy.AUTO_ACTIONS))
        self.assertEqual(y["pending_ttl_hours"], policy.PENDING_TTL_HOURS)
        self.assertEqual(y["handoff_sla_hours"], policy.HANDOFF_SLA_HOURS)
    def test_pg_adapter_has_a_versioned_shared_store(self):
        from app import pg_adapter
        from app.state import Store
        self.assertTrue(issubclass(pg_adapter.PostgresStore, Store))
        self.assertEqual(pg_adapter.LATEST_VERSION, 11)

if __name__ == "__main__":
    unittest.main()
