"""Evidence blob stores. Originals are private; integrity is pinned by SHA-256."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from abc import ABC, abstractmethod
from pathlib import Path


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class EvidenceIntegrityError(RuntimeError):
    pass


class EvidenceStore(ABC):
    @abstractmethod
    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Persist bytes and return a storage URI."""

    @abstractmethod
    async def get(self, uri: str) -> bytes: ...

    @abstractmethod
    async def view_url(self, uri: str, evidence_id: str) -> str:
        """URL a browser can use to view the evidence (signed URL on GCS)."""

    async def get_verified(self, uri: str, expected_sha256: str) -> bytes:
        data = await self.get(uri)
        if sha256_hex(data) != expected_sha256:
            raise EvidenceIntegrityError(f"SHA-256 mismatch for {uri}")
        return data


class LocalEvidenceStore(EvidenceStore):
    def __init__(self, root: Path):
        self.root = Path(root) / "evidence"
        self.root.mkdir(parents=True, exist_ok=True)

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return f"file://{path.resolve().as_posix()}"

    async def get(self, uri: str) -> bytes:
        path = Path(uri.removeprefix("file://"))
        return await asyncio.to_thread(path.read_bytes)

    async def view_url(self, uri: str, evidence_id: str) -> str:
        return f"/api/evidence/{evidence_id}/content"


class GcsEvidenceStore(EvidenceStore):
    def __init__(self, bucket: str, project: str = "", ttl_s: int = 900):
        from google.cloud import storage

        self.client = storage.Client(project=project or None)
        self.bucket = self.client.bucket(bucket)
        self.ttl_s = ttl_s

    def _blob(self, uri: str):
        name = uri.split(f"gs://{self.bucket.name}/", 1)[1]
        return self.bucket.blob(name)

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        blob = self.bucket.blob(key)
        await asyncio.to_thread(blob.upload_from_string, data, content_type=content_type, if_generation_match=0)
        return f"gs://{self.bucket.name}/{key}"

    async def get(self, uri: str) -> bytes:
        return await asyncio.to_thread(self._blob(uri).download_as_bytes)

    async def view_url(self, uri: str, evidence_id: str) -> str:
        blob = self._blob(uri)

        def _sign() -> str:
            # On Cloud Run there is no private key: sign via IAM using the runtime service account.
            import google.auth
            from google.auth.transport import requests as ga_requests

            creds, _ = google.auth.default()
            creds.refresh(ga_requests.Request())
            kwargs = {}
            sa_email = getattr(creds, "service_account_email", None)
            if sa_email and sa_email != "default":
                # IAM signBlob path (requires roles/iam.serviceAccountTokenCreator on itself)
                kwargs = {"service_account_email": sa_email, "access_token": creds.token}
            return blob.generate_signed_url(version="v4", expiration=dt.timedelta(seconds=self.ttl_s),
                                            method="GET", **kwargs)

        try:
            return await asyncio.to_thread(_sign)
        except Exception:
            # Signing unavailable (e.g. user credentials locally): stream through the API instead.
            return f"/api/evidence/{evidence_id}/content"
