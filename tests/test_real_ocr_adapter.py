from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from submit_flow_agent.ocr.factory import OCR_ADAPTER_ENV, OcrAdapterConfigError, create_ocr_adapter
from submit_flow_agent.ocr.paddle import PaddleOcrAdapter, PaddleOcrError, _bounded_ocr_image, _build_engine
from submit_flow_agent.pdf_renderer import RenderedPage


class PaddleOcrAdapterTests(unittest.TestCase):
    def test_build_engine_uses_bounded_mobile_models(self) -> None:
        with patch("paddleocr.PaddleOCR", side_effect=lambda **_: print("model status")) as paddle_ocr:
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                _build_engine(lang="ch")

        options = paddle_ocr.call_args.kwargs
        self.assertEqual(options["text_detection_model_name"], "PP-OCRv5_mobile_det")
        self.assertEqual(options["text_recognition_model_name"], "PP-OCRv5_mobile_rec")
        self.assertEqual(options["text_recognition_batch_size"], 1)
        self.assertFalse(options["enable_mkldnn"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("model status", stderr.getvalue())

    def test_paddle_adapter_wraps_engine_result_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "page.png"
            Image.new("RGB", (120, 40), "white").save(image_path)
            source = Path(tmpdir) / "neutral.pdf"
            adapter = PaddleOcrAdapter(engine=_FakeEngine())

            result = adapter.recognize(source, [RenderedPage(source_file=source, page=1, image_path=image_path)])

            self.assertEqual(result.source_file, source)
            self.assertEqual(result.pages[0].page, 1)
            self.assertEqual(result.pages[0].image_path, image_path)
            self.assertEqual(len(result.pages[0].blocks), 2)
            self.assertEqual(result.pages[0].blocks[0].text, "上网电费 407.29 元")
            self.assertEqual(result.pages[0].blocks[0].confidence, 0.98)
            self.assertEqual(result.pages[0].blocks[0].bbox, (1.0, 2.0, 101.0, 22.0))

    def test_paddle_adapter_blank_image_returns_no_sample_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "blank.png"
            Image.new("RGB", (120, 40), "white").save(image_path)
            source = Path(tmpdir) / "neutral.pdf"
            adapter = PaddleOcrAdapter(engine=_EmptyEngine())

            result = adapter.recognize(source, [RenderedPage(source_file=source, page=1, image_path=image_path)])

            self.assertEqual(result.pages[0].blocks, [])

    def test_paddle_adapter_keeps_engine_status_off_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "page.png"
            Image.new("RGB", (120, 40), "white").save(image_path)
            source = Path(tmpdir) / "neutral.pdf"
            adapter = PaddleOcrAdapter(engine=_NoisyEngine())
            stdout = StringIO()
            stderr = StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                adapter.recognize(source, [RenderedPage(source_file=source, page=1, image_path=image_path)])

            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("prediction status", stderr.getvalue())

    def test_paddle_adapter_rejects_unreadable_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "broken.png"
            image_path.write_text("not an image", encoding="utf-8")
            source = Path(tmpdir) / "neutral.pdf"
            adapter = PaddleOcrAdapter(engine=_FakeEngine())

            with self.assertRaisesRegex(PaddleOcrError, "not readable"):
                adapter.recognize(source, [RenderedPage(source_file=source, page=1, image_path=image_path)])

    def test_bounded_ocr_image_keeps_small_image_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "small.png"
            Image.new("RGB", (120, 80), "white").save(image_path)

            bounded = _bounded_ocr_image(image_path, max_side=200, max_pixels=30_000)

            self.assertEqual(bounded, image_path)

    def test_bounded_ocr_image_resizes_only_ocr_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "large.png"
            Image.new("RGB", (300, 240), "white").save(image_path)

            bounded = _bounded_ocr_image(image_path, max_side=120, max_pixels=12_000)

            self.assertNotEqual(bounded, image_path)
            with Image.open(image_path) as original_image:
                self.assertEqual(original_image.size, (300, 240))
            with Image.open(bounded) as bounded_image:
                bounded_size = bounded_image.size
            self.assertLessEqual(max(bounded_size), 120)
            self.assertLessEqual(bounded_size[0] * bounded_size[1], 12_000)


class OcrAdapterFactoryTests(unittest.TestCase):
    def test_unknown_adapter_name_fails_closed(self) -> None:
        with self.assertRaisesRegex(OcrAdapterConfigError, "Unknown OCR adapter"):
            create_ocr_adapter("local_sample")

    def test_unknown_adapter_env_fails_closed(self) -> None:
        with patch.dict("os.environ", {OCR_ADAPTER_ENV: "missing"}):
            with self.assertRaisesRegex(OcrAdapterConfigError, "Unknown OCR adapter"):
                create_ocr_adapter()


class _FakeEngine:
    def predict(self, image_path: str) -> list[dict[str, object]]:
        return [
            {
                "res": {
                    "rec_texts": ["上网电费 407.29 元", "本期起始日期 2026-05-01"],
                    "rec_scores": [0.98, 0.97],
                    "rec_boxes": [
                        [_FloatLike(1), _FloatLike(2), _FloatLike(101), _FloatLike(22)],
                        [_FloatLike(1), _FloatLike(24), _FloatLike(180), _FloatLike(44)],
                    ],
                }
            }
        ]


class _FloatLike:
    def __init__(self, value: float) -> None:
        self.value = value

    def __float__(self) -> float:
        return float(self.value)


class _EmptyEngine:
    def predict(self, image_path: str) -> list[dict[str, object]]:
        return [{"res": {"rec_texts": [], "rec_scores": [], "rec_boxes": []}}]


class _NoisyEngine(_EmptyEngine):
    def predict(self, image_path: str) -> list[dict[str, object]]:
        print("prediction status")
        return super().predict(image_path)


if __name__ == "__main__":
    unittest.main()
