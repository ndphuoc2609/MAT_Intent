"""Real PostgreSQL races; run only against a disposable *_test database."""
import os
import threading
import unittest
import uuid

from app.engine import Engine, WallClock
from app.facts import Facts
from app.pg_adapter import connect, migrate
from app.state import FakeSender


class PostgreSqlEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ.get("MAT_INTENT_TEST_DATABASE_URL")
        if not dsn:
            raise unittest.SkipTest("MAT_INTENT_TEST_DATABASE_URL is unset")
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        options = conninfo_to_dict(dsn)
        if not options.get("dbname", "").endswith("_test"):
            raise RuntimeError("PostgreSQL tests require a database name ending in _test")
        cls.base_dsn = dsn
        cls.schema = "mat_intent_test_" + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'CREATE SCHEMA "{cls.schema}"')
        cls.dsn = make_conninfo(dsn, options=f"-csearch_path={cls.schema}")
        try:
            migrate(cls.dsn)
            migrate(cls.dsn)  # version records make repeated migration runs safe
            cls.a = connect(cls.dsn)
            cls.b = connect(cls.dsn)
            cls.engine_a = Engine(cls.a, Facts([]), FakeSender(), WallClock())
            cls.engine_b = Engine(cls.b, Facts([]), FakeSender(), WallClock())
        except Exception:
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
            raise

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "schema"):
            return
        import psycopg
        for name in ("a", "b"):
            store = getattr(cls, name, None)
            if store:
                store.conn.close()
        with psycopg.connect(cls.base_dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')

    def event(self, comment_id, actor="actor-a", thread="root"):
        return dict(page_id="page-a", post_id="post-a", thread_root_id=thread,
                    parent_id=thread, comment_id=comment_id, actor_id=actor,
                    actor_role="user", event_type="create", content_version=1,
                    event_dedupe_key=f"{comment_id}:1", text="chào")

    def process_no_semantic(self, engine, event):
        return engine.process(event, {"id": "post-a", "page_id": "page-a",
                                      "text": "Creta", "vehicles": ["Creta"]}, None)

    def test_duplicate_event_is_idempotent_across_connections(self):
        event = self.event("same-event")
        self.assertEqual(self.process_no_semantic(self.engine_a, event)["routing"], ["REVIEW_NO_SEMANTIC"])
        self.assertIn("DUPLICATE_IGNORED", self.process_no_semantic(self.engine_b, event)["routing"])

    def test_two_actor_thread_races_and_same_actor_thread_races(self):
        cases = [(("actor-a", "actor-a"), "root"), (("actor-b", "actor-c"), "shared-root")]
        for actors, root in cases:
            barrier = threading.Barrier(2)
            errors = []
            def run(engine, actor, comment_id):
                try:
                    barrier.wait(timeout=3)
                    self.process_no_semantic(engine, self.event(comment_id, actor, root))
                except Exception as exc:
                    errors.append(exc)
            first = threading.Thread(target=run, args=(self.engine_a, actors[0], uuid.uuid4().hex))
            second = threading.Thread(target=run, args=(self.engine_b, actors[1], uuid.uuid4().hex))
            first.start(); second.start(); first.join(10); second.join(10)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])

    def test_optimistic_request_version_rejects_stale_writer(self):
        store = self.a
        store.conn.execute("BEGIN IMMEDIATE")
        store.insert_request(dict(id="versioned", page_id="page-a", actor_id="actor-a",
            post_id="post-a", thread_root_id="root", topic="price", status="OPEN",
            version=1, slots={}, source_events=[], desired_outcomes=["price"],
            created_at="now", updated_at="now"))
        store.conn.commit()
        stale = store.get_request("versioned")
        current = store.get_request("versioned")
        current.update(status="READY", version=2)
        store.save_request(current); store.conn.commit()
        stale.update(status="WAITING_USER", version=2)
        store.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(RuntimeError, "version conflict"):
            store.save_request(stale)
        store.conn.rollback()

    def test_state_change_during_inference_is_rejected_as_stale(self):
        event = self.event("late-model-event")
        token = self.engine_a.context(event, {"id": "post-a", "page_id": "page-a",
                                               "text": "Creta", "vehicles": ["Creta"]})["state_token"]
        self.process_no_semantic(self.engine_b, self.event("intervening-event"))
        result = self.engine_a.process(event, {"id": "post-a", "page_id": "page-a",
            "text": "Creta", "vehicles": ["Creta"]}, {"scope": "in_scope"},
            expected_state_token=token)
        self.assertIn("REVIEW_STALE_CONTEXT", result["routing"])

