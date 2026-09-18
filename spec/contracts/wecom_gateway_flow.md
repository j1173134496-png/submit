# WeCom Gateway Flow

## High-Level Flow

```text
WeCom user/app event
  -> Gateway validates source and message type
  -> Gateway creates or updates runtime task
  -> Gateway dispatches schema-valid Worker task when needed
  -> Worker calls task service only
  -> Task service updates task.json and audit/events.jsonl
  -> Gateway sends WeCom message template
```

## Event To Action Mapping

| WeCom event | Gateway behavior | Task service action | Worker action | Main audit event |
| --- | --- | --- | --- | --- |
| User starts monthly task | Parse site/month, create task_id | `create-task` | None | `task_created` |
| User uploads generation statement PDF | Download attachment, save/register file | `add-file` | None | `file_received` |
| User uploads fee statement PDF | Download attachment, save/register file | `add-file` | None | `file_received` |
| User uploads energy statement PDF | Download attachment, save/register file | `add-file` | None | `file_received` |
| All files received | Notify ready and dispatch Worker | None | `run_task` | `task_ready`, `run_started` |
| User queries progress | Read task state | `show-task` | `show_task` optional | None |
| User asks for outputs | List task outputs | `list-outputs` | `list_outputs` optional | None |
| Worker returns completed | Send result template | None | None | `task_completed` |
| Worker returns need_review | Send review template | None | `show_task` optional | `need_review` |
| User confirms field values | Write `confirmed_data.json` | None | `confirm_task` | `confirmed_data_received`, `run_started` |
| Worker returns failed | Send failed template | None | `show_task` optional | `task_failed` |

## Task Creation Flow

1. User sends a supported create intent, for example: `创建 2026-05 新安高中部任务`.
2. Gateway validates site and month.
3. Gateway creates `task_id`, for example `pv_xinan_high_school_2026_05_001`.
4. Gateway calls task service `create-task`.
5. Gateway sends “Task Created” template.

Gateway stores WeCom context next to task metadata in a future-compatible way:

```json
{
  "source_channel": "wecom",
  "wecom_user_id": "zhangsan",
  "wecom_conversation_id": "conversation-id",
  "task_id": "pv_xinan_high_school_2026_05_001"
}
```

MVP-014 does not require changing `task.json`; formal implementation can append these fields compatibly.

## Attachment Flow

1. Gateway receives file message.
2. Gateway checks file extension and size before download if metadata exists.
3. Gateway downloads attachment into a Gateway temp directory.
4. Gateway normalizes file path and keeps original filename for file scanner compatibility.
5. Gateway calls `add-file <task_id> <downloaded_pdf>`.
6. Task service copies the file into `runtime/tasks/<task_id>/inputs/`.
7. If all files are present, task service marks `ready_to_run`.
8. Gateway dispatches Worker `run_task`.

## Worker Dispatch Payloads

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

Gateway must validate payloads against `worker_task.schema.json` before dispatch.

## Review / Manual Flow

1. Worker returns `need_review` with `review_report` path.
2. Gateway reads review report summary and sends the Need Review template.
3. User replies with a supported confirmation syntax.
4. Gateway parses only field/value/reason; it does not pass raw text to Worker.
5. Gateway writes `confirmed_data.json`:

```json
{
  "confirmed_by": "wecom_user_id",
  "confirmed_at": "gateway_time_iso8601",
  "fields": {
    "grid_fee": {
      "value": 407.29,
      "reason": "人工核对电费结算单后确认"
    }
  }
}
```

6. Gateway dispatches Worker `confirm_task`.
7. Task service reruns validation and Excel writing through existing business engine.
8. Gateway sends completed, need_review, or failed template based on Worker result.

## Output Delivery Flow

Gateway should not assume every output can be directly sent as a WeCom file in MVP. The formal implementation must choose one of these strategies:

| Strategy | Use case | Notes |
| --- | --- | --- |
| Direct file send | Small Excel result files | Requires WeCom file upload/send capability. |
| Internal download link | Larger outputs or multiple files | Requires access control and expiry. |
| Notification only | Early internal rollout | User retrieves files from shared runtime/output path. |

Regardless of delivery strategy, Gateway message must include `task_id` and output manifest from `list_outputs`.

## Failure Flow

Gateway distinguishes three failure classes:

| Failure | Handling |
| --- | --- |
| WeCom attachment download failure | Do not dispatch Worker; ask user to reupload. |
| Task service failed | Worker returns `failed`; Gateway sends failed template with task_id and error. |
| Business validation failed | Worker returns `need_review`; Gateway sends review template, not failed template. |

## Security Rules

- Gateway never forwards raw WeCom text as Worker instructions.
- Gateway maps user messages to fixed intents only.
- Worker payload must pass MVP-013 schema validation.
- Worker can call only task service actions: `run_task`, `confirm_task`, `show_task`, `list_outputs`.
- Gateway must reject unsupported user commands with the Unsupported Text template.
- Gateway must not store WeCom secrets in repo files.
- Gateway must not let users choose arbitrary runtime paths.
- Gateway must not expose local filesystem paths to unauthorized users in production; MVP paths are internal diagnostics only.

## Minimal Production Readiness Checklist

Before formal WeCom implementation:

- Confirm final entry form: self-built app vs app robot plus notification webhook.
- Confirm attachment download API permissions and file size limits.
- Confirm identity source fields for user id and conversation id.
- Confirm output delivery strategy.
- Confirm secret storage strategy outside git.
- Confirm Gateway deployment network and callback domain.
- Confirm operator authorization model for manual confirmation.
