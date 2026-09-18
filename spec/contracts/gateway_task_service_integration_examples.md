# Gateway Task Service Integration Examples

## Purpose

This note freezes the local Submit Flow Agent integration surface for MVP-018. It is for the OpenClaw Gateway side to call the existing Task Service and Worker Adapter without adding Worker actions or changing Worker contract semantics.

Gateway owns conversation state, attachment download, file checks, task creation, and user-facing messages. Submit Flow Worker owns only schema-valid Worker actions:

- `run_task`
- `confirm_task`
- `show_task`
- `list_outputs`

`create-task` and `add-file` are Task Service CLI calls made by Gateway before Worker dispatch. They are not Worker actions.

## Fixed Runtime

WSL/OpenClaw runtime:

```bash
cd /mnt/k/Workers/Submit_Flow_Agent
export PYTHONPATH=src
export PYTHONIOENCODING=utf-8
RUNTIME_ROOT=runtime
```

Windows local verification runtime:

```powershell
cd K:\Workers\Submit_Flow_Agent
$env:PYTHONPATH='src'
$env:PYTHONIOENCODING='utf-8'
$RUNTIME_ROOT='runtime'
```

Gateway must set the runtime root from deployment configuration. Users must not provide runtime paths, shell commands, Python arguments, or Worker action names.

## Task Id

Recommended task id shape:

```text
pv_<site_key>_<YYYY>_<MM>_<sequence>
```

Example:

```text
pv_xinan_high_school_2026_05_001
```

The current Worker schema accepts `A-Z`, `a-z`, `0-9`, `_`, `.`, and `-` in `task_id`. Avoid spaces and Chinese characters in `task_id`; keep user-facing labels in Gateway session metadata instead.

## File Types

Gateway must collect these three PDF source types for one task month:

| file_type | filename keyword | example |
| --- | --- | --- |
| `generation_statement` | `发电单` | `福域发电单202605-2.pdf` |
| `fee_statement` | `电费结算单` | `福域电费结算单202605_1.pdf` |
| `energy_statement` | `电量结算单` | `福域电量结算单202605_1-2.pdf` |

Gateway should reject non-PDF files before calling Task Service. Task Service also rejects unknown file types and files whose detected month does not match `task.json`.

## Supported Call Sequence

### 1. Create Or Locate Task

```bash
python3 -m submit_flow_agent.cli create-task pv_xinan_high_school_2026_05_001 --site-key xinan_high_school --month 2026-05 --runtime-root runtime
```

Expected task state:

```text
collecting_files
```

Task state source of truth:

```text
runtime/tasks/pv_xinan_high_school_2026_05_001/task.json
runtime/tasks/pv_xinan_high_school_2026_05_001/audit/events.jsonl
```

### 2. Add The Three PDFs

```bash
python3 -m submit_flow_agent.cli add-file pv_xinan_high_school_2026_05_001 /gateway/downloads/福域发电单202605-2.pdf --runtime-root runtime
python3 -m submit_flow_agent.cli add-file pv_xinan_high_school_2026_05_001 /gateway/downloads/福域电费结算单202605_1.pdf --runtime-root runtime
python3 -m submit_flow_agent.cli add-file pv_xinan_high_school_2026_05_001 /gateway/downloads/福域电量结算单202605_1-2.pdf --runtime-root runtime
```

Expected state transition:

```text
collecting_files -> ready_to_run
```

Gateway may dispatch Worker `run_task` only after `task.json.status == "ready_to_run"`.

### 3. Dispatch Worker run_task

Worker payload sample:

```json
{
  "action": "run_task",
  "task_id": "pv_xinan_high_school_2026_05_001",
  "runtime_root": "runtime",
  "site_key": "xinan_high_school",
  "month": "2026-05",
  "mode": "strict",
  "request_id": "gw-run-pv_xinan_high_school_2026_05_001"
}
```

Sample file path:

```text
spec/contracts/samples/gateway_run_task.worker_task.json
```

Invocation:

```bash
python3 -m submit_flow_agent.openclaw_worker_adapter spec/contracts/samples/gateway_run_task.worker_task.json
```

### 4. Query Status

Task Service direct query:

```bash
python3 -m submit_flow_agent.cli show-task pv_xinan_high_school_2026_05_001 --runtime-root runtime
```

Worker query payload:

```text
spec/contracts/samples/gateway_show_task.worker_task.json
```

### 5. List Outputs

Task Service direct query:

```bash
python3 -m submit_flow_agent.cli list-outputs pv_xinan_high_school_2026_05_001 --runtime-root runtime
```

Worker query payload:

```text
spec/contracts/samples/gateway_list_outputs.worker_task.json
```

### 6. Confirm need_review

Gateway must convert user confirmation into `confirmed_data.json`; it must not pass the raw user reply to Worker.

```json
{
  "confirmed_by": "operator_id",
  "confirmed_at": "2026-07-10T10:00:00+08:00",
  "fields": {
    "grid_fee": {
      "value": 407.29,
      "reason": "人工核对电费结算单后确认"
    }
  }
}
```

Worker payload sample:

```text
spec/contracts/samples/gateway_confirm_task.worker_task.json
```

Invocation:

```bash
python3 -m submit_flow_agent.openclaw_worker_adapter spec/contracts/samples/gateway_confirm_task.worker_task.json
```

## State And Error Mapping

| Source | Condition | Gateway handling |
| --- | --- | --- |
| `task.json.status=collecting_files` | Missing one or more PDFs | Ask for remaining source files. |
| `task.json.status=ready_to_run` | Three PDF types collected | Dispatch schema-valid `run_task`. |
| Worker result `completed` | Excel copies generated | Return summary and output manifest. |
| Worker result `need_review` | Validation or confidence issue | Ask for structured confirmation data. |
| Worker result `failed` | Task Service or pipeline error | Return error summary and keep audit trail. |
| CLI exits with `Task service error` | Bad file, month mismatch, task not ready | Do not retry as shell; ask user/Gateway to fix inputs. |
| Worker result validation error | Bad Worker payload | Treat as Gateway integration defect. |

## Regression Command

```powershell
$env:PYTHONPATH='src'
$env:PYTHONIOENCODING='utf-8'
python -m unittest tests.test_gateway_integration_contract tests.test_openclaw_worker_adapter tests.test_worker_contract
```

## Known Limits

- This project does not implement OpenClaw conversation storage or WeCom entry in MVP-018 local scope.
- This project does not accept natural-language commands as Worker input.
- Runtime root must be deployment configuration, not user input.
- Gateway must validate file ownership, extension, size, and task association before `add-file`.
- Worker actions remain limited to the existing four-action schema.

## MVP-022 Canonical Site/Month Resolution

Submit owns durable task state by canonical `site_key + month + revision`. OpenClaw session task pointers are cache/binding values only. Before status, PDF intake, run, output list, or resend, OpenClaw should resolve the canonical state and pass the trusted `site_key` and `month` guard when it acts on a task id.

Read-only state query, with no pricing proposal, task creation, revision creation, run, or effective-promotion side effect:

```bash
python3 -m submit_flow_agent.cli resolve-task-state --site-key xinan_high_school --month 2026-05 --runtime-root runtime
```

Returned state is intended for session binding and user-safe summaries. It includes canonical site identity, month, task id when one exists, revision, stage, pricing-pending flag, PDF progress, and output availability. It does not expose local workbook or PDF paths.

Explicit restart from a canonical site conversation must not depend on a legacy session task pointer. Restart without a pricing snapshot enters pricing confirmation and does not create a revision:

```bash
python3 -m submit_flow_agent.cli resolve-task --site-key xinan_high_school --month 2026-05 --mode restart --runtime-root runtime
```

After OpenClaw confirms the returned pricing proposal through `confirm-task-pricing`, it calls restart again with the committed snapshot. Only then may Submit create or reuse the collecting-files revision and OpenClaw binds the canonical conversation to that returned task/revision:

```bash
python3 -m submit_flow_agent.cli resolve-task --site-key xinan_high_school --month 2026-05 --mode restart --pricing-snapshot-json '<committed snapshot json>' --runtime-root runtime
```

Task-id operations keep backward compatibility, but OpenClaw should pass canonical guards so Submit fails closed on stale or cross-site/month pointers:

```bash
python3 -m submit_flow_agent.cli collect-pdf-batch <task_id> <pdf1> <pdf2> <pdf3> --site-key xinan_high_school --month 2026-05 --runtime-root runtime
python3 -m submit_flow_agent.cli run-task <task_id> --site-key xinan_high_school --month 2026-05 --runtime-root runtime
python3 -m submit_flow_agent.cli show-task <task_id> --site-key xinan_high_school --month 2026-05 --runtime-root runtime
python3 -m submit_flow_agent.cli list-outputs <task_id> --site-key xinan_high_school --month 2026-05 --runtime-root runtime
```

If the task id is not the canonical current task for that `site_key + month`, Submit returns a task service error and OpenClaw must leave its session binding unchanged.