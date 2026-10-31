"""Planner contract: FieldPlanningContext in, PlannerDecision out.

Both the Gemini planner (ADK LlmAgent) and the offline heuristic planner implement
this contract, so the ADK workflow that hosts the loop is identical in both modes.
"""

