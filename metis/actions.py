"""Recovery-action validity, inspired by R2Act (arXiv 2607.04623).

R2Act reports that recovery-oriented methods pick invalid actions for 39.5% to 62% of
correctly diagnosed Kubernetes incidents. "Invalid" means the action is not defined for
the actual state of the system - e.g., recommending a rollback when nothing was deployed.

We measure the same thing on our scenario set with a small, bounded action space.

Action set (read-only; nothing is executed):
    rollback_latest_commit       undo the latest change
    deploy_pending_change        ship a committed-but-not-yet-deployed change
    revert_config                undo an applied config change
    no_action_safe               take no action; the system is in a safe or neutral state
    request_more_evidence        abstain; evidence is contradictory, late, or missing

Validity is a function of the reconstructed state at incident time, NOT ground truth.
An action is `valid` iff it is defined for the state. `no_action_safe` and
`request_more_evidence` are always defined. The others require specific state.

We score two systems:
    NAIVE    always picks rollback_latest_commit (R2Act's straw-man).
    METIS    uses the deterministic reconciliation to pick an action it can prove valid.

Metis is not expected to always "fix" the incident; this is not a causality claim.
The claim is: Metis picks STATE-VALID actions much more often than a naive recommender.

Labels and the ground-truth action per scenario type are team-written. See README.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import Scenario, Status
from .reconcile import reconcile


class Action(str, Enum):
    ROLLBACK_LATEST_COMMIT = "rollback_latest_commit"
    DEPLOY_PENDING_CHANGE = "deploy_pending_change"
    REVERT_CONFIG = "revert_config"
    NO_ACTION = "no_action_safe"
    REQUEST_MORE_EVIDENCE = "request_more_evidence"


@dataclass
class Validity:
    valid: bool
    why: str


@dataclass
class ActionResult:
    action: Action
    validity: Validity
    rationale: str


def _signals(sc: Scenario):
    """Pull out the state facts the recommender and the validator both use."""
    rec = reconcile(sc.evidence, sc.service, sc.incident_time)
    statuses = {c.id: c.status for c in rec.changes}
    deployed = [k for k, s in statuses.items() if s == Status.DEPLOYED]
    not_deployed = [k for k, s in statuses.items() if s == Status.NOT_DEPLOYED]
    unknown = [k for k, s in statuses.items() if s == Status.UNKNOWN]
    applied = [k for k, s in statuses.items() if s == Status.APPLIED]
    deploy_attempts = {}
    for e in sc.evidence:
        if e.kind == "deploy":
            deploy_attempts.setdefault(e.data.get("digest"), []).append(e.data.get("status", "unknown"))
    pending_already_tried = []
    for change_id in not_deployed:
        # Look for a deploy event whose digest came from a build for this commit
        sha = change_id.split(":")[-1]
        for e in sc.evidence:
            if e.kind == "build" and e.data.get("sha") == sha:
                digest = e.data.get("digest")
                if digest in deploy_attempts:
                    pending_already_tried.append(change_id)
                    break
    return rec, deployed, not_deployed, unknown, applied, pending_already_tried


def is_valid(sc: Scenario, action: Action) -> Validity:
    """State-validity check. Always safe; never calls the model."""
    rec, deployed, not_deployed, unknown, applied, tried = _signals(sc)
    conflict = any(c["type"] in ("active_version_conflict", "partial_rollout", "build_identity_conflict")
                   for c in rec.contradictions)
    late = bool(rec.late_records)

    if action in (Action.NO_ACTION, Action.REQUEST_MORE_EVIDENCE):
        return Validity(True, "always defined; takes no risk with production")

    if action == Action.ROLLBACK_LATEST_COMMIT:
        if conflict:
            return Validity(False, "live version is ambiguous (contradicting records); a rollback could land on either side")
        if late:
            return Validity(False, "a record arrived after the incident; what is live is not yet settled")
        if unknown:
            return Validity(False, f"{len(unknown)} change(s) have UNKNOWN status; rollback target is undefined")
        if not deployed:
            return Validity(False, "nothing is DEPLOYED; there is no live version to roll back from")
        if rec.history_model == "unsupported":
            return Validity(False, "git history is unsupported here; prior-version pointer is not reliable")
        return Validity(True, f"{len(deployed)} change(s) cleanly DEPLOYED; prior version is well-defined")

    if action == Action.DEPLOY_PENDING_CHANGE:
        if not not_deployed:
            return Validity(False, "no NOT_DEPLOYED change to ship")
        if conflict or late:
            return Validity(False, "live state is unsettled; shipping now could clash with an in-flight change")
        pending_fresh = [c for c in not_deployed if c not in tried]
        if not pending_fresh:
            return Validity(False, "the only pending change already had a deploy that did not succeed; retrying blindly is not safe")
        return Validity(True, f"a pending change ({pending_fresh[0].split(':')[-1]}) has not been attempted yet")

    if action == Action.REVERT_CONFIG:
        if not applied:
            return Validity(False, "no APPLIED config change to revert")
        return Validity(True, f"an APPLIED config change ({applied[0].split(':')[-1]}) exists")

    return Validity(False, "unknown action")


def naive_recommend(sc: Scenario) -> Action:
    """R2Act straw-man: always rollback the latest change."""
    return Action.ROLLBACK_LATEST_COMMIT


def metis_recommend(sc: Scenario) -> Action:
    """Pick the action with the strongest state justification. Deterministic; no model."""
    rec, deployed, not_deployed, unknown, applied, tried = _signals(sc)
    conflict = any(c["type"] in ("active_version_conflict", "partial_rollout", "build_identity_conflict")
                   for c in rec.contradictions)
    late = bool(rec.late_records)

    # Any ambiguity about what is live → ask.
    if conflict or late or unknown or rec.history_model == "unsupported":
        return Action.REQUEST_MORE_EVIDENCE

    # Config-only change: a code rollback would not undo it.
    if applied and not not_deployed:
        return Action.REVERT_CONFIG

    # A fresh pending change exists → shipping it is defined.
    pending_fresh = [c for c in not_deployed if c not in tried]
    if pending_fresh and not deployed:
        return Action.REQUEST_MORE_EVIDENCE  # nothing live to compare against
    if pending_fresh:
        return Action.DEPLOY_PENDING_CHANGE

    # Everything clean, nothing pending: a rollback is state-defined.
    # We do not claim it will fix the incident; the claim is only that it is defined.
    if deployed and not not_deployed:
        return Action.ROLLBACK_LATEST_COMMIT

    return Action.NO_ACTION


def score(sc: Scenario, action: Action) -> ActionResult:
    v = is_valid(sc, action)
    return ActionResult(action=action, validity=v, rationale=v.why)
