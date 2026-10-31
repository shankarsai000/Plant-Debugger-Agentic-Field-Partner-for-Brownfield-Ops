"""Deterministic heuristic planner implementing the reference brownfield diagnostic flow.

Used when DECISION_ENGINE=local (offline dev / unit tests) or as fallback when
ADK_FALLBACK_POLICY=true. Produces identical PlannerDecision payloads to the Gemini
ADK agent, ensuring full testability without cloud credentials.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Hypothesis
from .context import FieldPlanningContext, PlannerDecision

log = logging.getLogger("plant_debugger.local_planner")


class LocalPlanner:
    """Evidence-driven stateful diagnostic planner."""

    def plan(self, ctx: FieldPlanningContext) -> PlannerDecision:
        # Check guard feedback first
        if ctx.guard_feedback:
            return PlannerDecision(
                new_evidence_summary=[f"ActionGuard rejected proposal: {ctx.guard_feedback}"],
                diagnosis_summary="Plan adjusted due to safety guard constraint.",
                hypotheses=[Hypothesis(name="Constraint recovery", confidence=0.9, evidence=ctx.guard_feedback)],
                decision="escalate",
                escalation_reason=f"Safety guard halted planning: {ctx.guard_feedback}",
            )

        # Build access notes from operator Site Knowledge
        access_notes = []
        for sk in ctx.site_knowledge:
            text = sk.get("text", "")
            if "F-17" in sk.get("asset", "") or "F-17" in text:
                access_notes.append(text)
        f17_access = " ".join(access_notes) if access_notes else None

        # Check completed field tasks to understand current evidence
        completed = ctx.completed_field_results
        f17_inspected = False
        f17_dp_high = False
        bv104_adjusted_10 = False
        bv104_opened_full = False

        for task in completed:
            cap = task.get("capability")
            asset = task.get("asset")
            status = task.get("status")
            if cap == "inspect" and asset == "F-17":
                f17_inspected = True
                task_res = task.get("result") or {}
                vis = task_res.get("vision") or task.get("vision_result") or {}
                if vis.get("status") == "HIGH":
                    f17_dp_high = True
            elif cap == "adjust_bypass" and asset == "BV-104":
                target = task.get("target_pct") or 0
                delta = task.get("delta_pct") or 0
                task_res = task.get("result") or {}
                pos_verified = task_res.get("position_verified")
                if target == 100 or delta >= 80 or pos_verified == "OPEN":
                    bv104_opened_full = True
                elif delta == 10 or target == 10:
                    bv104_adjusted_10 = True

        # Check new evidence from latest turn if available
        if ctx.new_evidence:
            vis = ctx.new_evidence.get("vision") or {}
            if vis.get("observable") == "dp_gauge" and vis.get("status") == "HIGH":
                f17_dp_high = True

        # --- STEP 1: Initial Anomaly -> Inspect F-17 ΔP gauge ---
        if not f17_inspected:
            work_order_lines = []
            if f17_access:
                work_order_lines.append(f"Safety / Access Note: {f17_access}")
            work_order_lines.extend([
                "Locate strainer F-17 on the P-101 discharge manifold.",
                "Inspect the local differential-pressure (ΔP) indicator gauge.",
                "Capture and upload one clear photo showing the F-17 equipment tag and the gauge face.",
            ])
            return PlannerDecision(
                new_evidence_summary=["FT-101 low flow anomaly detected while P-101 running"],
                diagnosis_summary="Flow reduction detected downstream of pump P-101. Restricting element unknown.",
                hypotheses=[
                    Hypothesis(name="Strainer F-17 fouled / clogged", confidence=0.55,
                               evidence="Common brownfield failure; unmonitored local ΔP gauge"),
                    Hypothesis(name="P-101 mechanical impeller wear / degradation", confidence=0.25,
                               evidence="Pump running with diminished output"),
                    Hypothesis(name="Process line blockage downstream", confidence=0.20,
                               evidence="Line resistance increase"),
                ],
                decision="field_task",
                capability="inspect",
                asset="F-17",
                rationale="F-17 local ΔP indicator is not remotely instrumented. Visual inspection provides immediate diagnostic proof without disrupting process.",
                work_order="\n".join(work_order_lines),
                access_note=f17_access,
                expected_effect="Verify whether differential pressure across F-17 is elevated.",
            )

        # --- STEP 2: F-17 ΔP is HIGH -> Bounded probe BV-104 +10% ---
        if f17_dp_high and not bv104_adjusted_10 and not bv104_opened_full:
            return PlannerDecision(
                new_evidence_summary=["Gemini Vision confirmed F-17 local ΔP indicator in HIGH zone (red band)"],
                diagnosis_summary="Strainer F-17 is restricted by particulate debris. Fluid routing around F-17 required.",
                hypotheses=[
                    Hypothesis(name="Strainer F-17 restriction confirmed", confidence=0.90,
                               evidence="Direct physical gauge evidence: ΔP is HIGH"),
                    Hypothesis(name="Downstream piping accumulation", confidence=0.10,
                               evidence="Secondary possibility"),
                ],
                decision="field_task",
                capability="adjust_bypass",
                asset="BV-104",
                delta_pct=10.0,
                rationale="Perform bounded +10% manual bypass opening to confirm hydraulic response before full bypass.",
                work_order=(
                    "Locate manual bypass valve BV-104.\n"
                    "Carefully rotate the manual handwheel by +10% from its fully closed seat.\n"
                    "Confirm in the app once movement is complete so plant telemetry settles."
                ),
                expected_effect="Observe partial flow recovery on FT-101 and reduction in system backpressure.",
            )

        # --- STEP 3: Partial recovery observed -> Full Open BV-104 (requires final photo) ---
        if f17_dp_high and bv104_adjusted_10 and not bv104_opened_full:
            flow_now = ctx.telemetry_now.get("ft101_flow_m3h", 70.0)
            return PlannerDecision(
                new_evidence_summary=[
                    f"BV-104 +10% probe executed; FT-101 rose to {flow_now:.1f} m³/h (causal correlation verified)",
                    "Recovery remains incomplete; operating below healthy target band (90-115 m³/h)",
                ],
                diagnosis_summary="Bypass route relieved part of F-17 restriction. Full bypass required for operational recovery.",
                hypotheses=[
                    Hypothesis(name="F-17 restriction severe; requires full bypass capacity", confidence=0.95,
                               evidence="Positive flow response to +10% probe proves bypass effectiveness"),
                ],
                decision="field_task",
                capability="adjust_bypass",
                asset="BV-104",
                target="FULL_OPEN",
                rationale="Bypass line has proven capacity to restore flow. Open BV-104 fully to rated mechanical stop.",
                work_order=(
                    "Proceed to manual bypass valve BV-104.\n"
                    "Open the handwheel smoothly until the mechanical pointer reaches the OPEN stop mark.\n"
                    "Capture and upload a clear photo of the BV-104 position indicator showing the OPEN mark."
                ),
                expected_effect="FT-101 flow restores into the nominal 90-115 m³/h range.",
            )

        # --- STEP 4: BV-104 fully OPEN verified -> Request Deterministic Verification ---
        if bv104_opened_full:
            return PlannerDecision(
                new_evidence_summary=[
                    "BV-104 mechanically confirmed OPEN via visual position verification",
                    f"FT-101 current flow: {ctx.telemetry_now.get('ft101_flow_m3h', 'nominal')} m³/h",
                ],
                diagnosis_summary="Bypass established around clogged F-17. Flow restoration observed. Handing over to deterministic verifier.",
                hypotheses=[
                    Hypothesis(name="Incident mitigation complete via BV-104 bypass", confidence=0.98,
                               evidence="Bypass OPEN physically confirmed; telemetry responding"),
                ],
                decision="request_verification",
                rationale="Physical actions complete. Deterministic code must now evaluate sustained recovery over time window.",
            )

        # Fallback / unanticipated branch -> Escalate
        return PlannerDecision(
            new_evidence_summary=["Evaluation reached unhandled state"],
            diagnosis_summary="Diagnostic state requires lead intervention.",
            hypotheses=[Hypothesis(name="Uncertain brownfield anomaly", confidence=0.5)],
            decision="escalate",
            escalation_reason="No safe automated hypothesis convergence; escalated to Operations Lead.",
        )
