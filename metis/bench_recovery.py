"""Recovery-action validity benchmark. Prints the % of state-valid actions for naive vs metis.
Usage: python -m metis.bench_recovery [--version v1|v2]
"""
import argparse, json
from .actions import metis_recommend, naive_recommend, score
from .scenarios import generate

def run(version="v2", n_per_type=2, seed=7):
    scs = generate(n_per_type, seed, version)
    totals = {"naive": {"n": 0, "valid": 0}, "metis": {"n": 0, "valid": 0}}
    rows = []
    for sc in scs:
        for name, pick in (("naive", naive_recommend), ("metis", metis_recommend)):
            r = score(sc, pick(sc))
            totals[name]["n"] += 1
            totals[name]["valid"] += int(r.validity.valid)
            rows.append({"system": name, "scenario": sc.id, "type": sc.type,
                         "action": r.action.value, "valid": r.validity.valid, "why": r.validity.why})
    return {"version": version, "n": len(scs), "totals": totals, "rows": rows}

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--version", default="v2"); ap.add_argument("--json", action="store_true"); ap.add_argument("--out")
    args = ap.parse_args()
    r = run(args.version)
    if args.out: open(args.out, "w").write(json.dumps(r, indent=2))
    if args.json: print(json.dumps(r, indent=2))
    else:
        print(f"Scenario set {args.version} ({r['n']} scenarios)\n")
        print(f"{'system':10}  {'valid':>12}  {'pct':>6}")
        for k, v in r["totals"].items():
            pct = 100 * v["valid"] / v["n"] if v["n"] else 0
            print(f"{k:10}  {v['valid']:>5}/{v['n']:>5}  {pct:>5.1f}%")
        print("\nNaive always picks rollback_latest_commit (R2Act straw-man).")
        print("Metis picks a state-defined action from the reconciled view.")
        print("Validity = action is defined for the reconstructed state; it does NOT mean the action will fix the incident.")
