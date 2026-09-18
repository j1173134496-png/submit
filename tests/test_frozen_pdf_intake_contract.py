from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from submit_flow_agent.task_service import add_task_file, collect_task_pdf_batch
from submit_flow_agent.task_store import init_task


TASK_ID = "pdf-intake-contract-task"


class FrozenPdfIntakeContractTests(unittest.TestCase):
    def test_collect_pdf_batch_returns_openclaw_boundary_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            init_task(runtime, task_id=TASK_ID, site_key="xinan_high_school", site_name="新安高中部", month="2026-05")
            generation = _write_pdf(root / "opaque-a-202605.pdf", "发电单 电表编号 上期示数 本期示数 月总发电量")
            fee = _write_pdf(root / "opaque-b-202605.pdf", "电费结算单 上网电费 上网电价 结算电价 结算小计")
            energy = _write_pdf(root / "opaque-c-202605.pdf", "电量结算单 上网电量 结算电量 电量结算")

            with patch("submit_flow_agent.task_service._require_task_pricing_snapshot", return_value=None):
                payload = collect_task_pdf_batch(runtime, TASK_ID, [generation, fee, energy])
                replay = collect_task_pdf_batch(runtime, TASK_ID, [generation, fee, energy])

            for response in (payload, replay):
                self.assertEqual(response["batch_state"], "ready")
                self.assertEqual(response["received_count"], 3)
                self.assertEqual(response["required_count"], 3)
                self.assertEqual(response["missing_roles"], [])
                self.assertEqual(response["conflicting_roles"], [])
                self.assertEqual(response["next_allowed_action"], "run_task")
                self.assertEqual(len(response["classified_files"]), 3)
                self.assertEqual(
                    {item["file_type"] for item in response["classified_files"]},
                    {"generation_statement", "fee_statement", "energy_statement"},
                )
            self.assertEqual(payload, replay)
            events = _events(runtime / "tasks" / TASK_ID)
            self.assertEqual(sum(event["event"] == "pdf_staged" for event in events), 3)
            self.assertEqual(sum(event["event"] == "task_ready" for event in events), 1)

    def test_stages_until_three_pdfs_then_registers_roles_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            init_task(runtime, task_id=TASK_ID, site_key="xinan_high_school", site_name="新安高中部", month="2026-05")

            generation = _write_pdf(root / "wrong-fee-hint-202605.pdf", "发电单 电表编号 上期示数 本期示数 月总发电量")
            fee = _write_pdf(root / "wrong-energy-hint-202605.pdf", "电费结算单 上网电费 上网电价 结算电价 结算小计")
            energy = _write_pdf(root / "wrong-generation-hint-202605.pdf", "电量结算单 上网电量 结算电量 电量结算")

            with patch("submit_flow_agent.task_service._require_task_pricing_snapshot", return_value=None):
                first = add_task_file(runtime, TASK_ID, generation, file_type="fee_statement")
                second = add_task_file(runtime, TASK_ID, fee, file_type="energy_statement")
                third = add_task_file(runtime, TASK_ID, energy, file_type="generation_statement")

            self.assertEqual(first["status"], "collecting_files")
            self.assertEqual(second["status"], "collecting_files")
            self.assertTrue(all(value is None for value in second["files"].values()))
            self.assertEqual(second["pdf_intake"]["batch_state"], "staging")
            self.assertEqual(second["pdf_intake"]["received_count"], 2)

            self.assertEqual(third["status"], "ready_to_run")
            self.assertEqual(third["pdf_intake"]["batch_state"], "ready")
            self.assertEqual(set(third["files"]), {"generation_statement", "fee_statement", "energy_statement"})
            self.assertTrue(all(str(value).startswith("inputs/staged/") for value in third["files"].values()))
            self.assertEqual(
                {item["role"] for item in third["pdf_intake"]["role_results"]},
                {"generation_statement", "fee_statement", "energy_statement"},
            )
            self.assertTrue(all(item["confidence"] == "high" for item in third["pdf_intake"]["role_results"]))
            events = _events(runtime / "tasks" / TASK_ID)
            self.assertEqual(sum(event["event"] == "pdf_staged" for event in events), 3)
            self.assertEqual(sum(event["event"] == "file_received" for event in events), 3)
            self.assertEqual(sum(event["event"] == "task_ready" for event in events), 1)

    def test_repeated_same_pdf_is_idempotent_before_batch_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            init_task(runtime, task_id=TASK_ID, site_key="xinan_high_school", site_name="新安高中部", month="2026-05")
            source = _write_pdf(root / "generation-202605.pdf", "发电单 电表编号 上期示数 本期示数 月总发电量")

            with patch("submit_flow_agent.task_service._require_task_pricing_snapshot", return_value=None):
                first = add_task_file(runtime, TASK_ID, source)
                second = add_task_file(runtime, TASK_ID, source)

            self.assertEqual(first["pdf_intake"]["received_count"], 1)
            self.assertEqual(second["pdf_intake"]["received_count"], 1)
            events = _events(runtime / "tasks" / TASK_ID)
            self.assertEqual(sum(event["event"] == "pdf_staged" for event in events), 1)

    def test_duplicate_or_low_confidence_roles_enter_review_without_registered_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            init_task(runtime, task_id=TASK_ID, site_key="xinan_high_school", site_name="新安高中部", month="2026-05")
            first_generation = _write_pdf(root / "generation-a-202605.pdf", "发电单 电表编号 上期示数 本期示数 月总发电量")
            second_generation = _write_pdf(root / "generation-b-202605.pdf", "发电单 电表编号 上期示数 本期示数 月总发电量 第二块发电表")
            fee = _write_pdf(root / "fee-202605.pdf", "电费结算单 上网电费 上网电价 结算电价 结算小计")

            with patch("submit_flow_agent.task_service._require_task_pricing_snapshot", return_value=None):
                add_task_file(runtime, TASK_ID, first_generation)
                add_task_file(runtime, TASK_ID, second_generation)
                task = add_task_file(runtime, TASK_ID, fee)

            self.assertEqual(task["status"], "need_review")
            self.assertEqual(task["pdf_intake"]["batch_state"], "need_review")
            self.assertIn("energy_statement", task["pdf_intake"]["missing_roles"])
            self.assertIn("generation_statement", task["pdf_intake"]["conflicting_roles"])
            self.assertTrue(all(value is None for value in task["files"].values()))
            events = _events(runtime / "tasks" / TASK_ID)
            self.assertEqual(sum(event["event"] == "file_received" for event in events), 0)
            self.assertEqual(sum(event["event"] == "task_ready" for event in events), 0)


def _write_pdf(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _events(task_root: Path) -> list[dict[str, object]]:
    event_path = task_root / "audit" / "events.jsonl"
    return [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()



