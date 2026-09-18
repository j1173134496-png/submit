from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from submit_flow_agent.field_mapper import build_manual_baseline_recognized_data
from submit_flow_agent.normalizer import NormalizationError, normalize_recognized_data
from submit_flow_agent.validator import validate_data_to_file, validate_monthly_data


class NormalizerValidatorTests(unittest.TestCase):
    def test_sample_data_validates_and_can_write_excel(self) -> None:
        payload = _recognized_payload()
        report = validate_monthly_data(normalize_recognized_data(payload))
        data = report.to_dict()

        self.assertEqual(data["status"], "pass")
        self.assertTrue(data["can_write_excel"])
        self.assertEqual(data["month"], "2026-05")
        self.assertEqual(data["normalized_data"]["previous_reading"], 816.82)
        self.assertEqual(data["normalized_data"]["current_reading"], 1147.27)
        self.assertEqual(data["normalized_data"]["multiplier"], 150)
        self.assertEqual(data["normalized_data"]["school_discount_price"], 0.6998)
        self.assertEqual(data["normalized_data"]["school_consumption_kwh"], 47888)
        self.assertEqual(data["normalized_data"]["calculated_generation_kwh"], 49567.5)
        self.assertEqual(data["normalized_data"]["calculated_grid_fee"], 407.28912)
        self.assertEqual(data["normalized_data"]["calculated_school_fee"], 33512.0224)
        self.assertEqual(data["normalized_data"]["calculated_total_fee"], 33919.31152)
        total_check = next(check for check in data["checks"] if check["name"] == "total_fee")
        self.assertEqual(total_check["message"], "单电表汇总合计已按上网电费和学校消纳电费计算。")
        self.assertEqual(data["errors"], [])
        self.assertTrue(all(check["status"] == "pass" for check in data["checks"]))
        self.assertTrue(any("manual_baseline" in warning for warning in data["warnings"]))

    def test_validate_data_to_file_writes_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            recognized_path = root / "recognized_data.json"
            output_path = root / "validation_report.json"
            recognized_path.write_text(
                json.dumps(_recognized_payload(), ensure_ascii=False),
                encoding="utf-8",
            )

            report = validate_data_to_file(recognized_path, output_path)
            written = json.loads(output_path.read_text(encoding="utf-8"))

            self.assertEqual(written, report.to_dict())
            self.assertEqual(written["status"], "pass")
            self.assertTrue(written["can_write_excel"])

    def test_pre_august_source_generation_requires_confident_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            recognized_path = root / "recognized_data.json"
            output_path = root / "validation_report.json"
            payload = _recognized_payload()
            payload["fields"]["total_generation_kwh"].update(source_type="ocr_raw", confidence=0.1)
            payload["meter_rows"][0]["generation_kwh"].update(source_type="ocr_raw", confidence=0.1)
            recognized_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            report = validate_data_to_file(recognized_path, output_path)

            self.assertEqual(report.status, "fail")
            self.assertFalse(report.can_write_excel)

    def test_missing_required_field_is_clear_normalization_error(self) -> None:
        payload = _recognized_payload()
        del payload["fields"]["grid_fee"]

        with self.assertRaisesRegex(NormalizationError, "Missing required fields: grid_fee"):
            normalize_recognized_data(payload)

    def test_non_numeric_field_is_clear_normalization_error(self) -> None:
        payload = _recognized_payload()
        payload["fields"]["current_reading"]["value"] = "not-a-number"

        with self.assertRaisesRegex(NormalizationError, "current_reading.*numeric"):
            normalize_recognized_data(payload)

    def test_pre_august_reported_generation_is_authoritative(self) -> None:
        payload = _recognized_payload()
        payload["fields"]["total_generation_kwh"]["value"] = 48000
        payload["meter_rows"][0]["generation_kwh"]["value"] = 48000
        payload["meter_rows"][0]["school_consumption_kwh"]["value"] = 46320

        report = validate_monthly_data(normalize_recognized_data(payload)).to_dict()

        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["can_write_excel"])
        self.assertEqual(report["normalized_data"]["total_generation_kwh"], 48000)
        self.assertEqual(report["normalized_data"]["school_consumption_kwh"], 46320)
        self.assertTrue(any(check["name"] == "generation_formula" and check["status"] == "pass" for check in report["checks"]))

    def test_august_generation_formula_is_authoritative(self) -> None:
        payload = _recognized_payload()
        payload["month"] = "2026-08"
        payload["fields"]["month"] = {"value": "2026-08"}
        payload["fields"]["period_start"]["value"] = "2026-08-01"
        payload["fields"]["period_end"]["value"] = "2026-09-01"
        payload["fields"]["total_generation_kwh"]["value"] = 48000
        payload["meter_rows"][0]["generation_kwh"]["value"] = 48000
        report = validate_monthly_data(normalize_recognized_data(payload)).to_dict()

        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["can_write_excel"])
        self.assertEqual(report["normalized_data"]["total_generation_kwh"], 49568)
        self.assertTrue(any(check["name"] == "generation_formula" and check["status"] == "fail" for check in report["checks"]))

    def test_grid_export_greater_than_generation_blocks_excel(self) -> None:
        payload = _recognized_payload()
        payload["fields"]["grid_export_kwh"]["value"] = 50000

        report = validate_monthly_data(normalize_recognized_data(payload)).to_dict()

        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["can_write_excel"])
        self.assertTrue(any(check["name"] == "school_consumption_non_negative" and check["status"] == "fail" for check in report["checks"]))
        self.assertTrue(any("上网电量大于月总发电量" in error for error in report["errors"]))

    def test_site_grid_export_must_equal_meter_row_sum(self) -> None:
        payload = _recognized_payload()
        payload["fields"]["grid_export_kwh"]["value"] = 2000

        report = validate_monthly_data(normalize_recognized_data(payload)).to_dict()

        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["can_write_excel"])
        check = next(check for check in report["checks"] if check["name"] == "school_consumption_non_negative")
        self.assertEqual(check["status"], "fail")
        self.assertEqual(check["expected"], 2000)
        self.assertEqual(check["actual"], 1680)
        self.assertIn("各电表上网电量合计", check["message"])

    def test_grid_fee_mismatch_blocks_excel(self) -> None:
        payload = _recognized_payload()
        payload["fields"]["grid_fee"]["value"] = 500

        report = validate_monthly_data(normalize_recognized_data(payload)).to_dict()

        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["can_write_excel"])
        self.assertTrue(any(check["name"] == "grid_fee" and check["status"] == "fail" for check in report["checks"]))
        self.assertTrue(any("上网电费金额差异超过容差" in error for error in report["errors"]))

    def test_month_period_mismatch_blocks_excel(self) -> None:
        payload = _recognized_payload()
        payload["fields"]["period_end"]["value"] = "2026-05-31"

        report = validate_monthly_data(normalize_recognized_data(payload)).to_dict()

        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["can_write_excel"])
        self.assertTrue(any(check["name"] == "month_period" and check["status"] == "fail" for check in report["checks"]))
        self.assertTrue(any("月份和抄表周期不一致" in error for error in report["errors"]))


def _recognized_payload() -> dict[str, object]:
    return build_manual_baseline_recognized_data(
        Path("深圳新安中学高中部650KW分布式光伏项目")
    ).to_dict()


if __name__ == "__main__":
    unittest.main()
