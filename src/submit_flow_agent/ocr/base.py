"""Base OCR adapter contracts and raw JSON structures."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from submit_flow_agent.pdf_renderer import RenderedPage


class OcrRawError(ValueError):
    """Raised when OCR raw JSON cannot be loaded."""


@dataclass(frozen=True)
class OcrBlock:
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "bbox": list(self.bbox),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class OcrPage:
    page: int
    image_path: Path
    blocks: list[OcrBlock] = field(default_factory=list)

    def to_dict(self, relative_to: Path | None = None) -> dict[str, object]:
        return {
            "page": self.page,
            "image_path": _display_path(self.image_path, relative_to),
            "blocks": [block.to_dict() for block in self.blocks],
        }


@dataclass(frozen=True)
class OcrRawResult:
    source_file: Path
    pages: list[OcrPage]

    def to_dict(self, relative_to: Path | None = None) -> dict[str, object]:
        return {
            "source_file": _display_path(self.source_file, relative_to),
            "pages": [page.to_dict(relative_to=relative_to) for page in self.pages],
        }


class OcrAdapter(Protocol):
    def recognize(self, source_file: Path, pages: list[RenderedPage]) -> OcrRawResult:
        """Return raw OCR structures for rendered PDF pages."""


def write_ocr_raw_result(
    result: OcrRawResult,
    output_path: Path | str,
    *,
    relative_to: Path | None = None,
) -> Path:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.to_dict(relative_to=relative_to), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def load_ocr_raw_result(path: Path | str, *, base_dir: Path | None = None) -> OcrRawResult:
    source = Path(path)
    if not source.exists():
        raise OcrRawError(f"OCR raw JSON does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise OcrRawError(f"OCR raw JSON is not valid JSON: {source}") from exc
    if not isinstance(payload, dict):
        raise OcrRawError("OCR raw JSON must contain an object.")
    return ocr_raw_result_from_dict(payload, base_dir=base_dir or source.parent)


def ocr_raw_result_from_dict(payload: dict[str, object], *, base_dir: Path | None = None) -> OcrRawResult:
    source_file = _path_from_text(_required_string(payload, "source_file"), base_dir)
    pages_payload = payload.get("pages")
    if not isinstance(pages_payload, list):
        raise OcrRawError("OCR raw JSON must contain a pages array.")

    pages: list[OcrPage] = []
    for page_payload in pages_payload:
        if not isinstance(page_payload, dict):
            raise OcrRawError("OCR raw page entries must be objects.")
        page_number = page_payload.get("page")
        if not isinstance(page_number, int):
            raise OcrRawError("OCR raw page must contain integer page.")
        image_path = _path_from_text(str(page_payload.get("image_path") or ""), base_dir)
        blocks_payload = page_payload.get("blocks")
        if not isinstance(blocks_payload, list):
            raise OcrRawError("OCR raw page must contain blocks array.")
        blocks = [_ocr_block_from_dict(block) for block in blocks_payload]
        pages.append(OcrPage(page=page_number, image_path=image_path, blocks=blocks))
    return OcrRawResult(source_file=source_file, pages=pages)


def _ocr_block_from_dict(payload: object) -> OcrBlock:
    if not isinstance(payload, dict):
        raise OcrRawError("OCR raw block entries must be objects.")
    text = _required_string(payload, "text")
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)):
        raise OcrRawError("OCR raw block must contain numeric confidence.")
    bbox_payload = payload.get("bbox")
    if not isinstance(bbox_payload, list) or len(bbox_payload) != 4:
        raise OcrRawError("OCR raw block must contain bbox with 4 numbers.")
    try:
        bbox = tuple(float(value) for value in bbox_payload)
    except (TypeError, ValueError) as exc:
        raise OcrRawError("OCR raw block bbox values must be numeric.") from exc
    return OcrBlock(text=text, bbox=bbox, confidence=float(confidence))


def _display_path(path: Path, relative_to: Path | None) -> str:
    if relative_to is None:
        return str(path)
    try:
        return str(path.relative_to(relative_to))
    except ValueError:
        return str(path)


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        raise OcrRawError(f"OCR raw JSON is missing '{key}'.")
    return str(value)


def _path_from_text(text: str, base_dir: Path | None) -> Path:
    path = Path(text)
    if path.is_absolute() or base_dir is None or text == "":
        return path
    return base_dir / path
