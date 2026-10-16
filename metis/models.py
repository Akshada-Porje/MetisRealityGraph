"""Core domain types. Plain dataclasses: no framework needed for the reconciliation core."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Status(str, Enum):
    DEPLOYED = "DEPLOYED"          # evidence says this change was live at incident time
    NOT_DEPLOYED = "NOT_DEPLOYED"  # evidence says it was NOT live
    UNKNOWN = "UNKNOWN"            # evidence insufficient or contradictory
    APPLIED = "APPLIED"            # runtime config change recorded by the orchestrator audit log


@dataclass(frozen=True)
class Evidence:
    """One record in the temporal evidence ledger.

    event_time  : when the thing happened in the world
    ingest_time : when our system learned about it (late events have ingest > event)
    """
    id: str
    source: str   # git | ci | deploy_log | orchestrator | runtime | k8s_audit
    kind: str     # commit | build | deploy | rollout | runtime_version | config_change
    service: str
    event_time: datetime
    ingest_time: datetime
    data: dict = field(default_factory=dict)


@dataclass
class ChangeStatus:
    id: str                  # evidence id of the commit / config change
    kind: str                # commit | config_change
    status: Status
    touches: list[str]
    event_time: datetime
    evidence_ids: list[str]  # provenance: records that justify the status
    note: str = ""


@dataclass
class Reconciliation:
    service: str
    as_of: datetime
    active_digests: list[str]
    confidence: str                      # high | medium | low | none
    basis: str                           # how the active version was established
    contradictions: list[dict]
    missing_evidence: list[str]
    changes: list[ChangeStatus]


@dataclass
class Truth:
    cause_id: str | None          # None when the right answer is "insufficient evidence"
    answerable: bool
    commit_deployed: dict[str, bool]  # commit evidence id -> truly live at incident time


@dataclass
class Scenario:
    id: str
    type: str
    service: str
    incident_time: datetime
    implicated: list[str]         # files / config keys named by the symptom
    symptom: str
    evidence: list[Evidence]
    truth: Truth


@dataclass
class Prediction:
    cause_id: str | None          # None = abstain
    reason: str
    evidence_ids: list[str] = field(default_factory=list)
