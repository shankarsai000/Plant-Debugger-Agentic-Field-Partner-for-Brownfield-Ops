"""Firestore-backed field-ops store (Google Cloud submission mode)."""

from __future__ import annotations

from typing import Any

from google.cloud import firestore

from ..models import AuditEvent, Evidence, FieldTask, Incident, SiteKnowledge
from .base import FieldStore, audit_hash


class FirestoreFieldStore(FieldStore):
    def __init__(self, project: str, database: str = "(default)", prefix: str = "pd_"):
        self.db = firestore.AsyncClient(project=project or None, database=database)
        self.p = prefix

    def _c(self, name: str):
        return self.db.collection(f"{self.p}{name}")

    # incidents --------------------------------------------------------------
    async def get_incident(self, incident_id: str) -> Incident | None:
        snap = await self._c("incidents").document(incident_id).get()
        return Incident.model_validate(snap.to_dict()) if snap.exists else None

    async def save_incident(self, incident: Incident) -> None:
        await self._c("incidents").document(incident.id).set(incident.model_dump(mode="json"))

    async def list_incidents(self, limit: int = 20) -> list[Incident]:
        q = self._c("incidents").order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)
        return [Incident.model_validate(s.to_dict()) async for s in q.stream()]

    # tasks ------------------------------------------------------------------
    async def get_task(self, task_id: str) -> FieldTask | None:
        snap = await self._c("tasks").document(task_id).get()
        return FieldTask.model_validate(snap.to_dict()) if snap.exists else None

    async def save_task(self, task: FieldTask) -> None:
        await self._c("tasks").document(task.id).set(task.model_dump(mode="json"))

    async def list_tasks(self, incident_id: str) -> list[FieldTask]:
        q = self._c("tasks").where(filter=firestore.FieldFilter("incident_id", "==", incident_id))
        items = [FieldTask.model_validate(s.to_dict()) async for s in q.stream()]
        return sorted(items, key=lambda t: t.created_at)

    async def find_task_by_key(self, idempotency_key: str) -> FieldTask | None:
        q = self._c("tasks").where(filter=firestore.FieldFilter("idempotency_key", "==", idempotency_key)).limit(1)
        async for s in q.stream():
            return FieldTask.model_validate(s.to_dict())
        return None

    # evidence ---------------------------------------------------------------
    async def save_evidence(self, evidence: Evidence) -> None:
        await self._c("evidence").document(evidence.id).set(evidence.model_dump(mode="json"))

    async def get_evidence(self, evidence_id: str) -> Evidence | None:
        snap = await self._c("evidence").document(evidence_id).get()
        return Evidence.model_validate(snap.to_dict()) if snap.exists else None

    async def list_evidence(self, incident_id: str) -> list[Evidence]:
        q = self._c("evidence").where(filter=firestore.FieldFilter("incident_id", "==", incident_id))
        items = [Evidence.model_validate(s.to_dict()) async for s in q.stream()]
        return sorted(items, key=lambda e: e.created_at)

    # site knowledge ----------------------------------------------------------
    async def add_knowledge(self, item: SiteKnowledge) -> None:
        await self._c("knowledge").document(item.id).set(item.model_dump(mode="json"))

    async def list_knowledge(self, active_only: bool = True) -> list[SiteKnowledge]:
        items = [SiteKnowledge.model_validate(s.to_dict()) async for s in self._c("knowledge").stream()]
        items = [i for i in items if i.active or not active_only]
        return sorted(items, key=lambda i: i.created_at)

    async def deactivate_knowledge(self) -> int:
        n = 0
        q = self._c("knowledge").where(filter=firestore.FieldFilter("active", "==", True))
        async for s in q.stream():
            await s.reference.update({"active": False})
            n += 1
        return n

    # audit ------------------------------------------------------------------
    async def append_audit(self, event: AuditEvent) -> AuditEvent:
        head_ref = self._c("meta").document("audit_head")
        audit_coll = self._c("audit")
        transaction = self.db.transaction()

        @firestore.async_transactional
        async def _txn(txn):
            head = await head_ref.get(transaction=txn)
            data = head.to_dict() if head.exists else {"seq": 0, "hash": "GENESIS"}
            event.seq = int(data["seq"]) + 1
            event.prev_hash = data["hash"]
            event.hash = audit_hash(event.prev_hash, event)
            txn.set(audit_coll.document(f"{event.seq:09d}"), event.model_dump(mode="json"))
            txn.set(head_ref, {"seq": event.seq, "hash": event.hash})

        await _txn(transaction)
        return event

    async def list_audit(self, incident_id: str | None = None, limit: int = 200) -> list[AuditEvent]:
        q = self._c("audit")
        if incident_id:
            q = q.where(filter=firestore.FieldFilter("incident_id", "==", incident_id))
        q = q.order_by("seq", direction=firestore.Query.DESCENDING).limit(limit)
        rows = [AuditEvent.model_validate(s.to_dict()) async for s in q.stream()]
        return list(reversed(rows))

    # misc -------------------------------------------------------------------
    async def claim_message(self, message_id: str) -> bool:
        ref = self._c("messages").document(message_id)
        try:
            await ref.create({"claimed": True, "ts": firestore.SERVER_TIMESTAMP})
            return True
        except Exception as exc:  # AlreadyExists
            if exc.__class__.__name__ == "AlreadyExists":
                return False
            raise

    async def get_meta(self, key: str) -> Any:
        snap = await self._c("meta").document(key).get()
        return snap.to_dict().get("value") if snap.exists else None

    async def set_meta(self, key: str, value: Any) -> None:
        await self._c("meta").document(key).set({"value": value})

