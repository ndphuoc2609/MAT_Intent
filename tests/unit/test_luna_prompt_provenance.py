import unittest

from app.adapters.luna import SYSTEM, SYSTEM_V2


class LunaPromptProvenanceTests(unittest.TestCase):
    def test_both_versions_leave_inherited_slot_provenance_to_server(self):
        for prompt in (SYSTEM, SYSTEM_V2):
            with self.subTest(version="v2" if prompt is SYSTEM_V2 else "v1"):
                self.assertIn("server gắn", prompt)
                self.assertNotIn("source_state_token đúng từ context", prompt)
                self.assertNotIn("source_actor_id và source_state_token đúng từ context", prompt)

    def test_single_vehicle_post_inference_uses_post_evidence_and_server_provenance(self):
        for prompt in (SYSTEM, SYSTEM_V2):
            with self.subTest(version="v2" if prompt is SYSTEM_V2 else "v1"):
                self.assertIn("evidence_excerpt của slot phải trích đúng tên xe từ post", prompt)
                self.assertIn("explicit=false", prompt)
                self.assertIn("server gắn nguồn đã xác minh", prompt)
                self.assertNotIn("trỏ source_message_id tới post", prompt)
                self.assertNotIn("source_message_id tới post", prompt)


if __name__ == "__main__":
    unittest.main()
