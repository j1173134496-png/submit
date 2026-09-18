from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from submit_flow_agent.http_service import ServiceConfig, SubmitFlowAdapter
from submit_flow_agent.task_store import load_task, write_task
from tests.pricing_helpers import create_task_with_pricing


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "sites.json"


class HttpServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Path(tempfile.mkdtemp()) / "runtime"
        self.config = ServiceConfig(
            runtime_root=self.runtime,
            config_path=CONFIG_PATH,
            token="test-service-token",
            tenant_id="tenant-test",
            agent_id="smart-submit-v1",
            agent_version="1.0.0",
            max_upload_bytes=1024 * 1024,
        )
        self.adapter = SubmitFlowAdapter(self.config)
        self.headers = {
            "Authorization": "Bearer test-service-token",
            "X-AgentOS-Tenant-Id": "tenant-test",
            "X-AgentOS-Agent-Id": "smart-submit-v1",
            "X-AgentOS-Agent-Version": "1.0.0",
            "X-LibreChat-User-Id": "user-a",
            "X-LibreChat-User-Role": "employee",
            "X-LibreChat-User-Email": "user-a@example.test",
            "X-AgentOS-Conversation-Id": "conversation-a",
        }

    def test_health_and_mcp_authentication(self) -> None:
        health = self.adapter.handle("GET", "/healthz", {})
        self.assertEqual(health.status, 200)
        unauthorized = self.adapter.handle("POST", "/mcp", {}, b"{}")
        self.assertEqual(unauthorized.status, 401)

        initialized = self._mcp("initialize", {}, request_id=1)
        self.assertEqual(initialized["protocolVersion"], "2025-03-26")
        self.assertIn("MCP-Session-Id", self._last_headers)
        tools = self._mcp("tools/list", {}, request_id=2)
        names = {item["name"] for item in tools["tools"]}
        self.assertEqual(
            names,
            {
                "submit_flow.create_task",
                "submit_flow.attach_file",
                "submit_flow.run_task",
                "submit_flow.get_task",
                "submit_flow.get_review",
                "submit_flow.confirm_task",
                "submit_flow.list_outputs",
            },
        )
        serialized = json.dumps(tools, ensure_ascii=False)
        for forbidden in ("runtime_root", "command", "shell", "python", "sql"):
            self.assertNotIn(forbidden, serialized)

    def test_streamable_http_optional_sse_and_termination_are_explicitly_unsupported(self) -> None:
        capability_probe = self.adapter.handle("HEAD", "/mcp", {})
        self.assertEqual(capability_probe.status, 405)
        self.assertEqual(capability_probe.headers["Allow"], "POST")

        sse_probe = self.adapter.handle("GET", "/mcp", self.headers)
        self.assertEqual(sse_probe.status, 405)
        self.assertEqual(sse_probe.headers["Allow"], "POST")

        termination = self.adapter.handle("DELETE", "/mcp", self.headers)
        self.assertEqual(termination.status, 405)
        self.assertEqual(termination.headers["Allow"], "POST")

    def test_create_is_pricing_gated_and_idempotent(self) -> None:
        first = self._call_tool("submit_flow.create_task", {"site_key": "xinan_high_school", "month": "2026-05"})
        self.assertEqual(first["status"], "pricing_required")
        self.assertFalse((self.runtime / "tasks").exists())

        create_task_with_pricing(self.runtime, task_id="pricing-seed", month="2026-05")
        created = self._call_tool("submit_flow.create_task", {"site_key": "xinan_high_school", "month": "2026-05"})
        self.assertEqual(created["status"], "collecting_files")
        task_id = str(created["task_id"])
        replayed = self._call_tool("submit_flow.create_task", {"site_key": "xinan_high_school", "month": "2026-05"})
        self.assertEqual(replayed["task_id"], task_id)

        other_headers = {**self.headers, "X-LibreChat-User-Id": "user-b", "X-AgentOS-Conversation-Id": "conversation-b"}
        response = self.adapter.handle(
            "POST",
            "/mcp",
            other_headers,
            self._rpc("tools/call", {"name": "submit_flow.get_task", "arguments": {"task_id": task_id}}, request_id=3),
        )
        result = json.loads(response.body.decode("utf-8"))["result"]
        self.assertTrue(result["isError"])
        self.assertIn("forbidden", json.dumps(result, ensure_ascii=False))

    def test_attachment_is_pdf_bound_to_owner_and_task(self) -> None:
        create_task_with_pricing(self.runtime, task_id="pricing-seed", month="2026-05")
        created = self._call_tool("submit_flow.create_task", {"site_key": "xinan_high_school", "month": "2026-05"})
        task_id = str(created["task_id"])

        body, content_type = _multipart_pdf("福域发电单202605-2.pdf")
        uploaded_response = self.adapter.handle(
            "POST",
            "/v1/attachments",
            {**self.headers, "Content-Type": content_type, "Content-Length": str(len(body))},
            body,
        )
        self.assertEqual(uploaded_response.status, 201)
        uploaded = json.loads(uploaded_response.body.decode("utf-8"))
        attachment_id = uploaded["attachment_id"]

        attached = self._call_tool(
            "submit_flow.attach_file",
            {"task_id": task_id, "attachment_id": attachment_id, "file_type": "generation_statement"},
        )
        self.assertEqual(attached["status"], "attached")
        self.assertEqual(attached["task"]["received_count"], 1)
        self.assertFalse(attached["task"]["files"]["generation_statement"])

        other_headers = {**self.headers, "X-LibreChat-User-Id": "user-b", "X-AgentOS-Conversation-Id": "conversation-b"}
        cross_user = self.adapter.handle(
            "POST",
            "/mcp",
            other_headers,
            self._rpc(
                "tools/call",
                {"name": "submit_flow.attach_file", "arguments": {"task_id": task_id, "attachment_id": attachment_id}},
                request_id=4,
            ),
        )
        self.assertTrue(json.loads(cross_user.body.decode("utf-8"))["result"]["isError"])

    def test_tool_rejects_extra_fields_and_artifact_path_traversal(self) -> None:
        response = self.adapter.handle(
            "POST",
            "/mcp",
            self.headers,
            self._rpc(
                "tools/call",
                {"name": "submit_flow.get_task", "arguments": {"task_id": "task-1", "runtime_root": "C:/secret"}},
                request_id=5,
            ),
        )
        payload = json.loads(response.body.decode("utf-8"))
        self.assertTrue(payload["result"]["isError"])
        self.assertIn("unknown arguments", payload["result"]["content"][0]["text"])

        create_task_with_pricing(self.runtime, task_id="pricing-seed", month="2026-05")
        created = self._call_tool("submit_flow.create_task", {"site_key": "xinan_high_school", "month": "2026-05"})
        task_id = str(created["task_id"])
        task_root = self.runtime / "tasks" / task_id
        task = load_task(task_root)
        task["status"] = "completed"
        task["outputs"] = ["outputs/result.txt"]
        (task_root / "outputs").mkdir(parents=True, exist_ok=True)
        (task_root / "outputs" / "result.txt").write_text("controlled-result", encoding="utf-8")
        write_task(task_root, task)
        listed = self._call_tool("submit_flow.list_outputs", {"task_id": task_id})
        self.assertEqual(len(listed["artifacts"]), 1)
        artifact = listed["artifacts"][0]
        self.assertNotIn("_path", artifact)
        download = self.adapter.handle(
            "GET",
            f"/v1/tasks/{task_id}/artifacts/{artifact['artifact_id']}",
            self.headers,
        )
        self.assertEqual(download.status, 200)
        self.assertEqual(download.body, b"controlled-result")

        unsafe_index = task_root / "artifact_index.json"
        unsafe_index.write_text(json.dumps({"artifacts": [{"path": "../outside.txt"}]}), encoding="utf-8")
        with self.assertRaises(Exception):
            self._call_tool("submit_flow.list_outputs", {"task_id": task_id})

    def _call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        response = self.adapter.handle("POST", "/mcp", self.headers, self._rpc("tools/call", {"name": name, "arguments": arguments}, request_id=10))
        payload = json.loads(response.body.decode("utf-8"))
        result = payload["result"]
        if result.get("isError"):
            self.fail(result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def _mcp(self, method: str, params: dict[str, object], *, request_id: int) -> dict[str, object]:
        response = self.adapter.handle("POST", "/mcp", self.headers, self._rpc(method, params, request_id=request_id))
        self._last_headers = dict(response.headers or {})
        return json.loads(response.body.decode("utf-8"))["result"]

    @staticmethod
    def _rpc(method: str, params: dict[str, object], *, request_id: int) -> bytes:
        return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode("utf-8")


def _multipart_pdf(filename: str) -> tuple[bytes, str]:
    boundary = "----submit-test-boundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8") + b"%PDF-1.7\ncontrolled-test-pdf\n" + f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"


if __name__ == "__main__":
    unittest.main()
