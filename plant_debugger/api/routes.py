"""REST API endpoints for Commander and Field Operator interfaces."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..models import FieldTask, Incident, SiteKnowledge
from ..stores.evidence import EvidenceIntegrityError

log = logging.getLogger("plant_debugger.api")
router = APIRouter()


def get_orchestrator(request: Request):
    return request.app.state.orchestrator


def get_store(request: Request):
    return request.app.state.store


def get_evidence_store(request: Request):
    return request.app.state.evidence_store


def get_sim_client(request: Request):
    return request.app.state.sim


# ---------------------------------------------------------------------- Incidents
@router.get("/incidents", response_model=list[Incident])
async def list_incidents(limit: int = 20, store=Depends(get_store)):
    return await store.list_incidents(limit=limit)


@router.get("/incidents/{incident_id}", response_model=Incident)
async def get_incident(incident_id: str, store=Depends(get_store)):
    inc = await store.get_incident(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="Incident not found")
    return inc


@router.get("/incidents/{incident_id}/tasks", response_model=list[FieldTask])
async def get_incident_tasks(incident_id: str, store=Depends(get_store)):
    return await store.list_tasks(incident_id)


class SimulateRequest(BaseModel):
    fault: Literal["f17_restriction", "pump_degradation", "healthy"] = "f17_restriction"


@router.post("/incidents/simulate")
async def simulate_scenario(
    req: SimulateRequest,
    sim=Depends(get_sim_client),
    orch=Depends(get_orchestrator),
):
    """Trigger a scenario in plant-sim to start an incident demo."""
    res = await sim.start_scenario(req.fault)
    return {"status": "started", "scenario": req.fault, "result": res}


@router.post("/incidents/reset")
async def reset_simulation(sim=Depends(get_sim_client)):
    await sim.reset()
    return {"status": "reset"}


# ---------------------------------------------------------------------- Tasks
@router.get("/tasks/{task_id}", response_model=FieldTask)
async def get_task(task_id: str, store=Depends(get_store)):
    t = await store.get_task(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    return t


@router.post("/tasks/{task_id}/confirm")
async def confirm_task_action(task_id: str, orch=Depends(get_orchestrator)):
    try:
        t = await orch.confirm_manual_action(task_id)
        return {"status": "confirmed", "task": t}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/evidence")
async def upload_task_evidence(
    task_id: str,
    file: UploadFile | None = File(None),
    use_sim_camera: bool = Form(False),
    orch=Depends(get_orchestrator),
    sim=Depends(get_sim_client),
    store=Depends(get_store),
):
    task = await store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    content_type = "image/png"
    source = "operator_upload"

    if use_sim_camera or file is None:
        # Operator clicked 'Capture from simulated camera'
        image_bytes = await sim.camera(task.asset)
        source = "sim_camera"
    else:
        image_bytes = await file.read()
        content_type = file.content_type or "image/png"

    try:
        ev = await orch.submit_evidence_photo(
            task_id=task_id,
            image_bytes=image_bytes,
            content_type=content_type,
            source=source,
        )
        return {"status": "accepted", "evidence": ev}
    except Exception as exc:
        log.exception("Evidence upload error: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------- Site Knowledge
class TeachKnowledgeRequest(BaseModel):
    asset: str = Field(min_length=2)
    text: str = Field(min_length=5)
    author: str = "field-operator"


@router.get("/knowledge", response_model=list[SiteKnowledge])
async def list_knowledge(store=Depends(get_store)):
    return await store.list_knowledge(active_only=True)


@router.post("/knowledge", response_model=SiteKnowledge)
async def teach_knowledge(req: TeachKnowledgeRequest, orch=Depends(get_orchestrator)):
    return await orch.teach_site_knowledge(asset=req.asset, text=req.text, author=req.author)


@router.post("/knowledge/reset")
async def reset_knowledge(store=Depends(get_store)):
    n = await store.deactivate_knowledge()
    return {"status": "deactivated", "count": n}


# ---------------------------------------------------------------------- Audit Trail
@router.get("/audit")
async def list_audit(incident_id: str | None = None, limit: int = 150, store=Depends(get_store)):
    return await store.list_audit(incident_id=incident_id, limit=limit)


# ---------------------------------------------------------------------- Telemetry
@router.get("/telemetry")
async def get_telemetry(last_n: int = 60, sim=Depends(get_sim_client)):
    return await sim.telemetry(last_n=last_n)


# ---------------------------------------------------------------------- Evidence Blob Stream
@router.get("/evidence/{evidence_id}/content")
async def get_evidence_content(
    evidence_id: str,
    store=Depends(get_store),
    ev_store=Depends(get_evidence_store),
):
    ev = await store.get_evidence(evidence_id)
    if not ev:
        raise HTTPException(status_code=404, detail="Evidence not found")
    try:
        data = await ev_store.get_verified(ev.storage_uri, ev.sha256)
        return Response(content=data, media_type=ev.content_type)
    except EvidenceIntegrityError as eie:
        raise HTTPException(status_code=409, detail=str(eie)) from eie
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------- Pub/Sub Anomaly Push
@router.post("/pubsub/anomaly")
async def pubsub_anomaly_push(
    request: Request,
    authorization: str | None = Header(None),
    x_sim_token: str | None = Header(None),
    orch=Depends(get_orchestrator),
    store=Depends(get_store),
    settings: Settings = Depends(get_settings),
):
    """Google Cloud Pub/Sub push subscription endpoint.
    Pub/Sub pushes HTTP POST requests with JSON payload:
    {"message": {"data": "base64", "messageId": "...", "attributes": {...}}}
    """
    body = await request.json()
    message = body.get("message", {})
    message_id = message.get("messageId") or message.get("message_id")
    if not message_id:
        raise HTTPException(status_code=400, detail="Missing messageId in Pub/Sub envelope")

    # Idempotent message deduplication
    claimed = await store.claim_message(message_id)
    if not claimed:
        log.info("Duplicate Pub/Sub message %s dropped", message_id)
        return {"status": "duplicate_ignored"}

    data_b64 = message.get("data", "")
    try:
        raw_json = base64.b64decode(data_b64).decode("utf-8")
        event = json.loads(raw_json)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 payload: {exc}") from exc

    inc = await orch.ingest_anomaly(event)
    return {"status": "processed", "incident_id": inc.id}

