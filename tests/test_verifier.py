"""Unit tests for the deterministic recovery verifier."""

import pytest
from plant_debugger.config import Settings
from plant_debugger.safety.verifier import RecoveryVerifier


@pytest.fixture
def verifier():
    settings = Settings(
        healthy_flow_min_m3h=90.0,
        healthy_flow_max_m3h=115.0,
        verify_window_s=5.0,
        verify_timeout_s=15.0,
    )
    return RecoveryVerifier(settings)


def test_verifier_pass_window(verifier):
    t0 = 1000.0
    st = verifier.begin(call_id="call-1", now=t0)
    assert st.status == "RUNNING"

    # Sample 1: flow healthy
    st = verifier.evaluate(st, {"pump_running": True, "ft101_flow_m3h": 95.0, "ts": 1000.0}, now=1000.0)
    assert st.status == "RUNNING"
    assert st.healthy_since == 1000.0

    # Sample 2: flow still healthy at 3 seconds (not yet window of 5s)
    st = verifier.evaluate(st, {"pump_running": True, "ft101_flow_m3h": 98.0, "ts": 1003.0}, now=1003.0)
    assert st.status == "RUNNING"

    # Sample 3: flow healthy at 5.5 seconds -> PASSED!
    st = verifier.evaluate(st, {"pump_running": True, "ft101_flow_m3h": 97.5, "ts": 1005.5}, now=1005.5)
    assert st.status == "PASSED"
    assert "sustained" in st.detail


def test_verifier_resets_on_flow_dip(verifier):
    t0 = 1000.0
    st = verifier.begin(call_id="call-1", now=t0)

    # Healthy at t=1000
    st = verifier.evaluate(st, {"pump_running": True, "ft101_flow_m3h": 95.0, "ts": 1000.0}, now=1000.0)
    assert st.healthy_since == 1000.0

    # Dip at t=1002 (flow drops below 90)
    st = verifier.evaluate(st, {"pump_running": True, "ft101_flow_m3h": 82.0, "ts": 1002.0}, now=1002.0)
    assert st.status == "RUNNING"
    assert st.healthy_since is None  # Reset window!


def test_verifier_timeout(verifier):
    t0 = 1000.0
    st = verifier.begin(call_id="call-1", now=t0)

    # Sub-nominal flow continues past 15s timeout
    st = verifier.evaluate(st, {"pump_running": True, "ft101_flow_m3h": 75.0, "ts": 1016.0}, now=1016.0)
    assert st.status == "FAILED"
    assert "No sustained recovery" in st.detail

