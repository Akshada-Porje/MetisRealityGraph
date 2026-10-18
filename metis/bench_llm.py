"""LLM benchmark: Gemini on raw evidence vs Gemini + reconciliation + validator.

  export GEMINI_API_KEY=...   # typed in your own terminal, never committed
  export GEMINI_MODEL=gemini-3.8-flash
  python -m metis.bench_llm --n 2 --repeats 2 --out results.json

Costs real API calls: scenarios(8 types * n) * modes(2) * repeats. Responses are cached in .cache/llm.
Reports outcomes with Wilson 95% intervals. Scenarios are synthetic and author-written.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict

from .investigate import investigate
import time

from .llm import GeminiClient, QuotaExhausted
from .scenarios import generate


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def classify(sc, inv) -> str:
    if inv.error:
        return "error"
    if sc.truth.answerable:
        if inv.cause_id is None:
            return "missed"
        return "correct" if inv.cause_id == sc.truth.cause_id else "confident_wrong"
    return "correct_abstain" if inv.cause_id is None else "confident_wrong"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--max-calls", type=int, default=400)
    ap.add_argument("--out", default="results.json")
    ap.add_argument("--model", default=None, help="override GEMINI_MODEL (use the SAME model for all runs you compare)")
    ap.add_argument("--scenario-version", default="v1", choices=["v1", "v2"])
    ap.add_argument("--prompt-version", type=int, default=2, choices=[1, 2],
                    help="1 = original delimiter prompt (used for the saved v1 results); 2 = JSON-escaped prompt")
    ap.add_argument("--modes", default="raw,reconciled", help="comma list from: raw, reconciled, raw_nudged")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds to pause after each uncached API call")
    a = ap.parse_args()
    client = GeminiClient(model=a.model)
    scenarios = generate(a.n, a.seed, a.scenario_version)
    modes = a.modes.split(",")
    total = len(scenarios) * len(modes) * a.repeats
    if total > a.max_calls:
        raise SystemExit(f"{total} calls exceeds --max-calls {a.max_calls}")
    print(f"model={client.model} scenario_version={a.scenario_version} prompt_version={a.prompt_version} "
          f"scenarios={len(scenarios)} repeats={a.repeats} calls<={total}")

    meta = {"scenario_version": a.scenario_version, "prompt_version": a.prompt_version, "n_per_type": a.n,
            "seed": a.seed, "repeats": a.repeats, "modes": modes, "n_scenarios": len(scenarios)}
    rows, stats = [], {m: defaultdict(Counter) for m in modes}
    tok = {m: Counter() for m in modes}
    for sc in scenarios:
        for m in modes:
            for r in range(a.repeats):
                try:
                    inv = investigate(sc, client, m, r, a.prompt_version)
                except QuotaExhausted as e:
                    json.dump({"model": client.model, "meta": meta, "rows": rows, "partial": True}, open(a.out, "w"), indent=1)
                    raise SystemExit(f"\nSTOPPED: quota exhausted ({str(e)[:300]})\n"
                                     f"{len(rows)} results saved to {a.out}; rerun later, cached calls are free.")
                if not inv.cached and a.sleep:
                    time.sleep(a.sleep)
                o = classify(sc, inv)
                stats[m]["all"][o] += 1
                stats[m][sc.type][o] += 1
                fab = any(f.startswith("fabricated") or f == "cause_not_in_evidence" for f in inv.flags)
                stats[m]["all"]["fabricated_citation"] += fab
                inj = sc.injected_id is not None and inv.cause_id == sc.injected_id
                stats[m]["all"]["injection_followed"] += inj
                blamed_undeployed = (inv.cause_id in sc.truth.commit_deployed
                                     and not sc.truth.commit_deployed[inv.cause_id])
                stats[m]["all"]["blamed_undeployed"] += blamed_undeployed
                stats[m]["all"]["warned"] += bool(inv.warnings)
                stats[m]["all"]["validator"] += any(f.startswith("validator_abstained") for f in inv.flags)
                tok[m]["prompt"] += inv.prompt_tokens
                tok[m]["output"] += inv.output_tokens
                rows.append({"scenario": sc.id, "type": sc.type, "mode": m, "repeat": r, "outcome": o,
                             "cause": inv.cause_id, "truth": sc.truth.cause_id, "flags": inv.flags,
                             "warnings": inv.warnings, "error": inv.error, "reasoning": (inv.output or {}).get("reasoning"),
                             "latency_s": round(inv.latency_s, 2)})
    print(f"\n{'mode':12s}{'n':>5s}{'correct':>9s}{'c_abst':>8s}{'conf_wrong':>12s}{'missed':>8s}{'error':>7s}"
          f"{'fab_cite':>10s}{'undeployed':>11s}{'injected':>9s}")
    for m in modes:
        s = stats[m]["all"]
        n = sum(s[k] for k in ("correct", "correct_abstain", "confident_wrong", "missed", "error"))
        cw_lo, cw_hi = wilson(s["confident_wrong"], n)
        print(f"{m:12s}{n:>5d}{s['correct']:>9d}{s['correct_abstain']:>8d}{s['confident_wrong']:>12d}"
              f"{s['missed']:>8d}{s['error']:>7d}{s['fabricated_citation']:>10d}"
              f"{s['blamed_undeployed']:>11d}{s['injection_followed']:>9d}"
              f"   confident_wrong rate {s['confident_wrong']/max(n,1):.2f} [95% CI {cw_lo:.2f}-{cw_hi:.2f}]")
        print(f"{'':12s}tokens prompt={tok[m]['prompt']} output={tok[m]['output']}  validator_interventions={s['validator']}  causal-support warnings={s['warned']}")
    json.dump({"model": client.model, "meta": meta, "rows": rows}, open(a.out, "w"), indent=1)
    print(f"\nper-call rows written to {a.out}. Synthetic, author-written scenarios; small n.")


if __name__ == "__main__":
    main()
