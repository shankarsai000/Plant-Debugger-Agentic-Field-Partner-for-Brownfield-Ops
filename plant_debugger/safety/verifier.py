"""Deterministic recovery verifier: the ONLY authority that can mark an incident RESOLVED.

Gemini can request verification, but RESOLVED requires FT-101 to stay inside the
healthy band, with the pump running, for a sustained window.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..models import VerificationState


class RecoveryVerifier:
    def __init__(self, settings: Settings):
        self.s = settings

    def begin(self, call_id: str | None, now: float) -> VerificationState:
        return VerificationState(
            status="RUNNING", call_id=call_id, started_at=now, window_s=self.s.verify_window_s,
            flow_min_m3h=self.s.healthy_flow_min_m3h, flow_max_m3h=self.s.healthy_flow_max_m3h,
            detail=(f"Requires FT-101 in [{self.s.healthy_flow_min_m3h:g}, {self.s.healthy_flow_max_m3h:g}] m³/h "
                    f"with P-101 running for {self.s.verify_window_s:g}s"),
        )

    def evaluate(self, state: VerificationState, sample: dict[str, Any], now: float) -> VerificationState:
        """Pure state transition, unit-testable. Call once per new telemetry sample."""
        if state.status != "RUNNING":
            return state
        st = state.model_copy()
        st.samples_checked += 1
        flow = float(sample.get("ft101_flow_m3h", 0.0))
        st.last_flow_m3h = flow
        healthy = bool(sample.get("pump_running")) and st.flow_min_m3h <= flow <= st.flow_max_m3h
        if healthy:
            st.healthy_since = st.healthy_since or now
            if now - st.healthy_since >= st.window_s:
                st.status = "PASSED"
                st.finished_at = now
                st.detail = (f"FT-101 sustained {flow:.1f} m³/h inside the healthy band for "
                             f"{now - st.healthy_since:.0f}s ({st.samples_checked} samples)")
        else:
            st.healthy_since = None
        if st.status == "RUNNING" and st.started_at and now - st.started_at >= self.s.verify_timeout_s:
            st.status = "FAILED"
            st.finished_at = now
            st.detail = (f"No sustained recovery within {self.s.verify_timeout_s:g}s; last FT-101 {flow:.1f} m³/h "
                         f"(healthy band {st.flow_min_m3h:g}-{st.flow_max_m3h:g})")
        return st

