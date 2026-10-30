# Persona conversation evaluation

`frozen12.json` (`frozen12-synthetic-v2`) is a regression set of 12 synthetic conversations, each with four customer turns. It contains expected behavior and known unsupported claims, not model replies or human scores. All cases are labeled `SYNTHETIC FIXTURE`. Each turn's `required_fact_keys` and `required_memory_topics` identify supporting evidence the snapshot must make available; tests fail if that evidence is inactive, revoked, or blocked by the frozen memory state. The actor-isolation case seeds a synthetic actor-A message before actor B asks about prior context.

`collection_status.json` records that zero real conversations have been collected, the 30–50 conversation target is missing, and no human evaluation has been run. Do not change those counts until consented, redacted conversations and actual human labels exist.

## Commands

Validate the frozen set and check collection status:

```sh
python3 scripts/evaluate_persona_conversations.py validate
python3 scripts/evaluate_persona_conversations.py status
```

The real transcript importer requires a source reference, capture date, collector, granted evaluation consent, and completed privacy redaction review. It rejects common direct identifiers and writes only redacted customer turns; source references, consent IDs, and original assistant replies are omitted. Files use mode `0600`. Keep source transcripts and importer outputs outside the repository.

```sh
python3 scripts/evaluate_persona_conversations.py import-real raw.json --output /private/tmp/persona-real-redacted.json
```

Evaluation run files contain `run_id`, `model_version`, `prompt_version`, and `results`. Each result has a `case_id` and a `turns` list with matching `turn_id`, `response`, `latency_ms`, `cost_usd`, `fallback`, `review`, and `attempts` fields. Metrics must come from the actual run; missing costs or attempts are not filled with defaults.

Create the blind pack, private unblinding key, and empty two-rater label template. The random seed is generated automatically and kept in the private key; pass `--seed` to reproduce an assignment. Real redacted cases are excluded unless `--include-real` and `--real-dataset` are both supplied.

```sh
python3 scripts/evaluate_persona_conversations.py export-ab old-run.json new-run.json \
  --output /private/tmp/persona-blind.json \
  --key-output /private/tmp/persona-blind-key.json
```

Each evaluator should use a distinct `labeler_id`, attest `independence_attested=true`, score all five criteria from 1 to 5 for both A and B, and mark the three issue flags true or false. The aggregator reports incomplete labels as incomplete and does not infer human evaluation results.

```sh
python3 scripts/evaluate_persona_conversations.py aggregate \
  /private/tmp/persona-blind.json /private/tmp/persona-blind-key.json \
  /private/tmp/persona-blind.labels.csv --output /private/tmp/persona-evaluation-summary.json
```
