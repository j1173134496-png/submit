from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_dotenv(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        match = ENV_LINE.match(raw_line)
        if match is None:
            continue
        name, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[name] = value


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="submit-flow-mcp-service")
    parser.add_argument("--env-path", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=8121)
    args = parser.parse_args(argv)

    env_path = args.env_path.resolve(strict=True)
    project_root = args.project_root.resolve(strict=True)
    load_dotenv(env_path)

    host = args.host or os.environ.get("SUBMIT_MCP_HOST", "0.0.0.0")
    service_root = project_root / "runtime" / "service"
    log_root = project_root / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_root / f"submit-mcp-scheduled-{stamp}.log"
    state_path = service_root / "submit-mcp-service.json"
    state = {
        "service": "submit-flow-mcp",
        "supervisor_pid": os.getpid(),
        "child_pid": os.getpid(),
        "port": args.port,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "restart_count": 0,
        "stdout_log": str(log_path),
        "stderr_log": str(log_path),
        "host": host,
        "managed_by": "windows-task-scheduler",
    }
    _write_state(state_path, state)

    from submit_flow_agent.http_service import main as http_main

    try:
        with log_path.open("a", encoding="utf-8", buffering=1) as log_handle:
            with contextlib.redirect_stdout(log_handle), contextlib.redirect_stderr(log_handle):
                result = http_main(
                    [
                        "--project-root",
                        str(project_root),
                        "--host",
                        host,
                        "--port",
                        str(args.port),
                    ]
                )
        return result if result else 1
    finally:
        try:
            current = json.loads(state_path.read_text(encoding="utf-8-sig"))
            if current.get("child_pid") == os.getpid():
                state_path.unlink(missing_ok=True)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
