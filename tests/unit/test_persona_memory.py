import sqlite3
import unittest

from app.persona_fixture import seed_nam_creta
from app.persona_store import PersonaStore, SQLITE_MIGRATIONS, migrate_sqlite


class PersonaMemoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        migrate_sqlite(self.db)
        self.store = PersonaStore(self.db)

    def tearDown(self):
        self.db.close()

    def _approved_persona(self):
        persona = self.store.create_persona(
            "approved_memory_persona", "Approved Memory Persona", source_kind="user_provided")
        self.store.approve_persona(persona, "persona-owner")
        self.store.register_key("vehicle", "vehicle.current.model", value_type="text")
        fact = self.store.add_fact(
            persona, "vehicle", "vehicle.current.model", "Example Car",
            source_ref="user-provided:car", source_kind="user_provided", tellable="public")
        self.store.approve_fact(fact, "persona-owner")
        release = self.store.create_release(persona)
        self.store.add_release_fact(release, fact)
        return persona, release, fact

    def _fixture_draft(self):
        persona = self.store.create_persona("memory_fixture", "Memory Fixture", source_kind="fictional_fixture")
        self.store.register_key("identity", "identity.name", value_type="text")
        fact = self.store.add_fact(
            persona, "identity", "identity.name", "Fixture", source_ref="fixture:name",
            source_kind="fictional_fixture", tellable="public")
        self.store.approve_fact(fact, "fixture-reviewer")
        release = self.store.create_release(persona)
        self.store.add_release_fact(release, fact)
        return persona, release, fact

    def test_v9_migration_is_idempotent_and_preserves_existing_release_binding(self):
        legacy = sqlite3.connect(":memory:")
        try:
            legacy.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            for version, migration in SQLITE_MIGRATIONS[:4]:
                legacy.executescript(migration.read_text(encoding="utf-8"))
                legacy.execute("INSERT INTO schema_migrations VALUES (?, CURRENT_TIMESTAMP)", (version,))
            legacy.commit()
            store = PersonaStore(legacy)
            persona = store.create_persona("legacy_fixture", "Legacy Fixture", source_kind="fictional_fixture")
            store.register_key("identity", "identity.name", value_type="text")
            fact = store.add_fact(persona, "identity", "identity.name", "Legacy",
                                  source_ref="fixture:legacy-name", source_kind="fictional_fixture",
                                  tellable="public")
            store.approve_fact(fact, "fixture-reviewer")
            release = store.create_release(persona, style={"tone": "warm"})
            store.add_release_fact(release, fact)
            store.publish_release(release, "fixture-reviewer")
            store.bind_post("page", "post", persona, release)
            old_checksum = legacy.execute(
                "SELECT checksum FROM persona_releases WHERE release_id=?", (release,)).fetchone()[0]
            migrate_sqlite(legacy)
            migrate_sqlite(legacy)

            self.assertEqual(store.binding("page", "post")["release_id"], release)
            self.assertEqual(store.get_release_snapshot(persona, release)["release"]["checksum"], old_checksum)
            self.assertEqual(legacy.execute(
                "SELECT count(*) FROM schema_migrations WHERE version=9").fetchone()[0], 1)
            self.assertEqual(legacy.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            legacy.close()

    def test_fixture_memory_is_in_release_and_binding_snapshots_with_checksum(self):
        persona, release = seed_nam_creta(self.store)
        memory = self.store.get_release_snapshot(persona, release)["memories"][0]["memory_id"]
        self.store.bind_post("page", "post", persona, release)

        snapshot = self.store.get_release_snapshot(persona, release)
        binding = self.store.get_binding_snapshot("page", "post")

        self.assertEqual([item["memory_id"] for item in snapshot["memories"]], [memory])
        self.assertEqual(snapshot["memories"][0]["occurred_at"], None)
        self.assertEqual(snapshot["memories"][0]["approval"], "fixture")
        self.assertEqual(binding["snapshot"]["memories"], snapshot["memories"])
        self.assertTrue(snapshot["memories"][0]["checksum"])

    def test_public_production_memory_requires_consent_and_approved_supporting_fact(self):
        persona, release, fact = self._approved_persona()

        with self.assertRaisesRegex(ValueError, "consent"):
            self.store.add_memory(
                release, topic="vehicle_current", precision="current", source_ref="interview:1",
                source_kind="user_provided", approval="approved", privacy="public",
                tellable="public", fact_ids=[fact], approved_summary="Mình đang đi Example Car.")

        memory = self.store.add_memory(
            release, topic="vehicle_current", precision="current", source_ref="interview:1",
            source_kind="user_provided", approval="approved", privacy="public",
            tellable="public", fact_ids=[fact], consent_ref="consent:record-1",
            approved_summary="Mình đang đi Example Car.")
        self.store.publish_release(release, "persona-owner")

        self.assertEqual(self.store.get_release_snapshot(persona, release)["memories"][0]["memory_id"], memory)

    def test_memory_fact_must_be_in_the_same_release(self):
        persona, release, _ = self._fixture_draft()
        self.store.register_key("identity", "identity.nickname", value_type="text")
        other = self.store.add_fact(
            persona, "identity", "identity.nickname", "N", source_ref="fixture:nickname",
            source_kind="fictional_fixture", tellable="public")
        self.store.approve_fact(other, "fixture-reviewer")
        with self.assertRaisesRegex(ValueError, "same release"):
            self.store.add_memory(
                release, topic="identity", precision="unspecified", source_ref="fixture:nickname",
                source_kind="fictional_fixture", approval="fixture", privacy="public",
                tellable="public", fact_ids=[other], approved_summary="Mình tên N.")

    def test_memory_topic_must_match_closed_persona_query_registry(self):
        _, release, fact = self._fixture_draft()
        with self.assertRaisesRegex(ValueError, "registered persona topic"):
            self.store.add_memory(
                release, topic="weekend_travel", precision="undated", source_ref="fixture:name",
                source_kind="fictional_fixture", approval="fixture", privacy="public",
                tellable="public", fact_ids=[fact], approved_summary="Fixture memory.")

    def test_private_memory_remains_marked_for_generation_context_filtering(self):
        persona, release, _ = self._fixture_draft()
        fact = self.store.conn.execute("SELECT fact_id,source_ref FROM persona_facts WHERE persona_id=?", (persona,)).fetchone()
        self.store.add_memory(
            release, topic="identity", precision="unspecified", source_ref=fact[1],
            source_kind="fictional_fixture", approval="fixture", privacy="private",
            tellable="never", fact_ids=[fact[0]],
            approved_summary="Fixture-only private note.")
        self.store.publish_release(release, "fixture-reviewer")

        memory = self.store.get_release_snapshot(persona, release)["memories"][0]
        self.assertEqual(memory["privacy"], "private")
        self.assertEqual(memory["tellable"], "never")

    def test_revocation_is_recorded_and_removes_memory_without_changing_release_checksum(self):
        persona, release, fact = self._fixture_draft()
        source_ref = self.store.conn.execute("SELECT source_ref FROM persona_facts WHERE fact_id=?", (fact,)).fetchone()[0]
        memory = self.store.add_memory(
            release, topic="identity", precision="undated", source_ref=source_ref,
            source_kind="fictional_fixture", approval="fixture", privacy="public",
            tellable="public", fact_ids=[fact], approved_summary="A fixture memory.")
        self.store.publish_release(release, "fixture-reviewer")
        checksum = self.store.get_release_snapshot(persona, release)["release"]["checksum"]

        self.store.revoke_memory(memory, reviewer="reviewer", reason="withdrawn")

        self.assertEqual(self.store.get_release_snapshot(persona, release)["memories"], [])
        self.assertEqual(self.store.revoked_memory_ids(persona, release), [memory])
        self.assertEqual(self.store.get_release_snapshot(persona, release)["release"]["checksum"], checksum)
        self.assertTrue(self.store.verify_release(release))

    def test_published_memory_is_immutable(self):
        persona, release, fact = self._fixture_draft()
        source_ref = self.store.conn.execute("SELECT source_ref FROM persona_facts WHERE fact_id=?", (fact,)).fetchone()[0]
        self.store.add_memory(
            release, topic="identity", precision="undated", source_ref=source_ref,
            source_kind="fictional_fixture", approval="fixture", privacy="public",
            tellable="public", fact_ids=[fact], approved_summary="A fixture memory.")
        self.store.publish_release(release, "fixture-reviewer")

        with self.assertRaisesRegex(ValueError, "published release is immutable"):
            self.store.add_memory(
                release, topic="vehicle_current", precision="undated", source_ref=source_ref,
                source_kind="fictional_fixture", approval="fixture", privacy="public",
                tellable="public", fact_ids=[fact], approved_summary="Another memory.")


if __name__ == "__main__":
    unittest.main()
