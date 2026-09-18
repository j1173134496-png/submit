from __future__ import annotations

import json
import shutil
import shutil
import shutil
import shutil
import shutil
import shutil
import shutil
import shutil
import shutil
import shutil
import shutil
import shutil
import tempfile
import unittest
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from submit_flow_agent.excel_writer import ExcelWriteError, write_summary_excel
from submit_flow_agent.field_mapper import build_manual_baseline_recognized_data
from submit_flow_agent.normalizer import normalize_recognized_data
from submit_flow_agent.validator import validate_monthly_data
from tests.pricing_helpers import static_pricing_snapshot


PROJECT_DIR = Path("深圳新安中学高中部650KW分布式光伏项目")
TEMPLATE = PROJECT_DIR / "汇总表格.xlsx"
CONTROLLED_TEMPLATE = Path("config/templates/xinan_high_school_blank/汇总表格.xlsx")
MONTH_NUMBER_FORMAT = 'yyyy"年"m"月"'


class SummaryExcelWriterTests(unittest.TestCase):
    def test_controlled_template_is_blank_but_styled(self) -> None:
        workbook = load_workbook(CONTROLLED_TEMPLATE, data_only=False)
        worksheet = workbook["Sheet1"]

        for row in (4, 5, 6):
            self.assertTrue(all(worksheet.cell(row=row, column=column).value in (None, "") for column in range(1, 14)))
        self.assertTrue(any(worksheet.cell(row=4, column=column).has_style for column in range(2, 14)))
        workbook.close()

    def test_write_summary_excel_uses_blank_may_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            validation_path = root / "validation_report.json"
            output_path = root / "汇总表格_2026-05.xlsx"
            validation_path.write_text(json.dumps(_validation_report(), ensure_ascii=False), encoding="utf-8")
            original_size = TEMPLATE.stat().st_size

            result = write_summary_excel(validation_path, PROJECT_DIR, output_path, pricing_snapshot=static_pricing_snapshot())

            self.assertTrue(output_path.exists())
            self.assertEqual(result.month, "2026-05")
            self.assertEqual(result.sheet, "Sheet1")
            self.assertEqual(result.mode, "append")
            self.assertEqual(result.target_row, 6)
            self.assertEqual(TEMPLATE.stat().st_size, original_size)
            self.assertIn("B6", result.written_cells)
            self._assert_summary_row(output_path, 6)

    def test_write_summary_excel_preserves_history_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            validation_path = root / "validation_report.json"
            output_path = root / "汇总表格_2026-05.xlsx"
            validation_path.write_text(json.dumps(_validation_report(), ensure_ascii=False), encoding="utf-8")
            shutil.copy2(CONTROLLED_TEMPLATE, output_path)
            before_values, before_styles = _history_rows(output_path)

            write_summary_excel(validation_path, PROJECT_DIR, output_path, pricing_snapshot=static_pricing_snapshot())

            after_values, after_styles = _history_rows(output_path)
            self.assertEqual(after_values, before_values)

    def test_write_summary_excel_updates_existing_month_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project_dir = root / "project"
            project_dir.mkdir()
            template_copy = project_dir / TEMPLATE.name
            shutil.copy2(TEMPLATE, template_copy)
            workbook = load_workbook(template_copy, data_only=False)
            worksheet = workbook["Sheet1"]
            worksheet["B6"] = date(2026, 5, 1)
            worksheet["C6"] = 0
            workbook.save(template_copy)
            workbook.close()
            validation_path = root / "validation_report.json"
            output_path = root / "汇总表格_2026-05.xlsx"
            validation_path.write_text(json.dumps(_validation_report(), ensure_ascii=False), encoding="utf-8")

            result = write_summary_excel(validation_path, project_dir, output_path, pricing_snapshot=static_pricing_snapshot())

            self.assertEqual(result.target_row, 6)
            self.assertEqual(result.mode, "update")
            self._assert_summary_row(output_path, 6)

    def test_write_summary_excel_updates_existing_month_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project_dir = root / "project"
            project_dir.mkdir()
            template_copy = project_dir / TEMPLATE.name
            shutil.copy2(TEMPLATE, template_copy)
            workbook = load_workbook(template_copy, data_only=False)
            worksheet = workbook["Sheet1"]
            worksheet["B6"] = date(2026, 5, 1)
            worksheet["C6"] = 0
            workbook.save(template_copy)
            workbook.close()
            validation_path = root / "validation_report.json"
            output_path = root / "汇总表格_2026-05.xlsx"
            validation_path.write_text(json.dumps(_validation_report(), ensure_ascii=False), encoding="utf-8")

            result = write_summary_excel(validation_path, project_dir, output_path, pricing_snapshot=static_pricing_snapshot())

            self.assertEqual(result.target_row, 6)
            self.assertEqual(result.mode, "update")
            self._assert_summary_row(output_path, 6)

    def test_write_summary_excel_updates_existing_month_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project_dir = root / "project"
            project_dir.mkdir()
            template_copy = project_dir / TEMPLATE.name
            shutil.copy2(TEMPLATE, template_copy)
            workbook = load_workbook(template_copy, data_only=False)
            worksheet = workbook["Sheet1"]
            worksheet["B6"] = date(2026, 5, 1)
            worksheet["C6"] = 0
            workbook.save(template_copy)
            workbook.close()
            validation_path = root / "validation_report.json"
            output_path = root / "汇总表格_2026-05.xlsx"
            validation_path.write_text(json.dumps(_validation_report(), ensure_ascii=False), encoding="utf-8")

            result = write_summary_excel(
                validation_path,
                project_dir,
                output_path,
                pricing_snapshot=static_pricing_snapshot(),
            )

            self.assertEqual(result.target_row, 6)
            self.assertEqual(result.mode, "update")
            self._assert_summary_row(output_path, 6)

    def test_write_summary_excel_updates_existing_month_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project_dir = root / "project"
            project_dir.mkdir()
            template_copy = project_dir / TEMPLATE.name
            shutil.copy2(TEMPLATE, template_copy)
            workbook = load_workbook(template_copy, data_only=False)
            worksheet = workbook["Sheet1"]
            worksheet["B6"] = date(2026, 5, 1)
            worksheet["C6"] = 0
            workbook.save(template_copy)
            workbook.close()
            validation_path = root / "validation_report.json"
            output_path = root / "汇总表格_2026-05.xlsx"
            validation_path.write_text(json.dumps(_validation_report(), ensure_ascii=False), encoding="utf-8")

            result = write_summary_excel(validation_path, project_dir, output_path)

            self.assertEqual(result.target_row, 6)
            self.assertEqual(result.mode, "update")
            self._assert_summary_row(output_path, 6)

    def test_write_summary_excel_repeated_run_does_not_duplicate_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            validation_path = root / "validation_report.json"
            output_path = root / "汇总表格_2026-05.xlsx"
            validation_path.write_text(json.dumps(_validation_report(), ensure_ascii=False), encoding="utf-8")

            first = write_summary_excel(validation_path, PROJECT_DIR, output_path, pricing_snapshot=static_pricing_snapshot())
            second = write_summary_excel(validation_path, PROJECT_DIR, output_path, pricing_snapshot=static_pricing_snapshot())

            self.assertEqual(first.target_row, 6)
            self.assertEqual(first.mode, "append")
            self.assertEqual(second.target_row, 6)
            self.assertEqual(second.mode, "append")
            workbook = load_workbook(output_path, data_only=False)
            worksheet = workbook["Sheet1"]
            month_rows = [row for row in range(1, worksheet.max_row + 1) if _is_2026_05(worksheet.cell(row=row, column=2).value)]
            workbook.close()
            self.assertEqual(month_rows, [6])

    def test_write_summary_excel_blocks_failed_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            validation_path = root / "validation_report.json"
            output_path = root / "blocked.xlsx"
            report = _validation_report()
            report["status"] = "fail"
            report["can_write_excel"] = False
            report["errors"] = ["validation failed"]
            validation_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(ExcelWriteError, "blocks Excel writing"):
                write_summary_excel(validation_path, PROJECT_DIR, output_path)

            self.assertFalse(output_path.exists())

    def test_write_summary_excel_refuses_template_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            validation_path = Path(tmpdir) / "validation_report.json"
            validation_path.write_text(json.dumps(_validation_report(), ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(ExcelWriteError, "Refusing to overwrite"):
                write_summary_excel(validation_path, PROJECT_DIR, TEMPLATE, pricing_snapshot=static_pricing_snapshot())

    def _assert_summary_row(self, output_path: Path, row: int) -> None:
        workbook = load_workbook(output_path, data_only=False)
        worksheet = workbook["Sheet1"]
        self.assertTrue(_is_2026_05(worksheet[f"B{row}"].value))
        self.assertEqual(worksheet[f"B{row}"].number_format, MONTH_NUMBER_FORMAT)
        self.assertEqual(worksheet[f"C{row}"].value, 816.82)
        self.assertEqual(worksheet[f"D{row}"].value, 1147.27)
        self.assertEqual(worksheet[f"E{row}"].value, 150)
        self.assertEqual(worksheet[f"F{row}"].value, 49568)
        self.assertEqual(worksheet[f"G{row}"].value, 1680)
        self.assertEqual(worksheet[f"H{row}"].value, 0.242434)
        self.assertEqual(worksheet[f"I{row}"].value, f"=G{row}*H{row}")
        self.assertEqual(worksheet[f"J{row}"].value, f"=F{row}-G{row}")
        self.assertEqual(worksheet[f"K{row}"].value, 0.6998)
        self.assertEqual(worksheet[f"L{row}"].value, f"=J{row}*K{row}")
        self.assertEqual(worksheet[f"M{row}"].value, f"=L{row}+I{row}")
        workbook.close()


def _validation_report() -> dict[str, object]:
    recognized = build_manual_baseline_recognized_data(PROJECT_DIR).to_dict()
    report = validate_monthly_data(normalize_recognized_data(recognized)).to_dict()
    return deepcopy(report)


def _is_2026_05(value: object) -> bool:
    if isinstance(value, datetime):
        return value.year == 2026 and value.month == 5
    if isinstance(value, date):
        return value.year == 2026 and value.month == 5
    if isinstance(value, (int, float)):
        serial_date = datetime.fromordinal(datetime(1899, 12, 30).toordinal() + int(value))
        return serial_date.year == 2026 and serial_date.month == 5
    return False


def _history_rows(path: Path) -> tuple[list[list[object]], list[list[str]]]:
    workbook = load_workbook(path, data_only=False)
    worksheet = workbook["Sheet1"]
    values = [[worksheet.cell(row=row, column=column).value for column in range(1, 14)] for row in range(1, 3)]
    styles = [[worksheet.cell(row=row, column=column).style_id for column in range(1, 14)] for row in range(1, 3)]
    workbook.close()
    return values, [[str(style) for style in row] for row in styles]


if __name__ == "__main__":
    unittest.main()
