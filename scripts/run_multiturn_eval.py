"""Chạy hội thoại nhiều lượt với Luna thật qua Engine (không inject fixture).

Mỗi lượt: context lấy từ state thật của engine (recent của đúng actor,
active requests, open pendings kèm id) -> Luna -> validate (1 repair nếu lỗi)
-> eng.process(). Script/expected KHÔNG BAO GIỜ vào inference: runner chỉ đọc
turn["text"], không đọc turn["expected"] (scorer đọc expected sau).
Checkpoint theo lượt, key v2 = hash(comment, parent, postcard, state
fingerprint, SYSTEM, schema, MODEL, context): resume chỉ khi key v2 khớp;
checkpoint v1 cũ (không có state/input đầy đủ) thì cảnh báo + gọi lại.
Checkpoint chỉ lưu model response; kết quả engine tính lại khi resume.
Budget: ledger cộng dồn (runs/budget_ledger.jsonl) + prior args, guard trước
mỗi call và mỗi repair.
Usage: python3 scripts/run_multiturn_eval.py --input datasets/multiturn/dev.jsonl \
  --out runs/mt_dev_v2.json --ckdir runs/mt_checkpoints_v2
"""
import argparse, hashlib, json, os, sys, uuid
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from datetime import datetime, timezone
from app.adapters.luna import call_luna, MODEL, SYSTEM
from app.semantics import validate_semantic
from app.harness import make_engine
from app.engine import Clock
from app.facts import Facts
from app.policy import AUTO_ACTIONS, HANDOFF_SLA_HOURS, PENDING_TTL_HOURS

CONTEXT_TOKEN_LIMIT = 12000
CONTEXT_CHAR_LIMIT = CONTEXT_TOKEN_LIMIT * 3


def _over_context_budget(serialized):
    # shortcut: UTF-8 bytes/3 estimates tokens; use a provider tokenizer when one is available.
    return len(serialized.encode("utf-8")) > CONTEXT_TOKEN_LIMIT * 3


def fmt_context(ctx, *, prior_customer_turns=None):
    """Serialize context as a bounded object; drop old low-priority messages whole."""
    recent = list(ctx.get("recent") or [])
    bot = list(ctx.get("recent_bot_replies") or [])
    prior = list(prior_customer_turns or [])
    messages = list(ctx.get("messages") or [])
    if not any(m.get("role") in ("root", "parent") for m in messages if isinstance(m, dict)):
        messages += [{"role": "parent", "text": text} for text in ctx.get("parent_texts", [])
                     if isinstance(text, str)]
    payload = {
        "format": "mat_intent_context_v1", "state_token": ctx.get("state_token"),
        "postcard": ctx.get("postcard") or {}, "messages": messages,
        "recent": recent, "active_requests": ctx.get("active_requests") or [],
        "pending": ctx.get("pending") or [], "contact_blocks": ctx.get("contact_blocks") or [],
        "prior_customer_turns": prior,
        "recent_bot_replies": [{"text": x, "verified_fact": False} for x in bot],
    }
    encode = lambda: json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while _over_context_budget(encode()) and recent:
        recent.pop()  # newest is first; discard only the oldest whole message
    while _over_context_budget(encode()) and prior:
        prior.pop(0)
    while _over_context_budget(encode()) and bot:
        bot.pop(0)
        payload["recent_bot_replies"] = [{"text": x, "verified_fact": False} for x in bot]
    if _over_context_budget(encode()):
        raise ValueError("required post/root/parent/current/state context exceeds budget")
    return encode()

def snapshot(eng, page, actor):
    with eng.store.lock:
        reqs = eng.store.conn.execute(
            "SELECT id, topic, status, slots, actor_id FROM requests ORDER BY rowid").fetchall()
        pend = eng.store.conn.execute(
            "SELECT id, request_id, status, proposition, actor_id FROM pending ORDER BY rowid").fetchall()
        out = eng.store.conn.execute("SELECT dedupe_key, purpose, status FROM outbox").fetchall()
    return ([dict(id=r[0], topic=r[1], status=r[2], slots=json.loads(r[3]), actor_id=r[4]) for r in reqs],
            [dict(id=p[0], request_id=p[1], status=p[2], proposition=p[3], actor_id=p[4]) for p in pend],
            [dict(dedupe_key=o[0], purpose=o[1], status=o[2]) for o in out])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--ckdir", default=os.path.join(ROOT, "runs", "mt_checkpoints_v2"))
    ap.add_argument("--prior-calls", type=int, default=148)
    ap.add_argument("--prior-cost", type=float, default=0.0392)
    ap.add_argument("--max-calls", type=int, default=300)
    ap.add_argument("--max-cost", type=float, default=2.00)
    ap.add_argument("--reserve-cost", type=float, default=0.00032)
    args = ap.parse_args()
    os.makedirs(args.ckdir, exist_ok=True)
    schema = open(os.path.join(ROOT, "config", "schema_versions", "semantic_v1.json")).read()
    convos = [json.loads(l) for l in open(args.input) if l.strip()]
    total_calls, total_cost = 0, 0.0
    budget_hit = False
    # A8: single source of truth — đọc ledger MỘT LẦN lúc bắt đầu làm
    # baseline, rồi guard chỉ cộng prior + run hiện tại. Trước đây guard
    # đọc lại totals() sau mỗi lần record (ledger đã chứa call vừa chạy)
    # rồi CỘNG THÊM total_calls -> cùng một call bị đếm hai lần, cap=2
    # dừng sau đúng 1 call.
    from scripts import budget_ledger as _bl
    lc0, lcost0, lunk0, _ = _bl.totals()
    run_unknown = False
    out_convos = []
    for conv in convos:
        if budget_hit: break
        eng = make_engine(clock=Clock(datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)), facts=Facts())
        pc = conv["postcard"]
        turns_out = []
        for ti, turn in enumerate(conv["turns"]):
            actor, text = turn["actor"], turn["text"]
            cid = f"{conv['id']}_t{ti}"
            ev = dict(page_id="page_hyundai_sample", post_id="post_1", thread_root_id="root",
                      comment_id=cid, parent_id="root", actor_id=actor, actor_role="user",
                      event_type="create", content_version=1, text=text, event_dedupe_key=f"{cid}:1")
            # Context từ state thật TRƯỚC khi process lượt này.
            ctx = eng.context(ev, pc)
            ctx_str = fmt_context(ctx)
            open_before = [p["id"] for p in ctx["pending"]] if ctx["pending"] else []
            req_before, pend_before, out_before = snapshot(eng, "page_hyundai_sample", actor)
            # Cache key v2 (R13): gồm cả state fingerprint (requests/pendings
            # dạng logical + recent texts), không chỉ text+prompt+model.
            from scripts.cache_key import (full_cache_key, state_fingerprint, is_legacy_checkpoint,
                                           request_logical_key, pending_logical_key)
            from scripts import budget_ledger
            actor_reqs = [r for r in req_before if r.get("actor_id") == actor]
            actor_pends = [p for p in pend_before if p.get("actor_id") == actor]
            # snapshot pendings thiếu qtype/expected_slot/options -> lấy từ store.
            full_pends = []
            for p in actor_pends:
                full = eng.store.get_pending(p["id"]) or {}
                full_pends.append(dict(p, qtype=full.get("qtype"),
                                       expected_slot=full.get("expected_slot"),
                                       options=full.get("options")))
            cache_context = {
                "scope": {k: ctx.get("event", {}).get(k)
                          for k in ("page_id", "post_id", "thread_root_id", "actor_id")},
                "messages": [{k: m.get(k) for k in ("role", "actor_id", "text")}
                             for m in ctx.get("messages", []) if m.get("role") != "current"],
                "recent": [{k: m.get(k) for k in ("actor_id", "text")}
                           for m in ctx.get("recent", [])],
                "requests": sorted(request_logical_key(r) for r in actor_reqs),
                "pendings": sorted(pending_logical_key(p, {r["id"]: r for r in actor_reqs})
                                   for p in full_pends if p.get("status") == "OPEN"),
                "contact_blocks": ctx.get("contact_blocks", []),
            }
            state_fp = state_fingerprint(actor_reqs, full_pends,
                                         [r["text"] for r in ctx["recent"]],
                                         context=cache_context,
                                         policy={"facts_version": eng.facts.version,
                                                 "kill_switch": eng.kill_switch,
                                                 "auto_actions": sorted(AUTO_ACTIONS),
                                                 "handoff_sla_hours": HANDOFF_SLA_HOURS,
                                                 "pending_ttl_hours": PENDING_TTL_HOURS})
            parent_context = [m.get("text", "") for m in ctx.get("messages", [])
                              if m.get("role") in ("root", "parent")]
            cache_key = full_cache_key(comment=text, parent=parent_context, postcard=pc,
                                       state_fp=state_fp, system=SYSTEM, schema=schema,
                                       model=MODEL)
            ck = os.path.join(args.ckdir, f"{conv['id']}_{ti}.json")
            sem, attempts, cost, model_eff, val_errs = None, 0, 0.0, MODEL, []
            if os.path.exists(ck):
                rec = json.load(open(ck))
                if rec.get("cache_key_v2") == cache_key and rec.get("semantic"):
                    # A7: cache hit KHÔNG có nghĩa dùng nguyên semantic cũ —
                    # semantic có thể mang pending/request id của engine cũ.
                    # Remap sang id của engine hiện tại theo logical key; nếu
                    # không remap được thì coi như miss (gọi lại adapter).
                    from scripts.resume import try_remap_cached
                    # actor_reqs / full_pends đã tính ở trên cho state_fp:
                    # chính là state logic hiện tại (kèm id mới) để remap.
                    remapped, remap_err = try_remap_cached(
                        rec["semantic"], rec.get("state_snapshot"), actor_reqs, full_pends)
                    if remapped is not None:
                        sem, attempts, cost = remapped, 0, 0.0
                        model_eff, val_errs = rec.get("model_effective", MODEL), rec.get("validation_errors", [])
                    else:
                        print(f"WARNING checkpoint {ck} cache hit but remap failed ({remap_err}); re-calling")
                elif is_legacy_checkpoint(rec):
                    # Legacy key không chứng minh được input/state trùng khớp:
                    # cảnh báo + gọi lại, không reuse im lặng.
                    print(f"WARNING legacy checkpoint {ck} ignored (v1 key, state unverified); re-calling")
            def _guard(est_cost=0.0, unknown=False):
                # Guard cộng dồn trên baseline duy nhất: ledger lúc bắt đầu
                # + prior args + run hiện tại (không đọc lại ledger sau mỗi
                # record — đó là double count cũ).
                base_calls = lc0 + args.prior_calls + total_calls
                base_cost = lcost0 + args.prior_cost + total_cost
                if base_calls + 1 > args.max_calls:
                    return False, f"calls {base_calls}+1 > {args.max_calls}"
                if unknown or lunk0 > 0 or run_unknown:
                    return False, "cost unknown in ledger, spend blocked fail-closed"
                if base_cost + est_cost > args.max_cost:
                    return False, f"cost {base_cost:.5f}+{est_cost:.5f} > {args.max_cost}"
                return True, "ok"
            def _make_hook():
                # Guard per-attempt (A8): adapter gọi hook này trước MỖI
                # HTTP attempt (kể cả retry); reservation ghi atomically trước network.
                reservations = []
                def hook():
                    reservation_id = uuid.uuid4().hex
                    allowed, _ = budget_ledger.reserve(
                        budget_ledger.DEFAULT_PATH, reservation_id=reservation_id,
                        max_calls=args.max_calls, max_cost=args.max_cost,
                        reserve_cost_usd=args.reserve_cost,
                        prior_calls=args.prior_calls, prior_cost=args.prior_cost)
                    if allowed: reservations.append(reservation_id)
                    return allowed
                return hook, reservations
            if sem is None:
                ok, reason = _guard()
                if not ok:
                    print(f"BUDGET STOP trước {conv['id']} turn {ti}: {reason}")
                    budget_hit = True; break
                post_str = f"{pc.get('topic')} ({', '.join(pc.get('vehicles') or [])})"
                call_guard, reservations = _make_hook()
                api = call_luna(post_str, text, parent="", context=ctx_str,
                                budget_guard=call_guard)
                attempts, cost = api.get("attempts", 1), api.get("cost_usd_est")
                total_calls += attempts; total_cost += cost or 0.0
                if cost is None: run_unknown = True
                if attempts:
                    budget_ledger.record(budget_ledger.DEFAULT_PATH, run_id=os.path.basename(args.out),
                                         kind="call", attempts=attempts,
                                         cost_usd_est=cost,
                                         model=api.get("model_effective", MODEL),
                                         status=None if api.get("ok") else
                                         ("failed" if cost is not None else "unknown_cost"))
                    budget_ledger.settle_reservations(budget_ledger.DEFAULT_PATH, reservations)
                if api.get("ok"):
                    sem, model_eff = api["semantic"], api.get("model_effective", MODEL)
                    val_errs = validate_semantic(sem, text, [r["text"] for r in ctx["recent"]] + ctx["parent_texts"])
                    if val_errs:  # 1 lần repair (guard TRƯỚC repair, R14)
                        ok2, reason2 = _guard()
                        if not ok2:
                            print(f"BUDGET STOP trước repair {conv['id']} turn {ti}: {reason2}")
                            budget_hit = True
                        else:
                            repair_guard, reservations = _make_hook()
                            rep = call_luna(post_str, text, parent="", context=ctx_str,
                                            repair_note="; ".join(val_errs), budget_guard=repair_guard)
                            attempts += rep.get("attempts", 1); total_calls += rep.get("attempts", 1)
                            _rc = rep.get("cost_usd_est")
                            cost = (cost or 0.0) + (_rc or 0.0); total_cost += _rc or 0.0
                            if _rc is None: run_unknown = True
                            budget_ledger.record(budget_ledger.DEFAULT_PATH, run_id=os.path.basename(args.out),
                                                 kind="repair", attempts=rep.get("attempts", 1),
                                                 cost_usd_est=rep.get("cost_usd_est"),
                                                 model=rep.get("model_effective", MODEL),
                                                 status=None if rep.get("ok") else "failed")
                            budget_ledger.settle_reservations(budget_ledger.DEFAULT_PATH, reservations)
                            if rep.get("ok"):
                                sem = rep["semantic"]
                                val_errs = validate_semantic(sem, text, [r["text"] for r in ctx["recent"]] + ctx["parent_texts"])
                    json.dump(dict(cache_key_v2=cache_key, semantic=sem, model_effective=model_eff,
                                   validation_errors=val_errs,
                                   state_snapshot=dict(requests=actor_reqs, pendings=full_pends)),
                              open(ck, "w"), ensure_ascii=False, indent=2)
                else:
                    val_errs = [f"api_error: {api.get('error')}"]
            if sem is not None and not val_errs:
                res = eng.process(ev, pc, sem)
            elif sem is not None:
                # Semantic vẫn lỗi sau repair: không process để khỏi làm bẩn
                # state; ghi nhận lượt fail và đi tiếp bằng state thật.
                res = dict(routing=["SKIPPED_INVALID_SEMANTIC"], sends=[], response_text=None,
                           errors=list(val_errs))
            else:
                res = dict(routing=["SKIPPED_API_ERROR"], sends=[], response_text=None, errors=list(val_errs))
            req_after, pend_after, out_after = snapshot(eng, "page_hyundai_sample", actor)
            before_keys = {o["dedupe_key"] for o in out_before}
            added = [o for o in out_after if o["dedupe_key"] not in before_keys]
            turns_out.append(dict(turn=ti, actor=actor, text=text, comment_id=cid,
                                  semantic=sem, validation_errors=val_errs, attempts=attempts,
                                  cost_usd_est=cost, model_effective=model_eff,
                                  open_pendings_before=open_before,
                                  routing=res.get("routing"), response_text=res.get("response_text"),
                                  sends_n=len(res.get("sends", [])),
                                  requests=req_after, pendings=pend_after,
                                  prefs=sorted(eng.store.blocks("page_hyundai_sample", actor)),
                                  ledger_purposes_added=[o["purpose"] for o in added],
                                  errors=res.get("errors", [])))
        out_convos.append(dict(id=conv["id"], group=conv["group"], turns=turns_out))
    run = dict(run_id=os.path.splitext(os.path.basename(args.out))[0], input=args.input,
               model=MODEL, prompt_hash=hashlib.sha256(SYSTEM.encode()).hexdigest()[:16],
               schema_hash=hashlib.sha256(schema.encode()).hexdigest()[:16],
               prior_calls=args.prior_calls, prior_cost_usd=args.prior_cost,
               new_calls=total_calls, new_cost_usd=round(total_cost, 6),
               # Cumulative = ledger baseline lúc bắt đầu + prior + run này
               # (trước đây bỏ qua ledger baseline nên thiếu lịch sử sau
               # khi ledger đã tồn tại).
               cumulative_calls=lc0 + args.prior_calls + total_calls,
               cumulative_cost_usd=round(lcost0 + args.prior_cost + total_cost, 6),
               budget_hit=budget_hit, conversations=out_convos)
    json.dump(run, open(os.path.join(ROOT, args.out), "w"), ensure_ascii=False, indent=2)
    n_turns = sum(len(c["turns"]) for c in out_convos)
    print(f"multiturn: {len(out_convos)} conversations, {n_turns} turns, new_calls={total_calls}, "
          f"new_cost=${total_cost:.5f}, cumulative={run['cumulative_calls']} calls / ${run['cumulative_cost_usd']:.4f}, budget_hit={budget_hit}")

if __name__ == "__main__":
    main()
