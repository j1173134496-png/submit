# Gateway / Worker Contract

## Purpose

This contract defines the structured boundary between an OpenClaw Gateway and a Codex Worker for the submit-flow-agent MVP. The Gateway may dispatch only schema-valid worker tasks. The Worker may call only the file-backed task service introduced in MVP-012.

The Worker must not execute free-form user text, shell commands, SQL, Python snippets, or business-code edits from Gateway messages.

## Components

| Component | Responsibility |
| --- | --- |
| Gateway | Owns user session, message parsing, attachment persistence, task creation, Worker dispatch, and user-facing result delivery. |
| Codex Worker | Receives `worker_task.schema.json` payloads only; validates action whitelist; calls task service functions or equivalent CLI commands; returns `worker_result.schema.json`. |
| Task Service | Owns `runtime/tasks/<task_id>/task.json`, `inputs/`, `outputs/`, `audit/events.jsonl`, and state transitions. |
| Business Engine | Owns OCR raw mapping, normalization, validation, review/manual flow, and Excel writing through existing `run-month` logic. |

## Allowed Worker Actions

Only these actions are allowed:

| action | Task service operation | CLI equivalent | Purpose |
| --- | --- | --- | --- |
| `run_task` | `run_task(runtime_root, task_id)` | `submit-flow-agent run-task <task_id>` | Run a `ready_to_run` task. |
| `confirm_task` | `confirm_task(runtime_root, task_id, confirmed_data)` | `submit-flow-agent confirm-task <task_id> <confirmed_data>` | Apply `confirmed_data.json` and rerun. |
| `show_task` | `get_task(runtime_root, task_id)` | `submit-flow-agent show-task <task_id>` | Read `task.json`. |
| `list_outputs` | `list_task_outputs(runtime_root, task_id)` | `submit-flow-agent list-outputs <task_id>` | List task output files. |

`create_task` and `add_file` remain Gateway/task-service responsibilities because Gateway owns user attachment collection before Worker dispatch.

## Forbidden Worker Inputs

Worker task payloads must not include any of the following fields:

```text
command
shell
cmd
prompt
user_text
natural_language_instruction
python
sql
script
```

`worker_task.schema.json` uses `additionalProperties=false`; any extra field is invalid. Gateway must translate user intent into a whitelist action before dispatching a Worker task.

## Worker Task Examples

### run_task

```json
{
  "action": "run_task",
  "task_id": "pv_xinan_high_school_2026_05_001",
  "runtime_root": "runtime",
  "site_key": "xinan_high_school",
  "month": "2026-05",
  "mode": "strict"
}
```

### confirm_task

```json
{
  "action": "confirm_task",
  "task_id": "pv_xinan_high_school_2026_05_001",
  "runtime_root": "runtime",
  "confirmed_data": "runtime/tasks/pv_xinan_high_school_2026_05_001/confirmed_data.json",
  "mode": "manual"
}
```

### show_task

```json
{
  "action": "show_task",
  "task_id": "pv_xinan_high_school_2026_05_001",
  "runtime_root": "runtime",
  "mode": "query"
}
```

### list_outputs

```json
{
  "action": "list_outputs",
  "task_id": "pv_xinan_high_school_2026_05_001",
  "runtime_root": "runtime",
  "mode": "query"
}
```

## Worker Result Statuses

### completed

```json
{
  "task_id": "pv_xinan_high_school_2026_05_001",
  "status": "completed",
  "task_status": "completed",
  "summary": "校验通过，已生成 2 个 Excel 结果文件。",
  "outputs": [
    "outputs/新安高中部/2026-05/recognized_data.json",
    "outputs/新安高中部/2026-05/validation_report.json",
    "outputs/新安高中部/2026-05/新安中学（高中部）光伏发电项目学校用电统计表_2026-05.xlsx",
    "outputs/新安高中部/2026-05/汇总表格_2026-05.xlsx"
  ],
  "review_report": null,
  "error": null,
  "task_json": "task.json",
  "request_id": null
}
```

### need_review

```json
{
  "task_id": "pv_xinan_high_school_2026_05_001",
  "status": "need_review",
  "task_status": "need_review",
  "summary": "发现低置信度、字段缺失或业务校验问题，需要人工复核。",
  "outputs": [
    "outputs/新安高中部/2026-05/recognized_data.json",
    "outputs/新安高中部/2026-05/validation_report.json",
    "outputs/新安高中部/2026-05/review_report.json"
  ],
  "review_report": "outputs/新安高中部/2026-05/review_report.json",
  "error": null,
  "task_json": "task.json",
  "request_id": null
}
```

### failed

```json
{
  "task_id": "pv_xinan_high_school_2026_05_001",
  "status": "failed",
  "task_status": "failed",
  "summary": "任务运行失败。",
  "outputs": [],
  "review_report": null,
  "error": "RuntimeError: boom",
  "task_json": "task.json",
  "request_id": null
}
```

## State Mapping

| task.json status | Worker result status | Gateway action |
| --- | --- | --- |
| `completed` | `completed` | Return summary and output files to user. |
| `need_review` | `need_review` | Return review issues and ask for confirmation data. |
| `failed` | `failed` | Return failure reason and log references. |
| `collecting_files` | `collecting_files` | Ask Gateway to collect missing files. |
| `ready_to_run` | `ready_to_run` | Gateway may dispatch `run_task`. |
| `running` | `running` | Tell user the task is in progress. |
| `confirmed` | `running` or `completed` after dispatch | Gateway may dispatch `confirm_task`. |

`task.json` is the source of truth. Chat history, Gateway memory, or Worker notes must not replace task status.

## Audit Requirements

The task service must keep appending `audit/events.jsonl`. The Worker result should not be considered complete unless `task.json` and `events.jsonl` agree on the final state.

Minimum events expected across task lifecycle:

```text
task_created
file_received
task_ready
run_started
need_review
confirmed_data_received
task_completed
task_failed
```

## Security Boundary

The Worker is a task-service caller, not a shell executor.

Required behavior:

- Validate `worker_task.schema.json` before action dispatch.
- Reject unknown `action` values.
- Reject extra fields because `additionalProperties=false`.
- Reject natural-language command fields such as `prompt`, `user_text`, `command`, `shell`, `python`, or `sql`.
- Call only `run_task`, `confirm_task`, `get_task`, or `list_task_outputs`.
- Never bypass `validation_report.json` or write Excel directly.
- Never edit source code, templates, original PDFs, or task output contracts in response to a Worker task.

## Error Handling

Validation failure before dispatch returns a `failed` worker result with a structured `error` explaining the schema violation. Runtime service failures return `failed` with the task service error message and preserve task audit events.
