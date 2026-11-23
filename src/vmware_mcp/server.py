"""VMware Workstation / Fusion MCP Server (Stdio transport via official FastMCP).

Exposes 22 focused tools for VM discovery, power lifecycle, snapshots, guest OS
automation, host networking/NAT forwarding, shared folders, screen capture, and
cloning. All diagnostics are written exclusively to sys.stderr so stdout remains
clean for MCP JSON-RPC framing.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

# CRITICAL: MCP stdio transport owns sys.stdout. All logging MUST go to sys.stderr.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
    force=True,
)
logger = logging.getLogger("vmware-mcp.server")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from .client import VMwareClient, VMwareError, parse_vmx_metadata  # noqa: E402

mcp = FastMCP("vmware-mcp")
_client: Optional[VMwareClient] = None


def get_client() -> VMwareClient:
    """Return a lazily-initialized singleton VMwareClient."""
    global _client
    if _client is None:
        _client = VMwareClient()
    return _client


def _error_response(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, VMwareError):
        return {"ok": False, "error": str(exc), "details": exc.details}
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\s]+', "_", name).strip("_")
    return cleaned or "vm"


# =============================================================================
# 1. Asset Discovery & Status (4 read-only tools)
# =============================================================================


@mcp.tool()
def list_vms(include_specs: bool = True) -> dict[str, Any]:
    """List all discovered VMware VMs (from inventory, VM folders, and running state).

    Args:
        include_specs: Include parsed .vmx specs (guest OS, vCPUs, memory MB, NICs).
    """
    try:
        client = get_client()
        vms = client.discover_vms(include_specs=include_specs)
        return {
            "ok": True,
            "vmrun_path": str(client.vmrun_path),
            "count": len(vms),
            "running_count": sum(1 for v in vms if v["running"]),
            "vms": vms,
        }
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def list_running_vms() -> dict[str, Any]:
    """List all currently running VMware VMs via `vmrun list`."""
    try:
        client = get_client()
        running = client.list_running_vmx_paths()
        items = []
        for p in running:
            vmx = Path(p)
            meta = parse_vmx_metadata(vmx)
            items.append(
                {
                    "name": meta["display_name"],
                    "folder_name": vmx.parent.name,
                    "vmx_path": str(vmx),
                }
            )
        return {"ok": True, "count": len(items), "running_vms": items}
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def get_vm_info(vm: str) -> dict[str, Any]:
    """Get detailed info for a VM: .vmx specs, running status, VMware Tools state, and guest IP.

    Args:
        vm: VM display name, folder name, unique substring, or full .vmx path.
    """
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        meta = parse_vmx_metadata(vmx)
        running_norm = {os.path.normcase(os.path.abspath(p)) for p in client.list_running_vmx_paths()}
        is_running = os.path.normcase(str(vmx)) in running_norm

        tools_res = client.run_vmrun(["checkToolsState", str(vmx)], check=False, timeout=20)
        tools_state = tools_res["stdout"] if tools_res["ok"] else (tools_res["stderr"] or "unknown")

        guest_ip: Optional[str] = None
        if is_running:
            ip_res = client.run_vmrun(["getGuestIPAddress", str(vmx)], check=False, timeout=15)
            if ip_res["ok"] and ip_res["stdout"]:
                guest_ip = ip_res["stdout"].splitlines()[-1].strip()

        return {
            "ok": True,
            "name": meta["display_name"],
            "folder_name": vmx.parent.name,
            "vmx_path": str(vmx),
            "running": is_running,
            "tools_state": tools_state,
            "guest_ip": guest_ip,
            "specs": meta,
        }
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def get_guest_ip(vm: str, wait: bool = False) -> dict[str, Any]:
    """Query the guest OS IP address of a running VM (requires VMware Tools).

    Args:
        vm: VM display name, folder name, unique substring, or full .vmx path.
        wait: If True, wait until an IP address becomes available (up to 60s).
    """
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        args = ["getGuestIPAddress", str(vmx)]
        if wait:
            args.append("-wait")
        res = client.run_vmrun(args, timeout=60 if wait else 20)
        ip = res["stdout"].splitlines()[-1].strip() if res["stdout"] else ""
        return {"ok": True, "vmx_path": str(vmx), "guest_ip": ip}
    except Exception as exc:
        return _error_response(exc)


# =============================================================================
# 2. Power Lifecycle (4 tools)
# =============================================================================


@mcp.tool()
def start_vm(vm: str, gui: bool = True) -> dict[str, Any]:
    """Start a VM in GUI or headless (nogui) mode.

    Args:
        vm: VM name, substring, or full .vmx path.
        gui: True to open the VMware window; False for background headless mode.
    """
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        mode = "gui" if gui else "nogui"
        res = client.run_vmrun(["start", str(vmx), mode], timeout=90)
        return {"ok": True, "action": "start", "mode": mode, "vmx_path": str(vmx), "output": res["stdout"]}
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def stop_vm(vm: str, hard: bool = False, confirm: bool = False) -> dict[str, Any]:
    """Stop (power off) a running VM.

    Args:
        vm: VM name, substring, or full .vmx path.
        hard: False for graceful soft shutdown; True for immediate hard power-off.
        confirm: Must be True when hard=True (safety guardrail against data loss).
    """
    if hard and not confirm:
        return {
            "ok": False,
            "error": "Safety guardrail: hard power-off can corrupt guest filesystems. Re-call with confirm=True to proceed.",
        }
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        mode = "hard" if hard else "soft"
        res = client.run_vmrun(["stop", str(vmx), mode], timeout=90)
        return {"ok": True, "action": "stop", "mode": mode, "vmx_path": str(vmx), "output": res["stdout"]}
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def reset_vm(vm: str, hard: bool = False, confirm: bool = False) -> dict[str, Any]:
    """Reset (reboot) a running VM.

    Args:
        vm: VM name, substring, or full .vmx path.
        hard: False for graceful guest reboot; True for immediate hard reset.
        confirm: Must be True when hard=True (safety guardrail).
    """
    if hard and not confirm:
        return {
            "ok": False,
            "error": "Safety guardrail: hard reset can corrupt guest state. Re-call with confirm=True to proceed.",
        }
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        mode = "hard" if hard else "soft"
        res = client.run_vmrun(["reset", str(vmx), mode], timeout=90)
        return {"ok": True, "action": "reset", "mode": mode, "vmx_path": str(vmx), "output": res["stdout"]}
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def power_state_vm(vm: str, action: Literal["suspend", "pause", "unpause"]) -> dict[str, Any]:
    """Suspend, pause, or unpause a VM.

    Args:
        vm: VM name, substring, or full .vmx path.
        action: One of 'suspend', 'pause', or 'unpause'.
    """
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        res = client.run_vmrun([action, str(vmx)], timeout=90)
        return {"ok": True, "action": action, "vmx_path": str(vmx), "output": res["stdout"]}
    except Exception as exc:
        return _error_response(exc)


# =============================================================================
# 3. Snapshot Management (4 tools)
# =============================================================================


@mcp.tool()
def list_snapshots(vm: str, show_tree: bool = True) -> dict[str, Any]:
    """List snapshots for a VM.

    Args:
        vm: VM name, substring, or full .vmx path.
        show_tree: Return indented snapshot hierarchy when True.
    """
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        args = ["listSnapshots", str(vmx)]
        if show_tree:
            args.append("showTree")
        res = client.run_vmrun(args, timeout=30)
        lines = res["stdout"].splitlines()
        total = 0
        snapshots: list[str] = []
        for idx, line in enumerate(lines):
            if idx == 0 and line.lower().startswith("total snapshots:"):
                num = line.split(":", 1)[1].strip()
                if num.isdigit():
                    total = int(num)
                continue
            if line.strip():
                snapshots.append(line)
        return {
            "ok": True,
            "vmx_path": str(vmx),
            "total": total or len(snapshots),
            "show_tree": show_tree,
            "snapshots": snapshots,
        }
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def create_snapshot(vm: str, name: str) -> dict[str, Any]:
    """Create a named snapshot of a VM.

    Args:
        vm: VM name, substring, or full .vmx path.
        name: Snapshot name.
    """
    if not name.strip():
        return {"ok": False, "error": "Snapshot name cannot be empty."}
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        res = client.run_vmrun(["snapshot", str(vmx), name.strip()], timeout=300)
        return {"ok": True, "action": "snapshot", "vmx_path": str(vmx), "snapshot_name": name.strip(), "output": res["stdout"]}
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def revert_to_snapshot(vm: str, name: str, confirm: bool = False) -> dict[str, Any]:
    """Revert a VM back to a named snapshot (discards unsaved current state).

    Args:
        vm: VM name, substring, or full .vmx path.
        name: Exact snapshot name to revert to.
        confirm: Must be True to confirm discarding current VM state.
    """
    if not confirm:
        return {
            "ok": False,
            "error": "Safety guardrail: reverting to a snapshot discards current unsaved VM state. Re-call with confirm=True to proceed.",
        }
    if not name.strip():
        return {"ok": False, "error": "Snapshot name cannot be empty."}
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        res = client.run_vmrun(["revertToSnapshot", str(vmx), name.strip()], timeout=180)
        return {"ok": True, "action": "revertToSnapshot", "vmx_path": str(vmx), "snapshot_name": name.strip(), "output": res["stdout"]}
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def delete_snapshot(
    vm: str,
    name: str,
    delete_children: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete a named snapshot from a VM.

    Args:
        vm: VM name, substring, or full .vmx path.
        name: Exact snapshot name to remove.
        delete_children: Also delete child snapshots when True.
        confirm: Must be True to confirm permanent snapshot deletion.
    """
    if not confirm:
        return {
            "ok": False,
            "error": "Safety guardrail: deleting a snapshot is irreversible. Re-call with confirm=True to proceed.",
        }
    if not name.strip():
        return {"ok": False, "error": "Snapshot name cannot be empty."}
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        args = ["deleteSnapshot", str(vmx), name.strip()]
        if delete_children:
            args.append("andDeleteChildren")
        res = client.run_vmrun(args, timeout=300)
        return {
            "ok": True,
            "action": "deleteSnapshot",
            "vmx_path": str(vmx),
            "snapshot_name": name.strip(),
            "delete_children": delete_children,
            "output": res["stdout"],
        }
    except Exception as exc:
        return _error_response(exc)


# =============================================================================
# 4. Guest OS Operations & Diagnostics (6 tools, requires VMware Tools)
# =============================================================================


@mcp.tool()
def run_command_in_guest(
    vm: str,
    program_path: str,
    args: Optional[list[str]] = None,
    guest_user: str = "",
    guest_password: str = "",
    no_wait: bool = False,
    interactive: bool = False,
    timeout: int = 60,
) -> dict[str, Any]:
    """Run an executable inside the guest OS via VMware Tools (`runProgramInGuest`).

    Args:
        vm: VM name, substring, or full .vmx path.
        program_path: Absolute path to the executable inside the guest OS.
        args: Optional list of program arguments.
        guest_user: Guest OS username.
        guest_password: Guest OS password (masked in logs/responses).
        no_wait: Return immediately without waiting for the program to exit.
        interactive: Launch in the interactive desktop session.
        timeout: Subprocess timeout in seconds.
    """
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        cmd_args = ["runProgramInGuest", str(vmx)]
        if no_wait:
            cmd_args.append("-noWait")
        if interactive:
            cmd_args.append("-interactive")
        cmd_args.append(program_path)
        if args:
            cmd_args.extend(args)
        res = client.run_vmrun(
            cmd_args,
            guest_user=guest_user,
            guest_password=guest_password,
            timeout=timeout,
        )
        return {"ok": True, "vmx_path": str(vmx), "stdout": res["stdout"], "stderr": res["stderr"]}
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def run_script_in_guest(
    vm: str,
    interpreter: str,
    script_text: str,
    guest_user: str = "",
    guest_password: str = "",
    no_wait: bool = False,
    timeout: int = 60,
) -> dict[str, Any]:
    """Execute inline script text inside the guest OS (`runScriptInGuest`).

    Note: vmrun itself does not stream guest stdout back to the host; redirect
    output to a file inside the guest and retrieve it with `copy_file_guest_to_host`
    if you need full stdout capture.

    Args:
        vm: VM name, substring, or full .vmx path.
        interpreter: Interpreter absolute path in guest (e.g. '/bin/bash' or 'C:\\Windows\\System32\\cmd.exe').
        script_text: Script body to execute.
        guest_user: Guest OS username.
        guest_password: Guest OS password (masked in logs/responses).
        no_wait: Return immediately without waiting for completion.
        timeout: Subprocess timeout in seconds.
    """
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        cmd_args = ["runScriptInGuest", str(vmx)]
        if no_wait:
            cmd_args.append("-noWait")
        cmd_args.extend([interpreter, script_text])
        res = client.run_vmrun(
            cmd_args,
            guest_user=guest_user,
            guest_password=guest_password,
            timeout=timeout,
        )
        return {"ok": True, "vmx_path": str(vmx), "stdout": res["stdout"], "stderr": res["stderr"]}
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def list_processes_in_guest(
    vm: str,
    guest_user: str = "",
    guest_password: str = "",
) -> dict[str, Any]:
    """List running processes inside the guest OS (`listProcessesInGuest`).

    Args:
        vm: VM name, substring, or full .vmx path.
        guest_user: Guest OS username.
        guest_password: Guest OS password (masked in logs/responses).
    """
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        res = client.run_vmrun(
            ["listProcessesInGuest", str(vmx)],
            guest_user=guest_user,
            guest_password=guest_password,
            timeout=30,
        )
        lines = [line.strip() for line in res["stdout"].splitlines() if line.strip()]
        processes = [line for line in lines if not line.lower().startswith("process list:")]
        return {"ok": True, "vmx_path": str(vmx), "count": len(processes), "processes": processes}
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def copy_file_host_to_guest(
    vm: str,
    host_path: str,
    guest_path: str,
    guest_user: str = "",
    guest_password: str = "",
) -> dict[str, Any]:
    """Copy a file from the host to the guest OS (`CopyFileFromHostToGuest`).

    Args:
        vm: VM name, substring, or full .vmx path.
        host_path: Existing source file path on the host.
        guest_path: Destination file path inside the guest OS.
        guest_user: Guest OS username.
        guest_password: Guest OS password (masked in logs/responses).
    """
    src = Path(host_path)
    if not src.is_file():
        return {"ok": False, "error": f"Host file does not exist: {host_path}"}
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        res = client.run_vmrun(
            ["CopyFileFromHostToGuest", str(vmx), str(src.resolve()), guest_path],
            guest_user=guest_user,
            guest_password=guest_password,
            timeout=120,
        )
        return {
            "ok": True,
            "vmx_path": str(vmx),
            "host_path": str(src.resolve()),
            "guest_path": guest_path,
            "output": res["stdout"],
        }
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def copy_file_guest_to_host(
    vm: str,
    guest_path: str,
    host_path: str,
    guest_user: str = "",
    guest_password: str = "",
) -> dict[str, Any]:
    """Copy a file from the guest OS to the host (`CopyFileFromGuestToHost`).

    Args:
        vm: VM name, substring, or full .vmx path.
        guest_path: Source file path inside the guest OS.
        host_path: Destination file path on the host (parent dirs created automatically).
        guest_user: Guest OS username.
        guest_password: Guest OS password (masked in logs/responses).
    """
    try:
        dst = Path(host_path).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
        client = get_client()
        vmx = client.resolve_vmx(vm)
        res = client.run_vmrun(
            ["CopyFileFromGuestToHost", str(vmx), guest_path, str(dst)],
            guest_user=guest_user,
            guest_password=guest_password,
            timeout=120,
        )
        return {
            "ok": True,
            "vmx_path": str(vmx),
            "guest_path": guest_path,
            "host_path": str(dst),
            "size_bytes": dst.stat().st_size if dst.is_file() else 0,
            "output": res["stdout"],
        }
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def capture_vm_screen(vm: str, output_path: str = "") -> dict[str, Any]:
    """Capture the current display of a running VM to a local PNG file (`captureScreen`).

    Args:
        vm: VM name, substring, or full .vmx path.
        output_path: Optional host PNG file path. Defaults to <temp>/vmware-mcp-screens/<vm>_<ts>.png.
    """
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        if output_path.strip():
            dst = Path(output_path).resolve()
        else:
            out_dir = Path(tempfile.gettempdir()) / "vmware-mcp-screens"
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dst = out_dir / f"{_safe_filename(vmx.stem)}_{ts}.png"
        dst.parent.mkdir(parents=True, exist_ok=True)
        res = client.run_vmrun(["captureScreen", str(vmx), str(dst)], timeout=30)
        if not dst.is_file() or dst.stat().st_size == 0:
            return {
                "ok": False,
                "error": "captureScreen completed but PNG output file was not created or is empty.",
                "output": res["stdout"],
            }
        return {
            "ok": True,
            "vmx_path": str(vmx),
            "screenshot_path": str(dst),
            "size_bytes": dst.stat().st_size,
        }
    except Exception as exc:
        return _error_response(exc)


# =============================================================================
# 5. Host Networks, NAT Port Forwarding & Shared Folders (3 tools)
# =============================================================================


@mcp.tool()
def list_host_networks() -> dict[str, Any]:
    """List VMware host virtual networks (vmnet*) and their NAT port forwarding rules."""
    try:
        client = get_client()
        res = client.run_vmrun(["listHostNetworks"], timeout=30)
        raw_lines = [line.rstrip() for line in res["stdout"].splitlines() if line.strip()]
        networks: list[dict[str, Any]] = []
        for line in raw_lines:
            low = line.lower()
            if low.startswith("total host networks:") or low.startswith("index") or set(line.strip()) <= {"-", " "}:
                continue
            parts = line.split()
            if len(parts) >= 2:
                net_name = next((p for p in parts if p.lower().startswith("vmnet")), parts[1])
                entry: dict[str, Any] = {"name": net_name, "raw": line}
                if "nat" in low:
                    pf = client.run_vmrun(["listPortForwardings", net_name], check=False, timeout=15)
                    entry["port_forwardings"] = pf["stdout"].splitlines() if pf["ok"] else []
                networks.append(entry)
        return {"ok": True, "raw": res["stdout"], "networks": networks}
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def manage_port_forwarding(
    network: str,
    action: Literal["set", "delete"],
    protocol: Literal["tcp", "udp"],
    host_port: int,
    guest_ip: str = "",
    guest_port: int = 0,
    description: str = "",
) -> dict[str, Any]:
    """Add/update or delete a NAT port forwarding rule on a VMware host network (e.g. vmnet8).

    Args:
        network: Host network name (e.g. 'vmnet8').
        action: 'set' to add/update, 'delete' to remove.
        protocol: 'tcp' or 'udp'.
        host_port: Host TCP/UDP port number (1-65535).
        guest_ip: Guest IP address (required when action='set').
        guest_port: Guest port number 1-65535 (required when action='set').
        description: Optional rule description (only for action='set').
    """
    if not (1 <= host_port <= 65535):
        return {"ok": False, "error": "host_port must be between 1 and 65535."}
    try:
        client = get_client()
        if action == "set":
            if not guest_ip.strip() or not (1 <= guest_port <= 65535):
                return {"ok": False, "error": "guest_ip and valid guest_port (1-65535) are required when action='set'."}
            cmd_args = ["setPortForwarding", network, protocol, str(host_port), guest_ip.strip(), str(guest_port)]
            if description.strip():
                cmd_args.append(description.strip())
        else:
            cmd_args = ["deletePortForwarding", network, protocol, str(host_port)]
        res = client.run_vmrun(cmd_args, timeout=30)
        return {"ok": True, "action": action, "network": network, "protocol": protocol, "host_port": host_port, "output": res["stdout"]}
    except Exception as exc:
        return _error_response(exc)


@mcp.tool()
def manage_shared_folder(
    vm: str,
    action: Literal["enable", "disable", "add", "remove", "readonly", "writable"],
    share_name: str = "",
    host_path: str = "",
) -> dict[str, Any]:
    """Manage VMware Host-Guest shared folders for a VM.

    Args:
        vm: VM name, substring, or full .vmx path.
        action: 'enable', 'disable', 'add', 'remove', 'readonly', or 'writable'.
        share_name: Shared folder name (required for add/remove/readonly/writable).
        host_path: Host directory path (required for add/readonly/writable).
    """
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        if action == "enable":
            cmd_args = ["enableSharedFolders", str(vmx)]
        elif action == "disable":
            cmd_args = ["disableSharedFolders", str(vmx)]
        elif action == "add":
            if not share_name.strip() or not host_path.strip():
                return {"ok": False, "error": "share_name and host_path are required for action='add'."}
            hp = Path(host_path).resolve()
            if not hp.is_dir():
                return {"ok": False, "error": f"Host directory does not exist: {host_path}"}
            cmd_args = ["addSharedFolder", str(vmx), share_name.strip(), str(hp)]
        elif action == "remove":
            if not share_name.strip():
                return {"ok": False, "error": "share_name is required for action='remove'."}
            cmd_args = ["removeSharedFolder", str(vmx), share_name.strip()]
        else:
            if not share_name.strip() or not host_path.strip():
                return {"ok": False, "error": f"share_name and host_path are required for action='{action}'."}
            hp = Path(host_path).resolve()
            cmd_args = ["setSharedFolderState", str(vmx), share_name.strip(), str(hp), action]

        res = client.run_vmrun(cmd_args, timeout=30)
        return {"ok": True, "action": action, "vmx_path": str(vmx), "output": res["stdout"]}
    except Exception as exc:
        return _error_response(exc)


# =============================================================================
# 6. VM Cloning (1 tool)
# =============================================================================


@mcp.tool()
def clone_vm(
    vm: str,
    dest_vmx_path: str,
    clone_type: Literal["linked", "full"] = "linked",
    snapshot_name: str = "",
    clone_name: str = "",
) -> dict[str, Any]:
    """Create a linked or full clone of a VM (`vmrun clone`).

    Args:
        vm: Source VM name, substring, or full .vmx path.
        dest_vmx_path: Destination .vmx file path for the cloned VM.
        clone_type: 'linked' (fast, shares base disk) or 'full' (independent copy).
        snapshot_name: Optional source snapshot name to clone from.
        clone_name: Optional display name for the new clone.
    """
    dst = Path(dest_vmx_path).resolve()
    if dst.suffix.lower() != ".vmx":
        return {"ok": False, "error": "dest_vmx_path must end with '.vmx'."}
    if dst.exists():
        return {"ok": False, "error": f"Destination already exists: {dst}"}
    try:
        client = get_client()
        vmx = client.resolve_vmx(vm)
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd_args = ["clone", str(vmx), str(dst), clone_type]
        if snapshot_name.strip():
            cmd_args.append(f"-snapshot={snapshot_name.strip()}")
        if clone_name.strip():
            cmd_args.append(f"-cloneName={clone_name.strip()}")
        res = client.run_vmrun(cmd_args, timeout=600)
        return {
            "ok": True,
            "source_vmx": str(vmx),
            "dest_vmx": str(dst),
            "clone_type": clone_type,
            "output": res["stdout"],
        }
    except Exception as exc:
        return _error_response(exc)


def main() -> None:
    logger.info("Starting vmware-mcp stdio server")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
