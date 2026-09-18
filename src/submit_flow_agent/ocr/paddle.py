"""PaddleOCR-backed real OCR adapter."""

from __future__ import annotations

import json
import math
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from submit_flow_agent.ocr.base import OcrBlock, OcrPage, OcrRawResult
from submit_flow_agent.pdf_renderer import RenderedPage


class PaddleOcrError(RuntimeError):
    """Raised when the real OCR engine cannot process an image."""


MAX_OCR_IMAGE_SIDE = 2200
MAX_OCR_IMAGE_PIXELS = 3_000_000
TEXT_DETECTION_MODEL = "PP-OCRv5_mobile_det"
TEXT_RECOGNITION_MODEL = "PP-OCRv5_mobile_rec"


class PaddleOcrAdapter:
    """Run PaddleOCR over rendered page images and return raw OCR blocks."""

    def __init__(self, *, engine: Any | None = None, lang: str = "ch") -> None:
        self._engine = engine or _build_engine(lang=lang)

    def recognize(self, source_file: Path, pages: list[RenderedPage]) -> OcrRawResult:
        ocr_pages: list[OcrPage] = []
        for page in pages:
            _verify_image(page.image_path)
            ocr_image_path = _bounded_ocr_image(page.image_path)
            try:
                # Paddle/PaddleX writes model status messages to stdout. Keep the
                # CLI stdout channel reserved for its JSON response contract.
                with redirect_stdout(sys.stderr):
                    result = self._engine.predict(str(ocr_image_path))
            except Exception as exc:  # pragma: no cover - exercised with real engine
                raise PaddleOcrError(f"PaddleOCR failed for image {page.image_path}: {exc}") from exc
            ocr_pages.append(
                OcrPage(
                    page=page.page,
                    image_path=ocr_image_path,
                    blocks=_blocks_from_result(result),
                )
            )
        return OcrRawResult(source_file=Path(source_file), pages=ocr_pages)


def _build_engine(*, lang: str) -> Any:
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:  # pragma: no cover - depends on optional runtime
        raise PaddleOcrError("PaddleOCR is not installed in the active Python environment.") from exc

    try:
        with redirect_stdout(sys.stderr):
            return PaddleOCR(
                lang=lang,
                text_detection_model_name=TEXT_DETECTION_MODEL,
                text_recognition_model_name=TEXT_RECOGNITION_MODEL,
                text_recognition_batch_size=1,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                text_rec_score_thresh=0.0,
                enable_mkldnn=False,
            )
    except Exception as exc:  # pragma: no cover - depends on optional runtime
        raise PaddleOcrError(f"Could not initialize PaddleOCR engine: {exc}") from exc


def _verify_image(path: Path) -> None:
    if not path.exists():
        raise PaddleOcrError(f"Rendered page image does not exist: {path}")
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, UnidentifiedImageError) as exc:
        raise PaddleOcrError(f"Rendered page image is not readable: {path}") from exc


def _bounded_ocr_image(
    path: Path,
    *,
    max_side: int = MAX_OCR_IMAGE_SIDE,
    max_pixels: int = MAX_OCR_IMAGE_PIXELS,
) -> Path:
    """Create a bounded OCR-only copy while preserving the rendered source image."""

    try:
        with Image.open(path) as image:
            width, height = image.size
            if max(width, height) <= max_side and width * height <= max_pixels:
                return path
            scale = min(max_side / max(width, height), math.sqrt(max_pixels / (width * height)))
            target_size = (max(1, round(width * scale)), max(1, round(height * scale)))
            bounded_path = path.with_name(f"{path.stem}.ocr-bounded.png")
            normalized = image.convert("RGB")
            normalized.resize(target_size, Image.Resampling.LANCZOS).save(bounded_path, format="PNG")
            return bounded_path
    except (OSError, UnidentifiedImageError) as exc:
        raise PaddleOcrError(f"Could not prepare bounded OCR image {path}: {exc}") from exc


def _blocks_from_result(result: Any) -> list[OcrBlock]:
    blocks: list[OcrBlock] = []
    for item in result or []:
        payload = _result_payload(item)
        texts = _list(payload.get("rec_texts"))
        scores = _list(payload.get("rec_scores"))
        boxes_value = payload.get("rec_boxes")
        if boxes_value is None:
            boxes_value = payload.get("rec_polys")
        boxes = _list(boxes_value)
        for index, text_value in enumerate(texts):
            text = str(text_value).strip()
            if not text:
                continue
            confidence = _float_at(scores, index, default=0.0)
            bbox = _bbox_at(boxes, index)
            blocks.append(OcrBlock(text=text, bbox=bbox, confidence=confidence))
    return blocks


def _result_payload(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        payload = item
    elif hasattr(item, "json"):
        payload = item.json
        if isinstance(payload, str):
            payload = json.loads(payload)
    elif hasattr(item, "to_json"):
        payload = item.to_json()
    else:
        payload = {}
    if not isinstance(payload, dict):
        return {}
    result = payload.get("res")
    return result if isinstance(result, dict) else payload


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        return list(value)
    except TypeError:
        return []


def _float_at(values: list[Any], index: int, *, default: float) -> float:
    try:
        return float(values[index])
    except (IndexError, TypeError, ValueError):
        return default


def _bbox_at(values: list[Any], index: int) -> tuple[float, float, float, float]:
    try:
        value = values[index]
    except IndexError:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        points = list(value)
    except TypeError:
        return (0.0, 0.0, 0.0, 0.0)
    if len(points) == 4:
        flat_box = _coerce_float_tuple(points)
        if flat_box is not None:
            x1, y1, x2, y2 = flat_box
            return (x1, y1, x2, y2)
    flattened: list[tuple[float, float]] = []
    for point in points:
        try:
            pair = list(point)[:2]
        except TypeError:
            continue
        flat_point = _coerce_float_tuple(pair)
        if flat_point is None or len(flat_point) != 2:
            continue
        x, y = flat_point
        flattened.append((x, y))
    if not flattened:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [point[0] for point in flattened]
    ys = [point[1] for point in flattened]
    return (min(xs), min(ys), max(xs), max(ys))


def _coerce_float_tuple(values: list[Any]) -> tuple[float, ...] | None:
    converted: list[float] = []
    for value in values:
        try:
            converted.append(float(value))
        except (TypeError, ValueError):
            return None
    return tuple(converted)
