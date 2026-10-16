from datetime import timedelta

from metis.bench import outcome, run
from metis.models import Evidence, Status
from metis.reconcile import reconcile
from metis.scenarios import generate
from metis.systems import SYSTEMS, r2_reconcile


def _by_type(t):
    return [s for s in generate(5, 7) if s.type == t]


def _status(rec, sc, label):
    cid = f"{sc.id}/commit:{label}"
    return next(c.status for c in rec.changes if c.id == cid)


def test_committed_but_undeployed_is_not_deployed():
    for sc in _by_type("undeployed_fix"):
        rec = reconcile(sc.evidence, sc.service, sc.incident_time)
        assert _status(rec, sc, "C2") == Status.NOT_DEPLOYED
        assert _status(rec, sc, "C1") == Status.DEPLOYED


def test_late_deploy_event_resolved_from_runtime():
    for sc in _by_type("late_deploy_event"):
        rec = reconcile(sc.evidence, sc.service, sc.incident_time)
        assert _status(rec, sc, "C2") == Status.DEPLOYED
        assert any("missing or late" in rec.basis for _ in [0])


def test_unresolvable_conflict_is_reported_and_abstained():
    for sc in _by_type("conflict_unresolvable"):
        rec = reconcile(sc.evidence, sc.service, sc.incident_time)
        assert rec.contradictions and rec.contradictions[0]["type"] == "active_version_conflict"
        assert _status(rec, sc, "C2") == Status.UNKNOWN
        assert r2_reconcile(sc).cause_id is None


def test_failed_rollout_resolves_conflict():
    for sc in _by_type("conflict_resolvable"):
        rec = reconcile(sc.evidence, sc.service, sc.incident_time)
        assert not rec.contradictions
        assert _status(rec, sc, "C2") == Status.NOT_DEPLOYED


def test_config_change_without_deploy_is_flagged():
    for sc in _by_type("config_without_deploy"):
        rec = reconcile(sc.evidence, sc.service, sc.incident_time)
        assert any(c["type"] == "config_change_without_deploy" for c in rec.contradictions)


def test_no_evidence_means_unknown_never_guess():
    for sc in _by_type("insufficient_evidence"):
        rec = reconcile(sc.evidence, sc.service, sc.incident_time)
        assert rec.confidence == "none"
        assert all(c.status == Status.UNKNOWN for c in rec.changes)


def test_evidence_ingested_after_incident_is_invisible():
    sc = _by_type("late_deploy_event")[0]
    late = [e for e in sc.evidence if e.kind == "deploy" and e.ingest_time > sc.incident_time]
    assert late, "scenario must contain a late-arriving deploy record"
    rec = reconcile(sc.evidence, sc.service, sc.incident_time)
    assert all(late[0].id not in c.evidence_ids for c in rec.changes)


def test_future_events_invisible():
    sc = _by_type("undeployed_fix")[0]
    future = Evidence(f"{sc.id}/future", "git", "commit", sc.service,
                      sc.incident_time + timedelta(hours=1), sc.incident_time + timedelta(hours=1, minutes=1),
                      {"sha": "CF", "touches": ["pricing.py"]})
    rec = reconcile(sc.evidence + [future], sc.service, sc.incident_time)
    assert future.id not in [c.id for c in rec.changes]


def test_every_prediction_cites_existing_evidence():
    for sc in generate(5, 7):
        ids = {e.id for e in sc.evidence}
        pred = r2_reconcile(sc)
        assert set(pred.evidence_ids) <= ids


def test_ground_truth_ids_exist_and_benchmark_deterministic():
    for sc in generate(3, 1):
        if sc.truth.answerable:
            assert sc.truth.cause_id in {e.id for e in sc.evidence}
    assert run(3, 1) == run(3, 1)


def test_r2_never_confidently_wrong_on_designed_scenarios():
    # Regression guard only: these scenarios were designed alongside the rules (see README caveat).
    for sc in generate(5, 7):
        assert outcome(sc, SYSTEMS["R2 reconcile"](sc).cause_id) in ("correct", "correct_abstain")
