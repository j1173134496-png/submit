"""File-backed task service for MVP-012."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from submit_flow_agent.artifact_index import ArtifactIndexError, write_task_artifact_index
from submit_flow_agent.config import load_site_config
from submit_flow_agent.file_scanner import (
    REQUIRED_FILE_TYPES,
    classify_pdf_text_content,
    classify_source_pdf_by_content,
    detect_month,
    detect_semantic_months,
    extract_pdf_text,
)
from submit_flow_agent.ocr.base import write_ocr_raw_result
from submit_flow_agent.ocr.factory import create_ocr_adapter
from submit_flow_agent.pdf_renderer import (
    MONTHLY_SOURCE_EXTENSIONS,
    PdfRenderError,
    render_monthly_source_to_images,
    validate_monthly_source_file,
)
from submit_flow_agent.pipeline import run_month
from submit_flow_agent.pricing import (
    PricingError,
    confirm_site_pricing,
    propose_site_pricing,
    pricing_business_values_equal,
    validate_task_pricing_snapshot,
    verify_committed_task_pricing_snapshot,
)
from submit_flow_agent.review_contract import ReviewContractError, validate_confirmed_data, validate_review_report
from submit_flow_agent.task_series import (
    TaskSeriesError,
    note_task_pricing_snapshot,
    note_task_status,
    promote_effective_revision,
    resolve_history_workbooks,
    resolve_task as resolve_series_task,
)
from submit_flow_agent.task_store import (
    append_event_locked,
    init_task,
    list_outputs as store_list_outputs,
    load_task,
    TaskStoreError,
    task_lock,
    task_root_from_id,
    update_status,
    utc_now_iso,
    write_task,
)


FILE_INTAKE_STATUSES = {"collecting_files", "ready_to_run"}
RESTART_BUSY_STATUSES = {"running", "confirmed"}
CANCELLABLE_STATUSES = {"collecting_files", "ready_to_run", "need_review"}
CANCELLED_STATUS = "cancelled"
CANCELLED_REASON = "operator_cancelled"


class TaskServiceError(RuntimeError):
    """Raised when task service operations fail."""


@dataclass(frozen=True)
class TaskRunResult:
    task: dict[str, Any]
    task_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {"task_dir": str(self.task_dir), **self.task}


def propose_task_pricing(
    runtime_root: Path | str,
    *,
    site_key: str,
    month: str,
    pricing: dict[str, Any] | None = None,
    config_path: Path | str = "config/sites.json",
) -> dict[str, Any]:
    return propose_site_pricing(
        runtime_root,
        site_key=site_key,
        month=month,
        proposal=pricing or {},
        config_path=config_path,
    ).to_dict()


def confirm_task_pricing(
    runtime_root: Path | str,
    *,
    proposal_id: str,
    config_path: Path | str = "config/sites.json",
) -> dict[str, Any]:
    return confirm_site_pricing(
        runtime_root,
        proposal_id=proposal_id,
        config_path=config_path,
    )


def create_task(
    runtime_root: Path | str,
    *,
    task_id: str,
    site_key: str = "xinan_high_school",
    month: str,
    config_path: Path | str = "config/sites.json",
    pricing_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    site = load_site_config(site_key=site_key, config_path=config_path)
    snapshot = _resolve_pricing_snapshot_for_task(
        runtime_root,
        site.site_key,
        month,
        pricing_snapshot=pricing_snapshot,
        config_path=config_path,
    )
    task = init_task(
        runtime_root,
        task_id=task_id,
        site_key=site.site_key,
        site_name=site.site_name,
        month=month,
    )
    return _freeze_task_pricing_snapshot(runtime_root, task, snapshot)

def resolve_task(
    runtime_root: Path | str,
    *,
    site_key: str,
    month: str,
    mode: str = "locate",
    config_path: Path | str = "config/sites.json",
    pricing_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mode == "restart":
        restart_state = resolve_task_state(runtime_root, site_key=site_key, month=month, config_path=config_path)
        if not _restart_state_has_source(restart_state):
            return _restart_source_missing_payload(restart_state)
        if pricing_snapshot is None:
            return _prepare_restart_pricing_confirmation(
                runtime_root,
                site_key=site_key,
                month=month,
                config_path=config_path,
                state=restart_state,
            )
    if pricing_snapshot is None:
        _preflight_resolve_without_pricing_snapshot(runtime_root, site_key=site_key, month=month, mode=mode, config_path=config_path)
    try:
        payload = resolve_series_task(runtime_root, site_key=site_key, month=month, mode=mode, config_path=config_path)
    except TaskSeriesError as exc:
        raise TaskServiceError(str(exc)) from exc
    return _ensure_resolved_task_pricing_snapshot(
        runtime_root,
        payload,
        pricing_snapshot=pricing_snapshot,
        config_path=config_path,
    )


def resolve_task_state(
    runtime_root: Path | str,
    *,
    site_key: str,
    month: str,
    config_path: Path | str = "config/sites.json",
) -> dict[str, Any]:
    site = load_site_config(site_key=site_key, config_path=config_path)
    _validate_resolve_month(month)
    root = Path(runtime_root)
    series_path = root / "site_series" / site.site_key / month / "series.json"
    series = _read_series_for_state(series_path)
    latest = _latest_state_revision(series)
    effective = _state_revision_by_number(series, series.get("effective_revision")) if series else None
    active_task_id = _canonical_active_task_id(series, latest, effective)
    task = _load_task_for_state(root, active_task_id, site.site_key, month)
    return _canonical_state_payload(site, month, series, latest, effective, task)


def _prepare_restart_pricing_confirmation(
    runtime_root: Path | str,
    *,
    site_key: str,
    month: str,
    config_path: Path | str,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if state is None:
        state = resolve_task_state(runtime_root, site_key=site_key, month=month, config_path=config_path)
    status = str(state.get("status") or "")
    if status in RESTART_BUSY_STATUSES:
        raise TaskServiceError("Current site/month task is busy; retry after processing finishes.")
    proposal = propose_task_pricing(runtime_root, site_key=str(state["site_key"]), month=month, pricing={}, config_path=config_path)
    return {
        **state,
        "resolution": "restart_pricing_pending",
        "mode": "restart",
        "stage": "pricing_pending",
        "pricing_pending": True,
        "pricing_proposal": proposal,
        "restart_contract": {
            "creates_revision": False,
            "next_action": "confirm_task_pricing_then_resolve_restart",
        },
    }


def _restart_state_has_source(state: dict[str, Any]) -> bool:
    return bool(state.get("task_exists") and state.get("task_id") and state.get("revision") is not None)


def _restart_source_missing_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "resolution": "restart_source_missing",
        "mode": "restart",
        "site_key": state.get("site_key"),
        "site_name": state.get("site_name"),
        "month": state.get("month"),
        "status": "restart_source_missing",
        "stage": "restart_unavailable",
        "pricing_pending": False,
        "error_code": "restart_source_missing",
        "message": "Restart requires existing canonical task/series history for this site/month.",
        "restart_contract": {
            "creates_revision": False,
            "next_allowed_action": "locate_or_create_month",
        },
    }


def _read_series_for_state(series_path: Path) -> dict[str, Any] | None:
    if not series_path.is_file() or series_path.is_symlink():
        return None
    try:
        payload = json.loads(series_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TaskServiceError("Existing task series is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise TaskServiceError("Existing task series must be a JSON object.")
    return payload


def _state_revisions(series: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not series:
        return []
    revisions = [item for item in series.get("revisions") or [] if isinstance(item, dict)]
    return sorted(revisions, key=lambda item: int(item.get("revision") or 0))


def _latest_state_revision(series: dict[str, Any] | None) -> dict[str, Any] | None:
    revisions = _state_revisions(series)
    return revisions[-1] if revisions else None


def _state_revision_by_number(series: dict[str, Any] | None, revision_number: Any) -> dict[str, Any] | None:
    if revision_number is None:
        return None
    for revision in _state_revisions(series):
        if revision.get("revision") == revision_number:
            return revision
    return None


def _canonical_active_task_id(
    series: dict[str, Any] | None,
    latest: dict[str, Any] | None,
    effective: dict[str, Any] | None,
) -> str | None:
    if latest is not None and isinstance(latest.get("task_id"), str):
        return str(latest["task_id"])
    if series and isinstance(series.get("active_task_id"), str):
        return str(series["active_task_id"])
    if effective is not None and isinstance(effective.get("task_id"), str):
        return str(effective["task_id"])
    return None


def _load_task_for_state(runtime_root: Path, task_id: str | None, site_key: str, month: str) -> dict[str, Any] | None:
    if not task_id:
        return None
    try:
        task = load_task(task_root_from_id(runtime_root, task_id))
    except TaskStoreError as exc:
        raise TaskServiceError("Canonical task state references an unavailable task.") from exc
    if str(task.get("site_key") or "") != site_key or str(task.get("month") or "") != month:
        raise TaskServiceError("Canonical task state crossed site/month boundaries.")
    return task


def _canonical_state_payload(
    site: Any,
    month: str,
    series: dict[str, Any] | None,
    latest: dict[str, Any] | None,
    effective: dict[str, Any] | None,
    task: dict[str, Any] | None,
) -> dict[str, Any]:
    task_status = task.get("status") if task else None
    latest_status = latest.get("status") if latest else None
    status = str(task_status or latest_status or "none")
    task_id_value = task.get("task_id") if task else latest.get("task_id") if latest else None
    task_id = str(task_id_value) if task_id_value else None
    revision = task.get("revision") if task else latest.get("revision") if latest else None
    pricing_snapshot = task.get("pricing_snapshot") if task else None
    pricing_pending = not isinstance(pricing_snapshot, dict)
    intake = _normalize_pdf_intake(task.get("pdf_intake") if task else None)
    files = task.get("files") if task and isinstance(task.get("files"), dict) else {}
    outputs = task.get("outputs") if task and isinstance(task.get("outputs"), list) else []
    output_count = len(outputs) if status == "completed" else 0
    staleness = _state_staleness(series, latest, effective)
    downstream_staleness = task.get("downstream_staleness") if task and isinstance(task.get("downstream_staleness"), dict) else {}
    return {
        "site_key": site.site_key,
        "site_name": site.site_name,
        "month": month,
        "series_id": series.get("series_id") if series else f"{site.site_key}:{month}",
        "resolution": "canonical_state",
        "task_exists": task is not None,
        "task_id": task_id,
        "revision": revision,
        "status": status,
        "stage": _stage_for_status(status, pricing_pending),
        "pricing_pending": pricing_pending,
        "pricing_snapshot": _safe_pricing_snapshot_state(pricing_snapshot),
        "pdf_intake": {
            "batch_state": intake.get("batch_state"),
            "received_count": intake.get("received_count"),
            "required_count": intake.get("required_count"),
            "missing_roles": intake.get("missing_roles") or [],
            "conflicting_roles": intake.get("conflicting_roles") or [],
            "next_allowed_action": intake.get("next_allowed_action"),
            "registered_file_count": _registered_file_count({"files": files}),
        },
        "outputs": {
            "available": status == "completed" and output_count >= 2,
            "count": output_count,
            "expected_count": 2,
            "staleness": staleness,
        },
        "staleness": staleness,
        "downstream_staleness": {
            "affected_months": downstream_staleness.get("affected_months") or [],
            "next_rebuild_month": downstream_staleness.get("next_rebuild_month"),
        },
        "series": {
            "latest_revision": series.get("latest_revision") if series else None,
            "working_revision": series.get("working_revision") if series else None,
            "effective_revision": series.get("effective_revision") if series else None,
            "active_task_id": series.get("active_task_id") if series else None,
            "effective_task_id": series.get("effective_task_id") if series else None,
        },
        "binding": {
            "site_key": site.site_key,
            "month": month,
            "task_id": task_id,
            "revision": revision,
            "stage": _stage_for_status(status, pricing_pending),
        },
    }


def _state_staleness(
    series: dict[str, Any] | None,
    latest: dict[str, Any] | None,
    effective: dict[str, Any] | None,
) -> dict[str, Any]:
    raw = effective.get("staleness") if isinstance(effective, dict) else None
    is_stale = isinstance(raw, dict) and raw.get("is_stale") is True
    current_month = series.get("month") if isinstance(series, dict) else None
    affected_months = [current_month] if is_stale and isinstance(current_month, str) else []
    return {
        "is_stale": is_stale,
        "upstream_changed_month": raw.get("upstream_changed_month") if is_stale else None,
        "upstream_changed_revision": raw.get("upstream_changed_revision") if is_stale else None,
        "marked_at": raw.get("marked_at") if is_stale else None,
        "affected_months": affected_months,
        "next_rebuild_month": affected_months[0] if affected_months else None,
    }


def _safe_pricing_snapshot_state(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {"confirmed": False}
    return {
        "confirmed": True,
        "pricing_version": snapshot.get("pricing_version"),
        "pricing_hash": snapshot.get("pricing_hash"),
        "discount_rate": snapshot.get("discount_rate"),
        "effective_unit_price": snapshot.get("effective_unit_price"),
    }


def _stage_for_status(status: str, pricing_pending: bool) -> str:
    if pricing_pending:
        return "pricing_pending"
    if status in {"collecting_files", "ready_to_run", "running", "completed", "need_review", "failed", "cancelled"}:
        return status
    if status == "confirmed":
        return "running"
    return "unresolved"


def _assert_canonical_task_reference(
    runtime_root: Path | str,
    task_id: str,
    *,
    site_key: str | None,
    month: str | None,
    config_path: Path | str,
) -> None:
    if site_key is None and month is None:
        return
    if not site_key or not month:
        raise TaskServiceError("Canonical task guard requires both site_key and month.")
    state = resolve_task_state(runtime_root, site_key=site_key, month=month, config_path=config_path)
    if state.get("task_id") != task_id:
        raise TaskServiceError("Task does not match the canonical site/month state.")

def _attach_canonical_state(
    runtime_root: Path | str,
    payload: dict[str, Any],
    *,
    site_key: str,
    month: str,
    config_path: Path | str,
) -> dict[str, Any]:
    state = resolve_task_state(runtime_root, site_key=site_key, month=month, config_path=config_path)
    payload["state"] = state
    payload["binding"] = state["binding"]
    payload["stage"] = state["stage"]
    payload["pricing_pending"] = state["pricing_pending"]
    payload["pdf_intake"] = state["pdf_intake"]
    payload["outputs"] = state["outputs"]
    return payload

def _preflight_resolve_without_pricing_snapshot(
    runtime_root: Path | str,
    *,
    site_key: str,
    month: str,
    mode: str,
    config_path: Path | str,
) -> None:
    site = load_site_config(site_key=site_key, config_path=config_path)
    _validate_resolve_month(month)
    series_path = Path(runtime_root) / "site_series" / site.site_key / month / "series.json"
    if not series_path.is_file() or series_path.is_symlink():
        raise TaskServiceError("A committed pricing snapshot is required before creating a new task revision.")
    try:
        series = json.loads(series_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TaskServiceError("Existing task series is not valid JSON.") from exc
    if not isinstance(series, dict):
        raise TaskServiceError("Existing task series must be a JSON object.")
    revisions = [item for item in series.get("revisions") or [] if isinstance(item, dict)]
    latest = sorted(revisions, key=lambda item: int(item.get("revision") or 0))[-1] if revisions else None
    if mode == "restart":
        if latest is not None:
            _verified_revision_task_pricing_snapshot(
                runtime_root,
                latest,
                site_key=site.site_key,
                month=month,
                label="restart",
                sync_series=True,
            )
            return
        raise TaskServiceError("restart without explicit pricing requires a source revision pricing_snapshot.")
    reusable_statuses = {"collecting_files", "ready_to_run", "running", "need_review", "confirmed", "completed"}
    if latest is not None and str(latest.get("status") or "") in reusable_statuses:
        _verified_revision_task_pricing_snapshot(
            runtime_root,
            latest,
            site_key=site.site_key,
            month=month,
            label="existing revision",
            sync_series=True,
        )
        return
    raise TaskServiceError("A committed pricing snapshot is required before creating a new task revision.")


def _validate_resolve_month(month: str) -> None:
    if len(month) != 7 or month[4] != "-" or not month[:4].isdigit() or not month[5:].isdigit():
        raise TaskServiceError(f"Invalid month: {month}")
    month_number = int(month[5:])
    if month_number < 1 or month_number > 12:
        raise TaskServiceError(f"Invalid month: {month}")

def _resolve_pricing_snapshot_for_task(
    runtime_root: Path | str,
    site_key: str,
    month: str,
    *,
    pricing_snapshot: dict[str, Any] | None,
    config_path: Path | str,
) -> dict[str, Any]:
    if pricing_snapshot is None:
        raise PricingError("A committed pricing snapshot is required before creating or resolving a new task revision.")
    return verify_committed_task_pricing_snapshot(runtime_root, pricing_snapshot, site_key=site_key, month=month)


def _ensure_resolved_task_pricing_snapshot(
    runtime_root: Path | str,
    payload: dict[str, Any],
    *,
    pricing_snapshot: dict[str, Any] | None,
    config_path: Path | str,
) -> dict[str, Any]:
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return payload
    root = task_root_from_id(runtime_root, task_id)
    with task_lock(root):
        task = load_task(root)
        site_key = str(task.get("site_key") or "")
        month = str(task.get("month") or "")
        if pricing_snapshot is None and isinstance(task.get("pricing_snapshot"), dict):
            snapshot = _committed_task_pricing_snapshot(runtime_root, task)
            _sync_task_series_pricing_snapshot(runtime_root, task, snapshot)
            payload["pricing_snapshot"] = snapshot
            return _attach_canonical_state(runtime_root, payload, site_key=site_key, month=month, config_path=config_path)
        if pricing_snapshot is None:
            snapshot = _restart_source_task_pricing_snapshot(runtime_root, task)
        else:
            snapshot = _resolve_pricing_snapshot_for_task(
                runtime_root,
                site_key,
                month,
                pricing_snapshot=pricing_snapshot,
                config_path=config_path,
            )
        existing = task.get("pricing_snapshot")
        if isinstance(existing, dict) and existing != snapshot:
            existing_snapshot = _committed_task_pricing_snapshot(runtime_root, task)
            if pricing_business_values_equal(existing_snapshot, snapshot):
                _sync_task_series_pricing_snapshot(runtime_root, task, existing_snapshot)
                payload["pricing_snapshot"] = existing_snapshot
                return _attach_canonical_state(runtime_root, payload, site_key=site_key, month=month, config_path=config_path)
            if pricing_snapshot is None:
                snapshot = existing_snapshot
                _sync_task_series_pricing_snapshot(runtime_root, task, snapshot)
                payload["pricing_snapshot"] = snapshot
                return _attach_canonical_state(runtime_root, payload, site_key=site_key, month=month, config_path=config_path)
            if str(task.get("status") or "") != "collecting_files" or _registered_file_count(task) != 0:
                raise TaskServiceError("Explicit pricing change requires a fresh task before files are accepted.")
        task["pricing_snapshot"] = snapshot
        task["pricing_version"] = snapshot["pricing_version"]
        task["pricing_source"] = snapshot["pricing_source"]
        write_task(root, task)
        _sync_task_series_pricing_snapshot(runtime_root, task, snapshot)
        append_event_locked(
            root,
            "pricing_snapshot_frozen",
            pricing_version=snapshot["pricing_version"],
            pricing_source=snapshot["pricing_source"],
        )
    payload["pricing_snapshot"] = snapshot
    return _attach_canonical_state(runtime_root, payload, site_key=site_key, month=month, config_path=config_path)


def _freeze_task_pricing_snapshot(runtime_root: Path | str, task: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    root = task_root_from_id(runtime_root, str(task["task_id"]))
    with task_lock(root):
        current = load_task(root)
        current["pricing_snapshot"] = snapshot
        current["pricing_version"] = snapshot["pricing_version"]
        current["pricing_source"] = snapshot["pricing_source"]
        write_task(root, current)
        _sync_task_series_pricing_snapshot(runtime_root, current, snapshot)
        append_event_locked(
            root,
            "pricing_snapshot_frozen",
            pricing_version=snapshot["pricing_version"],
            pricing_source=snapshot["pricing_source"],
        )
        return current


def _task_pricing_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    return validate_task_pricing_snapshot(
        task.get("pricing_snapshot"),
        site_key=str(task.get("site_key") or ""),
        month=str(task.get("month") or ""),
    )


def _committed_task_pricing_snapshot(runtime_root: Path | str, task: dict[str, Any]) -> dict[str, Any]:
    return verify_committed_task_pricing_snapshot(
        runtime_root,
        task.get("pricing_snapshot"),
        site_key=str(task.get("site_key") or ""),
        month=str(task.get("month") or ""),
    )


def _verified_revision_task_pricing_snapshot(
    runtime_root: Path | str,
    revision: dict[str, Any],
    *,
    site_key: str,
    month: str,
    label: str,
    sync_series: bool,
) -> dict[str, Any]:
    task_id = revision.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise TaskServiceError(f"{label} source revision does not reference a task.")
    try:
        root = task_root_from_id(runtime_root, task_id)
        task = load_task(root)
    except TaskStoreError as exc:
        raise TaskServiceError(f"{label} source task is unavailable.") from exc
    if str(task.get("site_key") or "") != site_key or str(task.get("month") or "") != month:
        raise TaskServiceError(f"{label} source task does not match the requested site/month.")
    try:
        snapshot = verify_committed_task_pricing_snapshot(
            runtime_root,
            task.get("pricing_snapshot"),
            site_key=site_key,
            month=month,
        )
    except PricingError as exc:
        raise TaskServiceError(f"{label} source task pricing_snapshot is not a committed site/month snapshot.") from exc
    if sync_series:
        _sync_task_series_pricing_snapshot(runtime_root, task, snapshot)
    return snapshot


def _restart_source_task_pricing_snapshot(runtime_root: Path | str, task: dict[str, Any]) -> dict[str, Any]:
    source_task_id = task.get("restart_source_task_id")
    if not isinstance(source_task_id, str) or not source_task_id:
        raise TaskServiceError("A committed pricing snapshot is required before creating or resolving a new task revision.")
    source_revision = {
        "task_id": source_task_id,
        "revision": task.get("restart_source_revision"),
    }
    return _verified_revision_task_pricing_snapshot(
        runtime_root,
        source_revision,
        site_key=str(task.get("site_key") or ""),
        month=str(task.get("month") or ""),
        label="restart",
        sync_series=True,
    )


def _sync_task_series_pricing_snapshot(runtime_root: Path | str, task: dict[str, Any], snapshot: dict[str, Any]) -> None:
    try:
        note_task_pricing_snapshot(runtime_root, task, snapshot)
    except TaskSeriesError as exc:
        raise TaskServiceError("Failed to synchronize task pricing snapshot into task series.") from exc


def add_task_file(
    runtime_root: Path | str,
    task_id: str,
    file_path: Path | str,
    *,
    file_type: str | None = None,
    config_path: Path | str = "config/sites.json",
    site_key: str | None = None,
    month: str | None = None,
) -> dict[str, Any]:
    _assert_canonical_task_reference(runtime_root, task_id, site_key=site_key, month=month, config_path=config_path)
    root = task_root_from_id(runtime_root, task_id)
    with task_lock(root):
        task = _stage_pdf_files_locked(runtime_root, root, [Path(file_path)], file_type_hints=[file_type])
        pending_events = task.pop("_pending_events", [])
        write_required = bool(task.pop("_write_required", True))
        if write_required:
            write_task(root, task)
        for event_name, event_payload in pending_events:
            append_event_locked(root, event_name, **event_payload)
    return task


def collect_task_pdf_batch(
    runtime_root: Path | str,
    task_id: str,
    pdf_paths: list[Path | str] | tuple[Path | str, ...],
    *,
    config_path: Path | str = "config/sites.json",
    site_key: str | None = None,
    month: str | None = None,
) -> dict[str, Any]:
    _assert_canonical_task_reference(runtime_root, task_id, site_key=site_key, month=month, config_path=config_path)
    if len(pdf_paths) != len(REQUIRED_FILE_TYPES):
        raise TaskServiceError("collect_task_pdf_batch requires exactly three PDF paths.")
    root = task_root_from_id(runtime_root, task_id)
    with task_lock(root):
        task = _stage_pdf_files_locked(runtime_root, root, [Path(path) for path in pdf_paths], file_type_hints=[None] * len(pdf_paths))
        pending_events = task.pop("_pending_events", [])
        write_required = bool(task.pop("_write_required", True))
        if write_required:
            write_task(root, task)
        for event_name, event_payload in pending_events:
            append_event_locked(root, event_name, **event_payload)
    return normalize_pdf_intake_response(task)


def normalize_pdf_intake_response(task: dict[str, Any]) -> dict[str, Any]:
    intake = _normalize_pdf_intake(task.get("pdf_intake"))
    files = task.get("files") if isinstance(task.get("files"), dict) else {}
    classified_files: list[dict[str, Any]] = []
    if intake.get("batch_state") == "ready" and isinstance(files, dict) and _files_ready(files):
        for role in REQUIRED_FILE_TYPES:
            stored_file = str(files[role])
            classified_files.append(
                {
                    "file_type": role,
                    "stored_file": stored_file,
                    "original_filename": _original_filename_for_role(intake["staged_files"], stored_file),
                }
            )
    return {
        "task_id": task.get("task_id"),
        "site_key": task.get("site_key"),
        "month": task.get("month"),
        "task_status": task.get("status"),
        "batch_state": intake.get("batch_state"),
        "received_count": intake.get("received_count"),
        "required_count": intake.get("required_count"),
        "role_results": intake.get("role_results") or [],
        "missing_roles": intake.get("missing_roles") or [],
        "conflicting_roles": intake.get("conflicting_roles") or [],
        "next_allowed_action": intake.get("next_allowed_action"),
        "classified_files": classified_files,
    }


def _stage_pdf_files_locked(
    runtime_root: Path | str,
    root: Path,
    sources: list[Path],
    *,
    file_type_hints: list[str | None],
) -> dict[str, Any]:
    task = load_task(root)
    _require_task_pricing_snapshot(runtime_root, task)
    if len(sources) != len(file_type_hints):
        raise TaskServiceError("Internal PDF intake error: file_type_hints count does not match sources.")
    prepared = [_prepare_pdf_source(task, source, hint) for source, hint in zip(sources, file_type_hints, strict=True)]

    intake = _normalize_pdf_intake(task.get("pdf_intake"))
    events: list[tuple[str, dict[str, Any]]] = []
    if intake.get("batch_state") in {"need_review", "failed"}:
        cleared_count = _clear_staged_pdf_batch(root, intake)
        intake = _empty_pdf_intake()
        task["files"] = {file_type_key: None for file_type_key in REQUIRED_FILE_TYPES}
        task["status"] = "collecting_files"
        task["error"] = None
        events.append(
            (
                "pdf_batch_reset_for_reupload",
                {
                    "cleared_count": cleared_count,
                    "reason": "previous_batch_not_usable",
                },
            )
        )
    staged_files = intake["staged_files"]
    existing_hashes = {str(item.get("sha256")) for item in staged_files if isinstance(item.get("sha256"), str)}
    new_records: list[dict[str, Any]] = []
    seen_hashes = set(existing_hashes)
    for record in prepared:
        if record["sha256"] in seen_hashes:
            continue
        seen_hashes.add(str(record["sha256"]))
        new_records.append(record)
    if not new_records:
        task["pdf_intake"] = intake
        task["_pending_events"] = []
        task["_write_required"] = False
        return task
    _validate_file_intake_status(task)
    if len(staged_files) + len(new_records) > len(REQUIRED_FILE_TYPES):
        raise TaskServiceError("PDF intake batch must contain exactly three unique files.")
    if intake.get("batch_state") == "ready" and new_records:
        raise TaskServiceError("PDF intake batch already contains three unique files; start a new revision for different files.")
    existing_files = task.get("files")
    if isinstance(existing_files, dict) and _files_ready(existing_files) and not staged_files and new_records:
        raise TaskServiceError("Task already has registered PDF roles; start a new revision for replacement files.")

    if new_records:
        staged_dir = root / "inputs" / "staged"
        staged_dir.mkdir(parents=True, exist_ok=True)
        for record in new_records:
            target = _unique_input_path(staged_dir, str(record["original_filename"]))
            shutil.copy2(record["source"], target)
            staged_record = {
                "stored_file": str(target.relative_to(root)).replace("\\", "/"),
                "original_filename": record["original_filename"],
                "sha256": record["sha256"],
                "month": str(task.get("month") or ""),
                "filename_month": record["filename_month"],
                "caller_file_type_hint": record["caller_file_type_hint"],
                "received_at": utc_now_iso(),
            }
            staged_files.append(staged_record)
            events.append(
                (
                    "pdf_staged",
                    {
                        "stored_file": staged_record["stored_file"],
                        "original_filename": staged_record["original_filename"],
                        "sha256": staged_record["sha256"],
                        "caller_file_type_hint": staged_record["caller_file_type_hint"],
                        "received_count": len(staged_files),
                        "required_count": len(REQUIRED_FILE_TYPES),
                    },
                )
            )

    intake["staged_files"] = staged_files
    intake["received_count"] = len(staged_files)
    intake["required_count"] = len(REQUIRED_FILE_TYPES)
    task["files"] = {file_type_key: None for file_type_key in REQUIRED_FILE_TYPES}
    task["error"] = None
    was_ready = task.get("status") == "ready_to_run"
    if len(staged_files) == len(REQUIRED_FILE_TYPES):
        classification = _classify_pdf_intake_batch(
            root,
            staged_files,
            expected_month=str(task.get("month") or ""),
        )
        intake.update(classification)
        if classification["batch_state"] == "ready":
            task["files"] = dict(classification["files"])
            task["status"] = "ready_to_run"
            if new_records:
                events.append(("pdf_batch_classified", {"batch_state": "ready", "role_results": classification["role_results"]}))
                for role in REQUIRED_FILE_TYPES:
                    events.append(
                        (
                            "file_received",
                            {
                                "file_type": role,
                                "file_name": _original_filename_for_role(staged_files, classification["files"][role]),
                                "stored_file": classification["files"][role],
                            },
                        )
                    )
                if not was_ready:
                    events.append(("task_ready", {}))
        else:
            task["status"] = "need_review"
            task["error"] = "PDF intake batch requires role review before task execution."
            if new_records:
                events.append(
                    (
                        "pdf_batch_classification_need_review",
                        {
                            "batch_state": classification["batch_state"],
                            "role_results": classification["role_results"],
                            "missing_roles": classification["missing_roles"],
                            "conflicting_roles": classification["conflicting_roles"],
                        },
                    )
                )
    else:
        intake["batch_state"] = "staging"
        intake["role_results"] = []
        intake["missing_roles"] = list(REQUIRED_FILE_TYPES)
        intake["conflicting_roles"] = []
        intake["next_allowed_action"] = "upload_remaining_pdfs"
        task["status"] = "collecting_files"

    task["pdf_intake"] = intake
    task["_pending_events"] = events
    task["_write_required"] = True
    return task


def _prepare_pdf_source(task: dict[str, Any], source: Path, file_type_hint: str | None) -> dict[str, Any]:
    if not source.exists() or not source.is_file() or source.is_symlink():
        raise TaskServiceError(f"Input file does not exist as a regular file: {source}")
    if source.suffix.lower() not in MONTHLY_SOURCE_EXTENSIONS:
        raise TaskServiceError(f"Unsupported input file extension: {source.name}")
    if source.suffix.lower() != ".pdf":
        try:
            validate_monthly_source_file(source)
        except PdfRenderError as exc:
            raise TaskServiceError(str(exc)) from exc
    _validate_file_month(task, source.name)
    return {
        "source": source,
        "original_filename": source.name,
        "sha256": _hash_file(source),
        "caller_file_type_hint": file_type_hint,
        "filename_month": detect_month(source.name),
    }

def _normalize_pdf_intake(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _empty_pdf_intake()
    staged = value.get("staged_files")
    if not isinstance(staged, list):
        staged = []
    normalized = dict(value)
    normalized["staged_files"] = [item for item in staged if isinstance(item, dict)]
    normalized["required_count"] = len(REQUIRED_FILE_TYPES)
    normalized["received_count"] = len(normalized["staged_files"])
    normalized.setdefault("batch_state", "staging")
    normalized.setdefault("role_results", [])
    normalized.setdefault("missing_roles", list(REQUIRED_FILE_TYPES))
    normalized.setdefault("conflicting_roles", [])
    normalized.setdefault("next_allowed_action", "upload_remaining_pdfs")
    return normalized


def _empty_pdf_intake() -> dict[str, Any]:
    return {
        "batch_state": "staging",
        "required_count": len(REQUIRED_FILE_TYPES),
        "received_count": 0,
        "staged_files": [],
        "role_results": [],
        "missing_roles": list(REQUIRED_FILE_TYPES),
        "conflicting_roles": [],
        "next_allowed_action": "upload_remaining_pdfs",
    }


def _clear_staged_pdf_batch(root: Path, intake: dict[str, Any]) -> int:
    cleared = 0
    for item in intake.get("staged_files") or []:
        if not isinstance(item, dict):
            continue
        relative_path = item.get("stored_file")
        if not isinstance(relative_path, str) or not relative_path:
            continue
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise TaskServiceError("Stored PDF intake path is outside the task directory.")
        source = root / candidate
        try:
            source.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise TaskServiceError("Stored PDF intake path escapes the task directory.") from exc
        if source.is_symlink():
            raise TaskServiceError("Stored PDF intake file must not be a symbolic link.")
        if source.is_file():
            source.unlink()
            cleared += 1
    return cleared


def _find_staged_pdf(task: dict[str, Any], sha256: str) -> dict[str, Any] | None:
    intake = task.get("pdf_intake")
    if not isinstance(intake, dict):
        return None
    staged = intake.get("staged_files")
    if not isinstance(staged, list):
        return None
    for item in staged:
        if isinstance(item, dict) and item.get("sha256") == sha256:
            return item
    return None


def _classify_staged_pdf_role(root: Path, source: Path) -> tuple[Any, str | None, list[str]]:
    embedded_text = extract_pdf_text(source)
    semantic_months = detect_semantic_months(embedded_text)
    result = classify_source_pdf_by_content(source)
    if result.confidence == "high":
        return result, None, semantic_months
    try:
        ocr_dir = root / "inputs" / "ocr_intake" / source.stem
        pages = render_monthly_source_to_images(source, ocr_dir / "pages")
        raw_result = create_ocr_adapter().recognize(source, pages)
        write_ocr_raw_result(raw_result, ocr_dir / "ocr_raw.json", relative_to=root)
    except Exception:
        return result, "unavailable", semantic_months
    text = "\n".join(block.text for page in raw_result.pages for block in page.blocks)
    semantic_months = list(dict.fromkeys([*semantic_months, *detect_semantic_months(text)]))
    ocr_result = classify_pdf_text_content(text, source="ocr")
    if ocr_result.confidence == "high":
        return ocr_result, "classified", semantic_months
    return result, "unclassified", semantic_months


def _classify_pdf_intake_batch(
    root: Path,
    staged_files: list[dict[str, Any]],
    *,
    expected_month: str,
) -> dict[str, Any]:
    role_results: list[dict[str, Any]] = []
    roles: dict[str, list[str]] = {role: [] for role in REQUIRED_FILE_TYPES}
    conflicting_roles: list[str] = []
    for staged in staged_files:
        stored_file = str(staged.get("stored_file") or "")
        source = _task_owned_regular_file(root, stored_file, "staged PDF")
        result, ocr_status, semantic_months = _classify_staged_pdf_role(root, source)
        role_result = {
            "stored_file": stored_file,
            "original_filename": staged.get("original_filename"),
            "sha256": staged.get("sha256"),
            "role": result.role,
            "classification_source": result.source,
            "confidence": result.confidence,
            "filename_month": staged.get("filename_month"),
            "content_months": semantic_months,
            "resolved_month": semantic_months[0] if len(semantic_months) == 1 else expected_month,
            "month_source": "pdf_content" if len(semantic_months) == 1 else "task_context",
        }
        if ocr_status is not None:
            role_result["ocr_status"] = ocr_status
        role_results.append(role_result)
        if any(month != expected_month for month in semantic_months):
            conflicting_roles.append(f"month:{stored_file}")
        if result.role in REQUIRED_FILE_TYPES and result.confidence == "high":
            roles[str(result.role)].append(stored_file)
        elif result.role in REQUIRED_FILE_TYPES:
            conflicting_roles.append(str(result.role))

    missing_roles = [role for role in REQUIRED_FILE_TYPES if len(roles[role]) != 1]
    duplicated_roles = [role for role in REQUIRED_FILE_TYPES if len(roles[role]) > 1]
    unknown_files = [str(item.get("stored_file") or "") for item in role_results if item.get("role") not in REQUIRED_FILE_TYPES]
    conflicts = list(dict.fromkeys([*conflicting_roles, *duplicated_roles, *unknown_files]))
    if missing_roles or conflicts:
        return {
            "batch_state": "need_review",
            "role_results": role_results,
            "missing_roles": missing_roles,
            "conflicting_roles": conflicts,
            "next_allowed_action": "operator_review_pdf_roles",
            "files": {role: None for role in REQUIRED_FILE_TYPES},
        }
    return {
        "batch_state": "ready",
        "role_results": role_results,
        "missing_roles": [],
        "conflicting_roles": [],
        "next_allowed_action": "run_task",
        "files": {role: roles[role][0] for role in REQUIRED_FILE_TYPES},
    }


def _original_filename_for_role(staged_files: list[dict[str, Any]], stored_file: str) -> str:
    for staged in staged_files:
        if staged.get("stored_file") == stored_file:
            return str(staged.get("original_filename") or Path(stored_file).name)
    return Path(stored_file).name


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def cancel_task(runtime_root: Path | str, task_id: str) -> dict[str, Any]:
    root = task_root_from_id(runtime_root, task_id)
    with task_lock(root):
        task = load_task(root)
        status = str(task.get("status") or "")
        if status == CANCELLED_STATUS:
            return task
        if status not in CANCELLABLE_STATUSES:
            raise TaskServiceError(f"cancel_task does not accept task status {status!r}.")
        task["status"] = CANCELLED_STATUS
        task["error"] = None
        task["cancelled_reason"] = CANCELLED_REASON
        task["cancelled_at"] = utc_now_iso()
        write_task(root, task)
        append_event_locked(root, "task_cancelled", reason=CANCELLED_REASON)
    note_task_status(runtime_root, task)
    return task


def _unique_input_path(input_dir: Path, source_name: str) -> Path:
    suffix = Path(source_name).suffix
    stem = Path(source_name).stem or "input"
    safe_name = f"{stem}.{uuid.uuid4()}{suffix}"
    return input_dir / safe_name

def _require_task_pricing_snapshot(runtime_root: Path | str, task: dict[str, Any]) -> None:
    try:
        _committed_task_pricing_snapshot(runtime_root, task)
    except PricingError as exc:
        raise TaskServiceError("Task pricing snapshot must be frozen before files are accepted.") from exc

def _validate_file_intake_status(task: dict[str, Any]) -> None:
    status = str(task.get("status") or "")
    if status in FILE_INTAKE_STATUSES:
        return
    if status == "failed":
        raise TaskServiceError("add_task_file does not accept files when task status is failed; retry without changing input files.")
    raise TaskServiceError(f"add_task_file does not accept files when task status is {status!r}.")


def _validate_file_month(task: dict[str, Any], filename: str) -> None:
    expected_month = str(task.get("month") or "")
    actual_month = detect_month(filename)
    if actual_month is None:
        return
    if actual_month != expected_month:
        raise TaskServiceError(f"Input file month {actual_month} does not match task month {expected_month}.")




def run_task(
    runtime_root: Path | str,
    task_id: str,
    *,
    config_path: Path | str = "config/sites.json",
    site_key: str | None = None,
    month: str | None = None,
) -> TaskRunResult:
    _assert_canonical_task_reference(runtime_root, task_id, site_key=site_key, month=month, config_path=config_path)
    root = task_root_from_id(runtime_root, task_id)
    task = _start_task_run(root)
    try:
        _copy_site_templates(task, root / "inputs", config_path)
        with tempfile.TemporaryDirectory(prefix=".current_inputs.", dir=root) as selected_dir:
            _copy_current_task_inputs(task, root, Path(selected_dir), config_path=config_path)
            result = run_month(
                selected_dir,
                site_key=str(task["site_key"]),
                output_base=root / "outputs",
                config_path=config_path,
                pricing_snapshot=_committed_task_pricing_snapshot(runtime_root, task),
                expected_month=str(task["month"]),
            )
        return _finalize_run(root, result.status, result, config_path=config_path)
    except Exception as exc:
        task = update_status(
            root,
            "failed",
            error=f"{type(exc).__name__}: {exc}",
            events=[("task_failed", {"error": f"{type(exc).__name__}: {exc}"})],
        )
        note_task_status(root.parent.parent, task)
        return TaskRunResult(task=task, task_dir=root)


def retry_profile_exempt_review(
    runtime_root: Path | str,
    task_id: str,
    *,
    config_path: Path | str = "config/sites.json",
) -> TaskRunResult:
    root = task_root_from_id(runtime_root, task_id)
    task = _start_profile_exempt_review_retry(root, config_path=config_path)
    try:
        _copy_site_templates(task, root / "inputs", config_path)
        with tempfile.TemporaryDirectory(prefix=".current_inputs.", dir=root) as selected_dir:
            _copy_current_task_inputs(task, root, Path(selected_dir), config_path=config_path)
            result = run_month(
                selected_dir,
                site_key=str(task["site_key"]),
                output_base=root / "outputs",
                config_path=config_path,
                pricing_snapshot=_committed_task_pricing_snapshot(runtime_root, task),
                expected_month=str(task["month"]),
            )
        return _finalize_run(root, result.status, result, config_path=config_path)
    except Exception as exc:
        task = update_status(
            root,
            "failed",
            error=f"{type(exc).__name__}: {exc}",
            events=[("task_failed", {"error": f"{type(exc).__name__}: {exc}"})],
        )
        note_task_status(root.parent.parent, task)
        return TaskRunResult(task=task, task_dir=root)


def retry_missing_field_review(
    runtime_root: Path | str,
    task_id: str,
    *,
    config_path: Path | str = "config/sites.json",
) -> TaskRunResult:
    """Rerun a review task after mapping code gains support for its missing fields."""

    root = task_root_from_id(runtime_root, task_id)
    task = _start_missing_field_review_retry(root)
    try:
        _copy_site_templates(task, root / "inputs", config_path)
        with tempfile.TemporaryDirectory(prefix=".current_inputs.", dir=root) as selected_dir:
            _copy_current_task_inputs(task, root, Path(selected_dir), config_path=config_path)
            result = run_month(
                selected_dir,
                site_key=str(task["site_key"]),
                output_base=root / "outputs",
                config_path=config_path,
                pricing_snapshot=_committed_task_pricing_snapshot(runtime_root, task),
                expected_month=str(task["month"]),
            )
        return _finalize_run(root, result.status, result, config_path=config_path)
    except Exception as exc:
        task = update_status(
            root,
            "failed",
            error=f"{type(exc).__name__}: {exc}",
            events=[("task_failed", {"error": f"{type(exc).__name__}: {exc}", "mapping_retry": True})],
        )
        task = _write_artifact_index_or_record_error(root, task, config_path=config_path)
        note_task_status(root.parent.parent, task)
        return TaskRunResult(task=task, task_dir=root)


def retry_review_task(
    runtime_root: Path | str,
    task_id: str,
    *,
    config_path: Path | str = "config/sites.json",
) -> TaskRunResult:
    """Rerun a need_review task after deterministic mapping or validation code is repaired."""

    root = task_root_from_id(runtime_root, task_id)
    task = _start_review_task_retry(root)
    try:
        _copy_site_templates(task, root / "inputs", config_path)
        with tempfile.TemporaryDirectory(prefix=".current_inputs.", dir=root) as selected_dir:
            _copy_current_task_inputs(task, root, Path(selected_dir), config_path=config_path)
            result = run_month(
                selected_dir,
                site_key=str(task["site_key"]),
                output_base=root / "outputs",
                config_path=config_path,
                pricing_snapshot=_committed_task_pricing_snapshot(runtime_root, task),
                expected_month=str(task["month"]),
            )
        return _finalize_run(root, result.status, result, config_path=config_path)
    except Exception as exc:
        task = update_status(
            root,
            "failed",
            error=f"{type(exc).__name__}: {exc}",
            events=[("task_failed", {"error": f"{type(exc).__name__}: {exc}", "review_retry": True})],
        )
        task = _write_artifact_index_or_record_error(root, task, config_path=config_path)
        note_task_status(root.parent.parent, task)
        return TaskRunResult(task=task, task_dir=root)


def retry_failed_task(
    runtime_root: Path | str,
    task_id: str,
    *,
    config_path: Path | str = "config/sites.json",
) -> TaskRunResult:
    root = task_root_from_id(runtime_root, task_id)
    task = _start_failed_task_retry(root)
    try:
        _copy_site_templates(task, root / "inputs", config_path)
        with tempfile.TemporaryDirectory(prefix=".current_inputs.", dir=root) as selected_dir:
            _copy_current_task_inputs(task, root, Path(selected_dir), config_path=config_path)
            result = run_month(
                selected_dir,
                site_key=str(task["site_key"]),
                output_base=root / "outputs",
                config_path=config_path,
                pricing_snapshot=_committed_task_pricing_snapshot(runtime_root, task),
                expected_month=str(task["month"]),
            )
        return _finalize_run(root, result.status, result, config_path=config_path)
    except Exception as exc:
        task = update_status(
            root,
            "failed",
            error=f"{type(exc).__name__}: {exc}",
            events=[("task_failed", {"error": f"{type(exc).__name__}: {exc}", "retry": True})],
        )
        task = _write_artifact_index_or_record_error(root, task, config_path=config_path)
        note_task_status(root.parent.parent, task)
        return TaskRunResult(task=task, task_dir=root)


def _start_task_run(root: Path) -> dict[str, Any]:
    with task_lock(root):
        task = load_task(root)
        if task.get("status") != "ready_to_run":
            raise TaskServiceError(f"run_task requires task status ready_to_run, got {task.get('status')!r}.")
        files = task.get("files")
        if not isinstance(files, dict) or not _files_ready(files):
            raise TaskServiceError("Task input files are not ready.")
        task["status"] = "running"
        task["error"] = None
        write_task(root, task)
        append_event_locked(root, "run_started")
    return task


def _start_failed_task_retry(root: Path) -> dict[str, Any]:
    with task_lock(root):
        task = load_task(root)
        if task.get("status") != "failed":
            raise TaskServiceError("failed-task retry requires task status failed.")
        files = task.get("files")
        if not isinstance(files, dict) or not _files_ready(files):
            raise TaskServiceError("failed-task retry requires the original task input files.")
        _committed_task_pricing_snapshot(root.parent.parent, task)
        task["status"] = "running"
        task["error"] = None
        task["failed_task_retry"] = {
            "started_at": utc_now_iso(),
            "source_status": "failed",
            "registered_file_count": _registered_file_count(task),
        }
        write_task(root, task)
        append_event_locked(
            root,
            "failed_task_retry_started",
            registered_file_count=_registered_file_count(task),
        )
    return task


def _start_profile_exempt_review_retry(root: Path, *, config_path: Path | str) -> dict[str, Any]:
    with task_lock(root):
        task = load_task(root)
        if task.get("status") != "need_review":
            raise TaskServiceError("profile-exempt retry requires task status need_review.")
        files = task.get("files")
        if not isinstance(files, dict) or not _files_ready(files):
            raise TaskServiceError("profile-exempt retry requires the original task input files.")
        site = load_site_config(site_key=str(task.get("site_key") or ""), config_path=config_path)
        exempt_fields = tuple(site.validation_not_applicable_fields)
        if not exempt_fields:
            raise TaskServiceError("profile-exempt retry is not configured for this task profile.")
        review_payload = _load_retry_review_report(root, task)
        issues = _profile_exempt_retry_issues(review_payload, set(exempt_fields))
        snapshot_reference = _preserve_review_retry_evidence(root, task, review_payload)
        task["status"] = "running"
        task["error"] = None
        task["profile_exempt_review_retry"] = {
            "started_at": utc_now_iso(),
            "exempt_fields": list(exempt_fields),
            "preserved_review_evidence": snapshot_reference,
            "issue_count": len(issues),
        }
        write_task(root, task)
        append_event_locked(
            root,
            "profile_exempt_review_retry_started",
            exempt_fields=list(exempt_fields),
            issue_count=len(issues),
            preserved_review_evidence=snapshot_reference,
        )
    return task


def _start_missing_field_review_retry(root: Path) -> dict[str, Any]:
    with task_lock(root):
        task = load_task(root)
        if task.get("status") != "need_review":
            raise TaskServiceError("missing-field retry requires task status need_review.")
        files = task.get("files")
        if not isinstance(files, dict) or not _files_ready(files):
            raise TaskServiceError("missing-field retry requires the original task input files.")
        review_payload = _load_retry_review_report(root, task)
        issues = _missing_field_retry_issues(review_payload)
        snapshot_reference = _preserve_review_retry_evidence(root, task, review_payload)
        task["status"] = "running"
        task["error"] = None
        task["missing_field_review_retry"] = {
            "started_at": utc_now_iso(),
            "fields": sorted({str(issue["field"]) for issue in issues}),
            "preserved_review_evidence": snapshot_reference,
            "issue_count": len(issues),
        }
        write_task(root, task)
        append_event_locked(
            root,
            "missing_field_review_retry_started",
            fields=task["missing_field_review_retry"]["fields"],
            issue_count=len(issues),
            preserved_review_evidence=snapshot_reference,
        )
    return task


def _start_review_task_retry(root: Path) -> dict[str, Any]:
    with task_lock(root):
        task = load_task(root)
        if task.get("status") != "need_review":
            raise TaskServiceError("review-task retry requires task status need_review.")
        files = task.get("files")
        if not isinstance(files, dict) or not _files_ready(files):
            raise TaskServiceError("review-task retry requires the original task input files.")
        _committed_task_pricing_snapshot(root.parent.parent, task)
        review_payload = _load_retry_review_report(root, task)
        issues = review_payload.get("issues")
        if not isinstance(issues, list) or not issues:
            raise TaskServiceError("review-task retry requires non-empty review issues.")
        snapshot_reference = _preserve_review_retry_evidence(root, task, review_payload)
        task["status"] = "running"
        task["error"] = None
        task["review_task_retry"] = {
            "started_at": utc_now_iso(),
            "preserved_review_evidence": snapshot_reference,
            "issue_count": len(issues),
        }
        write_task(root, task)
        append_event_locked(
            root,
            "review_task_retry_started",
            issue_count=len(issues),
            preserved_review_evidence=snapshot_reference,
        )
    return task


def _load_retry_review_report(root: Path, task: dict[str, Any]) -> dict[str, Any]:
    report_reference = task.get("review_report")
    if not isinstance(report_reference, str) or not report_reference:
        raise TaskServiceError("profile-exempt retry requires a valid review report.")
    report_path = _task_owned_regular_file(root, report_reference, "review_report")
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TaskServiceError("profile-exempt retry requires a valid review report.") from exc
    if not isinstance(payload, dict):
        raise TaskServiceError("profile-exempt retry requires a valid review report.")
    return payload


def _profile_exempt_retry_issues(review_payload: dict[str, Any], exempt_fields: set[str]) -> list[dict[str, Any]]:
    issues = review_payload.get("issues")
    if not isinstance(issues, list) or not issues:
        raise TaskServiceError("profile-exempt retry requires non-empty review issues.")
    eligible: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            raise TaskServiceError("profile-exempt retry requires valid review issues.")
        field = issue.get("field")
        reason = issue.get("reason")
        if not isinstance(field, str) or reason != "field_missing" or field not in exempt_fields:
            raise TaskServiceError("profile-exempt retry is only allowed for exempt missing-field review issues.")
        eligible.append(issue)
    return eligible


def _missing_field_retry_issues(review_payload: dict[str, Any]) -> list[dict[str, Any]]:
    issues = review_payload.get("issues")
    if not isinstance(issues, list) or not issues:
        raise TaskServiceError("missing-field retry requires non-empty review issues.")
    eligible: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            raise TaskServiceError("missing-field retry requires valid review issues.")
        field = issue.get("field")
        if not isinstance(field, str) or not field or issue.get("reason") != "field_missing":
            raise TaskServiceError("missing-field retry only accepts field_missing review issues.")
        eligible.append(issue)
    return eligible


def _preserve_review_retry_evidence(root: Path, task: dict[str, Any], review_payload: dict[str, Any]) -> str:
    references = [task.get("review_report"), review_payload.get("recognized_data"), review_payload.get("validation_report")]
    sources: list[Path] = []
    seen: set[Path] = set()
    for index, reference in enumerate(references, start=1):
        if not isinstance(reference, str) or not reference:
            continue
        try:
            source = _task_owned_regular_file_reference(root, reference, f"review_retry_reference_{index}")
        except TaskServiceError:
            if index == 1:
                raise
            continue
        resolved = source.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        sources.append(source)

    snapshot_dir = root / "review_retry_snapshots" / _snapshot_name()
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    for source in sources:
        target_name = source.name
        target = snapshot_dir / target_name
        counter = 2
        while target.exists():
            target = snapshot_dir / f"{source.stem}_{counter}{source.suffix}"
            counter += 1
        shutil.copy2(source, target)
    manifest = {
        "source_task_id": task.get("task_id"),
        "source_status": "need_review",
        "preserved_at": utc_now_iso(),
        "source_review_report": task.get("review_report"),
        "source_review_issue_count": review_payload.get("issue_count"),
        "copied_files": [
            str(path.relative_to(snapshot_dir)).replace("\\", "/")
            for path in sorted(snapshot_dir.iterdir(), key=lambda item: item.name)
            if path.is_file()
        ],
    }
    (snapshot_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(snapshot_dir.relative_to(root)).replace("\\", "/")


def _task_owned_regular_file_reference(root: Path, reference: str, label: str) -> Path:
    candidate = Path(reference)
    if ".." in candidate.parts:
        raise TaskServiceError(f"Task {label} must be a regular file inside the task directory.")
    candidates = [candidate] if candidate.is_absolute() else [root / candidate, candidate]
    root_resolved = root.resolve()
    for source in candidates:
        try:
            source.resolve().relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        if source.is_symlink() or not source.is_file():
            continue
        return source
    raise TaskServiceError(f"Task {label} must be a regular file inside the task directory.")


def _snapshot_name() -> str:
    timestamp = utc_now_iso().replace(":", "").replace("+", "Z")
    return f"{timestamp}_{uuid.uuid4().hex[:8]}"


def confirm_task(
    runtime_root: Path | str,
    task_id: str,
    confirmed_data_path: Path | str,
    *,
    config_path: Path | str = "config/sites.json",
) -> TaskRunResult:
    root = task_root_from_id(runtime_root, task_id)
    source = Path(confirmed_data_path)
    if not source.exists() or not source.is_file():
        raise TaskServiceError(f"confirmed_data.json does not exist: {source}")
    target = root / "confirmed_data.json"
    with task_lock(root):
        task = load_task(root)
        _validate_confirmation_request(root, task, source)
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        task["status"] = "confirmed"
        task["error"] = None
        write_task(root, task)
        append_event_locked(root, "confirmed_data_received", file_name=source.name)
        append_event_locked(root, "run_started", mode="manual")
    try:
        _copy_site_templates(task, root / "inputs", config_path)
        with tempfile.TemporaryDirectory(prefix=".current_inputs.", dir=root) as selected_dir:
            _copy_current_task_inputs(task, root, Path(selected_dir), config_path=config_path)
            result = run_month(
                selected_dir,
                site_key=str(task["site_key"]),
                output_base=root / "outputs",
                config_path=config_path,
                pricing_snapshot=_committed_task_pricing_snapshot(runtime_root, task),
                expected_month=str(task["month"]),
                confirmed_data=target,
            )
        return _finalize_run(root, result.status, result, config_path=config_path)
    except Exception as exc:
        task = update_status(
            root,
            "failed",
            error=f"{type(exc).__name__}: {exc}",
            events=[("task_failed", {"error": f"{type(exc).__name__}: {exc}"})],
        )
        note_task_status(root.parent.parent, task)
        return TaskRunResult(task=task, task_dir=root)


def _validate_confirmation_request(root: Path, task: dict[str, Any], source: Path) -> None:
    if task.get("status") != "need_review":
        raise TaskServiceError(f"confirm_task requires task status need_review, got {task.get('status')!r}.")
    report_reference = task.get("review_report")
    if not isinstance(report_reference, str) or not report_reference:
        raise TaskServiceError("confirm_task requires the current task review_report path.")
    report_path = _task_owned_regular_file(root, report_reference, "review_report")
    try:
        review_payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
        confirmed_payload = json.loads(source.read_text(encoding="utf-8-sig"))
        confirmable_fields = validate_review_report(review_payload)
        validate_confirmed_data(confirmed_payload, allowed_fields=confirmable_fields)
    except (json.JSONDecodeError, ReviewContractError) as exc:
        raise TaskServiceError(f"Invalid confirmation request: {exc}") from exc


def _task_owned_regular_file(root: Path, relative_path: str, label: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise TaskServiceError(f"Task {label} path must be a relative path inside the task directory.")
    path = root / candidate
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise TaskServiceError(f"Task {label} path escapes the task directory.") from exc
    if path.is_symlink() or not path.is_file():
        raise TaskServiceError(f"Task {label} must be a regular file inside the task directory.")
    return path


def get_task(
    runtime_root: Path | str,
    task_id: str,
    *,
    site_key: str | None = None,
    month: str | None = None,
    config_path: Path | str = "config/sites.json",
) -> dict[str, Any]:
    _assert_canonical_task_reference(runtime_root, task_id, site_key=site_key, month=month, config_path=config_path)
    return load_task(task_root_from_id(runtime_root, task_id))


def list_task_outputs(
    runtime_root: Path | str,
    task_id: str,
    *,
    site_key: str | None = None,
    month: str | None = None,
    config_path: Path | str = "config/sites.json",
) -> list[str]:
    _assert_canonical_task_reference(runtime_root, task_id, site_key=site_key, month=month, config_path=config_path)
    return store_list_outputs(task_root_from_id(runtime_root, task_id))

def _copy_site_templates(task: dict[str, Any], target_dir: Path, config_path: Path | str) -> None:
    site_key = str(task.get("site_key") or "")
    if not site_key:
        raise TaskServiceError("Task does not contain site_key for template lookup.")
    site = load_site_config(site_key=site_key, config_path=config_path)
    template_dir = Path(site.template_dir)
    if not template_dir.is_dir():
        raise TaskServiceError(f"Configured template directory does not exist: {template_dir}")

    baseline_paths = _find_site_history_baseline(task, runtime_root=target_dir.parent.parent.parent, config_path=config_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    template_sources = {
        "detail_workbook": template_dir / site.detail_workbook,
        "summary_workbook": baseline_paths["summary_workbook"] if baseline_paths else template_dir / site.summary_workbook,
    }
    for manifest_key, template_name in zip(("detail_workbook", "summary_workbook"), (site.detail_workbook, site.summary_workbook), strict=True):
        source = template_sources[manifest_key]
        if not source.exists() or not source.is_file() or source.is_symlink():
            label = (
                "previous completed summary workbook"
                if manifest_key == "summary_workbook" and baseline_paths
                else "configured workbook template"
            )
            raise TaskServiceError(f"{label} does not exist: {source}")
        shutil.copy2(source, target_dir / template_name)


def _copy_current_task_inputs(task: dict[str, Any], root: Path, target_dir: Path, *, config_path: Path | str) -> None:
    site = load_site_config(site_key=str(task["site_key"]), config_path=config_path)
    files = task.get("files")
    if not isinstance(files, dict) or not _files_ready(files):
        raise TaskServiceError("Task input files are not ready.")
    target_dir.mkdir(parents=True, exist_ok=True)
    for file_type in REQUIRED_FILE_TYPES:
        source = _task_owned_regular_file(root, str(files[file_type]), file_type)
        if source.suffix.lower() not in MONTHLY_SOURCE_EXTENSIONS:
            raise TaskServiceError(f"Task registered file for {file_type} has an unsupported extension.")
        role_names = {
            "generation_statement": "发电单",
            "fee_statement": "电费结算单",
            "energy_statement": "电量结算单",
        }
        target_name = source.name
        if role_names[file_type] not in target_name:
            target_name = f"{role_names[file_type]}_{target_name}"
        shutil.copy2(source, target_dir / target_name)
    for workbook_name in (site.detail_workbook, site.summary_workbook):
        source = _task_owned_regular_file(root, f"inputs/{workbook_name}", workbook_name)
        shutil.copy2(source, target_dir / workbook_name)


def _find_site_history_baseline(
    task: dict[str, Any],
    *,
    runtime_root: Path,
    config_path: Path | str,
) -> dict[str, Path] | None:
    """Find the configured workbook baseline from the explicit site history chain."""

    site_key = str(task.get("site_key") or "")
    month = str(task.get("month") or "")
    previous_month = _previous_month(month)
    if not site_key or previous_month is None:
        return None

    site = load_site_config(site_key=site_key, config_path=config_path)
    effective_months = _effective_history_months(runtime_root, site)
    if site.summary_retroactive_full_history and effective_months:
        latest_effective = effective_months[-1]
        if month <= latest_effective:
            baseline_month = latest_effective
        elif previous_month in effective_months:
            baseline_month = previous_month
        else:
            raise TaskServiceError(
                f"Missing previous-month site history baseline for {site.site_key}: "
                f"expected {previous_month}; latest effective month is {latest_effective}."
            )
        baseline = resolve_history_workbooks(
            runtime_root,
            site_key=site.site_key,
            month=baseline_month,
            workbook_names=(site.detail_workbook, site.summary_workbook),
        )
        if baseline is not None:
            return baseline
        raise TaskServiceError(f"Configured full-history baseline cannot be resolved for {site.site_key}: {baseline_month}.")

    baseline = resolve_history_workbooks(
        runtime_root,
        site_key=site.site_key,
        month=previous_month,
        workbook_names=(site.detail_workbook, site.summary_workbook),
    )
    if baseline is not None:
        return baseline

    if not effective_months:
        return None
    if month in effective_months and not any(history_month < month for history_month in effective_months):
        return None

    earlier_months = [history_month for history_month in effective_months if history_month < month]
    latest_effective = earlier_months[-1] if earlier_months else effective_months[-1]
    raise TaskServiceError(
        f"Missing previous-month site history baseline for {site.site_key}: "
        f"expected {previous_month}; latest effective month is {latest_effective}."
    )


def _effective_history_months(runtime_root: Path, site: Any) -> list[str]:
    history_root = runtime_root / "site_history" / site.site_key
    if not history_root.is_dir() or history_root.is_symlink():
        return []
    months: list[str] = []
    for child in history_root.iterdir():
        month = child.name
        if not child.is_dir() or child.is_symlink() or _month_sort_key(month) is None:
            continue
        baseline = resolve_history_workbooks(
            runtime_root,
            site_key=site.site_key,
            month=month,
            workbook_names=(site.detail_workbook, site.summary_workbook),
        )
        if baseline is not None:
            months.append(month)
    return sorted(months, key=lambda value: _month_sort_key(value) or (0, 0))


def _month_sort_key(month: str) -> tuple[int, int] | None:
    try:
        year, month_number = (int(part) for part in month.split("-", 1))
    except (TypeError, ValueError):
        return None
    if month_number < 1 or month_number > 12:
        return None
    return year, month_number


def _previous_month(month: str) -> str | None:
    try:
        year, month_number = (int(part) for part in month.split("-", 1))
        current = date(year, month_number, 1)
    except (TypeError, ValueError):
        return None
    if current.month == 1:
        return f"{current.year - 1}-12"
    return f"{current.year}-{current.month - 1:02d}"


def _finalize_run(root: Path, run_status: str, result: Any, *, config_path: Path | str) -> TaskRunResult:
    outputs = store_list_outputs(root)
    review_report = _relative_or_none(root, result.review_report)
    task_status = "completed" if run_status == "pass" else "need_review" if run_status == "need_review" else "failed"
    event = (
        ("task_completed", {})
        if task_status == "completed"
        else ("need_review", {"review_report": review_report})
        if task_status == "need_review"
        else ("task_failed", {"error": f"Unexpected run status: {run_status}"})
    )
    task = update_status(
        root,
        task_status,
        outputs=outputs,
        review_report=review_report,
        error=None if task_status != "failed" else f"Unexpected run status: {run_status}",
        events=[event],
    )
    task = _write_artifact_index_or_record_error(root, task, config_path=config_path)
    note_task_status(root.parent.parent, task)
    if task_status == "completed":
        try:
            promotion = _promote_site_history(root, task, config_path=config_path)
            task = _record_downstream_staleness_result(root, promotion)
            note_task_status(root.parent.parent, task)
        except Exception as exc:
            task = _record_history_promotion_failure(root, exc)
            note_task_status(root.parent.parent, task)
    return TaskRunResult(task=task, task_dir=root)


def _write_artifact_index_or_record_error(root: Path, task: dict[str, Any], *, config_path: Path | str) -> dict[str, Any]:
    try:
        artifact_index = write_task_artifact_index(root, task, config_path=config_path)
    except ArtifactIndexError as exc:
        with task_lock(root):
            current = load_task(root)
            current["artifact_index_error"] = f"{type(exc).__name__}: {exc}"
            write_task(root, current)
            append_event_locked(root, "artifact_index_failed", error=current["artifact_index_error"])
            return current
    with task_lock(root):
        current = load_task(root)
        current["artifact_index"] = artifact_index
        current.pop("artifact_index_error", None)
        write_task(root, current)
        append_event_locked(root, "artifact_index_updated", artifact_index=artifact_index)
        return current


def _record_downstream_staleness_result(root: Path, promotion: dict[str, Any] | None) -> dict[str, Any]:
    payload = promotion or {"affected_months": [], "next_rebuild_month": None}
    with task_lock(root):
        task = load_task(root)
        task["downstream_staleness"] = {
            "affected_months": payload.get("affected_months") or [],
            "next_rebuild_month": payload.get("next_rebuild_month"),
            "baseline_dependency": payload.get("baseline_dependency"),
        }
        write_task(root, task)
        append_event_locked(
            root,
            "downstream_staleness_evaluated",
            affected_months=task["downstream_staleness"]["affected_months"],
            next_rebuild_month=task["downstream_staleness"].get("next_rebuild_month"),
        )
    return task


def _record_history_promotion_failure(root: Path, exc: Exception) -> dict[str, Any]:
    error = f"{type(exc).__name__}: {exc}"
    with task_lock(root):
        task = load_task(root)
        task["history_promotion_error"] = error
        write_task(root, task)
        append_event_locked(root, "history_promotion_failed", error=error)
    return task

def _promote_site_history(root: Path, task: dict[str, Any], *, config_path: Path | str) -> dict[str, Any]:
    """Promote completed workbook outputs into the immutable site/month chain."""

    site = load_site_config(site_key=str(task["site_key"]), config_path=config_path)
    month = str(task["month"])
    output_dir = root / "outputs" / site.site_name / month
    workbook_names = (site.detail_workbook, site.summary_workbook)
    source_paths = [
        output_dir / site.output_workbook_name("detail", month),
        output_dir / site.output_workbook_name("summary", month),
    ]
    if not all(path.is_file() and not path.is_symlink() for path in source_paths):
        raise TaskServiceError(f"Completed task is missing workbook outputs for site history: {task['task_id']}")

    workbook_paths = dict(zip(("detail_workbook", "summary_workbook"), source_paths, strict=True))
    baseline_month = _selected_history_baseline_month(
        task,
        runtime_root=root.parent.parent,
        config_path=config_path,
    )
    promotion = promote_effective_revision(
        root.parent.parent,
        root,
        task,
        workbook_paths,
        baseline_month=baseline_month,
    )
    if promotion.get("promoted"):
        return promotion

    history_dir = root.parent.parent / "site_history" / site.site_key / month
    history_dir.mkdir(parents=True, exist_ok=True)
    with _site_history_lock(history_dir):
        manifest = {
            "site_key": site.site_key,
            "site_name": site.site_name,
            "month": month,
            "source_task_id": str(task["task_id"]),
            "workbooks": {},
        }
        for manifest_key, source, workbook_name in zip(
            ("detail_workbook", "summary_workbook"),
            source_paths,
            workbook_names,
            strict=True,
        ):
            target = history_dir / workbook_name
            temporary_fd, temporary_name = tempfile.mkstemp(prefix=f".{workbook_name}.", suffix=".tmp", dir=history_dir)
            os.close(temporary_fd)
            temporary = Path(temporary_name)
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
            manifest["workbooks"][manifest_key] = {
                "path": workbook_name,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }

        manifest_path = history_dir / "manifest.json"
        temporary_manifest_fd, temporary_manifest_name = tempfile.mkstemp(
            prefix=".manifest.", suffix=".json", dir=history_dir
        )
        os.close(temporary_manifest_fd)
        temporary_manifest = Path(temporary_manifest_name)
        try:
            temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary_manifest, manifest_path)
        finally:
            if temporary_manifest.exists():
                temporary_manifest.unlink()
    return {"promoted": True, "affected_months": [], "next_rebuild_month": None, "baseline_dependency": None}


def _selected_history_baseline_month(
    task: dict[str, Any],
    *,
    runtime_root: Path,
    config_path: Path | str,
) -> str | None:
    site_key = str(task.get("site_key") or "")
    month = str(task.get("month") or "")
    previous_month = _previous_month(month)
    if not site_key or previous_month is None:
        return None
    site = load_site_config(site_key=site_key, config_path=config_path)
    effective_months = _effective_history_months(runtime_root, site)
    if not effective_months:
        return None
    if site.summary_retroactive_full_history:
        latest_effective = effective_months[-1]
        if month <= latest_effective:
            return latest_effective
    return previous_month if previous_month in effective_months else None


@contextmanager
def _site_history_lock(history_dir: Path) -> Iterator[None]:
    """Acquire a cross-process lock for one site/month history directory."""

    lock_path = history_dir / ".site_history.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        _lock_file(handle)
        try:
            yield
        finally:
            _unlock_file(handle)


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _registered_file_count(task: dict[str, Any]) -> int:
    files = task.get("files")
    if not isinstance(files, dict):
        return 0
    return sum(1 for value in files.values() if value)

def _files_ready(files: dict[str, Any]) -> bool:
    return all(files.get(file_type) for file_type in REQUIRED_FILE_TYPES)


def _relative_or_none(root: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(Path(path).relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)
