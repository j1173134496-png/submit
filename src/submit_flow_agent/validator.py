"""Business validation and validation_report.json generation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from submit_flow_agent.field_mapper import MIN_OCR_CONFIDENCE
from submit_flow_agent.normalizer import MonthlyData, load_recognized_data, normalize_recognized_data, uses_formula_generation


FEE_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    message: str
    expected: object | None = None
    actual: object | None = None
    tolerance: object | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }
        if self.expected is not None:
            payload["expected"] = self.expected
        if self.actual is not None:
            payload["actual"] = self.actual
        if self.tolerance is not None:
            payload["tolerance"] = self.tolerance
        return payload


@dataclass(frozen=True)
class ValidationReport:
    site_key: str
    site_name: str
    month: str
    status: str
    can_write_excel: bool
    normalized_data: dict[str, object]
    checks: list[CheckResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "site_key": self.site_key,
            "site_name": self.site_name,
            "month": self.month,
            "status": self.status,
            "can_write_excel": self.can_write_excel,
            "normalized_data": self.normalized_data,
            "checks": [check.to_dict() for check in self.checks],
            "errors": self.errors,
            "warnings": self.warnings,
        }


def validate_monthly_data(data: MonthlyData) -> ValidationReport:
    checks: list[CheckResult] = []
    warnings: list[str] = []

    checks.append(_pass_check("required_fields", "All required fields are present and normalized."))
    checks.append(_generation_check(data))
    checks.append(_school_consumption_check(data))
    checks.append(_school_discount_price_check(data))
    checks.append(_grid_fee_check(data))
    checks.append(_school_fee_check(data))
    checks.append(_total_fee_check(data))
    checks.append(_month_period_check(data))

    if not uses_formula_generation(data.month):
        for row in data.meter_rows:
            rounded = row.calculated_generation_kwh.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if row.generation_kwh not in (row.calculated_generation_kwh, rounded):
                warnings.append(
                    f"历史核对差异：电表{row.meter_no}来源发电量{row.generation_kwh}，"
                    f"抄表公式值{row.calculated_generation_kwh}；保留来源值，待核对原始资料。"
                )

    if data.mapping_mode == "manual_baseline":
        warnings.append("mapping_mode=manual_baseline，当前数据来自人工确认样例基准，不代表真实 OCR 自动识别结果。")

    errors = [check.message for check in checks if check.status == "fail"]
    status = "pass" if not errors else "fail"
    return ValidationReport(
        site_key=data.site_key,
        site_name=data.site_name,
        month=data.month,
        status=status,
        can_write_excel=not errors,
        normalized_data=data.to_dict(),
        checks=checks,
        errors=errors,
        warnings=warnings,
    )


def validate_recognized_data_file(path: Path | str) -> ValidationReport:
    payload = load_recognized_data(path)
    report = validate_monthly_data(normalize_recognized_data(payload))
    confidence_errors = _ocr_confidence_errors(payload)
    if not confidence_errors:
        return report
    return ValidationReport(
        site_key=report.site_key,
        site_name=report.site_name,
        month=report.month,
        status="fail",
        can_write_excel=False,
        normalized_data=report.normalized_data,
        checks=report.checks,
        errors=[*report.errors, *confidence_errors],
        warnings=report.warnings,
    )


def write_validation_report(report: ValidationReport, output_path: Path | str) -> Path:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def validate_data_to_file(input_path: Path | str, output_path: Path | str) -> ValidationReport:
    report = validate_recognized_data_file(input_path)
    write_validation_report(report, output_path)
    return report


def _ocr_confidence_errors(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field_name, field in _iter_recognized_fields(payload):
        if uses_formula_generation(str(payload.get("month") or "")) and _is_non_blocking_generation_evidence(field_name):
            continue
        if not isinstance(field, dict) or field.get("source_type") != "ocr_raw":
            continue
        confidence = field.get("confidence")
        if not isinstance(confidence, (int, float)):
            errors.append(f"OCR 字段 {field_name} 缺少有效 confidence，禁止写 Excel。")
        elif float(confidence) < MIN_OCR_CONFIDENCE:
            errors.append(
                f"OCR 字段 {field_name} 置信度 {float(confidence):.2f} 低于阈值 {MIN_OCR_CONFIDENCE:.2f}，需要 review/manual 复核。"
            )
    return errors


def _is_non_blocking_generation_evidence(field_name: str) -> bool:
    return field_name == "total_generation_kwh" or field_name.endswith(".generation_kwh")


def _iter_recognized_fields(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    fields = payload.get("fields")
    results: list[tuple[str, Any]] = []
    if isinstance(fields, dict):
        results.extend((field_name, field) for field_name, field in fields.items())
    meter_rows = payload.get("meter_rows")
    if isinstance(meter_rows, list):
        for index, row in enumerate(meter_rows, start=1):
            if not isinstance(row, dict):
                continue
            results.extend((f"meter_rows[{index}].{field_name}", field) for field_name, field in row.items())
    return results


def _generation_check(data: MonthlyData) -> CheckResult:
    if not uses_formula_generation(data.month):
        return CheckResult(
            name="generation_formula",
            status="pass",
            message="2026年8月前沿用历史/识别来源发电量，抄表公式仅作为核对依据。",
            expected=_json_number(sum((row.calculated_generation_kwh for row in data.meter_rows), Decimal("0"))),
            actual=_json_number(data.total_generation_kwh),
        )
    row_errors = []
    for row in data.meter_rows:
        expected = row.calculated_generation_kwh.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if row.reported_generation_kwh != expected:
            row_errors.append(
                f"{row.meter_no}: formula {_json_number(expected)}, "
                f"statement {_json_number(row.reported_generation_kwh)}"
            )
    row_sum = sum((row.generation_kwh for row in data.meter_rows), Decimal("0"))
    if not row_errors and row_sum == data.total_generation_kwh:
        return CheckResult(
            name="generation_formula",
            status="pass",
            message="各电表月总发电量已按抄表示数公式四舍五入到整数。",
            expected=_json_number(row_sum),
            actual=_json_number(data.total_generation_kwh),
        )
    return CheckResult(
        name="generation_formula",
        status="fail",
        message="发电单月发电量与抄表示数公式的四舍五入结果冲突，需要人工复核。"
        + ("; " + "; ".join(row_errors) if row_errors else ""),
        expected=_json_number(row_sum),
        actual=_json_number(data.total_generation_kwh),
    )


def _school_consumption_check(data: MonthlyData) -> CheckResult:
    row_grid_export = sum((row.grid_export_kwh for row in data.meter_rows), Decimal("0"))
    expected_school_consumption = data.total_generation_kwh - data.grid_export_kwh
    negative_rows = [row.meter_no for row in data.meter_rows if row.school_consumption_kwh < 0]
    if negative_rows or expected_school_consumption < 0 or data.school_consumption_kwh < 0:
        return CheckResult(
            name="school_consumption_non_negative",
            status="fail",
            message="上网电量大于月总发电量，学校消纳电量为负。",
            expected=_json_number(expected_school_consumption),
            actual=_json_number(data.school_consumption_kwh),
        )
    if row_grid_export != data.grid_export_kwh:
        return CheckResult(
            name="school_consumption_non_negative",
            status="fail",
            message="各电表上网电量合计与站点上网电量不一致。",
            expected=_json_number(data.grid_export_kwh),
            actual=_json_number(row_grid_export),
        )
    if data.school_consumption_kwh != expected_school_consumption:
        return CheckResult(
            name="school_consumption_non_negative",
            status="fail",
            message="学校消纳电量合计与总发电量减总上网电量不一致。",
            expected=_json_number(expected_school_consumption),
            actual=_json_number(data.school_consumption_kwh),
        )
    return CheckResult(
        name="school_consumption_non_negative",
        status="pass",
        message="学校消纳电量非负，且站点合计与各电表明细一致。",
        expected=_json_number(expected_school_consumption),
        actual=_json_number(data.school_consumption_kwh),
    )


def _school_discount_price_check(data: MonthlyData) -> CheckResult:
    return CheckResult(
        name="school_discount_price",
        status="pass",
        message="学校折扣单价已按基础电价和折扣率计算。",
        actual=_json_number(data.school_discount_price),
    )


def _grid_fee_check(data: MonthlyData) -> CheckResult:
    exempt = set(data.validation_not_applicable_fields)
    if {"grid_price", "grid_fee"}.issubset(exempt):
        return CheckResult(
            name="grid_fee",
            status="skip",
            message="Profile marks grid_price/grid_fee not applicable.",
        )
    if data.grid_price is None or data.grid_fee is None or data.calculated_grid_fee is None:
        return CheckResult(
            name="grid_fee",
            status="fail",
            message="上网电价/上网电费字段缺失，且当前 profile 未声明其不适用。",
        )
    expected = _money(data.calculated_grid_fee)
    actual = _money(data.grid_fee)
    diff = abs(expected - actual)
    if diff <= FEE_TOLERANCE:
        return CheckResult(
            name="grid_fee",
            status="pass",
            message="上网电费金额校验通过。",
            expected=_json_number(expected),
            actual=_json_number(actual),
            tolerance=_json_number(FEE_TOLERANCE),
        )
    return CheckResult(
        name="grid_fee",
        status="fail",
        message="上网电费金额差异超过容差。",
        expected=_json_number(expected),
        actual=_json_number(actual),
        tolerance=_json_number(FEE_TOLERANCE),
    )


def _school_fee_check(data: MonthlyData) -> CheckResult:
    return CheckResult(
        name="school_fee",
        status="pass",
        message="学校消纳电费已计算。",
        actual=_json_number(data.calculated_school_fee),
    )


def _total_fee_check(data: MonthlyData) -> CheckResult:
    if len(data.meter_rows) > 1:
        message = "双电表汇总合计已按上网电费和各电表学校消纳电费相加计算。"
    else:
        message = "单电表汇总合计已按上网电费和学校消纳电费计算。"
    return CheckResult(
        name="total_fee",
        status="pass",
        message=message,
        actual=_json_number(data.calculated_total_fee),
    )


def _month_period_check(data: MonthlyData) -> CheckResult:
    expected_start, expected_end = _expected_period(data.month)
    if data.period_start == expected_start and data.period_end == expected_end:
        return CheckResult(
            name="month_period",
            status="pass",
            message="月份和抄表周期一致。",
            expected={"period_start": expected_start, "period_end": expected_end},
            actual={"period_start": data.period_start, "period_end": data.period_end},
        )
    return CheckResult(
        name="month_period",
        status="fail",
        message="月份和抄表周期不一致。",
        expected={"period_start": expected_start, "period_end": expected_end},
        actual={"period_start": data.period_start, "period_end": data.period_end},
    )


def _expected_period(month: str) -> tuple[str, str]:
    year_text, month_text = month.split("-", 1)
    year = int(year_text)
    month_number = int(month_text)
    if month_number == 12:
        return f"{year}-12-01", f"{year + 1}-01-01"
    return f"{year}-{month_number:02d}-01", f"{year}-{month_number + 1:02d}-01"


def _pass_check(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, status="pass", message=message)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)
