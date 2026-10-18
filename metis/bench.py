"""Deterministic benchmark. Usage: python -m metis.bench [--n 5] [--seed 7]"""
from __future__ import annotations

import argparse
from collections import defaultdict

from .models import Scenario, Status
from .reconcile import reconcile
from .scenarios import generate
from .systems import SYSTEMS_STRUCTURED, SYSTEMS_TEXT


def outcome(sc: Scenario, cause_id) -> str:
    if sc.truth.answerable:
        if cause_id is None:
            return "missed"            # abstained but evidence sufficed
        return "correct" if cause_id == sc.truth.cause_id else "confident_wrong"
    return "correct_abstain" if cause_id is None else "confident_wrong"


def run(n: int, seed: int, version: str = "v1", input_mode: str = "structured") -> dict:
    scenarios = generate(n, seed, version)
    systems = SYSTEMS_STRUCTURED if input_mode == "structured" else SYSTEMS_TEXT
    res: dict = {"n": len(scenarios), "version": version, "input": input_mode, "systems": {}, "status": {}}
    for name, fn in systems.items():
        by_type = defaultdict(lambda: defaultdict(int))
        tot = defaultdict(int)
        for sc in scenarios:
            o = outcome(sc, fn(sc).cause_id)
            by_type[sc.type][o] += 1
            tot[o] += 1
        res["systems"][name] = {"total": dict(tot), "by_type": {k: dict(v) for k, v in by_type.items()}}
    # Per-commit deployed-status accuracy of R2 on answerable scenarios.
    s = defaultdict(int)
    for sc in scenarios:
        if not sc.truth.answerable:
            continue
        rec = reconcile(sc.evidence, sc.service, sc.incident_time)
        for c in rec.changes:
            if c.kind != "commit" or c.id not in sc.truth.commit_deployed:
                continue
            if c.status == Status.UNKNOWN:
                s["unknown"] += 1
            elif (c.status == Status.DEPLOYED) == sc.truth.commit_deployed[c.id]:
                s["correct"] += 1
            else:
                s["wrong"] += 1
    res["status"] = dict(s)
    return res


def _print(r: dict, seed: int) -> None:
    label = {"structured": "STRUCTURED input (oracle `implicated` field; Gemini never gets this)",
             "text": "SYMPTOM TEXT only (information-equivalent to the Gemini prompt)"}[r["input"]]
    print(f"\n=== {r['n']} scenarios, {r['version']}, seed={seed}; baselines receive: {label}")
    cols = ["correct", "correct_abstain", "confident_wrong", "missed"]
    print(f"{'system':22s}" + "".join(f"{c:>17s}" for c in cols))
    for name, d in r["systems"].items():
        print(f"{name:22s}" + "".join(f"{d['total'].get(c, 0):>17d}" for c in cols))
    print("\nper scenario type (correct+correct_abstain / n):")
    types = list(next(iter(r["systems"].values()))["by_type"])
    print(f"{'type':28s}" + "".join(f"{n[:18]:>20s}" for n in r["systems"]))
    for t in types:
        row = f"{t:28s}"
        for d in r["systems"].values():
            bt = d["by_type"][t]
            ok = bt.get("correct", 0) + bt.get("correct_abstain", 0)
            row += f"{ok}/{sum(bt.values())}".rjust(20)
        print(row)
    print("R2 per-commit deployed-status (answerable scenarios):", r["status"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--version", default="v1", choices=["v1", "v2"])
    ap.add_argument("--input", default="both", choices=["structured", "text", "both"])
    a = ap.parse_args()
    for mode in (["structured", "text"] if a.input == "both" else [a.input]):
        _print(run(a.n, a.seed, a.version, mode), a.seed)
    print("\nAll scenarios and labels are team-authored; the deterministic layer was developed alongside them.")


if __name__ == "__main__":
    main()
