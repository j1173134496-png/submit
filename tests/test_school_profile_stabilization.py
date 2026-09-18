from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from submit_flow_agent.artifact_index import ArtifactIndexError, write_task_artifact_index
from submit_flow_agent.config import list_sites, load_site_config, site_output_workbook_name
from submit_flow_agent.excel_writer import ExcelWriteError, _school_price_cell_value
from submit_flow_agent.field_mapper import validate_static_ocr_regexes
from submit_flow_agent.pricing import canonicalize_discount_rate
from submit_flow_agent.task_service import confirm_task_pricing, create_task, propose_task_pricing, retry_failed_task
from submit_flow_agent.task_store import load_task, write_task
from submit_flow_agent.template_profiles import (
    _site_workbook_name,
    _validate_new_site_identity,
    auto_register_site_from_profile,
    list_template_profiles,
)


class SchoolProfileStabilizationTests(unittest.TestCase):
    def test_new_site_rejects_meter_assigned_to_another_site(self) -> None:
        new_site = {
            "site_key": "future_school",
            "site_name": "Future School",
            "meter_no": "METER-001",
            "meters": [{"meter_no": "METER-001", "multiplier": "100"}],
        }
        existing = {
            "site_key": "existing_school",
            "site_name": "Existing School",
            "meter_no": "METER-001",
            "meters": [{"meter_no": "METER-001", "multiplier": "150"}],
        }

        with self.assertRaisesRegex(Exception, "meter-001 is already assigned to Existing School"):
            _validate_new_site_identity(new_site, [existing])

    def test_discount_normalization_preserves_meaningful_high_precision(self) -> None:
        self.assertEqual(canonicalize_discount_rate(Decimal("0.88888")), Decimal("0.88888"))

    def test_existing_site_pricing_removes_binary_float_artifact_from_discount_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            proposal = propose_task_pricing(
                Path(tmpdir) / "runtime",
                site_key="xinan_high_school",
                month="2026-05",
                pricing={"discount_rate": "0.8800000000000001", "effective_unit_price": "0.61424"},
            )

        self.assertEqual(proposal["pricing"]["discount_rate"], "0.88")
        self.assertEqual(proposal["pricing"]["effective_unit_price"], "0.61424")

    def test_profile_registry_has_two_active_meter_topology_profiles_and_two_workbooks(self) -> None:
        payload = list_template_profiles()
        profiles = payload["template_profiles"]
        active = [profile for profile in profiles if not profile["legacy_read_only"]]

        self.assertEqual([profile["default"] for profile in profiles].count(True), 1)
        self.assertEqual(
            {
                profile["meter_topology_profile"]: profile["profile_key"]
                for profile in active
                if profile["meter_topology_profile"] in {"single_meter_school", "dual_meter_school"}
            },
            {
                "single_meter_school": "xinan_single_meter_v1",
                "dual_meter_school": "xixiang_meter_rows_v1",
            },
        )
        for profile in profiles:
            self.assertEqual(set(profile["workbooks"]), {"detail", "summary"})
            self.assertFalse(
                {"base_unit_price", "school_base_price", "school_discount_rate", "effective_school_price"}
                & {field["key"] for field in profile["metadata_fields"]}
            )
            self.assertEqual(
                [field["key"] for field in profile["pricing_confirmation_fields"]],
                ["discount_rate", "effective_unit_price"],
            )

    def test_active_sites_expose_topology_and_future_xinan_second_selects_dual_xixiang_profile(self) -> None:
        payload = list_sites()
        sites = {site["site_key"]: site for site in payload["sites"]}

        self.assertNotIn("xinan_high_school", sites)
        self.assertEqual(sites["site_5f14cc7ab0e2"]["meter_topology_profile"], "single_meter_school")
        self.assertEqual(sites["site_5f14cc7ab0e2"]["template_profile_key"], "xinan_single_meter_v1")
        self.assertEqual(sites["xixiang_middle_school"]["meter_topology_profile"], "dual_meter_school")
        self.assertEqual(sites["xixiang_middle_school"]["template_profile_key"], "xixiang_meter_rows_v1")

        xinan_second = auto_register_site_from_profile(
            site_name="深圳市新安中学(集团)第二外国语学校",
            meter_topology_profile="dual_meter_school",
            metadata={},
        )
        self.assertEqual(xinan_second["status"], "waiting_for_required_metadata")
        self.assertEqual(xinan_second["profile_key"], "xixiang_meter_rows_v1")
        self.assertEqual(xinan_second["profile_selection"], "meter_topology")

    def test_auto_registration_asks_only_for_meter_topology_before_profile_selection(self) -> None:
        result = auto_register_site_from_profile(site_name="Future School", metadata={})

        self.assertEqual(result["status"], "waiting_for_meter_topology")
        self.assertEqual(result["error_code"], "METER_TOPOLOGY_REQUIRED")
        self.assertEqual(result["meter_topology_profiles"], ["dual_meter_school", "single_meter_school"])

    def test_auto_registration_selects_profile_from_topology_without_code_change(self) -> None:
        result = auto_register_site_from_profile(
            site_name="Future Dual Meter School",
            meter_topology_profile="dual_meter_school",
            metadata={},
        )

        self.assertEqual(result["status"], "waiting_for_required_metadata")
        self.assertEqual(result["profile_key"], "xixiang_meter_rows_v1")
        self.assertEqual(result["profile_selection"], "meter_topology")

    def test_dynamic_site_workbook_names_are_site_owned_not_master_site_names(self) -> None:
        self.assertEqual(_site_workbook_name("Future School", "detail"), "Future Schooldetail.xlsx")
        self.assertEqual(_site_workbook_name("Future School", "summary"), "Future Schoolsummary.xlsx")
        self.assertEqual(site_output_workbook_name("Future School", "detail", "2026-05"), "Future School统计表_2026-05.xlsx")
        self.assertEqual(site_output_workbook_name("Future School", "summary", "2026-05"), "Future School汇总表格_2026-05.xlsx")
        with self.assertRaisesRegex(Exception, "unsafe"):
            _site_workbook_name("Future/School", "summary")

    def test_new_dynamic_site_pricing_has_no_cross_site_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = _dynamic_pricing_required_config(Path(tmpdir), site_key="future_school")
            proposal = propose_task_pricing(
                Path(tmpdir) / "runtime",
                site_key="future_school",
                month="2026-05",
                pricing={"discount_rate": "0.95"},
                config_path=config_path,
            )

        self.assertFalse(proposal["ready_for_confirmation"])
        self.assertEqual(proposal["missing_fields"], ["effective_unit_price"])
        self.assertIsNone(proposal["pricing"])

    def test_existing_site_partial_pricing_reuses_settlement_price_without_discount_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            first = propose_task_pricing(
                runtime,
                site_key="xinan_high_school",
                month="2026-05",
                pricing={"discount_rate": "0.95", "effective_unit_price": "0.6998"},
            )
            snapshot = confirm_task_pricing(runtime, proposal_id=str(first["proposal_id"]))
            self.assertEqual(snapshot["effective_unit_price"], "0.6998")

            changed_header_only = propose_task_pricing(
                runtime,
                site_key="xinan_high_school",
                month="2026-06",
                pricing={"discount_rate": "0.85"},
            )

        self.assertTrue(changed_header_only["ready_for_confirmation"])
        self.assertEqual(changed_header_only["pricing"]["discount_rate"], "0.85")
        self.assertEqual(changed_header_only["pricing"]["effective_unit_price"], "0.6998")
        self.assertIn("effective_unit_price", changed_header_only["inherited_fields"])

    def test_dynamic_existing_site_inherits_latest_same_site_prior_month_pricing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            config_path = _dynamic_pricing_required_config(root, site_key="future_school")
            may = propose_task_pricing(
                runtime,
                site_key="future_school",
                month="2026-05",
                pricing={"discount_rate": "0.95", "effective_unit_price": "0.6998"},
                config_path=config_path,
            )
            confirm_task_pricing(runtime, proposal_id=str(may["proposal_id"]), config_path=config_path)

            june = propose_task_pricing(
                runtime,
                site_key="future_school",
                month="2026-06",
                pricing={},
                config_path=config_path,
            )

        self.assertTrue(june["ready_for_confirmation"])
        self.assertEqual(june["pricing"]["discount_rate"], "0.95")
        self.assertEqual(june["pricing"]["effective_unit_price"], "0.6998")
        self.assertEqual(set(june["inherited_fields"]), {"discount_rate", "effective_unit_price"})
        self.assertEqual(june["source_metadata"]["source_type"], "latest_same_site_confirmed_month")

    def test_dynamic_existing_site_partial_update_preserves_unsupplied_prior_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            config_path = _dynamic_pricing_required_config(root, site_key="future_school")
            may = propose_task_pricing(
                runtime,
                site_key="future_school",
                month="2026-05",
                pricing={"discount_rate": "0.95", "effective_unit_price": "0.6998"},
                config_path=config_path,
            )
            confirm_task_pricing(runtime, proposal_id=str(may["proposal_id"]), config_path=config_path)

            june = propose_task_pricing(
                runtime,
                site_key="future_school",
                month="2026-06",
                pricing={"discount_rate": "0.85"},
                config_path=config_path,
            )

        self.assertTrue(june["ready_for_confirmation"])
        self.assertEqual(june["pricing"]["discount_rate"], "0.85")
        self.assertEqual(june["pricing"]["effective_unit_price"], "0.6998")
        self.assertEqual(june["changed_fields"], ["discount_rate"])
        self.assertIn("effective_unit_price", june["inherited_fields"])

    def test_excel_price_cell_never_derives_settlement_price_from_discount(self) -> None:
        self.assertEqual(
            _school_price_cell_value(Decimal("0.6998"), Decimal("0.95"), Decimal("0.6998")),
            0.6998,
        )
        with self.assertRaisesRegex(ExcelWriteError, "effective_unit_price"):
            _school_price_cell_value(Decimal("0.6998"), Decimal("0.95"), None)

    def test_operator_pricing_rejects_legacy_base_unit_price_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(Exception, "accepts only discount_rate and effective_unit_price"):
                propose_task_pricing(
                    Path(tmpdir) / "runtime",
                    site_key="xinan_high_school",
                    month="2026-05",
                    pricing={
                        "discount_rate": "0.95",
                        "effective_unit_price": "0.6998",
                        "base_unit_price": "0.7366",
                    },
                )

    def test_malformed_static_ocr_regex_is_rejected_before_runtime_mapping(self) -> None:
        with self.assertRaisesRegex(Exception, "Static OCR regex pattern is invalid"):
            validate_static_ocr_regexes(["("])

    def test_artifact_index_contains_task_owned_artifacts_and_profile_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            root = runtime / "tasks" / "task_a"
            (root / "inputs").mkdir(parents=True)
            (root / "outputs").mkdir(parents=True)
            (root / "outputs" / "raw_ocr").mkdir(parents=True)
            (root / "inputs" / "a.pdf").write_bytes(b"%PDF-1.4\n")
            site = load_site_config("xixiang_middle_school")
            detail_name = site.output_workbook_name("detail", "2026-05")
            summary_name = site.output_workbook_name("summary", "2026-05")
            task = {
                "task_id": "task_a",
                "site_key": "xixiang_middle_school",
                "site_name": "xixiang",
                "month": "2026-05",
                "revision": 1,
                "status": "completed",
                "files": {"generation_statement": "inputs/a.pdf"},
                "outputs": [],
            }
            (root / "outputs" / detail_name).write_bytes(b"detail")
            (root / "outputs" / summary_name).write_bytes(b"summary")
            (root / "outputs" / "recognized_data.json").write_text("{}", encoding="utf-8")
            (root / "outputs" / "raw_ocr" / "generation.json").write_text("{}", encoding="utf-8")
            task["outputs"] = [
                f"outputs/{detail_name}",
                f"outputs/{summary_name}",
                "outputs/recognized_data.json",
                "outputs/raw_ocr/generation.json",
            ]
            task["pricing_snapshot"] = {
                "site_key": "xixiang_middle_school",
                "month": "2026-05",
                "effective_month": "2026-05",
                "pricing_version": 2,
                "pricing_source": "operator_confirmed",
                "confirmed_at": "2026-07-28T10:00:00+00:00",
                "proposal_id": "proposal-a",
                "proposal_hash": "proposal-hash-a",
                "pricing_hash": "pricing-hash-a",
                "discount_rate": "0.95",
                "effective_unit_price": "0.6998",
            }

            index_path = write_task_artifact_index(root, task)
            payload = json.loads((root / index_path).read_text(encoding="utf-8"))
            site_revision_exists = (runtime / payload["index_paths"]["site_revision"]).is_file()

        self.assertEqual(payload["site_contract"]["meter_topology_profile"], "dual_meter_school")
        self.assertEqual(
            {artifact["kind"] for artifact in payload["artifacts"]},
            {"source_pdf", "detail_workbook", "summary_workbook", "recognized_data", "ocr_raw"},
        )
        self.assertEqual(payload["pricing_snapshot"]["pricing_hash"], "pricing-hash-a")
        self.assertEqual(payload["pricing_snapshot"]["confirmed_at"], "2026-07-28T10:00:00+00:00")
        self.assertEqual(payload["index_paths"]["site_revision"], "artifact_index/xixiang_middle_school/2026-05/r0001.json")
        self.assertTrue(site_revision_exists)

    def test_artifact_index_rejects_paths_outside_task_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            root = runtime / "tasks" / "task_a"
            root.mkdir(parents=True)
            task = {
                "task_id": "task_a",
                "site_key": "xinan_high_school",
                "site_name": "xinan",
                "month": "2026-05",
                "status": "failed",
                "files": {"generation_statement": "../outside.pdf"},
            }

            with self.assertRaises(ArtifactIndexError):
                write_task_artifact_index(root, task)

    def test_artifact_index_skips_removed_intermediate_output_but_not_missing_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "runtime" / "tasks" / "task_a"
            root.mkdir(parents=True)
            task = {
                "task_id": "task_a",
                "site_key": "xinan_high_school",
                "site_name": "xinan",
                "month": "2026-05",
                "status": "failed",
                "files": {},
                "outputs": ["outputs/raw_ocr/images/removed.png"],
            }

            index_path = write_task_artifact_index(root, task)
            payload = json.loads((root / index_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["artifacts"], [])

            site = load_site_config("xinan_high_school")
            task["status"] = "completed"
            task["outputs"] = [f"outputs/{site.output_workbook_name('detail', '2026-05')}"]
            with self.assertRaises(ArtifactIndexError):
                write_task_artifact_index(root, task)

    def test_failed_task_retry_reuses_existing_files_and_does_not_create_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            proposal = propose_task_pricing(
                runtime,
                site_key="xixiang_middle_school",
                month="2026-05",
                pricing={"discount_rate": "0.95", "effective_unit_price": "0.6998"},
            )
            snapshot = confirm_task_pricing(runtime, proposal_id=str(proposal["proposal_id"]))
            task = create_task(
                runtime,
                task_id="failed_xixiang_task",
                site_key="xixiang_middle_school",
                month="2026-05",
                pricing_snapshot=snapshot,
            )
            root = runtime / "tasks" / task["task_id"]
            for file_type, filename in {
                "generation_statement": "generation.pdf",
                "fee_statement": "fee.pdf",
                "energy_statement": "energy.pdf",
            }.items():
                (root / "inputs" / filename).write_bytes(b"%PDF-1.4\n")
                task["files"][file_type] = f"inputs/{filename}"
            task["status"] = "failed"
            write_task(root, task)

            with (
                patch("submit_flow_agent.task_service._copy_site_templates"),
                patch("submit_flow_agent.task_service._copy_current_task_inputs"),
                patch("submit_flow_agent.task_service.run_month"),
                patch("submit_flow_agent.task_service._finalize_run") as finalize,
            ):
                finalize.return_value.task = {**task, "status": "completed"}
                finalize.return_value.task_dir = root
                retry_failed_task(runtime, task["task_id"])

            updated = load_task(root)

        self.assertEqual(updated["status"], "running")
        self.assertEqual(updated["failed_task_retry"]["registered_file_count"], 3)
        self.assertNotIn("restart_source_task_id", updated)


def _dynamic_pricing_required_config(tmpdir: Path, *, site_key: str) -> Path:
    source = json.loads(Path("config/sites.json").read_text(encoding="utf-8-sig"))
    site = dict(source["sites"][0])
    site["site_key"] = site_key
    site["site_name"] = "Future School"
    site["aliases"] = []
    site["dynamic_site_signature"] = "future-school-signature"
    site["pricing_required"] = True
    source["sites"] = [site]
    config_path = tmpdir / "sites.json"
    config_path.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


if __name__ == "__main__":
    unittest.main()





