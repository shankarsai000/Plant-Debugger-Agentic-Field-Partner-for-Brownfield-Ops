"""End-to-end integration test of the full Plant Debugger diagnostic loop."""

import asyncio
import time
import pytest
from plant_debugger.config import Settings
from plant_debugger.agent.orchestrator import IncidentOrchestrator
from plant_debugger.models import IncidentStatus, TaskKind, TaskStatus
from plant_debugger.sim_client import SimClient
from plant_debugger.stores.evidence import LocalEvidenceStore
from plant_debugger.stores.json_store import JsonFieldStore
from plant_debugger.vision.engines import LocalVision
from plant_sim.engine import Simulator


@pytest.fixture
def sim_engine():
    sim = Simulator(tick_s=0.1)
    return sim


class InProcessSimClient(SimClient):
    """In-process mock of SimClient that directly calls Simulator."""
    def __init__(self, sim: Simulator):
        self.sim_engine = sim

    async def telemetry(self, last_n: int = 120):
        return self.sim_engine.telemetry(last_n)

    async def latest(self):
        return self.sim_engine.telemetry(1)["latest"]

    async def apply_action(self, action: dict):
        return await self.sim_engine.apply_action(action)

    async def start_scenario(self, fault: str):
        return await self.sim_engine.start_scenario(fault)

    async def reset(self):
        self.sim_engine.reset_plant()
        return {"status": "reset"}

    async def camera(self, asset: str):
        from plant_sim import camera
        return camera.capture(asset, self.sim_engine.field_truth())

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_full_brownfield_troubleshooting_loop(tmp_path, sim_engine):
    settings = Settings(
        decision_engine="local",
        vision_engine="local",
        action_settle_s=0.1,
        verify_window_s=0.5,
        verify_timeout_s=5.0,
    )
    store = JsonFieldStore(data_dir=tmp_path)
    evidence_store = LocalEvidenceStore(root=tmp_path)
    vision = LocalVision()
    sim_client = InProcessSimClient(sim_engine)

    orch = IncidentOrchestrator(
        settings=settings,
        store=store,
        evidence_store=evidence_store,
        vision=vision,
        sim=sim_client,
    )

    # 0. Start scenario in simulator: F-17 restriction
    await sim_engine.start_scenario("f17_restriction")
    t0 = sim_engine.scenario.started_at
    # Advance physics to let fault manifest
    for i in range(20):
        sim_engine._advance(1.0, now=t0 + i + 1.0)

    # 1. Anomaly Ingestion
    anomaly_event = {
        "event_id": "evt-test-101",
        "type": "LOW_FLOW",
        "asset": "P-101",
        "instrument": "FT-101",
        "title": "P-101 LOW-FLOW INCIDENT",
        "observed_flow_m3h": 62.1,
        "threshold_m3h": 85.0,
        "pump_running": True,
        "detected_at": time.time(),
        "scenario_id": "run-test",
    }
    incident = await orch.ingest_anomaly(anomaly_event)
    assert incident.status == IncidentStatus.AWAITING_FIELD
    assert incident.field_task_count == 1

    # Check Task 1: Inspect F-17 ΔP gauge
    tasks = await store.list_tasks(incident.id)
    assert len(tasks) == 1
    task1 = tasks[0]
    assert task1.kind == TaskKind.INSPECTION
    assert task1.asset == "F-17"
    assert task1.observable == "dp_gauge"

    # 2. Operator submits photo of F-17 gauge
    img1 = await sim_client.camera("F-17")
    ev1 = await orch.submit_evidence_photo(task1.id, img1)
    assert ev1.vision.status == "HIGH"

    # Reload incident: Agent should have re-planned and dispatched Task 2 (BV-104 +10% probe)
    tasks = await store.list_tasks(incident.id)
    assert len(tasks) == 2
    task2 = tasks[1]
    assert task2.kind == TaskKind.MANUAL_ACTION
    assert task2.asset == "BV-104"
    assert task2.delta_pct == 10.0
    assert not task2.requires_final_photo

    # 3. Operator confirms manual +10% movement
    await orch.confirm_manual_action(task2.id)

    # Advance simulator physics to see partial flow recovery
    for i in range(10):
        sim_engine._advance(0.5, now=t0 + 25.0 + i * 0.5)

    # Reload incident: Incomplete recovery triggers Task 3 (BV-104 FULL_OPEN with photo requirement)
    tasks = await store.list_tasks(incident.id)
    assert len(tasks) == 3
    task3 = tasks[2]
    assert task3.kind == TaskKind.MANUAL_ACTION
    assert task3.target_pct == 100.0
    assert task3.requires_final_photo is True
    assert task3.status == TaskStatus.AWAITING_PHOTO

    # 4. Operator applies full open and captures position verification photo
    # Simulator mirrors 100% position
    await sim_client.apply_action({"action_id": "full-act", "asset": "BV-104", "op": "set_position", "target_pct": 100.0})
    img2 = await sim_client.camera("BV-104")
    ev2 = await orch.submit_evidence_photo(task3.id, img2)
    assert ev2.vision.status == "OPEN"

    # 5. Reload incident: Agent requested deterministic verification
    inc = await store.get_incident(incident.id)
    assert inc.status == IncidentStatus.VERIFYING
    assert inc.verification.status == "RUNNING"

    # Advance simulator physics with bypass fully open (flow recovers > 90 m³/h)
    for i in range(15):
        sim_engine._advance(0.5, now=t0 + 35.0 + i * 0.5)

    # Verifier checks sustained recovery
    for _ in range(8):
        await asyncio.sleep(0.1)
        inc = await orch.tick_verifier(inc.id)
        if inc.status == IncidentStatus.RESOLVED:
            break

    assert inc.status == IncidentStatus.RESOLVED
    assert inc.closed_at is not None
    assert "sustained" in inc.resolution

    # Verify immutable audit trail
    audits = await store.list_audit(inc.id)
    assert len(audits) >= 6
    # Verify hash chain continuity
    for i in range(1, len(audits)):
        assert audits[i].prev_hash == audits[i-1].hash
