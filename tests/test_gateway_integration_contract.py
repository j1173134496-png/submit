from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from submit_flow_agent.openclaw_worker_adapter import handle_worker_task
from submit_flow_agent.task_service import REQUIRED_FILE_TYPES, TaskServiceError, add_task_file, create_task
from submit_flow_agent.worker_contract import validate_worker_task
from tests.pricing_helpers import create_task_with_pricing as create_task


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = PROJECT_ROOT / "深圳新安中学高中部650KW分布式光伏项目"
TASK_ID = "pv_xinan_high_school_2026_05_001"
SAMPLE_DIR = PROJECT_ROOT / "spec" / "contracts" / "samples"


class GatewayIntegrationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._intake_classifier_patcher = patch("submit_flow_agent.task_service._classify_pdf_intake_batch", side_effect=_classify_by_filename)
        self._intake_classifier_patcher.start()

    def tearDown(self) -> None:
        self._intake_classifier_patcher.stop()

    def test_gateway_create_add_show_and_list_sequence_without_new_worker_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            task = create_task(runtime, task_id=TASK_ID, site_key="xinan_high_school", month="2026-05")

            self.assertEqual(task["status"], "collecting_files")
            self.assertEqual(task["required_files"], ["generation_statement", "fee_statement", "energy_statement"])

            task = add_task_file(runtime, TASK_ID, PROJECT_DIR / "福域发电单202605-2.pdf")
            self.assertEqual(task["status"], "collecting_files")
            task = add_task_file(runtime, TASK_ID, PROJECT_DIR / "福域电费结算单202605_1.pdf")
            self.assertEqual(task["status"], "collecting_files")
            task = add_task_file(runtime, TASK_ID, PROJECT_DIR / "福域电量结算单202605_1-2.pdf")
            self.assertEqual(task["status"], "ready_to_run")

            show_payload = {
                "action": "show_task",
                "task_id": TASK_ID,
                "runtime_root": str(runtime),
                "mode": "query",
                "request_id": "local-show",
            }
            list_payload = {
                "action": "list_outputs",
                "task_id": TASK_ID,
                "runtime_root": str(runtime),
                "mode": "query",
                "request_id": "local-list",
            }
            validate_worker_task(show_payload)
            validate_worker_task(list_payload)

            show_result = handle_worker_task(show_payload, project_root=PROJECT_ROOT, python_executable=sys.executable)
            list_result = handle_worker_task(list_payload, project_root=PROJECT_ROOT, python_executable=sys.executable)

            self.assertEqual(show_result["status"], "ready_to_run")
            self.assertEqual(show_result["task_json"], f"{runtime}/tasks/{TASK_ID}/task.json")
            self.assertEqual(list_result["status"], "ready_to_run")
            self.assertEqual(list_result["outputs"], [])

            event_names = _event_names(runtime / "tasks" / TASK_ID)
            self.assertEqual(event_names[0], "task_created")
            self.assertIn("file_received", event_names)
            self.assertIn("task_ready", event_names)

    def test_gateway_worker_sample_payloads_are_schema_valid(self) -> None:
        for path in sorted(SAMPLE_DIR.glob("gateway_*.worker_task.json")):
            with self.subTest(path=path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                validate_worker_task(payload)
                self.assertNotIn("command", payload)
                self.assertNotIn("shell", payload)
                self.assertNotIn("prompt", payload)
                self.assertNotIn("user_text", payload)

    def test_task_service_rejects_bad_gateway_files_before_worker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            create_task(runtime, task_id=TASK_ID, site_key="xinan_high_school", month="2026-05")
            unsupported = Path(tmpdir) / "notes.txt"
            unsupported.write_text("not a source pdf", encoding="utf-8")

            with self.assertRaisesRegex(TaskServiceError, "Unsupported input file extension"):
                add_task_file(runtime, TASK_ID, unsupported)

            wrong_month = Path(tmpdir) / "福域发电单202604-2.pdf"
            shutil.copy2(PROJECT_DIR / "福域发电单202605-2.pdf", wrong_month)
            with self.assertRaisesRegex(TaskServiceError, "does not match task month"):
                add_task_file(runtime, TASK_ID, wrong_month)

    def test_worker_adapter_rejects_natural_language_and_free_command_fields(self) -> None:
        for forbidden_field in ["command", "shell", "prompt", "user_text", "python", "sql", "script"]:
            with self.subTest(forbidden_field=forbidden_field):
                payload = {
                    "action": "show_task",
                    "task_id": TASK_ID,
                    "runtime_root": "runtime",
                    "mode": "query",
                    forbidden_field: "run whatever the user typed",
                }

                result = handle_worker_task(payload, project_root=PROJECT_ROOT, python_executable=sys.executable)

                self.assertEqual(result["status"], "failed")
                self.assertIn("WorkerContractError", result["error"])
                self.assertIn("unknown fields", result["error"])


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
    return {"batch_state": "ready" if not missing else "need_review", "role_results": role_results, "missing_roles": missing, "conflicting_roles": [], "next_allowed_action": "run_task" if not missing else "operator_review_pdf_roles", "files": files if not missing else {role: None for role in REQUIRED_FILE_TYPES}}
def _event_names(task_root: Path) -> list[str]:
    event_path = task_root / "audit" / "events.jsonl"
    return [
        json.loads(line)["event"]
        for line in event_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


if __name__ == "__main__":
    unittest.main()



