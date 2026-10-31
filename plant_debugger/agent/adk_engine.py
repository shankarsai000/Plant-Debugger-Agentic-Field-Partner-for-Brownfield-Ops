"""Google Agent Development Kit (ADK) Dynamic Workflow host.

Runs Gemini on Vertex AI or Google GenAI API using ADK LlmAgent, LongRunningFunctionTool
for pause/resume across physical field steps, and persistent session storage
(DatabaseSessionService with SQLite or FirestoreSessionService).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.adk.sessions.database_session_service import DatabaseSessionService
from google.adk.tools.long_running_tool import LongRunningFunctionTool
from google.genai import types

from ..config import Settings
from ..models import Diagnosis, Hypothesis, TraceEntry
from ..safety.action_guard import ActionGuard, Proposal
from .context import FieldPlanningContext, PlannerDecision
from .prompts import DIAGNOSTIC_SYSTEM_INSTRUCTION, PLANNER_TURN_PROMPT

log = logging.getLogger("plant_debugger.adk")


def create_session_service(settings: Settings) -> BaseSessionService:
    backend = settings.adk_session_backend
    if backend == "firestore":
        from google.adk.integrations.firestore.firestore_session_service import FirestoreSessionService
        from google.cloud import firestore

        client = firestore.AsyncClient(project=settings.google_cloud_project or None,
                                       database=settings.firestore_database)
        return FirestoreSessionService(client=client, root_collection=f"{settings.firestore_prefix}adk_sessions")
    elif backend == "sqlite":
        db_path = settings.data_dir / "adk_sessions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{db_path.resolve().as_posix()}")
    else:
        from google.adk.sessions import InMemorySessionService
        return InMemorySessionService()


class AdkWorkflowHost:
    """Hosts the resumable diagnostic agent via ADK Runner."""

    def __init__(self, settings: Settings, session_service: BaseSessionService):
        self.s = settings
        self.session_service = session_service

    def _build_agent(self, on_field_task: Any, on_verification: Any, on_escalate: Any) -> LlmAgent:
        # Wrap request_field_task as a LongRunningFunctionTool
        lro_task_tool = LongRunningFunctionTool(on_field_task)

        tools = [lro_task_tool, on_verification, on_escalate]

        # Configure model & Vertex AI env if specified
        if self.s.google_cloud_project and self.s.google_genai_use_vertexai:
            os.environ["GOOGLE_CLOUD_PROJECT"] = self.s.google_cloud_project
            os.environ["GOOGLE_CLOUD_LOCATION"] = self.s.google_cloud_location
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"

        return LlmAgent(
            name="PlantDebuggerPlanner",
            description="Agentic brownfield diagnostic planner",
            model=self.s.gemini_model,
            instruction=DIAGNOSTIC_SYSTEM_INSTRUCTION,
            tools=tools,
        )

    async def execute_turn(
        self,
        session_id: str,
        user_message: types.Content | str,
        on_field_task: Any,
        on_verification: Any,
        on_escalate: Any,
    ) -> list[Event]:
        """Execute one run step of the ADK agent until a pause (LRO tool call) or completion."""
        agent = self._build_agent(on_field_task, on_verification, on_escalate)
        runner = Runner(
            agent=agent,
            session_service=self.session_service,
            auto_create_session=True,
        )

        new_msg = (
            user_message
            if isinstance(user_message, types.Content)
            else types.Content(role="user", parts=[types.Part(text=str(user_message))])
        )

        events: list[Event] = []
        async for event in runner.run_async(
            user_id="plant-operator",
            session_id=session_id,
            new_message=new_msg,
        ):
            events.append(event)
            # Log ADK event
            log.debug("ADK Event: author=%s lr_ids=%s", getattr(event, "author", None), getattr(event, "long_running_tool_ids", None))

        return events

