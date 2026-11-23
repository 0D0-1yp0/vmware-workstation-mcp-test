# Security Policy

## Scope & Design Guardrails

`vmware-workstation-mcp` bridges an MCP client directly to VMware's local `vmrun` utility. To minimize accidental damage and credential leakage when operated by AI agents, the following guardrails are enforced by design:

1. **Destructive Disk Deletion Disabled**: `vmrun deleteVM` is intentionally **not exposed** as an MCP tool.
2. **Explicit Confirmation (`confirm=True`)**: High-impact state-altering tools require `confirm=True` before executing:
   - `stop_vm(..., hard=True, confirm=True)`
   - `reset_vm(..., hard=True, confirm=True)`
   - `revert_to_snapshot(..., confirm=True)`
   - `delete_snapshot(..., confirm=True)`
3. **No Shell Interpolation**: Every `vmrun` invocation runs with `subprocess.run(argv_list, shell=False)` to avoid command injection via VM paths, snapshot names, or guest parameters.
4. **Credential Redaction**: Passwords passed to `-gp` (guest password) or `-vp` (encrypted VM password) are never persisted to disk and are masked as `***` in logs, returned command metadata, and error messages.
5. **Local-Only Stdio Transport**: Communication happens over local standard input/output without opening any network port or requiring the `vmrest` daemon.

## Operational Recommendations

- Only pass guest credentials (`guest_user`, `guest_password`) when invoking guest-level tools (`run_command_in_guest`, `run_script_in_guest`, `copy_file_*`, `list_processes_in_guest`).
- Prefer dedicated non-privileged lab accounts inside target VMs rather than domain administrator credentials.
- Take a snapshot before running automated changes inside important VMs.

## Reporting a Vulnerability

If you discover a security issue (such as argument injection, secret leakage, or guardrail bypass), please open a private security advisory on GitHub or report it via the repository Issues page without posting sensitive host data.
