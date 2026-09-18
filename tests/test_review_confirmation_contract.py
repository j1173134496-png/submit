from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from submit_flow_agent.field_mapper import REQUIRED_RECOGNIZED_FIELDS
from submit_flow_agent.ocr.base import OcrPage, OcrRawResult
from submit_flow_agent.ocr.local_stub import LocalSampleOcrAdapter
from submit_flow_agent.review import build_review_report
from submit_flow_agent.review_contract import (
    allowed_field_types,
    confirmable_fields_for_issue,
    load_review_confirmation_contract,
    validate_confirmed_data,
)
from submit_flow_agent.task_service import REQUIRED_FILE_TYPES, TaskServiceError, add_task_file, confirm_task, create_task, run_task
from submit_flow_agent.task_store import load_task, write_task
from submit_flow_agent.validator import CheckResult, ValidationReport
from tests.pricing_helpers import create_task_with_pricing as create_task


PROJECT_DIR = Path("深圳新安中学高中部650KW分布式光伏项目")
TASK_ID = "mvp019_review_contract_test"


class ReviewConfirmationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._ocr_patcher = patch("submit_flow_agent.pipeline.create_ocr_adapter", return_value=LocalSampleOcrAdapter())
        self._ocr_patcher.start()
        self._intake_classifier_patcher = patch("submit_flow_agent.task_service._classify_pdf_intake_batch", side_effect=_classify_by_filename)
        self._intake_classifier_patcher.start()

    def tearDown(self) -> None:
        self._intake_classifier_patcher.stop()
        self._ocr_patcher.stop()

    def test_contract_covers_all_recognized_fields_and_expected_types(self) -> None:
        types = allowed_field_types()

        self.assertEqual(set(types), set(REQUIRED_RECOGNIZED_FIELDS) - {"effective_unit_price"})
        self.assertEqual(types["grid_fee"], ("number",))
        self.assertEqual(types["month"], ("string",))
        self.assertEqual(load_review_confirmation_contract()["contract_version"], "1.0")

    def test_confirmable_fields_cover_low_confidence_missing_and_business_checks(self) -> None:
        self.assertEqual(confirmable_fields_for_issue("grid_fee", "low_confidence"), ["grid_fee"])
        self.assertEqual(confirmable_fields_for_issue("meter_no", "field_missing"), ["meter_no"])
        self.assertEqual(
            confirmable_fields_for_issue("generation_formula", "business_validation_failed"),
            ["previous_reading", "current_reading", "multiplier", "total_generation_kwh"],
        )
        self.assertEqual(
            confirmable_fields_for_issue("school_consumption_non_negative", "business_validation_failed"),
            ["total_generation_kwh", "grid_export_kwh"],
        )
        self.assertEqual(
            confirmable_fields_for_issue("month_period", "business_validation_failed"),
            ["month", "period_start", "period_end"],
        )
        self.assertEqual(confirmable_fields_for_issue("school_fee", "business_validation_failed"), [])

    def test_review_report_adds_business_validation_confirmable_fields(self) -> None:
        report = ValidationReport(
            site_key="xinan_high_school",
            site_name="新安高中部",
            month="2026-05",
            status="fail",
            can_write_excel=False,
            normalized_data={},
            checks=[CheckResult(name="grid_fee", status="fail", message="上网电费金额差异超过容差。")],
                errors=["上网电费金额差异超过容差。"],
        )
        review = build_review_report(
            month="2026-05",
            recognized_data_path=Path("missing-recognized.json"),
            validation_report_path=Path("validation_report.json"),
            validation_report=report,
        )

        self.assertEqual(review["issues"][0]["confirmable_fields"], ["grid_export_kwh", "grid_price", "grid_fee"])

    def test_confirm_task_rejects_invalid_confirmations_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            task_root = _create_need_review_task(runtime)
            valid = _confirmed_data({"grid_fee": {"value": 407.29}})
            cases = {
                "unknown": _confirmed_data({"unknown_field": {"value": 1}}),
                "unrelated": _confirmed_data({"total_generation_kwh": {"value": 49568}}),
                "wrong_type": _confirmed_data({"grid_fee": {"value": "407.29"}}),
                "missing_actor": {"confirmed_at": "2026-07-14T10:00:00+08:00", "fields": valid["fields"]},
                "empty_fields": _confirmed_data({}),
            }
            for name, payload in cases.items():
                with self.subTest(name=name):
                    source = root / f"{name}.json"
                    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    before = _task_snapshot(task_root)
                    with self.assertRaisesRegex(TaskServiceError, "Invalid confirmation request"):
                        confirm_task(runtime, TASK_ID, source)
                    self.assertEqual(_task_snapshot(task_root), before)

    def test_confirm_task_rejects_non_need_review_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            source = root / "confirmed.json"
            source.write_text(json.dumps(_confirmed_data({"grid_fee": {"value": 407.29}}), ensure_ascii=False), encoding="utf-8")
            task_root = runtime / "tasks" / TASK_ID
            before = _task_snapshot(task_root)

            with self.assertRaisesRegex(TaskServiceError, "requires task status need_review"):
                confirm_task(runtime, TASK_ID, source)

            self.assertEqual(_task_snapshot(task_root), before)

    def test_confirm_task_rejects_repeat_confirmation_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            task_root = _create_need_review_task(runtime)
            source = root / "confirmed.json"
            source.write_text(json.dumps(_confirmed_data({"grid_fee": {"value": 407.29}}), ensure_ascii=False), encoding="utf-8")

            with patch.object(LocalSampleOcrAdapter, "recognize", _low_grid_fee_recognize):
                completed = confirm_task(runtime, TASK_ID, source)
            self.assertEqual(completed.task["status"], "completed")
            before = _task_snapshot(task_root)

            with self.assertRaisesRegex(TaskServiceError, "requires task status need_review"):
                confirm_task(runtime, TASK_ID, source)
            self.assertEqual(_task_snapshot(task_root), before)

    def test_confirmed_data_rejects_boolean_for_number_field(self) -> None:
        with self.assertRaisesRegex(Exception, "JSON type: number"):
            validate_confirmed_data(_confirmed_data({"grid_fee": {"value": True}}), allowed_fields={"grid_fee"})

    def test_confirmed_data_accepts_multiple_business_check_fields(self) -> None:
        payload = _confirmed_data(
            {
                "previous_reading": {"value": 816.82},
                "current_reading": {"value": 1147.27},
                "multiplier": {"value": 150},
                "total_generation_kwh": {"value": 49568},
            }
        )

        validated = validate_confirmed_data(payload, allowed_fields=set(payload["fields"]))
        self.assertEqual(validated, payload)

    def test_confirm_task_rejects_invalid_or_escaping_report_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            task_root = _create_need_review_task(runtime)
            source = root / "confirmed.json"
            source.write_text(json.dumps(_confirmed_data({"grid_fee": {"value": 407.29}}), ensure_ascii=False), encoding="utf-8")
            report_path = task_root / "outputs" / "新安高中部" / "2026-05" / "review_report.json"

            report_path.write_text("{", encoding="utf-8")
            before_invalid = _task_snapshot(task_root)
            with self.assertRaisesRegex(TaskServiceError, "Invalid confirmation request"):
                confirm_task(runtime, TASK_ID, source)
            self.assertEqual(_task_snapshot(task_root), before_invalid)

            task = load_task(task_root)
            task["review_report"] = "../outside.json"
            write_task(task_root, task)
            before_escape = _task_snapshot(task_root)
            with self.assertRaisesRegex(TaskServiceError, "relative path inside the task directory"):
                confirm_task(runtime, TASK_ID, source)
            self.assertEqual(_task_snapshot(task_root), before_escape)


def _classify_by_filename(
    root: Path,
    staged_files: list[dict[str, object]],
    *,
    expected_month: str,
) -> dict[str, object]:
    files: dict[str, str] = {role: "" for role in REQUIRED_FILE_TYPES}
    role_results: list[dict[str, object]] = []
    for staged in staged_files:
        name = str(staged.get("original_filename") or "")
        stored = str(staged.get("stored_file") or "")
        if "发电单" in name:
            role = "generation_statement"
        elif "电费" in name:
            role = "fee_statement"
        elif "电量" in name:
            role = "energy_statement"
        else:
            role = "unknown"
        role_results.append({"stored_file": stored, "original_filename": name, "sha256": staged.get("sha256"), "role": role, "classification_source": "test_filename", "confidence": "high" if role in REQUIRED_FILE_TYPES else "low"})
        if role in REQUIRED_FILE_TYPES:
            files[role] = stored
    missing = [role for role in REQUIRED_FILE_TYPES if not files[role]]
    return {
        "batch_state": "ready" if not missing else "need_review",
        "role_results": role_results,
        "missing_roles": missing,
        "conflicting_roles": [],
        "next_allowed_action": "run_task" if not missing else "operator_review_pdf_roles",
        "files": files if not missing else {role: None for role in REQUIRED_FILE_TYPES},
    }
def _create_need_review_task(runtime: Path) -> Path:
    create_task(runtime, task_id=TASK_ID, month="2026-05")
    for filename in ("福域发电单202605-2.pdf", "福域电费结算单202605_1.pdf", "福域电量结算单202605_1-2.pdf"):
        add_task_file(runtime, TASK_ID, PROJECT_DIR / filename)
    with patch.object(LocalSampleOcrAdapter, "recognize", _low_grid_fee_recognize):
        result = run_task(runtime, TASK_ID)
    if result.task["status"] != "need_review":
        raise AssertionError(f"Expected need_review, got {result.task['status']!r}")
    return result.task_dir


def _confirmed_data(fields: dict[str, object]) -> dict[str, object]:
    return {
        "confirmed_by": "operator_001",
        "confirmed_at": "2026-07-14T10:00:00+08:00",
        "fields": fields,
    }


def _task_snapshot(task_root: Path) -> tuple[str, str, bool]:
    task_json = (task_root / "task.json").read_text(encoding="utf-8")
    events = (task_root / "audit" / "events.jsonl").read_text(encoding="utf-8")
    return task_json, events, (task_root / "confirmed_data.json").exists()


def _low_grid_fee_recognize(self: LocalSampleOcrAdapter, source_file: Path, pages: list[object]) -> OcrRawResult:
    result = _ORIGINAL_RECOGNIZE(self, source_file, pages)
    return OcrRawResult(
        source_file=result.source_file,
        pages=[
            OcrPage(
                page=page.page,
                image_path=page.image_path,
                blocks=[replace(block, confidence=0.80) if "上网电费" in block.text else block for block in page.blocks],
            )
            for page in result.pages
        ],
    )


_ORIGINAL_RECOGNIZE = LocalSampleOcrAdapter.recognize


if __name__ == "__main__":
    unittest.main()









