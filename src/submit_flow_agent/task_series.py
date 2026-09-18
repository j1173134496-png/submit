"""Shared site/month revision series for file-backed tasks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from submit_flow_agent.config import load_site_config
from submit_flow_agent.task_store import (
    append_event_locked,
    init_task,
    load_task,
    task_dir,
    task_lock,
    utc_now_iso,
    write_task,
)


WORKING_STATUSES = {"collecting_files", "ready_to_run", "running", "need_review", "confirmed"}
FRESH_STATUSES = {"collecting_files", "ready_to_run"}
TERMINAL_RETRY_STATUSES = {"failed", "cancelled"}
RESTART_BUSY_STATUSES = {"running", "confirmed"}


class TaskSeriesError(RuntimeError):
    """Raised when a site/month task series cannot be resolved."""


def resolve_task(
    runtime_root: Path | str,
    *,
    site_key: str,
    month: str,
    mode: str = "locate",
    config_path: Path | str = "config/sites.json",
) -> dict[str, Any]:
    if mode not in {"locate", "restart"}:
        raise TaskSeriesError(f"Unsupported resolve mode: {mode}")
    site = load_site_config(site_key=site_key, config_path=config_path)
    root = Path(runtime_root)
    series_root = _series_dir(root, site.site_key, month)
    with _series_lock(series_root):
        series = _load_or_init_series(root, site.site_key, month)
        _refresh_revision_statuses(root, series)
        _bootstrap_legacy_effective(
            root,
            series,
            site.site_key,
            site.site_name,
            month,
            (site.detail_workbook, site.summary_workbook),
        )
        if mode == "restart":
            latest = _latest_revision(series)
            if latest is not None and _revision_has_status(latest, RESTART_BUSY_STATUSES):
                _write_series(series_root, series)
                raise TaskSeriesError("Current site/month task is busy; retry after processing finishes.")

            if latest is not None and _restart_can_reuse_revision(latest):
                _write_series(series_root, series)
                return _resolve_payload(series, latest, "restart_empty_collecting_revision")

            task = _create_revision(
                root,
                series,
                site.site_key,
                site.site_name,
                month,
                reason="explicit_restart",
                metadata=_restart_metadata(latest),
            )
            _write_series(series_root, series)
            return _resolve_payload(series, task, "created_restart_revision")

        latest = _latest_revision(series)
        if latest is not None and _revision_has_status(latest, WORKING_STATUSES):
            _write_series(series_root, series)
            return _resolve_payload(series, latest, "working_revision")

        effective = _effective_revision(series)
        if (
            effective is not None
            and latest is not None
            and latest.get("status") == "completed"
            and effective.get("revision") == latest.get("revision")
        ):
            _write_series(series_root, series)
            return _resolve_payload(series, effective, "effective_revision")

        if latest is not None and _revision_has_status(latest, FRESH_STATUSES):
            _write_series(series_root, series)
            return _resolve_payload(series, latest, "fresh_revision")

        if latest is not None and latest.get("status") == "completed":
            _write_series(series_root, series)
            return _resolve_payload(series, latest, "latest_completed_revision")

        if latest is not None and latest.get("status") not in TERMINAL_RETRY_STATUSES and effective is not None:
            _write_series(series_root, series)
            return _resolve_payload(series, effective, "effective_revision")

        task = _create_revision(root, series, site.site_key, site.site_name, month, reason="locate_after_terminal")
        _write_series(series_root, series)
        return _resolve_payload(series, task, "created_revision")


def note_task_status(runtime_root: Path | str, task: dict[str, Any]) -> None:
    revision = task.get("revision")
    if not isinstance(revision, int):
        return
    site_key = str(task.get("site_key") or "")
    month = str(task.get("month") or "")
    if not site_key or not month:
        return
    root = Path(runtime_root)
    series_root = _series_dir(root, site_key, month)
    with _series_lock(series_root):
        series = _load_or_init_series(root, site_key, month)
        _upsert_revision(series, _revision_record(task))
        _recompute_series_pointers(series)
        _write_series(series_root, series)


def note_task_pricing_snapshot(runtime_root: Path | str, task: dict[str, Any], pricing_snapshot: dict[str, Any]) -> None:
    """Cache an already verified task pricing snapshot on its series revision."""

    revision = task.get("revision")
    if not isinstance(revision, int):
        return
    site_key = str(task.get("site_key") or "")
    month = str(task.get("month") or "")
    task_id = str(task.get("task_id") or "")
    if not site_key or not month or not task_id:
        return
    root = Path(runtime_root)
    series_root = _series_dir(root, site_key, month)
    with _series_lock(series_root):
        series = _load_or_init_series(root, site_key, month)
        record = _revision_record(task)
        record["pricing_snapshot"] = pricing_snapshot
        _upsert_revision(series, record)
        _recompute_series_pointers(series)
        _write_series(series_root, series)


def record_historical_revision(runtime_root: Path | str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Record an already-published historical migration as the effective revision."""

    site_key = str(manifest.get("site_key") or "")
    month = str(manifest.get("month") or "")
    task_id = str(manifest.get("effective_task_id") or manifest.get("source_task_id") or "")
    try:
        revision = int(manifest.get("effective_revision") or manifest.get("revision"))
    except (TypeError, ValueError) as exc:
        raise TaskSeriesError("Historical revision manifest has an invalid revision.") from exc
    if not site_key or not month or not task_id:
        raise TaskSeriesError("Historical revision manifest is missing site, month, or task identity.")
    root = Path(runtime_root)
    series_root = _series_dir(root, site_key, month)
    with _series_lock(series_root):
        series = _load_or_init_series(root, site_key, month)
        record = {
            "revision": revision,
            "task_id": task_id,
            "status": "completed",
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
            "outputs": [],
            "review_report": None,
            "registered_file_count": 0,
            "created_reason": "historical_backfill",
            "history_manifest": f"site_history/{site_key}/{month}/manifest.json",
            "baseline_dependency": manifest.get("baseline_dependency"),
            "staleness": manifest.get("staleness"),
            "migration": manifest.get("migration"),
        }
        _upsert_revision(series, record)
        series["effective_revision"] = revision
        series["effective_task_id"] = task_id
        _recompute_series_pointers(series)
        _write_series(series_root, series)
    affected_months = _mark_downstream_months_stale(root, site_key, month, revision, task_id)
    return {
        "effective_revision": revision,
        "effective_task_id": task_id,
        "affected_months": affected_months,
        "next_rebuild_month": affected_months[0]["month"] if affected_months else None,
    }


def promote_effective_revision(
    runtime_root: Path | str,
    task_root: Path,
    task: dict[str, Any],
    workbook_paths: dict[str, Path],
    *,
    baseline_month: str | None = None,
) -> dict[str, Any]:
    revision = task.get("revision")
    if not isinstance(revision, int):
        return {
            "promoted": False,
            "legacy_non_versioned": True,
            "affected_months": [],
            "next_rebuild_month": None,
            "baseline_dependency": None,
        }
    if str(task.get("status") or "") != "completed":
        raise TaskSeriesError("Only completed tasks can be promoted as effective revisions.")
    site_key = str(task["site_key"])
    month = str(task["month"])
    task_id = str(task["task_id"])
    root = Path(runtime_root)
    site_history_root = root / "site_history" / site_key
    history_dir = site_history_root / month
    revision_name = _revision_name(revision)
    revision_dir = history_dir / "revisions" / revision_name

    with _site_chain_lock(site_history_root):
        existing = _existing_effective_manifest(history_dir)
        if _manifest_matches_effective_revision(existing, task_id, revision):
            return {
                "promoted": True,
                "idempotent": True,
                "baseline_dependency": existing.get("baseline_dependency") if existing else None,
                "affected_months": [],
                "next_rebuild_month": None,
            }

        baseline_dependency = _baseline_dependency(root, site_key, month, baseline_month=baseline_month)
        with _history_lock(history_dir):
            revision_dir.mkdir(parents=True, exist_ok=True)
            manifest = {
                "site_key": site_key,
                "site_name": str(task.get("site_name") or ""),
                "month": month,
                "revision": revision,
                "revision_name": revision_name,
                "source_task_id": task_id,
                "baseline_dependency": baseline_dependency,
                "staleness": _fresh_staleness_state(),
                "workbooks": {},
            }
            for manifest_key, source in workbook_paths.items():
                if not source.is_file() or source.is_symlink():
                    raise TaskSeriesError(f"Completed task workbook is missing or unsafe: {source}")
                target = revision_dir / source.name
                _copy_atomic(source, target)
                manifest["workbooks"][manifest_key] = {
                    "path": f"revisions/{revision_name}/{source.name}",
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            _write_json_atomic(revision_dir / "manifest.json", manifest)
            effective_manifest = {
                **manifest,
                "effective_revision": revision,
                "effective_task_id": task_id,
            }
            _write_json_atomic(history_dir / "manifest.json", effective_manifest)

        series_root = _series_dir(root, site_key, month)
        with _series_lock(series_root):
            series = _load_or_init_series(root, site_key, month)
            record = _revision_record({**task, "status": "completed"})
            record["history_manifest"] = f"site_history/{site_key}/{month}/manifest.json"
            record["baseline_dependency"] = baseline_dependency
            record["staleness"] = _fresh_staleness_state()
            _upsert_revision(series, record)
            series["effective_revision"] = revision
            series["effective_task_id"] = task_id
            _recompute_series_pointers(series)
            _write_series(series_root, series)
        affected_months = _mark_downstream_months_stale(root, site_key, month, revision, task_id)
        return {
            "promoted": True,
            "idempotent": False,
            "baseline_dependency": baseline_dependency,
            "affected_months": affected_months,
            "next_rebuild_month": affected_months[0]["month"] if affected_months else None,
        }

def _existing_effective_manifest(history_dir: Path) -> dict[str, Any] | None:
    manifest_path = history_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    return _read_json(manifest_path)


def _manifest_matches_effective_revision(manifest: dict[str, Any] | None, task_id: str, revision: int) -> bool:
    if not isinstance(manifest, dict):
        return False
    if manifest.get("effective_task_id") != task_id and manifest.get("source_task_id") != task_id:
        return False
    try:
        manifest_revision = int(manifest.get("effective_revision") or manifest.get("revision"))
    except (TypeError, ValueError):
        return False
    return manifest_revision == revision

def _baseline_dependency(
    runtime_root: Path,
    site_key: str,
    month: str,
    *,
    baseline_month: str | None = None,
) -> dict[str, Any] | None:
    dependency_month = baseline_month or _previous_month(month)
    if dependency_month is None:
        return None
    manifest_path = runtime_root / "site_history" / site_key / dependency_month / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    manifest = _read_json(manifest_path)
    return {
        "month": dependency_month,
        "effective_revision": manifest.get("effective_revision") or manifest.get("revision"),
        "effective_task_id": manifest.get("effective_task_id") or manifest.get("source_task_id"),
        "history_manifest": f"site_history/{site_key}/{dependency_month}/manifest.json",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }


def _fresh_staleness_state() -> dict[str, Any]:
    return {
        "is_stale": False,
        "upstream_changed_month": None,
        "upstream_changed_revision": None,
        "upstream_changed_task_id": None,
        "marked_at": None,
        "cleared_at": utc_now_iso(),
    }


def _stale_state(changed_month: str, changed_revision: int, changed_task_id: str) -> dict[str, Any]:
    return {
        "is_stale": True,
        "reason": "upstream_effective_revision_changed",
        "upstream_changed_month": changed_month,
        "upstream_changed_revision": changed_revision,
        "upstream_changed_task_id": changed_task_id,
        "marked_at": utc_now_iso(),
        "cleared_at": None,
    }


def _mark_downstream_months_stale(
    runtime_root: Path,
    site_key: str,
    changed_month: str,
    changed_revision: int,
    changed_task_id: str,
) -> list[dict[str, Any]]:
    history_root = runtime_root / "site_history" / site_key
    if not history_root.is_dir() or history_root.is_symlink():
        return []
    affected: list[dict[str, Any]] = []
    for child in sorted(history_root.iterdir(), key=lambda item: _month_sort_key(item.name) or (0, 0)):
        month = child.name
        if _month_sort_key(month) is None or month <= changed_month:
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            continue
        manifest = _read_json(manifest_path)
        task_id = manifest.get("effective_task_id") or manifest.get("source_task_id")
        revision = manifest.get("effective_revision") or manifest.get("revision")
        if not isinstance(task_id, str) or not task_id or revision is None:
            continue
        with _history_lock(child):
            current_manifest = _read_json(manifest_path)
            current_staleness = current_manifest.get("staleness")
            if _staleness_matches_source(current_staleness, changed_month, changed_revision, changed_task_id):
                staleness = current_staleness
            else:
                staleness = _stale_state(changed_month, changed_revision, changed_task_id)
                current_manifest["staleness"] = staleness
                _write_json_atomic(manifest_path, current_manifest)
        _mark_series_month_stale(runtime_root, site_key, month, task_id, revision, staleness)
        affected.append({
            "month": month,
            "revision": revision,
            "staleness": staleness,
        })
    return affected


def _staleness_matches_source(staleness: Any, changed_month: str, changed_revision: int, changed_task_id: str) -> bool:
    if not isinstance(staleness, dict) or staleness.get("is_stale") is not True:
        return False
    return (
        staleness.get("upstream_changed_month") == changed_month
        and staleness.get("upstream_changed_revision") == changed_revision
        and staleness.get("upstream_changed_task_id") == changed_task_id
    )

def _mark_series_month_stale(
    runtime_root: Path,
    site_key: str,
    month: str,
    task_id: str,
    revision: Any,
    staleness: dict[str, Any],
) -> None:
    try:
        revision_number = int(revision)
    except (TypeError, ValueError):
        return
    series_root = _series_dir(runtime_root, site_key, month)
    with _series_lock(series_root):
        series = _load_or_init_series(runtime_root, site_key, month)
        for item in series.get("revisions") or []:
            if (
                isinstance(item, dict)
                and item.get("revision") == revision_number
                and item.get("task_id") == task_id
                and item.get("staleness") == staleness
            ):
                return
        record = {
            "revision": revision_number,
            "task_id": task_id,
            "status": "completed",
            "history_manifest": f"site_history/{site_key}/{month}/manifest.json",
            "staleness": staleness,
        }
        _upsert_revision(series, record)
        _recompute_series_pointers(series)
        _write_series(series_root, series)


def _month_sort_key(month: str) -> tuple[int, int] | None:
    try:
        year, month_number = (int(part) for part in month.split("-", 1))
    except (TypeError, ValueError):
        return None
    if month_number < 1 or month_number > 12:
        return None
    return year, month_number


def _previous_month(month: str) -> str | None:
    key = _month_sort_key(month)
    if key is None:
        return None
    year, month_number = key
    if month_number == 1:
        return f"{year - 1}-12"
    return f"{year}-{month_number - 1:02d}"

def resolve_history_workbooks(
    runtime_root: Path | str,
    *,
    site_key: str,
    month: str,
    workbook_names: tuple[str, str],
) -> dict[str, Path] | None:
    history_dir = Path(runtime_root) / "site_history" / site_key / month
    manifest_path = history_dir / "manifest.json"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        manifest = _read_json(manifest_path)
        workbooks = manifest.get("workbooks")
        if isinstance(workbooks, dict):
            paths: dict[str, Path] = {}
            for key in ("detail_workbook", "summary_workbook"):
                item = workbooks.get(key)
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    return None
                path = _safe_history_path(history_dir, item["path"])
                expected_hash = item.get("sha256")
                if isinstance(expected_hash, str) and hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                    raise TaskSeriesError(f"Site history hash mismatch: {path}")
                paths[key] = path
            return paths
    detail, summary = (history_dir / name for name in workbook_names)
    if detail.is_file() and summary.is_file() and not detail.is_symlink() and not summary.is_symlink():
        return {"detail_workbook": detail, "summary_workbook": summary}
    return None


def _existing_history_resolution(
    runtime_root: Path,
    site_key: str,
    site_name: str,
    month: str,
    workbook_names: tuple[str, str],
) -> dict[str, Any] | None:
    history_dir = runtime_root / "site_history" / site_key / month
    manifest_path = history_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    resolve_history_workbooks(runtime_root, site_key=site_key, month=month, workbook_names=workbook_names)
    manifest = _read_json(manifest_path)
    task_id = manifest.get("effective_task_id") or manifest.get("source_task_id")
    if not isinstance(task_id, str) or not task_id:
        return None
    revision = manifest.get("effective_revision") or manifest.get("revision") or 0
    return {
        "site_key": site_key,
        "site_name": site_name,
        "month": month,
        "series_id": _series_id(site_key, month),
        "resolution": "effective_history",
        "task_id": task_id,
        "revision": revision,
        "status": "completed",
        "working_revision": None,
        "effective_revision": revision,
        "latest_revision": revision,
        "active_task_id": task_id,
        "effective_task_id": task_id,
        "baseline_dependency": manifest.get("baseline_dependency"),
        "staleness": manifest.get("staleness"),
    }

def _bootstrap_legacy_effective(
    runtime_root: Path,
    series: dict[str, Any],
    site_key: str,
    site_name: str,
    month: str,
    workbook_names: tuple[str, str],
) -> None:
    if _effective_revision(series) is not None:
        return
    if any(
        isinstance(item, dict)
        and item.get("status") == "completed"
        and (item.get("history_manifest") or item.get("legacy_effective"))
        for item in series.get("revisions") or []
    ):
        _recompute_series_pointers(series)
        return

    history_dir = runtime_root / "site_history" / site_key / month
    manifest_path = history_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return
    resolve_history_workbooks(runtime_root, site_key=site_key, month=month, workbook_names=workbook_names)
    manifest = _read_json(manifest_path)
    task_id = manifest.get("effective_task_id") or manifest.get("source_task_id")
    if not isinstance(task_id, str) or not task_id:
        return
    raw_revision = manifest.get("effective_revision") or manifest.get("revision") or 0
    try:
        revision = int(raw_revision)
    except (TypeError, ValueError):
        revision = 0
    record = {
        "revision": revision,
        "task_id": task_id,
        "status": "completed",
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at") or manifest.get("completed_at"),
        "outputs": [],
        "review_report": None,
        "history_manifest": f"site_history/{site_key}/{month}/manifest.json",
        "legacy_effective": True,
        "site_name": site_name,
    }
    if isinstance(manifest.get("baseline_dependency"), dict):
        record["baseline_dependency"] = manifest["baseline_dependency"]
    if isinstance(manifest.get("staleness"), dict):
        record["staleness"] = manifest["staleness"]
    _upsert_revision(series, record)
    series["effective_revision"] = revision
    series["effective_task_id"] = task_id
    _recompute_series_pointers(series)


def _create_revision(
    runtime_root: Path,
    series: dict[str, Any],
    site_key: str,
    site_name: str,
    month: str,
    *,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    revision = int(series.get("latest_revision") or 0) + 1
    task_id = _task_id(site_key, month, revision)
    task = init_task(
        runtime_root,
        task_id=task_id,
        site_key=site_key,
        site_name=site_name,
        month=month,
        revision=revision,
        series_id=_series_id(site_key, month),
    )
    record_metadata = dict(metadata or {})
    if reason == "explicit_restart":
        task_root = task_dir(runtime_root, task_id)
        with task_lock(task_root):
            task = load_task(task_root)
            task["created_reason"] = reason
            task.update(record_metadata)
            write_task(task_root, task)
            append_event_locked(task_root, "revision_created", reason=reason, **record_metadata)
    record = _revision_record(task)
    record["created_reason"] = reason
    record.update(record_metadata)
    _upsert_revision(series, record)
    _recompute_series_pointers(series)
    return record


def _load_or_init_series(runtime_root: Path, site_key: str, month: str) -> dict[str, Any]:
    path = _series_dir(runtime_root, site_key, month) / "series.json"
    if path.is_file() and not path.is_symlink():
        return _read_json(path)
    return {
        "site_key": site_key,
        "month": month,
        "series_id": _series_id(site_key, month),
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "latest_revision": 0,
        "working_revision": None,
        "effective_revision": None,
        "active_task_id": None,
        "effective_task_id": None,
        "revisions": [],
    }


def _refresh_revision_statuses(runtime_root: Path, series: dict[str, Any]) -> None:
    for revision in series.get("revisions") or []:
        if not isinstance(revision, dict):
            continue
        task_id = revision.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        task_root = task_dir(runtime_root, task_id)
        if not (task_root / "task.json").is_file():
            continue
        task = load_task(task_root)
        revision.update(_revision_record(task))
    _recompute_series_pointers(series)


def _recompute_series_pointers(series: dict[str, Any]) -> None:
    revisions = [item for item in series.get("revisions") or [] if isinstance(item, dict)]
    revisions.sort(key=lambda item: int(item.get("revision") or 0))
    series["revisions"] = revisions
    latest = revisions[-1] if revisions else None
    series["latest_revision"] = int(latest.get("revision")) if latest else 0
    latest_is_working = latest is not None and _revision_has_status(latest, WORKING_STATUSES)
    series["working_revision"] = latest.get("revision") if latest_is_working and latest is not None else None
    series["active_task_id"] = latest.get("task_id") if latest else None
    completed = [
        item
        for item in revisions
        if item.get("status") == "completed" and (item.get("history_manifest") or item.get("legacy_effective"))
    ]
    current_effective = _effective_revision(series)
    if current_effective is not None and current_effective not in completed:
        current_effective = None
        series["effective_revision"] = None
        series["effective_task_id"] = None
    if current_effective is None and completed:
        effective = completed[-1]
        series["effective_revision"] = effective.get("revision")
        series["effective_task_id"] = effective.get("task_id")
    elif current_effective is not None:
        series["effective_task_id"] = current_effective.get("task_id")
    series["updated_at"] = utc_now_iso()


def _revision_record(task: dict[str, Any]) -> dict[str, Any]:
    record = {
        "revision": int(task["revision"]),
        "task_id": str(task["task_id"]),
        "status": str(task.get("status") or ""),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "outputs": task.get("outputs") if isinstance(task.get("outputs"), list) else [],
        "review_report": task.get("review_report"),
        "registered_file_count": _registered_file_count(task),
    }
    if isinstance(task.get("pricing_snapshot"), dict):
        record["pricing_snapshot"] = task["pricing_snapshot"]
    if isinstance(task.get("created_reason"), str) and task["created_reason"]:
        record["created_reason"] = task["created_reason"]
    for key in ("restart_source_revision", "restart_source_status"):
        if key in task:
            record[key] = task[key]
    return record


def _upsert_revision(series: dict[str, Any], record: dict[str, Any]) -> None:
    revisions = [item for item in series.get("revisions") or [] if isinstance(item, dict)]
    for index, item in enumerate(revisions):
        if item.get("revision") == record.get("revision"):
            revisions[index] = {**item, **record}
            series["revisions"] = revisions
            return
    revisions.append(record)
    series["revisions"] = revisions


def _latest_revision(series: dict[str, Any]) -> dict[str, Any] | None:
    revisions = [item for item in series.get("revisions") or [] if isinstance(item, dict)]
    if not revisions:
        return None
    return sorted(revisions, key=lambda item: int(item.get("revision") or 0))[-1]


def _effective_revision(series: dict[str, Any]) -> dict[str, Any] | None:
    revision_number = series.get("effective_revision")
    for revision in series.get("revisions") or []:
        if isinstance(revision, dict) and revision.get("revision") == revision_number:
            return revision
    return None


def _latest_matching_revision(series: dict[str, Any], statuses: set[str]) -> dict[str, Any] | None:
    latest = _latest_revision(series)
    if latest is None or not _revision_has_status(latest, statuses):
        return None
    return latest


def _revision_has_status(revision: dict[str, Any], statuses: set[str]) -> bool:
    return str(revision.get("status") or "") in statuses


def _restart_can_reuse_revision(revision: dict[str, Any]) -> bool:
    return str(revision.get("status") or "") == "collecting_files" and _revision_registered_file_count(revision) == 0


def _restart_metadata(source_revision: dict[str, Any] | None) -> dict[str, Any]:
    if source_revision is None:
        return {}
    return {
        "restart_source_revision": source_revision.get("revision"),
        "restart_source_status": str(source_revision.get("status") or ""),
        "restart_source_task_id": source_revision.get("task_id"),
    }


def _revision_registered_file_count(revision: dict[str, Any]) -> int:
    count = revision.get("registered_file_count")
    if isinstance(count, int):
        return count
    return 0


def _registered_file_count(task: dict[str, Any]) -> int:
    files = task.get("files")
    if not isinstance(files, dict):
        return 0
    return sum(1 for value in files.values() if value)


def _resolve_payload(series: dict[str, Any], revision: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "site_key": series["site_key"],
        "month": series["month"],
        "series_id": series["series_id"],
        "resolution": reason,
        "task_id": revision["task_id"],
        "revision": revision["revision"],
        "status": revision["status"],
        "working_revision": series.get("working_revision"),
        "effective_revision": series.get("effective_revision"),
        "latest_revision": series.get("latest_revision"),
        "active_task_id": series.get("active_task_id"),
        "effective_task_id": series.get("effective_task_id"),
        "baseline_dependency": revision.get("baseline_dependency"),
        "staleness": revision.get("staleness"),
    }


def _series_dir(runtime_root: Path, site_key: str, month: str) -> Path:
    return runtime_root / "site_series" / _safe_segment(site_key, "site_key") / _safe_month(month)


def _series_id(site_key: str, month: str) -> str:
    return f"{site_key}:{month}"


def _task_id(site_key: str, month: str, revision: int) -> str:
    compact_month = month.replace("-", "_")
    suffix = hashlib.sha256(f"{site_key}:{month}:{revision}".encode("utf-8")).hexdigest()[:8]
    return f"pv_{site_key}_{compact_month}_r{revision:04d}_{suffix}"


def _revision_name(revision: int) -> str:
    return f"r{revision:04d}"


def _safe_segment(value: str, label: str) -> str:
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1 or not str(value).strip():
        raise TaskSeriesError(f"Unsafe {label}: {value}")
    return str(value)


def _safe_month(month: str) -> str:
    text = _safe_segment(month, "month")
    if len(text) != 7 or text[4] != "-" or not text[:4].isdigit() or not text[5:].isdigit():
        raise TaskSeriesError(f"Invalid month: {month}")
    month_number = int(text[5:])
    if month_number < 1 or month_number > 12:
        raise TaskSeriesError(f"Invalid month: {month}")
    return text


def _safe_history_path(history_dir: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise TaskSeriesError(f"Unsafe site history manifest path: {relative_path}")
    path = history_dir / candidate
    try:
        path.resolve().relative_to(history_dir.resolve())
    except ValueError as exc:
        raise TaskSeriesError(f"Site history manifest path escapes history directory: {relative_path}") from exc
    if path.is_symlink() or not path.is_file():
        raise TaskSeriesError(f"Site history workbook is missing or unsafe: {relative_path}")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TaskSeriesError(f"JSON file is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise TaskSeriesError(f"JSON file must contain an object: {path}")
    return payload


def _write_series(series_root: Path, series: dict[str, Any]) -> None:
    series_root.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(series_root / "series.json", series)


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


def _copy_atomic(source: Path, target: Path) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _site_chain_lock(site_history_root: Path) -> Iterator[None]:
    site_history_root.mkdir(parents=True, exist_ok=True)
    with (site_history_root / ".site_chain.lock").open("a+b") as handle:
        _lock_handle(handle)
        try:
            yield
        finally:
            _unlock_handle(handle)

@contextmanager
def _series_lock(series_root: Path) -> Iterator[None]:
    series_root.mkdir(parents=True, exist_ok=True)
    with (series_root / ".series.lock").open("a+b") as handle:
        _lock_handle(handle)
        try:
            yield
        finally:
            _unlock_handle(handle)


@contextmanager
def _history_lock(history_dir: Path) -> Iterator[None]:
    history_dir.mkdir(parents=True, exist_ok=True)
    with (history_dir / ".site_history.lock").open("a+b") as handle:
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

