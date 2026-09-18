# OpenClaw Worker Adapter Examples

## Purpose

MVP-017 exposes a controlled Submit Flow Worker Adapter for OpenClaw. The adapter accepts only structured `worker_task` JSON, validates the existing contract, calls the Submit Flow Agent CLI with fixed argv lists, and returns structured `worker_result` JSON.

It does not parse natural language, does not execute free-form commands, does not modify Worker schema semantics, and does not connect formal WeCom entry logic.

## WSL Invocation

Run inside the existing Ubuntu WSL environment:

```bash
cd /mnt/k/Workers/Submit_Flow_Agent
export PYTHONPATH=src
export PYTHONIOENCODING=utf-8
python3 -m submit_flow_agent.openclaw_worker_adapter tmp/mvp017_show_task.json
```

The adapter internally calls the existing task-service CLI in this shape:

```bash
python3 -m submit_flow_agent.cli show-task <task_id> --runtime-root <runtime_root>
python3 -m submit_flow_agent.cli list-outputs <task_id> --runtime-root <runtime_root>
python3 -m submit_flow_agent.cli run-task <task_id> --runtime-root <runtime_root>
python3 -m submit_flow_agent.cli confirm-task <task_id> <confirmed_data> --runtime-root <runtime_root>
```

The subprocess call is made with an argv list and `shell=False`.

## show_task Request

```json
{
  "action": "show_task",
  "task_id": "mvp017_adapter_query_20260709",
  "runtime_root": "runtime",
  "mode": "query",
  "request_id": "mvp017-show"
}
```

Example response:

```json
{
  "task_id": "mvp017_adapter_query_20260709",
  "status": "collecting_files",
  "task_status": "collecting_files",
  "summary": "任务查询完成。",
  "outputs": [],
  "review_report": null,
  "error": null,
  "task_json": "runtime/tasks/mvp017_adapter_query_20260709/task.json",
  "request_id": "mvp017-show"
}
```

## list_outputs Request

```json
{
  "action": "list_outputs",
  "task_id": "mvp017_adapter_query_20260709",
  "runtime_root": "runtime",
  "mode": "query",
  "request_id": "mvp017-list"
}
```

Example response:

```json
{
  "task_id": "mvp017_adapter_query_20260709",
  "status": "collecting_files",
  "task_status": "collecting_files",
  "summary": "任务输出列表查询完成。",
  "outputs": [],
  "review_report": null,
  "error": null,
  "task_json": "runtime/tasks/mvp017_adapter_query_20260709/task.json",
  "request_id": "mvp017-list"
}
```

## run_task Request

```json
{
  "action": "run_task",
  "task_id": "mvp017_adapter_success_20260709",
  "runtime_root": "runtime",
  "mode": "strict",
  "request_id": "mvp017-run"
}
```

Example controlled-failure response from the current Ubuntu environment, which lacks `pdftoppm`:

```json
{
  "task_id": "mvp017_adapter_success_20260709",
  "status": "failed",
  "task_status": "failed",
  "summary": "任务运行失败。",
  "outputs": [],
  "review_report": null,
  "error": "PdfRenderError: Could not find pdftoppm executable for PDF rendering.",
  "task_json": "runtime/tasks/mvp017_adapter_success_20260709/task.json",
  "request_id": "mvp017-run"
}
```

## confirm_task Request

```json
{
  "action": "confirm_task",
  "task_id": "mvp017_adapter_success_20260709",
  "runtime_root": "runtime",
  "mode": "manual",
  "confirmed_data": "tmp/mvp017_confirmed_data.json",
  "request_id": "mvp017-confirm"
}
```

`confirmed_data` must point to an existing JSON file visible inside WSL. The adapter passes it as a CLI argument, not as shell text.

## Rejected Unknown Action

```json
{
  "action": "delete_task",
  "task_id": "mvp017_adapter_success_20260709",
  "runtime_root": "runtime",
  "request_id": "mvp017-unknown"
}
```

Response:

```json
{
  "task_id": "mvp017_adapter_success_20260709",
  "status": "failed",
  "task_status": "failed",
  "summary": "Worker Adapter 调用失败。",
  "outputs": [],
  "review_report": null,
  "error": "WorkerContractError: Unsupported worker action: delete_task",
  "task_json": null,
  "request_id": "mvp017-unknown"
}
```

## Rejected Free Command Field

```json
{
  "action": "show_task",
  "task_id": "mvp017_adapter_success_20260709",
  "runtime_root": "runtime",
  "mode": "query",
  "command": "rm -rf /",
  "request_id": "mvp017-command"
}
```

Response:

```json
{
  "task_id": "mvp017_adapter_success_20260709",
  "status": "failed",
  "task_status": "failed",
  "summary": "Worker Adapter 调用失败。",
  "outputs": [],
  "review_report": null,
  "error": "WorkerContractError: worker_task contains unknown fields: command",
  "task_json": null,
  "request_id": "mvp017-command"
}
```

## OpenClaw Binding Notes

A future OpenClaw Tool or MCP wrapper should pass the model-produced structured JSON to this adapter as-is only after the Gateway has selected a whitelisted action. The wrapper must not add `command`, `shell`, `cmd`, `prompt`, `user_text`, `natural_language_instruction`, `python`, `sql`, or `script` fields.

Recommended fixed runtime settings:

```bash
cd /mnt/k/Workers/Submit_Flow_Agent
PYTHONPATH=src
PYTHONIOENCODING=utf-8
python3 -m submit_flow_agent.openclaw_worker_adapter <worker_task.json>
```
