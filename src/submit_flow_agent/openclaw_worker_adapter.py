"""OpenClaw Tool / Submit Flow Worker Adapter for MVP-017."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from submit_flow_agent.worker_contract import (
    WorkerContractError,
    load_json,
    validate_worker_result,
    validate_worker_task,
    worker_result_from_task,
)


DEFAULT_PROJECT_ROOT = Path("/mnt/k/Workers/Submit_Flow_Agent")
DEFAULT_PYTHON = sys.executable


class AdapterError(RuntimeError):
    """Raised when the adapter cannot call the controlled worker CLI."""


def handle_worker_task(
    payload: dict[str, Any],
    *,
    project_root: Path | str = DEFAULT_PROJECT_ROOT,
    python_executable: str = DEFAULT_PYTHON,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Validate a worker_task payload, call the CLI, and return worker_result."""

    request_id = payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
    try:
        validate_worker_task(payload)
        result = _dispatch(payload, Path(project_root), python_executable, timeout_seconds)
    except Exception as exc:
        result = _failed_result(payload, exc, request_id=request_id)
    validate_worker_result(result)
    return result


def handle_worker_task_file(
    path: Path | str,
    *,
    project_root: Path | str = DEFAULT_PROJECT_ROOT,
    python_executable: str = DEFAULT_PYTHON,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    payload = load_json(path)
    return handle_worker_task(
        payload,
        project_root=project_root,
        python_executable=python_executable,
        timeout_seconds=timeout_seconds,
    )


def _dispatch(payload: dict[str, Any], project_root: Path, python_executable: str, timeout_seconds: int) -> dict[str, Any]:
    action = str(payload["action"])
    task_id = str(payload["task_id"])
    runtime_root = str(payload["runtime_root"])

    if action == "run_task":
        cli_payload = _run_cli(project_root, python_executable, ["run-task", task_id, "--runtime-root", runtime_root], timeout_seconds)
        task = _task_from_run_payload(cli_payload)
        return _result_from_task(task, payload)

    if action == "retry_failed_task":
        cli_payload = _run_cli(
            project_root,
            python_executable,
            ["retry-failed-task", task_id, "--runtime-root", runtime_root],
            timeout_seconds,
        )
        task = _task_from_run_payload(cli_payload)
        return _result_from_task(task, payload)

    if action == "confirm_task":
        confirmed_data = str(payload["confirmed_data"])
        cli_payload = _run_cli(
            project_root,
            python_executable,
            ["confirm-task", task_id, confirmed_data, "--runtime-root", runtime_root],
            timeout_seconds,
        )
        task = _task_from_run_payload(cli_payload)
        return _result_from_task(task, payload)

    if action == "show_task":
        task = _run_cli(project_root, python_executable, ["show-task", task_id, "--runtime-root", runtime_root], timeout_seconds)
        return _result_from_task(task, payload, summary="任务查询完成。")

    if action == "list_outputs":
        task = _run_cli(project_root, python_executable, ["show-task", task_id, "--runtime-root", runtime_root], timeout_seconds)
        outputs_payload = _run_cli(project_root, python_executable, ["list-outputs", task_id, "--runtime-root", runtime_root], timeout_seconds)
        task["outputs"] = outputs_payload.get("outputs") if isinstance(outputs_payload.get("outputs"), list) else []
        return _result_from_task(task, payload, summary="任务输出列表查询完成。")

    raise WorkerContractError(f"Unsupported worker action: {action}")


def _run_cli(project_root: Path, python_executable: str, cli_args: Sequence[str], timeout_seconds: int) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    env["PYTHONIOENCODING"] = "utf-8"
    command = [python_executable, "-m", "submit_flow_agent.cli", *cli_args]
    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            env=env,
            shell=False,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterError(f"CLI timeout after {timeout_seconds}s: {' '.join(command)}") from exc

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        raise AdapterError(f"CLI exited {completed.returncode}: {message}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AdapterError("CLI returned non-JSON output.") from exc
    if not isinstance(payload, dict):
        raise AdapterError("CLI JSON output must be an object.")
    return payload


def _task_from_run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    task = dict(payload)
    task.pop("task_dir", None)
    return task


def _result_from_task(task: dict[str, Any], request: dict[str, Any], *, summary: str | None = None) -> dict[str, Any]:
    result = worker_result_from_task(task, summary=summary, request_id=request.get("request_id"))
    task_id = str(request["task_id"])
    runtime_root = str(request["runtime_root"]).rstrip("/")
    result["task_json"] = f"{runtime_root}/tasks/{task_id}/task.json"
    validate_worker_result(result)
    return result


def _failed_result(payload: dict[str, Any], exc: Exception, *, request_id: str | None) -> dict[str, Any]:
    task_id = payload.get("task_id") if isinstance(payload.get("task_id"), str) and payload.get("task_id") else "unknown"
    result = {
        "task_id": task_id,
        "status": "failed",
        "task_status": "failed",
        "summary": "Worker Adapter 调用失败。",
        "outputs": [],
        "review_report": None,
        "error": f"{type(exc).__name__}: {exc}",
        "task_json": None,
        "request_id": request_id,
    }
    validate_worker_result(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="submit-flow-worker-adapter")
    parser.add_argument("worker_task", type=Path, nargs="?", help="Path to worker_task JSON. Reads stdin when omitted.")
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--python", default=DEFAULT_PYTHON)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args(argv)

    if args.worker_task is None:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise SystemExit("worker_task stdin must contain a JSON object.")
        result = handle_worker_task(
            payload,
            project_root=args.project_root,
            python_executable=args.python,
            timeout_seconds=args.timeout_seconds,
        )
    else:
        result = handle_worker_task_file(
            args.worker_task,
            project_root=args.project_root,
            python_executable=args.python,
            timeout_seconds=args.timeout_seconds,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



