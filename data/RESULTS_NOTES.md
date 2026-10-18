# Result provenance

- `results.json` — ORIGINAL Gemini run: scenario set v1, prompt v1 (`<evidence>` delimiters), model gemini-3.8-flash, 2 repeats, 96 runs. No `meta` block (pre-dates it). Preserved unmodified.
- `bench_det_v1.txt`, `bench_det_v2.txt` — deterministic baselines (no LLM), n=2/type, seed 7, both input modes. Regenerate with `python -m metis.bench --n 2 --version vN --input both`.
- Gemini run on scenario set v2 + prompt v2: NOT YET RUN. Command: `python -m metis.bench_llm --scenario-version v2 --prompt-version 2 --out data/results_v2.json` (needs GEMINI_API_KEY).
- Scenario labels are team-authored; v2 was written after the review and the deterministic layer was developed alongside it.
