"""JSON-file backed store for local development (single process)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from ..models import AuditEvent, Evidence, FieldTask, Incident, SiteKnowledge
from .base import FieldStore, audit_hash

_COLLECTIONS = ("incidents", "tasks", "evidence", "knowledge", "audit", "messages", "meta")


class JsonFieldStore(FieldStore):
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "field_state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._db: dict[str, Any] = {c: {} for c in _COLLECTIONS}
        self._db["audit"] = []
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self._db.update(loaded)

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._db, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    async def _put(self, coll: str, key: str, value: dict[str, Any]) -> None:
        async with self._lock:
            self._db[coll][key] = value
            self._flush()

    # incidents --------------------------------------------------------------
    async def get_incident(self, incident_id: str) -> Incident | None:
        raw = self._db["incidents"].get(incident_id)
        return Incident.model_validate(raw) if raw else None

    async def save_incident(self, incident: Incident) -> None:
        await self._put("incidents", incident.id, incident.model_dump(mode="json"))

    async def list_incidents(self, limit: int = 20) -> list[Incident]:
        items = sorted(self._db["incidents"].values(), key=lambda r: r["created_at"], reverse=True)
        return [Incident.model_validate(r) for r in items[:limit]]

    # tasks ------------------------------------------------------------------
    async def get_task(self, task_id: str) -> FieldTask | None:
        raw = self._db["tasks"].get(task_id)
        return FieldTask.model_validate(raw) if raw else None

    async def save_task(self, task: FieldTask) -> None:
        await self._put("tasks", task.id, task.model_dump(mode="json"))

    async def list_tasks(self, incident_id: str) -> list[FieldTask]:
        items = [r for r in self._db["tasks"].values() if r["incident_id"] == incident_id]
        return [FieldTask.model_validate(r) for r in sorted(items, key=lambda r: r["created_at"])]

    async def find_task_by_key(self, idempotency_key: str) -> FieldTask | None:
        for r in self._db["tasks"].values():
            if r["idempotency_key"] == idempotency_key:
                return FieldTask.model_validate(r)
        return None

    # evidence ---------------------------------------------------------------
    async def save_evidence(self, evidence: Evidence) -> None:
        await self._put("evidence", evidence.id, evidence.model_dump(mode="json"))

    async def get_evidence(self, evidence_id: str) -> Evidence | None:
        raw = self._db["evidence"].get(evidence_id)
        return Evidence.model_validate(raw) if raw else None

    async def list_evidence(self, incident_id: str) -> list[Evidence]:
        items = [r for r in self._db["evidence"].values() if r["incident_id"] == incident_id]
        return [Evidence.model_validate(r) for r in sorted(items, key=lambda r: r["created_at"])]

    # site knowledge ----------------------------------------------------------
    async def add_knowledge(self, item: SiteKnowledge) -> None:
        await self._put("knowledge", item.id, item.model_dump(mode="json"))

    async def list_knowledge(self, active_only: bool = True) -> list[SiteKnowledge]:
        items = sorted(self._db["knowledge"].values(), key=lambda r: r["created_at"])
        return [SiteKnowledge.model_validate(r) for r in items if r["active"] or not active_only]

    async def deactivate_knowledge(self) -> int:
        async with self._lock:
            n = 0
            for r in self._db["knowledge"].values():
                if r["active"]:
                    r["active"] = False
                    n += 1
            self._flush()
            return n

    # audit ------------------------------------------------------------------
    async def append_audit(self, event: AuditEvent) -> AuditEvent:
        async with self._lock:
            chain = self._db["audit"]
            prev = chain[-1]["hash"] if chain else "GENESIS"
            event.seq = (chain[-1]["seq"] + 1) if chain else 1
            event.prev_hash = prev
            event.hash = audit_hash(prev, event)
            chain.append(event.model_dump(mode="json"))
            self._flush()
            return event

    async def list_audit(self, incident_id: str | None = None, limit: int = 200) -> list[AuditEvent]:
        rows = [r for r in self._db["audit"] if incident_id is None or r.get("incident_id") == incident_id]
        return [AuditEvent.model_validate(r) for r in rows[-limit:]]

    # misc -------------------------------------------------------------------
    async def claim_message(self, message_id: str) -> bool:
        async with self._lock:
            if message_id in self._db["messages"]:
                return False
            self._db["messages"][message_id] = True
            self._flush()
            return True

    async def get_meta(self, key: str) -> Any:
        return self._db["meta"].get(key)

    async def set_meta(self, key: str, value: Any) -> None:
        await self._put("meta", key, value)

