"""Structured Gateway/Worker contract helpers for MVP-013."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ALLOWED_ACTIONS = {"run_task", "retry_failed_task", "confirm_task", "show_task", "list_outputs"}
ALLOWED_RESULT_STATUSES = {"completed", "need_review", "failed", "collecting_files", "ready_to_run", "running"}
FORBIDDEN_TASK_FIELDS = {
    "command",
    "shell",
    "cmd",
    "prompt",
    "user_text",
    "natural_language_instruction",
    "python",
    "sql",
    "script",
}
TASK_SCHEMA_PATH = Path("spec/contracts/worker_task.schema.json")
RESULT_SCHEMA_PATH = Path("spec/contracts/worker_result.schema.json")


class WorkerContractError(RuntimeError):
    """Raised when a Worker contract payload is invalid."""


def load_json(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise WorkerContractError(f"JSON is invalid: {source}") from exc
    if not isinstance(payload, dict):
        raise WorkerContractError(f"JSON must contain an object: {source}")
    return payload


def load_worker_task_schema(path: Path | str = TASK_SCHEMA_PATH) -> dict[str, Any]:
    return load_json(path)


def load_worker_result_schema(path: Path | str = RESULT_SCHEMA_PATH) -> dict[str, Any]:
    return load_json(path)


def validate_worker_task(payload: dict[str, Any]) -> None:
    allowed_fields = {
        "action",
        "task_id",
        "runtime_root",
        "site_key",
        "month",
        "mode",
        "confirmed_data",
        "request_id",
    }
    _reject_unknown_fields(payload, allowed_fields, "worker_task")
    forbidden = sorted(field for field in payload if field in FORBIDDEN_TASK_FIELDS)
    if forbidden:
        raise WorkerContractError(f"worker_task contains forbidden fields: {', '.join(forbidden)}")
    action = _required_str(payload, "action")
    if action not in ALLOWED_ACTIONS:
        raise WorkerContractError(f"Unsupported worker action: {action}")
    _required_str(payload, "task_id")
    _required_str(payload, "runtime_root")

    mode = payload.get("mode")
    if action == "confirm_task":
        _required_str(payload, "confirmed_data")
        if mode not in (None, "manual"):
            raise WorkerContractError("confirm_task mode must be manual.")
    elif action in {"run_task", "retry_failed_task"}:
        if "confirmed_data" in payload:
            raise WorkerContractError(f"{action} must not contain confirmed_data.")
        if mode not in (None, "strict"):
            raise WorkerContractError(f"{action} mode must be strict.")
    else:
        if "confirmed_data" in payload:
            raise WorkerContractError(f"{action} must not contain confirmed_data.")
        if mode not in (None, "query"):
            raise WorkerContractError(f"{action} mode must be query.")


def validate_worker_result(payload: dict[str, Any]) -> None:
    allowed_fields = {
        "task_id",
        "status",
        "task_status",
        "summary",
        "outputs",
        "review_report",
        "error",
        "task_json",
        "request_id",
    }
    _reject_unknown_fields(payload, allowed_fields, "worker_result")
    _required_str(payload, "task_id")
    status = _required_str(payload, "status")
    task_status = _required_str(payload, "task_status")
    _required_str(payload, "summary")
    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or not all(isinstance(item, str) for item in outputs):
        raise WorkerContractError("worker_result outputs must be a string array.")
    if status not in ALLOWED_RESULT_STATUSES:
        raise WorkerContractError(f"Unsupported worker result status: {status}")
    if status in {"completed", "need_review", "failed"} and task_status != status:
        raise WorkerContractError("terminal worker status must match task_status.")
    if status == "completed":
        if payload.get("review_report") is not None or payload.get("error") is not None:
            raise WorkerContractError("completed result must not contain review_report or error.")
    elif status == "need_review":
        if not isinstance(payload.get("review_report"), str) or not payload["review_report"]:
            raise WorkerContractError("need_review result must contain review_report.")
        if payload.get("error") is not None:
            raise WorkerContractError("need_review result must not contain error.")
    elif status == "failed":
        if payload.get("review_report") is not None:
            raise WorkerContractError("failed result must not contain review_report.")
        if not isinstance(payload.get("error"), str) or not payload["error"]:
            raise WorkerContractError("failed result must contain error.")


def worker_result_from_task(task: dict[str, Any], *, summary: str | None = None, request_id: str | None = None) -> dict[str, Any]:
    task_id = _required_str(task, "task_id")
    task_status = str(task.get("status") or "failed")
    status = _worker_status(task_status)
    result = {
        "task_id": task_id,
        "status": status,
        "task_status": task_status,
        "summary": summary or _summary_for_status(status),
        "outputs": task.get("outputs") if isinstance(task.get("outputs"), list) else [],
        "review_report": task.get("review_report") if status == "need_review" else None,
        "error": task.get("error") if status == "failed" else None,
        "task_json": "task.json",
        "request_id": request_id,
    }
    validate_worker_result(result)
    return result


def _worker_status(task_status: str) -> str:
    if task_status in ALLOWED_RESULT_STATUSES:
        return task_status
    if task_status == "confirmed":
        return "running"
    return "failed"


def _summary_for_status(status: str) -> str:
    if status == "completed":
        return "校验通过，已生成任务输出文件。"
    if status == "need_review":
        return "发现低置信度、字段缺失或业务校验问题，需要人工复核。"
    if status == "failed":
        return "任务运行失败。"
    if status == "ready_to_run":
        return "任务文件已收齐，可以派发 run_task。"
    if status == "collecting_files":
        return "任务仍在收集输入文件。"
    return "任务处理中。"


def _reject_unknown_fields(payload: dict[str, Any], allowed_fields: set[str], label: str) -> None:
    unknown = sorted(set(payload) - allowed_fields)
    if unknown:
        raise WorkerContractError(f"{label} contains unknown fields: {', '.join(unknown)}")


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkerContractError(f"Missing required string field: {key}")
    return value
