"""Review/manual report helpers for MVP-011."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from submit_flow_agent.field_mapper import MIN_OCR_CONFIDENCE, REQUIRED_RECOGNIZED_FIELDS
from submit_flow_agent.normalizer import load_recognized_data
from submit_flow_agent.review_contract import confirmable_fields_for_issue
from submit_flow_agent.validator import ValidationReport


class ReviewError(RuntimeError):
    """Raised when review/manual data cannot be processed."""


def write_review_report(report: dict[str, Any], output_path: Path | str) -> Path:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def build_review_report(
    *,
    month: str,
    recognized_data_path: Path | str,
    validation_report_path: Path | str,
    validation_report: ValidationReport | None = None,
    mapping_error: Exception | None = None,
) -> dict[str, Any]:
    recognized = _load_optional_json(recognized_data_path)
    issues: list[dict[str, Any]] = []

    if mapping_error is not None:
        issues.extend(_mapping_error_issues(mapping_error))
    if isinstance(recognized, dict):
        issues.extend(_missing_field_issues(recognized))
        issues.extend(_low_confidence_issues(recognized))
    if validation_report is not None:
        issues.extend(_validation_issues(validation_report))
    issues = _deduplicate_issues(issues)
    for issue in issues:
        issue["confirmable_fields"] = confirmable_fields_for_issue(issue.get("field"), issue.get("reason"))

    return {
        "task_status": "need_review",
        "month": month,
        "recognized_data": str(recognized_data_path),
        "validation_report": str(validation_report_path),
        "issue_count": len(issues),
        "issues": issues,
    }


def _deduplicate_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the most informative issue when layers report the same field and reason."""
    deduplicated: dict[tuple[object, object], dict[str, Any]] = {}
    for issue in issues:
        key = (issue.get("field"), issue.get("reason"))
        current = deduplicated.get(key)
        if current is None or _issue_detail_score(issue) > _issue_detail_score(current):
            deduplicated[key] = issue
    return list(deduplicated.values())


def _issue_detail_score(issue: dict[str, Any]) -> int:
    evidence_keys = (
        "source_file",
        "page",
        "raw_text",
        "current_value",
        "confidence",
        "expected",
        "actual",
        "tolerance",
    )
    return sum(issue.get(key) is not None for key in evidence_keys)


def apply_confirmed_data(
    recognized_data_path: Path | str,
    confirmed_data_path: Path | str,
    output_path: Path | str | None = None,
) -> Path:
    recognized_path = Path(recognized_data_path)
    target = Path(output_path) if output_path is not None else recognized_path
    payload = load_recognized_data(recognized_path)
    confirmed = _load_confirmed_data(confirmed_data_path)
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        raise ReviewError("recognized_data.json must contain fields object.")

    confirmed_fields = confirmed.get("fields")
    if not isinstance(confirmed_fields, dict) or not confirmed_fields:
        raise ReviewError("confirmed_data.json must contain non-empty fields object.")

    confirmed_by = str(confirmed.get("confirmed_by") or "unknown")
    confirmed_at = str(confirmed.get("confirmed_at") or "")
    source_file = Path(confirmed_data_path).name

    patched = deepcopy(payload)
    patched_fields = patched["fields"]
    for field_name, patch in confirmed_fields.items():
        if field_name not in REQUIRED_RECOGNIZED_FIELDS:
            raise ReviewError(f"confirmed_data.json contains unknown field: {field_name}")
        if not isinstance(patch, dict) or "value" not in patch:
            raise ReviewError(f"confirmed field '{field_name}' must contain value.")
        old_field = patched_fields.get(field_name)
        if not isinstance(old_field, dict):
            old_field = {}
        reason = str(patch.get("reason") or "人工确认。")
        patched_fields[field_name] = {
            **old_field,
            "value": patch["value"],
            "source_file": source_file,
            "page": None,
            "confidence": 1.0,
            "raw_text": _manual_raw_text(field_name, reason, confirmed_by, confirmed_at),
            "source_type": "manual_confirmed",
        }
        _patch_meter_rows_for_confirmed_field(patched, field_name, patched_fields[field_name])

    notes = patched.setdefault("mapping_notes", [])
    if isinstance(notes, list):
        notes.append(f"manual confirmed fields from {source_file}; confirmed_by={confirmed_by}; confirmed_at={confirmed_at}")
    patched["mapping_mode"] = "manual_confirmed"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(patched, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _load_confirmed_data(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        raise ReviewError(f"confirmed_data.json does not exist: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ReviewError(f"confirmed_data.json is not valid JSON: {source}") from exc
    if not isinstance(payload, dict):
        raise ReviewError("confirmed_data.json must contain a JSON object.")
    return payload


def _patch_meter_rows_for_confirmed_field(
    payload: dict[str, Any],
    field_name: str,
    patched_field: dict[str, Any],
) -> None:
    meter_rows = payload.get("meter_rows")
    if not isinstance(meter_rows, list) or not meter_rows:
        return
    target_row = next((row for row in meter_rows if isinstance(row, dict) and _field_value(row.get("detail_enabled")) is True), meter_rows[0])
    if not isinstance(target_row, dict):
        return
    row_field = {
        "meter_no": "meter_no",
        "previous_reading": "previous_reading",
        "current_reading": "current_reading",
        "multiplier": "multiplier",
        "total_generation_kwh": "generation_kwh",
        "grid_export_kwh": "grid_export_kwh",
    }.get(field_name)
    if row_field is None:
        return
    target_row[row_field] = deepcopy(patched_field)
    if row_field in {"generation_kwh", "grid_export_kwh"}:
        _refresh_row_school_consumption(target_row)


def _refresh_row_school_consumption(row: dict[str, Any]) -> None:
    generation = _field_value(row.get("generation_kwh"))
    grid_export = _field_value(row.get("grid_export_kwh"))
    try:
        value = float(generation) - float(grid_export)
    except (TypeError, ValueError):
        return
    row["school_consumption_kwh"] = {
        "value": int(value) if value.is_integer() else value,
        "source_file": "manual_confirmed",
        "page": None,
        "confidence": 1.0,
        "raw_text": "人工确认后重新计算 generation_kwh - grid_export_kwh。",
        "source_type": "manual_confirmed",
    }


def _field_value(field: Any) -> Any:
    if isinstance(field, dict):
        return field.get("value")
    return None


def _load_optional_json(path: Path | str) -> dict[str, Any] | None:
    source = Path(path)
    if not source.exists():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _mapping_error_issues(error: Exception) -> list[dict[str, Any]]:
    message = str(error)
    field = _field_from_error(message)
    return [
        {
            "field": field,
            "reason": "field_missing",
            "message": message,
            "source_file": None,
            "page": None,
            "raw_text": None,
            "current_value": None,
            "confidence": None,
        }
    ]


def _missing_field_issues(recognized: dict[str, Any]) -> list[dict[str, Any]]:
    fields = recognized.get("fields")
    if not isinstance(fields, dict):
        return []
    not_applicable = _validation_not_applicable_fields(recognized)
    issues = []
    for field_name in REQUIRED_RECOGNIZED_FIELDS:
        if field_name not in fields and field_name not in not_applicable:
            issues.append(
                {
                    "field": field_name,
                    "reason": "field_missing",
                    "message": f"recognized_data.json 缺少字段 {field_name}。",
                    "source_file": None,
                    "page": None,
                    "raw_text": None,
                    "current_value": None,
                    "confidence": None,
                }
            )
    return issues


def _validation_not_applicable_fields(recognized: dict[str, Any]) -> set[str]:
    value = recognized.get("validation_not_applicable_fields")
    if not isinstance(value, list):
        return set()
    return {item.strip() for item in value if isinstance(item, str) and item.strip()}


def _low_confidence_issues(recognized: dict[str, Any]) -> list[dict[str, Any]]:
    fields = recognized.get("fields")
    if not isinstance(fields, dict):
        return []
    issues = []
    for field_name, field in fields.items():
        if not isinstance(field, dict) or field.get("source_type") != "ocr_raw":
            continue
        confidence = field.get("confidence")
        if isinstance(confidence, (int, float)) and float(confidence) >= MIN_OCR_CONFIDENCE:
            continue
        issues.append(
            {
                "field": field_name,
                "reason": "low_confidence",
                "message": f"OCR 字段 {field_name} 置信度 {confidence} 低于阈值 {MIN_OCR_CONFIDENCE:.2f}，需要人工确认。",
                "source_file": field.get("source_file"),
                "page": field.get("page"),
                "raw_text": field.get("raw_text"),
                "current_value": field.get("value"),
                "confidence": confidence,
            }
        )
    return issues


def _validation_issues(report: ValidationReport) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for check in report.checks:
        if check.status != "fail":
            continue
        issues.append(
            {
                "field": check.name,
                "reason": "business_validation_failed",
                "message": check.message,
                "source_file": None,
                "page": None,
                "raw_text": None,
                "current_value": check.actual,
                "confidence": None,
                "expected": check.expected,
                "actual": check.actual,
                "tolerance": check.tolerance,
            }
        )
    for error in report.errors:
        if any(issue.get("message") == error for issue in issues):
            continue
        issues.append(
            {
                "field": _field_from_error(error),
                "reason": _reason_from_error(error),
                "message": error,
                "source_file": None,
                "page": None,
                "raw_text": None,
                "current_value": None,
                "confidence": None,
            }
        )
    return issues


def _field_from_error(message: str) -> str | None:
    quoted = re.search(r"'([a-zA-Z0-9_]+)'", message)
    if quoted:
        return quoted.group(1)
    explicit = re.search(r"OCR 字段\s+([a-zA-Z0-9_]+)", message)
    if explicit:
        return explicit.group(1)
    for field in REQUIRED_RECOGNIZED_FIELDS:
        if field in message:
            return field
    return None


def _reason_from_error(message: str) -> str:
    if "置信度" in message or "confidence" in message:
        return "low_confidence"
    if "missing" in message.lower() or "缺少" in message:
        return "field_missing"
    return "business_validation_failed"


def _manual_raw_text(field_name: str, reason: str, confirmed_by: str, confirmed_at: str) -> str:
    suffix = f"confirmed_by={confirmed_by}"
    if confirmed_at:
        suffix = f"{suffix}; confirmed_at={confirmed_at}"
    return f"人工确认字段 {field_name}: {reason}; {suffix}"
