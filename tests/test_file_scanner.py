from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from submit_flow_agent.file_scanner import (
    FileScanError,
    classify_source_file,
    classify_source_pdf,
    classify_pdf_text_content,
    detect_month,
    detect_semantic_months,
    scan_input_files,
)


class FileScannerTests(unittest.TestCase):
    def test_scans_mixed_pdf_jpg_png_monthly_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_pdf_text(root / "发电单202605.pdf", "发电单 电表编号 月总发电量")
            Image.new("RGB", (16, 16), "white").save(root / "电费结算单202605.jpg", format="JPEG")
            Image.new("RGB", (16, 16), "white").save(root / "电量结算单202605.png", format="PNG")

            result = scan_input_files(root, expected_month="2026-05")

            self.assertEqual(result.month, "2026-05")
            self.assertEqual(result.files["generation_statement"].suffix.lower(), ".pdf")
            self.assertEqual(result.files["fee_statement"].suffix.lower(), ".jpg")
            self.assertEqual(result.files["energy_statement"].suffix.lower(), ".png")

    def test_detect_month_from_compact_filename_month(self) -> None:
        self.assertEqual(detect_month("福域发电单202605-2.pdf"), "2026-05")

    def test_detect_month_from_separated_filename_months_with_suffixes(self) -> None:
        self.assertEqual(
            detect_month("兴围-电费结算单 2026.05.7f2b0185-5696-4788-9da7-a2937d7b92e0.pdf"),
            "2026-05",
        )
        self.assertEqual(
            detect_month("发电单_2026-06 (2).7f2b0185-5696-4788-9da7-a2937d7b92e0.pdf"),
            "2026-06",
        )

    def test_detect_month_from_chinese_filename_month(self) -> None:
        self.assertEqual(detect_month("福域电量结算单2026年5月_1-2.pdf"), "2026-05")
        self.assertEqual(detect_month("福域电量结算单2026年05月_1-2.pdf"), "2026-05")

    def test_reject_invalid_filename_month(self) -> None:
        self.assertIsNone(detect_month("福域发电单202613-2.pdf"))
        self.assertIsNone(detect_month("福域发电单2026-00.pdf"))
        self.assertIsNone(detect_month("福域发电单2026.13.pdf"))
        self.assertIsNone(detect_month("福域发电单2026年13月.pdf"))

    def test_reject_uuid_digits_without_document_month(self) -> None:
        self.assertIsNone(detect_month("福域发电单.a202605b-5696-4788-9da7-a2937d7b92e0.pdf"))

    def test_detect_semantic_month_only_from_business_label(self) -> None:
        self.assertEqual(detect_semantic_months("结算月份：2026年5月"), ["2026-05"])
        self.assertEqual(detect_semantic_months("本期起始日期 2026-05-01"), [])

    def test_classify_source_file_types(self) -> None:
        self.assertEqual(classify_source_file("福域发电单202605-2.pdf"), "generation_statement")
        self.assertEqual(classify_source_file("福域电费结算单202605_1.pdf"), "fee_statement")
        self.assertEqual(classify_source_file("福域电量结算单202605_1-2.pdf"), "energy_statement")
        self.assertIsNone(classify_source_file("汇总表格.xlsx"))

    def test_pdf_content_classification_overrides_swapped_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            swapped = self._write_pdf_text(
                root / "福域发电单202605-but-content-is-energy.pdf",
                "电量结算单\n上网电量 1680 kWh\n结算电量 1680 kWh",
            )

            self.assertEqual(classify_source_pdf(swapped), "energy_statement")

    def test_classifies_scanned_generation_account_vocabulary(self) -> None:
        result = classify_pdf_text_content(
            "尊敬的用户，以下是2026-05发电情况电子账单。发电项目户号 094607004414792。",
            source="ocr",
        )

        self.assertEqual(result.role, "generation_statement")
        self.assertEqual(result.confidence, "high")

    def test_scan_input_files_uses_pdf_content_when_filenames_are_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_pdf_text(root / "福域电量结算单202605-swapped.pdf", "发电单 电表编号 上期示数 本期示数 月总发电量")
            self._write_pdf_text(root / "福域发电单202605-swapped.pdf", "电费结算单 上网电价 上网电费 结算小计")
            self._write_pdf_text(root / "福域电费结算单202605-swapped.pdf", "电量结算单 上网电量 结算电量")

            result = scan_input_files(root)

            self.assertEqual(result.files["generation_statement"].name, "福域电量结算单202605-swapped.pdf")
            self.assertEqual(result.files["fee_statement"].name, "福域发电单202605-swapped.pdf")
            self.assertEqual(result.files["energy_statement"].name, "福域电费结算单202605-swapped.pdf")

    def test_scan_input_files_success_is_order_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._touch(root / "福域电量结算单202605_1-2.pdf")
            self._touch(root / "福域发电单202605-2.pdf")
            self._touch(root / "福域电费结算单202605_1.pdf")
            self._touch(root / "ignored.txt")

            result = scan_input_files(root)

            self.assertEqual(result.month, "2026-05")
            self.assertEqual(
                set(result.files),
                {"generation_statement", "fee_statement", "energy_statement"},
            )
            self.assertEqual(result.files["generation_statement"].name, "福域发电单202605-2.pdf")
            self.assertEqual(
                result.to_dict(relative_to=root),
                {
                    "month": "2026-05",
                    "files": {
                        "generation_statement": "福域发电单202605-2.pdf",
                        "fee_statement": "福域电费结算单202605_1.pdf",
                        "energy_statement": "福域电量结算单202605_1-2.pdf",
                    },
                },
            )

    def test_expected_month_allows_filenames_without_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_pdf_text(root / "发电单.pdf", "发电单 电表编号 月总发电量 结算月份：2026年5月")
            self._write_pdf_text(root / "电费结算单.pdf", "电费结算单 上网电价 账单月份：2026-05")
            self._write_pdf_text(root / "电量结算单.pdf", "电量结算单 上网电量 所属月份：2026.05")

            result = scan_input_files(root, expected_month="2026-05")

            self.assertEqual(result.month, "2026-05")

    def test_expected_month_uses_controlled_context_when_content_has_no_statement_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_pdf_text(root / "发电单.pdf", "发电单 电表编号 月总发电量")
            self._write_pdf_text(root / "电费结算单.pdf", "电费结算单 上网电价 上网电费")
            self._write_pdf_text(root / "电量结算单.pdf", "电量结算单 上网电量 结算电量")

            result = scan_input_files(root, expected_month="2026-05")

            self.assertEqual(result.month, "2026-05")

    def test_expected_month_rejects_semantic_content_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_pdf_text(root / "发电单.pdf", "发电单 电表编号 结算月份：2026年6月")
            self._write_pdf_text(root / "电费结算单.pdf", "电费结算单 上网电价 结算月份：2026年5月")
            self._write_pdf_text(root / "电量结算单.pdf", "电量结算单 上网电量 结算月份：2026年5月")

            with self.assertRaisesRegex(FileScanError, "PDF content months do not match"):
                scan_input_files(root, expected_month="2026-05")

    def test_expected_month_rejects_explicit_filename_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_pdf_text(root / "发电单202606.pdf", "发电单 电表编号 月总发电量")
            self._write_pdf_text(root / "电费结算单202605.pdf", "电费结算单 上网电价 上网电费")
            self._write_pdf_text(root / "电量结算单202605.pdf", "电量结算单 上网电量 结算电量")

            with self.assertRaisesRegex(FileScanError, "months do not match expected month"):
                scan_input_files(root, expected_month="2026-05")

    def test_missing_required_file_type_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._touch(root / "福域发电单202605-2.pdf")
            self._touch(root / "福域电费结算单202605_1.pdf")

            with self.assertRaisesRegex(FileScanError, "Missing required source file types"):
                scan_input_files(root)

    def test_duplicate_file_type_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._touch(root / "福域发电单202605-1.pdf")
            self._touch(root / "福域发电单202605-2.pdf")
            self._touch(root / "福域电费结算单202605_1.pdf")
            self._touch(root / "福域电量结算单202605_1-2.pdf")

            with self.assertRaisesRegex(FileScanError, "Duplicate source files found"):
                scan_input_files(root)

    def test_mismatched_months_raise_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._touch(root / "福域发电单202605-2.pdf")
            self._touch(root / "福域电费结算单202606_1.pdf")
            self._touch(root / "福域电量结算单202605_1-2.pdf")

            with self.assertRaisesRegex(FileScanError, "months do not match"):
                scan_input_files(root)

    def test_directory_must_exist(self) -> None:
        with self.assertRaisesRegex(FileScanError, "does not exist"):
            scan_input_files(Path("missing-directory"))

    @staticmethod
    def _touch(path: Path) -> None:
        path.write_bytes(b"%PDF-1.4\n")

    @staticmethod
    def _write_pdf_text(path: Path, text: str) -> Path:
        path.write_bytes(("%PDF-1.4\n" + text).encode("utf-8"))
        return path


if __name__ == "__main__":
    unittest.main()
