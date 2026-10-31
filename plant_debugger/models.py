"""Domain models shared across the application."""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class IncidentStatus(str, Enum):
    DETECTED = "DETECTED"
    PLANNING = "PLANNING"
    AWAITING_FIELD = "AWAITING_FIELD"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    BLOCKED = "BLOCKED"


TERMINAL_STATUSES = {IncidentStatus.RESOLVED, IncidentStatus.ESCALATED, IncidentStatus.BLOCKED}


class TaskKind(str, Enum):
    INSPECTION = "inspection"          # image task: evidence only, no state change
    MANUAL_ACTION = "manual_action"    # bounded physical adjustment


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    AWAITING_PHOTO = "AWAITING_PHOTO"  # final-position evidence required before completion
    SETTLING = "SETTLING"              # action executed; sampling plant response
    COMPLETED = "COMPLETED"
    CANNOT_PERFORM = "CANNOT_PERFORM"
    CANCELLED = "CANCELLED"


class Hypothesis(BaseModel):
    name: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = ""


class Diagnosis(BaseModel):
    summary: str
    hypotheses: list[Hypothesis] = []


class TraceEntry(BaseModel):
    """One ADK turn rendered as: NEW PLANT EVIDENCE -> DIAGNOSIS UPDATE -> NEXT FIELD TASK."""

    turn: int
    ts: float = Field(default_factory=now)
    engine: str
    new_evidence: list[str] = []
    diagnosis: Diagnosis | None = None
    next_step: str | None = None
    next_task_id: str | None = None
    decision: Literal["field_task", "request_verification", "escalate", "verification", "escalation", "error", "none"] = "none"


class VisionResult(BaseModel):
    engine: str
    observable: Literal["dp_gauge", "valve_position"]
    status: Literal["NORMAL", "HIGH", "OPEN", "CLOSED", "UNCERTAIN"]
    asset_visible: bool = False
    asset_tag_read: str | None = None
    reading: str | None = None
    confidence: float = 0.0
    rationale: str = ""


class Evidence(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ev"))
    incident_id: str
    task_id: str
    kind: Literal["inspection_photo", "final_position_photo"]
    sha256: str
    content_type: str
    size_bytes: int
    storage_uri: str
    source: Literal["operator_upload", "sim_camera"] = "operator_upload"
    vision: VisionResult | None = None
    created_at: float = Field(default_factory=now)


class FieldTask(BaseModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    incident_id: str
    kind: TaskKind
    capability: str
    asset: str
    observable: Literal["dp_gauge", "valve_position"] | None = None
    title: str
    rationale: str = ""
    instructions: list[str] = []
    safety_notes: list[str] = []
    access_note: str | None = None
    # bounded manual action parameters (validated by ActionGuard)
    delta_pct: float | None = None
    from_pct: float | None = None
    target_pct: float | None = None
    requires_final_photo: bool = False
    expected_effect: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    call_id: str | None = None               # ADK function_call.id that this task will answer
    idempotency_key: str
    evidence_ids: list[str] = []
    photo_attempts: int = 0
    result: dict[str, Any] | None = None
    created_at: float = Field(default_factory=now)
    completed_at: float | None = None


class VerificationState(BaseModel):
    status: Literal["NOT_STARTED", "RUNNING", "PASSED", "FAILED"] = "NOT_STARTED"
    call_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    healthy_since: float | None = None
    window_s: float = 0.0
    flow_min_m3h: float = 0.0
    flow_max_m3h: float = 0.0
    samples_checked: int = 0
    last_flow_m3h: float | None = None
    detail: str = ""


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: new_id("inc"))
    title: str
    status: IncidentStatus = IncidentStatus.DETECTED
    anomaly: dict[str, Any]
    engine: str
    adk_session_id: str | None = None
    diagnosis: Diagnosis | None = None
    trace: list[TraceEntry] = []
    task_ids: list[str] = []
    field_task_count: int = 0
    manual_action_count: int = 0
    agent_turns: int = 0
    bv104_known_pct: float = 0.0             # last *confirmed* bypass position (P&ID normal: closed)
    verification: VerificationState = Field(default_factory=VerificationState)
    resolution: str | None = None
    escalation: str | None = None
    error: str | None = None
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    closed_at: float | None = None


class SiteKnowledge(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sk"))
    asset: str
    text: str
    author: str = "field-operator"
    source: Literal["operator_assertion"] = "operator_assertion"   # the ONLY admissible source
    created_at: float = Field(default_factory=now)
    active: bool = True


class AuditEvent(BaseModel):
    seq: int
    id: str = Field(default_factory=lambda: new_id("au"))
    ts: float = Field(default_factory=now)
    incident_id: str | None = None
    actor: Literal["system", "plant-sim", "pubsub", "adk", "local-planner", "operator",
                   "commander", "action-guard", "vision", "verifier"]
    type: str
    summary: str
    details: dict[str, Any] = {}
    prev_hash: str = ""
    hash: str = ""
