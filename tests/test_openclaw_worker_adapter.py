from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from submit_flow_agent.openclaw_worker_adapter import handle_worker_task


class OpenClawWorkerAdapterTests(unittest.TestCase):
    def test_show_task_returns_structured_result(self) -> None:
        with patch("submit_flow_agent.openclaw_worker_adapter.subprocess.run") as run:
            run.return_value = _completed(
                {
                    "task_id": "task-1",
                    "status": "collecting_files",
                    "outputs": [],
                    "review_report": None,
                    "error": None,
                }
            )

            result = handle_worker_task(
                {"action": "show_task", "task_id": "task-1", "runtime_root": "runtime", "mode": "query"},
                project_root=Path("/project"),
            )

        self.assertEqual(result["status"], "collecting_files")
        self.assertEqual(result["task_status"], "collecting_files")
        self.assertEqual(result["task_json"], "runtime/tasks/task-1/task.json")
        self.assertEqual(run.call_args.args[0], [sys.executable, "-m", "submit_flow_agent.cli", "show-task", "task-1", "--runtime-root", "runtime"])
        self.assertNotEqual(run.call_args.args[0][0], "python3")
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONPATH"], "src")
        self.assertEqual(run.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_list_outputs_queries_task_then_outputs(self) -> None:
        with patch("submit_flow_agent.openclaw_worker_adapter.subprocess.run") as run:
            run.side_effect = [
                _completed({"task_id": "task-1", "status": "completed", "outputs": [], "review_report": None, "error": None}),
                _completed({"task_id": "task-1", "outputs": ["outputs/a.json"]}),
            ]

            result = handle_worker_task(
                {"action": "list_outputs", "task_id": "task-1", "runtime_root": "runtime", "mode": "query"},
                project_root=Path("/project"),
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outputs"], ["outputs/a.json"])
        self.assertEqual(run.call_args_list[1].args[0][-4:], ["list-outputs", "task-1", "--runtime-root", "runtime"])

    def test_explicit_python_executable_is_passed_through(self) -> None:
        with patch("submit_flow_agent.openclaw_worker_adapter.subprocess.run") as run:
            run.return_value = _completed(
                {
                    "task_id": "task-1",
                    "status": "collecting_files",
                    "outputs": [],
                    "review_report": None,
                    "error": None,
                }
            )

            handle_worker_task(
                {"action": "show_task", "task_id": "task-1", "runtime_root": "runtime", "mode": "query"},
                project_root=Path("/project"),
                python_executable="/opt/submit-flow/custom-python",
            )

        self.assertEqual(run.call_args.args[0][0], "/opt/submit-flow/custom-python")

    def test_run_retry_and_confirm_task_are_supported(self) -> None:
        with patch("submit_flow_agent.openclaw_worker_adapter.subprocess.run") as run:
            run.return_value = _completed(
                {
                    "task_dir": "runtime/tasks/task-1",
                    "task_id": "task-1",
                    "status": "completed",
                    "outputs": ["outputs/result.xlsx"],
                    "review_report": None,
                    "error": None,
                }
            )
            run_result = handle_worker_task(
                {"action": "run_task", "task_id": "task-1", "runtime_root": "runtime", "mode": "strict"},
                project_root=Path("/project"),
            )
            retry_result = handle_worker_task(
                {"action": "retry_failed_task", "task_id": "task-1", "runtime_root": "runtime", "mode": "strict"},
                project_root=Path("/project"),
            )
            confirm_result = handle_worker_task(
                {
                    "action": "confirm_task",
                    "task_id": "task-1",
                    "runtime_root": "runtime",
                    "mode": "manual",
                    "confirmed_data": "runtime/tasks/task-1/confirmed_data.json",
                },
                project_root=Path("/project"),
            )

        self.assertEqual(run_result["status"], "completed")
        self.assertEqual(retry_result["status"], "completed")
        self.assertEqual(confirm_result["status"], "completed")
        self.assertIn([sys.executable, "-m", "submit_flow_agent.cli", "run-task", "task-1", "--runtime-root", "runtime"], [call.args[0] for call in run.call_args_list])
        self.assertIn(
            [sys.executable, "-m", "submit_flow_agent.cli", "retry-failed-task", "task-1", "--runtime-root", "runtime"],
            [call.args[0] for call in run.call_args_list],
        )
        self.assertIn(
            [
                sys.executable,
                "-m",
                "submit_flow_agent.cli",
                "confirm-task",
                "task-1",
                "runtime/tasks/task-1/confirmed_data.json",
                "--runtime-root",
                "runtime",
            ],
            [call.args[0] for call in run.call_args_list],
        )

    def test_rejects_unknown_extra_and_free_command_fields_as_failed_result(self) -> None:
        for payload, expected in [
            ({"action": "delete_task", "task_id": "task-1", "runtime_root": "runtime"}, "Unsupported worker action"),
            ({"action": "show_task", "task_id": "task-1", "runtime_root": "runtime", "extra": "no"}, "unknown fields"),
            ({"action": "show_task", "task_id": "task-1", "runtime_root": "runtime", "command": "rm -rf /"}, "unknown fields"),
        ]:
            with self.subTest(payload=payload):
                result = handle_worker_task(payload, project_root=Path("/project"))
                self.assertEqual(result["status"], "failed")
                self.assertIn(expected, result["error"])

    def test_cli_failure_is_structured_failed_result(self) -> None:
        with patch("submit_flow_agent.openclaw_worker_adapter.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["python3"],
                returncode=2,
                stdout="",
                stderr="Task service error: Task input files are not ready.\n",
            )

            result = handle_worker_task(
                {"action": "run_task", "task_id": "task-1", "runtime_root": "runtime", "mode": "strict"},
                project_root=Path("/project"),
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("Task input files are not ready", result["error"])


def _completed(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python3"],
        returncode=0,
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr="",
    )


if __name__ == "__main__":
    unittest.main()
