"""Runtime configuration.

Submission mode (Google Cloud) runs fail-closed::

    DECISION_ENGINE=adk            ADK_FALLBACK_POLICY=false
    ADK_SESSION_BACKEND=firestore  VISION_ENGINE=gemini
    VISION_FALLBACK_POLICY=false   FIELD_STATE_BACKEND=firestore
    FIELD_EVIDENCE_BACKEND=gcs

Local development defaults to offline engines so the full loop can be exercised
without cloud credentials.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- engines ---------------------------------------------------------------
    decision_engine: Literal["adk", "local"] = "local"
    adk_fallback_policy: bool = False          # if True, ADK failure falls back to the local planner
    adk_session_backend: Literal["memory", "sqlite", "firestore"] = "sqlite"
    vision_engine: Literal["gemini", "local"] = "local"
    vision_fallback_policy: bool = False
    field_state_backend: Literal["json", "firestore"] = "json"
    field_evidence_backend: Literal["local", "gcs"] = "local"

    # --- Gemini / Vertex -------------------------------------------------------
    gemini_model: str = "gemini-2.5-flash"
    gemini_vision_model: str = "gemini-2.5-flash"
    google_cloud_project: str = ""
    google_cloud_location: str = "us-central1"
    google_genai_use_vertexai: bool = True

    # --- storage ---------------------------------------------------------------
    data_dir: Path = Path("./.data")
    gcs_evidence_bucket: str = ""
    firestore_database: str = "(default)"
    firestore_prefix: str = "pd_"
    signed_url_ttl_s: int = 900

    # --- simulator -------------------------------------------------------------
    sim_url: str = "http://127.0.0.1:8081"
    sim_auth: Literal["none", "token", "google"] = "none"
    sim_shared_token: str = ""

    # --- Pub/Sub push ----------------------------------------------------------
    pubsub_auth: Literal["none", "token", "oidc"] = "token"
    pubsub_audience: str = ""
    pubsub_service_account: str = ""

    # --- UI auth ---------------------------------------------------------------
    basic_auth_user: str = ""
    basic_auth_password: str = ""

    # --- deterministic safety boundaries ---------------------------------------
    max_field_tasks: int = 8
    max_manual_actions: int = 4
    max_agent_turns: int = 14
    bypass_max_step_pct: float = 25.0           # largest partial movement per task (full OPEN handled separately)
    action_settle_s: float = 12.0               # wait after an action before sampling the plant response
    max_photo_retries: int = 3

    # --- deterministic recovery verifier ---------------------------------------
    healthy_flow_min_m3h: float = 90.0
    healthy_flow_max_m3h: float = 115.0
    verify_window_s: float = 20.0
    verify_timeout_s: float = 75.0

    # --- demo ------------------------------------------------------------------
    enable_sim_camera: bool = True              # operator UI can grab a simulated field photo

    @property
    def submission_mode(self) -> bool:
        return (
            self.decision_engine == "adk"
            and not self.adk_fallback_policy
            and self.vision_engine == "gemini"
            and not self.vision_fallback_policy
            and self.field_state_backend == "firestore"
            and self.field_evidence_backend == "gcs"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

