"""Offline unit tests for vmware_mcp (no VMware installation required)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from vmware_mcp import server  # noqa: E402
from vmware_mcp.client import (  # noqa: E402
    VMwareClient,
    VMwareError,
    decode_bytes,
    parse_inventory_file,
    parse_vmx_metadata,
    redact_text,
    sanitize_argv,
)


class TestEncodingAndRedaction(unittest.TestCase):
    def test_decode_bytes_utf8_and_gbk(self) -> None:
        sample = "Ubuntu-22.04-测试机 / Win11 克隆"
        self.assertEqual(decode_bytes(sample.encode("utf-8")), sample)
        self.assertEqual(decode_bytes(sample.encode("utf-8-sig")), sample)
        self.assertEqual(decode_bytes(sample.encode("gb18030")), sample)
        self.assertEqual(decode_bytes(sample.encode("gbk")), sample)
        self.assertEqual(decode_bytes(b""), "")

    def test_credential_redaction(self) -> None:
        argv = [
            "vmrun",
            "-T",
            "ws",
            "-vp",
            "DiskSecret!123",
            "-gu",
            "admin",
            "-gp",
            "GuestPass#456",
            "listProcessesInGuest",
            "demo.vmx",
        ]
        safe = sanitize_argv(argv, ["DiskSecret!123", "GuestPass#456"])
        self.assertNotIn("DiskSecret!123", safe)
        self.assertNotIn("GuestPass#456", safe)
        self.assertEqual(safe[4], "***")
        self.assertEqual(safe[8], "***")
        self.assertEqual(
            redact_text("auth failed for GuestPass#456", ["GuestPass#456"]),
            "auth failed for ***",
        )


class TestVmxAndInventoryDiscovery(unittest.TestCase):
    def test_parse_vmx_metadata_gbk(self) -> None:
        content = (
            'displayName = "示例靶机-Linux"\n'
            'guestOS = "ubuntu-64"\n'
            'numvcpus = "4"\n'
            'memsize = "4096"\n'
            'ethernet0.present = "TRUE"\n'
            'ethernet0.connectionType = "nat"\n'
            'ethernet0.generatedAddress = "00:0c:29:11:22:33"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            vmx = Path(tmp) / "demo.vmx"
            vmx.write_bytes(content.encode("gb18030"))
            meta = parse_vmx_metadata(vmx)
        self.assertEqual(meta["display_name"], "示例靶机-Linux")
        self.assertEqual(meta["guest_os"], "ubuntu-64")
        self.assertEqual(meta["vcpus"], 4)
        self.assertEqual(meta["memory_mb"], 4096)
        self.assertEqual(len(meta["nics"]), 1)
        self.assertEqual(meta["nics"][0]["connection_type"], "nat")

    def test_gbk_inventory_and_fuzzy_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_vmrun = root / "vmrun.exe"
            fake_vmrun.write_text("stub", encoding="utf-8")

            vm1_dir = root / "Ubuntu-测试节点"
            vm1_dir.mkdir()
            vm1 = vm1_dir / "Ubuntu-测试节点.vmx"
            vm1.write_text('displayName = "Ubuntu Lab 01"\nguestOS = "ubuntu-64"\n', encoding="utf-8")

            vm2_dir = root / "Ubuntu-备用节点"
            vm2_dir.mkdir()
            vm2 = vm2_dir / "Ubuntu-备用节点.vmx"
            vm2.write_text('displayName = "Ubuntu Lab 02"\nguestOS = "ubuntu-64"\n', encoding="utf-8")

            inv = root / "inventory.vmls"
            inv_text = (
                'vmlist1.config = "folder1"\n'
                f'vmlist2.config = "{vm1}"\n'
                f'vmlist3.config = "{vm2}"\n'
                'vmlist4.config = ""\n'
            )
            inv.write_bytes(inv_text.encode("gb18030"))

            parsed = parse_inventory_file(inv)
            self.assertEqual(len(parsed), 2)

            client = VMwareClient(
                str(fake_vmrun),
                host_type="ws",
                inventory_path=inv,
                vm_dirs=[],
            )
            with mock.patch.object(client, "list_running_vmx_paths", return_value=[str(vm1)]):
                vms = client.discover_vms(include_specs=True)
                self.assertEqual(len(vms), 2)
                self.assertTrue(vms[0]["running"])
                self.assertEqual(client.resolve_vmx("Lab 01"), vm1.resolve())
                self.assertEqual(client.resolve_vmx("备用节点"), vm2.resolve())
                with self.assertRaises(VMwareError):
                    client.resolve_vmx("Ubuntu")  # ambiguous
                with self.assertRaises(VMwareError):
                    client.resolve_vmx("NonExistentVM")


class TestMockedVmrunAndGuardrails(unittest.TestCase):
    def test_run_vmrun_masks_password_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_vmrun = Path(tmp) / "vmrun"
            fake_vmrun.write_text("stub", encoding="utf-8")
            client = VMwareClient(str(fake_vmrun), host_type="ws", vm_dirs=[])

            fake_proc = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=b"",
                stderr=b"Authentication failed for TopSecretPwd!",
            )
            with mock.patch("subprocess.run", return_value=fake_proc) as run_mock:
                with self.assertRaises(VMwareError) as ctx:
                    client.run_vmrun(
                        ["listProcessesInGuest", "demo.vmx"],
                        guest_user="alice",
                        guest_password="TopSecretPwd!",
                    )
                self.assertFalse(run_mock.call_args.kwargs["shell"])
                self.assertNotIn("TopSecretPwd!", str(ctx.exception))
                self.assertNotIn("TopSecretPwd!", str(ctx.exception.details))

    def test_safety_guardrails(self) -> None:
        r_stop = server.stop_vm("any-vm", hard=True, confirm=False)
        self.assertFalse(r_stop["ok"])
        self.assertIn("confirm=True", r_stop["error"])

        r_reset = server.reset_vm("any-vm", hard=True, confirm=False)
        self.assertFalse(r_reset["ok"])
        self.assertIn("confirm=True", r_reset["error"])

        r_rev = server.revert_to_snapshot("any-vm", "snap1", confirm=False)
        self.assertFalse(r_rev["ok"])
        self.assertIn("confirm=True", r_rev["error"])

        r_del = server.delete_snapshot("any-vm", "snap1", confirm=False)
        self.assertFalse(r_del["ok"])
        self.assertIn("confirm=True", r_del["error"])


if __name__ == "__main__":
    unittest.main()
