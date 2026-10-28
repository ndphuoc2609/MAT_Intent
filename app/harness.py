"""Harness: chạy event stream qua cùng Engine, ghi trace + checkpoint từng ca.
Dùng cho contract tests, replay và API eval (không dùng logic riêng trong test)."""
from __future__ import annotations
import json
from .state import Store, FakeSender
from .facts import DealerDirectoryError, Facts, load_dealers
from .engine import Engine, Clock

def make_engine(db=":memory:", clock=None, facts=None, kill_switch=False,
                receiver_path=None, drain_on_start=False, dealer_db_path=None):
    # A5c: receiver mô phỏng external system PHẢI bền qua restart khi engine
    # dùng DB file: mặc định receiver file nằm cạnh DB (db + ".receiver"),
    # để engine mới mở cùng DB vẫn thấy effect cũ (không coi receiver mới
    # rỗng là bằng chứng lần gửi cũ chưa xảy ra). DB :memory: vẫn dùng
    # receiver :memory: (không có gì bền qua restart để giữ).
    from .state import FakeReceiver
    if receiver_path is None and db != ":memory:":
        receiver_path = db + ".receiver"
    sender = FakeSender(receiver=FakeReceiver(receiver_path) if receiver_path else FakeReceiver())
    store = Store(db)
    directory_error = None
    if dealer_db_path is not None:
        try:
            dealer_facts = load_dealers(dealer_db_path)
        except DealerDirectoryError as exc:
            dealer_facts = []
            directory_error = str(exc)
        facts = Facts(facts=facts.facts if facts is not None else [],
                      verified_at=facts.verified_at if facts is not None else None,
                      dealer_facts=dealer_facts)
    elif facts is None:
        facts = Facts()
    eng = Engine(store, facts, sender, clock or Clock(__import__("datetime").datetime.now(__import__("datetime").timezone.utc)), kill_switch)
    eng.dealer_directory_error = directory_error
    if drain_on_start:
        eng.drain_intended()
    return eng

def run_scenario(scenario, engine=None, semantic_by_turn=None):
    """scenario: {id, postcard, turns:[{event, semantic?}], initial?...}
    semantic_by_turn: dict turn_index -> semantic (fixture) hoặc callable(event, ctx)."""
    eng = engine or make_engine()
    traces = []
    for i, turn in enumerate(scenario["turns"]):
        ev = turn["event"]
        sem = turn.get("semantic")
        if sem is None and semantic_by_turn is not None:
            sem = semantic_by_turn(i, ev, eng) if callable(semantic_by_turn) else semantic_by_turn.get(i)
        res = eng.process(ev, scenario["postcard"], sem)
        traces.append(dict(turn=i, event=ev, semantic=sem, result={
            k: v for k, v in res.items() if k != "audit"}))
    # state cuối
    with eng.store.lock:
        reqs = eng.store.conn.execute("SELECT id, topic, status, version, slots, actor_id FROM requests").fetchall()
        pend = eng.store.conn.execute("SELECT id, request_id, status, proposition FROM pending").fetchall()
    return dict(scenario_id=scenario["id"], traces=traces,
                final_requests=[dict(id=r[0], topic=r[1], status=r[2], version=r[3],
                                     slots=json.loads(r[4]), actor_id=r[5]) for r in reqs],
                final_pending=[dict(id=p[0], request_id=p[1], status=p[2], proposition=p[3]) for p in pend],
                ledger=eng.store.ledger(), sends=eng.sender.sent)
