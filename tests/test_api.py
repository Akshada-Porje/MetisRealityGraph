from fastapi.testclient import TestClient

from metis.api import app

c = TestClient(app)


def test_health_and_list():
    assert c.get("/healthz").json()["ok"] is True
    ids = [i["id"] for i in c.get("/api/incidents").json()]
    assert len(ids) == 16 and len(set(ids)) == 16


def test_late_evidence_marked_not_visible():
    inc = c.get("/api/incidents/late-0").json()
    assert any(not e["visible_at_incident"] for e in inc["evidence"])


def test_deterministic_mode_works_without_llm_and_abstains_on_conflict():
    r = c.post("/api/investigate", json={"incident_id": "conflict-0", "mode": "deterministic"}).json()
    assert r["abstained"] is True and r["reconciliation"]["contradictions"]


def test_llm_modes_report_503_when_unconfigured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import metis.api as api
    api._client = None
    assert c.post("/api/investigate", json={"incident_id": "late-0", "mode": "raw"}).status_code == 503


def test_unknown_incident_and_bad_mode():
    assert c.get("/api/incidents/nope").status_code == 404
    assert c.post("/api/investigate", json={"incident_id": "late-0", "mode": "x"}).status_code == 422


def test_truth_endpoint():
    assert c.get("/api/incidents/late-0/truth").json()["answerable"] is True


def _write_results(tmp_path, monkeypatch):
    import json
    rows = [
        {"scenario": "late-0", "type": "late_deploy_event", "mode": "raw", "repeat": 0, "outcome": "missed",
         "cause": None, "truth": "late-0/commit:C2", "flags": [], "error": None, "reasoning": "r", "latency_s": 1},
        {"scenario": "late-0", "type": "late_deploy_event", "mode": "reconciled", "repeat": 0, "outcome": "correct",
         "cause": "late-0/commit:C2", "truth": "late-0/commit:C2", "flags": ["validator_abstained:x"],
         "error": None, "reasoning": "r", "latency_s": 1},
    ]
    p = tmp_path / "results.json"
    p.write_text(json.dumps({"model": "m", "rows": rows}))
    monkeypatch.setenv("METIS_RESULTS", str(p))


def test_results_summary_and_recorded_rows(tmp_path, monkeypatch):
    _write_results(tmp_path, monkeypatch)
    r = c.get("/api/results").json()
    assert r["available"] and r["total"]["raw"]["missed"] == 1
    assert r["total"]["reconciled"]["validator_interventions"] == 1
    assert len(c.get("/api/incidents/late-0/recorded").json()["rows"]) == 2
    assert c.get("/api/incidents/conflict-0/recorded").json()["rows"] == []


def test_results_absent_is_graceful(tmp_path, monkeypatch):
    monkeypatch.setenv("METIS_RESULTS", str(tmp_path / "nope.json"))
    assert c.get("/api/results").json() == {"available": False}
    assert c.get("/api/incidents/late-0/recorded").json()["rows"] == []


def test_live_cap_blocks_after_limit(monkeypatch):
    import metis.api as api
    from metis.llm import LLMResult
    class Fake:
        model = "m"
        def generate_json(self, s, u, r=0):
            return LLMResult('{"cause_id": null, "confidence": "low", "reasoning": "x", "cited_evidence_ids": []}',
                             {"cause_id": None, "confidence": "low", "reasoning": "x", "cited_evidence_ids": []}, 1, 1, 0.0)
    monkeypatch.setattr(api, "_client", Fake())
    monkeypatch.setattr(api, "LIVE_CAP", 2)
    api._live.update({"day": "", "n": 0})
    ok = [c.post("/api/investigate", json={"incident_id": "late-0", "mode": "raw"}).status_code for _ in range(3)]
    assert ok == [200, 200, 429]
    api._live.update({"day": "", "n": 0})
    api._client = None


def test_index_page_served():
    r = c.get("/")
    assert r.status_code == 200 and "Metis" in r.text and "textContent" in r.text
