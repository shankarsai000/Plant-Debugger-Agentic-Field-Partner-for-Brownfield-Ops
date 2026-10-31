"""Unit tests for ActionGuard safety boundaries and policies."""

import pytest
from plant_debugger.config import Settings
from plant_debugger.models import Incident, IncidentStatus, TaskKind, TaskStatus
from plant_debugger.safety.action_guard import ActionGuard, GuardViolation, Proposal


@pytest.fixture
def guard():
    settings = Settings(max_field_tasks=4, max_manual_actions=2, max_agent_turns=6, bypass_max_step_pct=25.0)
    return ActionGuard(settings)


@pytest.fixture
def incident():
    return Incident(
        id="inc-test-1",
        title="P-101 LOW-FLOW TEST",
        status=IncidentStatus.PLANNING,
        anomaly={"type": "LOW_FLOW"},
        engine="local",
    )


def test_guard_allows_valid_inspection(guard, incident):
    p = Proposal(capability="inspect", asset="F-17", rationale="Check local DP gauge")
    task = guard.validate(incident, open_tasks=[], p=p)
    assert task.kind == TaskKind.INSPECTION
    assert task.asset == "F-17"
    assert task.status == TaskStatus.PENDING
    assert not task.requires_final_photo


def test_guard_rejects_unsupported_capability(guard, incident):
    p = Proposal(capability="remote_flush", asset="F-17")
    with pytest.raises(GuardViolation, match="not allowed"):
        guard.validate(incident, open_tasks=[], p=p)


def test_guard_enforces_bounded_bypass_delta(guard, incident):
    # Step size > 25% should be rejected
    p_large = Proposal(capability="adjust_bypass", asset="BV-104", delta_pct=50.0)
    with pytest.raises(GuardViolation, match="delta_pct must be non-zero and within ±25"):
        guard.validate(incident, open_tasks=[], p=p_large)

    # Valid +10% step
    p_ok = Proposal(capability="adjust_bypass", asset="BV-104", delta_pct=10.0)
    task = guard.validate(incident, open_tasks=[], p=p_ok)
    assert task.kind == TaskKind.MANUAL_ACTION
    assert task.delta_pct == 10.0
    assert task.target_pct == 10.0
    assert not task.requires_final_photo


def test_guard_enforces_final_photo_for_full_open(guard, incident):
    p_full = Proposal(capability="adjust_bypass", asset="BV-104", target="FULL_OPEN")
    task = guard.validate(incident, open_tasks=[], p=p_full)
    assert task.kind == TaskKind.MANUAL_ACTION
    assert task.target_pct == 100.0
    assert task.requires_final_photo is True
    assert task.status == TaskStatus.AWAITING_PHOTO


def test_guard_blocks_concurrent_tasks(guard, incident):
    p1 = Proposal(capability="inspect", asset="F-17")
    task1 = guard.validate(incident, open_tasks=[], p=p1)

    p2 = Proposal(capability="adjust_bypass", asset="BV-104", delta_pct=10.0)
    with pytest.raises(GuardViolation, match="already outstanding"):
        guard.validate(incident, open_tasks=[task1], p=p2)


def test_guard_enforces_manual_action_budget(guard, incident):
    incident.manual_action_count = 2  # Max is 2
    p = Proposal(capability="adjust_bypass", asset="BV-104", delta_pct=10.0)
    with pytest.raises(GuardViolation, match="manual action budget exhausted"):
        guard.validate(incident, open_tasks=[], p=p)
