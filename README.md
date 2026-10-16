# Metis — evidence reconciliation for incident investigation

Practical Intelligence. Engineered.

Status: **deterministic core only.** No Gemini layer, API, UI, or deployment yet.

## Run
    pip install pytest
    python -m pytest -q
    python -m metis.bench --n 5 --seed 7

## What exists
- `metis/reconcile.py`  incident-time reconstruction from a temporal evidence ledger (event time vs ingest time), conflict detection, UNKNOWN instead of guessing.
- `metis/scenarios.py`  6 synthetic scenario types with ground truth fixed at generation time.
- `metis/systems.py`    R0 latest-change, R1 deploy-log-only, R2 reconcile.
- `metis/bench.py`      outcomes: correct / correct_abstain / confident_wrong / missed.

## Honest limits
- Scenarios and rules were written together. R2 scoring 30/30 on them is a regression check,
  NOT evidence of generality. The meaningful tests are (a) held-out scenarios written without
  looking at the rules and (b) the LLM comparison: Gemini on raw evidence vs Gemini + reconciliation.
- Linear git history only; no merges, cherry-picks, or multi-artifact services.
- Synthetic data only; no real incidents.
