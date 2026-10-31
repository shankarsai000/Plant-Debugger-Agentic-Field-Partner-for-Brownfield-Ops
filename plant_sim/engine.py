"""Simulation engine: scenarios, hidden faults, telemetry history, anomaly detection
and idempotent action mirroring."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .physics import PlantState

log = logging.getLogger("plant_sim")

# Hidden fault catalogue. Labels never leave the simulator.
SCENARIOS: dict[str, dict[str, Any]] = {
    "f17_restriction": {"f17_clog": 0.73, "pump_health": 1.0, "description": "Strainer F-17 progressively fouled"},
    "pump_degradation": {"f17_clog": 0.0, "pump_health": 0.6, "description": "P-101 impeller wear"},
    "healthy": {"f17_clog": 0.0, "pump_health": 1.0, "description": "No fault"},
}

LOW_FLOW_THRESHOLD_M3H = 85.0
LOW_FLOW_CONFIRM_SAMPLES = 5
FAULT_RAMP_S = 15.0


@dataclass
class Scenario:
    scenario_id: str
    fault_label: str
    started_at: float
    target_clog: float
    target_health: float
    anomaly_published: bool = False


AnomalyPublisher = Callable[[dict[str, Any]], Awaitable[None]]


class Simulator:
    def __init__(self, tick_s: float = 1.0, history: int = 900):
        self.tick_s = tick_s
        self.state = PlantState()
        self.history: deque[dict[str, Any]] = deque(maxlen=history)
        self.applied_actions: dict[str, dict[str, Any]] = {}
        self.scenario: Scenario | None = None
        self._low_flow_count = 0
        self._publisher: AnomalyPublisher | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.seq = 0

    # ------------------------------------------------------------------ lifecycle
    def set_publisher(self, publisher: AnomalyPublisher) -> None:
        self._publisher = publisher

    async def start(self) -> None:
        # Warm up so the history buffer holds a healthy baseline.
        for _ in range(30):
            self._advance(self.tick_s, now=time.time() - (30 - _) * self.tick_s)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            async with self._lock:
                sample = self._advance(self.tick_s)
            await self._detect_anomaly(sample)

    # ------------------------------------------------------------------ physics
    def _advance(self, dt: float, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        sc = self.scenario
        if sc:
            ramp = min(1.0, (now - sc.started_at) / FAULT_RAMP_S)
            # Faults develop progressively; manual maintenance (not modelled) would be needed to clear them.
            self.state.f17_clog = max(self.state.f17_clog, sc.target_clog * ramp) if sc.target_clog else self.state.f17_clog
            if sc.target_health < 1.0:
                self.state.pump_health = 1.0 - (1.0 - sc.target_health) * ramp
        self.state.step(dt)
        self.seq += 1
        sample = {
            "seq": self.seq,
            "ts": now,
            "pump_running": self.state.pump_running,
            "ft101_flow_m3h": round(self.state.flow_m3h(), 2),
            "p101_discharge_bar": round(self.state.discharge_pressure_bar(), 3),
            "p101_vibration_mm_s": round(self.state.vibration_mm_s(), 2),
            "p101_bearing_temp_c": round(self.state.bearing_temp_c, 2),
            "p101_motor_current_a": round(self.state.motor_current_a(), 2),
        }
        self.history.append(sample)
        return sample

    async def _detect_anomaly(self, sample: dict[str, Any]) -> None:
        sc = self.scenario
        if not sc or sc.anomaly_published:
            return
        if sample["pump_running"] and sample["ft101_flow_m3h"] < LOW_FLOW_THRESHOLD_M3H:
            self._low_flow_count += 1
        else:
            self._low_flow_count = 0
        if self._low_flow_count >= LOW_FLOW_CONFIRM_SAMPLES:
            sc.anomaly_published = True
            event = {
                "event_id": f"evt-{uuid.uuid4().hex[:12]}",
                "type": "LOW_FLOW",
                "asset": "P-101",
                "instrument": "FT-101",
                "title": "P-101 LOW-FLOW INCIDENT",
                "observed_flow_m3h": sample["ft101_flow_m3h"],
                "threshold_m3h": LOW_FLOW_THRESHOLD_M3H,
                "pump_running": True,
                "detected_at": sample["ts"],
                "scenario_id": sc.scenario_id,  # opaque run id, NOT the fault label
            }
            log.info("Publishing anomaly %s", event["event_id"])
            if self._publisher:
                try:
                    await self._publisher(event)
                except Exception:  # pragma: no cover - surfaced in logs
                    log.exception("Anomaly publish failed")
                    sc.anomaly_published = False
                    self._low_flow_count = 0

    # ------------------------------------------------------------------ scenario control
    async def start_scenario(self, fault: str) -> dict[str, Any]:
        if fault not in SCENARIOS:
            raise ValueError(f"unknown scenario {fault}")
        async with self._lock:
            self.reset_plant()
            spec = SCENARIOS[fault]
            self.scenario = Scenario(
                scenario_id=f"run-{uuid.uuid4().hex[:10]}",
                fault_label=fault,
                started_at=time.time(),
                target_clog=spec["f17_clog"],
                target_health=spec["pump_health"],
            )
            self._low_flow_count = 0
        return {"scenario_id": self.scenario.scenario_id}

    def reset_plant(self) -> None:
        noise = self.state.noise
        q = self.state.q
        self.state = PlantState(noise=noise, q=q)
        self.scenario = None
        self.applied_actions.clear()

    # ------------------------------------------------------------------ telemetry
    def telemetry(self, last_n: int = 120) -> dict[str, Any]:
        samples = list(self.history)[-last_n:]
        return {"latest": samples[-1] if samples else None, "samples": samples}

    # ------------------------------------------------------------------ actions
    async def apply_action(self, action: dict[str, Any]) -> dict[str, Any]:
        """Mirror a physically executed field action. Idempotent on action_id."""
        action_id = action["action_id"]
        async with self._lock:
            if action_id in self.applied_actions:
                return {**self.applied_actions[action_id], "duplicate": True}
            asset = action.get("asset")
            op = action.get("op")
            if asset != "BV-104":
                raise ValueError(f"asset {asset} has no simulated actuator")
            before = self.state.bv104_pct
            if op == "set_position":
                self.state.bv104_pct = float(max(0.0, min(100.0, action["target_pct"])))
            elif op == "adjust":
                self.state.bv104_pct = float(max(0.0, min(100.0, before + action["delta_pct"])))
            else:
                raise ValueError(f"unsupported op {op}")
            result = {"action_id": action_id, "asset": asset, "before_pct": before,
                      "after_pct": self.state.bv104_pct, "applied_at": time.time(), "duplicate": False}
            self.applied_actions[action_id] = result
            return result

    # ------------------------------------------------------------------ demo-only ground truth
    def field_truth(self) -> dict[str, Any]:
        """What a human standing at the equipment would see. Used ONLY by the demo
        photo generator endpoint so the operator UI can produce realistic evidence.
        Never consumed by the planner."""
        return {
            "f17_dp_bar": round(self.state.strainer_dp_bar(), 2),
            "bv104_pct": round(self.state.bv104_pct, 1),
        }

