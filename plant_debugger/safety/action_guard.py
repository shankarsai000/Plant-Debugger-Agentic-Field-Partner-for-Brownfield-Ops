"""ActionGuard: deterministic boundary between ADK's choices and the physical world.

ADK/Gemini may *propose* any next field capability. Only proposals that pass this
guard become field tasks. The guard owns:

* the capability allow-list (what may be asked of an operator at all),
* numeric action bounds (BV-104 step size, position range),
* step and action budgets,
* one-outstanding-task-at-a-time and idempotency,
* the final-position evidence policy for critical physical states (BV-104 fully OPEN).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..models import TERMINAL_STATUSES, FieldTask, Incident, TaskKind, TaskStatus

# Capability allow-list: capability -> asset -> spec
CAPABILITIES: dict[str, dict[str, dict[str, Any]]] = {
    "inspect": {
        "F-17": {"observable": "dp_gauge", "title": "Inspect F-17 local differential-pressure indicator",
                 "default_steps": ["Go to strainer F-17.",
                                   "Capture ONE readable photo showing the F-17 tag and its local ΔP gauge face."]},
        "BV-104": {"observable": "valve_position", "title": "Inspect BV-104 position indicator",
                   "default_steps": ["Go to manual bypass valve BV-104.",
                                     "Capture ONE photo showing the BV-104 tag and the mechanical position indicator."]},
    },
    "adjust_bypass": {
        "BV-104": {"observable": "valve_position"},
    },
}

OPEN_POSITION_PCT = 100.0
BASE_SAFETY_NOTES = [
    "Wear standard PPE (gloves, eye protection, hearing protection).",
    "Stop immediately and report if you meet abnormal resistance, binding, leakage or an unsafe condition.",
]


class GuardViolation(Exception):
    """Raised when a proposal violates a deterministic boundary. Message is returned to ADK."""


@dataclass
class Proposal:
    capability: str
    asset: str
    rationale: str = ""
    work_order: str = ""                 # agent-authored, context-aware instruction text
    access_note: str | None = None
    delta_pct: float | None = None       # adjust_bypass: partial movement
    target: str | None = None            # adjust_bypass: "FULL_OPEN"
    expected_effect: str | None = None
    call_id: str | None = None


class ActionGuard:
    def __init__(self, settings: Settings):
        self.s = settings

    @staticmethod
    def idempotency_key(incident_id: str, proposal: Proposal, ordinal: int) -> str:
        basis = proposal.call_id or f"{proposal.capability}|{proposal.asset}|{proposal.delta_pct}|{proposal.target}|{ordinal}"
        return hashlib.sha256(f"{incident_id}|{basis}".encode()).hexdigest()[:24]

    def check_turn_budget(self, incident: Incident) -> None:
        if incident.agent_turns >= self.s.max_agent_turns:
            raise GuardViolation(f"agent turn budget exhausted ({self.s.max_agent_turns}); escalate to a human lead")

    def validate(self, incident: Incident, open_tasks: list[FieldTask], p: Proposal) -> FieldTask:
        if incident.status in TERMINAL_STATUSES:
            raise GuardViolation(f"incident is {incident.status.value}; no further field tasks allowed")
        if any(t.status in (TaskStatus.PENDING, TaskStatus.AWAITING_PHOTO, TaskStatus.SETTLING) for t in open_tasks):
            raise GuardViolation("a field task is already outstanding; wait for its evidence before dispatching another")
        if incident.verification.status == "RUNNING":
            raise GuardViolation("deterministic verification is running; field tasks are frozen")
        if incident.field_task_count >= self.s.max_field_tasks:
            raise GuardViolation(f"field task budget exhausted ({self.s.max_field_tasks}); escalate")

        caps = CAPABILITIES.get(p.capability)
        if caps is None:
            raise GuardViolation(f"capability '{p.capability}' is not allowed. Allowed: {sorted(CAPABILITIES)}")
        spec = caps.get(p.asset)
        if spec is None:
            raise GuardViolation(f"capability '{p.capability}' is not allowed on asset '{p.asset}'. Allowed assets: {sorted(caps)}")

        key = self.idempotency_key(incident.id, p, incident.field_task_count)
        steps_from_agent = [s.strip() for s in p.work_order.split("\n") if s.strip()] if p.work_order else []

        if p.capability == "inspect":
            return FieldTask(
                incident_id=incident.id, kind=TaskKind.INSPECTION, capability="inspect", asset=p.asset,
                observable=spec["observable"], title=spec["title"], rationale=p.rationale,
                instructions=steps_from_agent or spec["default_steps"],
                safety_notes=["Observe only. Do not operate any equipment for this task.", BASE_SAFETY_NOTES[0]],
                access_note=p.access_note, idempotency_key=key, call_id=p.call_id,
                expected_effect=p.expected_effect,
            )

        # ---- adjust_bypass ---------------------------------------------------
        if incident.manual_action_count >= self.s.max_manual_actions:
            raise GuardViolation(f"manual action budget exhausted ({self.s.max_manual_actions}); escalate")
        current = incident.bv104_known_pct
        if p.target is not None:
            if p.target.upper() != "FULL_OPEN":
                raise GuardViolation("adjust_bypass target must be 'FULL_OPEN' or use delta_pct")
            if current >= OPEN_POSITION_PCT:
                raise GuardViolation("BV-104 is already confirmed fully OPEN")
            target_pct = OPEN_POSITION_PCT
            delta = target_pct - current
        else:
            if p.delta_pct is None:
                raise GuardViolation("adjust_bypass requires delta_pct or target='FULL_OPEN'")
            delta = float(p.delta_pct)
            if delta == 0 or abs(delta) > self.s.bypass_max_step_pct:
                raise GuardViolation(
                    f"delta_pct must be non-zero and within ±{self.s.bypass_max_step_pct:g}% per step "
                    f"(use target='FULL_OPEN' for a full-open move)")
            target_pct = current + delta
            if not (0.0 <= target_pct <= OPEN_POSITION_PCT):
                raise GuardViolation(f"resulting BV-104 position {target_pct:g}% is outside 0..100% (current {current:g}%)")

        full_open = target_pct >= OPEN_POSITION_PCT
        direction = "Open" if delta > 0 else "Close"
        if full_open:
            title = "Open BV-104 fully to the mechanical OPEN mark"
            bounded = [
                "Go to manual bypass valve BV-104 (handwheel).",
                f"Open the handwheel from the current {current:g}% until the indicator reaches the marked OPEN stop.",
                "Do NOT force the handwheel past the OPEN stop.",
                "Capture ONE photo showing the BV-104 tag and the position indicator at OPEN — this photo completes the task.",
            ]
        else:
            title = f"{direction} BV-104 by {abs(delta):g}% ({current:g}% → {target_pct:g}%)"
            bounded = [
                "Go to manual bypass valve BV-104 (handwheel).",
                f"{direction} the handwheel by exactly {abs(delta):g}% of travel, from {current:g}% to {target_pct:g}%.",
                "Confirm in the app once the movement is complete; the plant response will be sampled automatically.",
            ]
        return FieldTask(
            incident_id=incident.id, kind=TaskKind.MANUAL_ACTION, capability="adjust_bypass", asset="BV-104",
            observable="valve_position", title=title, rationale=p.rationale,
            instructions=(steps_from_agent[:2] if steps_from_agent else []) + bounded,
            safety_notes=BASE_SAFETY_NOTES, access_note=p.access_note,
            delta_pct=round(delta, 2), from_pct=current, target_pct=round(target_pct, 2),
            requires_final_photo=full_open, expected_effect=p.expected_effect,
            status=TaskStatus.AWAITING_PHOTO if full_open else TaskStatus.PENDING,
            idempotency_key=key, call_id=p.call_id,
        )

