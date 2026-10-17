# Metis RealityGraph: evidence-first production incident reconstruction

*Practical Intelligence. Engineered.*

Team Metis, Google Cloud AI Builder Cup 2026 (theme: Future of Work & Enterprise Productivity).

## Problem

During a production incident, evidence from source control, deployment records, configuration, database schemas and runtime telemetry can be incomplete, delayed or contradictory. A diagnosis system that treats its system model as fully accurate may draw conclusions from a version or dependency that was not actually active.

Recent research shows why this matters downstream: in the R2Act benchmark (Microsoft Research, 302 Kubernetes incidents, arXiv 2607.04623), recovery-oriented methods still chose invalid actions for 39.5% to 62.0% of correctly diagnosed incidents, even when both the root-cause service and fault type were correct.

## Solution

A temporal evidence engine that reconstructs system state at incident time, detects inconsistencies between evidence sources, and gives Gemini a traceable, uncertainty-aware evidence set for investigation. Gemini may abstain when the evidence is insufficient or contradictory.

**Research hypothesis:** explicit temporal reconstruction and cross-source contradiction detection can reduce incorrect incident-time assumptions and unsupported root-cause claims, compared with an otherwise equivalent baseline investigator.

**Verdict on our benchmark:** not shown. The baseline also made zero wrong or unsupported claims. What changed was over-abstention (5 missed answers down to 0). We did **not** measure recovery-action validity, which is the R2Act question; that is our main future test.

**Live demo:** PASTE LIVE URL HERE
**Synthetic data only. Read-only. Nothing is ever executed.**

## How it works

1. **Evidence ledger** (`metis/models.py`): every record keeps source, event time and ingest time.
2. **Reconciliation** (`metis/reconcile.py`): deterministic code decides which changes were live at incident time (`DEPLOYED`, `NOT_DEPLOYED`, `UNKNOWN`, `APPLIED`), reports contradictions and missing evidence, and ignores anything ingested after the incident.
3. **Gemini investigator** (`metis/investigate.py`): returns structured JSON that cites record ids, or abstains.
4. **Validator**: rejects answers that cite unknown records, cite nothing, or blame a change the reconciliation shows was not live or is unresolved.
5. **API and UI** (`metis/api.py`, `metis/static/index.html`): FastAPI service and a one-page ledger view.

Evidence text (commit messages, notes) is treated as untrusted data. One scenario contains a commit message that tries to hijack the investigator.

## Benchmark (what we measured)

16 synthetic scenarios (8 types x 2), each run twice per mode, model `gemini-3.8-flash`. 24 runs per mode are answerable, 8 are not (the correct output is to abstain).

| Mode | Correct cause (of 24) | Correct abstain (of 8) | Wrong attribution | Missed (abstained, evidence sufficed) |
|---|---|---|---|---|
| Gemini, raw evidence | 19 | 8 | 0 | 5 |
| Gemini, raw + same decisiveness instruction | 20 | 8 | 0 | 4 |
| Gemini + reconciliation + validator | 24 | 8 | 0 | 0 |

Across all 96 Gemini runs: 0 wrong attributions, 0 fabricated citations, 0 blame on undeployed changes, 0 followed the injected instruction. Reconciliation used about 24% more input tokens. Raw mode scored 18 correct in an earlier full run and 19 in this one.

Reproduce (needs your own key; costs real API calls):

    pip install -r requirements-dev.txt
    python -m pytest -q
    python -m metis.bench                      # deterministic baselines, no key needed
    export GEMINI_API_KEY=...   GEMINI_MODEL=gemini-3.8-flash
    python -m metis.smoke                      # one call to confirm key and model
    python -m metis.bench_llm --n 2 --repeats 2 --modes raw,reconciled,raw_nudged --sleep 2 --out results.json
    python -m metis.report results.json

Raw results are committed in `data/results.json` and shown in the app as recorded runs.

## Limitations (please read)

- **We wrote the scenarios and the ground truth.** Two scenario types (`conflict_resolvable`, `unrelated_recent_change`) have debatable labels: in the cases raw Gemini missed, its reasoning about what was deployed was correct and it declined to attribute because the commit text did not clearly match the symptom. That is defensible.
- **Small sample.** Repeats are not independent. 24/24 versus 19/24 is suggestive, not proof. We did not isolate the mechanism of the difference.
- **The deterministic layer scores 100% on scenarios designed alongside it.** That shows the code does what it was designed to do, not that it generalises. Its baseline comparison also receives a structured `implicated` field that the Gemini modes do not.
- **The validator never fired on real Gemini output.** It is tested only with unit tests and a fake client, so we claim no prevented errors.
- A well-prompted Gemini already avoided the dangerous failures we tested. We did not show that reconciliation reduces misattribution.
- Linear git history only; one model; synthetic data; no real incidents; no measurement of investigation time.

## Prior art

Related work we reviewed (not exhaustive; no patent search was done): ESRO (arXiv 2309.07230), PRAXIS (2512.22113), Auditable Graph-Guided RCA for Kubernetes (2606.08590), R2Act (2607.04623), DBA-Bench (2607.22165), FaulT-Bench (2608.27021), and the products Causely, CodeKarma and Pacto. They cover dependency graphs, deploy correlation, evidence graphs and verdict validation. Our narrow focus is explicit reconciliation of commit, build, deploy and runtime records at incident time, with late-arrival awareness and abstention. The reviewed sources suggest this specific combination may be less covered, but we have not established that no one has done it.

## Run the app

    uvicorn metis.api:app --reload      # http://localhost:8000
    # Gemini buttons need GEMINI_API_KEY and GEMINI_MODEL; the deterministic mode and recorded results do not.
    # Live Gemini calls are capped per instance per day (METIS_LIVE_CAP, default 25).

## Deploy (Cloud Run)

    gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
    echo -n "$GEMINI_API_KEY" | gcloud secrets create gemini-key --data-file=-
    gcloud run deploy metis --source . --region asia-south1 --allow-unauthenticated \
      --set-secrets GEMINI_API_KEY=gemini-key:latest \
      --set-env-vars GEMINI_MODEL=gemini-3.8-flash,METIS_LIVE_CAP=25

## Provenance

All code in this repository was written during the hackathon build window. Commit history is public.
