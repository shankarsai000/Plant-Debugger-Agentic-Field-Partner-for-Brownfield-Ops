"""plant-sim FastAPI service (deployed as a *private* Cloud Run service)."""

from __future__ import annotations

import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from . import camera
from .engine import SCENARIOS, Simulator

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("plant_sim")

PUBLISHER = os.getenv("ANOMALY_PUBLISHER", "http")          # http | pubsub | none
DEBUGGER_URL = os.getenv("DEBUGGER_URL", "http://127.0.0.1:8080")
PUBSUB_TOPIC = os.getenv("PUBSUB_TOPIC", "plant-anomaly-events")
GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
SIM_SHARED_TOKEN = os.getenv("SIM_SHARED_TOKEN", "")         # local-dev auth between services

sim = Simulator(tick_s=float(os.getenv("SIM_TICK_S", "1.0")))


async def _publish_http(event: dict[str, Any]) -> None:
    """Local stand-in for Pub/Sub push: posts a Pub/Sub-shaped envelope."""
    envelope = {
        "message": {
            "data": base64.b64encode(json.dumps(event).encode()).decode(),
            "messageId": event["event_id"],
            "attributes": {"type": event["type"], "asset": event["asset"]},
        },
        "subscription": "local/plant-debugger-push",
    }
    headers = {"X-Sim-Token": SIM_SHARED_TOKEN} if SIM_SHARED_TOKEN else {}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{DEBUGGER_URL}/api/pubsub/anomaly", json=envelope, headers=headers)
        r.raise_for_status()


async def _publish_pubsub(event: dict[str, Any]) -> None:
    from google.cloud import pubsub_v1  # imported lazily: only needed on GCP

    publisher = pubsub_v1.PublisherClient()
    topic = publisher.topic_path(GCP_PROJECT, PUBSUB_TOPIC)
    future = publisher.publish(topic, json.dumps(event).encode(), type=event["type"], asset=event["asset"])
    future.result(timeout=30)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if PUBLISHER == "http":
        sim.set_publisher(_publish_http)
    elif PUBLISHER == "pubsub":
        sim.set_publisher(_publish_pubsub)
    await sim.start()
    log.info("plant-sim started (publisher=%s)", PUBLISHER)
    yield
    await sim.stop()


app = FastAPI(title="plant-sim", lifespan=lifespan)


def _auth(x_sim_token: str | None = Header(default=None)) -> None:
    # On Cloud Run the service is private (IAM invoker). Locally an optional shared token is used.
    if SIM_SHARED_TOKEN and x_sim_token != SIM_SHARED_TOKEN:
        raise HTTPException(status_code=401, detail="invalid sim token")


class ScenarioRequest(BaseModel):
    fault: Literal["f17_restriction", "pump_degradation", "healthy"] = "f17_restriction"


class ActionRequest(BaseModel):
    action_id: str = Field(min_length=6)
    asset: str
    op: Literal["adjust", "set_position"]
    delta_pct: float | None = None
    target_pct: float | None = None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/sim/scenario", dependencies=[Depends(_auth)])
async def start_scenario(req: ScenarioRequest) -> dict[str, Any]:
    return await sim.start_scenario(req.fault)


@app.post("/sim/reset", dependencies=[Depends(_auth)])
async def reset() -> dict[str, str]:
    sim.reset_plant()
    return {"status": "reset"}


@app.get("/sim/scenarios", dependencies=[Depends(_auth)])
def scenarios() -> dict[str, Any]:
    return {k: v["description"] for k, v in SCENARIOS.items()}


@app.get("/sim/telemetry", dependencies=[Depends(_auth)])
def telemetry(last_n: int = 120) -> dict[str, Any]:
    """Remote instrumentation only (FT-101 and P-101 sensors). No ΔP, no valve position."""
    return sim.telemetry(last_n)


@app.post("/sim/actions", dependencies=[Depends(_auth)])
async def actions(req: ActionRequest) -> dict[str, Any]:
    try:
        return await sim.apply_action(req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/sim/camera/{asset}", dependencies=[Depends(_auth)])
def camera_view(asset: str) -> Response:
    try:
        png = camera.capture(asset, sim.field_truth())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(content=png, media_type="image/png")

