# Responsive specialist implementation plan

**Goal:** Make the single-user sandbox more responsive and useful while retaining the intent engine's evidence and policy checks. Automotive remains the supported domain; future domains require their own taxonomy, facts, policy and evaluation.

**Constraints:** Preserve the working tree, existing guardrails and caller contracts. Keep the shared sandbox lock unchanged. No provider calls without the configured budget and user authorization; no invented facts, human labels, or model performance claims.

## Tasks

- [x] Instrument post generation and comment processing with elapsed time, model usage and retry counts; surface measurements without logging raw comments or secrets.
- [x] Remove duplicate prior messages only when actor scope is verified; retain post, branch, pending and evidence. Further context reduction requires measurement.
- [x] Reduce the default requested length of generated posts; preserve persona and groundedness. Actual latency improvement remains unmeasured.
- [x] Surface waiting progress and measured duration in the sandbox UI.
- [x] Handle the existing color-preference question as subjective advice without inventing stock or treating advice as a purchase confirmation.
- [x] Verify relevant tests and the complete offline suite; build the local sandbox image. Report actual results and limits.
- [ ] Measure Luna and Jev on the same authorized human-labeled holdout before enabling a decision fast path or claiming accuracy or speed improvements.
- [ ] Extract domain boundaries only alongside a specified second domain with its own schemas, verified facts, policy and evaluations; avoid advertising unimplemented domains.
