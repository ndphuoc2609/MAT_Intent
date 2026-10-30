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

    def test_story_addendum_is_versioned_inside_api_style_transaction_guard(self):
        from app.persona_fixture import seed_nam_creta
        from app.persona_store import PersonaStore
        from app.persona_stories import StoryStore
        repo = PersonaStore(self.a)
        persona_id, release_id = seed_nam_creta(repo)
        nonce = uuid.uuid4().hex
        page, post = "story-page-" + nonce, "story-post-" + nonce
        repo.bind_post(page, post, persona_id, release_id)
        snapshot = repo.get_binding_snapshot(page, post)
        vehicle = next(f for f in snapshot["facts"] if f["key"] == "vehicle.current.model")
        stories = StoryStore(repo)
        original = {'proposal_id': 'story-v1', 'idempotency_key': nonce + '-story',
            'persona_version': release_id, 'topic': 'travel_story',
            'event': 'Mình đi Đà Lạt bằng Creta', 'time_start': '2025-04-01', 'time_end': None,
            'entities': {'vehicle': 'Creta', 'location': 'Đà Lạt', 'people': []},
            'base_fact_ids': [vehicle['fact_id']], 'new_details': [], 'relation': None,
            'vehicle_use': 'owned'}
        created = stories.propose(snapshot, original)
        self.assertEqual(created['status'], 'ACCEPTED', created)
        addition = dict(original, proposal_id='story-v2', idempotency_key=nonce + '-addendum',
            new_details=['Trong chuyến đó xe bị ngập'],
            relation={'type': 'addendum', 'memory_id': created['memory_id'], 'base_version': 1})

        def read_guard(operation):
            with self.a.lock:
                return operation()

        def mutation_guard(operation):
            with self.a.lock, self.a.conn.raw.transaction():
                return operation()

        updated = stories.propose(snapshot, addition, judge=lambda *_: 'ADDENDUM',
            read_guard=read_guard, mutation_guard=mutation_guard)
        self.assertEqual(updated['status'], 'ADDENDUM', updated)
        self.assertEqual(updated['memory_id'], created['memory_id'])
        self.assertEqual(updated['memory']['version'], 2)
        self.assertEqual(updated['memory']['new_details'], ['Trong chuyến đó xe bị ngập'])
        self.assertEqual(repo.conn.execute(
            'SELECT count(*) FROM persona_story_revisions WHERE memory_id=?',
            (created['memory_id'],)).fetchone()[0], 1)

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

    def test_intent_api_expired_lease_fences_old_worker_and_restart_replays_review(self):
        from tests.unit.test_intent_api import request, semantic_for
        from app.intent_api import IntentService
        entered, release = threading.Event(), threading.Event()
        calls = []
        def blocking_model(post, comment, **kwargs):
            calls.append(comment)
            entered.set()
            release.wait(5)
            return {"ok": True, "semantic": semantic_for(comment), "attempts": 0}
        first_service = IntentService(store=connect(self.dsn), api_key="pg-test-key",
                                      model_call=blocking_model)
        second_service = IntentService(store=connect(self.dsn), api_key="pg-test-key",
                                       model_call=blocking_model)
        self.addCleanup(first_service.close)
        self.addCleanup(second_service.close)
        first_service.lease_seconds = second_service.lease_seconds = 1
        data = request("pg-lease-" + uuid.uuid4().hex, root_id="lease-root-" + uuid.uuid4().hex)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(first_service.process, data)
            self.assertTrue(entered.wait(3))
            second_service.store.conn.execute(
                "UPDATE intent_api_events SET lease_until=? WHERE event_id=?",
                ("2000-01-01T00:00:00+00:00", data["event_id"]))
            expired = second_service.process(data)
            release.set()
            first_result = first.result(timeout=8)
        self.assertEqual(expired[0], 200)
        self.assertEqual(expired[1]["status"], "REVIEW")
        self.assertEqual(first_result[0], 409)
        self.assertEqual(first_result[1]["status"], "WORKER_LEASE_LOST")
        self.assertEqual(first_service.get_event(data["event_id"])["status"], "REVIEW")
        rows = first_service.store.conn.execute("SELECT count(*) FROM requests WHERE thread_root_id=?",
                                                (data["thread_id"],)).fetchone()
        self.assertEqual(rows[0], 0)
        restarted = IntentService(store=connect(self.dsn), api_key="pg-test-key",
                                  model_call=blocking_model)
        self.addCleanup(restarted.close)
        replay = restarted.process(data)
        self.assertEqual(replay[0], 200)
        self.assertTrue(replay[1]["idempotent_replay"])
        self.assertEqual(len(calls), 1)

    def test_intent_api_crash_after_inference_is_durable_review_after_restart(self):
        from tests.unit.test_intent_api import request, semantic_for
        from app.intent_api import IntentService
        calls = []
        def model_call(post, comment, **kwargs):
            calls.append(comment)
            return {"ok": True, "semantic": semantic_for(comment), "attempts": 0}
        first_service = IntentService(store=connect(self.dsn), api_key="pg-test-key",
                                      model_call=model_call)
        self.addCleanup(first_service.close)
        first_service.lease_seconds = 1
        data = request("pg-crash-after-inference-" + uuid.uuid4().hex,
                       root_id="crash-root-" + uuid.uuid4().hex)
        first_service.engine.process = lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit())
        with self.assertRaises(SystemExit):
            first_service.process(data)
        row = first_service.get_event(data["event_id"])
        self.assertEqual(row["status"], "PROCESSING")
        first_service.store.conn.execute(
            "UPDATE intent_api_events SET lease_until=? WHERE event_id=?",
            ("2000-01-01T00:00:00+00:00", data["event_id"]))
        restarted = IntentService(store=connect(self.dsn), api_key="pg-test-key",
                                  model_call=model_call)
        self.addCleanup(restarted.close)
        result = restarted.process(data)
        self.assertEqual(result[0], 200)
        self.assertEqual(result[1]["status"], "REVIEW")
        self.assertIn("REVIEW_INFERENCE_OUTCOME_UNKNOWN", result[1]["decision"]["routing"])
        self.assertEqual(len(calls), 1)

    def test_intent_api_state_and_result_roll_back_together_and_event_is_retryable(self):
        from tests.unit.test_intent_api import request, semantic_for
        from app.intent_api import IntentService
        calls = []
        def model_call(post, comment, **kwargs):
            calls.append(comment)
            return {"ok": True, "semantic": semantic_for(comment), "attempts": 0}
        service = IntentService(store=connect(self.dsn), api_key="pg-test-key", model_call=model_call)
        self.addCleanup(service.close)
        data = request("pg-commit-failure-" + uuid.uuid4().hex,
                       root_id="commit-root-" + uuid.uuid4().hex)
        service.store.conn.execute("""CREATE FUNCTION fail_completed_result() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
              IF NEW.status = 'COMPLETED' THEN RAISE EXCEPTION 'injected result-commit failure'; END IF;
              RETURN NEW;
            END $$""")
        service.store.conn.execute("CREATE TRIGGER fail_result BEFORE UPDATE ON intent_api_events "
            "FOR EACH ROW EXECUTE FUNCTION fail_completed_result()")
        failed = service.process(data)
        self.assertEqual(failed[0], 503)
        self.assertEqual(failed[1]["status"], "PROCESSING_ERROR")
        self.assertEqual(service.get_event(data["event_id"])["status"], "PENDING")
        count = service.store.conn.execute("SELECT count(*) FROM requests WHERE thread_root_id=?",
                                           (data["thread_id"],)).fetchone()
        self.assertEqual(count[0], 0)
        service.store.conn.execute("DROP TRIGGER fail_result ON intent_api_events")
        service.store.conn.execute("DROP FUNCTION fail_completed_result()")
        succeeded = service.process(data)
        self.assertEqual(succeeded[0], 200, succeeded)
        self.assertEqual(service.get_event(data["event_id"])["status"], "COMPLETED")
        self.assertEqual(len(calls), 2)

