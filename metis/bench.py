"""Deterministic benchmark. Usage: python -m metis.bench [--n 5] [--seed 7]"""
from __future__ import annotations

import argparse
from collections import defaultdict

from .models import Scenario, Status
from .reconcile import reconcile
from .scenarios import generate
from .systems import SYSTEMS


def outcome(sc: Scenario, cause_id) -> str:
    if sc.truth.answerable:
        if cause_id is None:
            return "missed"            # abstained but evidence sufficed
        return "correct" if cause_id == sc.truth.cause_id else "confident_wrong"
    return "correct_abstain" if cause_id is None else "confident_wrong"


def run(n: int, seed: int) -> dict:
    scenarios = generate(n, seed)
    res: dict = {"n": len(scenarios), "systems": {}, "status": {}}
    for name, fn in SYSTEMS.items():
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    r = run(a.n, a.seed)
    print(f"{r['n']} synthetic scenarios (seed={a.seed}). Ground truth fixed at generation time.\n")
    cols = ["correct", "correct_abstain", "confident_wrong", "missed"]
    print(f"{'system':22s}" + "".join(f"{c:>17s}" for c in cols))
    for name, d in r["systems"].items():
        print(f"{name:22s}" + "".join(f"{d['total'].get(c, 0):>17d}" for c in cols))
    print("\nper scenario type (correct+correct_abstain / n):")
    types = list(next(iter(r["systems"].values()))["by_type"])
    print(f"{'type':26s}" + "".join(f"{n[:18]:>20s}" for n in r["systems"]))
    for t in types:
        row = f"{t:26s}"
        for d in r["systems"].values():
            bt = d["by_type"][t]
            ok = bt.get("correct", 0) + bt.get("correct_abstain", 0)
            row += f"{ok}/{sum(bt.values())}".rjust(20)
        print(row)
    print("\nR2 per-commit deployed-status (answerable scenarios):", r["status"])


if __name__ == "__main__":
    main()
