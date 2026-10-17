"""FastAPI service. Read-only over synthetic data; never executes remediation."""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .investigate import investigate
from .llm import GeminiClient, LLMError
from .reconcile import reconcile, visible
from .scenarios import generate
from .systems import r2_reconcile

app = FastAPI(title="Metis", version="0.1.0")
SCENARIOS = {s.id: s for s in generate(2, 7)}
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
def reconciliation(incident_id: str):
    sc = _get(incident_id)
    return jsonable_encoder(reconcile(sc.evidence, sc.service, sc.incident_time))


class InvestigateReq(BaseModel):
    incident_id: str
    mode: str = "reconciled"   # deterministic | raw | reconciled


@app.post("/api/investigate")
def run_investigation(req: InvestigateReq):
    sc = _get(req.incident_id)
    if req.mode == "deterministic":
        p = r2_reconcile(sc)
        return {"mode": "deterministic", "cause_id": p.cause_id, "abstained": p.cause_id is None,
                "reason": p.reason, "evidence_ids": p.evidence_ids,
                "reconciliation": jsonable_encoder(reconcile(sc.evidence, sc.service, sc.incident_time))}
    if req.mode not in ("raw", "reconciled"):
        raise HTTPException(422, "mode must be deterministic, raw or reconciled")
    client = _llm()
    _spend_live_call()
    inv = investigate(sc, client, req.mode)
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
