from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from submit_flow_agent.cli import main
from submit_flow_agent.monthly_regression import (
    BLOCKED_BY_SAMPLE,
    NOT_RUN,
    PASSED,
    load_monthly_regression_manifest,
    run_monthly_regression,
)
from submit_flow_agent.pipeline import RunMonthResult

GENERATION_PREFIX = "\u798f\u57df\u53d1\u7535\u5355"
FEE_PREFIX = "\u798f\u57df\u7535\u8d39\u7ed3\u7b97\u5355"
ENERGY_PREFIX = "\u798f\u57df\u7535\u91cf\u7ed3\u7b97\u5355"


class MonthlyRegressionTests(unittest.TestCase):
    def test_manifest_loads_cases_and_required_missing_month_sets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = _write_project(root, "202605")
            manifest = _write_manifest(root, project, required_count=2)

            loaded = load_monthly_regression_manifest(manifest)

            self.assertEqual(loaded.version, 1)
            self.assertEqual(loaded.cases[0].case_id, "case_2026_05")
            self.assertEqual(loaded.cases[0].month, "2026-05")
            self.assertEqual(len(loaded.required_sample_sets), 2)

    def test_dry_run_discovers_sample_and_reports_missing_real_months(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = _write_project(root, "202605")
            manifest = _write_manifest(root, project, required_count=2)
            matrix = root / "matrix.md"

            result = run_monthly_regression(manifest, output_base=root / "outputs", matrix_path=matrix, execute=False)

            self.assertEqual(result.rows[0].result, NOT_RUN)
            self.assertTrue(result.rows[0].input_complete)
            self.assertEqual(result.rows[0].detected_month, "2026-05")
            self.assertIn("input.generation_statement", result.rows[0].hashes)
            blocked = [row for row in result.rows if row.result == BLOCKED_BY_SAMPLE]
            self.assertEqual(len(blocked), 2)
            self.assertTrue(matrix.exists())
            matrix_text = matrix.read_text(encoding="utf-8")
            self.assertIn("BLOCKED_BY_SAMPLE", matrix_text)
            self.assertIn("does not claim real multi-month acceptance", matrix_text)

    def test_execute_uses_case_isolated_output_base_and_records_excel_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = _write_project(root, "202605")
            manifest = _write_manifest(root, project, required_count=0)
            output_base = root / "outputs"
            matrix = root / "matrix.md"

            result = run_monthly_regression(
                manifest,
                output_base=output_base,
                matrix_path=matrix,
                pipeline_runner=_fake_pipeline_runner,
            )

            row = result.rows[0]
            self.assertEqual(row.result, PASSED)
            self.assertEqual(row.pipeline_status, "pass")
            self.assertIn("case_2026_05", row.evidence_paths["output_dir"])
            self.assertTrue(row.detail_excel and Path(row.detail_excel).exists())
            self.assertTrue(row.summary_excel and Path(row.summary_excel).exists())
            self.assertEqual(row.key_cells["detail.A7"], "2026-07-17")
            self.assertEqual(row.key_cells["summary.M6"], "=L6+I6")
            self.assertIn("detail_excel", row.hashes)

    def test_required_sample_sets_consume_distinct_non_excluded_months(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            may_project = _write_project(root, "202605")
            june_project = _write_project(root, "202606")
            manifest = root / "manifest.json"
            payload = {
                "version": 1,
                "cases": [
                    {
                        "case_id": "case_2026_05",
                        "site_key": "xinan_high_school",
                        "month": "2026-05",
                        "project_dir": str(may_project),
                        "sample_status": "available",
                    },
                    {
                        "case_id": "case_2026_06",
                        "site_key": "xinan_high_school",
                        "month": "2026-06",
                        "project_dir": str(june_project),
                        "sample_status": "available",
                    },
                ],
                "required_sample_sets": [
                    {
                        "label": "missing_real_month_1",
                        "site_key": "xinan_high_school",
                        "exclude_months": ["2026-05"],
                        "required_file_types": ["generation_statement", "fee_statement", "energy_statement"],
                    },
                    {
                        "label": "missing_real_month_2",
                        "site_key": "xinan_high_school",
                        "exclude_months": ["2026-05"],
                        "required_file_types": ["generation_statement", "fee_statement", "energy_statement"],
                    },
                ],
            }
            manifest.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")

            result = run_monthly_regression(manifest, output_base=root / "outputs", matrix_path=root / "matrix.md", execute=False)

            blocked = [row for row in result.rows if row.result == BLOCKED_BY_SAMPLE]
            self.assertEqual(len(blocked), 1)
            self.assertEqual(blocked[0].case_id, "missing_real_month_2")
    def test_missing_required_pdf_is_blocked_by_sample_without_pipeline_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = _write_project(root, "202605", include_energy=False)
            manifest = _write_manifest(root, project, required_count=0)

            result = run_monthly_regression(
                manifest,
                output_base=root / "outputs",
                matrix_path=root / "matrix.md",
                pipeline_runner=_raising_pipeline_runner,
            )

            self.assertEqual(result.rows[0].result, BLOCKED_BY_SAMPLE)
            self.assertFalse(result.rows[0].input_complete)
            self.assertIn("Missing required source file types", result.rows[0].message)

    def test_manifest_month_mismatch_is_blocked_without_pipeline_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = _write_project(root, "202605")
            manifest = _write_manifest(root, project, month="2026-06", required_count=0)

            result = run_monthly_regression(
                manifest,
                output_base=root / "outputs",
                matrix_path=root / "matrix.md",
                pipeline_runner=_raising_pipeline_runner,
            )

            self.assertEqual(result.rows[0].result, BLOCKED_BY_SAMPLE)
            self.assertEqual(result.rows[0].detected_month, "2026-05")
            self.assertIn("does not match detected month", result.rows[0].message)

    def test_cli_dry_run_writes_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = _write_project(root, "202605")
            manifest = _write_manifest(root, project, required_count=1)
            matrix = root / "cli_matrix.md"

            code = main([
                "run-monthly-regression",
                "--manifest",
                str(manifest),
                "--output-base",
                str(root / "outputs"),
                "--matrix",
                str(matrix),
                "--dry-run",
            ])

            self.assertEqual(code, 0)
            self.assertTrue(matrix.exists())
            self.assertIn("case_2026_05", matrix.read_text(encoding="utf-8"))


def _write_project(root: Path, compact_month: str, *, include_energy: bool = True) -> Path:
    project = root / f"sample_project_{compact_month}"
    project.mkdir()
    (project / f"{GENERATION_PREFIX}{compact_month}-2.pdf").write_bytes(b"%PDF-1.4\n")
    (project / f"{FEE_PREFIX}{compact_month}_1.pdf").write_bytes(b"%PDF-1.4\n")
    if include_energy:
        (project / f"{ENERGY_PREFIX}{compact_month}_1-2.pdf").write_bytes(b"%PDF-1.4\n")
    return project


def _write_manifest(root: Path, project: Path, *, month: str = "2026-05", required_count: int) -> Path:
    payload = {
        "version": 1,
        "cases": [
            {
                "case_id": "case_2026_05",
                "site_key": "xinan_high_school",
                "month": month,
                "project_dir": str(project),
                "sample_status": "available",
            }
        ],
        "required_sample_sets": [
            {
                "label": f"missing_real_month_{index + 1}",
                "site_key": "xinan_high_school",
                "exclude_months": ["2026-05"],
                "required_file_types": ["generation_statement", "fee_statement", "energy_statement"],
            }
            for index in range(required_count)
        ],
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    return path


def _fake_pipeline_runner(project_dir: Path, *, site_key: str, output_base: Path, config_path: Path) -> RunMonthResult:
    output_dir = Path(output_base) / "site" / "2026-05"
    output_dir.mkdir(parents=True, exist_ok=True)
    recognized = output_dir / "recognized_data.json"
    validation = output_dir / "validation_report.json"
    process_log = output_dir / "process_log.txt"
    detail = output_dir / "detail.xlsx"
    summary = output_dir / "summary.xlsx"
    recognized.write_text(json.dumps({"fields": {}}, ensure_ascii=True), encoding="utf-8")
    validation.write_text(json.dumps({"status": "pass", "can_write_excel": True}, ensure_ascii=True), encoding="utf-8")
    process_log.write_text("run-month completed\n", encoding="utf-8")
    detail_wb = Workbook()
    detail_ws = detail_wb.active
    detail_ws.title = "1"
    detail_ws["A1"] = "title"
    detail_ws["E2"] = "2026-05-01"
    detail_ws["H2"] = "2026-06-01"
    detail_ws["A7"] = "2026-07-17"
    detail_wb.save(detail)
    detail_wb.close()
    summary_wb = Workbook()
    summary_ws = summary_wb.active
    summary_ws.title = "Sheet1"
    summary_ws["B6"] = "2026-05-01"
    summary_ws["F6"] = "=ROUND((D6-C6)*E6,0)"
    summary_ws["M6"] = "=L6+I6"
    summary_wb.save(summary)
    summary_wb.close()
    return RunMonthResult(
        site_key=site_key,
        site_name="site",
        month="2026-05",
        output_dir=output_dir,
        recognized_data=recognized,
        validation_report=validation,
        detail_workbook=detail,
        summary_workbook=summary,
        process_log=process_log,
        status="pass",
    )


def _raising_pipeline_runner(*args: object, **kwargs: object) -> RunMonthResult:
    raise AssertionError("pipeline must not run for invalid samples")


if __name__ == "__main__":
    unittest.main()
