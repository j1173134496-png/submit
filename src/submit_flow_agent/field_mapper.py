"""Field mapping for recognized_data.json generation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from submit_flow_agent.config import GRID_EXPORT_SITE_TOTAL_ALLOCATIONS, SiteConfig, load_site_config
from submit_flow_agent.file_scanner import ScanResult, scan_input_files
from submit_flow_agent.ocr.base import OcrBlock, OcrPage, OcrRawResult, load_ocr_raw_result


MANUAL_BASELINE_MAPPING_MODE = "manual_baseline"
OCR_RAW_MAPPING_MODE = "ocr_raw"
MIN_OCR_CONFIDENCE = 0.90
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
RuntimeClock = Callable[[], date | datetime]
REQUIRED_RECOGNIZED_FIELDS = (
    "site_key",
    "site_name",
    "project_name",
    "month",
    "meter_no",
    "period_start",
    "period_end",
    "previous_reading",
    "current_reading",
    "multiplier",
    "total_generation_kwh",
    "grid_export_kwh",
    "grid_price",
    "grid_fee",
    "school_base_price",
    "school_discount_rate",
    "effective_unit_price",
    "company_name",
    "statement_date",
)
_DATE_PATTERN = r"(\d{4}(?:[-/.]|\u5e74)\d{1,2}(?:[-/.]|\u6708)\d{1,2}(?:\u65e5)?)"
_NUMBER_PATTERN = r"(\d+(?:\.\d+)?)"
OCR_FIELD_SPECS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "period_start": (
        "generation_statement",
        (
            rf"(?:\u672c\u671f\u8d77\u59cb\u65e5\u671f|\u8d77\u59cb\u65e5\u671f|\u6284\u8868\u5f00\u59cb\u65e5\u671f)\s*[:\uff1a]?\s*{_DATE_PATTERN}",
        ),
        "date",
    ),
    "period_end": (
        "generation_statement",
        (
            rf"(?:\u672c\u671f\u7ed3\u675f\u65e5\u671f|\u7ed3\u675f\u65e5\u671f|\u6284\u8868\u7ed3\u675f\u65e5\u671f)\s*[:\uff1a]?\s*{_DATE_PATTERN}",
        ),
        "date",
    ),
    "grid_export_kwh": (
        "energy_statement",
        (rf"(?:\u4e0a\u7f51\u7535\u91cf|\u7ed3\u7b97\u7535\u91cf)\s*[:\uff1a]?\s*{_NUMBER_PATTERN}",),
        "number",
    ),
    "grid_price": (
        "fee_statement",
        (rf"(?:\u4e0a\u7f51\u7535\u4ef7|\u7ed3\u7b97\u7535\u4ef7)\s*[:\uff1a]?\s*{_NUMBER_PATTERN}",),
        "number",
    ),
    "grid_fee": (
        "fee_statement",
        (rf"(?:\u4e0a\u7f51\u7535\u8d39|\u7ed3\u7b97\u7535\u8d39|\u7535\u8d39)\s*[:\uff1a]?\s*{_NUMBER_PATTERN}",),
        "number",
    ),
}
FALLBACK_SINGLE_METER_SPECS: dict[str, tuple[tuple[str, ...], str]] = {
    "previous_reading": (
        (rf"(?:\u4e0a\u671f\u793a\u6570|\u4e0a\u6b21\u793a\u6570|\u4e0a\u6708\u8bfb\u6570)\s*[:\uff1a]?\s*{_NUMBER_PATTERN}",),
        "number",
    ),
    "current_reading": (
        (rf"(?:\u672c\u671f\u793a\u6570|\u672c\u6b21\u793a\u6570|\u672c\u6708\u8bfb\u6570)\s*[:\uff1a]?\s*{_NUMBER_PATTERN}",),
        "number",
    ),
    "total_generation_kwh": (
        (rf"(?:\u6708\u603b\u53d1\u7535\u91cf|\u603b\u53d1\u7535\u91cf|\u53d1\u7535\u91cf)\s*[:\uff1a]?\s*{_NUMBER_PATTERN}",),
        "number",
    ),
}
_LAYOUT_BELOW_HEADERS: dict[str, tuple[str, ...]] = {
    "period_start": (
        "\u4e0a\u671f\u622a\u8868\u65f6\u95f4",
        "\u4e0a\u671f\u85cf\u8868\u65f6\u95f4",
        "\u4e0a\u671f\u622a\u6b62\u65f6\u95f4",
        "\u4e0a\u671f\u6284\u8868\u65f6\u95f4",
    ),
    "period_end": ("\u672c\u671f\u622a\u8868\u65f6\u95f4", "\u672c\u671f\u622a\u6b62\u65f6\u95f4", "\u672c\u671f\u6284\u8868\u65f6\u95f4"),
    "previous_reading": ("\u4e0a\u671f\u793a\u6570",),
    "current_reading": ("\u672c\u671f\u793a\u6570",),
    "total_generation_kwh": ("\u6708\u603b\u53d1\u7535\u91cf", "\u603b\u53d1\u7535\u91cf", "\u53d1\u7535\u91cf"),
    "grid_price": ("\u4e0a\u7f51\u7535\u4ef7", "\u7ed3\u7b97\u7535\u4ef7", "\u7535\u4ef7"),
    "grid_fee": ("\u4e0a\u7f51\u7535\u8d39", "\u7ed3\u7b97\u7535\u8d39", "\u7535\u8d39"),
}
_LAYOUT_ROW_RIGHT_LABELS: dict[str, tuple[str, ...]] = {
    "period_start": ("\u4e0a\u6b21\u6284\u8868\u65e5\u671f",),
    "period_end": ("\u672c\u6b21\u6284\u8868\u65e5\u671f",),
    "grid_export_kwh": ("\u5408\u8ba1", "\u5c0f\u8ba1", "\u7ed3\u7b97\u7535\u91cf\u5408\u8ba1"),
}
_LAYOUT_SETTLEMENT_SUBTOTAL_LABELS = ("\u7ed3\u7b97\u5c0f\u8ba1",)
VALUE_TEXT_PATTERNS = {
    "date": r"\d{4}(?:[-/.]|\u5e74)\d{1,2}(?:[-/.]|\u6708)\d{1,2}(?:\u65e5)?",
    "number": r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])",
}
STATIC_OCR_REGEX_PATTERNS = (
    *(pattern for _, patterns, _ in OCR_FIELD_SPECS.values() for pattern in patterns),
    *(pattern for patterns, _ in FALLBACK_SINGLE_METER_SPECS.values() for pattern in patterns),
    *VALUE_TEXT_PATTERNS.values(),
)


class FieldMappingError(RuntimeError):
    """Raised when recognized_data.json cannot be generated."""


def runtime_statement_date(runtime_clock: RuntimeClock | None = None) -> str:
    """Return the trusted runtime statement date in Asia/Shanghai."""

    value = runtime_clock() if runtime_clock is not None else datetime.now(SHANGHAI_TZ)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI_TZ)
        else:
            value = value.astimezone(SHANGHAI_TZ)
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise FieldMappingError("runtime_clock must return a date or datetime.")


def validate_static_ocr_regexes(patterns: Iterable[str] = STATIC_OCR_REGEX_PATTERNS) -> None:
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise FieldMappingError(f"Static OCR regex pattern is invalid: {pattern}") from exc


@dataclass(frozen=True)
class RecognizedField:
    value: str | int | float | bool
    source_file: str
    page: int | None
    confidence: float
    raw_text: str
    source_type: str

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "source_file": self.source_file,
            "page": self.page,
            "confidence": self.confidence,
            "raw_text": self.raw_text,
            "source_type": self.source_type,
        }


@dataclass(frozen=True)
class RecognizedData:
    site_key: str
    site_name: str
    project_name: str
    month: str
    fields: dict[str, RecognizedField]
    mapping_notes: list[str]
    meter_rows: list[dict[str, RecognizedField]]
    mapping_mode: str = OCR_RAW_MAPPING_MODE
    validation_not_applicable_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "site_key": self.site_key,
            "site_name": self.site_name,
            "project_name": self.project_name,
            "month": self.month,
            "mapping_mode": self.mapping_mode,
            "fields": {
                field_name: field.to_dict()
                for field_name, field in self.fields.items()
            },
            "meter_rows": [
                {
                    field_name: field.to_dict()
                    for field_name, field in row.items()
                }
                for row in self.meter_rows
            ],
            "mapping_notes": self.mapping_notes,
            "validation_not_applicable_fields": list(self.validation_not_applicable_fields),
        }


@dataclass(frozen=True)
class _OcrEvidence:
    source_file: str
    page: int
    index: int
    block: OcrBlock


def build_ocr_recognized_data(
    project_dir: Path | str,
    ocr_raw_dir: Path | str,
    *,
    site_key: str = "xinan_high_school",
    config_path: Path | str = "config/sites.json",
    runtime_clock: RuntimeClock | None = None,
    pricing_snapshot: dict[str, Any] | None = None,
    expected_month: str | None = None,
) -> RecognizedData:
    validate_static_ocr_regexes()
    scan_result = scan_input_files(project_dir, expected_month=expected_month)
    site = load_site_config(site_key=site_key, config_path=config_path)
    raw_results = _load_ocr_raw_results(Path(ocr_raw_dir))
    statement_date = runtime_statement_date(runtime_clock)
    fields, meter_rows = _ocr_fields(site, scan_result, raw_results, Path(config_path), statement_date)
    _apply_pricing_snapshot_fields(site, fields, pricing_snapshot)
    required_fields = _required_recognized_fields(site)
    missing = [field for field in required_fields if field not in fields]
    if missing:
        raise FieldMappingError(f"OCR mapping is missing fields: {', '.join(missing)}.")

    return RecognizedData(
        site_key=site.site_key,
        site_name=site.site_name,
        project_name=site.project_name,
        month=scan_result.month,
        fields={field: fields[field] for field in REQUIRED_RECOGNIZED_FIELDS if field in fields},
        meter_rows=meter_rows,
        mapping_mode=OCR_RAW_MAPPING_MODE,
        validation_not_applicable_fields=site.validation_not_applicable_fields,
        mapping_notes=[
            "MVP-010 uses OCR raw structured text blocks to map business fields.",
            f"statement_date uses Asia/Shanghai runtime date {statement_date}; it is not extracted from OCR text, filenames, CLI, or Worker payload.",
            f"Key OCR fields retain source_file, page, raw_text and confidence; fields below {MIN_OCR_CONFIDENCE:.2f} are blocked by validation before writing.",
            "meter_no, multiplier, base electricity price, discount rate, and company name may fall back to site config; OCR conflicts require review/manual handling.",
        ],
    )


def build_manual_baseline_recognized_data(
    project_dir: Path | str,
    *,
    site_key: str = "xinan_high_school",
    config_path: Path | str = "config/sites.json",
    pricing_snapshot: dict[str, Any] | None = None,
) -> RecognizedData:
    """Build recognized_data.json from the confirmed 2026-05 manual baseline."""

    scan_result = scan_input_files(project_dir)
    site = load_site_config(site_key=site_key, config_path=config_path)
    if scan_result.month != "2026-05":
        raise FieldMappingError(
            f"manual_baseline mode only supports 2026-05, got {scan_result.month}."
        )

    fields = _manual_baseline_fields(site, scan_result, Path(config_path))
    _apply_pricing_snapshot_fields(site, fields, pricing_snapshot)
    meter_rows = [_legacy_meter_row(fields)]
    for field_name in site.validation_not_applicable_fields:
        fields.pop(field_name, None)
    required_fields = _required_recognized_fields(site)
    missing = [field for field in required_fields if field not in fields]
    if missing:
        raise FieldMappingError(f"Recognized data is missing fields: {', '.join(missing)}.")

    return RecognizedData(
        site_key=site.site_key,
        site_name=site.site_name,
        project_name=site.project_name,
        month=scan_result.month,
        fields={field: fields[field] for field in REQUIRED_RECOGNIZED_FIELDS if field in fields},
        meter_rows=meter_rows,
        mapping_mode=MANUAL_BASELINE_MAPPING_MODE,
        validation_not_applicable_fields=site.validation_not_applicable_fields,
        mapping_notes=[
            "MVP-005 currently uses manually confirmed sample baseline data; it can later be replaced by OCR text-block mapping.",
            "Current mapping_mode=manual_baseline; it does not claim to come from real OCR.",
            "statement_date temporarily uses sample value 2026-06-21 pending final business confirmation.",
            "period_start=2026-05-01 and period_end=2026-06-01 follow sample baseline output.",
            "meter_no, multiplier, base electricity price, discount rate, and company name may fall back to site config.",
        ],
    )


def write_recognized_data(recognized_data: RecognizedData, output_path: Path | str) -> Path:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(recognized_data.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def _apply_pricing_snapshot_fields(
    site: SiteConfig,
    fields: dict[str, RecognizedField],
    pricing_snapshot: dict[str, Any] | None,
) -> None:
    if pricing_snapshot is None:
        effective = site.effective_school_price if site.effective_school_price is not None else site.school_base_price
        fields["effective_unit_price"] = _site_config_field(
            _decimal_value(effective),
            "site_config_legacy_pricing",
            "Legacy direct settlement_unit_price from effective_school_price or school_base_price.",
        )
        return
    discount = _pricing_decimal(pricing_snapshot, "discount_rate")
    effective = _pricing_decimal(pricing_snapshot, "effective_unit_price")
    fields["school_base_price"] = _site_config_field(
        _decimal_value(effective),
        "task_pricing_snapshot",
        "Task pricing snapshot settlement_unit_price compatibility field school_base_price.",
    )
    fields["school_discount_rate"] = _site_config_field(
        _decimal_value(discount),
        "task_pricing_snapshot",
        "Task pricing snapshot field school_discount_rate.",
    )
    fields["effective_unit_price"] = _site_config_field(
        _decimal_value(effective),
        "task_pricing_snapshot",
        "Task pricing snapshot direct settlement_unit_price.",
    )


def _pricing_decimal(snapshot: dict[str, Any], field: str) -> Decimal:
    value = snapshot.get(field)
    if value is None or str(value).strip() == "":
        raise FieldMappingError(f"pricing_snapshot missing {field}.")
    return Decimal(str(value))


def _pricing_optional_decimal(snapshot: dict[str, Any], field: str) -> Decimal | None:
    value = snapshot.get(field)
    if value is None or str(value).strip() == "":
        return None
    return Decimal(str(value))


def _required_recognized_fields(site: SiteConfig) -> tuple[str, ...]:
    exempt = set(site.validation_not_applicable_fields)
    return tuple(field for field in REQUIRED_RECOGNIZED_FIELDS if field not in exempt)


def map_fields_to_file(
    project_dir: Path | str,
    output_path: Path | str,
    *,
    site_key: str = "xinan_high_school",
    config_path: Path | str = "config/sites.json",
    ocr_raw_dir: Path | str | None = None,
    runtime_clock: RuntimeClock | None = None,
    pricing_snapshot: dict[str, Any] | None = None,
    expected_month: str | None = None,
) -> RecognizedData:
    if ocr_raw_dir is None:
        raise FieldMappingError("ocr_raw_dir is required for OCR field mapping.")
    recognized_data = build_ocr_recognized_data(
        project_dir,
        ocr_raw_dir,
        site_key=site_key,
        config_path=config_path,
        runtime_clock=runtime_clock,
        pricing_snapshot=pricing_snapshot,
        expected_month=expected_month,
    )
    write_recognized_data(recognized_data, output_path)
    return recognized_data


def _load_ocr_raw_results(ocr_raw_dir: Path) -> list[OcrRawResult]:
    if not ocr_raw_dir.exists():
        raise FieldMappingError(f"OCR raw directory does not exist: {ocr_raw_dir}")
    paths = sorted(path for path in ocr_raw_dir.glob("*.json") if path.is_file())
    if not paths:
        raise FieldMappingError(f"OCR raw directory has no JSON files: {ocr_raw_dir}")
    try:
        return [load_ocr_raw_result(path, base_dir=ocr_raw_dir.parent) for path in paths]
    except ValueError as exc:
        raise FieldMappingError(str(exc)) from exc


def _ocr_fields(
    site: SiteConfig,
    scan_result: ScanResult,
    raw_results: list[OcrRawResult],
    config_path: Path,
    statement_date: str,
) -> tuple[dict[str, RecognizedField], list[dict[str, RecognizedField]]]:
    config_source = _display_config_path(config_path)
    evidence = list(_iter_evidence(raw_results))

    fields: dict[str, RecognizedField] = {
        "site_key": _site_config_field(site.site_key, config_source, "site config field site_key"),
        "site_name": _site_config_field(site.site_name, config_source, "site config field site_name"),
        "project_name": _site_config_field(site.project_name, config_source, "site config field project_name"),
        "month": RecognizedField(
            value=scan_result.month,
            source_file=", ".join(path.name for path in scan_result.files.values()),
            page=None,
            confidence=1.0,
            raw_text=f"Resolved {scan_result.month} from source PDFs and controlled task month context.",
            source_type="derived",
        ),
        "meter_no": _site_config_field(
            site.meter_no,
            config_source,
            "site config golden field meter_no; OCR text is retained only for conflict review.",
        ),
        "multiplier": _site_config_field(
            _decimal_value(site.multiplier),
            config_source,
            "site config golden field multiplier; OCR text is retained only for conflict review.",
        ),
        "school_base_price": _site_config_field(
            _decimal_value(site.school_base_price),
            config_source,
            "site config field school_base_price.",
        ),
        "school_discount_rate": _site_config_field(
            _decimal_value(site.school_discount_rate),
            config_source,
            "site config field school_discount_rate.",
        ),
        "company_name": _site_config_field(site.company_name, config_source, "site config field company_name"),
        "statement_date": _system_runtime_field(statement_date),
    }

    not_applicable = set(site.validation_not_applicable_fields)
    missing_ocr_fields: list[str] = []
    for field_name, (file_type, patterns, value_type) in OCR_FIELD_SPECS.items():
        if field_name in not_applicable:
            continue
        expected_file = scan_result.files[file_type].name
        field_evidence = [item for item in evidence if Path(item.source_file).name == expected_file]
        try:
            fields[field_name] = _extract_ocr_field(field_name, expected_file, field_evidence, patterns, value_type)
        except FieldMappingError:
            missing_ocr_fields.append(field_name)
    if missing_ocr_fields:
        raise FieldMappingError(f"OCR mapping is missing fields: {', '.join(missing_ocr_fields)}.")

    generation_file = scan_result.files["generation_statement"].name
    generation_evidence = [item for item in evidence if Path(item.source_file).name == generation_file]
    try:
        meter_rows = _extract_meter_rows(site, generation_file, generation_evidence, fields["grid_export_kwh"])
        fields.update(_legacy_fields_from_meter_rows(meter_rows))
        fields["total_generation_kwh"] = _total_generation_field(generation_file, meter_rows)
    except FieldMappingError:
        if len(site.meters) != 1:
            raise
        fields.update(_fallback_single_meter_fields(generation_file, generation_evidence))
        meter_rows = [_legacy_meter_row(fields)]

    for field_name in not_applicable:
        fields.pop(field_name, None)

    return fields, meter_rows


def _iter_evidence(raw_results: Iterable[OcrRawResult]) -> Iterable[_OcrEvidence]:
    for result in raw_results:
        source_file = Path(result.source_file).name
        for page in result.pages:
            for index, block in enumerate(page.blocks):
                yield _OcrEvidence(source_file=source_file, page=page.page, index=index, block=block)


def _extract_ocr_field(
    field_name: str,
    expected_file: str,
    evidence: list[_OcrEvidence],
    patterns: tuple[str, ...],
    value_type: str,
) -> RecognizedField:
    for item in evidence:
        text = _normalize_text(item.block.text)
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = _coerce_ocr_value(match.group(1), value_type)
                return RecognizedField(
                    value=value,
                    source_file=expected_file,
                    page=item.page,
                    confidence=item.block.confidence,
                    raw_text=item.block.text,
                    source_type="ocr_raw",
                )

    layout_field = _extract_layout_field(field_name, expected_file, evidence, value_type)
    if layout_field is not None:
        return layout_field

    raise FieldMappingError(f"OCR field '{field_name}' was not found in {expected_file}.")


def _extract_layout_field(
    field_name: str,
    expected_file: str,
    evidence: list[_OcrEvidence],
    value_type: str,
) -> RecognizedField | None:
    if field_name == "grid_price":
        field = _extract_nth_value_on_label_row(expected_file, evidence, _LAYOUT_SETTLEMENT_SUBTOTAL_LABELS, value_type, 2)
        if field is not None:
            return field
    if field_name == "grid_fee":
        field = _extract_nth_value_on_label_row(expected_file, evidence, _LAYOUT_SETTLEMENT_SUBTOTAL_LABELS, value_type, 3)
        if field is not None:
            return field
    if field_name in _LAYOUT_BELOW_HEADERS:
        field = _extract_value_below_header(
            expected_file,
            evidence,
            _LAYOUT_BELOW_HEADERS[field_name],
            value_type,
            trusted_header_aliases=("\u4e0a\u671f\u85cf\u8868\u65f6\u95f4",) if field_name == "period_start" else (),
        )
        if field is not None:
            return field
    if field_name in _LAYOUT_ROW_RIGHT_LABELS:
        return _extract_value_on_label_row(
            expected_file,
            evidence,
            _LAYOUT_ROW_RIGHT_LABELS[field_name],
            value_type,
            nearest=field_name in {"period_start", "period_end"},
        )
    return None


def _extract_nth_value_on_label_row(
    expected_file: str,
    evidence: list[_OcrEvidence],
    labels: tuple[str, ...],
    value_type: str,
    position: int,
) -> RecognizedField | None:
    for header in _matching_label_items(evidence, labels):
        candidates: list[tuple[float, int, _OcrEvidence, str | int | float]] = []
        header_center_x, header_center_y = _bbox_center(header.block.bbox)
        for item in evidence:
            if item.page != header.page or item.index <= header.index or not _has_bbox(item.block.bbox):
                continue
            value = _try_coerce_ocr_value(item.block.text, value_type)
            if value is None:
                continue
            center_x, center_y = _bbox_center(item.block.bbox)
            if abs(center_y - header_center_y) <= 35.0 and center_x > header_center_x:
                candidates.append((center_x, item.index, item, value))
        ordered = sorted(candidates, key=lambda entry: (entry[0], entry[1]))
        if len(ordered) >= position:
            _, _, item, value = ordered[position - 1]
            return _layout_recognized_field(expected_file, header, item, value)
    return None


def _extract_meter_rows(
    site: SiteConfig,
    expected_file: str,
    evidence: list[_OcrEvidence],
    grid_export_field: RecognizedField,
) -> list[dict[str, RecognizedField]]:
    rows: list[dict[str, RecognizedField]] = []
    grid_export_recipient_count = sum(
        1 for meter in site.meters if meter.grid_export_allocation in GRID_EXPORT_SITE_TOTAL_ALLOCATIONS
    )
    if grid_export_recipient_count != 1:
        raise FieldMappingError(
            "Site-level grid_export_kwh must be assigned to exactly one configured meter; "
            f"found {grid_export_recipient_count} recipients for site_key={site.site_key}."
        )
    for meter in site.meters:
        meter_item = _find_meter_item(evidence, meter.meter_no)
        if meter_item is None:
            raise FieldMappingError(f"OCR meter row '{meter.meter_no}' was not found in {expected_file}.")
        previous, current, multiplier, generation = _extract_meter_row_numbers(expected_file, meter_item, evidence)
        grid_export = _allocated_grid_export_field(meter, grid_export_field, expected_file)
        generation_value = _field_decimal(generation)
        grid_export_value = _field_decimal(grid_export)
        school_consumption = generation_value - grid_export_value
        rows.append(
            {
                "meter_no": _layout_recognized_field(expected_file, meter_item, meter_item, meter.meter_no),
                "previous_reading": previous,
                "current_reading": current,
                "multiplier": multiplier,
                "generation_kwh": generation,
                "grid_export_kwh": grid_export,
                "school_consumption_kwh": _derived_field(
                    _decimal_value(school_consumption),
                    expected_file,
                    f"meter {meter.meter_no}: generation_kwh - grid_export_kwh.",
                ),
                "detail_enabled": _site_config_field(
                    bool(meter.detail_enabled),
                    "config/sites.json",
                    f"site config meter {meter.meter_no} detail_enabled={meter.detail_enabled}.",
                ),
            }
        )
    return rows


def _fallback_single_meter_fields(
    generation_file: str,
    generation_evidence: list[_OcrEvidence],
) -> dict[str, RecognizedField]:
    fields: dict[str, RecognizedField] = {}
    missing: list[str] = []
    for field_name, (patterns, value_type) in FALLBACK_SINGLE_METER_SPECS.items():
        try:
            fields[field_name] = _extract_ocr_field(field_name, generation_file, generation_evidence, patterns, value_type)
        except FieldMappingError:
            missing.append(field_name)
    if missing:
        raise FieldMappingError(f"OCR mapping is missing fields: {', '.join(missing)}.")
    return fields


def _find_meter_item(evidence: list[_OcrEvidence], meter_no: str) -> _OcrEvidence | None:
    expected = _compact_text(meter_no)
    for item in evidence:
        if _compact_text(item.block.text) == expected:
            return item
    # Paddle may split a long meter number across two visual lines. Anchor the
    # row on the first fragment, whose y position matches the numeric cells,
    # after verifying that the nearby continuation completes the configured
    # meter number.
    for item in evidence:
        prefix = _compact_text(item.block.text)
        if len(prefix) < 8 or not expected.startswith(prefix) or prefix == expected:
            continue
        remaining = expected[len(prefix) :]
        item_x, item_y = _bbox_center(item.block.bbox)
        for continuation in evidence:
            if continuation.page != item.page or continuation.index == item.index:
                continue
            if _compact_text(continuation.block.text) != remaining:
                continue
            continuation_x, continuation_y = _bbox_center(continuation.block.bbox)
            if abs(continuation_x - item_x) <= 60.0 and 0.0 < continuation_y - item_y <= 45.0:
                left = min(item.block.bbox[0], continuation.block.bbox[0])
                right = max(item.block.bbox[2], continuation.block.bbox[2])
                return _OcrEvidence(
                    source_file=item.source_file,
                    page=item.page,
                    index=item.index,
                    block=OcrBlock(
                        text=meter_no,
                        # The first fragment is aligned with the numeric row;
                        # the continuation may sit below it in the asset cell.
                        bbox=(left, item.block.bbox[1], right, item.block.bbox[3]),
                        confidence=min(item.block.confidence, continuation.block.confidence),
                    ),
                )
    suffix = expected[-6:]
    for item in evidence:
        text = _compact_text(item.block.text)
        if text.endswith(suffix) and len(text) >= len(suffix):
            return item
    return None


def _allocated_grid_export_field(meter: Any, grid_export_field: RecognizedField, expected_file: str) -> RecognizedField:
    if meter.grid_export_allocation in GRID_EXPORT_SITE_TOTAL_ALLOCATIONS:
        return grid_export_field
    if meter.grid_export_allocation == "none":
        return _derived_field(
            0,
            expected_file,
            f"site config meter {meter.meter_no} grid_export_allocation=none; no grid export is allocated to this row.",
        )
    raise FieldMappingError(
        f"Unsupported grid_export_allocation={meter.grid_export_allocation!r} for meter {meter.meter_no}."
    )


def _extract_meter_row_numbers(
    expected_file: str,
    meter_item: _OcrEvidence,
    evidence: list[_OcrEvidence],
) -> tuple[RecognizedField, RecognizedField, RecognizedField, RecognizedField]:
    header_values = _extract_meter_row_numbers_by_headers(expected_file, meter_item, evidence)
    if header_values is not None:
        return header_values

    meter_center_x, meter_center_y = _bbox_center(meter_item.block.bbox)
    candidates: list[tuple[float, int, _OcrEvidence, str | int | float]] = []
    for item in evidence:
        if item.page != meter_item.page or item.index == meter_item.index or not _has_bbox(item.block.bbox):
            continue
        center_x, center_y = _bbox_center(item.block.bbox)
        if center_x <= meter_center_x or abs(center_y - meter_center_y) > 28.0:
            continue
        value = _try_coerce_ocr_value(item.block.text, "number")
        if value is None:
            continue
        candidates.append((center_x, item.index, item, value))
    ordered = sorted(candidates, key=lambda entry: (entry[0], entry[1]))
    if len(ordered) < 4:
        raise FieldMappingError(f"OCR meter row '{meter_item.block.text}' does not contain four numeric cells.")
    return tuple(
        _layout_recognized_field(expected_file, meter_item, item, value)
        for _, _, item, value in ordered[:4]
    )  # type: ignore[return-value]


def _extract_meter_row_numbers_by_headers(
    expected_file: str,
    meter_item: _OcrEvidence,
    evidence: list[_OcrEvidence],
) -> tuple[RecognizedField, RecognizedField, RecognizedField, RecognizedField] | None:
    column_aliases = (
        ("\u4e0a\u6708\u884c\u7801", "\u4e0a\u671f\u793a\u6570", "\u4e0a\u6708\u8bfb\u6570"),
        ("\u672c\u6708\u884c\u7801", "\u672c\u671f\u793a\u6570", "\u672c\u6708\u8bfb\u6570"),
        ("\u500d\u7387",),
        ("\u5408\u8ba1", "\u7535\u8868\u7535\u91cf"),
    )
    fields: list[RecognizedField] = []
    for aliases in column_aliases:
        field = _extract_meter_row_value_by_header(expected_file, meter_item, evidence, aliases)
        if field is None:
            return None
        fields.append(field)
    return tuple(fields)  # type: ignore[return-value]


def _extract_meter_row_value_by_header(
    expected_file: str,
    meter_item: _OcrEvidence,
    evidence: list[_OcrEvidence],
    aliases: tuple[str, ...],
) -> RecognizedField | None:
    _, meter_center_y = _bbox_center(meter_item.block.bbox)
    for alias in aliases:
        headers: list[tuple[float, int, _OcrEvidence]] = []
        for header in _matching_label_items(evidence, (alias,)):
            if header.page != meter_item.page:
                continue
            _, header_center_y = _bbox_center(header.block.bbox)
            y_gap = meter_center_y - header_center_y
            if 0.0 < y_gap <= 320.0:
                headers.append((y_gap, header.index, header))
        for _, _, header in sorted(headers, key=lambda entry: (entry[0], entry[1])):
            header_center_x, _ = _bbox_center(header.block.bbox)
            candidates: list[tuple[float, float, int, _OcrEvidence, str | int | float]] = []
            for item in evidence:
                if item.page != meter_item.page or item.index == meter_item.index or not _has_bbox(item.block.bbox):
                    continue
                value = _try_coerce_ocr_value(item.block.text, "number")
                if value is None:
                    continue
                center_x, center_y = _bbox_center(item.block.bbox)
                x_gap = abs(center_x - header_center_x)
                y_gap = abs(center_y - meter_center_y)
                if x_gap <= 180.0 and y_gap <= 40.0:
                    candidates.append((x_gap, y_gap, item.index, item, value))
            if candidates:
                # Keep the closest visual row first, allowing small OCR bbox
                # jitter between cells, then select the value aligned with the
                # requested column header.
                closest_y_gap = min(entry[1] for entry in candidates)
                row_candidates = [entry for entry in candidates if entry[1] <= closest_y_gap + 8.0]
                _, _, _, item, value = min(row_candidates, key=lambda entry: (entry[0], entry[1], entry[2]))
                return _layout_recognized_field(expected_file, header, item, value)
    return None


def _legacy_fields_from_meter_rows(rows: list[dict[str, RecognizedField]]) -> dict[str, RecognizedField]:
    detail_rows = [row for row in rows if bool(row["detail_enabled"].value)]
    row = detail_rows[0] if detail_rows else rows[0]
    return {
        "meter_no": row["meter_no"],
        "previous_reading": row["previous_reading"],
        "current_reading": row["current_reading"],
        "multiplier": row["multiplier"],
    }


def _legacy_meter_row(fields: dict[str, RecognizedField]) -> dict[str, RecognizedField]:
    generation = fields["total_generation_kwh"]
    grid_export = fields["grid_export_kwh"]
    return {
        "meter_no": fields["meter_no"],
        "previous_reading": fields["previous_reading"],
        "current_reading": fields["current_reading"],
        "multiplier": fields["multiplier"],
        "generation_kwh": generation,
        "grid_export_kwh": grid_export,
        "school_consumption_kwh": _derived_field(
            _decimal_value(_field_decimal(generation) - _field_decimal(grid_export)),
            generation.source_file,
            "legacy single meter: total_generation_kwh - grid_export_kwh",
        ),
        "detail_enabled": _site_config_field(True, fields["meter_no"].source_file, "legacy single meter detail_enabled=True"),
    }


def _total_generation_field(expected_file: str, rows: list[dict[str, RecognizedField]]) -> RecognizedField:
    total = sum((_field_decimal(row["generation_kwh"]) for row in rows), Decimal("0"))
    confidence = min(float(row["generation_kwh"].confidence) for row in rows)
    raw_text = " + ".join(str(row["generation_kwh"].value) for row in rows)
    return RecognizedField(
        value=_decimal_value(total),
        source_file=expected_file,
        page=None,
        confidence=confidence,
        raw_text=f"meter_rows generation_kwh sum: {raw_text}",
        source_type="derived",
    )


def _extract_value_below_header(
    expected_file: str,
    evidence: list[_OcrEvidence],
    labels: tuple[str, ...],
    value_type: str,
    trusted_header_aliases: tuple[str, ...] = (),
) -> RecognizedField | None:
    for header in _matching_label_items(evidence, labels):
        candidates: list[tuple[float, float, int, _OcrEvidence, str | int | float]] = []
        header_center_x, header_center_y = _bbox_center(header.block.bbox)
        header_width = max(1.0, header.block.bbox[2] - header.block.bbox[0])
        max_x_gap = max(160.0, header_width * 1.5)
        for item in evidence:
            if item.page != header.page or item.index <= header.index or not _has_bbox(item.block.bbox):
                continue
            value = _try_coerce_ocr_value(item.block.text, value_type)
            if value is None:
                continue
            center_x, center_y = _bbox_center(item.block.bbox)
            y_gap = center_y - header_center_y
            x_gap = abs(center_x - header_center_x)
            if 0.0 < y_gap <= 140.0 and x_gap <= max_x_gap:
                candidates.append((y_gap, x_gap, item.index, item, value))
        if candidates:
            _, _, _, item, value = sorted(candidates, key=lambda entry: (entry[1], entry[0], entry[2]))[0]
            field = _layout_recognized_field(expected_file, header, item, value)
            if _text_matches_label(header.block.text, trusted_header_aliases):
                return RecognizedField(
                    value=field.value,
                    source_file=field.source_file,
                    page=field.page,
                    confidence=item.block.confidence,
                    raw_text=field.raw_text,
                    source_type=field.source_type,
                )
            return field
    return None


def _extract_value_on_label_row(
    expected_file: str,
    evidence: list[_OcrEvidence],
    labels: tuple[str, ...],
    value_type: str,
    *,
    nearest: bool = False,
) -> RecognizedField | None:
    for label in labels:
        for header in _matching_label_items(evidence, (label,)):
            candidates: list[tuple[float, float, int, _OcrEvidence, str | int | float]] = []
            header_center_x, header_center_y = _bbox_center(header.block.bbox)
            for item in evidence:
                if item.page != header.page or item.index <= header.index or not _has_bbox(item.block.bbox):
                    continue
                value = _try_coerce_ocr_value(item.block.text, value_type)
                if value is None:
                    continue
                center_x, center_y = _bbox_center(item.block.bbox)
                y_gap = abs(center_y - header_center_y)
                x_gap = center_x - header_center_x
                if y_gap <= 35.0 and x_gap > 0.0:
                    horizontal_rank = x_gap if nearest else -center_x
                    candidates.append((y_gap, horizontal_rank, item.index, item, value))
            if candidates:
                sort_key = (
                    (lambda entry: (entry[1], entry[0], entry[2]))
                    if nearest
                    else (lambda entry: (entry[0], entry[1], entry[2]))
                )
                _, _, _, item, value = min(candidates, key=sort_key)
                return _layout_recognized_field(expected_file, header, item, value)
    return None


def _matching_label_items(evidence: list[_OcrEvidence], labels: tuple[str, ...]) -> list[_OcrEvidence]:
    return [
        item
        for item in evidence
        if _has_bbox(item.block.bbox) and _text_matches_label(item.block.text, labels)
    ]


def _text_matches_label(text: str, labels: tuple[str, ...]) -> bool:
    compact_text = _compact_text(text)
    for label in labels:
        compact_label = _compact_text(label)
        if compact_text == compact_label:
            return True
        if compact_text.startswith(f"{compact_label}("):
            return True
        if len(compact_label) >= 4 and compact_label in compact_text:
            return True
    return False


def _layout_recognized_field(
    expected_file: str,
    header: _OcrEvidence,
    item: _OcrEvidence,
    value: str | int | float,
) -> RecognizedField:
    return RecognizedField(
        value=value,
        source_file=expected_file,
        page=item.page,
        confidence=min(header.block.confidence, item.block.confidence),
        raw_text=f"{header.block.text} -> {item.block.text}",
        source_type="ocr_raw",
    )


def _try_coerce_ocr_value(text: str, value_type: str) -> str | int | float | None:
    value_text = _extract_value_text(text, value_type)
    if value_text is None:
        return None
    try:
        return _coerce_ocr_value(value_text, value_type)
    except Exception:
        return None


def _extract_value_text(text: str, value_type: str) -> str | None:
    pattern = VALUE_TEXT_PATTERNS.get(value_type)
    if pattern is None:
        raise FieldMappingError(f"Unsupported OCR value_type: {value_type}")
    source = text.replace(",", "") if value_type == "number" else text
    match = re.search(pattern, source)
    return match.group(0) if match else None


def _bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _has_bbox(bbox: tuple[float, float, float, float]) -> bool:
    return any(value != 0.0 for value in bbox)


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _coerce_ocr_value(value: str, value_type: str) -> str | int | float:
    text = value.strip()
    if value_type == "date":
        return _normalize_date(text)
    number = Decimal(text.replace(",", ""))
    return _decimal_value(number)


def _normalize_date(value: str) -> str:
    text = value.strip().replace("年", "-").replace("月", "-").replace("日", "")
    text = text.replace("/", "-").replace(".", "-")
    parts = [part for part in text.split("-") if part]
    if len(parts) != 3:
        raise FieldMappingError(f"Could not normalize OCR date value: {value}")
    return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _manual_baseline_fields(
    site: SiteConfig,
    scan_result: ScanResult,
    config_path: Path,
) -> dict[str, RecognizedField]:
    generation_file = scan_result.files["generation_statement"].name
    fee_file = scan_result.files["fee_statement"].name
    energy_file = scan_result.files["energy_statement"].name
    config_source = _display_config_path(config_path)

    return {
        "site_key": _site_config_field(site.site_key, config_source, "site config field site_key"),
        "site_name": _site_config_field(site.site_name, config_source, "site config field site_name"),
        "project_name": _site_config_field(site.project_name, config_source, "site config field project_name"),
        "month": RecognizedField(
            value=scan_result.month,
            source_file=", ".join(path.name for path in scan_result.files.values()),
            page=None,
            confidence=1.0,
            raw_text="Derived from source PDF filenames.",
            source_type="derived",
        ),
        "meter_no": _site_config_field(
            site.meter_no,
            config_source,
            "site config fallback field meter_no; sample baseline matches generation-statement template",
        ),
        "period_start": _manual_field("2026-05-01", generation_file, "manual sample baseline period_start 2026-05-01"),
        "period_end": _manual_field("2026-06-01", generation_file, "manual sample baseline period_end 2026-06-01"),
        "previous_reading": _manual_field(816.82, generation_file, "manual sample baseline previous_reading 816.82"),
        "current_reading": _manual_field(1147.27, generation_file, "manual sample baseline current_reading 1147.27"),
        "multiplier": _site_config_field(
            _decimal_value(site.multiplier),
            config_source,
            "site config fallback field multiplier; sample baseline matches generation-statement template",
        ),
        "total_generation_kwh": _manual_field(49568, generation_file, "manual sample baseline total_generation_kwh 49568 kWh"),
        "grid_export_kwh": _manual_field(1680, energy_file, "manual sample baseline grid_export_kwh 1680 kWh"),
        "grid_price": _manual_field(0.242434, fee_file, "manual sample baseline grid_price 0.242434"),
        "grid_fee": _manual_field(407.29, fee_file, "manual sample baseline grid_fee 407.29"),
        "school_base_price": _site_config_field(_decimal_value(site.school_base_price), config_source, "site config field school_base_price"),
        "school_discount_rate": _site_config_field(_decimal_value(site.school_discount_rate), config_source, "site config field school_discount_rate"),
        "company_name": _site_config_field(site.company_name, config_source, "site config field company_name"),
        "statement_date": _manual_field("2026-06-21", generation_file, "manual sample baseline statement_date 2026-06-21"),
    }


def _manual_field(value: str | int | float, source_file: str, raw_text: str) -> RecognizedField:
    return RecognizedField(
        value=value,
        source_file=source_file,
        page=1,
        confidence=1.0,
        raw_text=raw_text,
        source_type="manual_baseline",
    )


def _derived_field(value: str | int | float | bool, source_file: str, raw_text: str) -> RecognizedField:
    return RecognizedField(
        value=value,
        source_file=source_file,
        page=None,
        confidence=1.0,
        raw_text=raw_text,
        source_type="derived",
    )


def _site_config_field(value: str | int | float | bool, source_file: str, raw_text: str) -> RecognizedField:
    return RecognizedField(
        value=value,
        source_file=source_file,
        page=None,
        confidence=1.0,
        raw_text=raw_text,
        source_type="site_config",
    )


def _system_runtime_field(statement_date: str) -> RecognizedField:
    return RecognizedField(
        value=statement_date,
        source_file="system_runtime",
        page=None,
        confidence=1.0,
        raw_text=f"Asia/Shanghai runtime date: {statement_date}",
        source_type="system_runtime",
    )


def _decimal_value(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _field_decimal(field: RecognizedField) -> Decimal:
    return Decimal(str(field.value))


def _display_config_path(config_path: Path) -> str:
    text = str(config_path)
    return text.replace("/", "\\")

