"""Incident Orchestrator: connects ADK/Local planning, ActionGuard, Field Tasks,
Vision verification, Plant Simulator, and the Deterministic Recovery Verifier.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from google.genai import types

from ..config import Settings
from ..models import (
    AuditEvent,
    Diagnosis,
    Evidence,
    FieldTask,
    Hypothesis,
    Incident,
    IncidentStatus,
    SiteKnowledge,
    TaskKind,
    TaskStatus,
    TraceEntry,
    new_id,
)
from ..safety.action_guard import ActionGuard, GuardViolation, Proposal
from ..safety.verifier import RecoveryVerifier
from ..sim_client import SimClient, summarize_window
from ..stores.base import FieldStore
from ..stores.evidence import EvidenceStore, sha256_hex
from ..vision.engines import VisionEngine
from .adk_engine import AdkWorkflowHost
from .context import FieldPlanningContext, PlannerDecision
from .local_engine import LocalPlanner
from .prompts import PLANNER_TURN_PROMPT

log = logging.getLogger("plant_debugger.orchestrator")


class IncidentOrchestrator:
    def __init__(
        self,
        settings: Settings,
        store: FieldStore,
        evidence_store: EvidenceStore,
        vision: VisionEngine,
        sim: SimClient,
        adk_host: AdkWorkflowHost | None = None,
    ):
        self.s = settings
        self.store = store
        self.evidence_store = evidence_store
        self.vision = vision
        self.sim = sim
        self.guard = ActionGuard(settings)
        self.verifier = RecoveryVerifier(settings)
        self.local_planner = LocalPlanner()
        self.adk_host = adk_host
        self._lock = asyncio.Lock()

    async def _audit(self, incident_id: str | None, actor: str, event_type: str, summary: str, details: dict[str, Any] = None) -> AuditEvent:
        evt = AuditEvent(
            seq=0,
            incident_id=incident_id,
            actor=actor,
            type=event_type,
            summary=summary,
            details=details or {},
        )
        saved = await self.store.append_audit(evt)
        log.info("[AUDIT #%d] %s: %s", saved.seq, saved.type, saved.summary)
        return saved

    # ------------------------------------------------------------------ Incident Ingestion
    async def ingest_anomaly(self, event: dict[str, Any]) -> Incident:
        """Handle incoming anomaly (from Pub/Sub push or simulator)."""
        async with self._lock:
            # Check for existing open incident for this asset/scenario
            existing_incidents = await self.store.list_incidents(limit=5)
            for inc in existing_incidents:
                if inc.status not in (IncidentStatus.RESOLVED, IncidentStatus.ESCALATED, IncidentStatus.BLOCKED):
                    log.info("Active incident %s already in progress; skipping duplicate anomaly", inc.id)
                    return inc

            engine = self.s.decision_engine
            incident = Incident(
                title=event.get("title", "P-101 LOW-FLOW INCIDENT"),
                status=IncidentStatus.DETECTED,
                anomaly=event,
                engine=engine,
                adk_session_id=new_id("session"),
            )
            await self.store.save_incident(incident)
            await self._audit(
                incident.id,
                actor="pubsub" if self.s.pubsub_auth != "none" else "plant-sim",
                event_type="ANOMALY_RECEIVED",
                summary=f"Received anomaly: {incident.title}",
                details=event,
            )

            # Start planning cycle
            await self._run_planning_cycle(incident, new_evidence={"anomaly": event})
            return incident

    # ------------------------------------------------------------------ Planning Loop
    async def _build_planning_context(self, incident: Incident, new_evidence: dict[str, Any] | None = None) -> FieldPlanningContext:
        telemetry = await self.sim.telemetry(last_n=20)
        samples = telemetry.get("samples", [])
        telemetry_now = samples[-1] if samples else {}
        telemetry_baseline = summarize_window(samples[:5]) if len(samples) >= 5 else telemetry_now

        active_knowledge = await self.store.list_knowledge(active_only=True)
        sk_items = [{"asset": k.asset, "text": k.text, "author": k.author} for k in active_knowledge]

        tasks = await self.store.list_tasks(incident.id)
        completed_tasks = [t.model_dump(mode="json") for t in tasks if t.status == TaskStatus.COMPLETED]

        return FieldPlanningContext(
            incident_id=incident.id,
            incident_title=incident.title,
            anomaly=incident.anomaly,
            telemetry_now=telemetry_now,
            telemetry_baseline=telemetry_baseline,
            bv104_confirmed_pct=incident.bv104_known_pct,
            current_diagnosis=incident.diagnosis,
            completed_field_results=completed_tasks,
            new_evidence=new_evidence,
            site_knowledge=sk_items,
            budgets={
                "remaining_field_tasks": max(0, self.s.max_field_tasks - incident.field_task_count),
                "remaining_manual_actions": max(0, self.s.max_manual_actions - incident.manual_action_count),
                "remaining_agent_turns": max(0, self.s.max_agent_turns - incident.agent_turns),
            },
        )

    async def _run_planning_cycle(self, incident: Incident, new_evidence: dict[str, Any] | None = None, function_response_id: str | None = None):
        incident.agent_turns += 1
        incident.updated_at = time.time()
        incident.status = IncidentStatus.PLANNING

        ctx = await self._build_planning_context(incident, new_evidence)

        # Safety: check agent turn budget
        try:
            self.guard.check_turn_budget(incident)
        except GuardViolation as gv:
            incident.status = IncidentStatus.ESCALATED
            incident.escalation = str(gv)
            await self.store.save_incident(incident)
            await self._audit(incident.id, actor="action-guard", event_type="BUDGET_EXHAUSTED", summary=str(gv))
            return

        decision: PlannerDecision | None = None

        if self.s.decision_engine == "adk" and self.adk_host:
            try:
                decision = await self._plan_with_adk(incident, ctx, function_response_id, new_evidence)
            except Exception as exc:
                log.exception("ADK planning failed: %s", exc)
                if self.s.adk_fallback_policy:
                    log.warning("ADK fallback policy enabled; switching to local planner")
                    decision = self.local_planner.plan(ctx)
                else:
                    incident.status = IncidentStatus.BLOCKED
                    incident.error = f"ADK Decision Engine error (fail-closed): {exc}"
                    await self.store.save_incident(incident)
                    await self._audit(incident.id, actor="adk", event_type="ENGINE_FAILED", summary=incident.error)
                    return
        else:
            decision = self.local_planner.plan(ctx)

        await self._apply_decision(incident, decision, call_id=function_response_id)

    async def _plan_with_adk(
        self,
        incident: Incident,
        ctx: FieldPlanningContext,
        function_response_id: str | None,
        new_evidence: dict[str, Any] | None,
    ) -> PlannerDecision:
        """Call ADK Workflow Host and translate events into PlannerDecision."""
        captured_decision: dict[str, Any] = {}

        def request_field_task(
            capability: str,
            asset: str,
            rationale: str = "",
            work_order: str = "",
            access_note: str = "",
            delta_pct: float | None = None,
            target: str | None = None,
            expected_effect: str = "",
        ) -> dict[str, str]:
            captured_decision["action"] = "field_task"
            captured_decision["capability"] = capability
            captured_decision["asset"] = asset
            captured_decision["rationale"] = rationale
            captured_decision["work_order"] = work_order
            captured_decision["access_note"] = access_note or None
            captured_decision["delta_pct"] = delta_pct
            captured_decision["target"] = target
            captured_decision["expected_effect"] = expected_effect
            return {"status": "pending", "message": "Dispatched to Field Operator; awaiting physical evidence"}

        def request_verification(rationale: str = "") -> dict[str, str]:
            captured_decision["action"] = "request_verification"
            captured_decision["rationale"] = rationale
            return {"status": "started", "message": "Deterministic recovery verifier initiated"}

        def escalate(reason: str = "") -> dict[str, str]:
            captured_decision["action"] = "escalate"
            captured_decision["escalation_reason"] = reason
            return {"status": "escalated"}

        # Format user prompt
        prompt_text = PLANNER_TURN_PROMPT.format(
            incident_title=ctx.incident_title,
            incident_id=ctx.incident_id,
            anomaly_json=json.dumps(ctx.anomaly),
            telemetry_now_json=json.dumps(ctx.telemetry_now),
            telemetry_baseline_json=json.dumps(ctx.telemetry_baseline),
            bv104_confirmed_pct=ctx.bv104_confirmed_pct,
            site_knowledge_json=json.dumps(ctx.site_knowledge),
            completed_field_results_json=json.dumps(ctx.completed_field_results),
            new_evidence_json=json.dumps(ctx.new_evidence or {}),
            guard_feedback_section=f"GUARD FEEDBACK: {ctx.guard_feedback}" if ctx.guard_feedback else "",
            remaining_field_tasks=ctx.budgets.get("remaining_field_tasks", 0),
            remaining_manual_actions=ctx.budgets.get("remaining_manual_actions", 0),
            remaining_agent_turns=ctx.budgets.get("remaining_agent_turns", 0),
        )

        if function_response_id:
            message_payload = types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=function_response_id,
                            name="request_field_task",
                            response={"evidence": new_evidence},
                        )
                    ),
                    types.Part(text=prompt_text),
                ],
            )
        else:
            message_payload = types.Content(role="user", parts=[types.Part(text=prompt_text)])

        events = await self.adk_host.execute_turn(
            session_id=incident.adk_session_id,
            user_message=message_payload,
            on_field_task=request_field_task,
            on_verification=request_verification,
            on_escalate=escalate,
        )

        # Extract reasoning and diagnosis from model text response
        model_thoughts = []
        for ev in events:
            c = getattr(ev, "content", None)
            if c and c.parts:
                for p in c.parts:
                    if getattr(p, "text", None):
                        model_thoughts.append(p.text)

        summary_text = " ".join(model_thoughts)[:300] if model_thoughts else "Evaluated field telemetry and physical observations."
        action_kind = captured_decision.get("action", "field_task")

        return PlannerDecision(
            new_evidence_summary=[f"Turn {incident.agent_turns} input evaluated"],
            diagnosis_summary=summary_text,
            hypotheses=[Hypothesis(name="Current operational state", confidence=0.85)],
            decision=action_kind,
            capability=captured_decision.get("capability"),
            asset=captured_decision.get("asset"),
            delta_pct=captured_decision.get("delta_pct"),
            target=captured_decision.get("target"),
            rationale=captured_decision.get("rationale", ""),
            work_order=captured_decision.get("work_order", ""),
            access_note=captured_decision.get("access_note"),
            expected_effect=captured_decision.get("expected_effect"),
            escalation_reason=captured_decision.get("escalation_reason"),
        )

    async def _apply_decision(self, incident: Incident, decision: PlannerDecision, call_id: str | None = None):
        incident.diagnosis = decision.diagnosis()

        trace_entry = TraceEntry(
            turn=incident.agent_turns,
            engine=incident.engine,
            new_evidence=decision.new_evidence_summary,
            diagnosis=incident.diagnosis,
            next_step=decision.rationale or decision.decision,
            decision=decision.decision,
        )

        if decision.decision == "field_task":
            prop = Proposal(
                capability=decision.capability,
                asset=decision.asset,
                rationale=decision.rationale,
                work_order=decision.work_order,
                access_note=decision.access_note,
                delta_pct=decision.delta_pct,
                target=decision.target,
                expected_effect=decision.expected_effect,
                call_id=call_id or new_id("call"),
            )
            open_tasks = await self.store.list_tasks(incident.id)
            try:
                task = self.guard.validate(incident, open_tasks, prop)
            except GuardViolation as gv:
                log.warning("ActionGuard rejected proposal: %s", gv)
                await self._audit(incident.id, actor="action-guard", event_type="PROPOSAL_REJECTED", summary=str(gv))
                # Re-run planning with feedback
                ctx_fb = await self._build_planning_context(incident, new_evidence={"guard_violation": str(gv)})
                ctx_fb.guard_feedback = str(gv)
                fallback_dec = self.local_planner.plan(ctx_fb)
                if fallback_dec.decision == "escalate":
                    incident.status = IncidentStatus.ESCALATED
                    incident.escalation = fallback_dec.escalation_reason or str(gv)
                    await self.store.save_incident(incident)
                return

            await self.store.save_task(task)
            incident.task_ids.append(task.id)
            incident.field_task_count += 1
            if task.kind == TaskKind.MANUAL_ACTION:
                incident.manual_action_count += 1

            incident.status = IncidentStatus.AWAITING_FIELD
            trace_entry.next_task_id = task.id
            incident.trace.append(trace_entry)
            await self.store.save_incident(incident)

            await self._audit(
                incident.id,
                actor="adk" if incident.engine == "adk" else "local-planner",
                event_type="FIELD_TASK_DISPATCHED",
                summary=f"Dispatched field task: {task.title}",
                details={"task_id": task.id, "kind": task.kind.value, "asset": task.asset},
            )

        elif decision.decision == "request_verification":
            incident.status = IncidentStatus.VERIFYING
            incident.verification = self.verifier.begin(call_id=call_id, now=time.time())
            incident.trace.append(trace_entry)
            await self.store.save_incident(incident)
            await self._audit(
                incident.id,
                actor="adk" if incident.engine == "adk" else "local-planner",
                event_type="VERIFICATION_REQUESTED",
                summary="Agent requested deterministic recovery verification",
                details=incident.verification.model_dump(mode="json"),
            )

        elif decision.decision == "escalate":
            incident.status = IncidentStatus.ESCALATED
            incident.escalation = decision.escalation_reason
            incident.trace.append(trace_entry)
            await self.store.save_incident(incident)
            await self._audit(
                incident.id,
                actor="adk" if incident.engine == "adk" else "local-planner",
                event_type="INCIDENT_ESCALATED",
                summary=f"Incident escalated to Operations Lead: {decision.escalation_reason}",
            )

    # ------------------------------------------------------------------ Evidence & Actions
    async def submit_evidence_photo(
        self,
        task_id: str,
        image_bytes: bytes,
        content_type: str = "image/png",
        source: str = "operator_upload",
    ) -> Evidence:
        """Process an uploaded inspection or final-position photo with Vision."""
        async with self._lock:
            task = await self.store.get_task(task_id)
            if not task:
                raise ValueError(f"Task {task_id} not found")
            incident = await self.store.get_incident(task.incident_id)
            if not incident:
                raise ValueError(f"Incident {task.incident_id} not found")

            # Validate and store evidence blob
            digest = sha256_hex(image_bytes)
            key = f"incidents/{incident.id}/{task.id}_{digest[:12]}.png"
            storage_uri = await self.evidence_store.put(key, image_bytes, content_type)

            # Strict vision classification
            vis_result = await self.vision.classify(
                image=image_bytes,
                content_type=content_type,
                observable=task.observable or "dp_gauge",
                asset=task.asset,
            )

            evidence_kind = "final_position_photo" if task.requires_final_photo else "inspection_photo"
            evidence = Evidence(
                incident_id=incident.id,
                task_id=task.id,
                kind=evidence_kind,
                sha256=digest,
                content_type=content_type,
                size_bytes=len(image_bytes),
                storage_uri=storage_uri,
                source=source,
                vision=vis_result,
            )
            await self.store.save_evidence(evidence)
            task.evidence_ids.append(evidence.id)
            task.photo_attempts += 1

            await self._audit(
                incident.id,
                actor="operator",
                event_type="EVIDENCE_UPLOADED",
                summary=f"Uploaded photo for {task.asset} ({digest[:8]}...)",
                details={"vision_status": vis_result.status, "observable": task.observable},
            )

            # Evaluate task completion based on vision result
            if task.kind == TaskKind.INSPECTION:
                task.status = TaskStatus.COMPLETED
                task.completed_at = time.time()
                task.result = {"vision": vis_result.model_dump(mode="json")}
                await self.store.save_task(task)

                # Resume planning loop with multimodal inspection evidence
                await self._run_planning_cycle(
                    incident,
                    new_evidence={"inspection_task": task.id, "vision": vis_result.model_dump(mode="json")},
                    function_response_id=task.call_id,
                )

            elif task.kind == TaskKind.MANUAL_ACTION and task.requires_final_photo:
                # Critical physical state verification: BV-104 fully OPEN
                if vis_result.status == "OPEN":
                    task.status = TaskStatus.COMPLETED
                    task.completed_at = time.time()
                    task.result = {"position_verified": "OPEN", "vision": vis_result.model_dump(mode="json")}
                    incident.bv104_known_pct = 100.0
                    await self.store.save_task(task)
                    await self.store.save_incident(incident)

                    await self._audit(
                        incident.id,
                        actor="vision",
                        event_type="FINAL_POSITION_VERIFIED",
                        summary="Gemini Vision verified BV-104 physically OPEN at mechanical stop",
                    )

                    # Resume agent with verified position evidence
                    await self._run_planning_cycle(
                        incident,
                        new_evidence={"action_task": task.id, "bv104_position": 100.0, "status": "VERIFIED_OPEN"},
                        function_response_id=task.call_id,
                    )
                else:
                    # Vision did not confirm OPEN
                    if task.photo_attempts >= self.s.max_photo_retries:
                        task.status = TaskStatus.CANNOT_PERFORM
                        await self.store.save_task(task)
                        incident.status = IncidentStatus.ESCALATED
                        incident.escalation = f"Failed to visually verify BV-104 OPEN position after {task.photo_attempts} attempts: {vis_result.rationale}"
                        await self.store.save_incident(incident)
                    else:
                        task.status = TaskStatus.AWAITING_PHOTO
                        await self.store.save_task(task)

            return evidence

    async def confirm_manual_action(self, task_id: str) -> FieldTask:
        """Field operator confirms execution of manual adjustment (e.g. +10% move)."""
        async with self._lock:
            task = await self.store.get_task(task_id)
            if not task or task.kind != TaskKind.MANUAL_ACTION:
                raise ValueError(f"Task {task_id} is not an actionable manual task")
            incident = await self.store.get_incident(task.incident_id)

            # Mirror to simulator (idempotent)
            sim_action = {
                "action_id": task.id,
                "asset": task.asset,
                "op": "set_position" if task.target_pct is not None else "adjust",
                "target_pct": task.target_pct,
                "delta_pct": task.delta_pct,
            }
            sim_res = await self.sim.apply_action(sim_action)

            # Wait brief settling delay for physical hydraulics to respond
            if self.s.action_settle_s > 0:
                await asyncio.sleep(min(3.0, self.s.action_settle_s))  # brief wait in async loop

            # Sample post-action telemetry
            telem = await self.sim.telemetry(last_n=5)
            settled_telem = telem.get("latest", {})

            if task.requires_final_photo:
                # Requires confirmation photo before completion
                task.status = TaskStatus.AWAITING_PHOTO
                task.result = {"sim_mirror": sim_res, "settled_telemetry": settled_telem}
                await self.store.save_task(task)
                await self._audit(
                    incident.id,
                    actor="operator",
                    event_type="MANUAL_ACTION_EXECUTED",
                    summary=f"Adjusted {task.asset} to {task.target_pct}%; awaiting final-position confirmation photo",
                )
            else:
                # Partial bounded step completes directly
                task.status = TaskStatus.COMPLETED
                task.completed_at = time.time()
                task.result = {"sim_mirror": sim_res, "settled_telemetry": settled_telem}
                incident.bv104_known_pct = float(task.target_pct or (incident.bv104_known_pct + (task.delta_pct or 0)))
                await self.store.save_task(task)
                await self.store.save_incident(incident)

                await self._audit(
                    incident.id,
                    actor="operator",
                    event_type="MANUAL_ACTION_COMPLETED",
                    summary=f"Adjusted {task.asset} by {task.delta_pct}%; FT-101 flow settled at {settled_telem.get('ft101_flow_m3h', 'unknown')} m³/h",
                )

                # Resume planning loop with causal telemetry evidence
                await self._run_planning_cycle(
                    incident,
                    new_evidence={
                        "action_task": task.id,
                        "action": "adjust_bypass",
                        "delta_pct": task.delta_pct,
                        "resulting_position_pct": incident.bv104_known_pct,
                        "settled_telemetry": settled_telem,
                    },
                    function_response_id=task.call_id,
                )

            return task

    # ------------------------------------------------------------------ Recovery Verifier Tick
    async def tick_verifier(self, incident_id: str) -> Incident:
        """Deterministic verifier loop step: evaluates whether recovery has been sustained."""
        async with self._lock:
            incident = await self.store.get_incident(incident_id)
            if not incident or incident.status != IncidentStatus.VERIFYING:
                return incident

            sample = await self.sim.latest()
            if not sample:
                return incident

            now = time.time()
            v_state = self.verifier.evaluate(incident.verification, sample, now)
            incident.verification = v_state

            if v_state.status == "PASSED":
                incident.status = IncidentStatus.RESOLVED
                incident.resolution = v_state.detail
                incident.closed_at = now
                await self.store.save_incident(incident)
                await self._audit(
                    incident.id,
                    actor="verifier",
                    event_type="INCIDENT_RESOLVED",
                    summary=f"Deterministic verifier confirmed recovery: {v_state.detail}",
                )
            elif v_state.status == "FAILED":
                incident.status = IncidentStatus.ESCALATED
                incident.escalation = f"Recovery verification failed: {v_state.detail}"
                await self.store.save_incident(incident)
                await self._audit(
                    incident.id,
                    actor="verifier",
                    event_type="VERIFICATION_FAILED",
                    summary=incident.escalation,
                )
            else:
                await self.store.save_incident(incident)

            return incident

    # ------------------------------------------------------------------ Site Knowledge
    async def teach_site_knowledge(self, asset: str, text: str, author: str = "field-operator") -> SiteKnowledge:
        """Record explicit operator assertion into durable Site Knowledge."""
        item = SiteKnowledge(asset=asset, text=text, author=author)
        await self.store.add_knowledge(item)
        await self._audit(
            None,
            actor="operator",
            event_type="SITE_KNOWLEDGE_RECORDED",
            summary=f"Operator taught site fact for {asset}: '{text}'",
        )
        return item

