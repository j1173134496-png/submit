from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from submit_flow_agent.ocr.local_stub import LocalStubOcrAdapter
from submit_flow_agent.pdf_renderer import (
    PdfRenderError,
    RenderedPage,
    _find_pdftoppm_from_path,
    render_monthly_source_to_images,
    render_pdf_to_images,
)


PROJECT_DIR = Path("深圳新安中学高中部650KW分布式光伏项目")
SAMPLE_PDFS = [
    "福域发电单202605-2.pdf",
    "福域电费结算单202605_1.pdf",
    "福域电量结算单202605_1-2.pdf",
]


class PdfRendererAndOcrTests(unittest.TestCase):
    def test_find_pdftoppm_accepts_windows_exe_from_path(self) -> None:
        with patch("submit_flow_agent.pdf_renderer.shutil.which", side_effect=[r"C:\\tools\\pdftoppm.exe"]):
            executable = _find_pdftoppm_from_path(windows=True)

        self.assertEqual(executable, Path(r"C:\\tools\\pdftoppm.exe"))

    def test_find_pdftoppm_accepts_linux_executable_without_extension(self) -> None:
        with patch("submit_flow_agent.pdf_renderer.shutil.which", return_value="/opt/poppler/bin/pdftoppm"):
            executable = _find_pdftoppm_from_path(windows=False)

        self.assertEqual(executable, Path("/opt/poppler/bin/pdftoppm"))

    def test_render_sample_pdfs_to_png_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            for filename in SAMPLE_PDFS:
                pages = render_pdf_to_images(PROJECT_DIR / filename, output_dir, dpi=120)

                self.assertGreaterEqual(len(pages), 1)
                self.assertEqual(pages[0].page, 1)
                self.assertTrue(pages[0].image_path.exists())
                self.assertEqual(pages[0].image_path.suffix.lower(), ".png")
                with Image.open(pages[0].image_path) as image:
                    self.assertGreater(image.width, 0)
                    self.assertGreater(image.height, 0)

    def test_render_missing_pdf_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(PdfRenderError, "does not exist"):
                render_pdf_to_images(Path("missing.pdf"), Path(tmpdir))

    def test_normalizes_jpg_and_png_as_single_ocr_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sources = [root / "电费结算单202605.jpg", root / "电量结算单202605.png"]
            Image.new("RGB", (24, 16), "white").save(sources[0], format="JPEG")
            Image.new("RGB", (24, 16), "white").save(sources[1], format="PNG")

            for source in sources:
                pages = render_monthly_source_to_images(source, root / "ocr" / source.stem)

                self.assertEqual(len(pages), 1)
                self.assertEqual(pages[0].source_file, source)
                self.assertEqual(pages[0].page, 1)
                self.assertEqual(pages[0].image_path.suffix.lower(), ".png")
                self.assertTrue(pages[0].image_path.is_file())

    def test_long_source_name_uses_stable_short_ocr_artifact_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / ("202605_沙井中学发电单." + "668a3173-6686-4c83-9406-7477937d42b6." * 4 + "jpg")
            Image.new("RGB", (24, 16), "white").save(source, format="JPEG")

            first = render_monthly_source_to_images(source, root / "ocr")[0]
            second = render_monthly_source_to_images(source, root / "ocr")[0]

            self.assertEqual(first.image_path, second.image_path)
            self.assertTrue(first.image_path.is_file())
            self.assertLessEqual(len(first.image_path.stem.rsplit("-", 1)[0]), 72)
            self.assertNotEqual(first.image_path.stem.rsplit("-", 1)[0], source.stem)

    def test_local_stub_outputs_raw_ocr_json_shape(self) -> None:
        source = PROJECT_DIR / "福域发电单202605-2.pdf"
        page = RenderedPage(
            source_file=source,
            page=1,
            image_path=Path("tmp/pdf_pages/福域发电单202605-2_p1.png"),
        )

        result = LocalStubOcrAdapter().recognize(source, [page])

        self.assertEqual(result.source_file, source)
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].page, 1)
        self.assertEqual(result.pages[0].blocks, [])
        self.assertEqual(
            result.to_dict(),
            {
                "source_file": str(source),
                "pages": [
                    {
                        "page": 1,
                        "image_path": str(page.image_path),
                        "blocks": [],
                    }
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
