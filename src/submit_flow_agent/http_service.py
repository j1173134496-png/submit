"""Controlled HTTP and MCP adapter for the Submit Flow task service.

The adapter is deliberately thin: the existing file-backed task service remains
the source of truth for business state, while this module owns identity binding,
attachment staging, artifact authorization, and the MCP transport boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as email_default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote, urlsplit

from submit_flow_agent.file_scanner import REQUIRED_FILE_TYPES
from submit_flow_agent.pricing import PricingError, latest_site_pricing
from submit_flow_agent.review_contract import ReviewContractError, validate_confirmed_data, validate_review_report
from submit_flow_agent.task_service import (
    TaskServiceError,
    add_task_file,
    confirm_task,
    create_task,
    get_task,
    list_task_outputs,
    run_task,
)
from submit_flow_agent.task_store import (
    TaskStoreError,
    load_task,
    task_lock,
    task_root_from_id,
    utc_now_iso,
)


SERVICE_NAME = "submit-flow-mcp"
SERVICE_VERSION = "0.4.0"
MCP_PROTOCOL_VERSION = "2025-03-26"
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ATTACHMENT_ID_PATTERN = re.compile(r"^att_[A-Za-z0-9_-]{16,96}$")
ARTIFACT_ID_PATTERN = re.compile(r"^art_[A-Za-z0-9_-]{16,96}$")
MONTH_PATTERN = re.compile(r"^20[0-9]{2}-(0[1-9]|1[0-2])$")
SAFE_FILE_TYPE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class AdapterError(RuntimeError):
    """An error safe to expose at the integration boundary."""

    def __init__(self, message: str, *, code: str = "invalid_request", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ServiceConfig:
    runtime_root: Path
    config_path: Path
    token: str
    tenant_id: str
    agent_id: str
    agent_version: str
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES

    @classmethod
    def from_environment(cls, *, project_root: Path | str = ".") -> "ServiceConfig":
        root = Path(project_root).resolve()
        runtime_value = os.environ.get("SUBMIT_RUNTIME_ROOT", "runtime")
        config_value = os.environ.get("SUBMIT_CONFIG_PATH", "config/sites.json")
        runtime_root = _resolve_project_path(root, runtime_value)
        config_path = _resolve_project_path(root, config_value)
        token = os.environ.get("SUBMIT_MCP_TOKEN", "").strip()
        tenant_id = os.environ.get("SUBMIT_TENANT_ID", "lnp-default").strip()
        agent_id = os.environ.get("SUBMIT_AGENT_ID", "smart-submit-v1").strip()
        agent_version = os.environ.get("SUBMIT_AGENT_VERSION", "1.0.0").strip()
        try:
            max_upload = int(os.environ.get("SUBMIT_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES)))
        except ValueError as exc:
            raise AdapterError("SUBMIT_MAX_UPLOAD_BYTES must be an integer.", code="configuration_error", status=500) from exc
        return cls(
            runtime_root=runtime_root,
            config_path=config_path,
            token=token,
            tenant_id=tenant_id,
            agent_id=agent_id,
            agent_version=agent_version,
            max_upload_bytes=max(1024, min(max_upload, 500 * 1024 * 1024)),
        )


@dataclass(frozen=True)
class RequestContext:
    tenant_id: str
    user_id: str
    user_role: str
    user_email: str | None
    conversation_id: str
    agent_id: str
    agent_version: str


@dataclass(frozen=True)
class AdapterResponse:
    status: int
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: Mapping[str, str] | None = None


class SubmitFlowAdapter:
    """HTTP-independent request dispatcher used by the Windows service and tests."""

    def __init__(self, config: ServiceConfig) -> None:
        self.config = config

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes = b"") -> AdapterResponse:
        parsed = urlsplit(target)
        path = parsed.path.rstrip("/") or "/"
        try:
            if method.upper() == "GET" and path in {"/healthz", "/readyz"}:
                return self._health(path)
            if method.upper() == "POST" and path == "/v1/attachments":
                context = self._authenticate(headers, require_conversation=True)
                return self._upload_attachment(context, headers, body)
            if method.upper() == "POST" and path == "/mcp":
                context = self._authenticate(headers, require_conversation=True)
                return self._handle_mcp(context, headers, body)
            if method.upper() == "HEAD" and path == "/mcp":
                # OAuth-capability probes use HEAD before attempting MCP
                # initialization. A method response avoids a misleading 501
                # while keeping the probe from being treated as OAuth auth.
                return AdapterResponse(status=405, headers={"Allow": "POST"})
            if method.upper() in {"GET", "DELETE"} and path == "/mcp":
                # Streamable HTTP clients may probe an optional SSE stream and
                # may attempt explicit session termination. This service is
                # request/response-only, so advertise that explicitly instead
                # of returning 404 and making the client treat the connection
                # as broken.
                self._authenticate(headers, require_conversation=True)
                return AdapterResponse(status=405, headers={"Allow": "POST"})
            artifact_match = re.fullmatch(r"/v1/tasks/([^/]+)/artifacts/([^/]+)", path)
            if method.upper() == "GET" and artifact_match:
                context = self._authenticate(headers, require_conversation=True)
                return self._download_artifact(context, unquote(artifact_match.group(1)), unquote(artifact_match.group(2)))
            raise AdapterError("Route not found.", code="not_found", status=404)
        except AdapterError as exc:
            return self._json_response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (PricingError, TaskServiceError, TaskStoreError, OSError, ValueError) as exc:
            return self._json_response(422, {"error": {"code": "task_error", "message": f"Submit task operation failed: {exc}"}})
        except Exception:
            return self._json_response(500, {"error": {"code": "internal_error", "message": "Submit service encountered an internal error."}})

    def _health(self, path: str) -> AdapterResponse:
        if path == "/healthz":
            return self._json_response(200, {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION})
        checks = {
            "token_configured": bool(self.config.token),
            "runtime_root": self.config.runtime_root.is_dir() or self._can_create_runtime_root(),
            "site_config": self.config.config_path.is_file() and not self.config.config_path.is_symlink(),
        }
        ready = all(checks.values())
        return self._json_response(200 if ready else 503, {"status": "ready" if ready else "not_ready", "checks": checks})

    def _can_create_runtime_root(self) -> bool:
        try:
            self.config.runtime_root.mkdir(parents=True, exist_ok=True)
            return self.config.runtime_root.is_dir() and not self.config.runtime_root.is_symlink()
        except OSError:
            return False

    def _authenticate(self, headers: Mapping[str, str], *, require_conversation: bool) -> RequestContext:
        if not self.config.token:
            raise AdapterError("Submit MCP service token is not configured.", code="configuration_error", status=503)
        authorization = _header(headers, "Authorization")
        if not authorization.startswith("Bearer ") or not hmac.compare_digest(authorization[7:].strip(), self.config.token):
            raise AdapterError("Service authentication failed.", code="unauthorized", status=401)
        tenant_id = _required_header(headers, "X-AgentOS-Tenant-Id")
        agent_id = _required_header(headers, "X-AgentOS-Agent-Id")
        agent_version = _required_header(headers, "X-AgentOS-Agent-Version")
        if tenant_id != self.config.tenant_id or agent_id != self.config.agent_id or agent_version != self.config.agent_version:
            raise AdapterError("AgentOS binding is not authorized for this service.", code="forbidden", status=403)
        user_id = _bounded_header(headers, "X-LibreChat-User-Id", required=True)
        user_role = _bounded_header(headers, "X-LibreChat-User-Role", required=True)
        user_email = _bounded_header(headers, "X-LibreChat-User-Email", required=False)
        conversation_id = _bounded_header(headers, "X-AgentOS-Conversation-Id", required=require_conversation)
        if require_conversation and not conversation_id:
            raise AdapterError("Conversation binding is required.", code="missing_identity", status=400)
        return RequestContext(
            tenant_id=tenant_id,
            user_id=user_id,
            user_role=user_role,
            user_email=user_email,
            conversation_id=conversation_id or "",
            agent_id=agent_id,
            agent_version=agent_version,
        )

    def _handle_mcp(self, context: RequestContext, headers: Mapping[str, str], body: bytes) -> AdapterResponse:
        payload = _parse_json_object(body, max_bytes=2 * 1024 * 1024)
        request_id = payload.get("id")
        if payload.get("jsonrpc") != "2.0":
            return self._mcp_error(request_id, -32600, "Invalid JSON-RPC request.")
        method = payload.get("method")
        if not isinstance(method, str) or not method:
            return self._mcp_error(request_id, -32600, "Invalid JSON-RPC request.")
        if method == "notifications/initialized":
            return AdapterResponse(status=202, body=b"", content_type="application/json; charset=utf-8")
        if method == "initialize":
            session_id = _header(headers, "MCP-Session-Id") or f"mcp_{secrets.token_urlsafe(24)}"
            return self._mcp_result(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVICE_NAME, "version": SERVICE_VERSION},
                    "instructions": "Submit Flow tools operate only on the authenticated user's bound tasks and attachments.",
                },
                headers={"MCP-Session-Id": session_id},
            )
        if method == "ping":
            return self._mcp_result(request_id, {})
        if method == "tools/list":
            return self._mcp_result(request_id, {"tools": TOOL_DEFINITIONS})
        if method != "tools/call":
            return self._mcp_error(request_id, -32601, f"Unsupported MCP method: {method}")
        params = payload.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return self._mcp_error(request_id, -32602, "tools/call requires params.name.")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return self._mcp_error(request_id, -32602, "Tool arguments must be a JSON object.")
        try:
            result = self._call_tool(context, params["name"], arguments)
            return self._mcp_result(
                request_id,
                {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result},
            )
        except AdapterError as exc:
            error_payload = {"error": {"code": exc.code, "message": str(exc)}}
            return self._mcp_result(
                request_id,
                {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(error_payload, ensure_ascii=False)}],
                    "structuredContent": error_payload,
                },
            )
        except (PricingError, TaskServiceError, TaskStoreError, OSError, ValueError) as exc:
            error_payload = {"error": {"code": "task_error", "message": f"Submit task operation failed: {exc}"}}
            return self._mcp_result(
                request_id,
                {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(error_payload, ensure_ascii=False)}],
                    "structuredContent": error_payload,
                },
            )

    def _call_tool(self, context: RequestContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        definition = next((item for item in TOOL_DEFINITIONS if item["name"] == name), None)
        if definition is None:
            raise AdapterError(f"Unknown Submit tool: {name}", code="unknown_tool", status=404)
        _reject_extra_arguments(arguments, definition["inputSchema"].get("properties", {}), name)
        required = definition["inputSchema"].get("required", [])
        for field in required:
            if field not in arguments:
                raise AdapterError(f"Tool {name} requires argument: {field}.", code="invalid_arguments", status=422)
        try:
            if name == "submit_flow.create_task":
                return self._create_task(context, arguments)
            if name == "submit_flow.attach_file":
                return self._attach_file(context, arguments)
            if name == "submit_flow.run_task":
                return self._run_task(context, arguments)
            if name == "submit_flow.get_task":
                return self._get_task(context, arguments)
            if name == "submit_flow.get_review":
                return self._get_review(context, arguments)
            if name == "submit_flow.confirm_task":
                return self._confirm_task(context, arguments)
            if name == "submit_flow.list_outputs":
                return self._list_outputs(context, arguments)
        except (PricingError, TaskServiceError, TaskStoreError, OSError, ValueError) as exc:
            raise AdapterError(f"Submit task operation failed: {exc}", code="task_error", status=422) from exc
        raise AdapterError(f"Unsupported Submit tool: {name}", code="unknown_tool", status=404)

    def _create_task(self, context: RequestContext, arguments: dict[str, Any]) -> dict[str, Any]:
        site_key = _required_string(arguments, "site_key", max_length=128)
        month = _required_month(arguments, "month")
        requested_task_id = arguments.get("task_id")
        idempotency_key = _optional_string(arguments, "idempotency_key", max_length=128) or f"create:{site_key}:{month}"
        if requested_task_id is not None:
            task_id = _safe_task_id(requested_task_id)
        else:
            task_seed = "|".join((context.tenant_id, context.user_id, context.conversation_id, site_key, month, idempotency_key))
            task_id = "submit_" + hashlib.sha256(task_seed.encode("utf-8")).hexdigest()[:32]

        snapshot = latest_site_pricing(self.config.runtime_root, site_key=site_key)
        if not isinstance(snapshot, dict) or snapshot.get("month") != month:
            return {
                "status": "pricing_required",
                "site_key": site_key,
                "month": month,
                "task_id": None,
                "message": "A committed pricing snapshot is required before creating this task.",
            }

        def operation() -> dict[str, Any]:
            current_snapshot = latest_site_pricing(self.config.runtime_root, site_key=site_key)
            if not isinstance(current_snapshot, dict) or current_snapshot.get("month") != month:
                raise AdapterError("Pricing snapshot is no longer available for this month.", code="pricing_required", status=422)
            if (self.config.runtime_root / "tasks" / task_id).exists():
                raise AdapterError("Task ID already exists; retry with a new idempotency key.", code="conflict", status=409)
            try:
                task = create_task(
                    self.config.runtime_root,
                    task_id=task_id,
                    site_key=site_key,
                    month=month,
                    config_path=self.config.config_path,
                    pricing_snapshot=current_snapshot,
                )
            except FileExistsError as exc:
                raise AdapterError("Task ID already exists; retry with a new idempotency key.", code="conflict", status=409) from exc
            self._write_task_link(context, task)
            return self._task_view(task)

        fingerprint = _fingerprint({"site_key": site_key, "month": month, "task_id": task_id})
        return self._idempotent(context, "create_task", idempotency_key, operation, fingerprint=fingerprint)

    def _attach_file(self, context: RequestContext, arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = _safe_task_id(_required_string(arguments, "task_id", max_length=128))
        attachment_id = _safe_attachment_id(_required_string(arguments, "attachment_id", max_length=128))
        file_type = _optional_string(arguments, "file_type", max_length=64)
        if file_type is not None and (file_type not in REQUIRED_FILE_TYPES or not SAFE_FILE_TYPE_PATTERN.fullmatch(file_type)):
            raise AdapterError("file_type is not an allowed Submit input role.", code="invalid_arguments", status=422)
        self._assert_task_owner(context, task_id)
        metadata_path, metadata = self._load_attachment(attachment_id)
        self._assert_context_match(context, metadata, label="attachment")
        attached_task_id = metadata.get("attached_task_id")
        if attached_task_id is not None:
            if attached_task_id != task_id:
                raise AdapterError("Attachment is already bound to another task.", code="forbidden", status=403)
            task = get_task(self.config.runtime_root, task_id)
            return {"status": "already_attached", "task_id": task_id, "task": self._task_view(task)}

        idempotency_key = _optional_string(arguments, "idempotency_key", max_length=128) or f"attach:{attachment_id}:{task_id}:{file_type or ''}"

        def operation() -> dict[str, Any]:
            task = add_task_file(
                self.config.runtime_root,
                task_id,
                Path(str(metadata["path"])),
                file_type=file_type,
                config_path=self.config.config_path,
            )
            metadata["attached_task_id"] = task_id
            metadata["attached_at"] = utc_now_iso()
            _write_json_atomic(metadata_path, metadata)
            return {"status": "attached", "task_id": task_id, "task": self._task_view(task)}

        fingerprint = _fingerprint({"task_id": task_id, "attachment_id": attachment_id, "file_type": file_type})
        return self._idempotent(context, "attach_file", idempotency_key, operation, fingerprint=fingerprint)

    def _run_task(self, context: RequestContext, arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = _safe_task_id(_required_string(arguments, "task_id", max_length=128))
        self._assert_task_owner(context, task_id)
        idempotency_key = _optional_string(arguments, "idempotency_key", max_length=128) or f"run:{task_id}"

        def operation() -> dict[str, Any]:
            result = run_task(self.config.runtime_root, task_id, config_path=self.config.config_path)
            return self._task_view(result.task)

        return self._idempotent(context, "run_task", idempotency_key, operation, fingerprint=_fingerprint({"task_id": task_id}))

    def _get_task(self, context: RequestContext, arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = _safe_task_id(_required_string(arguments, "task_id", max_length=128))
        self._assert_task_owner(context, task_id)
        task = get_task(self.config.runtime_root, task_id)
        return self._task_view(task)

    def _get_review(self, context: RequestContext, arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = _safe_task_id(_required_string(arguments, "task_id", max_length=128))
        self._assert_task_owner(context, task_id)
        task = get_task(self.config.runtime_root, task_id)
        reference = task.get("review_report")
        if not isinstance(reference, str) or not reference:
            return {"task_id": task_id, "status": task.get("status"), "review_report": None}
        path = self._task_path(task_id, reference)
        if not path.is_file() or path.is_symlink():
            raise AdapterError("Current review report is unavailable.", code="artifact_missing", status=404)
        try:
            review = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise AdapterError("Current review report is invalid.", code="artifact_invalid", status=500) from exc
        return {"task_id": task_id, "status": task.get("status"), "review_report": review}

    def _confirm_task(self, context: RequestContext, arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = _safe_task_id(_required_string(arguments, "task_id", max_length=128))
        fields = arguments.get("field_confirmations")
        if not isinstance(fields, dict) or not fields or len(fields) > 64:
            raise AdapterError("field_confirmations must be a non-empty object.", code="invalid_arguments", status=422)
        _validate_confirmation_shape(fields)
        self._assert_task_owner(context, task_id)
        idempotency_key = _optional_string(arguments, "idempotency_key", max_length=128) or f"confirm:{task_id}:{_fingerprint(fields)}"

        def operation() -> dict[str, Any]:
            root = task_root_from_id(self.config.runtime_root, task_id)
            confirmed_path = root / "confirmed_data.json"
            payload = {
                "confirmed_by": context.user_id,
                "confirmed_at": utc_now_iso(),
                "fields": fields,
            }
            task = get_task(self.config.runtime_root, task_id)
            report_reference = task.get("review_report")
            if not isinstance(report_reference, str) or not report_reference:
                raise AdapterError("Task has no current review report.", code="invalid_state", status=422)
            report_path = self._task_path(task_id, report_reference)
            try:
                review_payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
                confirmable_fields = validate_review_report(review_payload)
                validate_confirmed_data(payload, allowed_fields=confirmable_fields)
            except (OSError, json.JSONDecodeError, ReviewContractError) as exc:
                raise AdapterError(f"Invalid confirmation request: {exc}", code="invalid_arguments", status=422) from exc
            _write_json_atomic(confirmed_path, payload)
            result = confirm_task(self.config.runtime_root, task_id, confirmed_path, config_path=self.config.config_path)
            return self._task_view(result.task)

        return self._idempotent(context, "confirm_task", idempotency_key, operation)

    def _list_outputs(self, context: RequestContext, arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = _safe_task_id(_required_string(arguments, "task_id", max_length=128))
        self._assert_task_owner(context, task_id)
        task = get_task(self.config.runtime_root, task_id)
        outputs = list_task_outputs(self.config.runtime_root, task_id)
        artifacts = [{key: value for key, value in item.items() if key != "_path"} for item in self._artifact_views(task_id, outputs)]
        return {"task_id": task_id, "status": task.get("status"), "artifacts": artifacts}

    def _upload_attachment(self, context: RequestContext, headers: Mapping[str, str], body: bytes) -> AdapterResponse:
        content_length = _content_length(headers)
        if content_length > self.config.max_upload_bytes:
            raise AdapterError("Attachment exceeds the configured size limit.", code="payload_too_large", status=413)
        content_type = _header(headers, "Content-Type")
        if not content_type.lower().startswith("multipart/form-data"):
            raise AdapterError("Attachment upload requires multipart/form-data.", code="invalid_request", status=415)
        if len(body) > self.config.max_upload_bytes:
            raise AdapterError("Attachment exceeds the configured size limit.", code="payload_too_large", status=413)
        fields = _parse_multipart(content_type, body)
        file_item = fields.get("file")
        if not isinstance(file_item, dict) or not isinstance(file_item.get("data"), bytes):
            raise AdapterError("Multipart request must include one file field named file.", code="invalid_request", status=422)
        filename = _safe_filename(str(file_item.get("filename") or "upload.pdf"))
        data = file_item["data"]
        if not filename.casefold().endswith(".pdf") or not data.startswith(b"%PDF-"):
            raise AdapterError("Only regular PDF attachments are accepted.", code="unsupported_file", status=415)
        if not data:
            raise AdapterError("Attachment is empty.", code="invalid_request", status=422)
        idempotency_key = _header(headers, "Idempotency-Key")
        fingerprint = _fingerprint({"filename": filename, "sha256": hashlib.sha256(data).hexdigest()})

        def operation() -> dict[str, Any]:
            attachment_id = f"att_{secrets.token_urlsafe(24)}"
            attachment_root = self.config.runtime_root / "attachments" / attachment_id
            attachment_root.mkdir(parents=True, exist_ok=False)
            payload_path = attachment_root / "payload.pdf"
            _atomic_write_bytes(payload_path, data)
            metadata = {
                "attachment_id": attachment_id,
                "tenant_id": context.tenant_id,
                "user_id": context.user_id,
                "user_role": context.user_role,
                "user_email": context.user_email,
                "conversation_id": context.conversation_id,
                "agent_id": context.agent_id,
                "agent_version": context.agent_version,
                "original_filename": filename,
                "content_type": "application/pdf",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "path": str(payload_path),
                "created_at": utc_now_iso(),
                "attached_task_id": None,
            }
            metadata_path = attachment_root / "metadata.json"
            _write_json_atomic(metadata_path, metadata)
            return {
                "status": "uploaded",
                "attachment_id": attachment_id,
                "file_name": filename,
                "size": len(data),
                "sha256": metadata["sha256"],
            }

        if idempotency_key:
            result = self._idempotent(context, "upload_attachment", idempotency_key, operation, fingerprint=fingerprint)
        else:
            result = operation()
        return self._json_response(201, result)

    def _download_artifact(self, context: RequestContext, task_id: str, artifact_id: str) -> AdapterResponse:
        task_id = _safe_task_id(task_id)
        if not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise AdapterError("Invalid artifact ID.", code="invalid_request", status=400)
        self._assert_task_owner(context, task_id)
        outputs = list_task_outputs(self.config.runtime_root, task_id)
        artifact = next((item for item in self._artifact_views(task_id, outputs) if item["artifact_id"] == artifact_id), None)
        if artifact is None:
            raise AdapterError("Artifact not found.", code="artifact_missing", status=404)
        path = self._task_path(task_id, str(artifact["_path"]))
        if path.is_symlink() or not path.is_file():
            raise AdapterError("Artifact is unavailable.", code="artifact_missing", status=404)
        data = path.read_bytes()
        filename = _safe_filename(str(artifact["file_name"]))
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return AdapterResponse(
            status=200,
            body=data,
            content_type=content_type,
            headers={
                "Content-Disposition": f"attachment; filename=artifact; filename*=UTF-8''{quote(filename)}",
                "Cache-Control": "private, no-store",
                "X-Content-SHA256": str(artifact["sha256"]),
            },
        )

    def _artifact_views(self, task_id: str, outputs: list[str]) -> list[dict[str, Any]]:
        root = task_root_from_id(self.config.runtime_root, task_id)
        index_path = root / "artifact_index.json"
        records: list[dict[str, Any]] = []
        if index_path.is_file() and not index_path.is_symlink():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as exc:
                raise AdapterError("Task artifact index is invalid.", code="artifact_invalid", status=500) from exc
            if isinstance(index, dict) and isinstance(index.get("artifacts"), list):
                records = [item for item in index["artifacts"] if isinstance(item, dict) and item.get("kind") != "source_pdf"]
        if not records:
            records = [{"path": reference, "kind": "output", "role": "output", "status": "available"} for reference in outputs]
        views: list[dict[str, Any]] = []
        for record in records:
            reference = record.get("path")
            if not isinstance(reference, str):
                continue
            path = self._task_path(task_id, reference)
            if path.is_symlink() or not path.is_file():
                continue
            digest = str(record.get("sha256") or _sha256(path))
            artifact_id = "art_" + hashlib.sha256(f"{task_id}:{reference}:{digest}".encode("utf-8")).hexdigest()[:32]
            views.append(
                {
                    "artifact_id": artifact_id,
                    "kind": record.get("kind", "output"),
                    "role": record.get("role", "output"),
                    "file_name": path.name,
                    "size": path.stat().st_size,
                    "sha256": digest,
                    "status": record.get("status", "available"),
                    "download_url": f"/v1/tasks/{quote(task_id)}/artifacts/{artifact_id}",
                    "_path": reference,
                }
            )
        return views

    def _write_task_link(self, context: RequestContext, task: dict[str, Any]) -> None:
        task_id = _safe_task_id(str(task.get("task_id") or ""))
        link_path = self.config.runtime_root / "integration" / "task_links" / f"{task_id}.json"
        payload = {
            "task_id": task_id,
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "user_role": context.user_role,
            "user_email": context.user_email,
            "conversation_id": context.conversation_id,
            "agent_id": context.agent_id,
            "agent_version": context.agent_version,
            "created_at": utc_now_iso(),
        }
        with self._integration_lock():
            if link_path.exists():
                raise AdapterError("Task is already bound to an integration identity.", code="conflict", status=409)
            _write_json_atomic(link_path, payload)

    def _assert_task_owner(self, context: RequestContext, task_id: str) -> dict[str, Any]:
        link_path = self.config.runtime_root / "integration" / "task_links" / f"{task_id}.json"
        if not link_path.is_file() or link_path.is_symlink():
            raise AdapterError("Task not found.", code="not_found", status=404)
        try:
            link = json.loads(link_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise AdapterError("Task identity link is invalid.", code="identity_invalid", status=500) from exc
        if not isinstance(link, dict):
            raise AdapterError("Task identity link is invalid.", code="identity_invalid", status=500)
        self._assert_context_match(context, link, label="task")
        return link

    @staticmethod
    def _assert_context_match(context: RequestContext, record: Mapping[str, Any], *, label: str) -> None:
        fields = {
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "conversation_id": context.conversation_id,
            "agent_id": context.agent_id,
            "agent_version": context.agent_version,
        }
        if any(record.get(key) != value for key, value in fields.items()):
            raise AdapterError(f"{label.capitalize()} does not belong to the current user or conversation.", code="forbidden", status=403)

    def _load_attachment(self, attachment_id: str) -> tuple[Path, dict[str, Any]]:
        metadata_path = self.config.runtime_root / "attachments" / attachment_id / "metadata.json"
        if not metadata_path.is_file() or metadata_path.is_symlink():
            raise AdapterError("Attachment not found.", code="not_found", status=404)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise AdapterError("Attachment metadata is invalid.", code="attachment_invalid", status=500) from exc
        if not isinstance(metadata, dict) or metadata.get("attachment_id") != attachment_id:
            raise AdapterError("Attachment metadata is invalid.", code="attachment_invalid", status=500)
        path = Path(str(metadata.get("path") or ""))
        expected_root = (self.config.runtime_root / "attachments" / attachment_id).resolve()
        try:
            path.resolve().relative_to(expected_root)
        except ValueError as exc:
            raise AdapterError("Attachment path is unsafe.", code="attachment_invalid", status=500) from exc
        if path.is_symlink() or not path.is_file():
            raise AdapterError("Attachment is unavailable.", code="not_found", status=404)
        return metadata_path, metadata

    def _task_path(self, task_id: str, reference: str) -> Path:
        candidate = Path(reference)
        if candidate.is_absolute() or ".." in candidate.parts or not reference:
            raise AdapterError("Task artifact path is unsafe.", code="artifact_invalid", status=500)
        root = task_root_from_id(self.config.runtime_root, task_id)
        if root.is_symlink():
            raise AdapterError("Task root is unsafe.", code="storage_invalid", status=500)
        path = root / candidate
        try:
            path.resolve(strict=False).relative_to(root.resolve())
        except ValueError as exc:
            raise AdapterError("Task artifact path is unsafe.", code="artifact_invalid", status=500) from exc
        return path

    def _task_view(self, task: dict[str, Any]) -> dict[str, Any]:
        files = task.get("files") if isinstance(task.get("files"), dict) else {}
        intake = task.get("pdf_intake") if isinstance(task.get("pdf_intake"), dict) else {}
        return {
            "task_id": task.get("task_id"),
            "site_key": task.get("site_key"),
            "site_name": task.get("site_name"),
            "month": task.get("month"),
            "status": task.get("status"),
            "required_files": task.get("required_files", list(REQUIRED_FILE_TYPES)),
            "files": {str(key): bool(value) for key, value in files.items()},
            "received_count": intake.get("received_count"),
            "required_count": intake.get("required_count", len(REQUIRED_FILE_TYPES)),
            "missing_roles": intake.get("missing_roles", []),
            "next_allowed_action": intake.get("next_allowed_action"),
            "output_count": len(task.get("outputs") or []) if task.get("status") == "completed" else 0,
            "has_review_report": bool(task.get("review_report")),
            "error": task.get("error"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "revision": task.get("revision"),
        }

    def _idempotent(
        self,
        context: RequestContext,
        action: str,
        key: str,
        operation: Callable[[], dict[str, Any]],
        *,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        if not key or len(key) > 128:
            raise AdapterError("Idempotency key must be 1-128 characters.", code="invalid_arguments", status=422)
        clean_fingerprint = fingerprint or _fingerprint({"action": action, "key": key})
        record_path = self._idempotency_path(context, action, key)
        with self._integration_lock():
            existing = _read_json_if_exists(record_path)
            if isinstance(existing, dict):
                if existing.get("fingerprint") != clean_fingerprint:
                    raise AdapterError("Idempotency key was reused with different arguments.", code="idempotency_conflict", status=409)
                if existing.get("status") == "completed" and isinstance(existing.get("result"), dict):
                    return existing["result"]
                if existing.get("status") == "in_progress":
                    raise AdapterError("The same request is already in progress.", code="request_in_progress", status=409)
                if existing.get("status") == "failed":
                    raise AdapterError(str(existing.get("error") or "Previous request failed."), code="previous_request_failed", status=409)
            _write_json_atomic(
                record_path,
                {"status": "in_progress", "action": action, "fingerprint": clean_fingerprint, "created_at": utc_now_iso()},
            )
        try:
            result = operation()
        except Exception as exc:
            with self._integration_lock():
                _write_json_atomic(record_path, {"status": "failed", "action": action, "fingerprint": clean_fingerprint, "error": str(exc), "updated_at": utc_now_iso()})
            raise
        with self._integration_lock():
            _write_json_atomic(record_path, {"status": "completed", "action": action, "fingerprint": clean_fingerprint, "result": result, "updated_at": utc_now_iso()})
        return result

    def _idempotency_path(self, context: RequestContext, action: str, key: str) -> Path:
        digest = hashlib.sha256(
            "|".join((context.tenant_id, context.user_id, context.conversation_id, context.agent_id, context.agent_version, action, key)).encode("utf-8")
        ).hexdigest()
        return self.config.runtime_root / "integration" / "idempotency" / f"{digest}.json"

    def _integration_lock(self):
        lock_root = self.config.runtime_root / "integration"
        lock_root.mkdir(parents=True, exist_ok=True)
        return task_lock(lock_root)

    def _mcp_result(self, request_id: Any, result: dict[str, Any], *, headers: Mapping[str, str] | None = None) -> AdapterResponse:
        return self._json_response(200, {"jsonrpc": "2.0", "id": request_id, "result": result}, headers=headers)

    def _mcp_error(self, request_id: Any, code: int, message: str) -> AdapterResponse:
        return self._json_response(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    @staticmethod
    def _json_response(status: int, payload: Any, *, headers: Mapping[str, str] | None = None) -> AdapterResponse:
        return AdapterResponse(status=status, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)


class SubmitFlowHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], adapter: SubmitFlowAdapter) -> None:
        self.adapter = adapter
        super().__init__(address, SubmitFlowRequestHandler)


class SubmitFlowRequestHandler(BaseHTTPRequestHandler):
    server: SubmitFlowHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            length = _content_length(self.headers)
            if method.upper() == "POST" and self.path.rstrip("/") == "/mcp":
                authorization = self.headers.get("Authorization", "")
                if not authorization:
                    auth_state = "missing"
                elif "${" in authorization or "{{" in authorization:
                    auth_state = "placeholder"
                elif hmac.compare_digest(authorization, f"Bearer {self.server.adapter.config.token}"):
                    auth_state = "valid"
                else:
                    auth_state = "invalid"
                self.log_message("MCP authorization state=%s", auth_state)
            if length > self.server.adapter.config.max_upload_bytes + 2 * 1024 * 1024:
                response = SubmitFlowAdapter._json_response(413, {"error": {"code": "payload_too_large", "message": "Request body is too large."}})
            else:
                body = self.rfile.read(length) if length else b""
                response = self.server.adapter.handle(method, self.path, self.headers, body)
        except (ConnectionError, OSError):
            return
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (response.headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep request logs free of authorization headers and request bodies.
        super().log_message("%s", format % args)


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "submit_flow.create_task",
        "description": "Create an authenticated Submit Flow task using a server-committed pricing snapshot.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["site_key", "month"],
            "properties": {
                "site_key": {"type": "string", "minLength": 1, "maxLength": 128},
                "month": {"type": "string", "pattern": MONTH_PATTERN.pattern},
                "task_id": {"type": "string", "pattern": TASK_ID_PATTERN.pattern},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
            },
        },
    },
    {
        "name": "submit_flow.attach_file",
        "description": "Attach a previously authorized PDF attachment to the current user's Submit Flow task.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id", "attachment_id"],
            "properties": {
                "task_id": {"type": "string", "pattern": TASK_ID_PATTERN.pattern},
                "attachment_id": {"type": "string", "pattern": ATTACHMENT_ID_PATTERN.pattern},
                "file_type": {"type": "string", "enum": list(REQUIRED_FILE_TYPES)},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
            },
        },
    },
    {
        "name": "submit_flow.run_task",
        "description": "Run the bound Submit Flow task through the deterministic business engine.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string", "pattern": TASK_ID_PATTERN.pattern},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
            },
        },
    },
    {
        "name": "submit_flow.get_task",
        "description": "Read the current structured status for the authenticated user's task.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string", "pattern": TASK_ID_PATTERN.pattern}},
        },
    },
    {
        "name": "submit_flow.get_review",
        "description": "Read the current review_report fields and evidence for a task needing review.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string", "pattern": TASK_ID_PATTERN.pattern}},
        },
    },
    {
        "name": "submit_flow.confirm_task",
        "description": "Submit field-level confirmations; the adapter creates the controlled confirmation file and reruns the task.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id", "field_confirmations"],
            "properties": {
                "task_id": {"type": "string", "pattern": TASK_ID_PATTERN.pattern},
                "field_confirmations": {"type": "object", "minProperties": 1, "maxProperties": 64},
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128},
            },
        },
    },
    {
        "name": "submit_flow.list_outputs",
        "description": "List authorized result artifacts and opaque download URLs for a task.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["task_id"],
            "properties": {"task_id": {"type": "string", "pattern": TASK_ID_PATTERN.pattern}},
        },
    },
]


def _resolve_project_path(project_root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate.resolve() if candidate.is_absolute() else (project_root / candidate).resolve()


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if str(key).casefold() == name.casefold():
            return str(value).strip()
    return ""


def _required_header(headers: Mapping[str, str], name: str) -> str:
    value = _header(headers, name)
    if not value:
        raise AdapterError(f"Missing required header: {name}.", code="missing_identity", status=400)
    return value


def _bounded_header(headers: Mapping[str, str], name: str, *, required: bool) -> str | None:
    value = _header(headers, name)
    if not value and required:
        raise AdapterError(f"Missing required header: {name}.", code="missing_identity", status=400)
    if len(value) > 256:
        raise AdapterError(f"Header {name} is too long.", code="invalid_identity", status=400)
    return value or None


def _content_length(headers: Mapping[str, str]) -> int:
    value = _header(headers, "Content-Length") or "0"
    try:
        length = int(value)
    except ValueError as exc:
        raise AdapterError("Content-Length must be an integer.", code="invalid_request", status=400) from exc
    if length < 0:
        raise AdapterError("Content-Length must not be negative.", code="invalid_request", status=400)
    return length


def _parse_json_object(body: bytes, *, max_bytes: int) -> dict[str, Any]:
    if len(body) > max_bytes:
        raise AdapterError("JSON request is too large.", code="payload_too_large", status=413)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("Request body must be valid UTF-8 JSON.", code="invalid_json", status=400) from exc
    if not isinstance(payload, dict):
        raise AdapterError("Request body must be a JSON object.", code="invalid_json", status=400)
    return payload


def _parse_multipart(content_type: str, body: bytes) -> dict[str, Any]:
    message = BytesParser(policy=email_default_policy).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if not message.is_multipart():
        raise AdapterError("Malformed multipart body.", code="invalid_request", status=400)
    result: dict[str, Any] = {}
    for part in message.iter_parts():
        disposition = part.get_content_disposition()
        name = part.get_param("name", header="content-disposition")
        if disposition != "form-data" or not isinstance(name, str):
            continue
        data = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            if name in result:
                raise AdapterError("Only one file field is accepted.", code="invalid_request", status=422)
            result[name] = {"filename": filename, "data": data}
        elif name not in result:
            try:
                result[name] = data.decode(part.get_content_charset() or "utf-8")
            except UnicodeDecodeError:
                raise AdapterError("Multipart text field is not valid UTF-8.", code="invalid_request", status=400)
    return result


def _required_string(arguments: Mapping[str, Any], name: str, *, max_length: int) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise AdapterError(f"Argument {name} must be a non-empty string of at most {max_length} characters.", code="invalid_arguments", status=422)
    return value.strip()


def _optional_string(arguments: Mapping[str, Any], name: str, *, max_length: int) -> str | None:
    if name not in arguments or arguments[name] is None:
        return None
    if not isinstance(arguments[name], str) or not arguments[name].strip() or len(arguments[name]) > max_length:
        raise AdapterError(f"Argument {name} must be a non-empty string of at most {max_length} characters.", code="invalid_arguments", status=422)
    return arguments[name].strip()


def _required_month(arguments: Mapping[str, Any], name: str) -> str:
    value = _required_string(arguments, name, max_length=7)
    if not MONTH_PATTERN.fullmatch(value):
        raise AdapterError(f"Argument {name} must use YYYY-MM format.", code="invalid_arguments", status=422)
    return value


def _reject_extra_arguments(arguments: Mapping[str, Any], properties: Mapping[str, Any], tool_name: str) -> None:
    extra = sorted(set(arguments) - set(properties))
    if extra:
        raise AdapterError(f"Tool {tool_name} received unknown arguments: {', '.join(extra)}.", code="invalid_arguments", status=422)


def _safe_task_id(value: Any) -> str:
    if not isinstance(value, str) or not TASK_ID_PATTERN.fullmatch(value):
        raise AdapterError("Invalid task ID.", code="invalid_arguments", status=422)
    return value


def _safe_attachment_id(value: Any) -> str:
    if not isinstance(value, str) or not ATTACHMENT_ID_PATTERN.fullmatch(value):
        raise AdapterError("Invalid attachment ID.", code="invalid_arguments", status=422)
    return value


def _safe_filename(value: str) -> str:
    name = Path(value).name
    if not name or name in {".", ".."} or "\x00" in name or any(ord(char) < 32 for char in name) or len(name) > 255:
        raise AdapterError("Invalid attachment filename.", code="invalid_arguments", status=422)
    return name


def _validate_confirmation_shape(fields: Mapping[str, Any]) -> None:
    for field, patch in fields.items():
        if not isinstance(field, str) or not field or len(field) > 128 or not isinstance(patch, dict):
            raise AdapterError("Each field confirmation must be an object keyed by a field name.", code="invalid_arguments", status=422)
        if set(patch) - {"value", "reason"} or "value" not in patch:
            raise AdapterError(f"Invalid confirmation payload for field {field}.", code="invalid_arguments", status=422)
        if isinstance(patch["value"], (dict, list, bool)) or patch["value"] is None:
            raise AdapterError(f"Confirmation value for field {field} must be a string or number.", code="invalid_arguments", status=422)
        if isinstance(patch["value"], float) and not math.isfinite(patch["value"]):
            raise AdapterError(f"Confirmation value for field {field} must be finite.", code="invalid_arguments", status=422)
        if "reason" in patch and (not isinstance(patch["reason"], str) or not patch["reason"].strip() or len(patch["reason"]) > 1000):
            raise AdapterError(f"Confirmation reason for field {field} is invalid.", code="invalid_arguments", status=422)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_if_exists(path: Path) -> Any:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AdapterError("Stored integration record is invalid.", code="storage_invalid", status=500) from exc


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_server(config: ServiceConfig, *, host: str, port: int) -> SubmitFlowHTTPServer:
    return SubmitFlowHTTPServer((host, port), SubmitFlowAdapter(config))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="submit-flow-mcp")
    parser.add_argument("--host", default=os.environ.get("SUBMIT_MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SUBMIT_MCP_PORT", "8121")))
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    config = ServiceConfig.from_environment(project_root=args.project_root)
    if not config.token:
        parser.error("SUBMIT_MCP_TOKEN is required")
    server = build_server(config, host=args.host, port=args.port)
    readiness = server.adapter._health("/readyz")
    startup_event = {
        "event": "service_started",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "listen": {"host": args.host, "port": args.port},
        "mcp_protocol": MCP_PROTOCOL_VERSION,
        "ready": readiness.status == 200,
    }
    print(json.dumps(startup_event, ensure_ascii=False, separators=(",", ":")), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
