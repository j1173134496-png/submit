from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from submit_flow_agent.pricing import (
    PricingError,
    _snapshot_hash,
    validate_task_pricing_snapshot,
)
from submit_flow_agent.task_series import note_task_status
from submit_flow_agent.task_service import (
    add_task_file,
    confirm_task_pricing,
    propose_task_pricing,
    resolve_task,
)
from submit_flow_agent.task_store import load_task, write_task


SITE_KEY = "xinan_high_school"
MONTH = "2026-05"


class TaskPricingRevisionRolloverTests(unittest.TestCase):
    def test_same_month_restart_accepts_historical_decimal_representation_without_rewriting_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            historical_snapshot = _rewrite_as_historical_discount(
                runtime,
                _confirmed_snapshot(
                    runtime,
                    discount_rate="0.88",
                    effective_unit_price="0.61424",
                ),
            )
            completed = resolve_task(
                runtime,
                site_key=SITE_KEY,
                month=MONTH,
                mode="locate",
                pricing_snapshot=historical_snapshot,
            )
            completed_root = runtime / "tasks" / completed["task_id"]
            completed_task = load_task(completed_root)
            completed_task["status"] = "completed"
            write_task(completed_root, completed_task)
            note_task_status(runtime, completed_task)

            pending = resolve_task(runtime, site_key=SITE_KEY, month=MONTH, mode="restart")
            restarted_snapshot = confirm_task_pricing(
                runtime,
                proposal_id=str(pending["pricing_proposal"]["proposal_id"]),
            )
            restarted = resolve_task(
                runtime,
                site_key=SITE_KEY,
                month=MONTH,
                mode="restart",
                pricing_snapshot=restarted_snapshot,
            )

            self.assertEqual(restarted["revision"], 2)
            self.assertEqual(restarted_snapshot, historical_snapshot)
            self.assertEqual(load_task(completed_root)["pricing_snapshot"], historical_snapshot)

    def test_historical_decimal_snapshot_still_rejects_tampered_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            historical_snapshot = _rewrite_as_historical_discount(
                runtime,
                _confirmed_snapshot(
                    runtime,
                    discount_rate="0.88",
                    effective_unit_price="0.61424",
                ),
            )
            tampered = {**historical_snapshot, "effective_unit_price": "0.7"}

            with self.assertRaisesRegex(PricingError, "content hash does not match"):
                validate_task_pricing_snapshot(tampered, site_key=SITE_KEY, month=MONTH)

    def test_same_month_restart_rehydrates_stale_series_from_verified_completed_task_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            snapshot = _confirmed_snapshot(runtime)
            completed = resolve_task(runtime, site_key=SITE_KEY, month=MONTH, mode="locate", pricing_snapshot=snapshot)
            completed_root = runtime / "tasks" / completed["task_id"]
            completed_task = load_task(completed_root)
            completed_task["status"] = "completed"
            write_task(completed_root, completed_task)
            note_task_status(runtime, completed_task)
            _remove_series_pricing_snapshot(runtime, revision=1)

            pending = resolve_task(runtime, site_key=SITE_KEY, month=MONTH, mode="restart")
            self.assertEqual(pending["resolution"], "restart_pricing_pending")
            self.assertEqual(pending["revision"], 1)
            self.assertFalse(pending["restart_contract"]["creates_revision"])
            restarted_snapshot = confirm_task_pricing(
                runtime,
                proposal_id=str(pending["pricing_proposal"]["proposal_id"]),
            )
            restarted = resolve_task(
                runtime,
                site_key=SITE_KEY,
                month=MONTH,
                mode="restart",
                pricing_snapshot=restarted_snapshot,
            )

            restarted_root = runtime / "tasks" / restarted["task_id"]
            restarted_task = load_task(restarted_root)
            self.assertEqual(restarted["revision"], 2)
            self.assertEqual(restarted_task["pricing_snapshot"], snapshot)
            self.assertEqual(restarted_task["restart_source_task_id"], completed["task_id"])
            series = _read_series(runtime)
            self.assertEqual(series["revisions"][0]["pricing_snapshot"], snapshot)
            self.assertEqual(series["revisions"][1]["pricing_snapshot"], snapshot)

            first_pdf = Path(tmpdir) / "发电单202605.pdf"
            first_pdf.write_bytes(b"%PDF-1.4\n% static regression fixture\n")
            updated = add_task_file(runtime, restarted["task_id"], first_pdf, file_type="generation_statement")
            self.assertEqual(updated["status"], "collecting_files")
            self.assertIsNone(updated["files"]["generation_statement"])
            self.assertEqual(updated["pdf_intake"]["received_count"], 1)

    def test_same_month_restart_uses_committed_pricing_history_when_task_copy_is_tampered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            snapshot = _confirmed_snapshot(runtime)
            completed = resolve_task(runtime, site_key=SITE_KEY, month=MONTH, mode="locate", pricing_snapshot=snapshot)
            completed_root = runtime / "tasks" / completed["task_id"]
            completed_task = load_task(completed_root)
            completed_task["status"] = "completed"
            completed_task["pricing_snapshot"] = {**snapshot, "pricing_hash": "tampered"}
            write_task(completed_root, completed_task)
            note_task_status(runtime, completed_task)
            _remove_series_pricing_snapshot(runtime, revision=1)

            pending = resolve_task(runtime, site_key=SITE_KEY, month=MONTH, mode="restart")
            self.assertEqual(pending["pricing_proposal"]["pricing"]["effective_unit_price"], "0.6998")
            restarted_snapshot = confirm_task_pricing(
                runtime,
                proposal_id=str(pending["pricing_proposal"]["proposal_id"]),
            )
            restarted = resolve_task(
                runtime,
                site_key=SITE_KEY,
                month=MONTH,
                mode="restart",
                pricing_snapshot=restarted_snapshot,
            )
            self.assertEqual(restarted["revision"], 2)
            self.assertEqual(restarted["pricing_snapshot"], snapshot)

    def test_same_month_restart_uses_committed_pricing_history_when_task_copy_month_is_mismatched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            snapshot = _confirmed_snapshot(runtime)
            completed = resolve_task(runtime, site_key=SITE_KEY, month=MONTH, mode="locate", pricing_snapshot=snapshot)
            completed_root = runtime / "tasks" / completed["task_id"]
            completed_task = load_task(completed_root)
            completed_task["status"] = "completed"
            completed_task["pricing_snapshot"] = {**snapshot, "month": "2026-06"}
            write_task(completed_root, completed_task)
            note_task_status(runtime, completed_task)
            _remove_series_pricing_snapshot(runtime, revision=1)

            pending = resolve_task(runtime, site_key=SITE_KEY, month=MONTH, mode="restart")
            self.assertEqual(pending["pricing_proposal"]["pricing"]["effective_unit_price"], "0.6998")
            restarted_snapshot = confirm_task_pricing(
                runtime,
                proposal_id=str(pending["pricing_proposal"]["proposal_id"]),
            )
            restarted = resolve_task(
                runtime,
                site_key=SITE_KEY,
                month=MONTH,
                mode="restart",
                pricing_snapshot=restarted_snapshot,
            )
            self.assertEqual(restarted["revision"], 2)
            self.assertEqual(restarted["pricing_snapshot"], snapshot)


def _confirmed_snapshot(
    runtime: Path,
    *,
    discount_rate: str = "0.95",
    effective_unit_price: str = "0.6998",
) -> dict[str, object]:
    proposal = propose_task_pricing(
        runtime,
        site_key=SITE_KEY,
        month=MONTH,
        pricing={"discount_rate": discount_rate, "effective_unit_price": effective_unit_price},
    )
    if not proposal["ready_for_confirmation"]:
        raise AssertionError(proposal)
    return confirm_task_pricing(runtime, proposal_id=str(proposal["proposal_id"]))


def _rewrite_as_historical_discount(
    runtime: Path,
    snapshot: dict[str, object],
) -> dict[str, object]:
    historical_snapshot = {**snapshot, "discount_rate": "0.8800000000000001"}
    historical_snapshot["pricing_hash"] = _snapshot_hash(historical_snapshot)
    pricing_root = runtime / "site_pricing" / SITE_KEY
    history_path = pricing_root / "pricing.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    history["versions"][0] = historical_snapshot
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    proposal_path = pricing_root / "proposals" / f"{snapshot['proposal_id']}.json"
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    proposal["committed_snapshot"] = historical_snapshot
    proposal["committed_snapshot_hash"] = historical_snapshot["pricing_hash"]
    proposal_path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    return historical_snapshot


def _remove_series_pricing_snapshot(runtime: Path, *, revision: int) -> None:
    series_path = runtime / "site_series" / SITE_KEY / MONTH / "series.json"
    series = json.loads(series_path.read_text(encoding="utf-8"))
    for item in series["revisions"]:
        if item["revision"] == revision:
            item.pop("pricing_snapshot", None)
    series_path.write_text(json.dumps(series, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_series(runtime: Path) -> dict[str, object]:
    series_path = runtime / "site_series" / SITE_KEY / MONTH / "series.json"
    return json.loads(series_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


