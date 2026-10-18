"""Non-LLM systems under test. R2 is the reconciliation system; R0 and R1 are simple baselines.

INPUT FAIRNESS. Each system takes a `matcher` that decides which changes relate to the symptom:
  structured_matcher : uses the scenario's structured `implicated` field (files / config keys).
                       This is an ORACLE the Gemini modes never receive. Reported as "structured input".
  text_matcher       : uses ONLY the free-text symptom (lexical token overlap with a change's touched
                       files / config key). This is the information-equivalent baseline for Gemini.
Neither matcher reads commit messages, so they remain simple lexical baselines, not a semantic reader.
"""
from __future__ import annotations

import re

from .models import Prediction, Scenario, Status
from .reconcile import reconcile, visible

STOP = {"py", "js", "ts", "go", "java", "the", "a", "an", "in", "on", "of", "from", "to", "and", "is", "are",
        "with", "for", "at", "by", "500s", "null", "mode", "on"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in STOP}


def _touched(ev) -> list[str]:
    return list(ev.data.get("touches", [])) or [ev.data.get("key", "")]


def structured_matcher(sc: Scenario):
    imp = set(sc.implicated)
    return lambda ev: bool(set(_touched(ev)) & imp)


def text_matcher(sc: Scenario):
    sym = _tokens(sc.symptom)          # note: never reads sc.implicated
    return lambda ev: bool(sym & set().union(*[_tokens(t) for t in _touched(ev)]))


def r0_latest_change(sc: Scenario, matcher=structured_matcher) -> Prediction:
    """Most recent visible change (commit or config) matching the symptom."""
    m = matcher(sc)
    vis = [e for e in visible(sc.evidence, sc.service, sc.incident_time) if e.kind in ("commit", "config_change")]
    hits = sorted((e for e in vis if m(e)), key=lambda e: e.event_time)
    if hits:
        return Prediction(hits[-1].id, "latest change matching the symptom", [hits[-1].id])
    return Prediction(None, "no change matches the symptom")


def r1_deploy_log_only(sc: Scenario, matcher=structured_matcher) -> Prediction:
    """Trust the deploy log alone: commits in the latest logged deploy that match the symptom."""
    m = matcher(sc)
    vis = visible(sc.evidence, sc.service, sc.incident_time)
    deploys = sorted((e for e in vis if e.kind == "deploy" and e.data.get("status") == "success"),
                     key=lambda e: e.event_time)
    if not deploys:
        return Prediction(None, "no deploy record")
    builds = {e.data["digest"]: e for e in vis if e.kind == "build"}
    commits = sorted((e for e in vis if e.kind == "commit"), key=lambda e: e.event_time)
    b = builds.get(deploys[-1].data["digest"])
    if not b:
        return Prediction(None, "deploy has no build record")
    idx = next((i for i, c in enumerate(commits) if c.data["sha"] == b.data["sha"]), None)
    if idx is None:
        return Prediction(None, "build commit not visible")
    hits = [c for c in commits[: idx + 1] if m(c)]
    return Prediction(hits[-1].id, "latest live commit per deploy log", [deploys[-1].id]) if hits \
        else Prediction(None, "no live commit matches the symptom")


def r2_reconcile(sc: Scenario, matcher=structured_matcher) -> Prediction:
    m = matcher(sc)
    rec = reconcile(sc.evidence, sc.service, sc.incident_time)
    ev_by_id = {e.id: e for e in sc.evidence}
    cands = [c for c in rec.changes
             if c.status in (Status.DEPLOYED, Status.APPLIED, Status.UNKNOWN) and m(ev_by_id[c.id])]
    confirmed = sorted((c for c in cands if c.status != Status.UNKNOWN), key=lambda c: c.event_time)
    unknown = [c for c in cands if c.status == Status.UNKNOWN]
    newest_confirmed = confirmed[-1].event_time if confirmed else None
    # Abstain if an UNKNOWN change is newer than every confirmed one: it might be the cause.
    if unknown and (newest_confirmed is None or max(u.event_time for u in unknown) > newest_confirmed):
        return Prediction(None, f"abstain: {rec.basis}; missing: {', '.join(rec.missing_evidence) or 'n/a'}",
                          [i for c in unknown for i in c.evidence_ids])
    if confirmed:
        top = confirmed[-1]
        return Prediction(top.id, f"{top.status.value} change matching the symptom ({rec.basis})",
                          [top.id, *top.evidence_ids])
    return Prediction(None, "abstain: no live change matches the symptom")


def _bind(fn, matcher):
    return lambda sc: fn(sc, matcher)


SYSTEMS = {"R0 latest-change": r0_latest_change,
           "R1 deploy-log-only": r1_deploy_log_only,
           "R2 reconcile": r2_reconcile}                      # structured (oracle) input; kept for compatibility
SYSTEMS_STRUCTURED = SYSTEMS
SYSTEMS_TEXT = {k: _bind(v, text_matcher) for k, v in SYSTEMS.items()}   # symptom text only
