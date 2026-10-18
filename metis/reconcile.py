"""Deterministic incident-time reconciliation. No LLM involved.

QUESTION ANSWERED: for `service` at time `as_of`, which changes were live, using which evidence,
and where do records disagree?

TWO EVIDENCE VIEWS (they answer different questions; never conflate them)
  contemporaneous : only records with event_time <= as_of AND ingest_time <= as_of.
                    "What could an investigator acting at the incident time have known?"
                    This is the view Gemini receives.
  retrospective   : all records with event_time <= as_of, even if ingested after as_of.
                    "What do we now know about the system state at the incident time?"
Future-dated evidence (event_time > as_of) is excluded from BOTH views. A probe taken after the
incident is not used as evidence of the state at the incident (documented limitation).
Statuses are relative to the chosen view. In particular NOT_DEPLOYED requires positive evidence
(no artifact contains the change, a failed/superseded deploy, or runtime confirmation).
A change whose deploy record has merely not arrived yet is UNKNOWN, never NOT_DEPLOYED.
Equal timestamps are included (<=).

SUPPORTED HISTORY MODELS
  dag             every visible commit records `parents` (list of shas). Merges and branches are handled by
                  ancestor traversal; an artifact contains exactly the ancestors of its build commit.
  linear_assumed  no commit records `parents`: a linear history is ASSUMED (labelled in the output).
  unsupported     parent links only partly present, duplicate shas, or a missing parent record: every
                  change is UNKNOWN.
Reverts (`reverts: sha`) and cherry-picks (`cherry_picked_from: sha`) are NOT modelled: involved commits
become UNKNOWN rather than guessed. Partial rollouts report both versions as possibly serving.

RULES (each is tested)
 1. A direct runtime observation of digest X proves X was running, even if the deploy log lacks it.
 2. A runtime observation of an OLDER digest after a newer logged deploy is a conflict, not a tie-break.
 3. A deploy the orchestrator reports as failed/rolled back is not effective.
 4. If nothing establishes the live version, every commit is UNKNOWN. Never guess.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import ChangeStatus, Evidence, Reconciliation, Status

RUNTIME_MAX_AGE = timedelta(hours=6)
VIEWS = ("contemporaneous", "retrospective")
PARTIAL = ("partial", "in_progress", "canary")


def visible(evidence: list[Evidence], service: str, as_of: datetime, view: str = "contemporaneous") -> list[Evidence]:
    if view not in VIEWS:
        raise ValueError(f"view must be one of {VIEWS}")
    return [
        e for e in evidence
        if e.service == service and e.event_time <= as_of
        and (view == "retrospective" or e.ingest_time <= as_of)
    ]


def late_records(evidence: list[Evidence], service: str, as_of: datetime) -> list[str]:
    return sorted(e.id for e in evidence
                  if e.service == service and e.event_time <= as_of and e.ingest_time > as_of)


class _History:
    """Maps a commit sha to the set of commit evidence ids contained in an artifact built from it."""

    def __init__(self, commits: list[Evidence]):
        self.commits = commits
        self.by_sha = {c.data["sha"]: c for c in commits}
        self.problems: list[str] = []
        flags = ["parents" in c.data for c in commits]
        if not commits:
            self.model = "linear_assumed"
        elif len(self.by_sha) != len(commits):
            self.model = "unsupported"
            self.problems.append("duplicate commit sha in the evidence")
        elif all(flags):
            self.model = "dag"
            for c in commits:
                ps = c.data["parents"]
                if not isinstance(ps, list) or any(p not in self.by_sha for p in ps):
                    self.model = "unsupported"
                    self.problems.append(f"parent commit record missing for {c.data['sha']}")
                    break
        elif any(flags):
            self.model = "unsupported"
            self.problems.append("only some commits record parent links")
        else:
            self.model = "linear_assumed"
        self._order = {c.id: i for i, c in enumerate(commits)}

    def contained(self, sha: str) -> set[str] | None:
        if sha not in self.by_sha or self.model == "unsupported":
            return None
        if self.model == "linear_assumed":
            return {c.id for c in self.commits[: self._order[self.by_sha[sha].id] + 1]}
        seen, stack = set(), [sha]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.by_sha[cur].data["parents"])
        return {self.by_sha[x].id for x in seen}


def reconcile(evidence: list[Evidence], service: str, as_of: datetime, view: str = "contemporaneous") -> Reconciliation:
    vis = visible(evidence, service, as_of, view)
    commits = sorted((e for e in vis if e.kind == "commit"), key=lambda e: (e.event_time, e.id))
    hist = _History(commits)

    builds: dict[str, list[Evidence]] = {}
    for e in vis:
        if e.kind == "build":
            builds.setdefault(e.data["digest"], []).append(e)
    contradictions: list[dict] = []
    missing: list[str] = list(hist.problems)
    ambiguous_digests = set()
    for d, bs in builds.items():
        if len({b.data["sha"] for b in bs}) > 1:
            ambiguous_digests.add(d)
            contradictions.append({"type": "build_identity_conflict", "records": [b.id for b in bs], "digest": d})

    rollouts = [e for e in vis if e.kind == "rollout"]
    failed_ids = {e.data["deploy_id"] for e in rollouts if e.data.get("status") in ("failed", "rolled_back")}
    partial_ids = {e.data["deploy_id"] for e in rollouts if e.data.get("status") in PARTIAL}
    all_deploys = [e for e in vis if e.kind == "deploy"]
    deploys = sorted((e for e in all_deploys if e.data.get("status") == "success" and e.data["deploy_id"] not in failed_ids),
                     key=lambda e: (e.event_time, e.id))
    runtime = sorted((e for e in vis if e.kind == "runtime_version"), key=lambda e: (e.event_time, e.id))

    def live_ids(digest: str) -> set[str] | None:
        if digest in ambiguous_digests or digest not in builds:
            return None
        return hist.contained(builds[digest][0].data["sha"])

    claimed = deploys[-1] if deploys else None
    previous = deploys[-2] if len(deploys) > 1 else None
    obs = runtime[-1] if runtime and as_of - runtime[-1].event_time <= RUNTIME_MAX_AGE else None
    if obs and claimed and obs.data["digest"] != claimed.data["digest"] and obs.event_time < claimed.event_time:
        obs = None  # a probe older than the latest effective deploy says nothing about that deploy

    active: list[str] = []
    ev_ids: list[str] = []
    if claimed and claimed.data["deploy_id"] in partial_ids:
        active = [claimed.data["digest"]] + ([previous.data["digest"]] if previous else [])
        confidence, basis = "low", "PARTIAL ROLLOUT: both versions may be serving"
        ev_ids = [claimed.id] + [r.id for r in rollouts if r.data["deploy_id"] == claimed.data["deploy_id"]]
        contradictions.append({"type": "partial_rollout", "records": ev_ids})
    elif claimed and obs:
        c, r = claimed.data["digest"], obs.data["digest"]
        ev_ids = [claimed.id, obs.id]
        cset, rset = live_ids(c), live_ids(r)
        if c == r:
            active, confidence, basis = [c], "high", "deploy_log+runtime agree"
        elif rset is not None and (cset is None or rset > cset):
            active, confidence = [r], "medium"
            basis = "runtime observed a newer digest than the deploy log knows (deploy record missing or late)"
            missing.append("deploy record for the digest observed at runtime")
        else:
            active, confidence = [c, r], "low"
            basis = "CONFLICT: deploy log and runtime disagree"
            contradictions.append({"type": "active_version_conflict", "records": [claimed.id, obs.id],
                                   "deploy_log_digest": c, "runtime_digest": r})
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

    live_sets: list[set[str]] = []
    resolvable = bool(active)
    for d in active:
        ids = live_ids(d)
        if ids is None:
            resolvable = False
            missing.append(f"unambiguous build record linking digest {d} to a known commit")
        else:
            live_sets.append(ids)

    confirmed_by_runtime = basis == "deploy_log+runtime agree"

    def not_live(c: Evidence) -> tuple[Status, str]:
        """NOT_DEPLOYED only on positive evidence; otherwise UNKNOWN (absence of a record is not evidence)."""
        arts = []
        for d, bs in builds.items():
            ids = live_ids(d)
            if ids is None:
                return Status.UNKNOWN, f"cannot tell whether artifact {d} contains it"
            if c.id in ids:
                arts.append((d, bs[0]))
        if not arts:
            return Status.NOT_DEPLOYED, "no build artifact includes it"
        uncovered = []
        for d, b in arts:
            # Any deploy record (failed, or successful then superseded) is positive evidence about this artifact.
            if not [x for x in all_deploys if x.data["digest"] == d]:
                uncovered.append(b)
        if not uncovered:
            return Status.NOT_DEPLOYED, "its artifact failed to roll out or was superseded by a later deploy"
        if confirmed_by_runtime and obs is not None and all(obs.event_time >= b.event_time for b in uncovered):
            return Status.NOT_DEPLOYED, "runtime confirmed a different artifact after its build"
        return Status.UNKNOWN, "an artifact containing it was built but no deploy record is visible; the deploy log may lag"

    changes: list[ChangeStatus] = []
    hist_note = " (assumes linear history)" if hist.model == "linear_assumed" else ""
    for c in commits:
        if not resolvable:
            status, note = Status.UNKNOWN, "live version could not be established"
        elif all(c.id in s for s in live_sets):
            status, note = Status.DEPLOYED, basis + hist_note
        elif not any(c.id in s for s in live_sets):
            status, note = not_live(c)
        else:
            status, note = Status.UNKNOWN, "live only under one side of a conflict"
        changes.append(ChangeStatus(c.id, "commit", status, list(c.data.get("touches", [])), c.event_time,
                                    ev_ids, note))

    # Reverts and cherry-picks are not modelled: involved commits become UNKNOWN instead of being guessed.
    by_id = {x.id: x for x in changes}
    sha_id = {c.data["sha"]: c.id for c in commits}
    for c in commits:
        me = by_id[c.id]
        src = c.data.get("cherry_picked_from")
        if src in sha_id and by_id[sha_id[src]].status == Status.NOT_DEPLOYED and me.status in (Status.DEPLOYED, Status.UNKNOWN):
            by_id[sha_id[src]].status = Status.UNKNOWN
            by_id[sha_id[src]].note = f"its content may be live via cherry-pick {c.id}; equivalence not verified"
            missing.append("cherry-pick semantics are not modelled")
        tgt = c.data.get("reverts")
        if tgt in sha_id:
            other = by_id[sha_id[tgt]]
            if me.status in (Status.DEPLOYED, Status.UNKNOWN) and other.status in (Status.DEPLOYED, Status.UNKNOWN):
                for x in (me, other):
                    x.status, x.note = Status.UNKNOWN, f"revert relationship with {c.id if x is other else tgt}: effect not modelled"
                missing.append("revert semantics are not modelled")

    for e in vis:
        if e.kind == "config_change":
            attributed = bool(e.data.get("deploy_id"))
            changes.append(ChangeStatus(
                e.id, "config_change", Status.APPLIED, [e.data["key"]], e.event_time, [e.id],
                "orchestrator audit log shows applied" + ("" if attributed else "; NO matching deploy record"),
            ))
            if not attributed:
                contradictions.append({"type": "config_change_without_deploy", "records": [e.id]})

    return Reconciliation(service, as_of, active, confidence, basis, contradictions, sorted(set(missing)), changes,
                          view=view, late_records=late_records(evidence, service, as_of), history_model=hist.model)


def compare_views(evidence: list[Evidence], service: str, as_of: datetime) -> dict:
    """Both views plus the changes whose status differs once late-arriving records are counted."""
    cont = reconcile(evidence, service, as_of, "contemporaneous")
    retro = reconcile(evidence, service, as_of, "retrospective")
    c_by = {c.id: c for c in cont.changes}
    diffs = []
    for r in retro.changes:
        c = c_by.get(r.id)
        if c is None or c.status != r.status:
            diffs.append({"change_id": r.id, "contemporaneous": c.status.value if c else "NOT_VISIBLE",
                          "retrospective": r.status.value, "late_records": retro.late_records})
    return {"contemporaneous": cont, "retrospective": retro, "differences": diffs}
