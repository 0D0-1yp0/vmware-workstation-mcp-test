"""End-to-end MCP stdio protocol tests.

- Default test runs offline in any CI environment (no VMware required): verifies
  clean JSON-RPC stdio framing, exact tool surface (22 tools, no deleteVM), and
  safety guardrail enforcement over the wire.
- Live read-only test runs only when `RUN_LIVE_VMWARE_TESTS=1` is explicitly set.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _REPO_ROOT / "src"


def _build_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_SRC_DIR}{os.pathsep}{existing}" if existing else str(_SRC_DIR)
    return env


class TestMCPStdioProtocol(unittest.TestCase):
    def test_offline_stdio_handshake_tools_and_guardrails(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def _run() -> dict:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-u", "-m", "vmware_mcp"],
                env=_build_env(),
            )
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools_resp = await session.list_tools()
                    tool_names = [t.name for t in tools_resp.tools]

                    guard_res = await session.call_tool(
                        "delete_snapshot",
                        {"vm": "dummy-vm", "name": "snap1", "confirm": False},
                    )
                    guard_data = json.loads(guard_res.content[0].text)
                    return {"tool_names": tool_names, "guard": guard_data}

        result = asyncio.run(_run())
        tool_names = result["tool_names"]
        self.assertEqual(len(tool_names), 22, f"Expected 22 tools, got {len(tool_names)}: {tool_names}")
        self.assertNotIn("delete_vm", tool_names)
        self.assertNotIn("deleteVM", tool_names)
        self.assertFalse(result["guard"]["ok"])
        self.assertIn("confirm=True", result["guard"]["error"])

    @unittest.skipUnless(
        os.environ.get("RUN_LIVE_VMWARE_TESTS", "").strip() == "1",
        "Set RUN_LIVE_VMWARE_TESTS=1 to run read-only live VMware Workstation checks.",
    )
    def test_live_readonly_tools(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def _run() -> dict:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-u", "-m", "vmware_mcp"],
                env=_build_env(),
            )
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    vms_res = await session.call_tool("list_vms", {"include_specs": True})
                    running_res = await session.call_tool("list_running_vms", {})
                    nets_res = await session.call_tool("list_host_networks", {})
                    return {
                        "vms": json.loads(vms_res.content[0].text),
                        "running": json.loads(running_res.content[0].text),
                        "networks": json.loads(nets_res.content[0].text),
                    }

        result = asyncio.run(_run())
        self.assertTrue(result["vms"]["ok"])
        self.assertTrue(result["running"]["ok"])
        self.assertTrue(result["networks"]["ok"])


if __name__ == "__main__":
    unittest.main()
