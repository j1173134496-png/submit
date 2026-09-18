"""Machine-readable MVP-019 review confirmation contract helpers."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


CONTRACT_PATH = Path(__file__).resolve().parents[2] / "spec" / "contracts" / "review_confirmation_contract.json"


class ReviewContractError(RuntimeError):
    """Raised when review confirmation data violates the MVP-019 contract."""


@lru_cache(maxsize=1)
def load_review_confirmation_contract() -> dict[str, Any]:
    try:
        payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ReviewContractError(f"Review confirmation contract is invalid JSON: {CONTRACT_PATH}") from exc
    if not isinstance(payload, dict):
        raise ReviewContractError("Review confirmation contract must contain a JSON object.")
    return payload


def allowed_field_types() -> dict[str, tuple[str, ...]]:
    allowed = load_review_confirmation_contract().get("allowed_fields")
    if not isinstance(allowed, dict):
        raise ReviewContractError("Review confirmation contract is missing allowed_fields.")
    result: dict[str, tuple[str, ...]] = {}
    for field, definition in allowed.items():
        if not isinstance(field, str) or not isinstance(definition, dict):
            raise ReviewContractError("Review confirmation contract contains an invalid allowed_fields entry.")
        json_types = definition.get("json_types")
        if not isinstance(json_types, list) or not all(isinstance(item, str) for item in json_types):
            raise ReviewContractError(f"Review confirmation contract field {field!r} has invalid json_types.")
        result[field] = tuple(json_types)
    return result


def confirmable_fields_for_issue(field: object, reason: object) -> list[str]:
    allowed = allowed_field_types()
    if reason in {"low_confidence", "field_missing"}:
        return [field] if isinstance(field, str) and field in allowed else []
    if reason != "business_validation_failed" or not isinstance(field, str):
        return []

    mappings = load_review_confirmation_contract().get("business_validation_confirmable_fields")
    if not isinstance(mappings, dict):
        raise ReviewContractError("Review confirmation contract is missing business validation mappings.")
    mapped = mappings.get(field, [])
    if not isinstance(mapped, list) or not all(isinstance(item, str) and item in allowed for item in mapped):
        raise ReviewContractError(f"Review confirmation contract mapping {field!r} is invalid.")
    return list(mapped)


def validate_review_report(payload: object) -> set[str]:
    if not isinstance(payload, dict):
        raise ReviewContractError("review_report.json must contain a JSON object.")
    issues = payload.get("issues")
    if not isinstance(issues, list) or not issues:
        raise ReviewContractError("review_report.json must contain a non-empty issues array.")
    allowed = allowed_field_types()
    reason_enum = set(load_review_confirmation_contract().get("reason_enum") or [])
    issue_required = load_review_confirmation_contract().get("review_issue", {}).get("required")
    if not isinstance(issue_required, list) or not all(isinstance(key, str) for key in issue_required):
        raise ReviewContractError("Review confirmation contract has invalid review_issue.required.")
    confirmable: set[str] = set()
    for index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            raise ReviewContractError(f"review_report.json issue {index} must be an object.")
        missing = [key for key in issue_required if key not in issue]
        if missing:
            raise ReviewContractError(f"review_report.json issue {index} is missing required fields: {', '.join(missing)}")
        reason = issue.get("reason")
        if reason not in reason_enum:
            raise ReviewContractError(f"review_report.json issue {index} has unsupported reason: {reason!r}")
        fields = issue.get("confirmable_fields")
        if not isinstance(fields, list) or not all(isinstance(field, str) and field in allowed for field in fields):
            raise ReviewContractError(f"review_report.json issue {index} has invalid confirmable_fields.")
        confirmable.update(fields)
    return confirmable


def validate_confirmed_data(payload: object, *, allowed_fields: set[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReviewContractError("confirmed_data.json must contain a JSON object.")
    contract = load_review_confirmation_contract()
    allowed_top_level = set(contract["confirmed_data"]["allowed_top_level_fields"])
    unknown_top_level = sorted(set(payload) - allowed_top_level)
    if unknown_top_level:
        raise ReviewContractError(f"confirmed_data.json contains unknown fields: {', '.join(unknown_top_level)}")
    for key in contract["confirmed_data"]["required"]:
        if key not in payload:
            raise ReviewContractError(f"confirmed_data.json is missing required field: {key}")
    for key in ("confirmed_by", "confirmed_at"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ReviewContractError(f"confirmed_data.json field {key} must be a non-empty string.")

    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ReviewContractError("confirmed_data.json fields must be a non-empty object.")
    types = allowed_field_types()
    for field, patch in fields.items():
        if not isinstance(field, str) or field not in types:
            raise ReviewContractError(f"confirmed_data.json contains unknown confirmation field: {field!r}")
        if field not in allowed_fields:
            raise ReviewContractError(f"confirmed_data.json field is not confirmable for the current report: {field}")
        if not isinstance(patch, dict):
            raise ReviewContractError(f"confirmed_data.json field {field} must be an object.")
        unknown_patch_fields = sorted(set(patch) - {"value", "reason"})
        if unknown_patch_fields:
            raise ReviewContractError(
                f"confirmed_data.json field {field} contains unknown properties: {', '.join(unknown_patch_fields)}"
            )
        if "value" not in patch:
            raise ReviewContractError(f"confirmed_data.json field {field} is missing value.")
        if not _matches_json_types(patch["value"], types[field]):
            expected = ", ".join(types[field])
            raise ReviewContractError(f"confirmed_data.json field {field} value must have JSON type: {expected}")
        if "reason" in patch and (not isinstance(patch["reason"], str) or not patch["reason"].strip()):
            raise ReviewContractError(f"confirmed_data.json field {field} reason must be a non-empty string.")
    return payload


def _matches_json_types(value: object, json_types: tuple[str, ...]) -> bool:
    for json_type in json_types:
        if json_type == "string" and isinstance(value, str):
            return True
        if json_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
    return False
