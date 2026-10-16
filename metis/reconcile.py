"""Deterministic incident-time reconciliation. No LLM involved.

Question answered: for `service` at time `as_of`, which changes were actually live,
using only evidence the system had ingested by `as_of`, and where do records disagree?

Design rules (each is tested):
 1. Evidence ingested after `as_of`, or describing events after `as_of`, is invisible.
 2. A direct runtime observation of digest X proves X was running (so X was deployed),
    even if the deploy log has no record of it (late / missing deploy event).
 3. A runtime observation of an OLDER digest after a newer logged deploy is a conflict, not
    a tie-break: it could be a rollback, canary, or stale replica. We report UNKNOWN.
 4. A deploy that the orchestrator reports as failed/rolled back is not effective.
 5. If nothing establishes the live version, every commit is UNKNOWN. Never guess.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import ChangeStatus, Evidence, Reconciliation, Status

RUNTIME_MAX_AGE = timedelta(hours=6)


def visible(evidence: list[Evidence], service: str, as_of: datetime) -> list[Evidence]:
    return [
        e for e in evidence
        if e.service == service and e.ingest_time <= as_of and e.event_time <= as_of
    ]


def reconcile(evidence: list[Evidence], service: str, as_of: datetime) -> Reconciliation:
    vis = visible(evidence, service, as_of)
    commits = sorted((e for e in vis if e.kind == "commit"), key=lambda e: e.event_time)
    commit_index = {c.data["sha"]: i for i, c in enumerate(commits)}
    builds = {e.data["digest"]: e for e in vis if e.kind == "build"}
    failed_deploys = {
        e.data["deploy_id"] for e in vis
        if e.kind == "rollout" and e.data.get("status") in ("failed", "rolled_back")
    }
    deploys = sorted(
        (e for e in vis if e.kind == "deploy" and e.data.get("status") == "success"
         and e.data["deploy_id"] not in failed_deploys),
        key=lambda e: e.event_time,
    )
    runtime = sorted((e for e in vis if e.kind == "runtime_version"), key=lambda e: e.event_time)

    contradictions: list[dict] = []
    missing: list[str] = []

    def build_order(digest: str) -> int | None:
        b = builds.get(digest)
        return commit_index.get(b.data["sha"]) if b else None

    claimed = deploys[-1] if deploys else None
    obs = runtime[-1] if runtime and as_of - runtime[-1].event_time <= RUNTIME_MAX_AGE else None
    # Rule: a probe older than the latest effective deploy says nothing about that deploy.
    if obs and claimed and obs.data["digest"] != claimed.data["digest"] \
            and obs.event_time < claimed.event_time:
        obs = None

    active: list[str] = []
    ev_ids: list[str] = []
    if claimed and obs:
        c, r = claimed.data["digest"], obs.data["digest"]
        ev_ids = [claimed.id, obs.id]
        if c == r:
            active, confidence, basis = [c], "high", "deploy_log+runtime agree"
        else:
            oc, orr = build_order(c), build_order(r)
            if orr is not None and (oc is None or orr > oc):
                active, confidence = [r], "medium"
                basis = "runtime observed a newer digest than the deploy log knows (deploy record missing or late)"
                missing.append("deploy record for the digest observed at runtime")
            else:
                active, confidence = [c, r], "low"
                basis = "CONFLICT: deploy log and runtime disagree"
                contradictions.append({
                    "type": "active_version_conflict",
                    "records": [claimed.id, obs.id],
                    "deploy_log_digest": c, "runtime_digest": r,
                })
    elif claimed:
        active, confidence, basis = [claimed.data["digest"]], "medium", "deploy log only; no runtime confirmation"
        ev_ids = [claimed.id]
        missing.append("runtime version probe within %dh of incident" % (RUNTIME_MAX_AGE.total_seconds() // 3600))
    elif obs:
        active, confidence, basis = [obs.data["digest"]], "medium", "runtime observation only; no deploy record"
        ev_ids = [obs.id]
        missing.append("deploy record for the running digest")
    else:
        confidence, basis = "none", "no deploy or runtime evidence establishes the live version"
        missing += ["deploy record", "runtime version probe"]

    # Map each candidate digest to the set of commits it contains (linear history assumption).
    live_sets: list[set[str]] = []
    resolvable = bool(active)
    for d in active:
        order = build_order(d)
        if order is None:
            resolvable = False
            missing.append(f"build record linking digest {d} to a commit")
            continue
        live_sets.append({commits[i].id for i in range(order + 1)})
    if len(live_sets) != len(active):
        resolvable = False

    changes: list[ChangeStatus] = []
    for c in commits:
        if not resolvable:
            status, note = Status.UNKNOWN, "live version could not be established"
        elif all(c.id in s for s in live_sets):
            status, note = Status.DEPLOYED, basis
        elif not any(c.id in s for s in live_sets):
            status, note = Status.NOT_DEPLOYED, "not contained in the live artifact"
        else:
            status, note = Status.UNKNOWN, "live only under one side of a conflict"
        changes.append(ChangeStatus(c.id, "commit", status, list(c.data.get("touches", [])),
                                    c.event_time, ev_ids, note))

    for e in vis:
        if e.kind == "config_change":
            attributed = bool(e.data.get("deploy_id"))
            changes.append(ChangeStatus(
                e.id, "config_change", Status.APPLIED, [e.data["key"]], e.event_time, [e.id],
                "orchestrator audit log shows applied" + ("" if attributed else "; NO matching deploy record"),
            ))
            if not attributed:
                contradictions.append({"type": "config_change_without_deploy", "records": [e.id]})

    return Reconciliation(service, as_of, active, confidence, basis, contradictions, missing, changes)
