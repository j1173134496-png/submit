# Submit Flow MCP Contract

Version: `0.4.0`

This adapter is the only network boundary from AgentOS to Submit Flow. The
existing file-backed task service remains the source of truth for business
state and audit events.

## Transport

- MCP endpoint: `POST /mcp`
- Attachment Broker endpoint: `POST /v1/attachments`
- Artifact Broker endpoint: `GET /v1/tasks/{task_id}/artifacts/{artifact_id}`
- Health: `GET /healthz`
- Readiness: `GET /readyz`
- Default Windows host port: `8121`
- Transport: Streamable HTTP with JSON-RPC 2.0 MCP messages.

The service binds to the host interface needed by Docker Desktop, so the port
must be protected by the Windows firewall and the service token. The adapter
does not log request bodies or authorization values.

## Authentication and Binding

Every MCP, attachment, and artifact request requires:

```text
Authorization: Bearer <private service token>
X-AgentOS-Tenant-Id: <configured tenant>
X-AgentOS-Agent-Id: smart-submit-v1
X-AgentOS-Agent-Version: 1.0.0
X-LibreChat-User-Id: <current user id>
X-LibreChat-User-Role: <current role>
X-LibreChat-User-Email: <current email, if available>
X-AgentOS-Conversation-Id: <current conversation id>
```

Tasks and attachments are bound to the complete tenant/user/conversation/Agent
identity. A different user, conversation, tenant, Agent or Agent version is
rejected. The service token is required in the private environment and must
not be committed, returned by a tool, or exposed to the browser.

## MCP Tools

The server exposes only these business tools:

```text
submit_flow.create_task
submit_flow.attach_file
submit_flow.run_task
submit_flow.get_task
submit_flow.get_review
submit_flow.confirm_task
submit_flow.list_outputs
```

Tool arguments are closed objects. They never accept `runtime_root`, local
paths, URLs, shell commands, Python, SQL, prompts, or free-form commands.

### Create

`submit_flow.create_task` accepts `site_key`, `month`, and optional
`task_id`/`idempotency_key`. Creation succeeds only when the server already
has a committed pricing snapshot for the exact site and month. Otherwise it
returns `pricing_required` and creates no task.

### Attach

`submit_flow.attach_file` accepts `task_id`, `attachment_id`, and an optional
input role. Files must first be uploaded through the authenticated Attachment
Broker as regular PDFs. The adapter copies the broker-owned file through the
existing task service and never accepts a model-supplied file path.

### Run, review and confirm

`submit_flow.run_task` invokes the existing deterministic task service.
`submit_flow.get_task` is the status source for the UI. When the status is
`need_review`, `submit_flow.get_review` returns the current review report.
`submit_flow.confirm_task` accepts only field-level values, creates the
controlled `confirmed_data.json`, and invokes the existing manual rerun. It
does not accept a confirmation file path or arbitrary JSON document.

### Outputs

`submit_flow.list_outputs` returns opaque `artifact_id` values, names, hashes,
sizes and authorized download URLs. It does not return filesystem paths.
Artifact downloads re-check task ownership and resolve only files recorded by
the task's artifact index or task output list.

## State and Recovery

`task.json` and `audit/events.jsonl` remain authoritative. Adapter idempotency
records are stored below `runtime/integration/idempotency/` and task identity
links below `runtime/integration/task_links/`. Replaying a request with the
same identity and key returns the prior structured result; reusing a key with
different arguments is rejected.

The adapter does not mark a task completed based on model text. A completed
response requires the task service's completed state and its output artifacts.
