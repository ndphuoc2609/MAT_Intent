import unittest

from app.harness import make_engine
from app.persona_reply import utterance_checksum


class _PersonaStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_binding_snapshot(self, page_id, post_id):
        return self.snapshot

    def revoked_fact_ids(self, persona_uuid, release_id):
        return []


def _snapshot():
    utterance = {
        "utterance_id": "u-residence", "persona_uuid": "p1", "release_id": "r1",
        "text": "Mình đang sống ở Đà Nẵng.", "purpose": "answer_persona",
        "topic": "residence", "required_fact_ids": ["f1"],
        "claims": [{"text": "sống ở Đà Nẵng", "required_fact_ids": ["f1"],
                    "evidence": [{"fact_id": "f1", "source_ref": "fixture:nam_creta:residence"}]}],
        "approval": "approved",
    }
    utterance["checksum"] = utterance_checksum(utterance)
    return {
        "persona_uuid": "p1", "release_id": "r1", "approved": True,
        "facts": [{"fact_id": "f1", "persona_uuid": "p1", "release_id": "r1",
                   "value": "Đà Nẵng", "tellable": True, "approval": "approved",
                   "source_ref": "fixture:nam_creta:residence"}],
        "utterances": [utterance],
    }


class PersonaEngineTests(unittest.TestCase):
    def test_social_persona_question_uses_only_bound_approved_utterance(self):
        engine = make_engine()
        engine.persona_store = _PersonaStore(_snapshot())
        text = "Bạn ở đâu vậy?"
        semantic = {
            "schema_version": "1", "sentiment": "NEU", "sarcasm": "absent",
            "scope": "in_scope",
            "requests": [{"topic": "social", "desired_outcome": "ack", "polarity": "positive"}],
            "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": text}],
            "uncertainties": [],
        }
        event = {"page_id": "page-1", "post_id": "post-1", "thread_root_id": "root-1",
                 "parent_id": "root-1", "comment_id": "comment-1", "actor_id": "actor-1",
                 "actor_role": "user", "event_type": "create", "content_version": 1,
                 "text": text, "event_dedupe_key": "comment-1:1"}
        result = engine.process(event, {"text": "Bài viết", "persona_binding": {"release_id": "r1"}}, semantic)
        self.assertEqual(result["response_text"], "Mình đang sống ở Đà Nẵng.")
        self.assertIn("answer_persona", result["routing"])
        self.assertTrue(result["outbound_permission"])
        self.assertEqual(result["response_plan"]["persona_query"]["evidence_message_id"], "comment-1")
        self.assertEqual(result["response_plan"]["persona_query"]["evidence_actor_id"], "actor-1")


if __name__ == "__main__":
    unittest.main()
