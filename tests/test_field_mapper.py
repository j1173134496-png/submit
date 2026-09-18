from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from submit_flow_agent.config import ConfigError
from submit_flow_agent.field_mapper import (
    FieldMappingError,
    OCR_RAW_MAPPING_MODE,
    REQUIRED_RECOGNIZED_FIELDS,
    build_manual_baseline_recognized_data,
    build_ocr_recognized_data,
    map_fields_to_file,
    runtime_statement_date,
)
from submit_flow_agent.file_scanner import FileScanError
from submit_flow_agent.ocr.base import OcrBlock, OcrPage, OcrRawResult, write_ocr_raw_result
from submit_flow_agent.validator import validate_data_to_file


class FieldMapperTests(unittest.TestCase):
    def test_manual_baseline_contains_all_required_fields(self) -> None:
        data = build_manual_baseline_recognized_data(_project_dir())
        payload = data.to_dict()

        self.assertEqual(payload["site_key"], "xinan_high_school")
        self.assertEqual(payload["site_name"], "新安高中部")
        self.assertEqual(payload["project_name"], "新安中学（高中部）光伏发电项目")
        self.assertEqual(payload["month"], "2026-05")
        self.assertEqual(payload["mapping_mode"], "manual_baseline")
        self.assertEqual(set(payload["fields"]), set(REQUIRED_RECOGNIZED_FIELDS))

        for field in payload["fields"].values():
            self.assertIn("value", field)
            self.assertIn("source_file", field)
            self.assertIn("page", field)
            self.assertIn("confidence", field)
            self.assertIn("raw_text", field)
            self.assertIn("source_type", field)

    def test_manual_baseline_values_and_sources(self) -> None:
        fields = build_manual_baseline_recognized_data(_project_dir()).to_dict()["fields"]

        self.assertEqual(fields["previous_reading"]["value"], 816.82)
        self.assertEqual(fields["previous_reading"]["source_file"], "福域发电单202605-2.pdf")
        self.assertEqual(fields["grid_export_kwh"]["value"], 1680)
        self.assertEqual(fields["grid_export_kwh"]["source_file"], "福域电量结算单202605_1-2.pdf")
        self.assertEqual(fields["grid_price"]["source_file"], "福域电费结算单202605_1.pdf")
        self.assertEqual(fields["meter_no"]["value"], "09001SF00000042508945216")
        self.assertEqual(fields["meter_no"]["source_type"], "site_config")
        self.assertEqual(fields["multiplier"]["value"], 150)
        self.assertEqual(fields["school_base_price"]["value"], 0.6998)
        self.assertEqual(fields["statement_date"]["value"], "2026-06-21")

    def test_ocr_mapping_contains_all_required_fields_and_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_dir = _write_sample_ocr_raw(Path(tmpdir))

            data = build_ocr_recognized_data(_project_dir(), raw_dir, runtime_clock=_fixed_runtime_clock)
            payload = data.to_dict()
            fields = payload["fields"]

            self.assertEqual(payload["mapping_mode"], OCR_RAW_MAPPING_MODE)
            self.assertEqual(set(fields), set(REQUIRED_RECOGNIZED_FIELDS))
            self.assertEqual(fields["previous_reading"]["value"], 816.82)
            self.assertEqual(fields["previous_reading"]["source_type"], "ocr_raw")
            self.assertEqual(fields["previous_reading"]["source_file"], "福域发电单202605-2.pdf")
            self.assertEqual(fields["previous_reading"]["page"], 1)
            self.assertIn("上期示数", fields["previous_reading"]["raw_text"])
            self.assertEqual(fields["grid_export_kwh"]["value"], 1680)
            self.assertEqual(fields["grid_price"]["value"], 0.242434)
            self.assertEqual(fields["grid_fee"]["value"], 407.29)
            self.assertEqual(fields["statement_date"]["value"], "2026-07-17")
            self.assertEqual(fields["statement_date"]["source_file"], "system_runtime")
            self.assertIsNone(fields["statement_date"]["page"])
            self.assertEqual(fields["statement_date"]["confidence"], 1.0)
            self.assertEqual(fields["statement_date"]["raw_text"], "Asia/Shanghai runtime date: 2026-07-17")
            self.assertEqual(fields["statement_date"]["source_type"], "system_runtime")

    def test_write_recognized_data_json_file_from_ocr_raw(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_dir = _write_sample_ocr_raw(root)
            output = root / "recognized_data.json"

            data = map_fields_to_file(_project_dir(), output, ocr_raw_dir=raw_dir, runtime_clock=_fixed_runtime_clock)
            payload = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(payload["month"], "2026-05")
            self.assertEqual(payload["mapping_mode"], OCR_RAW_MAPPING_MODE)
            self.assertEqual(len(payload["fields"]), len(REQUIRED_RECOGNIZED_FIELDS))
            self.assertIn("Asia/Shanghai", "\n".join(payload["mapping_notes"]))
            self.assertIn("OCR raw structured text blocks", "\n".join(payload["mapping_notes"]))
            self.assertEqual(payload, data.to_dict())

    def test_ocr_mapping_reads_split_table_values_by_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_dir = _write_split_layout_ocr_raw(Path(tmpdir))

            fields = build_ocr_recognized_data(_project_dir(), raw_dir, runtime_clock=_fixed_runtime_clock).to_dict()["fields"]

            self.assertEqual(fields["period_start"]["value"], "2026-05-01")
            self.assertIn("上期截表时间", fields["period_start"]["raw_text"])
            self.assertEqual(fields["period_end"]["value"], "2026-06-01")
            self.assertEqual(fields["previous_reading"]["value"], 816.82)
            self.assertEqual(fields["current_reading"]["value"], 1147.27)
            self.assertEqual(fields["total_generation_kwh"]["value"], 49568)
            self.assertEqual(fields["grid_export_kwh"]["value"], 1680)
            self.assertEqual(fields["grid_price"]["value"], 0.242435)
            self.assertEqual(fields["grid_fee"]["value"], 407.29)
            self.assertEqual(fields["grid_fee"]["raw_text"], "电费 -> 407.29")
            self.assertEqual(fields["grid_fee"]["confidence"], 0.97)
            self.assertEqual(fields["statement_date"]["value"], "2026-07-17")

    def test_ocr_mapping_accepts_common_previous_period_header_misread(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_dir = _write_split_layout_ocr_raw(Path(tmpdir), previous_period_header="上期藏表时间")

            fields = build_ocr_recognized_data(
                _project_dir(),
                raw_dir,
                runtime_clock=_fixed_runtime_clock,
            ).to_dict()["fields"]

            self.assertEqual(fields["period_start"]["value"], "2026-05-01")
            self.assertEqual(fields["period_start"]["raw_text"], "上期藏表时间 -> 2026.05.01")
            self.assertEqual(fields["period_start"]["confidence"], 0.99)
            self.assertEqual(fields["period_end"]["value"], "2026-06-01")

    def test_ocr_mapping_reads_electronic_statement_meter_dates_on_same_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_dir = _write_split_layout_ocr_raw(Path(tmpdir), electronic_period_labels=True)

            fields = build_ocr_recognized_data(
                _project_dir(),
                raw_dir,
                runtime_clock=_fixed_runtime_clock,
            ).to_dict()["fields"]

            self.assertEqual(fields["period_start"]["value"], "2026-05-01")
            self.assertEqual(fields["period_start"]["raw_text"], "上次抄表日期 -> 2026-05-01")
            self.assertEqual(fields["period_end"]["value"], "2026-06-01")
            self.assertEqual(fields["period_end"]["raw_text"], "本次抄表日期 -> 2026-06-01")

    def test_electronic_statement_date_prefers_nearest_column_over_tiny_y_difference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_dir = _write_split_layout_ocr_raw(Path(tmpdir), electronic_period_labels=True)
            generation_json = raw_dir / "福域发电单202605-2.json"
            payload = json.loads(generation_json.read_text(encoding="utf-8"))
            for block in payload["pages"][0]["blocks"]:
                if block["text"] == "2026-05-01":
                    block["bbox"] = [480.0, 99.0, 570.0, 119.0]
            generation_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            fields = build_ocr_recognized_data(
                _project_dir(),
                raw_dir,
                runtime_clock=_fixed_runtime_clock,
            ).to_dict()["fields"]

            self.assertEqual(fields["period_end"]["value"], "2026-06-01")
            self.assertEqual(fields["period_start"]["value"], "2026-05-01")

    def test_ocr_mapping_without_statement_date_uses_system_runtime_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_dir = _write_sample_ocr_raw(Path(tmpdir), omit_text="填表日期")

            fields = build_ocr_recognized_data(_project_dir(), raw_dir, runtime_clock=_fixed_runtime_clock).to_dict()["fields"]

            self.assertEqual(fields["statement_date"]["value"], "2026-07-17")
            self.assertEqual(fields["statement_date"]["source_type"], "system_runtime")

    def test_ocr_historical_statement_date_does_not_override_runtime_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_dir = _write_sample_ocr_raw(Path(tmpdir))

            fields = build_ocr_recognized_data(_project_dir(), raw_dir, runtime_clock=_fixed_runtime_clock).to_dict()["fields"]

            self.assertEqual(fields["statement_date"]["value"], "2026-07-17")
            self.assertNotEqual(fields["statement_date"]["value"], "2026-06-21")

    def test_runtime_statement_date_uses_shanghai_date_across_utc_boundary(self) -> None:
        self.assertEqual(
            runtime_statement_date(lambda: datetime(2026, 7, 16, 16, 30, tzinfo=timezone.utc)),
            "2026-07-17",
        )

    def test_ocr_mapping_missing_field_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_dir = _write_sample_ocr_raw(Path(tmpdir), omit_text="上网电量")

            with self.assertRaisesRegex(FieldMappingError, "grid_export_kwh"):
                build_ocr_recognized_data(_project_dir(), raw_dir, runtime_clock=_fixed_runtime_clock)

    def test_low_confidence_ocr_field_blocks_validation_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_dir = _write_sample_ocr_raw(root, low_confidence_text="上网电费")
            recognized_path = root / "recognized_data.json"
            validation_path = root / "validation_report.json"

            map_fields_to_file(_project_dir(), recognized_path, ocr_raw_dir=raw_dir, runtime_clock=_fixed_runtime_clock)
            report = validate_data_to_file(recognized_path, validation_path)
            payload = json.loads(validation_path.read_text(encoding="utf-8"))

            self.assertEqual(report.status, "fail")
            self.assertFalse(report.can_write_excel)
            self.assertEqual(payload["status"], "fail")
            self.assertFalse(payload["can_write_excel"])
            self.assertTrue(any("grid_fee" in error and "置信度" in error for error in payload["errors"]))

    def test_missing_scan_file_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "福域发电单202605-2.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "福域电费结算单202605_1.pdf").write_bytes(b"%PDF-1.4\n")

            with self.assertRaisesRegex(FileScanError, "Missing required source file types"):
                build_manual_baseline_recognized_data(root)

    def test_unknown_site_raises_clear_config_error(self) -> None:
        with self.assertRaisesRegex(ConfigError, "was not found"):
            build_manual_baseline_recognized_data(_project_dir(), site_key="missing_site")

    def test_missing_config_field_raises_clear_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "sites.json"
            payload = json.loads(Path("config/sites.json").read_text(encoding="utf-8-sig"))
            del payload["sites"][0]["meters"]
            del payload["sites"][0]["meter_no"]
            config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "missing fields: meter_no"):
                build_manual_baseline_recognized_data(_project_dir(), config_path=config_path)

    def test_manual_baseline_rejects_other_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "福域发电单202606-2.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "福域电费结算单202606_1.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "福域电量结算单202606_1-2.pdf").write_bytes(b"%PDF-1.4\n")

            with self.assertRaisesRegex(FieldMappingError, "only supports 2026-05"):
                build_manual_baseline_recognized_data(root)


def _fixed_runtime_clock() -> datetime:
    return datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)


def _write_split_layout_ocr_raw(
    root: Path,
    *,
    previous_period_header: str = "上期截表时间",
    electronic_period_labels: bool = False,
) -> Path:
    raw_dir = root / "raw_ocr"
    raw_dir.mkdir(parents=True, exist_ok=True)
    samples = {
        "福域发电单202605-2.pdf": [
            *(
                [
                    OcrBlock("本次抄表日期", (100.0, 100.0, 210.0, 120.0), 0.98),
                    OcrBlock("2026-06-01", (230.0, 100.0, 320.0, 120.0), 0.99),
                    OcrBlock("上次抄表日期", (350.0, 100.0, 460.0, 120.0), 0.98),
                    OcrBlock("2026-05-01", (480.0, 100.0, 570.0, 120.0), 0.99),
                ]
                if electronic_period_labels
                else [
                    OcrBlock("本期截表时间", (100.0, 100.0, 200.0, 120.0), 0.98),
                    OcrBlock(
                        previous_period_header,
                        (300.0, 100.0, 400.0, 120.0),
                        0.88 if previous_period_header == "上期藏表时间" else 0.98,
                    ),
                    OcrBlock("2026.06.01", (105.0, 150.0, 195.0, 170.0), 0.99),
                    OcrBlock("2026.05.01", (305.0, 150.0, 395.0, 170.0), 0.99),
                ]
            ),
            OcrBlock("上期示数", (100.0, 220.0, 170.0, 240.0), 0.99),
            OcrBlock("本期示数", (240.0, 220.0, 310.0, 240.0), 0.99),
            OcrBlock("发电量(kWh)", (380.0, 220.0, 480.0, 240.0), 0.96),
            OcrBlock("816.82", (105.0, 270.0, 165.0, 290.0), 0.99),
            OcrBlock("1147.27", (245.0, 268.0, 305.0, 289.0), 0.99),
            OcrBlock("49568", (405.0, 270.0, 465.0, 290.0), 0.99),
            OcrBlock("填表日期 2026-06-21", (100.0, 340.0, 260.0, 360.0), 0.99),
            OcrBlock("资产编号", (100.0, 500.0, 180.0, 520.0), 0.99),
            OcrBlock("上月行码", (300.0, 500.0, 380.0, 520.0), 0.99),
            OcrBlock("本月行码", (450.0, 500.0, 530.0, 520.0), 0.99),
            OcrBlock("倍率", (600.0, 500.0, 650.0, 520.0), 0.99),
            OcrBlock("合计", (750.0, 500.0, 800.0, 520.0), 0.99),
            OcrBlock("09001SF000000425", (100.0, 550.0, 260.0, 570.0), 0.99),
            OcrBlock("08945216", (135.0, 572.0, 225.0, 592.0), 0.99),
            OcrBlock("816.82", (305.0, 550.0, 375.0, 570.0), 0.99),
            OcrBlock("1147.27", (450.0, 550.0, 530.0, 570.0), 0.99),
            OcrBlock("150", (605.0, 550.0, 645.0, 570.0), 0.99),
            OcrBlock("49568", (750.0, 550.0, 800.0, 570.0), 0.99),
            OcrBlock("09001SF000000425", (100.0, 600.0, 260.0, 620.0), 0.99),
            OcrBlock("08945216", (135.0, 622.0, 225.0, 642.0), 0.99),
            OcrBlock("10", (315.0, 600.0, 350.0, 620.0), 0.99),
            OcrBlock("20", (465.0, 600.0, 500.0, 620.0), 0.99),
            OcrBlock("150", (605.0, 600.0, 645.0, 620.0), 0.99),
            OcrBlock("1500", (755.0, 600.0, 795.0, 620.0), 0.99),
        ],
        "福域电量结算单202605_1-2.pdf": [
            OcrBlock("小计", (100.0, 100.0, 140.0, 120.0), 0.99),
            OcrBlock("1680", (400.0, 100.0, 460.0, 120.0), 0.99),
            OcrBlock("合计", (100.0, 150.0, 140.0, 170.0), 0.99),
            OcrBlock("1680", (400.0, 150.0, 460.0, 170.0), 0.99),
        ],
        "福域电费结算单202605_1.pdf": [
            OcrBlock("电费结算单", (300.0, 10.0, 420.0, 30.0), 0.99),
            OcrBlock("深圳市宝安区中心区N27区金科路", (280.0, 45.0, 560.0, 65.0), 0.99),
            OcrBlock("电量", (100.0, 100.0, 140.0, 120.0), 0.99),
            OcrBlock("电价", (250.0, 100.0, 290.0, 120.0), 0.99),
            OcrBlock("电费", (400.0, 100.0, 440.0, 120.0), 0.97),
            OcrBlock("市场化电费", (50.0, 150.0, 140.0, 170.0), 0.99),
            OcrBlock("1680", (105.0, 150.0, 145.0, 170.0), 0.99),
            OcrBlock("0.242435", (245.0, 150.0, 315.0, 170.0), 0.99),
            OcrBlock("407.29", (395.0, 150.0, 455.0, 170.0), 0.99),
        ],
    }
    for filename, blocks in samples.items():
        result = OcrRawResult(
            source_file=_project_dir() / filename,
            pages=[OcrPage(page=1, image_path=Path("images") / f"{Path(filename).stem}-1.png", blocks=blocks)],
        )
        write_ocr_raw_result(result, raw_dir / f"{Path(filename).stem}.json")
    return raw_dir


def _write_sample_ocr_raw(
    root: Path,
    *,
    omit_text: str | None = None,
    low_confidence_text: str | None = None,
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
        "福域电量结算单202605_1-2.pdf": [
            "结算月份 2026年05月",
            "上网电量 1680 kWh",
        ],
        "福域电费结算单202605_1.pdf": [
            "上网电价 0.242434 元/kWh",
            "上网电费 407.29 元",
        ],
    }
    for filename, texts in samples.items():
        blocks = []
        for index, text in enumerate(texts):
            if omit_text and omit_text in text:
                continue
            confidence = 0.80 if low_confidence_text and low_confidence_text in text else 0.98
            blocks.append(
                OcrBlock(
                    text=text,
                    bbox=(0.0, float(index * 20), 200.0, float(index * 20 + 18)),
                    confidence=confidence,
                )
            )
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

