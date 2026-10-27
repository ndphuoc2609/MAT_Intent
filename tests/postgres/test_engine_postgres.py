"""Real PostgreSQL races; run only against a disposable *_test database."""
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.engine import Engine, WallClock
from app.facts import Facts
from app.pg_adapter import connect, migrate
from app.state import FakeSender
from meta_services.runtime import CommentInbox
from app.intent_api import IntentService


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

    def test_inbox_claim_lease_survives_worker_restart_and_expiry_reclaims_once(self):
        worker_a = CommentInbox(self.a, lease_seconds=30)
        worker_b = CommentInbox(self.b, lease_seconds=30)
        event_id = "inbox-" + uuid.uuid4().hex
        event = {"comment_id": event_id, "text": "hello"}
        self.assertTrue(worker_a.enqueue(event))
        barrier = threading.Barrier(2)
        claimed = []
        def run(worker):
            barrier.wait(timeout=3)
            claimed.append((worker, worker.claim()))
        threads = [threading.Thread(target=run, args=(worker,)) for worker in (worker_a, worker_b)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        leases = [(worker, item) for worker, item in claimed if item]
        self.assertEqual(len(leases), 1)
        old_worker, _ = leases[0]
        # A newly constructed worker must leave the live lease owned by the old worker.
        restarted = CommentInbox(connect(self.dsn), lease_seconds=30)
        self.addCleanup(restarted.store.conn.close)
        self.assertIsNone(restarted.claim())
        self.a.conn.execute("UPDATE meta_inbox SET lease_until=? WHERE comment_id=?",
            ("2000-01-01T00:00:00+00:00", event_id))
        self.a.conn.commit()
        reclaimer = CommentInbox(connect(self.dsn), lease_seconds=30)
        self.addCleanup(reclaimer.store.conn.close)
        reclaimed = reclaimer.claim()
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed[0], event_id)
        self.assertEqual(reclaimed[2], 2)
        self.assertFalse(old_worker.finish(event_id, "DONE"))
        self.assertTrue(reclaimer.finish(event_id, "DONE"))

    def test_intent_api_postgres_persistence_duplicate_and_scope_isolation(self):
        from tests.unit.test_intent_api import request, semantic_for
        calls = []
        def model_call(post, comment, **kwargs):
            calls.append(comment)
            return {"ok": True, "semantic": semantic_for(comment), "attempts": 0,
                    "model_effective": "postgres-fixture"}
        service = IntentService(store=connect(self.dsn), api_key="pg-test-key", model_call=model_call)
        self.addCleanup(service.close)
        root_id = "root-" + uuid.uuid4().hex
        first = request("pg-event-" + uuid.uuid4().hex, root_id=root_id,
                        text="Giá lăn bánh Creta bao nhiêu?")
        first["post"]["id"] = "post-pg-" + uuid.uuid4().hex
        status, result = service.process(first)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "PROCESSED")
        duplicate_status, duplicate = service.process(first)
        self.assertEqual(duplicate_status, 200)
        self.assertTrue(duplicate["idempotent_replay"])
        self.assertEqual(service.get_event(first["event_id"])["status"], "COMPLETED")

        root = first["root"]
        other_actor = request("pg-event-" + uuid.uuid4().hex, actor="actor-b",
            root_id=root_id, current_id="pg-comment-b-" + uuid.uuid4().hex,
            text="Bản cao nhé", parent=root)
        other_actor["post"]["id"] = first["post"]["id"]
        status, result = service.process(other_actor)
        self.assertEqual(status, 200, result)
        self.assertTrue(any(route.startswith("REVIEW") for route in result["decision"]["routing"]))
        self.assertEqual(result["state_after"]["requests"], {})

        another_thread = request("pg-event-" + uuid.uuid4().hex, actor="A",
            root_id="other-" + uuid.uuid4().hex, current_id="pg-comment-c-" + uuid.uuid4().hex,
            text="Bản cao nhé")
        another_thread["post"]["id"] = first["post"]["id"]
        status, result = service.process(another_thread)
        self.assertEqual(status, 200, result)
        self.assertTrue(any(route.startswith("REVIEW") for route in result["decision"]["routing"]))
        self.assertEqual(result["state_after"]["requests"], {})

        reply = request("pg-event-" + uuid.uuid4().hex, actor="A",
            root_id=root_id, current_id="pg-comment-a-" + uuid.uuid4().hex,
            text="Bản cao nhé", parent=root)
        reply["post"]["id"] = first["post"]["id"]
        status, result = service.process(reply)
        self.assertEqual(status, 200, result)
        reqs = result["state_after"]["requests"]
        self.assertEqual(len(reqs), 1)
        self.assertEqual(next(iter(reqs.values()))["slots"]["variant"]["value"], "Cao cấp")
        self.assertEqual(calls.count("Giá lăn bánh Creta bao nhiêu?"), 1)

    def test_intent_api_duplicate_workers_and_stale_snapshot_retry(self):
        from tests.unit.test_intent_api import request, semantic_for
        from app.intent_api import IntentService
        entered, release = threading.Event(), threading.Event()
        calls = []
        def blocking_model(post, comment, **kwargs):
            calls.append(comment)
            entered.set()
            release.wait(5)
            return {"ok": True, "semantic": semantic_for(comment), "attempts": 0,
                    "model_effective": "postgres-fixture"}
        first_service = IntentService(store=connect(self.dsn), api_key="pg-test-key",
                                      model_call=blocking_model)
        second_service = IntentService(store=connect(self.dsn), api_key="pg-test-key",
                                       model_call=blocking_model)
        self.addCleanup(first_service.close)
        self.addCleanup(second_service.close)
        data = request("pg-duplicate-" + uuid.uuid4().hex)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(first_service.process, data)
            self.assertTrue(entered.wait(3))
            second = second_service.process(data)
            release.set()
            first_result = first.result(timeout=8)
        self.assertEqual(second[0], 202)
        self.assertEqual(first_result[0], 200)
        self.assertEqual(len(calls), 1)
        self.assertTrue(second_service.process(data)[1]["idempotent_replay"])

        root_id = "stale-root-" + uuid.uuid4().hex
        post_id = "stale-post-" + uuid.uuid4().hex
        root = {"id": root_id, "actor_id": "A", "text": "Giá lăn bánh Creta bao nhiêu?"}
        reply = request("pg-stale-" + uuid.uuid4().hex, root_id=root_id,
            current_id="stale-reply-" + uuid.uuid4().hex, text="Bản cao nhé", parent=root)
        reply["post"]["id"] = post_id
        state_change_calls = []
        postcard = {"id": post_id, "page_id": reply["page_id"],
                    "text": reply["post"]["text"], "vehicles": ["Creta"]}
        def stale_model(post, comment, **kwargs):
            state_change_calls.append(comment)
            if len(state_change_calls) == 1:
                external = {"page_id": reply["page_id"], "post_id": post_id,
                    "thread_root_id": root_id, "comment_id": "external-" + uuid.uuid4().hex,
                    "parent_id": root_id, "actor_id": "A", "actor_role": "user",
                    "event_type": "create", "content_version": 1,
                    "event_dedupe_key": "external-" + uuid.uuid4().hex,
                    "text": root["text"]}
                second_service.engine.process(external, postcard, semantic_for(root["text"]))
            return {"ok": True, "semantic": semantic_for(comment), "attempts": 0,
                    "model_effective": "postgres-fixture"}
        first_service.model_call = stale_model
        status, result = first_service.process(reply)
        self.assertEqual(status, 200, result)
        self.assertEqual(state_change_calls, ["Bản cao nhé", "Bản cao nhé"])
        self.assertNotIn("REVIEW_STALE_CONTEXT", result["decision"]["routing"])
        self.assertEqual(len(result["state_after"]["requests"]), 1)
        self.assertEqual(next(iter(result["state_after"]["requests"].values()))["slots"]
                         ["variant"]["value"], "Cao cấp")

