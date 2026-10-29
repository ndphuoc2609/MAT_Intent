"""Opt-in integration test. DSN must target an isolated disposable *test database*."""
import os
import unittest
import uuid


class PostgresPersonaStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ.get("MAT_INTENT_PERSONA_TEST_DSN")
        if not cls.dsn:
            raise unittest.SkipTest("MAT_INTENT_PERSONA_TEST_DSN is not configured")
        import psycopg
        with psycopg.connect(cls.dsn) as connection:
            dbname = connection.execute("SELECT current_database()").fetchone()[0]
        if "test" not in dbname.lower() and "tmp" not in dbname.lower():
            raise RuntimeError("persona PostgreSQL tests require a disposable database name containing test or tmp")
        from app.pg_adapter import connect, migrate
        migrate(cls.dsn)
        cls.connection = connect(cls.dsn)
        from app.persona_store import PersonaStore
        cls.store = PersonaStore(cls.connection)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "connection", None):
            cls.store.conn.close()

    def test_postgres_fixture_release_and_root_analysis_round_trip(self):
        from app.persona_fixture import seed_nam_creta
        # Each test run uses a fresh persona slug; no existing row is changed or deleted.
        persona_id, release_id = seed_nam_creta(self.store)
        snapshot = self.store.get_release_snapshot(persona_id, release_id)
        self.assertEqual(snapshot["release"]["status"], "FIXTURE")
        self.assertFalse(snapshot["release"]["approved"])
        self.assertTrue(any(claim.get("source_ref") for utterance in snapshot["utterances"]
                            for claim in utterance.get("claims", [])))
        nonce = uuid.uuid4().hex
        page_id, post_id, thread_id = f"persona-test-page-{nonce}", f"persona-test-post-{nonce}", f"persona-test-thread-{nonce}"
        self.store.bind_post(page_id, post_id, persona_id, release_id)
        root_semantic = {"schema_version": "1", "scope": "in_scope", "requests": [], "operations": []}
        self.store.record_root_analysis(page_id, post_id, thread_id, "root-comment", "actor-a",
            root_semantic, "reply", persona_snapshot={"fixture_only": True},
            persona_id=persona_id, release_id=release_id, root_text="Bạn ở đâu?")
        stored = self.store.root_analysis(page_id, post_id, thread_id)
        self.assertEqual(stored["root_actor_id"], "actor-a")
        self.assertEqual(stored["semantic"], root_semantic)


if __name__ == "__main__":
    unittest.main()
