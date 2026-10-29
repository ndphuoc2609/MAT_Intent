import sqlite3
import unittest

from app.persona_reply import render_persona_reply, utterance_checksum
from app.persona_store import PersonaStore, migrate_sqlite


def release_fixture():
    utterance = {
        "utterance_id": "u1", "persona_uuid": "p1", "release_id": "r1",
        "text": "Mình đang sống ở Đà Nẵng.", "purpose": "answer_persona",
        "topic": "residence", "required_fact_ids": ["f1"],
        "claims": [{"text": "sống ở Đà Nẵng", "required_fact_ids": ["f1"],
                    "evidence": [{"fact_id": "f1", "source_ref": "draft:row-1"}]}],
        "approval": "approved",
    }
    utterance["checksum"] = utterance_checksum(utterance)
    return {
        "persona_uuid": "p1", "release_id": "r1", "approved": True,
        "facts": [{"fact_id": "f1", "persona_uuid": "p1", "category": "location",
                   "key": "location.base.province", "value": "Đà Nẵng", "unit": None,
                   "tellable": True, "approval": "approved", "source_ref": "draft:row-1"}],
        "utterances": [utterance],
    }


class PersonaReplyTests(unittest.TestCase):
    def test_renders_single_matching_approved_utterance_from_snapshot(self):
        result = render_persona_reply(release_fixture(), topic="residence")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["text"], "Mình đang sống ở Đà Nẵng.")
        self.assertEqual(result["fact_ids"], ["f1"])

    def test_selector_can_only_choose_eligible_utterance_ids(self):
        snapshot = release_fixture()
        result = render_persona_reply(snapshot, topic="residence", plan={
            "schema_version": "persona_reply_plan_v1", "utterance_ids": ["u1"], "hook_id": None,
        })
        self.assertEqual(result["status"], "OK")
        bad = render_persona_reply(snapshot, topic="residence", plan={
            "schema_version": "persona_reply_plan_v1", "utterance_ids": ["u-foreign"],
            "hook_id": None,
        })
        self.assertEqual(bad["status"], "REVIEW")
        self.assertEqual(bad["reason"], "UTTERANCE_NOT_ELIGIBLE")

    def test_selector_cannot_supply_arbitrary_reply_text(self):
        result = render_persona_reply(release_fixture(), topic="residence", plan={
            "schema_version": "persona_reply_plan_v1", "utterance_ids": ["u1"],
            "hook_id": None, "reply_text": "I bought this car.",
        })
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "INVALID_PLAN")

    def test_tampered_text_invalidates_utterance_checksum(self):
        snapshot = release_fixture()
        snapshot["utterances"][0]["text"] = "Mình sống ở Hà Nội."
        result = render_persona_reply(snapshot, topic="residence")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "UTTERANCE_CHECKSUM_MISMATCH")

    def test_revoked_fact_is_not_rendered(self):
        result = render_persona_reply(release_fixture(), topic="residence", revoked_ids={"f1"})
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "FACT_REVOKED")

    def test_fact_from_another_persona_or_release_is_not_rendered(self):
        snapshot = release_fixture()
        snapshot["facts"][0]["persona_uuid"] = "p2"
        result = render_persona_reply(snapshot, topic="residence")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "FACT_SCOPE_MISMATCH")

    def test_claim_without_matching_evidence_is_not_rendered(self):
        snapshot = release_fixture()
        snapshot["utterances"][0]["claims"][0]["evidence"] = []
        snapshot["utterances"][0]["checksum"] = utterance_checksum(snapshot["utterances"][0])
        result = render_persona_reply(snapshot, topic="residence")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "CLAIM_EVIDENCE_MISSING")

    def test_returns_unknown_when_no_matching_utterance_exists(self):
        result = render_persona_reply(release_fixture(), topic="family")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["text"])

    def test_ambiguous_matching_utterances_need_selector(self):
        snapshot = release_fixture()
        second = dict(snapshot["utterances"][0], utterance_id="u2")
        second["checksum"] = utterance_checksum(second)
        snapshot["utterances"].append(second)
        result = render_persona_reply(snapshot, topic="residence")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "SELECTOR_REQUIRED")

    def test_unapproved_matching_utterance_is_review_not_unknown(self):
        snapshot = release_fixture()
        snapshot["utterances"][0]["approval"] = "draft"
        snapshot["utterances"][0]["checksum"] = utterance_checksum(snapshot["utterances"][0])
        result = render_persona_reply(snapshot, topic="residence")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "UTTERANCE_NOT_APPROVED")

    def test_duplicate_utterance_id_is_reviewed_as_ambiguous(self):
        snapshot = release_fixture()
        second = dict(snapshot["utterances"][0])
        snapshot["utterances"].append(second)
        result = render_persona_reply(snapshot, topic="residence")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "DUPLICATE_OR_INVALID_UTTERANCE_ID")

    def test_caps_reply_size_and_question_count(self):
        snapshot = release_fixture()
        snapshot["utterances"][0]["text"] = "Mình đang sống ở Đà Nẵng？ Bạn thì sao?"
        snapshot["utterances"][0]["claims"][0]["text"] = "sống ở Đà Nẵng"
        snapshot["utterances"][0]["checksum"] = utterance_checksum(snapshot["utterances"][0])
        result = render_persona_reply(snapshot, topic="residence")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "REPLY_LIMIT_EXCEEDED")

    def test_unhashable_fact_ids_return_review_without_crashing(self):
        snapshot = release_fixture()
        snapshot["utterances"][0]["required_fact_ids"] = [["f1"]]
        snapshot["utterances"][0]["checksum"] = utterance_checksum(snapshot["utterances"][0])
        result = render_persona_reply(snapshot, topic="residence")
        self.assertEqual(result["status"], "REVIEW")

    def test_malformed_snapshot_collections_return_review_without_crashing(self):
        snapshot = release_fixture()
        snapshot["facts"] = None
        result = render_persona_reply(snapshot, topic="residence")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "RELEASE_INVALID")

    def test_fictional_fixture_is_rejected_by_default(self):
        snapshot = release_fixture()
        snapshot["source_kind"] = "fictional_fixture"
        snapshot["facts"][0].update(source_kind="fictional_fixture", approval="fixture")
        snapshot["utterances"][0].update(source_kind="fictional_fixture", approval="fixture")
        snapshot["utterances"][0]["checksum"] = utterance_checksum(snapshot["utterances"][0])
        result = render_persona_reply(snapshot, topic="residence")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "FICTIONAL_FIXTURE_DISABLED")

    def test_explicit_sandbox_fixture_is_disclosed_in_result(self):
        snapshot = release_fixture()
        snapshot["source_kind"] = "fictional_fixture"
        snapshot["approved"] = False
        snapshot["fixture_only"] = True
        snapshot["release"] = {"status": "FIXTURE", "checksum": "release-checksum"}
        snapshot["facts"][0].update(source_kind="fictional_fixture", approval="fixture")
        snapshot["utterances"][0].update(source_kind="fictional_fixture", approval="fixture")
        snapshot["utterances"][0]["checksum"] = "release-checksum"
        result = render_persona_reply(snapshot, topic="residence", allow_fictional_fixture=True)
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["fictional_fixture"])

    def test_mismatched_fixture_fact_kind_is_rejected(self):
        snapshot = release_fixture()
        snapshot["source_kind"] = "fictional_fixture"
        snapshot["approved"] = False
        snapshot["fixture_only"] = True
        snapshot["release"] = {"status": "FIXTURE", "checksum": "release-checksum"}
        snapshot["facts"][0].update(source_kind="verified_source", approval="fixture")
        snapshot["utterances"][0].update(source_kind="fictional_fixture", approval="fixture")
        snapshot["utterances"][0]["checksum"] = "release-checksum"
        result = render_persona_reply(snapshot, topic="residence", allow_fictional_fixture=True)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["reason"], "FIXTURE_SOURCE_MISMATCH")

    def test_explicit_fixture_mode_accepts_real_store_binding_snapshot_shape(self):
        db = sqlite3.connect(":memory:")
        try:
            migrate_sqlite(db)
            store = PersonaStore(db)
            persona = store.create_persona("nam_creta", "Nam Creta",
                                           source_kind="fictional_fixture")
            store.register_key("identity", "age_years", value_type="integer")
            fact = store.add_fact(persona, "identity", "age_years", 31,
                                  source_ref="fixture:age", source_kind="fictional_fixture",
                                  tellable="public")
            store.approve_fact(fact, "sandbox-reviewer")
            release = store.create_release(persona)
            store.add_release_fact(release, fact)
            store.add_utterance(release, "Mình 31 tuổi.", purpose="answer", topic="identity",
                                required_fact_ids=[fact], claims=[{
                                    "text": "31 tuổi", "fact_id": fact,
                                    "source_ref": "fixture:age",
                                }], approved=True)
            store.publish_release(release, "sandbox-reviewer")
            store.bind_post("page", "post", persona, release)
            snapshot = store.get_binding_snapshot("page", "post")
            self.assertFalse(snapshot["approved"])
            self.assertTrue(snapshot["fixture_only"])
            self.assertEqual(snapshot["snapshot"]["release"]["status"], "FIXTURE")
            blocked = render_persona_reply(snapshot, topic="identity")
            self.assertEqual(blocked["status"], "REVIEW")
            self.assertEqual(blocked["reason"], "FICTIONAL_FIXTURE_DISABLED")
            allowed = render_persona_reply(snapshot, topic="identity",
                                           allow_fictional_fixture=True)
            self.assertEqual(allowed["status"], "OK")
            self.assertEqual(allowed["text"], "Mình 31 tuổi.")
            self.assertTrue(allowed["fictional_fixture"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
