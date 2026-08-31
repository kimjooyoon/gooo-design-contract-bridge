# Gooo Design Contract Bridge

This repository is a small closed user path: the authoritative `.gooo` design
declarations are checked by a released Gooo binary, projected into tokens and
CSS custom properties, and consumed by an independent reader. A separate
evaluator binds each source activity to its semantic-IR activity, generated
artifact, consumer receipt, and evaluator claim before it can close a claim.
The evaluator compares source intent with consumer-observed role and state;
visual similarity or file existence alone is not evidence of closure.

GitHub Actions is the only verification authority. It pins Gooo `v0.4.0-dev`
by release and asset identity, observes Go `1.27.x`, writes only to caller-owned
temporary output, and uploads the resulting evidence bundle. The `bridge` job
is the existing repository check; this workflow is not a cross-project required
gate.

The product-owned denominator is exactly 12 cells. `FOUNDATION/COHERENCE/REGRESSION`
and `DRIVER/OUTCOME/GUARDRAIL` are each `4/4/4`, with one source activity, one
semantic-IR activity, and one evaluator claim per cell. Reader resolutions are
separate `EXACT`, `ROLE`, and `EXISTENCE` paths. UNKNOWN preserves
`stage`, `step`, `reason`, `unknown_class`, `next_operation`, `blocked_by`, and
its causal frontier. Contradictions win as `REFUTED_OVER_UNKNOWN`; malformed
UNKNOWN, `FIXED_POINT`, and authority escalation fail closed.

The Actions artifact reports exact generated/consumer artifact counts and
bytes, descendant directories, regular files, Go/Gooo physical lines with the
root README excluded, wall time, peak RSS, stage reuse, and zero repository
writes/local test executions. External utility and released adoption remain
`0/1 UNKNOWN` until external user evidence or a product release exists. No
improvement claim is made without an exact comparable before/after pair.
