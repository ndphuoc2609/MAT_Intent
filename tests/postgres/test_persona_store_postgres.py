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

    def test_postgres_immutable_releases_and_root_snapshots(self):
        import psycopg
        from app.persona_fixture import seed_nam_creta
        fixture, fixture_release = seed_nam_creta(self.store)
        name = "pg-" + uuid.uuid4().hex
        persona = self.store.create_persona(name, "display-" + name, source_kind="user_provided")
        self.store.approve_persona(persona, "reviewer")
        release = self.store.create_release(persona)
        self.store.publish_release(release, "reviewer")
        for rid in (fixture_release, release):
            with self.subTest(release=rid):
                with self.assertRaises(psycopg.Error):
                    self.store.add_utterance(rid, "extra", purpose="answer_persona")
                with self.assertRaises(psycopg.Error):
                    self.store.add_hook(rid, "extra", purpose="answer_persona")
        nonce = uuid.uuid4().hex
        self.store.bind_post("p" + nonce, "post" + nonce, fixture, fixture_release)
        self.store.record_root_analysis("p" + nonce, "post" + nonce, "root", "root", "actor", {}, None,
            persona_snapshot={}, persona_id=fixture, release_id=fixture_release, root_text="hello")
        for statement in ("UPDATE persona_root_analyses SET root_text='changed' WHERE page_id=?",
                          "DELETE FROM persona_root_analyses WHERE page_id=?"):
            with self.assertRaises(psycopg.Error):
                self.store.conn.execute(statement, ("p" + nonce,))

    def test_postgres_fact_approval_pair_cannot_be_crossed(self):
        import psycopg
        if not self.store.conn.execute("SELECT 1 FROM persona_key_registry WHERE category=? AND canonical_key=?",
                                       ("review_test", "review_test.age")).fetchone():
            self.store.register_key("review_test", "review_test.age", value_type="integer")
        name = "pg-pair-" + uuid.uuid4().hex
        persona = self.store.create_persona(name, "display-" + name, source_kind="fictional_fixture")
        a = self.store.add_fact(persona, "review_test", "review_test.age", 31,
            source_ref="fixture:a", source_kind="fictional_fixture", tellable="public")
        b = self.store.add_fact(persona, "review_test", "review_test.age", 32,
            source_ref="fixture:b", source_kind="fictional_fixture", tellable="public")
        approval = self.store.approve_fact(a, "reviewer")
        release = self.store.create_release(persona)
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.store.conn.execute("INSERT INTO release_facts(release_id,persona_id,fact_id,approval_id) VALUES (?,?,?,?)",
                                    (release, persona, b, approval))
        self.store.add_release_fact(release, a)
        self.store.approve_fact(a, "reviewer", approved=False)
        with self.assertRaisesRegex(ValueError, "latest matching approval"):
            self.store.publish_release(release, "reviewer")
        self.assertEqual(self.store.conn.execute("SELECT status FROM persona_releases WHERE release_id=?",
                                                  (release,)).fetchone()[0], "DRAFT")

    def test_postgres_recovery_from_other_worker_respects_opt_out(self):
        from unittest.mock import patch
        from app.engine import Engine, WallClock
        from app.facts import Facts
        from app.pg_adapter import connect
        from app.persona_fixture import seed_nam_creta
        from app.persona_store import PersonaStore
        from app.state import FakeSender
        persona, release = seed_nam_creta(self.store)
        nonce = uuid.uuid4().hex
        page, post, thread, actor = "p" + nonce, "post" + nonce, "root" + nonce, "actor" + nonce
        self.store.bind_post(page, post, persona, release)
        sender = FakeSender()
        engine = Engine(self.connection, Facts([]), sender, WallClock())
        engine.persona_store, engine.allow_persona_fixtures = self.store, True
        text = "Bạn ở đâu vậy?"
        event = {"page_id": page, "post_id": post, "thread_root_id": thread, "parent_id": thread,
                 "comment_id": thread, "actor_id": actor, "actor_role": "user", "event_type": "create",
                 "content_version": 1, "text": text, "event_dedupe_key": thread + ":1"}
        semantic = {"schema_version": "1", "sentiment": "NEU", "sarcasm": "absent", "scope": "in_scope",
                    "requests": [{"topic": "social", "desired_outcome": "ack", "polarity": "positive"}],
                    "operations": [{"op": "SOCIAL_ACK", "evidence_excerpt": text}], "uncertainties": []}
        with patch.object(engine, "_dispatch", side_effect=RuntimeError("crash")):
            engine.process(event, {"text": "Bài viết", "persona_binding": {"release_id": release}}, semantic)
        other = connect(self.dsn)
        try:
            other.block(page, actor, "public_reply")
            other.conn.commit()
            restarted = Engine(other, Facts([]), sender, WallClock())
            restarted.persona_store, restarted.allow_persona_fixtures = PersonaStore(other), True
            self.assertIn((thread + ":reply", "BLOCKED"), restarted.drain_intended())
            self.assertFalse(sender.receiver.has_effect(thread + ":reply"))
        finally:
            other.conn.close()


if __name__ == "__main__":
    unittest.main()
