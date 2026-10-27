# Persona-aware Facebook Comment Flow

## Goal

Test a complete comment-reply path in the local sandbox. Given a Page post, a root comment and a current comment, understand their meaning, choose a fitting persona from `content_ai_fac.public.personas`, and draft a reply using that persona's `tone` and `perspective`. Reuse the root analysis when a user replies to that root. Keep Meta delivery out of scope; the sandbox remains local and uses `FakeSender`.

## Existing boundary

- `app/adapters/luna.py` already sends the post, parent/root text, recent context and current comment to Luna, and parses the existing automotive intent schema.
- `app/engine.py` produces fact-aware template replies and protects them with claim checks. `app/renderer.py` currently ignores its persona argument.
- `sandbox/server.py` provides a local UI, keeps conversations in memory and uses `FakeSender`; it does not have a separate root-comment analysis record.
- `app/pg_adapter.py` is a placeholder. `content_ai_fac` holds the 200 persona rows; `social_FB_crawl` holds posts/comments. Those are separate databases. No writes have been made.

## Proposed flow

1. Read the persona fields (`id`, `name`, `category`, `car_owned`, `occupation`, `role`, `tone`, `perspective`) from `content_ai_fac` using a read-only connection.
2. Treat the first customer comment in a sandbox conversation as the root. Build four field-specific query texts from the post and root comment (discussion category, vehicle relationship, useful occupation perspective, useful role). This lets persona matching happen before Luna's existing intent call. Luna then receives the selected persona and extracts intent plus root semantic/tags. Unknown personal attributes stay unknown; do not claim the user owns a car or has a particular occupation.
3. Embed the four query texts and the corresponding persona fields with `qwen/qwen3-embedding-0.6b` through OpenRouter. Cache persona vectors in the running process. Score all 200 candidates exactly in Python; use equal-weight Reciprocal Rank Fusion across the four field rankings (`sum(1 / (60 + rank))`). Ties resolve by category rank, then persona ID. Missing persona fields are omitted and the remaining axes are renormalized.
4. The Engine currently dispatches its template reply inside `Engine.process`. Add an optional, sandbox-only pre-dispatch response rewriter: it receives the factual draft and selected persona `tone`/`perspective`, returns styled wording, then the existing claim/vehicle checks run again before `FakeSender`. If rewriting fails or the checks fail, do not fake-send the styled reply; show the review error.
5. Persist a companion root-analysis record in `social_FB_crawl`, keyed by root comment ID: root text/post ID, tags and semantic JSON, selected persona ID plus a persona snapshot, reply text, and model/schema versions. Do not alter crawler-owned comment rows. A child reply loads this record and sends its root text, semantic/tags, selected persona and prior reply together with the post and current child comment. The root's persona stays stable through that thread.
6. Keep manually entered sandbox examples under synthetic IDs with a source marker so they are distinguishable from crawled comments. No Meta API calls or real delivery.

## Technology choice and alternatives

- **Recommended for this data size:** four field-specific embeddings, exact in-process scoring over 200 rows, and equal-weight RRF. This gives an inspectable score for every criterion without an ANN service. The database currently has no `pgvector` extension, so this does not require a database extension or a new vector service.
- **Embedding model:** start with Qwen3 Embedding 0.6B as a multilingual pilot; its model card lists 100+ supported languages. OpenRouter accepts a list of strings at its embeddings endpoint, so the four query vectors can be batched. This is a starting choice, not a claim that it is the most accurate for Vietnamese; compare it on labeled examples before tuning or switching.
- **Qdrant:** named vectors fit separate persona fields, but add a service. Qdrant's “multivectors” are matrices/late-interaction representations, a different mechanism from four independent persona attributes. At 200 rows neither is needed for retrieval speed.
- **pgvector:** keeps vectors in Postgres and offers exact and approximate search, but the extension is unavailable in the current database and approximate indexing has no practical benefit at 200 rows.
- **LLM-only selection:** send persona candidates to Luna and ask it to choose. This avoids embedding code but repeats a large candidate prompt and gives less reproducible per-field evidence.
- **Structured keyword scoring:** cheapest and easiest to explain, but weak when comment language and persona attributes use different wording. It is a useful fallback if embeddings fail.

## Acceptance checks

- Read the 200 personas from the configured database without changing `personas`.
- For test cases with known expected personas, expose the four per-field ranks and fused winner in the UI; verify that the output does not claim user attributes that the comment never stated.
- A root comment is analyzed and persisted once; a child reply demonstrably receives the saved root semantic, tags, persona and prior reply.
- The final draft reflects `tone` and `perspective`, passes the existing fact/claim checks after rewriting, and is only written to the fake ledger.
- Database/model failures fail closed with a visible error; no real Facebook write occurs.
- Add an additive SQL migration for the companion table. Apply it only to the sandbox/integration database during verification; leave crawler tables and the persona table untouched.

## Research references

- [OpenRouter embeddings API](https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings)
- [Qwen3 Embedding 0.6B model card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- [Qdrant named vectors and multivectors](https://qdrant.tech/documentation/manage-data/vectors/)
- [pgvector exact and approximate search](https://github.com/pgvector/pgvector)
