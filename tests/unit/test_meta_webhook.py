import hashlib
import hmac
import unittest

from meta_services.webhook import extract_comment_events, verify_signature


class MetaWebhookTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "object": "page",
            "entry": [{
                "id": "123",
                "time": 1720000000,
                "changes": [{
                    "field": "feed",
                    "value": {
                        "item": "comment",
                        "verb": "add",
                        "comment_id": "456_789",
                        "post_id": "123_456",
                        "parent_id": "123_456",
                        "from": {"id": "999", "name": "Customer"},
                        "message": "Creta còn màu trắng không?",
                    },
                }],
            }],
        }

    def test_signature_uses_raw_body_and_sha256_header(self):
        body = b'{"sample":true}'
        digest = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature(body, f"sha256={digest}", "app-secret"))
        self.assertFalse(verify_signature(body + b" ", f"sha256={digest}", "app-secret"))
        self.assertFalse(verify_signature(body, digest, "app-secret"))
        self.assertFalse(verify_signature(body, f"sha256={digest}", ""))

    def test_extracts_only_new_customer_comments_for_configured_page(self):
        events = extract_comment_events(self.payload, "123")
        self.assertEqual(events, [{
            "page_id": "123",
            "actor_id": "999",
            "post_id": "123_456",
            "comment_id": "456_789",
            "parent_id": "123_456",
            "thread_root_id": "123_456",
            "text": "Creta còn màu trắng không?",
            "event_type": "create",
            "content_version": 1,
        }])

    def test_ignores_other_pages_page_self_edits_and_non_comment_changes(self):
        other_page = {**self.payload, "entry": [{**self.payload["entry"][0], "id": "other"}]}
        page_self = {**self.payload, "entry": [{**self.payload["entry"][0], "changes": [{
            "field": "feed", "value": {**self.payload["entry"][0]["changes"][0]["value"],
                                           "from": {"id": "123"}}
        }]}]}
        edited = {**self.payload, "entry": [{**self.payload["entry"][0], "changes": [{
            "field": "feed", "value": {**self.payload["entry"][0]["changes"][0]["value"],
                                           "verb": "edited"}
        }]}]}
        reaction = {**self.payload, "entry": [{**self.payload["entry"][0], "changes": [{
            "field": "feed", "value": {"item": "reaction", "verb": "add"}
        }]}]}
        for payload in (other_page, page_self, edited, reaction):
            with self.subTest(payload=payload):
                self.assertEqual(extract_comment_events(payload, "123"), [])

    def test_rejects_malformed_or_oversized_comment_fields(self):
        self.assertEqual(extract_comment_events([], "123"), [])
        huge = {**self.payload, "entry": [{**self.payload["entry"][0], "changes": [{
            "field": "feed", "value": {**self.payload["entry"][0]["changes"][0]["value"],
                                           "message": "x" * 70000}
        }]}]}
        self.assertEqual(extract_comment_events(huge, "123"), [])


if __name__ == "__main__":
    unittest.main()
