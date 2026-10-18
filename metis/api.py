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

from .investigate import investigate
from .llm import GeminiClient, LLMError
from .reconcile import compare_views, reconcile, visible
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
def reconciliation(incident_id: str):
    """`changes` etc. describe the contemporaneous view (what an investigator at the time could know).
    `retrospective` and `view_differences` show what changes once late-arriving records are counted."""
    sc = _get(incident_id)
    both = compare_views(sc.evidence, sc.service, sc.incident_time)
    out = jsonable_encoder(both["contemporaneous"])
    out["retrospective"] = {"statuses": {c.id: c.status.value for c in both["retrospective"].changes},
                            "basis": both["retrospective"].basis, "confidence": both["retrospective"].confidence}
    out["view_differences"] = both["differences"]
    return out


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
