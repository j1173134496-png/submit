"""Data normalization from recognized_data.json to monthly business data."""

from __future__ import annotations

import json
from datetime import date
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


REQUIRED_NORMALIZED_FIELDS = (
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
    "company_name",
    "statement_date",
)

NUMERIC_FIELDS = (
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
)

METER_NUMERIC_FIELDS = (
    "previous_reading",
    "current_reading",
    "multiplier",
    "generation_kwh",
    "grid_export_kwh",
    "school_consumption_kwh",
)


class NormalizationError(RuntimeError):
    """Raised when recognized_data.json cannot be normalized."""


FORMULA_GENERATION_START_MONTH = "2026-08"


def uses_formula_generation(month: str) -> bool:
    """Use meter-difference generation only from August 2026 onward."""
    try:
        year, month_number = (int(part) for part in str(month).split("-", 1))
        date(year, month_number, 1)
    except (TypeError, ValueError):
        raise NormalizationError(f"Month must be YYYY-MM, got {month!r}.")
    return f"{year:04d}-{month_number:02d}" >= FORMULA_GENERATION_START_MONTH


@dataclass(frozen=True)
class MeterRowData:
    meter_no: str
    previous_reading: Decimal
    current_reading: Decimal
    multiplier: Decimal
    generation_kwh: Decimal
    reported_generation_kwh: Decimal
    grid_export_kwh: Decimal
    school_consumption_kwh: Decimal
    detail_enabled: bool
    calculated_generation_kwh: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "meter_no": self.meter_no,
            "previous_reading": _json_number(self.previous_reading),
            "current_reading": _json_number(self.current_reading),
            "multiplier": _json_number(self.multiplier),
            "generation_kwh": _json_number(self.generation_kwh),
            "reported_generation_kwh": _json_number(self.reported_generation_kwh),
            "grid_export_kwh": _json_number(self.grid_export_kwh),
            "school_consumption_kwh": _json_number(self.school_consumption_kwh),
            "detail_enabled": self.detail_enabled,
            "calculated_generation_kwh": _json_number(self.calculated_generation_kwh),
        }


@dataclass(frozen=True)
class MonthlyData:
    site_key: str
    site_name: str
    project_name: str
    month: str
    mapping_mode: str
    meter_no: str
    period_start: str
    period_end: str
    previous_reading: Decimal
    current_reading: Decimal
    multiplier: Decimal
    total_generation_kwh: Decimal
    grid_export_kwh: Decimal
    grid_price: Decimal | None
    grid_fee: Decimal | None
    school_base_price: Decimal
    school_discount_rate: Decimal
    effective_unit_price: Decimal
    company_name: str
    statement_date: str
    meter_rows: tuple[MeterRowData, ...]
    school_discount_price: Decimal
    school_consumption_kwh: Decimal
    calculated_generation_kwh: Decimal
    calculated_grid_fee: Decimal | None
    calculated_school_fee: Decimal
    calculated_total_fee: Decimal
    validation_not_applicable_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "site_key": self.site_key,
            "site_name": self.site_name,
            "project_name": self.project_name,
            "month": self.month,
            "mapping_mode": self.mapping_mode,
            "meter_no": self.meter_no,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "previous_reading": _json_number(self.previous_reading),
            "current_reading": _json_number(self.current_reading),
            "multiplier": _json_number(self.multiplier),
            "total_generation_kwh": _json_number(self.total_generation_kwh),
            "grid_export_kwh": _json_number(self.grid_export_kwh),
            "school_base_price": _json_number(self.school_base_price),
            "school_discount_rate": _json_number(self.school_discount_rate),
            "effective_unit_price": _json_number(self.effective_unit_price),
            "company_name": self.company_name,
            "statement_date": self.statement_date,
            "meter_rows": [row.to_dict() for row in self.meter_rows],
            "school_discount_price": _json_number(self.school_discount_price),
            "school_consumption_kwh": _json_number(self.school_consumption_kwh),
            "calculated_generation_kwh": _json_number(self.calculated_generation_kwh),
            "calculated_school_fee": _json_number(self.calculated_school_fee),
            "calculated_total_fee": _json_number(self.calculated_total_fee),
            "validation_not_applicable_fields": list(self.validation_not_applicable_fields),
        }
        if self.grid_price is not None:
            payload["grid_price"] = _json_number(self.grid_price)
        if self.grid_fee is not None:
            payload["grid_fee"] = _json_number(self.grid_fee)
        if self.calculated_grid_fee is not None:
            payload["calculated_grid_fee"] = _json_number(self.calculated_grid_fee)
        return payload


def load_recognized_data(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        raise NormalizationError(f"recognized_data.json does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise NormalizationError(f"recognized_data.json is not valid JSON: {source}") from exc
    if not isinstance(payload, dict):
        raise NormalizationError("recognized_data.json must contain a JSON object.")
    return payload


def normalize_recognized_data(payload: dict[str, Any]) -> MonthlyData:
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        raise NormalizationError("recognized_data.json must contain a fields object.")

    not_applicable = _validation_not_applicable_fields(payload)
    missing = [field for field in REQUIRED_NORMALIZED_FIELDS if field not in fields and field not in not_applicable]
    if missing:
        raise NormalizationError(f"Missing required fields: {', '.join(missing)}.")

    values = {
        field: _field_value(fields, field)
        for field in REQUIRED_NORMALIZED_FIELDS
        if field in fields and field not in not_applicable
    }
    if "effective_unit_price" in fields:
        values["effective_unit_price"] = _field_value(fields, "effective_unit_price")
    numbers = {field: _decimal_field(values, field) for field in NUMERIC_FIELDS if field in values}

    site_key = _required_top_level_string(payload, "site_key")
    site_name = _required_top_level_string(payload, "site_name")
    project_name = _required_top_level_string(payload, "project_name")
    month = _required_top_level_string(payload, "month")
    mapping_mode = str(payload.get("mapping_mode") or "unknown")

    meter_rows = _normalize_meter_rows(payload, values, numbers, month=month)
    detail_row = next((row for row in meter_rows if row.detail_enabled), meter_rows[0])
    school_discount_price = numbers["effective_unit_price"] if "effective_unit_price" in numbers else numbers["school_base_price"]
    total_generation_kwh = sum((row.generation_kwh for row in meter_rows), Decimal("0"))
    total_grid_export_kwh = numbers["grid_export_kwh"]
    school_consumption_kwh = sum((row.school_consumption_kwh for row in meter_rows), Decimal("0"))
    grid_price = numbers.get("grid_price")
    grid_fee = numbers.get("grid_fee")
    calculated_grid_fee = total_grid_export_kwh * grid_price if grid_price is not None else None
    calculated_school_fee = school_consumption_kwh * school_discount_price
    if len(meter_rows) > 1:
        calculated_school_fee = calculated_school_fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    calculated_total_fee = calculated_school_fee + (calculated_grid_fee or Decimal("0"))

    return MonthlyData(
        site_key=site_key,
        site_name=site_name,
        project_name=project_name,
        month=month,
        mapping_mode=mapping_mode,
        meter_no=detail_row.meter_no,
        period_start=str(values["period_start"]),
        period_end=str(values["period_end"]),
        previous_reading=detail_row.previous_reading,
        current_reading=detail_row.current_reading,
        multiplier=detail_row.multiplier,
        total_generation_kwh=total_generation_kwh,
        grid_export_kwh=total_grid_export_kwh,
        grid_price=grid_price,
        grid_fee=grid_fee,
        school_base_price=numbers["school_base_price"],
        school_discount_rate=numbers["school_discount_rate"],
        effective_unit_price=school_discount_price,
        company_name=str(values["company_name"]),
        statement_date=str(values["statement_date"]),
        meter_rows=meter_rows,
        school_discount_price=school_discount_price,
        school_consumption_kwh=school_consumption_kwh,
        calculated_generation_kwh=detail_row.calculated_generation_kwh,
        calculated_grid_fee=calculated_grid_fee,
        calculated_school_fee=calculated_school_fee,
        calculated_total_fee=calculated_total_fee,
        validation_not_applicable_fields=not_applicable,
    )


def normalize_recognized_data_file(path: Path | str) -> MonthlyData:
    return normalize_recognized_data(load_recognized_data(path))


def _normalize_meter_rows(
    payload: dict[str, Any],
    legacy_values: dict[str, Any],
    legacy_numbers: dict[str, Decimal],
    *,
    month: str,
) -> tuple[MeterRowData, ...]:
    rows_payload = payload.get("meter_rows")
    if rows_payload is None:
        rows_payload = [_legacy_meter_row_payload(legacy_values)]
    if not isinstance(rows_payload, list) or not rows_payload:
        raise NormalizationError("recognized_data.json meter_rows must be a non-empty array.")

    rows = []
    for index, row_payload in enumerate(rows_payload, start=1):
        if not isinstance(row_payload, dict):
            raise NormalizationError(f"meter_rows[{index}] must be an object.")
        row_values = {field: _field_value(row_payload, field) for field in ("meter_no", *METER_NUMERIC_FIELDS, "detail_enabled")}
        row_numbers = {field: _decimal_field(row_values, field) for field in METER_NUMERIC_FIELDS}
        calculated_generation = (
            row_numbers["current_reading"] - row_numbers["previous_reading"]
        ) * row_numbers["multiplier"]
        rounded_generation = calculated_generation.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        generation = rounded_generation if uses_formula_generation(month) else row_numbers["generation_kwh"]
        school_consumption = generation - row_numbers["grid_export_kwh"]
        rows.append(
            MeterRowData(
                meter_no=str(row_values["meter_no"]),
                previous_reading=row_numbers["previous_reading"],
                current_reading=row_numbers["current_reading"],
                multiplier=row_numbers["multiplier"],
                generation_kwh=generation,
                reported_generation_kwh=row_numbers["generation_kwh"],
                grid_export_kwh=row_numbers["grid_export_kwh"],
                school_consumption_kwh=school_consumption,
                detail_enabled=_bool_value(row_values["detail_enabled"]),
                calculated_generation_kwh=calculated_generation,
            )
        )
    return tuple(rows)


def _legacy_meter_row_payload(values: dict[str, Any]) -> dict[str, Any]:
    generation = values["total_generation_kwh"]
    grid_export = values["grid_export_kwh"]
    try:
        school_consumption = _decimal_field(values, "total_generation_kwh") - _decimal_field(values, "grid_export_kwh")
    except NormalizationError:
        school_consumption = Decimal("0")
    return {
        "meter_no": {"value": values["meter_no"]},
        "previous_reading": {"value": values["previous_reading"]},
        "current_reading": {"value": values["current_reading"]},
        "multiplier": {"value": values["multiplier"]},
        "generation_kwh": {"value": generation},
        "grid_export_kwh": {"value": grid_export},
        "school_consumption_kwh": {"value": _json_number(school_consumption)},
        "detail_enabled": {"value": True},
    }


def _field_value(fields: dict[str, Any], field_name: str) -> Any:
    field = fields.get(field_name)
    if not isinstance(field, dict) or "value" not in field:
        raise NormalizationError(f"Field '{field_name}' must contain a value.")
    return field["value"]


def _validation_not_applicable_fields(payload: dict[str, Any]) -> tuple[str, ...]:
    value = payload.get("validation_not_applicable_fields")
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise NormalizationError("validation_not_applicable_fields must be a list of strings.")
    return tuple(dict.fromkeys(item.strip() for item in value))


def _decimal_field(values: dict[str, Any], field_name: str) -> Decimal:
    value = values[field_name]
    if value is None or value == "":
        raise NormalizationError(f"Field '{field_name}' cannot be blank.")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise NormalizationError(f"Field '{field_name}' must be numeric, got {value!r}.") from exc


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
    raise NormalizationError(f"detail_enabled must be boolean, got {value!r}.")


def _required_top_level_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        raise NormalizationError(f"recognized_data.json is missing top-level '{key}'.")
    return str(value)


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)

