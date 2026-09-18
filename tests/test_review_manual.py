from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from submit_flow_agent.field_mapper import REQUIRED_RECOGNIZED_FIELDS, map_fields_to_file
from submit_flow_agent.ocr.base import OcrBlock, OcrPage, OcrRawResult, write_ocr_raw_result
from submit_flow_agent.review import apply_confirmed_data, build_review_report
from submit_flow_agent.validator import validate_data_to_file


class ReviewManualTests(unittest.TestCase):
    def test_review_report_lists_low_confidence_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_dir = _write_sample_ocr_raw(root, low_confidence_text="上网电费")
            recognized_path = root / "recognized_data.json"
            validation_path = root / "validation_report.json"

            map_fields_to_file(_project_dir(), recognized_path, ocr_raw_dir=raw_dir)
            validation = validate_data_to_file(recognized_path, validation_path)
            review = build_review_report(
                month="2026-05",
                recognized_data_path=recognized_path,
                validation_report_path=validation_path,
                validation_report=validation,
            )

            self.assertEqual(review["task_status"], "need_review")
            issues = [issue for issue in review["issues"] if issue["field"] == "grid_fee" and issue["reason"] == "low_confidence"]
            self.assertEqual(len(issues), 1)
            issue = issues[0]
            self.assertEqual(issue["source_file"], "福域电费结算单202605_1.pdf")
            self.assertEqual(issue["page"], 1)
            self.assertIn("上网电费", issue["raw_text"])

    def test_review_report_lists_missing_field_from_mapping_error(self) -> None:
        error = RuntimeError("OCR field 'grid_export_kwh' was not found in 福域电量结算单202605_1-2.pdf.")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            review = build_review_report(
                month="2026-05",
                recognized_data_path=root / "recognized_data.json",
                validation_report_path=root / "validation_report.json",
                mapping_error=error,
            )

            self.assertEqual(review["issues"][0]["field"], "grid_export_kwh")
            self.assertEqual(review["issues"][0]["reason"], "field_missing")
            self.assertIn("not found", review["issues"][0]["message"])

    def test_review_report_keeps_business_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_dir = _write_sample_ocr_raw(root, replace_text=("上网电量 1680", "上网电量 50000"))
            recognized_path = root / "recognized_data.json"
            validation_path = root / "validation_report.json"

            map_fields_to_file(_project_dir(), recognized_path, ocr_raw_dir=raw_dir)
            validation = validate_data_to_file(recognized_path, validation_path)
            review = build_review_report(
                month="2026-05",
                recognized_data_path=recognized_path,
                validation_report_path=validation_path,
                validation_report=validation,
            )

            self.assertFalse(validation.can_write_excel)
            self.assertTrue(any(issue["reason"] == "business_validation_failed" for issue in review["issues"]))
            issue = next(issue for issue in review["issues"] if issue["field"] == "school_consumption_non_negative")
            self.assertIn("上网电量大于月总发电量", issue["message"])
            self.assertIn("grid_export_kwh", issue["confirmable_fields"])

    def test_apply_confirmed_data_rewrites_field_then_validation_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_dir = _write_sample_ocr_raw(root, replace_text=("上网电量 1680", "上网电量 50000"))
            recognized_path = root / "recognized_data.json"
            validation_path = root / "validation_report.json"
            confirmed_path = root / "confirmed_data.json"

            map_fields_to_file(_project_dir(), recognized_path, ocr_raw_dir=raw_dir)
            failed = validate_data_to_file(recognized_path, validation_path)
            self.assertFalse(failed.can_write_excel)
            confirmed_path.write_text(
                json.dumps(
                    {
                        "confirmed_by": "operator",
                        "confirmed_at": "2026-07-08T10:00:00+08:00",
                        "fields": {
                            "grid_export_kwh": {
                                "value": 1680,
                                "reason": "人工核对电量结算单后确认",
                            }
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            apply_confirmed_data(recognized_path, confirmed_path)
            passed = validate_data_to_file(recognized_path, validation_path)
            payload = json.loads(recognized_path.read_text(encoding="utf-8"))

            self.assertTrue(passed.can_write_excel)
            self.assertEqual(payload["mapping_mode"], "manual_confirmed")
            self.assertEqual(payload["fields"]["grid_export_kwh"]["source_type"], "manual_confirmed")
            self.assertEqual(payload["fields"]["grid_export_kwh"]["confidence"], 1.0)
            self.assertEqual(payload["meter_rows"][0]["grid_export_kwh"]["value"], 1680)
            self.assertEqual(payload["meter_rows"][0]["school_consumption_kwh"]["value"], 47888)


def _write_sample_ocr_raw(
    root: Path,
    *,
    low_confidence_text: str | None = None,
    replace_text: tuple[str, str] | None = None,
) -> Path:
    raw_dir = root / "raw_ocr"
    raw_dir.mkdir(parents=True, exist_ok=True)
    samples = {
        "福域发电单202605-2.pdf": [
            "电表编号 09001SF00000042508945216",
            "本期起始日期 2026-05-01",
            "本期结束日期 2026-06-01",
            "上期示数 816.82",
            "本期示数 1147.27",
            "倍率 150",
            "月总发电量 49568 kWh",
            "填表日期 2026-06-21",
        ],
        "福域电量结算单202605_1-2.pdf": ["上网电量 1680 kWh"],
        "福域电费结算单202605_1.pdf": ["上网电价 0.242434 元/kWh", "上网电费 407.29 元"],
    }
    for filename, texts in samples.items():
        blocks = []
        for index, text in enumerate(texts):
            if replace_text is not None:
                text = text.replace(replace_text[0], replace_text[1])
            confidence = 0.80 if low_confidence_text and low_confidence_text in text else 0.98
            blocks.append(OcrBlock(text=text, bbox=(0.0, float(index * 20), 200.0, float(index * 20 + 18)), confidence=confidence))
        result = OcrRawResult(
            source_file=_project_dir() / filename,
            pages=[OcrPage(page=1, image_path=Path("images") / f"{Path(filename).stem}-1.png", blocks=blocks)],
        )
        write_ocr_raw_result(result, raw_dir / f"{Path(filename).stem}.json")
    return raw_dir


def _project_dir() -> Path:
    return Path("深圳新安中学高中部650KW分布式光伏项目")


if __name__ == "__main__":
    unittest.main()
