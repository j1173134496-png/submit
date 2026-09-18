"""Local OCR adapters for tests and offline development."""

from __future__ import annotations

from pathlib import Path

from submit_flow_agent.ocr.base import OcrBlock, OcrPage, OcrRawResult
from submit_flow_agent.pdf_renderer import RenderedPage


class LocalStubOcrAdapter:
    """Return structurally valid OCR JSON without doing text recognition."""

    def recognize(self, source_file: Path, pages: list[RenderedPage]) -> OcrRawResult:
        return OcrRawResult(
            source_file=Path(source_file),
            pages=[
                OcrPage(page=page.page, image_path=page.image_path, blocks=[])
                for page in pages
            ],
        )


class LocalSampleOcrAdapter:
    """Return deterministic raw OCR text blocks for the checked-in 2026-05 sample PDFs."""

    def recognize(self, source_file: Path, pages: list[RenderedPage]) -> OcrRawResult:
        source = Path(source_file)
        blocks = _sample_blocks(source.name)
        return OcrRawResult(
            source_file=source,
            pages=[
                OcrPage(
                    page=page.page,
                    image_path=page.image_path,
                    blocks=blocks if page.page == 1 else [],
                )
                for page in pages
            ],
        )


def _sample_blocks(filename: str) -> list[OcrBlock]:
    if "发电单" in filename:
        texts = [
            "电表编号 09001SF00000042508945216",
            "本期起始日期 2026-05-01",
            "本期结束日期 2026-06-01",
            "上期示数 816.82",
            "本期示数 1147.27",
            "倍率 150",
            "月总发电量 49568 kWh",
            "填表日期 2026-06-21",
        ]
    elif "电量" in filename:
        texts = [
            "结算月份 2026年05月",
            "上网电量 1680 kWh",
        ]
    elif "电费" in filename:
        texts = [
            "上网电价 0.242434 元/kWh",
            "上网电费 407.29 元",
        ]
    else:
        texts = []

    return [
        OcrBlock(
            text=text,
            bbox=(0.0, float(index * 24), 240.0, float(index * 24 + 20)),
            confidence=0.98,
        )
        for index, text in enumerate(texts)
    ]
