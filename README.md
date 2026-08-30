# Gooo Design Contract Bridge

This repository is a small closed-loop example that connects a released design-token semantic graph to a CSS custom-property fixture through one Gooo contract.

The GitHub Actions workflow is the execution authority. It reads the canonical token source, the CSS fixture, and the contract, then writes caller-owned outputs:

- `token-mapping.json` — deterministic exact mappings and scenario verdicts.
- `dossier.md` — a human-readable evidence dossier.
- `actions.json` — execution, inventory, policy, and stage accounting.

The workflow also records the immutable GitHub Actions artifact ID, name, digest, and expected file count in the run summary. The `main` branch requires the `bridge` check.

The fixed semantic denominator is 12 cells: proof phases `FOUNDATION/COHERENCE/REGRESSION` are `4/4/4`, and indicators `DRIVER/OUTCOME/GUARDRAIL` are `4/4/4`. The evaluator preserves `CLOSED`, `UNKNOWN`, and `REFUTED`; an explicit contradiction wins over missing-source uncertainty as `REFUTED_OVER_UNKNOWN`.

No local test, build, or formatter execution is part of the contract. The Actions run is the only verification authority.
