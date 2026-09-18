"""Controlled OCR adapter selection."""

from __future__ import annotations

import os

from submit_flow_agent.ocr.base import OcrAdapter


DEFAULT_OCR_ADAPTER = "paddle"
OCR_ADAPTER_ENV = "SUBMIT_FLOW_OCR_ADAPTER"


class OcrAdapterConfigError(RuntimeError):
    """Raised when OCR adapter configuration is invalid."""


def create_ocr_adapter(name: str | None = None) -> OcrAdapter:
    adapter_name = (name or os.environ.get(OCR_ADAPTER_ENV) or DEFAULT_OCR_ADAPTER).strip().lower()
    if adapter_name == "paddle":
        from submit_flow_agent.ocr.paddle import PaddleOcrAdapter

        return PaddleOcrAdapter()
    raise OcrAdapterConfigError(f"Unknown OCR adapter '{adapter_name}'.")
