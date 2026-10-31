"""Test that operator Site Knowledge adapts future ADK planning contexts."""

import pytest
from plant_debugger.config import Settings
from plant_debugger.agent.orchestrator import IncidentOrchestrator
from plant_debugger.models import IncidentStatus
from plant_debugger.stores.evidence import LocalEvidenceStore
from plant_debugger.stores.json_store import JsonFieldStore
from plant_debugger.vision.engines import LocalVision
from plant_sim.engine import Simulator
from tests.test_end_to_end_loop import InProcessSimClient


@pytest.mark.asyncio
async def test_site_knowledge_adapts_next_work_order(tmp_path):
    settings = Settings(decision_engine="local", vision_engine="local")
    store = JsonFieldStore(data_dir=tmp_path)
    evidence_store = LocalEvidenceStore(root=tmp_path)
    vision = LocalVision()
    sim_engine = Simulator(tick_s=0.1)
    sim_client = InProcessSimClient(sim_engine)

    orch = IncidentOrchestrator(
        settings=settings,
        store=store,
        evidence_store=evidence_store,
        vision=vision,
        sim=sim_client,
    )

    # 1. Operator teaches durable Site Knowledge
    fact_text = "Access F-17 from the west maintenance aisle. East-side access is restricted while P-101 is running."
    await orch.teach_site_knowledge(asset="F-17", text=fact_text)

    # 2. Later, an incident occurs
    anomaly_event = {
        "event_id": "evt-sk-test",
        "type": "LOW_FLOW",
        "asset": "P-101",
        "instrument": "FT-101",
        "title": "P-101 LOW-FLOW INCIDENT",
        "observed_flow_m3h": 65.0,
        "threshold_m3h": 85.0,
        "pump_running": True,
        "detected_at": 1000.0,
        "scenario_id": "run-sk",
    }
    incident = await orch.ingest_anomaly(anomaly_event)
    assert incident.status == IncidentStatus.AWAITING_FIELD

    # 3. Check generated work order task
    tasks = await store.list_tasks(incident.id)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.asset == "F-17"
    # The task access note must reflect the human assertion
    assert task.access_note is not None
    assert "west maintenance aisle" in task.access_note
    # The instructions should include the access constraint
    instruction_blob = " ".join(task.instructions)
    assert "west maintenance aisle" in instruction_blob

