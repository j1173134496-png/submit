"""Site-scoped pricing history and task pricing snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

from submit_flow_agent.config import SiteConfig, load_site_config
from submit_flow_agent.task_store import utc_now_iso


UNIT_PRICE_TOLERANCE = Decimal("0.0001")
DISCOUNT_RATE_ARTIFACT_QUANTUM = Decimal("0.0001")
DISCOUNT_RATE_ARTIFACT_TOLERANCE = Decimal("1e-12")
PROPOSAL_TTL_HOURS = 24


class PricingError(RuntimeError):
    """Raised when site pricing cannot be resolved or confirmed."""


@dataclass(frozen=True)
class PricingProposalResult:
    status: str
    site_key: str
    month: str
    ready_for_confirmation: bool
    pricing: dict[str, Any] | None
    inherited_fields: list[str]
    changed_fields: list[str]
    missing_fields: list[str]
    proposal_id: str | None = None
    proposal_hash: str | None = None
    base_pricing_version: int | None = None
    base_pricing_hash: str | None = None
    expires_at: str | None = None
    source_metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "site_key": self.site_key,
            "month": self.month,
            "ready_for_confirmation": self.ready_for_confirmation,
            "pricing": self.pricing,
            "inherited_fields": self.inherited_fields,
            "changed_fields": self.changed_fields,
            "missing_fields": self.missing_fields,
            "proposal_id": self.proposal_id,
            "proposal_hash": self.proposal_hash,
            "base_pricing_version": self.base_pricing_version,
            "base_pricing_hash": self.base_pricing_hash,
            "expires_at": self.expires_at,
            "source_metadata": self.source_metadata,
        }


def propose_site_pricing(
    runtime_root: Path | str,
    *,
    site_key: str,
    month: str,
    proposal: dict[str, Any] | None = None,
    config_path: Path | str = "config/sites.json",
) -> PricingProposalResult:
    site = load_site_config(site_key=site_key, config_path=config_path)
    _validate_month(month)
    root = Path(runtime_root)
    pricing_root = _pricing_dir(root, site.site_key)
    with _pricing_lock(pricing_root):
        history = _load_or_init_history(pricing_root, site.site_key)
        base_snapshot, source_metadata = _base_snapshot_for_proposal(site, month, history)
        normalized, inherited, changed, missing = _merge_pricing_proposal(site, month, proposal or {}, base_snapshot)
        ready = not missing
        if not ready:
            return PricingProposalResult(
                status="missing_pricing_fields",
                site_key=site.site_key,
                month=month,
                ready_for_confirmation=False,
                pricing=None,
                inherited_fields=inherited,
                changed_fields=changed,
                missing_fields=missing,
                base_pricing_version=_base_pricing_version(base_snapshot),
                base_pricing_hash=_base_pricing_hash(base_snapshot),
                source_metadata=source_metadata,
            )

        proposal_record = _new_proposal_record(
            site_key=site.site_key,
            month=month,
            pricing=normalized,
            inherited_fields=inherited,
            changed_fields=changed,
            base_snapshot=base_snapshot,
        )
        _write_json_atomic(_proposal_path(pricing_root, proposal_record["proposal_id"]), proposal_record)
        return PricingProposalResult(
            status="ready_for_confirmation",
            site_key=site.site_key,
            month=month,
            ready_for_confirmation=True,
            pricing=normalized,
            inherited_fields=inherited,
            changed_fields=changed,
            missing_fields=[],
            proposal_id=proposal_record["proposal_id"],
            proposal_hash=proposal_record["proposal_hash"],
            base_pricing_version=proposal_record["base_pricing_version"],
            base_pricing_hash=proposal_record["base_pricing_hash"],
            expires_at=proposal_record["expires_at"],
            source_metadata=source_metadata,
        )


def confirm_site_pricing(
    runtime_root: Path | str,
    *,
    proposal_id: str,
    config_path: Path | str = "config/sites.json",
    pricing_source: str = "operator_confirmed",
) -> dict[str, Any]:
    root = Path(runtime_root)
    proposal_site_key = _site_key_from_proposal_id(root, proposal_id)
    pricing_root = _pricing_dir(root, proposal_site_key)
    with _pricing_lock(pricing_root):
        proposal = _load_proposal(pricing_root, proposal_id)
        site = load_site_config(site_key=str(proposal["site_key"]), config_path=config_path)
        if site.site_key != proposal_site_key:
            raise PricingError("Pricing proposal site_key does not match storage boundary.")
        history = _load_or_init_history(pricing_root, site.site_key)
        _assert_proposal_hash(proposal)

        recovered = _committed_snapshot_for_proposal(history, proposal)
        if recovered is not None:
            _repair_committed_proposal_if_needed(pricing_root, proposal, recovered)
            return recovered

        if proposal.get("status") == "committed":
            raise PricingError("Pricing proposal is marked committed but no matching committed pricing version exists.")
        if proposal.get("status") != "prepared":
            raise PricingError("Pricing proposal is not prepared for confirmation.")
        _reject_expired_proposal(proposal)
        _assert_proposal_base_current(site, proposal, history)

        equivalent = _same_month_equivalent_committed_snapshot(history, proposal)
        if equivalent is not None:
            _mark_proposal_committed(proposal, equivalent)
            _write_json_atomic(_proposal_path(pricing_root, str(proposal["proposal_id"])), proposal)
            return equivalent

        version = int(history.get("latest_version") or 0) + 1
        snapshot = _versioned_snapshot(
            site_key=site.site_key,
            month=str(proposal["month"]),
            pricing=dict(proposal["pricing"]),
            version=version,
            pricing_source=pricing_source,
            proposal_id=str(proposal["proposal_id"]),
            proposal_hash=str(proposal["proposal_hash"]),
        )
        _append_history_version(pricing_root, history, snapshot)
        _mark_proposal_committed(proposal, snapshot)
        _write_json_atomic(_proposal_path(pricing_root, str(proposal["proposal_id"])), proposal)
        return snapshot


def latest_site_pricing(runtime_root: Path | str, *, site_key: str) -> dict[str, Any] | None:
    history_path = _pricing_dir(Path(runtime_root), site_key) / "pricing.json"
    if not history_path.is_file() or history_path.is_symlink():
        return None
    history = _read_history(history_path)
    _assert_history_site_key(history, site_key)
    return _latest_snapshot(history)


def validate_task_pricing_snapshot(snapshot: Any, *, site_key: str, month: str) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise PricingError("Task pricing_snapshot must be an object.")
    if snapshot.get("site_key") != site_key:
        raise PricingError("Task pricing_snapshot site_key does not match task site_key.")
    if snapshot.get("month") != month:
        raise PricingError("Task pricing_snapshot month does not match task month.")
    required = (
        "discount_rate",
        "effective_unit_price",
        "pricing_version",
        "pricing_source",
        "confirmed_at",
        "proposal_id",
        "proposal_hash",
        "pricing_hash",
    )
    missing = [field for field in required if _blank(snapshot.get(field))]
    if missing:
        raise PricingError(f"Task pricing_snapshot missing fields: {', '.join(missing)}.")
    # Verify the signed persisted representation before parsing numeric values. Rewriting a
    # legacy decimal here would invalidate its hash when the snapshot crosses a CLI boundary.
    if snapshot.get("pricing_hash") != _snapshot_hash(snapshot):
        raise PricingError("Task pricing_snapshot content hash does not match snapshot contents.")
    _parse_discount_rate(snapshot["discount_rate"])
    _positive_decimal(snapshot["effective_unit_price"], "effective_unit_price")
    _optional_positive_decimal(snapshot.get("base_unit_price"), "base_unit_price")
    return dict(snapshot)


def verify_committed_task_pricing_snapshot(
    runtime_root: Path | str,
    snapshot: Any,
    *,
    site_key: str,
    month: str,
) -> dict[str, Any]:
    validated = validate_task_pricing_snapshot(snapshot, site_key=site_key, month=month)
    pricing_root = _pricing_dir(Path(runtime_root), site_key)
    if not pricing_root.is_dir() or pricing_root.is_symlink():
        raise PricingError("Task pricing_snapshot site pricing history does not exist.")
    history_path = pricing_root / "pricing.json"
    if not history_path.is_file() or history_path.is_symlink():
        raise PricingError("Task pricing_snapshot site pricing history does not exist.")
    with _pricing_lock(pricing_root):
        history = _read_history(history_path)
        _assert_history_site_key(history, site_key)
        proposal = _load_proposal(pricing_root, str(validated["proposal_id"]))
        _assert_snapshot_committed(history, proposal, validated)
    return validated


def _committed_snapshot_for_proposal(history: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any] | None:
    _assert_proposal_hash(proposal)
    versions = [
        item
        for item in history.get("versions") or []
        if isinstance(item, dict) and item.get("proposal_id") == proposal.get("proposal_id")
    ]
    if not versions:
        return _committed_snapshot_recorded_on_proposal(history, proposal)
    if len(versions) != 1:
        raise PricingError("Pricing history contains duplicate versions for this proposal_id.")
    snapshot = validate_task_pricing_snapshot(
        versions[0],
        site_key=str(proposal.get("site_key") or ""),
        month=str(proposal.get("month") or ""),
    )
    if snapshot.get("proposal_hash") != proposal.get("proposal_hash"):
        raise PricingError("Committed pricing version proposal_hash does not match proposal.")
    if _proposal_pricing_values(snapshot) != _proposal_pricing_values(proposal.get("pricing")):
        raise PricingError("Committed pricing version values do not match proposal pricing.")
    return snapshot


def _committed_snapshot_recorded_on_proposal(history: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any] | None:
    if proposal.get("status") != "committed" or not isinstance(proposal.get("committed_snapshot"), dict):
        return None
    snapshot = validate_task_pricing_snapshot(
        proposal["committed_snapshot"],
        site_key=str(proposal.get("site_key") or ""),
        month=str(proposal.get("month") or ""),
    )
    if proposal.get("committed_snapshot_hash") != snapshot.get("pricing_hash"):
        raise PricingError("Committed pricing proposal snapshot hash does not match committed_snapshot.")
    if _business_pricing_values(snapshot) != _business_pricing_values(proposal.get("pricing")):
        raise PricingError("Committed pricing proposal snapshot values do not match proposal pricing.")
    matches = [
        item
        for item in history.get("versions") or []
        if isinstance(item, dict) and item.get("pricing_hash") == snapshot.get("pricing_hash")
    ]
    if len(matches) != 1:
        raise PricingError("Committed pricing proposal snapshot is not uniquely present in pricing history.")
    history_snapshot = validate_task_pricing_snapshot(
        matches[0],
        site_key=str(proposal.get("site_key") or ""),
        month=str(proposal.get("month") or ""),
    )
    if history_snapshot.get("pricing_hash") != snapshot.get("pricing_hash"):
        raise PricingError("Committed pricing proposal snapshot does not match pricing history.")
    return history_snapshot


def _proposal_pricing_values(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PricingError("Pricing proposal pricing must be an object.")
    discount = _parse_discount_rate(value.get("discount_rate"))
    effective = _positive_decimal(value.get("effective_unit_price"), "effective_unit_price")
    base = _optional_positive_decimal(value.get("base_unit_price"), "base_unit_price")
    return {
        "discount_rate": _decimal_text(discount),
        "effective_unit_price": _decimal_text(effective),
        "base_unit_price": _decimal_text(base) if base is not None else None,
    }


def pricing_business_values_equal(left: Any, right: Any) -> bool:
    return _business_pricing_values(left) == _business_pricing_values(right)


def _business_pricing_values(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict):
        raise PricingError("Pricing value must be an object.")
    discount = _parse_discount_rate(value.get("discount_rate"))
    effective = _positive_decimal(value.get("effective_unit_price"), "effective_unit_price")
    return {
        "discount_rate": _decimal_text(discount),
        "effective_unit_price": _decimal_text(effective),
    }


def _same_month_equivalent_committed_snapshot(history: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any] | None:
    proposal_values = _business_pricing_values(proposal.get("pricing"))
    matches: list[dict[str, Any]] = []
    for item in history.get("versions") or []:
        if not isinstance(item, dict) or item.get("month") != proposal.get("month"):
            continue
        snapshot = validate_task_pricing_snapshot(
            item,
            site_key=str(proposal.get("site_key") or ""),
            month=str(proposal.get("month") or ""),
        )
        if _business_pricing_values(snapshot) == proposal_values:
            matches.append(snapshot)
    if not matches:
        return None
    return sorted(matches, key=lambda item: int(item.get("pricing_version") or 0))[-1]


def _mark_proposal_committed(proposal: dict[str, Any], snapshot: dict[str, Any]) -> None:
    proposal["status"] = "committed"
    proposal["committed_at"] = snapshot["confirmed_at"]
    proposal["committed_pricing_version"] = snapshot["pricing_version"]
    proposal["committed_snapshot_hash"] = snapshot["pricing_hash"]
    proposal["committed_snapshot"] = snapshot


def _repair_committed_proposal_if_needed(pricing_root: Path, proposal: dict[str, Any], snapshot: dict[str, Any]) -> None:
    expected = dict(proposal)
    _mark_proposal_committed(expected, snapshot)
    if proposal != expected:
        proposal.clear()
        proposal.update(expected)
        _write_json_atomic(_proposal_path(pricing_root, str(proposal["proposal_id"])), proposal)

def _assert_snapshot_committed(history: dict[str, Any], proposal: dict[str, Any], snapshot: dict[str, Any]) -> None:
    recovered = _committed_snapshot_for_proposal(history, proposal)
    if recovered is None:
        raise PricingError("Task pricing_snapshot version is not committed for this site.")
    if recovered.get("pricing_hash") != snapshot.get("pricing_hash"):
        raise PricingError("Task pricing_snapshot hash does not match committed pricing version.")
    if proposal.get("status") != "committed":
        raise PricingError("Task pricing_snapshot proposal is not committed.")
    if proposal.get("committed_snapshot_hash") != snapshot.get("pricing_hash"):
        raise PricingError("Task pricing_snapshot hash does not match committed proposal.")


def pricing_snapshot_for_recognition(snapshot: dict[str, Any]) -> dict[str, Decimal]:
    discount = _parse_discount_rate(snapshot["discount_rate"])
    effective = _positive_decimal(snapshot["effective_unit_price"], "effective_unit_price")
    return {
        "school_base_price": effective,
        "school_discount_rate": discount,
        "effective_unit_price": effective,
    }


def snapshot_from_json(value: str | None) -> dict[str, Any] | None:
    if value is None or not str(value).strip():
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PricingError(f"pricing JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise PricingError("pricing JSON must be an object.")
    return payload


def _merge_pricing_proposal(
    site: SiteConfig,
    month: str,
    proposal: dict[str, Any],
    base_snapshot: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    _reject_cross_site_proposal(site.site_key, proposal)
    unsupported = sorted(
        key
        for key in proposal
        if key not in {"site_key", "month", "discount_rate", "effective_unit_price"}
    )
    if unsupported:
        raise PricingError(
            "Pricing proposal accepts only discount_rate and effective_unit_price; "
            f"unsupported fields: {', '.join(unsupported)}."
        )
    discount = _proposal_decimal(proposal, "discount_rate", parser=_parse_discount_rate)
    effective = _proposal_decimal(
        proposal,
        "effective_unit_price",
        parser=lambda value: _positive_decimal(value, "effective_unit_price"),
    )
    inherited: list[str] = []
    changed: list[str] = []
    missing: list[str] = []
    base_discount = _base_decimal(base_snapshot, "discount_rate", _parse_discount_rate)
    base_effective = _base_decimal(base_snapshot, "effective_unit_price", lambda value: _positive_decimal(value, "effective_unit_price"))

    if discount is None:
        discount = base_discount
        inherited.append("discount_rate") if discount is not None else missing.append("discount_rate")
    else:
        changed.append("discount_rate")
    if effective is None:
        effective = base_effective
        inherited.append("effective_unit_price") if effective is not None else missing.append("effective_unit_price")
    else:
        changed.append("effective_unit_price")
    if missing:
        return {}, inherited, changed, missing
    assert discount is not None and effective is not None
    return {
        "site_key": site.site_key,
        "month": month,
        "discount_rate": _decimal_text(discount),
        "effective_unit_price": _decimal_text(effective),
        # Retained as a read-compatible snapshot field only. It is never accepted
        # from the operator and is never used to derive the settlement price.
        "base_unit_price": None,
    }, inherited, changed, missing


def _base_snapshot_for_proposal(site: SiteConfig, month: str, history: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    same_month = _latest_snapshot_for_month(history, month)
    if same_month is not None:
        return _snapshot_for_month(same_month, month), _source_metadata("same_month_confirmed", site.site_key, same_month)
    prior = _latest_prior_month_snapshot(history, month)
    if prior is not None:
        return _snapshot_for_month(prior, month), _source_metadata("latest_same_site_confirmed_month", site.site_key, prior)
    seed = _config_seed_snapshot(site, month)
    if seed is not None:
        return seed, _source_metadata("site_default", site.site_key, seed)
    return None, {"source_type": "missing", "source_site_key": site.site_key, "source_month": None, "pricing_version": None, "pricing_hash": None}


def _latest_snapshot_for_month(history: dict[str, Any], month: str) -> dict[str, Any] | None:
    candidates = [item for item in history.get("versions") or [] if isinstance(item, dict) and item.get("month") == month]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: int(item.get("pricing_version") or 0))[-1]


def _latest_prior_month_snapshot(history: dict[str, Any], month: str) -> dict[str, Any] | None:
    candidates = [
        item
        for item in history.get("versions") or []
        if isinstance(item, dict) and isinstance(item.get("month"), str) and str(item.get("month")) < month
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (str(item.get("month") or ""), int(item.get("pricing_version") or 0)))[-1]


def _source_metadata(source_type: str, site_key: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_type": source_type,
        "source_site_key": site_key,
        "source_month": snapshot.get("effective_month") or snapshot.get("month"),
        "pricing_version": snapshot.get("pricing_version"),
        "pricing_hash": snapshot.get("pricing_hash"),
    }


def _config_seed_snapshot(site: SiteConfig, month: str) -> dict[str, Any] | None:
    if site.dynamic_site_signature or site.pricing_required:
        return None
    effective = site.effective_school_price or site.school_base_price
    seed = {
        "site_key": site.site_key,
        "month": month,
        "discount_rate": _decimal_text(site.school_discount_rate),
        "effective_unit_price": _decimal_text(effective),
        "base_unit_price": None,
        "pricing_version": 0,
        "pricing_source": "site_config_seed_candidate",
    }
    return {**seed, "pricing_hash": _hash_payload(seed)}


def _new_proposal_record(
    *,
    site_key: str,
    month: str,
    pricing: dict[str, Any],
    inherited_fields: list[str],
    changed_fields: list[str],
    base_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    created_at = utc_now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=PROPOSAL_TTL_HOURS)).isoformat(timespec="seconds")
    proposal_id = _new_proposal_id()
    record = {
        "proposal_id": proposal_id,
        "status": "prepared",
        "site_key": site_key,
        "month": month,
        "pricing": pricing,
        "inherited_fields": list(inherited_fields),
        "changed_fields": list(changed_fields),
        "base_pricing_version": _base_pricing_version(base_snapshot),
        "base_pricing_hash": _base_pricing_hash(base_snapshot),
        "created_at": created_at,
        "expires_at": expires_at,
    }
    record["proposal_hash"] = _proposal_hash(record)
    return record


def _versioned_snapshot(
    *,
    site_key: str,
    month: str,
    pricing: dict[str, Any],
    version: int,
    pricing_source: str,
    proposal_id: str,
    proposal_hash: str,
) -> dict[str, Any]:
    snapshot = {
        "site_key": site_key,
        "month": month,
        "discount_rate": pricing["discount_rate"],
        "effective_unit_price": pricing["effective_unit_price"],
        "base_unit_price": pricing.get("base_unit_price"),
        "pricing_version": version,
        "pricing_source": pricing_source,
        "confirmed_at": utc_now_iso(),
        "effective_month": month,
        "proposal_id": proposal_id,
        "proposal_hash": proposal_hash,
    }
    snapshot["pricing_hash"] = _snapshot_hash(snapshot)
    return snapshot


def _snapshot_for_month(snapshot: dict[str, Any], month: str) -> dict[str, Any]:
    return {**snapshot, "month": month}


def _append_history_version(pricing_root: Path, history: dict[str, Any], snapshot: dict[str, Any]) -> None:
    versions = history.setdefault("versions", [])
    if not isinstance(versions, list):
        raise PricingError("Pricing history versions must be a list.")
    versions.append(snapshot)
    history["latest_version"] = snapshot["pricing_version"]
    history["updated_at"] = utc_now_iso()
    _write_json_atomic(pricing_root / "pricing.json", history)


def _load_or_init_history(pricing_root: Path, site_key: str) -> dict[str, Any]:
    path = pricing_root / "pricing.json"
    if path.is_file() and not path.is_symlink():
        history = _read_history(path)
        _assert_history_site_key(history, site_key)
        return history
    return {
        "site_key": site_key,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "latest_version": 0,
        "versions": [],
    }


def _read_history(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise PricingError("Pricing history is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise PricingError("Pricing history must contain an object.")
    versions = payload.get("versions")
    if not isinstance(versions, list):
        raise PricingError("Pricing history versions must be a list.")
    return payload


def _assert_history_site_key(history: dict[str, Any], site_key: str) -> None:
    if history.get("site_key") != site_key:
        raise PricingError("Pricing history site_key does not match requested site_key.")


def _latest_snapshot(history: dict[str, Any]) -> dict[str, Any] | None:
    versions = [item for item in history.get("versions") or [] if isinstance(item, dict)]
    if not versions:
        return None
    return sorted(versions, key=lambda item: int(item.get("pricing_version") or 0))[-1]


def _history_version(history: dict[str, Any], version: int) -> dict[str, Any] | None:
    for item in history.get("versions") or []:
        if isinstance(item, dict) and item.get("pricing_version") == version:
            return item
    return None


def _pricing_dir(runtime_root: Path, site_key: str) -> Path:
    return runtime_root / "site_pricing" / _safe_segment(site_key, "site_key")


def _proposal_dir(pricing_root: Path) -> Path:
    return pricing_root / "proposals"


def _proposal_path(pricing_root: Path, proposal_id: str) -> Path:
    return _proposal_dir(pricing_root) / f"{_safe_segment(proposal_id, 'proposal_id')}.json"


def _site_key_from_proposal_id(runtime_root: Path, proposal_id: str) -> str:
    safe_id = _safe_segment(proposal_id, "proposal_id")
    pricing_root = runtime_root / "site_pricing"
    if not pricing_root.is_dir() or pricing_root.is_symlink():
        raise PricingError("Pricing proposal does not exist.")
    matches: list[str] = []
    for site_dir in pricing_root.iterdir():
        if not site_dir.is_dir() or site_dir.is_symlink():
            continue
        proposal_path = site_dir / "proposals" / f"{safe_id}.json"
        if proposal_path.is_file() and not proposal_path.is_symlink():
            matches.append(site_dir.name)
    if len(matches) != 1:
        raise PricingError("Pricing proposal identity is missing or ambiguous.")
    return matches[0]


def _load_proposal(pricing_root: Path, proposal_id: str) -> dict[str, Any]:
    path = _proposal_path(pricing_root, proposal_id)
    if not path.is_file() or path.is_symlink():
        raise PricingError("Pricing proposal does not exist.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise PricingError("Pricing proposal is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise PricingError("Pricing proposal must be an object.")
    if payload.get("proposal_id") != proposal_id:
        raise PricingError("Pricing proposal_id does not match proposal file.")
    return payload


def _new_proposal_id() -> str:
    return f"pp_{secrets.token_urlsafe(24).replace('-', '_')}"


def _safe_segment(value: str, label: str) -> str:
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1 or not str(value).strip():
        raise PricingError(f"Unsafe {label}: {value}")
    return str(value)


def _validate_month(month: str) -> None:
    if len(month) != 7 or month[4] != "-" or not month[:4].isdigit() or not month[5:].isdigit():
        raise PricingError(f"Invalid month: {month}")
    month_number = int(month[5:])
    if month_number < 1 or month_number > 12:
        raise PricingError(f"Invalid month: {month}")


def _reject_cross_site_proposal(site_key: str, proposal: dict[str, Any]) -> None:
    proposed_site_key = proposal.get("site_key")
    if proposed_site_key is not None and proposed_site_key != site_key:
        raise PricingError("Pricing proposal site_key does not match requested site_key.")


def _reject_expired_proposal(proposal: dict[str, Any]) -> None:
    expires_at = str(proposal.get("expires_at") or "")
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PricingError("Pricing proposal expiration is invalid.") from exc
    if expires <= datetime.now(timezone.utc):
        raise PricingError("Pricing proposal has expired; prepare a fresh proposal.")


def _assert_proposal_hash(proposal: dict[str, Any]) -> None:
    if proposal.get("proposal_hash") != _proposal_hash(proposal):
        raise PricingError("Pricing proposal hash does not match proposal contents.")


def _assert_proposal_base_current(site: SiteConfig, proposal: dict[str, Any], history: dict[str, Any]) -> None:
    current_base, _ = _base_snapshot_for_proposal(site, str(proposal["month"]), history)
    if _base_pricing_version(current_base) != proposal.get("base_pricing_version"):
        raise PricingError("Pricing proposal base version changed; prepare a fresh proposal.")
    if _base_pricing_hash(current_base) != proposal.get("base_pricing_hash"):
        raise PricingError("Pricing proposal base hash changed; prepare a fresh proposal.")


def _proposal_decimal(proposal: dict[str, Any], key: str, *, parser: Any) -> Decimal | None:
    if key not in proposal or _blank(proposal.get(key)):
        return None
    return parser(proposal[key])


def _base_decimal(base_snapshot: dict[str, Any] | None, key: str, parser: Any) -> Decimal | None:
    if base_snapshot is None or _blank(base_snapshot.get(key)):
        return None
    return parser(base_snapshot[key])


def _parse_discount_rate(value: Any) -> Decimal:
    text = str(value).strip()
    if not text:
        raise PricingError("discount_rate is required.")
    if text.endswith("%"):
        return _bounded_discount(_decimal_text_input(text[:-1]) / Decimal("100"))
    if text.endswith("折"):
        number = _decimal_text_input(text[:-1])
        return _bounded_discount(number / (Decimal("100") if number > Decimal("10") else Decimal("10")))
    number = _decimal_text_input(text)
    return _bounded_discount(number)


def _bounded_discount(value: Decimal) -> Decimal:
    normalized = canonicalize_discount_rate(value)
    if normalized <= 0 or normalized > 1:
        raise PricingError("discount_rate must be greater than 0 and no greater than 1.")
    return normalized


def canonicalize_discount_rate(value: Decimal) -> Decimal:
    nearest_business_decimal = value.quantize(DISCOUNT_RATE_ARTIFACT_QUANTUM, rounding=ROUND_HALF_UP)
    if abs(value - nearest_business_decimal) <= DISCOUNT_RATE_ARTIFACT_TOLERANCE:
        return nearest_business_decimal
    return value


def _positive_decimal(value: Any, field: str) -> Decimal:
    parsed = _decimal_text_input(value)
    if parsed <= 0:
        raise PricingError(f"{field} must be positive.")
    return parsed


def _optional_positive_decimal(value: Any, field: str) -> Decimal | None:
    if _blank(value):
        return None
    return _positive_decimal(value, field)


def _decimal_text_input(value: Any) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise PricingError(f"Invalid decimal value: {value!r}") from exc



def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _base_pricing_version(base_snapshot: dict[str, Any] | None) -> int:
    if base_snapshot is None:
        return 0
    try:
        return int(base_snapshot.get("pricing_version") or 0)
    except (TypeError, ValueError) as exc:
        raise PricingError("Base pricing version is invalid.") from exc


def _base_pricing_hash(base_snapshot: dict[str, Any] | None) -> str | None:
    if base_snapshot is None:
        return None
    if isinstance(base_snapshot.get("pricing_hash"), str):
        return str(base_snapshot["pricing_hash"])
    return _snapshot_hash(base_snapshot)


def _proposal_hash(proposal: dict[str, Any]) -> str:
    immutable_keys = (
        "proposal_id",
        "site_key",
        "month",
        "pricing",
        "inherited_fields",
        "changed_fields",
        "base_pricing_version",
        "base_pricing_hash",
        "created_at",
        "expires_at",
    )
    return _hash_payload({key: proposal.get(key) for key in immutable_keys})


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    return _hash_payload({key: value for key, value in snapshot.items() if key != "pricing_hash"})


def _hash_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _pricing_lock(pricing_root: Path) -> Iterator[None]:
    pricing_root.mkdir(parents=True, exist_ok=True)
    with (pricing_root / ".pricing.lock").open("a+b") as handle:
        _lock_handle(handle)
        try:
            yield
        finally:
            _unlock_handle(handle)


def _lock_handle(handle: Any) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)















