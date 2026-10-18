"""Summarise a results.json: python -m metis.report [results.json]"""
import json
import sys
from collections import Counter, defaultdict

d = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "results.json"))
rows = d["rows"]
meta = d.get("meta", {"scenario_version": "v1 (no meta recorded)", "prompt_version": "1 (no meta recorded)"})
print("model:", d.get("model"), "| rows:", len(rows), "| partial:", d.get("partial", False), "| meta:", meta)
by = defaultdict(lambda: defaultdict(Counter))
for r in rows:
    by[r["type"]][r["mode"]][r["outcome"]] += 1
print(f"\n{'scenario type':26s}{'mode':12s}  outcomes")
for t, m in by.items():
    for mode, c in m.items():
        print(f"{t:26s}{mode:12s}  {dict(c)}")
iv = Counter(r["mode"] for r in rows for f in r.get("flags", []) if f.startswith("validator_abstained"))
print("\nvalidator interventions:", dict(iv) or "none")
print("\nraw-mode answers that abstained when the evidence sufficed ('missed'):")
for r in rows:
    if r["mode"] == "raw" and r["outcome"] == "missed":
        print(f"- {r['scenario']} r{r['repeat']}: {(r.get('reasoning') or '')[:200]}")
