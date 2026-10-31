"""FieldPlanningContext (what the planner may see) and PlannerDecision (what it may say)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import Diagnosis, Hypothesis

PLANT_DESCRIPTION = (
    "Cooling-water loop. Pump P-101 discharges through strainer F-17 to flow meter FT-101. "
    "Manual bypass valve BV-104 (handwheel, normally CLOSED) routes flow around F-17. "
    "Remote telemetry: FT-101 flow, P-101 discharge pressure, vibration, bearing temperature, motor current. "
    "NOT remotely instrumented: F-17 local differential-pressure gauge (visual only) and the BV-104 position "
    "(mechanical indicator, visual only). There are no remote actuators on BV-104."
)

CAPABILITY_CATALOG = [
    {"capability": "inspect", "asset": "F-17", "kind": "image task",
     "returns": "Gemini Vision classification of the local ΔP gauge: NORMAL | HIGH | UNCERTAIN"},
    {"capability": "inspect", "asset": "BV-104", "kind": "image task",
     "returns": "valve position classification: OPEN | CLOSED | UNCERTAIN"},
    {"capability": "adjust_bypass", "asset": "BV-104", "kind": "manual action",
     "params": "delta_pct (non-zero, |delta| <= step bound) OR target='FULL_OPEN'",
     "returns": "execution confirmation + FT-101 telemetry before/after; FULL_OPEN additionally requires a "
                "final-position photo verified as OPEN"},
]


class FieldPlanningContext(BaseModel):
    incident_id: str
    incident_title: str
    plant: str = PLANT_DESCRIPTION
    anomaly: dict[str, Any]
    telemetry_now: dict[str, Any] = {}
    telemetry_baseline: dict[str, Any] = {}
    bv104_confirmed_pct: float = 0.0
    current_diagnosis: Diagnosis | None = None
    completed_field_results: list[dict[str, Any]] = []
    new_evidence: dict[str, Any] | None = None
    guard_feedback: str | None = None
    site_knowledge: list[dict[str, str]] = []
    capabilities: list[dict[str, Any]] = Field(default_factory=lambda: CAPABILITY_CATALOG)
    budgets: dict[str, Any] = {}
    healthy_flow_band_m3h: list[float] = [90.0, 115.0]


class PlannerDecision(BaseModel):
    """Structured output of one planning step."""

    new_evidence_summary: list[str] = Field(
        default_factory=list, description="Short bullets: what new plant evidence was considered this turn")
    diagnosis_summary: str = Field(description="One or two sentences: current belief about the cause")
    hypotheses: list[Hypothesis] = Field(default_factory=list, description="Ranked hypotheses with confidence 0..1")
    decision: Literal["field_task", "request_verification", "escalate"]
    capability: Literal["inspect", "adjust_bypass"] | None = None
    asset: Literal["F-17", "BV-104"] | None = None
    delta_pct: float | None = Field(default=None, description="adjust_bypass partial move, percent of travel")
    target: Literal["FULL_OPEN"] | None = None
    rationale: str = Field(default="", description="Why this is the highest-information / safest next step")
    work_order: str = Field(default="", description="Operator-facing instruction lines (newline separated), "
                                                    "adapted to site knowledge such as access routes")
    access_note: str | None = Field(default=None, description="Access/safety note derived from site knowledge")
    expected_effect: str | None = None
    escalation_reason: str | None = None

    def diagnosis(self) -> Diagnosis:
        return Diagnosis(summary=self.diagnosis_summary, hypotheses=self.hypotheses)

