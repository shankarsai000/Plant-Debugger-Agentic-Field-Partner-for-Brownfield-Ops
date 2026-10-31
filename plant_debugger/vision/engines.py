"""Vision engines.

* GeminiVision  – Gemini multimodal with structured output, then deterministic post-checks
                  (asset tag must match, low confidence => UNCERTAIN).
* LocalVision   – offline dev engine; only understands simulated-camera PNGs (reads the
                  embedded `pd:sim-reading` chunk). Any other photo => UNCERTAIN.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Literal

from PIL import Image
from pydantic import BaseModel, Field

from ..config import Settings
from ..models import VisionResult

log = logging.getLogger("plant_debugger.vision")

Observable = Literal["dp_gauge", "valve_position"]
MIN_CONFIDENCE = 0.6


class VisionEngine(ABC):
    name: str

    @abstractmethod
    async def classify(self, image: bytes, content_type: str, observable: Observable, asset: str) -> VisionResult: ...


# --------------------------------------------------------------------------- local
class LocalVision(VisionEngine):
    name = "local"

    async def classify(self, image: bytes, content_type: str, observable: Observable, asset: str) -> VisionResult:
        reading = None
        try:
            img = Image.open(io.BytesIO(image))
            raw = img.info.get("pd:sim-reading")
            reading = json.loads(raw) if raw else None
        except Exception:
            reading = None
        if not reading:
            return VisionResult(engine=self.name, observable=observable, status="UNCERTAIN", confidence=0.0,
                                rationale="Local vision engine can only read simulated-camera images. "
                                          "Set VISION_ENGINE=gemini to interpret real photos.")
        if reading.get("asset") != asset:
            return VisionResult(engine=self.name, observable=observable, status="UNCERTAIN", asset_visible=True,
                                asset_tag_read=reading.get("asset"), confidence=0.9,
                                rationale=f"Photo shows {reading.get('asset')}, but the task targets {asset}.")
        if observable == "dp_gauge":
            dp = float(reading["dp_bar"])
            status = "HIGH" if dp >= 0.8 else "NORMAL" if dp <= 0.6 else "UNCERTAIN"
            return VisionResult(engine=self.name, observable=observable, status=status, asset_visible=True,
                                asset_tag_read=asset, reading=f"~{dp:.1f} bar", confidence=0.95,
                                rationale=f"Needle at ~{dp:.1f} bar ({'red' if dp >= 1.0 else 'yellow' if dp > 0.6 else 'green'} zone).")
        pct = float(reading["opening_pct"])
        status = "OPEN" if pct >= 97 else "CLOSED" if pct <= 3 else "UNCERTAIN"
        return VisionResult(engine=self.name, observable=observable, status=status, asset_visible=True,
                            asset_tag_read=asset, reading=f"indicator ~{pct:.0f}% of travel", confidence=0.95,
                            rationale="Pointer at the marked OPEN stop." if status == "OPEN" else
                            "Pointer at CLOSED." if status == "CLOSED" else
                            f"Pointer between marks (~{pct:.0f}%); not at a mechanical end stop.")


# --------------------------------------------------------------------------- gemini
class _GaugeOut(BaseModel):
    asset_visible: bool = Field(description="True if a strainer/filter with a readable equipment tag is visible")
    asset_tag_read: str | None = Field(description="Equipment tag text exactly as read, e.g. 'F-17', or null")
    gauge_readable: bool
    needle_zone: Literal["GREEN", "YELLOW", "RED", "UNREADABLE"]
    approx_value: str | None = Field(description="Approximate gauge reading with units, or null")
    status: Literal["NORMAL", "HIGH", "UNCERTAIN"]
    confidence: float = Field(ge=0, le=1)
    rationale: str


class _ValveOut(BaseModel):
    asset_visible: bool
    asset_tag_read: str | None
    indicator_readable: bool
    approx_position_pct: float | None = Field(description="Estimated opening 0-100 from the mechanical indicator")
    status: Literal["OPEN", "CLOSED", "UNCERTAIN"]
    confidence: float = Field(ge=0, le=1)
    rationale: str


_GAUGE_PROMPT = """You are a strict industrial field-evidence classifier.
Task: classify the LOCAL differential-pressure (ΔP) gauge of strainer {asset}.
Rules:
- Read the equipment tag. If you cannot read a tag, set asset_tag_read=null.
- NORMAL: needle clearly in the green/normal band. HIGH: needle clearly in the red/high band (or clearly above the normal band).
- UNCERTAIN if the gauge face is not readable, the needle is ambiguous (e.g. yellow band boundary), the photo is blurred,
  or the photo does not show this asset. Never guess. Do not infer the cause of any fault.
Return JSON only."""

_VALVE_PROMPT = """You are a strict industrial field-evidence classifier.
Task: classify the PHYSICAL position of manual valve {asset} from its mechanical position indicator / handwheel stem.
Rules:
- Read the equipment tag. If you cannot read a tag, set asset_tag_read=null.
- OPEN only if the indicator is clearly at the marked OPEN end stop. CLOSED only if clearly at the CLOSED stop.
- UNCERTAIN for intermediate positions, unreadable indicators, blur, or if the photo does not show this asset. Never guess.
Return JSON only."""


class GeminiVision(VisionEngine):
    name = "gemini"

    def __init__(self, settings: Settings):
        from google import genai

        self.model = settings.gemini_vision_model
        if os.getenv("GOOGLE_API_KEY") and not settings.google_genai_use_vertexai:
            self.client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        else:
            self.client = genai.Client(vertexai=True, project=settings.google_cloud_project or None,
                                       location=settings.google_cloud_location)

    async def classify(self, image: bytes, content_type: str, observable: Observable, asset: str) -> VisionResult:
        from google.genai import types

        schema = _GaugeOut if observable == "dp_gauge" else _ValveOut
        prompt = (_GAUGE_PROMPT if observable == "dp_gauge" else _VALVE_PROMPT).format(asset=asset)
        resp = await self.client.aio.models.generate_content(
            model=self.model,
            contents=[types.Part.from_bytes(data=image, mime_type=content_type or "image/jpeg"), prompt],
            config=types.GenerateContentConfig(temperature=0.0, response_mime_type="application/json",
                                               response_schema=schema),
        )
        out = resp.parsed if isinstance(resp.parsed, schema) else schema.model_validate_json(resp.text)
        status = out.status
        notes = []
        # deterministic post-checks
        tag = (out.asset_tag_read or "").replace(" ", "").upper()
        if tag != asset.replace(" ", "").upper():
            status, notes = "UNCERTAIN", notes + [f"tag read '{out.asset_tag_read}' does not match {asset}"]
        if out.confidence < MIN_CONFIDENCE:
            status, notes = "UNCERTAIN", notes + [f"confidence {out.confidence:.2f} < {MIN_CONFIDENCE}"]
        reading = (out.approx_value if observable == "dp_gauge"
                   else (f"indicator ~{out.approx_position_pct:.0f}%" if out.approx_position_pct is not None else None))
        return VisionResult(engine=self.name, observable=observable, status=status, asset_visible=out.asset_visible,
                            asset_tag_read=out.asset_tag_read, reading=reading, confidence=out.confidence,
                            rationale=out.rationale + (f" [post-check: {'; '.join(notes)}]" if notes else ""))


class FallbackVision(VisionEngine):
    """Only used when VISION_FALLBACK_POLICY=true (never in submission mode)."""

    def __init__(self, primary: VisionEngine, fallback: VisionEngine):
        self.primary, self.fallback = primary, fallback
        self.name = f"{primary.name}+fallback"

    async def classify(self, image: bytes, content_type: str, observable: Observable, asset: str) -> VisionResult:
        try:
            return await self.primary.classify(image, content_type, observable, asset)
        except Exception as exc:
            log.warning("primary vision failed (%s); using fallback", exc)
            return await self.fallback.classify(image, content_type, observable, asset)


def build_vision(settings: Settings) -> VisionEngine:
    if settings.vision_engine == "local":
        return LocalVision()
    engine = GeminiVision(settings)
    return FallbackVision(engine, LocalVision()) if settings.vision_fallback_policy else engine


async def _selftest() -> None:  # pragma: no cover - manual helper
    from ..config import get_settings

    v = build_vision(get_settings())
    print(v.name)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_selftest())

