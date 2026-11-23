"""Cross-platform local VMware automation client built around `vmrun`.

Supports VMware Workstation (Windows/Linux), VMware Fusion (macOS), and VMware
Player without requiring the `vmrest` background daemon. Includes encoding
fallback for Windows non-UTF-8 locales (e.g. GBK/GB18030 `inventory.vmls` and
CJK VM paths) and strict credential redaction.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

if sys.platform == "win32":
    import winreg  # type: ignore[import-not-found]
else:
    winreg = None

logger = logging.getLogger("vmware-mcp.client")

WINDOWS_VMRUN_CANDIDATES = (
    Path(r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"),
    Path(r"C:\Program Files\VMware\VMware Workstation\vmrun.exe"),
    Path(r"D:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"),
    Path(r"D:\Program Files\VMware\VMware Workstation\vmrun.exe"),
)

MACOS_VMRUN_CANDIDATES = (
    Path("/Applications/VMware Fusion.app/Contents/Library/vmrun"),
    Path("/Applications/VMware Fusion.app/Contents/Public/vmrun"),
)

LINUX_VMRUN_CANDIDATES = (
    Path("/usr/bin/vmrun"),
    Path("/usr/local/bin/vmrun"),
)

_VMX_KV_RE = re.compile(r'^\s*([A-Za-z0-9_.:-]+)\s*=\s*"(.*)"\s*$')
_INVENTORY_CFG_RE = re.compile(r'^\s*vmlist\d+\.config\s*=\s*"(.*)"\s*$', re.IGNORECASE)
_SENSITIVE_FLAGS = {"-gp", "-vp"}


class VMwareError(RuntimeError):
    """Structured error raised when a VMware operation fails."""

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = details or {}


def decode_bytes(raw: bytes) -> str:
    """Decode bytes safely across UTF-8 and Windows CJK locales (GB18030/CP936)."""
    if not raw:
        return ""
    for enc in ("utf-8-sig", "gb18030", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_text_safely(path: Path) -> str:
    """Read a text file with automatic UTF-8 / GB18030 fallback."""
    return decode_bytes(path.read_bytes())


def sanitize_argv(argv: Iterable[str], secrets: Iterable[str] = ()) -> list[str]:
    """Return a copy of argv with password values masked."""
    secret_set = {s for s in secrets if s}
    sanitized: list[str] = []
    mask_next = False
    for token in argv:
        if mask_next:
            sanitized.append("***")
            mask_next = False
            continue
        if token in _SENSITIVE_FLAGS:
            sanitized.append(token)
            mask_next = True
            continue
        if token in secret_set:
            sanitized.append("***")
            continue
        sanitized.append(token)
    return sanitized


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask any secret strings that might appear in output or error messages."""
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    return result


def default_host_type() -> str:
    """Return the default `vmrun -T` host type for the current OS."""
    env_type = os.environ.get("VMWARE_HOST_TYPE", "").strip().lower()
    if env_type:
        return env_type
    if sys.platform == "darwin":
        return "fusion"
    return "ws"


def _find_vmrun_from_registry() -> Optional[Path]:
    """Query Windows registry for VMware Workstation InstallPath."""
    if winreg is None:
        return None
    subkeys = (
        r"SOFTWARE\WOW6432Node\VMware, Inc.\VMware Workstation",
        r"SOFTWARE\VMware, Inc.\VMware Workstation",
    )
    for subkey in subkeys:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
                install_path, _ = winreg.QueryValueEx(key, "InstallPath")
                if install_path:
                    candidate = Path(install_path) / "vmrun.exe"
                    if candidate.is_file():
                        return candidate
        except OSError:
            continue
    return None


def find_vmrun(explicit_path: Optional[str] = None) -> Path:
    """Locate vmrun executable via override, env var, PATH, registry, or standard dirs."""
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    env_path = os.environ.get("VMWARE_VMRUN_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))

    for name in ("vmrun", "vmrun.exe"):
        which = shutil.which(name)
        if which:
            candidates.append(Path(which))

    if sys.platform == "win32":
        reg_path = _find_vmrun_from_registry()
        if reg_path is not None:
            candidates.append(reg_path)
        candidates.extend(WINDOWS_VMRUN_CANDIDATES)
    elif sys.platform == "darwin":
        candidates.extend(MACOS_VMRUN_CANDIDATES)
    else:
        candidates.extend(LINUX_VMRUN_CANDIDATES)

    for cand in candidates:
        if cand.is_file():
            return cand.resolve()

    raise VMwareError(
        "Could not locate vmrun executable. Set VMWARE_VMRUN_PATH or ensure VMware Workstation/Fusion is installed."
    )


def _default_inventory_path() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "VMware" / "inventory.vmls"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VMware Fusion" / "vmInventory"
    return Path.home() / ".vmware" / "inventory.vmls"


def _default_vm_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        return [home / "Documents" / "Virtual Machines"]
    if sys.platform == "darwin":
        return [
            home / "Virtual Machines.localized",
            home / "Documents" / "Virtual Machines.localized",
        ]
    return [home / "vmware", home / "Documents" / "Virtual Machines"]


def parse_vmx_metadata(vmx_path: Path) -> dict[str, Any]:
    """Parse key read-only hardware/display fields from a .vmx file."""
    meta: dict[str, Any] = {
        "display_name": vmx_path.stem,
        "guest_os": "",
        "vcpus": None,
        "memory_mb": None,
        "annotation": "",
        "nics": [],
    }
    if not vmx_path.is_file():
        return meta

    kv: dict[str, str] = {}
    try:
        content = read_text_safely(vmx_path)
    except OSError as exc:
        logger.warning("Failed to read VMX file %s: %s", vmx_path, exc)
        return meta

    for line in content.splitlines():
        match = _VMX_KV_RE.match(line)
        if match:
            kv[match.group(1).lower()] = match.group(2)

    if kv.get("displayname"):
        meta["display_name"] = kv["displayname"]
    if kv.get("guestos"):
        meta["guest_os"] = kv["guestos"]
    if kv.get("numvcpus", "").isdigit():
        meta["vcpus"] = int(kv["numvcpus"])
    if kv.get("memsize", "").isdigit():
        meta["memory_mb"] = int(kv["memsize"])
    if kv.get("annotation"):
        meta["annotation"] = kv["annotation"]

    nic_indices: set[int] = set()
    for key in kv:
        m = re.match(r"^ethernet(\d+)\.present$", key)
        if m and kv[key].lower() == "true":
            nic_indices.add(int(m.group(1)))

    nics: list[dict[str, str]] = []
    for idx in sorted(nic_indices):
        prefix = f"ethernet{idx}."
        nics.append(
            {
                "adapter": f"ethernet{idx}",
                "connection_type": kv.get(prefix + "connectiontype", "bridged"),
                "vnet": kv.get(prefix + "vnet", ""),
                "mac": kv.get(prefix + "address", kv.get(prefix + "generatedaddress", "")),
                "virtual_dev": kv.get(prefix + "virtualdev", ""),
            }
        )
    meta["nics"] = nics
    return meta


def parse_inventory_file(inventory_path: Path) -> list[Path]:
    """Extract existing .vmx paths from VMware inventory.vmls."""
    found: list[Path] = []
    if not inventory_path.is_file():
        return found
    try:
        text = read_text_safely(inventory_path)
    except OSError as exc:
        logger.warning("Failed to read inventory %s: %s", inventory_path, exc)
        return found

    for line in text.splitlines():
        match = _INVENTORY_CFG_RE.match(line)
        if not match:
            continue
        raw_val = match.group(1).strip()
        if not raw_val or not raw_val.lower().endswith(".vmx"):
            continue
        p = Path(raw_val)
        if p.is_file():
            found.append(p)
    return found


def scan_vm_directories(dirs: Iterable[Path]) -> list[Path]:
    """Scan root VM directories (depth <= 2) for .vmx files."""
    found: list[Path] = []
    for root in dirs:
        if not root.is_dir():
            continue
        try:
            for item in root.iterdir():
                if item.is_file() and item.suffix.lower() == ".vmx":
                    found.append(item)
                elif item.is_dir():
                    try:
                        for sub in item.iterdir():
                            if sub.is_file() and sub.suffix.lower() == ".vmx":
                                found.append(sub)
                    except OSError:
                        continue
        except OSError:
            continue
    return found


class VMwareClient:
    """High-level wrapper around `vmrun` and local VMware metadata."""

    def __init__(
        self,
        vmrun_path: Optional[str] = None,
        *,
        host_type: Optional[str] = None,
        inventory_path: Optional[Path] = None,
        vm_dirs: Optional[list[Path]] = None,
        extra_vm_dirs: Optional[list[Path]] = None,
    ) -> None:
        self.vmrun_path = find_vmrun(vmrun_path)
        self.host_type = host_type or default_host_type()
        self.inventory_path = inventory_path if inventory_path is not None else _default_inventory_path()

        dirs: list[Path] = list(vm_dirs) if vm_dirs is not None else _default_vm_dirs()
        env_dirs = os.environ.get("VMWARE_VM_DIRS", "").strip()
        if env_dirs:
            for part in env_dirs.split(os.pathsep):
                part = part.strip()
                if part:
                    dirs.append(Path(part))
        if extra_vm_dirs:
            dirs.extend(extra_vm_dirs)
        self.vm_dirs = dirs

    def run_vmrun(
        self,
        args: list[str],
        *,
        guest_user: str = "",
        guest_password: str = "",
        vm_password: str = "",
        timeout: int = 120,
        check: bool = True,
    ) -> dict[str, Any]:
        """Execute vmrun safely without shell interpolation and redact secrets."""
        cmd: list[str] = [str(self.vmrun_path), "-T", self.host_type]
        secrets: list[str] = []
        if vm_password:
            cmd.extend(["-vp", vm_password])
            secrets.append(vm_password)
        if guest_user:
            cmd.extend(["-gu", guest_user])
        if guest_password:
            cmd.extend(["-gp", guest_password])
            secrets.append(guest_password)
        cmd.extend(args)

        safe_cmd = sanitize_argv(cmd, secrets)
        logger.info("Executing vmrun: %s", safe_cmd)

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            proc = subprocess.run(
                cmd,
                shell=False,
                capture_output=True,
                timeout=timeout,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise VMwareError(
                f"vmrun timed out after {timeout}s",
                details={"command": safe_cmd, "timeout": timeout},
            ) from exc
        except OSError as exc:
            raise VMwareError(
                f"Failed to launch vmrun: {exc}",
                details={"command": safe_cmd},
            ) from exc

        stdout = redact_text(decode_bytes(proc.stdout), secrets).strip()
        stderr = redact_text(decode_bytes(proc.stderr), secrets).strip()
        result = {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "command": safe_cmd,
        }
        if check and proc.returncode != 0:
            err_msg = stderr or stdout or f"vmrun exited with code {proc.returncode}"
            raise VMwareError(err_msg, details=result)
        return result

    def list_running_vmx_paths(self) -> list[str]:
        """Return absolute .vmx paths for all currently running VMs."""
        res = self.run_vmrun(["list"], timeout=30)
        paths: list[str] = []
        for line in res["stdout"].splitlines():
            s = line.strip()
            if not s or s.lower().startswith("total running vms:"):
                continue
            paths.append(s)
        return paths

    def discover_vms(self, *, include_specs: bool = True) -> list[dict[str, Any]]:
        """Discover VMs from inventory, configured VM directories, and running list."""
        running_paths = self.list_running_vmx_paths()
        running_norm = {os.path.normcase(os.path.abspath(p)) for p in running_paths}

        ordered_paths: list[Path] = []
        seen: set[str] = set()

        def _add(p: Path) -> None:
            norm = os.path.normcase(os.path.abspath(str(p)))
            if norm not in seen:
                seen.add(norm)
                ordered_paths.append(Path(os.path.abspath(str(p))))

        for p_str in running_paths:
            _add(Path(p_str))
        for p in parse_inventory_file(self.inventory_path):
            _add(p)
        for p in scan_vm_directories(self.vm_dirs):
            _add(p)

        vms: list[dict[str, Any]] = []
        for vmx_path in ordered_paths:
            norm = os.path.normcase(str(vmx_path))
            meta = parse_vmx_metadata(vmx_path) if include_specs else None
            entry: dict[str, Any] = {
                "name": meta["display_name"] if meta else vmx_path.stem,
                "folder_name": vmx_path.parent.name,
                "vmx_path": str(vmx_path),
                "running": norm in running_norm,
            }
            if meta is not None:
                entry["specs"] = {
                    "guest_os": meta["guest_os"],
                    "vcpus": meta["vcpus"],
                    "memory_mb": meta["memory_mb"],
                    "nics": meta["nics"],
                }
            vms.append(entry)
        return vms

    def resolve_vmx(self, vm_identifier: str) -> Path:
        """Resolve a VM name, folder name, unique keyword, or full .vmx path to a .vmx file."""
        query = vm_identifier.strip().strip('"').strip("'")
        if not query:
            raise VMwareError("VM identifier cannot be empty.")

        direct = Path(query)
        if direct.is_file() and direct.suffix.lower() == ".vmx":
            return direct.resolve()

        vms = self.discover_vms(include_specs=True)
        q_lower = query.lower()

        # 1. Exact match on vmx_path, display name, folder name, or file stem
        exact_matches: list[dict[str, Any]] = []
        for vm in vms:
            vmx_p = Path(vm["vmx_path"])
            candidates = {
                vm["vmx_path"].lower(),
                vm["name"].lower(),
                vm["folder_name"].lower(),
                vmx_p.stem.lower(),
            }
            if q_lower in candidates:
                exact_matches.append(vm)

        if len(exact_matches) == 1:
            return Path(exact_matches[0]["vmx_path"]).resolve()
        if len(exact_matches) > 1:
            raise VMwareError(
                f"Ambiguous VM identifier '{vm_identifier}': multiple exact matches found.",
                details={"matches": [m["vmx_path"] for m in exact_matches]},
            )

        # 2. Substring match on display name, folder name, or full path
        partial_matches = [
            vm
            for vm in vms
            if q_lower in vm["name"].lower()
            or q_lower in vm["folder_name"].lower()
            or q_lower in vm["vmx_path"].lower()
        ]
        if len(partial_matches) == 1:
            return Path(partial_matches[0]["vmx_path"]).resolve()
        if len(partial_matches) > 1:
            raise VMwareError(
                f"Ambiguous VM identifier '{vm_identifier}': matched {len(partial_matches)} VMs. "
                "Please provide a more specific name or the full .vmx path.",
                details={
                    "matches": [
                        {"name": m["name"], "folder_name": m["folder_name"], "vmx_path": m["vmx_path"]}
                        for m in partial_matches
                    ]
                },
            )

        raise VMwareError(
            f"VM '{vm_identifier}' not found.",
            details={
                "available_vms": [
                    {"name": v["name"], "folder_name": v["folder_name"], "vmx_path": v["vmx_path"]}
                    for v in vms
                ]
            },
        )
