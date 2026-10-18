"""FastAPI service. Read-only over synthetic data; never executes remediation."""
from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .investigate import build_prompt, investigate
from .llm import GeminiClient, LLMError
from .reconcile import compare_views, reconcile, visible
from .actions import Action, is_valid, metis_recommend, naive_recommend, score as score_action
from .scenarios import generate
from .systems import r2_reconcile

log = logging.getLogger("metis")
_DOCS = os.environ.get("METIS_DOCS") == "1"          # API docs are off in production unless explicitly enabled
app = FastAPI(title="Metis", version="0.2.0",
              docs_url="/docs" if _DOCS else None, redoc_url=None, openapi_url="/openapi.json" if _DOCS else None)


def _scenario_version() -> str:
    """Show the same scenario set that the recorded results were produced on (results meta), else v1."""
    v = os.environ.get("METIS_SCENARIO_VERSION")
    if v in ("v1", "v2"):
        return v
    try:
        path = Path(os.environ.get("METIS_RESULTS", "data/results.json"))
        meta = json.loads(path.read_text()).get("meta", {}) if path.exists() else {}
        return meta.get("scenario_version") if meta.get("scenario_version") in ("v1", "v2") else "v1"
    except (OSError, ValueError):
        return "v1"


SCENARIO_VERSION = _scenario_version()
SCENARIOS = {s.id: s for s in generate(2, 7, SCENARIO_VERSION)}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # The single-file UI uses inline script/style and makes same-origin requests only.
    resp.headers["Content-Security-Policy"] = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                              "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
    return resp


STATIC = Path(__file__).parent / "static"
_client: GeminiClient | None = None
LIVE_CAP = int(os.environ.get("METIS_LIVE_CAP", "25"))   # live Gemini calls per instance per UTC day
_live = {"day": "", "n": 0}


def _spend_live_call() -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _live["day"] != day:
        _live["day"], _live["n"] = day, 0
    if _live["n"] >= LIVE_CAP:
        raise HTTPException(429, f"Live Gemini calls are capped at {LIVE_CAP}/day on this demo to protect the "
                                 "free quota. Recorded benchmark results are shown for every incident.")
    _live["n"] += 1


def _recorded() -> dict:
    path = Path(os.environ.get("METIS_RESULTS", "data/results.json"))
    if not path.exists():
        return {"available": False, "rows": []}
    d = json.loads(path.read_text())
    d["available"] = True
    return d


def _llm() -> GeminiClient:
    global _client
    if _client is None:
        try:
            _client = GeminiClient(cache_dir=os.environ.get("METIS_CACHE", "/tmp/metis-cache"))
        except LLMError as e:
            raise HTTPException(503, f"LLM unavailable: {e}")
    return _client


def _get(incident_id: str):
    sc = SCENARIOS.get(incident_id)
    if not sc:
        raise HTTPException(404, "unknown incident")
    return sc


@app.get("/api/health")
@app.get("/healthz")
def healthz():
    return {"ok": True, "llm_configured": bool(os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_MODEL"))}


@app.get("/api/incidents")
def incidents():
    return [{"id": s.id, "type": s.type, "service": s.service, "symptom": s.symptom,
             "incident_time": s.incident_time} for s in SCENARIOS.values()]


@app.get("/api/incidents/{incident_id}")
def incident(incident_id: str):
    sc = _get(incident_id)
    vis = {e.id for e in visible(sc.evidence, sc.service, sc.incident_time)}
    ev = [{**jsonable_encoder(e), "visible_at_incident": e.id in vis}
          for e in sorted(sc.evidence, key=lambda e: e.event_time)]
    return {"id": sc.id, "type": sc.type, "symptom": sc.symptom, "incident_time": sc.incident_time,
            "implicated": sc.implicated, "evidence": ev}


@app.get("/api/incidents/{incident_id}/reconciliation")
def reconciliation(incident_id: str, as_of: str | None = None):
    """`changes` etc. describe the contemporaneous view (what an investigator at the time could know).
    `retrospective` and `view_differences` show what changes once late-arriving records are counted.
    Pass `as_of` (ISO-8601) to re-run the reconciliation at a different point in time (time-travel)."""
    sc = _get(incident_id)
    tgt = sc.incident_time
    if as_of:
        try:
            tgt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if tgt.tzinfo is None: tgt = tgt.replace(tzinfo=timezone.utc)
        except Exception:
            raise HTTPException(422, "as_of must be ISO-8601")
    both = compare_views(sc.evidence, sc.service, tgt)
    out = jsonable_encoder(both["contemporaneous"])
    out["retrospective"] = {"statuses": {c.id: c.status.value for c in both["retrospective"].changes},
                            "basis": both["retrospective"].basis, "confidence": both["retrospective"].confidence}
    out["view_differences"] = both["differences"]
    return out


@app.get("/api/incidents/{incident_id}/pipeline")
def pipeline(incident_id: str, mode: Literal["raw", "reconciled"] = "reconciled"):
    """Everything the engine does before the model is called, so the UI can show it step by step.
    The prompt returned is exactly what build_prompt produces for that mode (evidence only; no secrets)."""
    sc = _get(incident_id)
    vis_c = visible(sc.evidence, sc.service, sc.incident_time, "contemporaneous")
    vis_r = visible(sc.evidence, sc.service, sc.incident_time, "retrospective")
    both = compare_views(sc.evidence, sc.service, sc.incident_time)
    rec = both["contemporaneous"]
    system, user, _ = build_prompt(sc, mode)
    cap = 20000
    return {
        "ledger": {"total": len(sc.evidence), "sources": sorted({e.source for e in sc.evidence})},
        "time_gate": {"as_of": sc.incident_time.isoformat(), "visible_contemporaneous": len(vis_c),
                      "visible_retrospective": len(vis_r),
                      "excluded_late_or_future": sorted(e.id for e in sc.evidence if e.id not in {v.id for v in vis_c})},
        "reconstruction": {"history_model": rec.history_model, "active_digests": rec.active_digests,
                           "confidence": rec.confidence, "basis": rec.basis,
                           "changes": [{"id": c.id, "status": c.status.value, "note": c.note} for c in rec.changes]},
        "cross_check": {"contradictions": rec.contradictions, "missing_evidence": rec.missing_evidence,
                        "differences_when_late_records_counted": both["differences"]},
        "prompt": {"mode": mode, "prompt_version": 2, "system_chars": len(system), "user_chars": len(user),
                   "includes_reconciliation": mode == "reconciled",
                   "system": system[:cap], "user": user[:cap], "user_truncated": len(user) > cap},
    }


@app.get("/api/incidents/{incident_id}/recovery")
def recovery(incident_id: str):
    """Recovery-action recommendation and state-validity for naive and Metis.
    See metis.actions for how validity is defined. This never executes anything."""
    sc = _get(incident_id)
    nm, mm = naive_recommend(sc), metis_recommend(sc)
    nr, mr = score_action(sc, nm), score_action(sc, mm)
    return {"scenario": incident_id, "type": sc.type,
            "action_space": [a.value for a in Action],
            "naive": {"action": nr.action.value, "valid": nr.validity.valid, "why": nr.validity.why},
            "metis": {"action": mr.action.value, "valid": mr.validity.valid, "why": mr.validity.why},
            "note": "A valid action is one defined for the reconstructed state. Validity does not mean the action will fix the incident."}


@app.get("/api/recovery_summary")
def recovery_summary(version: str = "v2"):
    """Across the active scenario set, how often each system picks a state-valid action."""
    if version not in ("v1", "v2"):
        raise HTTPException(422, "version must be v1 or v2")
    from .scenarios import generate as _gen
    scs = _gen(2, 7, version)
    naive = {"total": 0, "valid": 0, "by_action": {}}; metis = {"total": 0, "valid": 0, "by_action": {}}
    rows = []
    for sc in scs:
        nm, mm = naive_recommend(sc), metis_recommend(sc)
        nr, mr = score_action(sc, nm), score_action(sc, mm)
        for d, r in ((naive, nr), (metis, mr)):
            d["total"] += 1; d["valid"] += int(r.validity.valid)
            d["by_action"][r.action.value] = d["by_action"].get(r.action.value, 0) + 1
        rows.append({"id": sc.id, "type": sc.type, "naive_valid": nr.validity.valid,
                     "metis_action": mr.action.value, "metis_valid": mr.validity.valid})
    return {"scenario_version": version, "naive": naive, "metis": metis, "rows": rows,
            "note": "Scenarios and ground truth are team-written; small sample; validity != incident resolution."}


class InvestigateReq(BaseModel):
    incident_id: str = Field(max_length=64)
    mode: Literal["deterministic", "raw", "reconciled"] = "reconciled"


@app.post("/api/investigate")
def run_investigation(req: InvestigateReq):
    sc = _get(req.incident_id)
    if req.mode == "deterministic":
        p = r2_reconcile(sc)
        return {"mode": "deterministic", "cause_id": p.cause_id, "abstained": p.cause_id is None,
                "reason": p.reason, "evidence_ids": p.evidence_ids,
                "reconciliation": jsonable_encoder(reconcile(sc.evidence, sc.service, sc.incident_time))}
    client = _llm()
    _spend_live_call()
    inv = investigate(sc, client, req.mode)
    if inv.error:
        log.warning("investigation failed for %s (%s): %s", sc.id, req.mode, inv.error[:500])   # detail stays server-side
        kind = "rate_limited" if "429" in inv.error else "unparseable_output" if "unparseable" in inv.error else "unavailable"
        inv.error = f"The model call failed ({kind}). Recorded results are still available."
    return jsonable_encoder(inv)


@app.get("/api/results")
def results():
    d = _recorded()
    if not d["available"]:
        return {"available": False}
    total, by_type = defaultdict(Counter), defaultdict(lambda: defaultdict(Counter))
    for r in d["rows"]:
        total[r["mode"]][r["outcome"]] += 1
        by_type[r["type"]][r["mode"]][r["outcome"]] += 1
        for f in r.get("flags", []):
            if f.startswith("validator_abstained"):
                total[r["mode"]]["validator_interventions"] += 1
    return {"available": True, "model": d.get("model"), "partial": d.get("partial", False),
            "total": total, "by_type": by_type}


@app.get("/api/incidents/{incident_id}/recorded")
def recorded(incident_id: str):
    _get(incident_id)
    return {"model": _recorded().get("model"),
            "rows": [r for r in _recorded()["rows"] if r["scenario"] == incident_id]}


@app.get("/api/incidents/{incident_id}/truth")
def truth(incident_id: str):
    """Ground-truth reveal for the demo. Never fed to any investigator."""
    return jsonable_encoder(_get(incident_id).truth)


@app.get("/")
def index():
    f = STATIC / "index.html"
    if not f.exists():
        return {"service": "metis", "docs": "/docs"}
    return FileResponse(f)
