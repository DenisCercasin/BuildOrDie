"""Optional HTTP wrapper so the engine can run as its own service.

Person 4 can either import ``DecisionEngine`` directly (simplest) or call this:

    uvicorn decision_engine.api:app --port 8003

POST /v1/evaluate   body: EvaluateInput  -> EngineResult
POST /v1/demo       runs the simulated Alice/Bob/Charlie scenario
GET  /health
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI

from . import mock_data
from .engine import DecisionEngine
from .models import EngineConfig, EngineResult, EvaluateInput

app = FastAPI(title="BookPool Decision Engine", version="0.1.0")
_engine = DecisionEngine()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "config": EngineConfig().model_dump(mode="json")}


@app.post("/v1/evaluate", response_model=EngineResult)
def evaluate(body: EvaluateInput) -> EngineResult:
    return _engine.evaluate_input(body)


@app.post("/v1/demo", response_model=EngineResult)
def demo() -> EngineResult:
    now = datetime.now(timezone.utc)
    reqs = list(mock_data.demo_requests(now.date()).values())
    return _engine.evaluate(reqs, mock_data.offers(now), mock_data.MERCHANTS, now)
