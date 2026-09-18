"""File-backed task store for MVP-012."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from submit_flow_agent.file_scanner import REQUIRED_FILE_TYPES


TASK_STATUSES = {
    "created",
    "collecting_files",
    "ready_to_run",
    "running",
    "completed",
    "need_review",
    "confirmed",
    "cancelled",
    "failed",
}


class TaskStoreError(RuntimeError):
    """Raised when a file-backed task cannot be read or written."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def task_dir(runtime_root: Path | str, task_id: str) -> Path:
    return Path(runtime_root) / "tasks" / task_id


def init_task(
    runtime_root: Path | str,
    *,
    task_id: str,
    site_key: str,
    site_name: str,
    month: str,
    revision: int | None = None,
    series_id: str | None = None,
) -> dict[str, Any]:
    root = task_dir(runtime_root, task_id)
    (root / "inputs").mkdir(parents=True, exist_ok=True)
    (root / "outputs").mkdir(parents=True, exist_ok=True)
    (root / "audit").mkdir(parents=True, exist_ok=True)
    now = utc_now_iso()
    task = {
        "task_id": task_id,
        "site_key": site_key,
        "site_name": site_name,
        "month": month,
        "status": "collecting_files",
        "required_files": list(REQUIRED_FILE_TYPES),
        "files": {file_type: None for file_type in REQUIRED_FILE_TYPES},
        "outputs": [],
        "review_report": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    if revision is not None:
        task["revision"] = revision
    if series_id is not None:
        task["series_id"] = series_id
    event_payload: dict[str, Any] = {"task_id": task_id}
    if revision is not None:
        event_payload["revision"] = revision
    if series_id is not None:
        event_payload["series_id"] = series_id
    with task_lock(root):
        write_task(root, task)
        append_event_locked(root, "task_created", **event_payload)
    return task


def load_task(task_root: Path | str) -> dict[str, Any]:
    path = Path(task_root) / "task.json"
    if not path.exists():
        raise TaskStoreError(f"task.json does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TaskStoreError(f"task.json is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise TaskStoreError("task.json must contain a JSON object.")
    return payload


def write_task(task_root: Path | str, task: dict[str, Any]) -> Path:
    root = Path(task_root)
    root.mkdir(parents=True, exist_ok=True)
    task["updated_at"] = utc_now_iso()
    target = root / "task.json"
    serialized = json.dumps(task, ensure_ascii=False, indent=2)
    fd, temporary_name = tempfile.mkstemp(prefix=".task.", suffix=".json", dir=root)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return target


def update_status(
    task_root: Path | str,
    status: str,
    *,
    events: Sequence[tuple[str, dict[str, Any]]] = (),
    **updates: Any,
) -> dict[str, Any]:
    if status not in TASK_STATUSES:
        raise TaskStoreError(f"Invalid task status: {status}")
    with task_lock(task_root):
        task = load_task(task_root)
        task["status"] = status
        task.update(updates)
        write_task(task_root, task)
        for event, payload in events:
            append_event_locked(task_root, event, **payload)
    return task


def append_event(task_root: Path | str, event: str, **payload: Any) -> Path:
    with task_lock(task_root):
        return append_event_locked(task_root, event, **payload)


def append_event_locked(task_root: Path | str, event: str, **payload: Any) -> Path:
    """Append one event while the caller holds this task's lock."""
    root = Path(task_root)
    audit_dir = root / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    record = {"ts": utc_now_iso(), "event": event, **payload}
    target = audit_dir / "events.jsonl"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return target


@contextmanager
def task_lock(task_root: Path | str) -> Iterator[None]:
    """Acquire an advisory, cross-process lock scoped to one task directory."""
    root = Path(task_root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".task.lock"
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


def list_outputs(task_root: Path | str) -> list[str]:
    root = Path(task_root)
    outputs_dir = root / "outputs"
    if not outputs_dir.exists():
        return []
    return [
        str(path.relative_to(root)).replace("\\", "/")
        for path in sorted(outputs_dir.rglob("*"), key=lambda item: str(item))
        if path.is_file()
    ]


def task_root_from_id(runtime_root: Path | str, task_id: str) -> Path:
    root = task_dir(runtime_root, task_id)
    if not root.exists():
        raise TaskStoreError(f"Task does not exist: {task_id}")
    return root
