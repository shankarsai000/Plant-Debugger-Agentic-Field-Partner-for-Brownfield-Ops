"""Client for the private plant-sim service."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger("plant_debugger.sim")


class SimClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.base = settings.sim_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=15)
        self._id_token: tuple[str, float] | None = None

    async def _headers(self) -> dict[str, str]:
        if self.s.sim_auth == "token" and self.s.sim_shared_token:
            return {"X-Sim-Token": self.s.sim_shared_token}
        if self.s.sim_auth == "google":
            import time

            if not self._id_token or self._id_token[1] < time.time() + 60:
                def _fetch() -> str:
                    from google.auth.transport.requests import Request
                    from google.oauth2 import id_token

                    return id_token.fetch_id_token(Request(), self.base)

                token = await asyncio.to_thread(_fetch)
                self._id_token = (token, time.time() + 3000)
            return {"Authorization": f"Bearer {self._id_token[0]}"}
        return {}

    async def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        r = await self._client.request(method, f"{self.base}{path}", headers=await self._headers(), **kw)
        r.raise_for_status()
        return r

    async def telemetry(self, last_n: int = 120) -> dict[str, Any]:
        return (await self._req("GET", "/sim/telemetry", params={"last_n": last_n})).json()

    async def latest(self) -> dict[str, Any] | None:
        return (await self.telemetry(1))["latest"]

    async def apply_action(self, action: dict[str, Any]) -> dict[str, Any]:
        return (await self._req("POST", "/sim/actions", json=action)).json()

    async def start_scenario(self, fault: str) -> dict[str, Any]:
        return (await self._req("POST", "/sim/scenario", json={"fault": fault})).json()

    async def reset(self) -> dict[str, Any]:
        return (await self._req("POST", "/sim/reset")).json()

    async def camera(self, asset: str) -> bytes:
        return (await self._req("GET", f"/sim/camera/{asset}")).content

    async def close(self) -> None:
        await self._client.aclose()


def summarize_window(samples: list[dict[str, Any]], last_n: int = 5) -> dict[str, Any]:
    """Compact, deterministic telemetry summary used as causal evidence."""
    window = samples[-last_n:]
    if not window:
        return {}
    avg = lambda k: round(sum(s[k] for s in window) / len(window), 2)  # noqa: E731
    return {
        "ft101_flow_m3h": avg("ft101_flow_m3h"),
        "p101_discharge_bar": avg("p101_discharge_bar"),
        "p101_vibration_mm_s": avg("p101_vibration_mm_s"),
        "p101_bearing_temp_c": avg("p101_bearing_temp_c"),
        "p101_motor_current_a": avg("p101_motor_current_a"),
        "pump_running": window[-1]["pump_running"],
        "samples": len(window),
        "ts": window[-1]["ts"],
    }

