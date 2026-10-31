"""Abstract state store + audit hash-chain helper."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from typing import Any

from ..models import AuditEvent, Evidence, FieldTask, Incident, SiteKnowledge


def audit_hash(prev_hash: str, event: AuditEvent) -> str:
    """SHA-256 over the previous hash and the canonical event body: tampering breaks the chain."""
    body = event.model_dump(exclude={"hash", "prev_hash"})
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


class FieldStore(ABC):
    # incidents --------------------------------------------------------------
    @abstractmethod
    async def get_incident(self, incident_id: str) -> Incident | None: ...

    @abstractmethod
    async def save_incident(self, incident: Incident) -> None: ...

    @abstractmethod
    async def list_incidents(self, limit: int = 20) -> list[Incident]: ...

    # tasks ------------------------------------------------------------------
    @abstractmethod
    async def get_task(self, task_id: str) -> FieldTask | None: ...

    @abstractmethod
    async def save_task(self, task: FieldTask) -> None: ...

    @abstractmethod
    async def list_tasks(self, incident_id: str) -> list[FieldTask]: ...

    @abstractmethod
    async def find_task_by_key(self, idempotency_key: str) -> FieldTask | None: ...

    # evidence ---------------------------------------------------------------
    @abstractmethod
    async def save_evidence(self, evidence: Evidence) -> None: ...

    @abstractmethod
    async def get_evidence(self, evidence_id: str) -> Evidence | None: ...

    @abstractmethod
    async def list_evidence(self, incident_id: str) -> list[Evidence]: ...

    # site knowledge ----------------------------------------------------------
    @abstractmethod
    async def add_knowledge(self, item: SiteKnowledge) -> None: ...

    @abstractmethod
    async def list_knowledge(self, active_only: bool = True) -> list[SiteKnowledge]: ...

    @abstractmethod
    async def deactivate_knowledge(self) -> int: ...

    # audit ------------------------------------------------------------------
    @abstractmethod
    async def append_audit(self, event: AuditEvent) -> AuditEvent:
        """Assign seq/prev_hash/hash atomically and persist."""

    @abstractmethod
    async def list_audit(self, incident_id: str | None = None, limit: int = 200) -> list[AuditEvent]: ...

    # misc -------------------------------------------------------------------
    @abstractmethod
    async def claim_message(self, message_id: str) -> bool:
        """Return True the first time a Pub/Sub message id is seen (idempotent delivery)."""

    @abstractmethod
    async def get_meta(self, key: str) -> Any: ...

    @abstractmethod
    async def set_meta(self, key: str, value: Any) -> None: ...

