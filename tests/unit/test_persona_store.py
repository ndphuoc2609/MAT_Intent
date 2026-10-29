import sqlite3
import unittest

from app.persona_store import PersonaStore, migrate_sqlite


class PersonaStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        migrate_sqlite(self.db)
        self.store = PersonaStore(self.db)
        self.persona = self.store.create_persona(
            "nam_creta", "Nam Creta", source_kind="fictional_fixture")
        self.store.register_key("identity", "age_years", value_type="integer")

    def tearDown(self):
        self.db.close()

    def test_fixture_source_cannot_be_marked_as_real_approved_persona(self):
        row = self.db.execute(
            "SELECT source_kind, status FROM personas WHERE persona_id=?", (self.persona,)
        ).fetchone()
        self.assertEqual(tuple(row), ("fictional_fixture", "DRAFT"))

    def test_published_release_is_immutable_and_post_binding_is_pinned(self):
        fact = self.store.add_fact(
            self.persona, "identity", "age_years", 31,
            source_ref="fixture:nam_creta", source_kind="fictional_fixture",
            tellable="public")
        self.store.approve_fact(fact, "test-reviewer")
        release = self.store.create_release(self.persona)
        self.store.add_release_fact(release, fact)
        self.store.publish_release(release, "test-reviewer")
        self.store.bind_post("page-1", "post-1", self.persona, release)

        with self.assertRaisesRegex(ValueError, "published release is immutable"):
            self.store.add_release_fact(release, fact)
        with self.assertRaisesRegex(ValueError, "published release is immutable"):
            self.store.update_release(release, style={"tone": "new"})
        self.assertEqual(self.store.binding("page-1", "post-1")["release_id"], release)

    def test_publish_rejects_overlapping_singleton_facts_atomically(self):
        first = self.store.add_fact(
            self.persona, "identity", "age_years", 31,
            source_ref="fixture:age-1", source_kind="fictional_fixture",
            tellable="public", valid_from="2020-01-01", valid_to="2025-01-01")
        second = self.store.add_fact(
            self.persona, "identity", "age_years", 32,
            source_ref="fixture:age-2", source_kind="fictional_fixture",
            tellable="public", valid_from="2024-01-01", valid_to="2026-01-01")
        for fact in (first, second):
            self.store.approve_fact(fact, "reviewer")
        release = self.store.create_release(self.persona)
        for fact in (first, second):
            self.store.add_release_fact(release, fact)

        with self.assertRaisesRegex(ValueError, "overlapping facts"):
            self.store.publish_release(release, "reviewer")
        self.assertEqual(self.db.execute(
            "SELECT status FROM persona_releases WHERE release_id=?", (release,)
        ).fetchone()[0], "DRAFT")

    def test_multi_fact_requires_distinct_entity_ids_and_allows_same_interval(self):
        self.store.register_key("vehicle", "name", value_type="text", cardinality="multi",
                                entity_kind="vehicle", temporal_mode="interval")
        left = self.store.add_fact(
            self.persona, "vehicle", "name", "Creta", source_ref="fixture:v1",
            source_kind="fictional_fixture", tellable="public", entity_id="vehicle-1",
            valid_from="2021-01-01")
        right = self.store.add_fact(
            self.persona, "vehicle", "name", "Accent", source_ref="fixture:v2",
            source_kind="fictional_fixture", tellable="public", entity_id="vehicle-2",
            valid_from="2021-01-01")
        self.store.approve_fact(left, "reviewer")
        self.store.approve_fact(right, "reviewer")
        release = self.store.create_release(self.persona)
        self.store.add_release_fact(release, left)
        self.store.add_release_fact(release, right)
        self.store.publish_release(release, "reviewer")

    def test_multi_fact_rejects_overlapping_values_for_same_entity(self):
        self.store.register_key("vehicle", "name", value_type="text", cardinality="multi",
                                entity_kind="vehicle", temporal_mode="interval")
        facts = [self.store.add_fact(
            self.persona, "vehicle", "name", name, source_ref=f"fixture:{name}",
            source_kind="fictional_fixture", tellable="public", entity_id="vehicle-1",
            valid_from="2020-01-01") for name in ("Creta", "Tucson")]
        for fact in facts:
            self.store.approve_fact(fact, "reviewer")
        release = self.store.create_release(self.persona)
        for fact in facts:
            self.store.add_release_fact(release, fact)
        with self.assertRaisesRegex(ValueError, "overlapping facts"):
            self.store.publish_release(release, "reviewer")

    def test_fact_value_matches_registry_type_and_interval_is_valid(self):
        with self.assertRaisesRegex(ValueError, "must be integer"):
            self.store.add_fact(self.persona, "identity", "age_years", "31",
                                source_ref="fixture", source_kind="fictional_fixture")
        with self.assertRaisesRegex(ValueError, "valid_from must be before valid_to"):
            self.store.add_fact(self.persona, "identity", "age_years", 31,
                                source_ref="fixture", source_kind="fictional_fixture",
                                valid_from="2025-01-01", valid_to="2024-01-01")

    def test_revocation_is_append_only_and_excluded_from_active_facts(self):
        fact = self.store.add_fact(
            self.persona, "identity", "age_years", 31,
            source_ref="fixture:age", source_kind="fictional_fixture", tellable="public")
        self.store.approve_fact(fact, "reviewer")
        release = self.store.create_release(self.persona)
        self.store.add_release_fact(release, fact)
        self.store.publish_release(release, "reviewer")
        self.assertEqual(len(self.store.tellable_facts(self.persona, release)), 1)
        self.store.revoke_fact(fact, "withdrawn", "reviewer")
        self.assertEqual(self.store.tellable_facts(self.persona, release), [])
        self.assertEqual(self.store.revocation_epoch(), 1)

    def test_snapshot_exposes_only_public_approved_facts_and_verifies_checksum(self):
        public = self.store.add_fact(
            self.persona, "identity", "age_years", 31,
            source_ref="fixture:age", source_kind="fictional_fixture", tellable="public",
            valid_from="2020-01-01", valid_to="2030-01-01")
        hidden = self.store.add_fact(
            self.persona, "identity", "age_years", 32,
            source_ref="fixture:secret", source_kind="fictional_fixture", tellable="never",
            valid_from="2030-01-01")
        for fact in (public, hidden):
            self.store.approve_fact(fact, "reviewer")
        release = self.store.create_release(self.persona, style={"voice": "plain"})
        for fact in (public, hidden):
            self.store.add_release_fact(release, fact)
        self.store.publish_release(release, "reviewer")
        snapshot = self.store.get_release_snapshot(self.persona, release, on_date="2026-01-01")

        self.assertEqual([fact["fact_id"] for fact in snapshot["facts"]], [public])
        self.assertEqual(snapshot["facts"][0]["value"], 31)
        self.assertEqual(snapshot["facts"][0]["source_ref"], "fixture:age")
        self.assertEqual(snapshot["persona"]["source_kind"], "fictional_fixture")
        self.assertEqual(snapshot["facts"][0]["approval"], "fixture")
        self.assertEqual(snapshot["release"]["status"], "FIXTURE")
        self.assertFalse(snapshot["release"]["approved"])
        self.assertTrue(snapshot["fixture_only"])
        self.assertTrue(self.store.verify_release(release))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "published release is immutable"):
            self.db.execute("UPDATE persona_releases SET style='{}' WHERE release_id=?", (release,))
        self.assertTrue(self.store.verify_release(release))

    def test_fictional_fixture_cannot_be_approved_as_production_persona(self):
        with self.assertRaisesRegex(ValueError, "cannot be approved as a production persona"):
            self.store.approve_persona(self.persona, "reviewer")

    def test_publish_rejects_required_fact_without_matching_claim_evidence(self):
        fact = self.store.add_fact(
            self.persona, "identity", "age_years", 31,
            source_ref="fixture:age", source_kind="fictional_fixture", tellable="public")
        self.store.approve_fact(fact, "reviewer")
        release = self.store.create_release(self.persona)
        self.store.add_release_fact(release, fact)
        self.store.add_utterance(release, "Mình 31 tuổi.", purpose="answer",
                                 required_fact_ids=[fact], claims=[], approved=True)

        with self.assertRaisesRegex(ValueError, "claim evidence does not match required facts"):
            self.store.publish_release(release, "reviewer")

    def test_publish_rejects_claim_with_wrong_source_evidence(self):
        fact = self.store.add_fact(
            self.persona, "identity", "age_years", 31,
            source_ref="fixture:age", source_kind="fictional_fixture", tellable="public")
        self.store.approve_fact(fact, "reviewer")
        release = self.store.create_release(self.persona)
        self.store.add_release_fact(release, fact)
        claim = {"text": "31 tuổi", "fact_id": fact, "source_ref": "fixture:wrong"}
        self.store.add_utterance(release, "Mình 31 tuổi.", purpose="answer",
                                 required_fact_ids=[fact], claims=[claim], approved=True)

        with self.assertRaisesRegex(ValueError, "claim source_ref does not match fact"):
            self.store.publish_release(release, "reviewer")

    def test_claim_evidence_is_returned_with_flat_binding_snapshot(self):
        fact = self.store.add_fact(
            self.persona, "identity", "age_years", 31,
            source_ref="fixture:age", source_kind="fictional_fixture", tellable="public")
        self.store.approve_fact(fact, "reviewer")
        release = self.store.create_release(self.persona)
        self.store.add_release_fact(release, fact)
        self.store.add_utterance(release, "Mình 31 tuổi.", purpose="answer",
                                 required_fact_ids=[fact], claims=[
                                     {"text": "31 tuổi", "fact_id": fact, "source_ref": "fixture:age"}],
                                 approved=True)
        self.store.publish_release(release, "sandbox-reviewer")
        self.store.bind_post("page", "post", self.persona, release)
        snapshot = self.store.get_binding_snapshot("page", "post")

        self.assertFalse(snapshot["approved"])
        self.assertTrue(snapshot["fixture_only"])
        self.assertEqual(snapshot["source_kind"], "fictional_fixture")
        self.assertEqual(snapshot["facts"][0]["approval"], "fixture")
        self.assertEqual(snapshot["utterances"][0]["text"], "Mình 31 tuổi.")
        claim = snapshot["utterances"][0]["claims"][0]
        self.assertEqual(claim["required_fact_ids"], [fact])
        self.assertEqual(claim["evidence"][0]["source_ref"], "fixture:age")
        from app.persona_reply import utterance_checksum
        self.assertEqual(snapshot["utterances"][0]["checksum"],
                         utterance_checksum({k: v for k, v in snapshot["utterances"][0].items()
                                             if k != "checksum"}))

    def test_root_analysis_replays_only_when_payload_matches(self):
        release = self.store.create_release(self.persona)
        self.store.publish_release(release, "sandbox-reviewer")
        self.store.bind_post("page", "post", self.persona, release)
        record = self.store.record_root_analysis(
            "page", "post", "root", "comment-1", "actor-1", {"scope": "in_scope"}, "draft",
            persona_snapshot={"source_kind": "fictional_fixture"}, persona_id=self.persona,
            release_id=release, root_text="Cmt gốc", model_version="mock", schema_version="v1")
        self.assertEqual(record["root_actor_id"], "actor-1")
        self.assertEqual(record["root_comment_id"], "comment-1")
        self.assertEqual(record["semantic"], {"scope": "in_scope"})
        with self.assertRaisesRegex(ValueError, "different content"):
            self.store.record_root_analysis(
                "page", "post", "root", "comment-1", "actor-2", {"scope": "in_scope"}, "draft",
                persona_snapshot={"source_kind": "fictional_fixture"}, persona_id=self.persona,
                release_id=release, root_text="Cmt gốc", model_version="mock", schema_version="v1")


if __name__ == "__main__":
    unittest.main()
