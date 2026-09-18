from __future__ import annotations

import json
import hashlib
import multiprocessing
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from submit_flow_agent.config import SiteConfig, load_site_config
from submit_flow_agent.ocr.base import OcrBlock, OcrPage, OcrRawResult
from submit_flow_agent.ocr.local_stub import LocalSampleOcrAdapter
from submit_flow_agent.task_service import (
    REQUIRED_FILE_TYPES,
    TaskServiceError,
    _copy_site_templates,
    _missing_field_retry_issues,
    _write_artifact_index_or_record_error,
    _promote_site_history,
    add_task_file,
    confirm_task,
    create_task,
    get_task,
    list_task_outputs,
    retry_review_task,
    run_task,
)
from submit_flow_agent.task_store import load_task, write_task
from submit_flow_agent.validator import ValidationReport
from tests.pricing_helpers import create_task_with_pricing as create_task


PROJECT_DIR = Path("深圳新安中学高中部650KW分布式光伏项目")
TASK_ID = "pv_xinan_high_school_2026_05_001"
FUYU_GENERATION = "\u798f\u57df\u53d1\u7535\u5355202605-2.pdf"
FUYU_FEE = "\u798f\u57df\u7535\u8d39\u7ed3\u7b97\u5355202605_1.pdf"
FUYU_ENERGY = "\u798f\u57df\u7535\u91cf\u7ed3\u7b97\u5355202605_1-2.pdf"
HUASHENGTENG_GENERATION = "\u534e\u76db\u817e\u53d1\u7535\u5355202605.pdf"
HUASHENGTENG_FEE = "\u534e\u76db\u817e\u7535\u8d39\u7ed3\u7b97\u5355202605.pdf"
HUASHENGTENG_ENERGY = "\u534e\u76db\u817e\u7535\u91cf\u7ed3\u7b97\u5355202605.pdf"


class TaskServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._ocr_patcher = patch("submit_flow_agent.pipeline.create_ocr_adapter", return_value=LocalSampleOcrAdapter())
        self._ocr_patcher.start()
        self._intake_classifier_patcher = patch("submit_flow_agent.task_service._classify_pdf_intake_batch", side_effect=_classify_by_filename)
        self._intake_classifier_patcher.start()

    def tearDown(self) -> None:
        self._intake_classifier_patcher.stop()
        self._ocr_patcher.stop()

    def test_create_task_writes_task_json_and_audit_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"

            task = create_task(runtime, task_id=TASK_ID, month="2026-05")

            task_root = runtime / "tasks" / TASK_ID
            self.assertEqual(task["status"], "collecting_files")
            self.assertTrue((task_root / "task.json").exists())
            self.assertTrue((task_root / "inputs").exists())
            self.assertTrue((task_root / "outputs").exists())
            self.assertTrue((task_root / "audit" / "events.jsonl").exists())
            events = _events(task_root)
            self.assertEqual(events[0]["event"], "task_created")

    def test_add_files_moves_task_to_ready_to_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")

            task = _add_all_files(runtime)

            self.assertEqual(task["status"], "ready_to_run")
            task_root = runtime / "tasks" / TASK_ID
            self.assertTrue(any(path.name.startswith("福域发电单202605-2.") for path in (task_root / "inputs" / "staged").glob("*.pdf")))
            self.assertTrue(all(task["files"].values()))
            event_names = [event["event"] for event in _events(task_root)]
            self.assertIn("file_received", event_names)
            self.assertIn("task_ready", event_names)

    def test_ready_to_run_allows_same_file_type_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            task = _add_all_files(runtime)
            self.assertEqual(task["status"], "ready_to_run")
            replacement = _write_pdf(root / "福域发电单202605-replacement.pdf")
            before = _task_snapshot(runtime / "tasks" / TASK_ID)

            with self.assertRaisesRegex(TaskServiceError, "exactly three unique files"):
                add_task_file(runtime, TASK_ID, replacement)

            self.assertEqual(_task_snapshot(runtime / "tasks" / TASK_ID), before)
    def test_concurrent_add_files_preserves_task_and_audit_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            sources = [
                PROJECT_DIR / "福域发电单202605-2.pdf",
                PROJECT_DIR / "福域电费结算单202605_1.pdf",
                PROJECT_DIR / "福域电量结算单202605_1-2.pdf",
            ]
            processes = [
                context.Process(target=_concurrent_add_file, args=(str(runtime), TASK_ID, str(source), start, results))
                for source in sources
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual([results.get(timeout=2) for _ in processes], [None, None, None])

            task_root = runtime / "tasks" / TASK_ID
            task = json.loads((task_root / "task.json").read_text(encoding="utf-8"))
            self.assertEqual(task["status"], "ready_to_run")
            self.assertTrue(all(task["files"].values()))
            events = _events(task_root)
            self.assertEqual(sum(event["event"] == "pdf_staged" for event in events), 3)
            self.assertEqual(sum(event["event"] == "task_ready" for event in events), 1)
    def test_run_task_completed_writes_outputs_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            _add_all_files(runtime)

            result = run_task(runtime, TASK_ID)

            self.assertEqual(result.task["status"], "completed")
            _assert_site_history_manifest(
                self,
                runtime / "site_history" / "xinan_high_school" / "2026-05",
                load_site_config("xinan_high_school"),
                "2026-05",
                TASK_ID,
            )
            site = load_site_config("xinan_high_school")
            self.assertIn(f"outputs/{site.site_name}/2026-05/recognized_data.json", result.task["outputs"])
            self.assertIn(f"outputs/{site.site_name}/2026-05/validation_report.json", result.task["outputs"])
            self.assertIsNone(result.task["review_report"])
            event_names = [event["event"] for event in _events(result.task_dir)]
            self.assertIn("run_started", event_names)
            self.assertIn("task_completed", event_names)

    def test_august_task_rejects_may_pdfs_before_copying_without_side_effects(self) -> None:
        for filename in (FUYU_GENERATION, FUYU_FEE, FUYU_ENERGY):
            with self.subTest(filename=filename):
                with tempfile.TemporaryDirectory() as tmpdir:
                    runtime = Path(tmpdir) / "runtime"
                    create_task(runtime, task_id=TASK_ID, month="2026-08")
                    task_root = runtime / "tasks" / TASK_ID
                    before = _task_snapshot(task_root)

                    with self.assertRaisesRegex(TaskServiceError, "Input file month 2026-05 does not match task month 2026-08"):
                        add_task_file(runtime, TASK_ID, PROJECT_DIR / filename)

                    after = _task_snapshot(task_root)
                    self.assertEqual(after, before)
                    self.assertEqual(load_task(task_root)["status"], "collecting_files")
                    self.assertFalse((task_root / "inputs" / filename).exists())

    def test_file_without_detectable_month_is_staged_using_task_month_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            task_root = runtime / "tasks" / TASK_ID
            source = _write_pdf(root / "\u798f\u57df\u53d1\u7535\u5355-no-month.pdf")

            task = add_task_file(runtime, TASK_ID, source)

            self.assertEqual(task["status"], "collecting_files")
            self.assertEqual(task["pdf_intake"]["received_count"], 1)
            self.assertIsNone(task["pdf_intake"]["staged_files"][0]["filename_month"])
            self.assertEqual(task["pdf_intake"]["staged_files"][0]["month"], "2026-05")
            self.assertTrue(list((task_root / "inputs" / "staged").glob("*.pdf")))

    def test_next_upload_resets_unusable_pdf_batch_without_gateway_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            task_root = runtime / "tasks" / TASK_ID
            staged_dir = task_root / "inputs" / "staged"
            staged_dir.mkdir(parents=True, exist_ok=True)
            stale = _write_pdf(staged_dir / "stale.pdf")
            task = load_task(task_root)
            task["status"] = "need_review"
            task["pdf_intake"] = {
                "batch_state": "need_review",
                "received_count": 1,
                "required_count": 3,
                "staged_files": [
                    {
                        "stored_file": "inputs/staged/stale.pdf",
                        "original_filename": "stale.pdf",
                        "sha256": "stale",
                    }
                ],
                "role_results": [],
                "missing_roles": list(REQUIRED_FILE_TYPES),
                "conflicting_roles": [],
                "next_allowed_action": "operator_review_pdf_roles",
            }
            write_task(task_root, task)
            replacement = _write_pdf(root / "\u798f\u57df\u53d1\u7535\u5355-no-month.pdf")

            updated = add_task_file(runtime, TASK_ID, replacement)

            self.assertFalse(stale.exists())
            self.assertEqual(updated["status"], "collecting_files")
            self.assertEqual(updated["pdf_intake"]["received_count"], 1)
            self.assertIn("pdf_batch_reset_for_reupload", [event["event"] for event in _events(task_root)])

    def test_same_month_alternate_filename_format_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            alternate = root / "\u798f\u57df-202605-\u53d1\u7535\u5355-v2.pdf"
            shutil.copy2(PROJECT_DIR / FUYU_GENERATION, alternate)

            task = add_task_file(runtime, TASK_ID, alternate)

            self.assertEqual(task["status"], "collecting_files")
            self.assertIsNone(task["files"]["generation_statement"])
            self.assertTrue(any(path.name.startswith(alternate.stem + ".") for path in (runtime / "tasks" / TASK_ID / "inputs" / "staged").glob("*.pdf")))

    def test_same_month_dotted_filename_with_staging_uuid_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            source = _write_pdf(root / "兴围-电费结算单 2026.05.7f2b0185-5696-4788-9da7-a2937d7b92e0.pdf")

            task = add_task_file(runtime, TASK_ID, source)

            self.assertEqual(task["status"], "collecting_files")
            self.assertIsNone(task["files"]["fee_statement"])
            self.assertTrue(any(path.name.startswith(source.stem + ".") for path in (runtime / "tasks" / TASK_ID / "inputs" / "staged").glob("*.pdf")))

    def test_cross_month_dashed_filename_with_staging_uuid_is_rejected_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            task_root = runtime / "tasks" / TASK_ID
            source = _write_pdf(root / "发电单_2026-06 (2).7f2b0185-5696-4788-9da7-a2937d7b92e0.pdf")
            before = _task_snapshot(task_root)

            with self.assertRaisesRegex(TaskServiceError, "Input file month 2026-06 does not match task month 2026-05"):
                add_task_file(runtime, TASK_ID, source)

            self.assertEqual(_task_snapshot(task_root), before)
            self.assertFalse((task_root / "inputs" / source.name).exists())

    def test_run_task_uses_task_json_files_when_inputs_contain_replaced_cross_site_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            _add_all_files(runtime)
            task_root = runtime / "tasks" / TASK_ID
            for filename in (HUASHENGTENG_GENERATION, HUASHENGTENG_FEE, HUASHENGTENG_ENERGY):
                _write_pdf(task_root / "inputs" / filename)

            result = run_task(runtime, TASK_ID)

            self.assertEqual(result.task["status"], "completed")
            log_text = (result.task_dir / "outputs" / "\u65b0\u5b89\u9ad8\u4e2d\u90e8" / "2026-05" / "process_log.txt").read_text(encoding="utf-8")
            self.assertIn("source_file[generation_statement]=福域发电单202605-2.", log_text)
            self.assertIn("source_file[fee_statement]=福域电费结算单202605_1.", log_text)
            self.assertIn("source_file[energy_statement]=福域电量结算单202605_1-2.", log_text)
            self.assertNotIn("\u534e\u76db\u817e", log_text)

    def test_xinan_rejects_xixiang_issuer_before_copying_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            task_root = runtime / "tasks" / TASK_ID
            source = _write_pdf(root / HUASHENGTENG_GENERATION)

            task = add_task_file(runtime, TASK_ID, source)

            self.assertEqual(task["status"], "collecting_files")
            self.assertIsNone(task["files"]["generation_statement"])
            self.assertTrue(any(path.name.startswith(Path(HUASHENGTENG_GENERATION).stem + ".") for path in (task_root / "inputs" / "staged").glob("*.pdf")))
    def test_xixiang_accepts_huashengteng_and_rejects_fuyu_issuer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            task_id = "pv_xixiang_middle_school_2026_05_001"
            create_task(runtime, task_id=task_id, site_key="xixiang_middle_school", month="2026-05")
            for filename in (HUASHENGTENG_GENERATION, HUASHENGTENG_FEE, HUASHENGTENG_ENERGY):
                task = add_task_file(runtime, task_id, _write_pdf(root / filename))
            self.assertEqual(task["status"], "ready_to_run")

            task_root = runtime / "tasks" / task_id
            source = _write_pdf(root / FUYU_GENERATION)
            before = _task_snapshot(task_root)
            with self.assertRaisesRegex(TaskServiceError, "exactly three unique files"):
                add_task_file(runtime, task_id, source)

            self.assertEqual(_task_snapshot(task_root), before)
    def test_replaced_same_type_file_is_the_only_visible_generation_statement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            add_task_file(runtime, TASK_ID, PROJECT_DIR / FUYU_GENERATION)
            replacement = root / "福域发电单202605-replacement.pdf"
            shutil.copy2(PROJECT_DIR / FUYU_GENERATION, replacement)
            add_task_file(runtime, TASK_ID, replacement)
            add_task_file(runtime, TASK_ID, PROJECT_DIR / FUYU_FEE)
            add_task_file(runtime, TASK_ID, PROJECT_DIR / FUYU_ENERGY)

            result = run_task(runtime, TASK_ID)

            self.assertEqual(result.task["status"], "completed")
            self.assertTrue(any(path.name.startswith(Path(FUYU_GENERATION).stem + ".") for path in (result.task_dir / "inputs" / "staged").glob("*.pdf")))
            self.assertFalse(any(path.name.startswith(replacement.stem + ".") for path in (result.task_dir / "inputs" / "staged").glob("*.pdf")))
            log_text = (result.task_dir / "outputs" / "\u65b0\u5b89\u9ad8\u4e2d\u90e8" / "2026-05" / "process_log.txt").read_text(encoding="utf-8")
            self.assertIn("source_file[generation_statement]=福域发电单202605-2.", log_text)
            self.assertNotIn("source_file[generation_statement]=福域发电单202605-replacement.", log_text)

    def test_site_without_issuer_aliases_keeps_compatibility_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            config_path = _write_future_site_config(root)
            task_id = "future-school-task"
            create_task(runtime, task_id=task_id, site_key="future_school", month="2026-05", config_path=config_path)

            task = add_task_file(runtime, task_id, _write_pdf(root / FUYU_GENERATION), config_path=config_path)

            self.assertIsNone(task["files"]["generation_statement"])
            self.assertEqual(task["status"], "collecting_files")

    def test_add_file_rejects_completed_task_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            _add_all_files(runtime)
            result = run_task(runtime, TASK_ID)
            self.assertEqual(result.task["status"], "completed")
            task_root = result.task_dir
            source = _write_pdf(root / "福域发电单202605-after-completed.pdf")
            before = _task_snapshot(task_root)

            with self.assertRaisesRegex(TaskServiceError, "status is 'completed'"):
                add_task_file(runtime, TASK_ID, source)

            self.assertEqual(_task_snapshot(task_root), before)
            self.assertFalse((task_root / "inputs" / source.name).exists())

    def test_add_file_rejects_non_intake_statuses_without_side_effects(self) -> None:
        for status in ("need_review", "running", "confirmed", "failed"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    runtime = root / "runtime"
                    create_task(runtime, task_id=TASK_ID, month="2026-05")
                    task_root = runtime / "tasks" / TASK_ID
                    task = load_task(task_root)
                    task["status"] = status
                    write_task(task_root, task)
                    source = _write_pdf(root / f"福域发电单202605-{status}.pdf")
                    before = _task_snapshot(task_root)

                    with self.assertRaisesRegex(TaskServiceError, "does not accept files"):
                        add_task_file(runtime, TASK_ID, source)

                    self.assertEqual(_task_snapshot(task_root), before)
                    self.assertFalse((task_root / "inputs" / source.name).exists())

    def test_pdf_only_task_loads_controlled_templates_without_changing_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            _add_all_files(runtime)
            task_root = runtime / "tasks" / TASK_ID
            self.assertFalse(any(task_root.joinpath("inputs").glob("*.xlsx")))

            template_dir = Path("config/templates/xinan_high_school_blank")
            template_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in template_dir.glob("*.xlsx")
            }
            result = run_task(runtime, TASK_ID)

            self.assertEqual(result.task["status"], "completed")
            site = load_site_config("xinan_high_school")
            self.assertTrue((result.task_dir / "inputs" / site.detail_workbook).exists())
            self.assertTrue((result.task_dir / "inputs" / site.summary_workbook).exists())
            output_dir = result.task_dir / "outputs" / site.site_name / "2026-05"
            self.assertTrue((output_dir / site.output_workbook_name("detail", "2026-05")).exists())
            self.assertTrue((output_dir / site.output_workbook_name("summary", "2026-05")).exists())
            self.assertEqual(
                template_hashes,
                {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in template_dir.glob("*.xlsx")},
            )

    def test_june_task_uses_completed_previous_month_workbook_copies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            previous = create_task(runtime, task_id="previous-may", month="2026-05")
            previous_root = runtime / "tasks" / "previous-may"
            previous["status"] = "completed"
            write_task(previous_root, previous)
            previous_site = load_site_config("xinan_high_school")
            previous_output = previous_root / "outputs" / previous_site.site_name / "2026-05"
            previous_output.mkdir(parents=True)
            template_dir = Path("config/templates/xinan_high_school_blank")
            shutil.copy2(
                template_dir / previous_site.detail_workbook,
                previous_output / previous_site.output_workbook_name("detail", "2026-05"),
            )
            shutil.copy2(
                template_dir / previous_site.summary_workbook,
                previous_output / previous_site.output_workbook_name("summary", "2026-05"),
            )
            _promote_site_history(previous_root, previous, config_path="config/sites.json")

            current = create_task(runtime, task_id="current-june", month="2026-06")
            current_root = runtime / "tasks" / "current-june"
            _copy_site_templates(current, current_root / "inputs", "config/sites.json")

            history_output = runtime / "site_history" / "xinan_high_school" / "2026-05"
            for template in template_dir.glob("*.xlsx"):
                copied = current_root / "inputs" / template.name
                self.assertEqual(_hash_file(copied), _hash_file(history_output / template.name))

    def test_retroactive_site_uses_latest_complete_history_and_records_actual_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            site = load_site_config("site_5f14cc7ab0e2")
            for task_id, month in (("full-history-may", "2026-05"), ("full-history-july", "2026-07")):
                task = _write_completed_task_outputs(runtime, task_id, site, month)
                task["revision"] = 1
                write_task(runtime / "tasks" / task_id, task)
                _promote_site_history(runtime / "tasks" / task_id, task, config_path="config/sites.json")

            current = create_task(
                runtime,
                task_id="retroactive-june",
                site_key=site.site_key,
                month="2026-06",
            )
            current["revision"] = 1
            write_task(runtime / "tasks" / "retroactive-june", current)
            current_root = runtime / "tasks" / "retroactive-june"
            _copy_site_templates(current, current_root / "inputs", "config/sites.json")

            july_manifest = json.loads(
                (runtime / "site_history" / site.site_key / "2026-07" / "manifest.json").read_text(encoding="utf-8")
            )
            july_summary = runtime / "site_history" / site.site_key / "2026-07" / july_manifest["workbooks"]["summary_workbook"]["path"]
            self.assertEqual(_hash_file(current_root / "inputs" / site.summary_workbook), _hash_file(july_summary))

            current["status"] = "completed"
            write_task(current_root, current)
            output_dir = current_root / "outputs" / site.site_name / "2026-06"
            output_dir.mkdir(parents=True)
            for role, workbook_name in (("detail", site.detail_workbook), ("summary", site.summary_workbook)):
                (output_dir / site.output_workbook_name(role, "2026-06")).write_text(
                    f"retroactive-june:{workbook_name}",
                    encoding="utf-8",
                )
            promotion = _promote_site_history(current_root, current, config_path="config/sites.json")

            self.assertEqual(promotion["baseline_dependency"]["month"], "2026-07")
            june_manifest = json.loads(
                (runtime / "site_history" / site.site_key / "2026-06" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(june_manifest["baseline_dependency"]["month"], "2026-07")

    def test_manifest_paths_resolve_to_canonical_history_workbooks_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            site = load_site_config("xinan_high_school")
            task_id = "canonical-manifest"
            task = _write_completed_task_outputs(runtime, task_id, site, "2026-05")

            _promote_site_history(runtime / "tasks" / task_id, task, config_path="config/sites.json")

            _assert_site_history_manifest(
                self,
                runtime / "site_history" / site.site_key / "2026-05",
                site,
                "2026-05",
                task_id,
            )

    def test_concurrent_same_site_month_promotions_leave_one_complete_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            site = load_site_config("xinan_high_school")
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            task_ids = ("history-task-a", "history-task-b")
            for task_id in task_ids:
                _write_completed_task_outputs(runtime, task_id, site, "2026-05")
            processes = [
                context.Process(
                    target=_concurrent_promote_site_history,
                    args=(str(runtime), task_id, "config/sites.json", start, results),
                )
                for task_id in task_ids
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual([results.get(timeout=2) for _ in processes], [None, None])

            history_dir = runtime / "site_history" / site.site_key / "2026-05"
            manifest = _assert_site_history_manifest(self, history_dir, site, "2026-05", None)
            source_task_id = manifest["source_task_id"]
            self.assertIn(source_task_id, task_ids)
            for workbook_name in (site.detail_workbook, site.summary_workbook):
                self.assertEqual((history_dir / workbook_name).read_text(encoding="utf-8"), f"{source_task_id}:{workbook_name}")

    def test_different_sites_promote_to_isolated_history_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            xinan = load_site_config("xinan_high_school")
            xixiang = load_site_config("xixiang_middle_school")
            xinan_task = _write_completed_task_outputs(runtime, "xinan-task", xinan, "2026-05")
            xixiang_task = _write_completed_task_outputs(runtime, "xixiang-task", xixiang, "2026-05")

            _promote_site_history(runtime / "tasks" / "xinan-task", xinan_task, config_path="config/sites.json")
            _promote_site_history(runtime / "tasks" / "xixiang-task", xixiang_task, config_path="config/sites.json")

            _assert_site_history_manifest(
                self,
                runtime / "site_history" / xinan.site_key / "2026-05",
                xinan,
                "2026-05",
                "xinan-task",
            )
            _assert_site_history_manifest(
                self,
                runtime / "site_history" / xixiang.site_key / "2026-05",
                xixiang,
                "2026-05",
                "xixiang-task",
            )
            self.assertFalse((runtime / "site_history" / xinan.site_key / "2026-05" / xixiang.detail_workbook).exists())
            self.assertFalse((runtime / "site_history" / xixiang.site_key / "2026-05" / xinan.detail_workbook).exists())

    def test_missing_previous_month_baseline_is_blocked_after_history_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            previous = create_task(runtime, task_id="previous-april", month="2026-04")
            previous_root = runtime / "tasks" / "previous-april"
            previous["status"] = "completed"
            write_task(previous_root, previous)
            previous_site = load_site_config("xinan_high_school")
            previous_output = previous_root / "outputs" / previous_site.site_name / "2026-04"
            previous_output.mkdir(parents=True)
            template_dir = Path("config/templates/xinan_high_school")
            shutil.copy2(
                template_dir / previous_site.detail_workbook,
                previous_output / previous_site.output_workbook_name("detail", "2026-04"),
            )
            shutil.copy2(
                template_dir / previous_site.summary_workbook,
                previous_output / previous_site.output_workbook_name("summary", "2026-04"),
            )
            _promote_site_history(previous_root, previous, config_path="config/sites.json")
            current = create_task(runtime, task_id="current-june", month="2026-06")

            with self.assertRaisesRegex(TaskServiceError, "Missing previous-month site history baseline"):
                _copy_site_templates(current, runtime / "tasks" / "current-june" / "inputs", "config/sites.json")

    def test_run_task_need_review_records_review_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            _add_all_files(runtime)

            with patch.object(LocalSampleOcrAdapter, "recognize", _low_confidence_recognize):
                result = run_task(runtime, TASK_ID)

            self.assertEqual(result.task["status"], "need_review")
            site = load_site_config("xinan_high_school")
            self.assertEqual(result.task["review_report"], f"outputs/{site.site_name}/2026-05/review_report.json")
            self.assertTrue((result.task_dir / result.task["review_report"]).exists())
            event_names = [event["event"] for event in _events(result.task_dir)]
            self.assertIn("need_review", event_names)

    def test_missing_field_retry_accepts_only_missing_field_issues(self) -> None:
        issues = _missing_field_retry_issues(
            {"issues": [{"field": "period_start", "reason": "field_missing"}]},
        )
        self.assertEqual(issues[0]["field"], "period_start")

        with self.assertRaisesRegex(TaskServiceError, "only accepts field_missing"):
            _missing_field_retry_issues(
                {"issues": [{"field": "grid_fee", "reason": "low_confidence"}]},
            )

    def test_successful_artifact_index_clears_stale_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            task = create_task(runtime, task_id=TASK_ID, month="2026-05")
            task_root = runtime / "tasks" / TASK_ID
            task["artifact_index_error"] = "previous failure"
            write_task(task_root, task)

            updated = _write_artifact_index_or_record_error(
                task_root,
                task,
                config_path="config/sites.json",
            )

            self.assertEqual(updated["artifact_index"], "artifact_index.json")
            self.assertNotIn("artifact_index_error", updated)

    def test_confirm_task_reruns_and_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            confirmed_path = Path(tmpdir) / "confirmed_data.json"
            confirmed_path.write_text(
                json.dumps(
                    {
                        "confirmed_by": "operator",
                        "confirmed_at": "2026-07-08T10:00:00+08:00",
                        "fields": {"grid_fee": {"value": 407.29, "reason": "浜哄伐鏍稿鐢佃垂缁撶畻鍗曞悗纭"}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            _add_all_files(runtime)
            with patch.object(LocalSampleOcrAdapter, "recognize", _low_confidence_recognize):
                need_review = run_task(runtime, TASK_ID)
                self.assertEqual(need_review.task["status"], "need_review")
                completed = confirm_task(runtime, TASK_ID, confirmed_path)

            self.assertEqual(completed.task["status"], "completed")
            site = load_site_config("xinan_high_school")
            recognized_path = completed.task_dir / "outputs" / site.site_name / "2026-05" / "recognized_data.json"
            recognized = json.loads(recognized_path.read_text(encoding="utf-8"))
            self.assertEqual(recognized["fields"]["grid_fee"]["source_type"], "manual_confirmed")
            event_names = [event["event"] for event in _events(completed.task_dir)]
            self.assertIn("confirmed_data_received", event_names)
            self.assertIn("task_completed", event_names)

    def test_retry_review_task_preserves_evidence_and_completes_without_manual_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            _add_all_files(runtime)
            with patch.object(LocalSampleOcrAdapter, "recognize", _low_confidence_recognize):
                need_review = run_task(runtime, TASK_ID)
            self.assertEqual(need_review.task["status"], "need_review")

            completed = retry_review_task(runtime, TASK_ID)

            self.assertEqual(completed.task["status"], "completed")
            self.assertTrue(completed.task["review_task_retry"]["preserved_review_evidence"])
            event_names = [event["event"] for event in _events(completed.task_dir)]
            self.assertIn("review_task_retry_started", event_names)
            self.assertIn("task_completed", event_names)

    def test_run_task_failed_records_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            _add_all_files(runtime)
            failed_report = ValidationReport(
                site_key="xinan_high_school",
                site_name=load_site_config("xinan_high_school").site_name,
                month="2026-05",
                status="fail",
                can_write_excel=False,
                normalized_data={},
                checks=[],
                errors=["forced validation failure"],
                warnings=[],
            )

            with patch("submit_flow_agent.pipeline.validate_data_to_file", side_effect=RuntimeError("boom")):
                result = run_task(runtime, TASK_ID)

            self.assertEqual(result.task["status"], "failed")
            self.assertIn("boom", result.task["error"])
            event_names = [event["event"] for event in _events(result.task_dir)]
            self.assertIn("task_failed", event_names)

    def test_show_and_list_outputs_use_task_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            create_task(runtime, task_id=TASK_ID, month="2026-05")
            _add_all_files(runtime)
            run_task(runtime, TASK_ID)

            task = get_task(runtime, TASK_ID)
            outputs = list_task_outputs(runtime, TASK_ID)

            self.assertEqual(task["status"], "completed")
            site = load_site_config("xinan_high_school")
            self.assertIn(f"outputs/{site.site_name}/2026-05/process_log.txt", outputs)


def _add_all_files(runtime: Path) -> dict[str, object]:
    task = add_task_file(runtime, TASK_ID, PROJECT_DIR / "福域发电单202605-2.pdf")
    task = add_task_file(runtime, TASK_ID, PROJECT_DIR / "福域电费结算单202605_1.pdf")
    task = add_task_file(runtime, TASK_ID, PROJECT_DIR / "福域电量结算单202605_1-2.pdf")
    return task


def _concurrent_add_file(runtime: str, task_id: str, source: str, start: multiprocessing.synchronize.Event, results: multiprocessing.queues.Queue) -> None:
    try:
        start.wait(timeout=10)
        # spawn starts a fresh interpreter, so the TestCase patch is not inherited.
        # Keep this concurrency test focused on task locking and atomic persistence.
        with patch("submit_flow_agent.task_service._classify_pdf_intake_batch", side_effect=_classify_by_filename):
            add_task_file(Path(runtime), task_id, Path(source))
        results.put(None)
    except Exception as exc:
        results.put(f"{type(exc).__name__}: {exc}")
        raise


def _concurrent_promote_site_history(
    runtime: str,
    task_id: str,
    config_path: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    try:
        start.wait(timeout=10)
        task_root = Path(runtime) / "tasks" / task_id
        _promote_site_history(task_root, load_task(task_root), config_path=config_path)
        results.put(None)
    except Exception as exc:
        results.put(f"{type(exc).__name__}: {exc}")
        raise


def _write_completed_task_outputs(runtime: Path, task_id: str, site: SiteConfig, month: str) -> dict[str, object]:
    task = create_task(runtime, task_id=task_id, site_key=site.site_key, month=month)
    task_root = runtime / "tasks" / task_id
    task["status"] = "completed"
    write_task(task_root, task)
    output_dir = task_root / "outputs" / site.site_name / month
    output_dir.mkdir(parents=True)
    for role, workbook_name in (("detail", site.detail_workbook), ("summary", site.summary_workbook)):
        source_name = site.output_workbook_name(role, month)
        (output_dir / source_name).write_text(f"{task_id}:{workbook_name}", encoding="utf-8")
    return task


def _assert_site_history_manifest(
    test_case: unittest.TestCase,
    history_dir: Path,
    site: SiteConfig,
    month: str,
    source_task_id: str | None,
) -> dict[str, object]:
    manifest_path = history_dir / "manifest.json"
    test_case.assertTrue(manifest_path.is_file())
    test_case.assertFalse(manifest_path.is_symlink())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    test_case.assertEqual(manifest["site_key"], site.site_key)
    test_case.assertEqual(manifest["site_name"], site.site_name)
    test_case.assertEqual(manifest["month"], month)
    if source_task_id is not None:
        test_case.assertEqual(manifest["source_task_id"], source_task_id)
    test_case.assertEqual(set(manifest["workbooks"]), {"detail_workbook", "summary_workbook"})
    expected_paths = {
        "detail_workbook": site.detail_workbook,
        "summary_workbook": site.summary_workbook,
    }
    for key, workbook in manifest["workbooks"].items():
        test_case.assertEqual(workbook["path"], expected_paths[key])
        workbook_path = history_dir / workbook["path"]
        test_case.assertTrue(workbook_path.is_file())
        test_case.assertFalse(workbook_path.is_symlink())
        test_case.assertEqual(workbook["sha256"], _hash_file(workbook_path))
    return manifest


def _write_future_site_config(root: Path) -> Path:
    config_path = root / "sites.json"
    payload = json.loads(Path("config/sites.json").read_text(encoding="utf-8-sig"))
    payload["sites"].append(
        {
            "site_key": "future_school",
            "site_name": "Future School",
            "project_name": "Future Project",
            "aliases": ["Future School"],
            "company_name": "Future Energy",
            "school_base_price": "0.6998",
            "school_discount_rate": "1",
            "template_dir": str(Path("config/templates/xinan_high_school_blank").resolve()),
            "detail_workbook": "\u65b0\u5b89\u4e2d\u5b66\uff08\u9ad8\u4e2d\u90e8\uff09\u5149\u4f0f\u53d1\u7535\u9879\u76ee\u5b66\u6821\u7528\u7535\u7edf\u8ba1\u8868.xlsx",
            "detail_sheet": "1",
            "summary_workbook": "\u6c47\u603b\u8868\u683c.xlsx",
            "summary_sheet": "Sheet1",
            "detail_profile": "single_detail_row",
            "summary_profile": "meter_rows_summary",
            "meters": [
                {
                    "meter_no": "FUTURE-001",
                    "multiplier": "150",
                    "role": "primary",
                    "detail_enabled": True,
                    "grid_export_allocation": "all",
                }
            ],
        }
    )
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


def _events(task_root: Path) -> list[dict[str, object]]:
    event_path = task_root / "audit" / "events.jsonl"
    return [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()]


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
        role_results.append(
            {
                "stored_file": stored,
                "original_filename": name,
                "sha256": staged.get("sha256"),
                "role": role,
                "classification_source": "test_filename",
                "confidence": "high" if role in REQUIRED_FILE_TYPES else "low",
            }
        )
        if role in REQUIRED_FILE_TYPES:
            files[role] = stored
    missing = [role for role in REQUIRED_FILE_TYPES if not files[role]]
    if missing:
        return {
            "batch_state": "need_review",
            "role_results": role_results,
            "missing_roles": missing,
            "conflicting_roles": [],
            "next_allowed_action": "operator_review_pdf_roles",
            "files": {role: None for role in REQUIRED_FILE_TYPES},
        }
    return {
        "batch_state": "ready",
        "role_results": role_results,
        "missing_roles": [],
        "conflicting_roles": [],
        "next_allowed_action": "run_task",
        "files": files,
    }
def _task_snapshot(task_root: Path) -> dict[str, object]:
    return {
        "task_json_sha256": _hash_file(task_root / "task.json"),
        "task_json": (task_root / "task.json").read_text(encoding="utf-8"),
        "audit_sha256": _hash_file(task_root / "audit" / "events.jsonl"),
        "audit_line_count": len(_events(task_root)),
        "inputs": _file_hashes(task_root / "inputs"),
        "outputs": _file_hashes(task_root / "outputs"),
    }


def _file_hashes(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)).replace("\\", "/"): _hash_file(path)
        for path in sorted(root.rglob("*"), key=lambda item: str(item))
        if path.is_file()
    }


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pdf(path: Path) -> Path:
    path.write_bytes(("%PDF-1.4\n% test " + path.name + "\n").encode("utf-8"))
    return path


def _low_confidence_recognize(self: LocalSampleOcrAdapter, source_file: Path, pages: list[object]) -> OcrRawResult:
    name = source_file.name
    if "发电单" in name:
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
    elif "电量" in name:
        texts = ["结算月份 2026年5月", "上网电量 1680 kWh"]
    elif "电费" in name:
        texts = ["上网电价 0.242434 元/kWh", "上网电费 407.29 元"]
    else:
        texts = []
    blocks = [
        OcrBlock(
            text=text,
            bbox=(0.0, float(index * 24), 240.0, float(index * 24 + 20)),
            confidence=0.80 if "上网电费" in text else 0.98,
        )
        for index, text in enumerate(texts)
    ]
    return OcrRawResult(
        source_file=source_file,
        pages=[OcrPage(page=page.page, image_path=page.image_path, blocks=blocks if page.page == 1 else []) for page in pages],
    )

if __name__ == "__main__":
    unittest.main()
