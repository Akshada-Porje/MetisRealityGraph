"""Non-LLM systems under test. R2 is the reconciliation system; R0 and R1 are fair simple baselines."""
from __future__ import annotations

from .models import Prediction, Scenario, Status
from .reconcile import reconcile, visible


def _touches(ev, implicated) -> bool:
    return bool(set(ev.data.get("touches", [ev.data.get("key")])) & set(implicated))


def r0_latest_change(sc: Scenario) -> Prediction:
    """Most recent visible change (commit or config) touching the implicated component."""
    vis = [e for e in visible(sc.evidence, sc.service, sc.incident_time)
           if e.kind in ("commit", "config_change")]
    hits = sorted((e for e in vis if _touches(e, sc.implicated)), key=lambda e: e.event_time)
    if hits:
        return Prediction(hits[-1].id, "latest change touching implicated component", [hits[-1].id])
    return Prediction(None, "no change touches the implicated component")


def r1_deploy_log_only(sc: Scenario) -> Prediction:
    """Trust the deploy log alone: commits in the latest logged deploy that touch the implicated component."""
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
    idx = next(i for i, c in enumerate(commits) if c.data["sha"] == b.data["sha"])
    hits = [c for c in commits[: idx + 1] if _touches(c, sc.implicated)]
    return Prediction(hits[-1].id, "latest live commit per deploy log", [deploys[-1].id]) if hits \
        else Prediction(None, "no live commit touches the implicated component")


def r2_reconcile(sc: Scenario) -> Prediction:
    rec = reconcile(sc.evidence, sc.service, sc.incident_time)
    cands = [c for c in rec.changes
             if c.status in (Status.DEPLOYED, Status.APPLIED, Status.UNKNOWN)
             and set(c.touches) & set(sc.implicated)]
    confirmed = sorted((c for c in cands if c.status != Status.UNKNOWN), key=lambda c: c.event_time)
    unknown = [c for c in cands if c.status == Status.UNKNOWN]
    newest_confirmed = confirmed[-1].event_time if confirmed else None
    # Abstain if an UNKNOWN change is newer than every confirmed one: it might be the cause.
    if unknown and (newest_confirmed is None or max(u.event_time for u in unknown) > newest_confirmed):
        return Prediction(None, f"abstain: {rec.basis}; missing: {', '.join(rec.missing_evidence) or 'n/a'}",
                          [i for c in unknown for i in c.evidence_ids])
    if confirmed:
        top = confirmed[-1]
        return Prediction(top.id, f"{top.status.value} change touching implicated component ({rec.basis})",
                          [top.id, *top.evidence_ids])
    return Prediction(None, "abstain: no live change touches the implicated component")


SYSTEMS = {"R0 latest-change": r0_latest_change,
           "R1 deploy-log-only": r1_deploy_log_only,
           "R2 reconcile": r2_reconcile}
