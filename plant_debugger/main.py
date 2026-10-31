"""Plant Debugger main application: FastAPI service with ADK host and Web UIs."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent.adk_engine import AdkWorkflowHost, create_session_service
from .agent.orchestrator import IncidentOrchestrator
from .api.routes import router as api_router
from .config import Settings, get_settings
from .models import IncidentStatus
from .sim_client import SimClient
from .stores.base import FieldStore
from .stores.evidence import EvidenceStore, GcsEvidenceStore, LocalEvidenceStore
from .stores.firestore_store import FirestoreFieldStore
from .stores.json_store import JsonFieldStore
from .vision.engines import build_vision

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("plant_debugger")


async def verifier_background_loop(app: FastAPI):
    """Background task checking recovery verification conditions on active incidents."""
    while True:
        try:
            await asyncio.sleep(1.0)
            orch: IncidentOrchestrator = getattr(app.state, "orchestrator", None)
            store: FieldStore = getattr(app.state, "store", None)
            if not orch or not store:
                continue

            incidents = await store.list_incidents(limit=10)
            for inc in incidents:
                if inc.status == IncidentStatus.VERIFYING:
                    await orch.tick_verifier(inc.id)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.error("Verifier background loop error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info("Starting Plant Debugger (engine=%s, vision=%s, state=%s, submission_mode=%s)",
             settings.decision_engine, settings.vision_engine, settings.field_state_backend, settings.submission_mode)

    # 1. State Store
    if settings.field_state_backend == "firestore":
        store = FirestoreFieldStore(
            project=settings.google_cloud_project,
            database=settings.firestore_database,
            prefix=settings.firestore_prefix,
        )
    else:
        store = JsonFieldStore(data_dir=settings.data_dir)

    # 2. Evidence Store
    if settings.field_evidence_backend == "gcs":
        evidence_store = GcsEvidenceStore(
            bucket=settings.gcs_evidence_bucket,
            project=settings.google_cloud_project,
            ttl_s=settings.signed_url_ttl_s,
        )
    else:
        evidence_store = LocalEvidenceStore(root=settings.data_dir)

    # 3. Vision Engine
    vision = build_vision(settings)

    # 4. Plant Simulator Client
    sim = SimClient(settings)

    # 5. ADK Workflow Host (if configured)
    adk_host = None
    if settings.decision_engine == "adk":
        session_svc = create_session_service(settings)
        adk_host = AdkWorkflowHost(settings, session_svc)

    # 6. Incident Orchestrator
    orchestrator = IncidentOrchestrator(
        settings=settings,
        store=store,
        evidence_store=evidence_store,
        vision=vision,
        sim=sim,
        adk_host=adk_host,
    )

    app.state.settings = settings
    app.state.store = store
    app.state.evidence_store = evidence_store
    app.state.vision = vision
    app.state.sim = sim
    app.state.adk_host = adk_host
    app.state.orchestrator = orchestrator

    # Start verifier background worker
    v_task = asyncio.create_task(verifier_background_loop(app))

    yield

    v_task.cancel()
    await sim.close()
    log.info("Plant Debugger shut down.")


app = FastAPI(title="Plant Debugger", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")

# Static Web UI directory
WEB_DIR = Path(__file__).parent / "web"
WEB_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def commander_ui():
    index_file = WEB_DIR / "commander.html"
    if not index_file.exists():
        return {"status": "Plant Debugger running"}
    return FileResponse(str(index_file))


@app.get("/operator")
def operator_ui():
    op_file = WEB_DIR / "operator.html"
    if not op_file.exists():
        return {"status": "Operator UI"}
    return FileResponse(str(op_file))

