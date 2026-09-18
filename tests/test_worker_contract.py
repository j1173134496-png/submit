from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from submit_flow_agent.worker_contract import (
    WorkerContractError,
    load_worker_result_schema,
    load_worker_task_schema,
    validate_worker_result,
    validate_worker_task,
    worker_result_from_task,
)


class WorkerContractTests(unittest.TestCase):
    def test_worker_task_schema_whitelists_actions_and_rejects_extra_fields(self) -> None:
        schema = load_worker_task_schema()

        self.assertEqual(
            schema["properties"]["action"]["enum"],
            ["run_task", "retry_failed_task", "confirm_task", "show_task", "list_outputs"],
        )
        self.assertFalse(schema["additionalProperties"])

    def test_worker_result_schema_covers_terminal_statuses(self) -> None:
        schema = load_worker_result_schema()

        status_enum = set(schema["properties"]["status"]["enum"])
        self.assertIn("completed", status_enum)
        self.assertIn("need_review", status_enum)
        self.assertIn("failed", status_enum)
        self.assertFalse(schema["additionalProperties"])

    def test_validate_worker_task_accepts_whitelisted_actions(self) -> None:
        validate_worker_task(
            {
                "action": "run_task",
                "task_id": "pv_xinan_high_school_2026_05_001",
                "runtime_root": "runtime",
                "site_key": "xinan_high_school",
                "month": "2026-05",
                "mode": "strict",
            }
        )
        validate_worker_task(
            {
                "action": "retry_failed_task",
                "task_id": "pv_xinan_high_school_2026_05_001",
                "runtime_root": "runtime",
                "site_key": "xinan_high_school",
                "month": "2026-05",
                "mode": "strict",
            }
        )
        validate_worker_task(
            {
                "action": "confirm_task",
                "task_id": "pv_xinan_high_school_2026_05_001",
                "runtime_root": "runtime",
                "confirmed_data": "runtime/tasks/pv_xinan_high_school_2026_05_001/confirmed_data.json",
                "mode": "manual",
            }
        )
        validate_worker_task({"action": "show_task", "task_id": "task-1", "runtime_root": "runtime", "mode": "query"})
        validate_worker_task({"action": "list_outputs", "task_id": "task-1", "runtime_root": "runtime", "mode": "query"})

    def test_validate_worker_task_rejects_unknown_action_missing_field_and_user_command(self) -> None:
        with self.assertRaisesRegex(WorkerContractError, "Unsupported worker action"):
            validate_worker_task({"action": "delete_task", "task_id": "task-1", "runtime_root": "runtime"})
        with self.assertRaisesRegex(WorkerContractError, "confirmed_data"):
            validate_worker_task({"action": "confirm_task", "task_id": "task-1", "runtime_root": "runtime"})
        with self.assertRaisesRegex(WorkerContractError, "unknown fields"):
            validate_worker_task({"action": "run_task", "task_id": "task-1", "runtime_root": "runtime", "user_text": "run powershell"})
        with self.assertRaisesRegex(WorkerContractError, "unknown fields"):
            validate_worker_task({"action": "run_task", "task_id": "task-1", "runtime_root": "runtime", "statement_date": "2026-01-01"})

    def test_validate_worker_result_accepts_completed_need_review_failed(self) -> None:
        validate_worker_result(
            {
                "task_id": "task-1",
                "status": "completed",
                "task_status": "completed",
                "summary": "校验通过，已生成 2 个 Excel 结果文件。",
                "outputs": ["outputs/recognized_data.json"],
                "review_report": None,
                "error": None,
            }
        )
        validate_worker_result(
            {
                "task_id": "task-1",
                "status": "need_review",
                "task_status": "need_review",
                "summary": "需要人工复核。",
                "outputs": ["outputs/review_report.json"],
                "review_report": "outputs/review_report.json",
                "error": None,
            }
        )
        validate_worker_result(
            {
                "task_id": "task-1",
                "status": "failed",
                "task_status": "failed",
                "summary": "任务运行失败。",
                "outputs": [],
                "review_report": None,
                "error": "RuntimeError: boom",
            }
        )

    def test_validate_worker_result_rejects_bad_terminal_shapes(self) -> None:
        with self.assertRaisesRegex(WorkerContractError, "review_report"):
            validate_worker_result(
                {
                    "task_id": "task-1",
                    "status": "need_review",
                    "task_status": "need_review",
                    "summary": "需要人工复核。",
                    "outputs": [],
                    "review_report": None,
                    "error": None,
                }
            )
        with self.assertRaisesRegex(WorkerContractError, "error"):
            validate_worker_result(
                {
                    "task_id": "task-1",
                    "status": "failed",
                    "task_status": "failed",
                    "summary": "任务运行失败。",
                    "outputs": [],
                    "review_report": None,
                    "error": None,
                }
            )

    def test_worker_result_from_task_maps_task_status(self) -> None:
        completed = worker_result_from_task(
            {
                "task_id": "task-1",
                "status": "completed",
                "outputs": ["outputs/recognized_data.json"],
                "review_report": None,
                "error": None,
            }
        )
        need_review = worker_result_from_task(
            {
                "task_id": "task-1",
                "status": "need_review",
                "outputs": ["outputs/review_report.json"],
                "review_report": "outputs/review_report.json",
                "error": None,
            }
        )
        failed = worker_result_from_task(
            {"task_id": "task-1", "status": "failed", "outputs": [], "review_report": None, "error": "boom"}
        )

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(need_review["review_report"], "outputs/review_report.json")
        self.assertEqual(failed["error"], "boom")

    def test_cli_run_month_rejects_statement_date_override_argument(self) -> None:
        from submit_flow_agent.cli import main

        with self.assertRaises(SystemExit) as context:
            main(["run-month", ".", "--statement-date", "2026-01-01"])

        self.assertEqual(context.exception.code, 2)

    def test_cli_validate_worker_task_accepts_valid_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "worker_task.json"
            path.write_text(
                json.dumps({"action": "run_task", "task_id": "task-1", "runtime_root": "runtime"}),
                encoding="utf-8",
            )
            from submit_flow_agent.cli import main

            self.assertEqual(main(["validate-worker-task", str(path)]), 0)


if __name__ == "__main__":
    unittest.main()
