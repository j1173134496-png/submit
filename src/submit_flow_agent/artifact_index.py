"""Task-owned web-ready artifact index generation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from submit_flow_agent.config import load_site_config
from submit_flow_agent.task_store import utc_now_iso


class ArtifactIndexError(RuntimeError):
    """Raised when a task artifact index cannot be written safely."""


def write_task_artifact_index(
    task_root: Path | str,
    task: dict[str, Any],
    *,
    config_path: Path | str = "config/sites.json",
) -> str:
    root = Path(task_root)
    site = load_site_config(site_key=str(task.get("site_key") or ""), config_path=config_path)
    artifacts = _collect_artifacts(root, task, site=site)
    site_revision_index = _site_revision_index_reference(root, task)
    payload = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "site_key": task.get("site_key"),
        "site_name": task.get("site_name"),
        "month": task.get("month"),
        "series_id": task.get("series_id"),
        "revision": task.get("revision"),
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "index_paths": {
            "task": "artifact_index.json",
            "site_revision": site_revision_index,
        },
        "site_contract": _site_contract(site),
        "pricing_snapshot": _pricing_snapshot_metadata(task.get("pricing_snapshot")),
        "artifacts": artifacts,
    }
    target = root / "artifact_index.json"
    _write_json_atomic(target, payload)
    _write_site_revision_index(root, task, payload)
    return "artifact_index.json"


def _collect_artifacts(root: Path, task: dict[str, Any], *, site: Any) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    files = task.get("files")
    if isinstance(files, dict):
        for file_type, reference in sorted(files.items()):
            record = _artifact_record(root, reference, kind="source_pdf", role=str(file_type), status="registered")
            if record is not None:
                artifacts.append(record)
    for reference in task.get("outputs") or []:
        if not isinstance(reference, str):
            continue
        kind = _output_kind(reference, site=site, month=str(task.get("month") or ""))
        try:
            record = _artifact_record(
                root,
                reference,
                kind=kind,
                role=kind,
                status=str(task.get("status") or "unknown"),
            )
        except ArtifactIndexError:
            if kind in {"detail_workbook", "summary_workbook", "workbook"}:
                raise
            _assert_missing_task_output(root, reference)
            continue
        if record is not None:
            artifacts.append(record)
    _validate_workbook_artifacts(task, artifacts)
    review_report = task.get("review_report")
    record = _artifact_record(root, review_report, kind="review_report", role="review_report", status="need_review")
    if record is not None:
        artifacts.append(record)
    return artifacts


def _site_contract(site: Any) -> dict[str, Any]:
    return {
        "site_key": site.site_key,
        "template_profile_key": site.template_profile_key,
        "detail_profile_key": site.detail_profile_key,
        "summary_profile_key": site.summary_profile_key,
        "meter_topology_profile": site.meter_topology_profile,
        "detail_workbook": site.detail_workbook,
        "summary_workbook": site.summary_workbook,
    }


def _artifact_record(root: Path, reference: Any, *, kind: str, role: str, status: str) -> dict[str, Any] | None:
    if not isinstance(reference, str) or not reference:
        return None
    path = _task_owned_regular_file(root, reference)
    return {
        "kind": kind,
        "role": role,
        "file_name": path.name,
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "sha256": _sha256(path),
        "status": status,
        "updated_at": utc_now_iso(),
    }


def _output_kind(reference: str, *, site: Any, month: str) -> str:
    path = Path(reference)
    name = path.name.casefold()
    if name.endswith(".xlsx"):
        role = _configured_workbook_role(path.name, site=site, month=month)
        if role == "detail":
            return "detail_workbook"
        if role == "summary":
            return "summary_workbook"
        return "workbook"
    if name == "recognized_data.json":
        return "recognized_data"
    if name == "validation_report.json":
        return "validation_report"
    if name == "process_log.txt":
        return "process_log"
    if "raw_ocr" in path.parts and name.endswith(".json"):
        return "ocr_raw"
    return "output"


def _configured_workbook_role(filename: str, *, site: Any, month: str) -> str | None:
    folded = filename.casefold()
    detail_names = _configured_workbook_names(site.detail_workbook, month)
    summary_names = _configured_workbook_names(site.summary_workbook, month)
    if month:
        detail_names.add(site.output_workbook_name("detail", month).casefold())
        summary_names.add(site.output_workbook_name("summary", month).casefold())
    if folded in detail_names:
        return "detail"
    if folded in summary_names:
        return "summary"
    return None


def _configured_workbook_names(workbook_name: str, month: str) -> set[str]:
    path = Path(workbook_name)
    names = {path.name.casefold()}
    if month:
        names.add(f"{path.stem}_{month}{path.suffix}".casefold())
    return names


def _validate_workbook_artifacts(task: dict[str, Any], artifacts: list[dict[str, Any]]) -> None:
    if task.get("status") != "completed":
        return
    detail_count = sum(1 for artifact in artifacts if artifact.get("kind") == "detail_workbook")
    summary_count = sum(1 for artifact in artifacts if artifact.get("kind") == "summary_workbook")
    if detail_count != 1 or summary_count != 1:
        raise ArtifactIndexError(
            "Completed task artifact index requires exactly one configured detail workbook and one configured summary workbook."
        )


def _site_revision_index_reference(root: Path, task: dict[str, Any]) -> str | None:
    location = _site_revision_index_path(root, task)
    if location is None:
        return None
    runtime_root, target = location
    return str(target.relative_to(runtime_root)).replace("\\", "/")


def _write_site_revision_index(root: Path, task: dict[str, Any], payload: dict[str, Any]) -> None:
    location = _site_revision_index_path(root, task)
    if location is None:
        return
    _, target = location
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(target, payload)


def _site_revision_index_path(root: Path, task: dict[str, Any]) -> tuple[Path, Path] | None:
    if root.parent.name != "tasks":
        return None
    revision = _safe_revision(task.get("revision"))
    if revision is None:
        return None
    site_key = _safe_segment(str(task.get("site_key") or ""), "site_key")
    month = _safe_month(str(task.get("month") or ""))
    runtime_root = root.parent.parent
    target = runtime_root / "artifact_index" / site_key / month / f"{revision}.json"
    return runtime_root, target


def _safe_segment(value: str, label: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not value or any(character not in allowed for character in value):
        raise ArtifactIndexError(f"Unsafe {label} for artifact index path.")
    return value


def _safe_month(value: str) -> str:
    parts = value.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2 or not all(part.isdigit() for part in parts):
        raise ArtifactIndexError("Unsafe month for artifact index path.")
    return value


def _safe_revision(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        revision = int(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactIndexError("Unsafe revision for artifact index path.") from exc
    if revision <= 0:
        raise ArtifactIndexError("Unsafe revision for artifact index path.")
    return f"r{revision:04d}"


def _pricing_snapshot_metadata(snapshot: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    return {
        "site_key": snapshot.get("site_key"),
        "month": snapshot.get("month"),
        "effective_month": snapshot.get("effective_month"),
        "pricing_version": snapshot.get("pricing_version"),
        "pricing_source": snapshot.get("pricing_source"),
        "confirmed_at": snapshot.get("confirmed_at"),
        "proposal_id": snapshot.get("proposal_id"),
        "proposal_hash": snapshot.get("proposal_hash"),
        "pricing_hash": snapshot.get("pricing_hash"),
        "discount_rate": snapshot.get("discount_rate"),
        "effective_unit_price": snapshot.get("effective_unit_price"),
    }


def _task_owned_regular_file(root: Path, reference: str) -> Path:
    candidate = Path(reference)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ArtifactIndexError(f"Artifact path escapes task root: {reference}")
    path = root / candidate
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ArtifactIndexError(f"Artifact path escapes task root: {reference}") from exc
    if path.is_symlink() or not path.is_file():
        raise ArtifactIndexError(f"Artifact is missing or unsafe: {reference}")
    return path


def _assert_missing_task_output(root: Path, reference: str) -> None:
    candidate = Path(reference)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ArtifactIndexError(f"Artifact path escapes task root: {reference}")
    path = root / candidate
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise ArtifactIndexError(f"Artifact path escapes task root: {reference}") from exc
    if path.is_symlink() or path.exists():
        raise ArtifactIndexError(f"Artifact is unsafe: {reference}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
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



