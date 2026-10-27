# MAT_Intent production design

## Intent and goals

Upgrade the existing Vietnamese intent engine so explicit slots are evidence-grounded, inference state is isolated by page/post/thread/actor, and state changes remain safe under concurrent processing. Preserve the public `Engine.process(event, postcard, semantic)` call shape and keep channel integrations out of the business engine.

The engine will return versioned semantic data, operations with provenance, policy outcomes, state snapshots/changes, and review reasons. Luna remains the extraction baseline. Jev 1.14 is a separately configured typed-decision adapter for shadow/eligible simple cases, never a replacement for open slot extraction.

## Non-goals

- No automatic production rollout or outbound enablement.
- No invented human labels and no claim that synthetic fixtures are human gold.
- No changes to existing spend limits or user data.
- No rewrite of the existing policy/state machine or new channel integration.

## Approaches

1. **Extend the existing seams (chosen):** strengthen `app/semantics.py`, `Engine.context/process`, `Store`, model adapters, and eval scripts. This preserves caller contracts and keeps policy authoritative.
2. Replace the engine with a new framework/service layer. Rejected because the current policy and durable outbox already encode important safety behavior.
3. Fix the prompt only. Rejected because prompts cannot enforce slot evidence, thread ownership, schema boundaries, or concurrent commits.

## Design

Use Pydantic v2 for application validation and publish full versioned JSON Schema contracts for structured output. Keep semantic v1 readable; introduce v2 for explicit multi-label/category/tag fields. Add deterministic evidence checks per slot kind and make unsupported values route to REVIEW without state mutation.

Store request scope as page, post, root thread, and actor. Parent/root messages remain readable context with author identity, while request slots and pending decisions stay attached to the scoped actor/request. Capture a state/context token before inference and reject stale semantic results at commit. PostgreSQL is a separate store implementation with versioned migrations and optimistic updates; SQLite remains for existing tests and callers.

Cache fingerprints include complete logical state and all prompt/schema/model/policy inputs. Model adapters return model/routing/usage/latency metadata; Jev shadow work does not block Luna. Fast path and Jev shadow default off pending labeled evaluation. Budget reservations are atomic and fail closed when cost cannot be verified.

## Constraints and evaluation

All model-call tests are offline unless the user explicitly requests a run and the local ledger permits it. PostgreSQL claims require a real disposable PostgreSQL integration run. Jev quality claims require the same human-labeled holdout as Luna; the current human-gold set is empty. Preserve the 9 known contract discrepancies unless evidence shows the policy is wrong; document any contract migration instead of changing expected behavior to inflate the score.
