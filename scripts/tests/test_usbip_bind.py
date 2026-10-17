import importlib.util
import json
import os
from pathlib import Path
import pwd
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service = load("usbip_bind", "usbip-bind.py")
BUSID = "8-1.4.2.2.4"
SERVER = "192.168.1.100"
HOST = "192.168.1.72"
WINDOWS = {"platform": "windows", "sudo": False, "agent": False}
LINUX = {"platform": "linux", "sudo": False, "agent": False}
LOCAL_USER = pwd.getpwuid(os.getuid()).pw_name


def ports(port=1, busid=BUSID, server=SERVER):
    return (
        "Imported USB devices\n====================\n"
        f"Port {port:02}: device in use at Full Speed\n"
        f"  USB Receiver\n  -> usbip://{server}:3240/{busid}\n"
        "  -> remote bus/dev: 008/052\n"
    )


def send(socket_path, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(socket_path))
        connection.sendall(json.dumps(request).encode() + b"\n")
        with connection.makefile("rb") as response:
            reply = json.loads(response.readline())
    if not reply["ok"]:
        raise RuntimeError(reply["error"])
    return reply["result"]


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "attachment.json"
        self.exported = False
        self.busid = BUSID
        self.device_mock = Mock(side_effect=self.device)
        self.remote_mock = Mock()
        self.detect_mock = Mock(return_value=WINDOWS)
        for name, mock in (
            ("device", self.device_mock),
            ("remote_usbip", self.remote_mock),
            ("detect_platform", self.detect_mock),
            ("local_address", Mock(return_value=SERVER)),
        ):
            patcher = patch.object(service, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def device(self, action, vendor, product):
        if action == "bind":
            self.exported = True
        elif action == "unbind":
            self.exported = False
        return {"busid": self.busid, "exported": self.exported}

    def saved(self, port=1, info=WINDOWS, user="racso", host=HOST):
        service.save_state(self.state, {
            "host": host, "server": SERVER, "busid": BUSID, "port": port,
            "platform": info["platform"], "sudo": info["sudo"],
            "agent": info["agent"], "ssh_user": user,
        })

    def capture(self, user=None, host=HOST):
        service.capture(host, user, self.state, "046d", "c547", None)

    def release(self, force=False):
        service.release(self.state, "046d", "c547", None, force=force)

    def test_windows_capture_once_and_targeted_release(self):
        self.remote_mock.side_effect = ["", "1\n", ports()]
        self.capture("racso")
        self.assertEqual(self.remote_mock.call_args_list[1].args[2], [
            "attach", "-r", SERVER, "-b", BUSID, "--once", "--terse",
        ])
        saved = service.read_state(self.state)
        self.assertEqual((saved["port"], saved["platform"], saved["ssh_user"]), (1, "windows", "racso"))
        self.remote_mock.side_effect = [ports(), ""]
        self.release()
        self.assertEqual(self.remote_mock.call_args.args[2], ["detach", "-p", "1"])
        self.assertFalse(self.exported)
        self.assertFalse(self.state.exists())

    def test_user_uses_user_at_host_and_default_uses_plain_host(self):
        self.remote_mock.side_effect = ["", "1\n", ports()]
        self.capture("racso")
        self.assertEqual(self.detect_mock.call_args.args[0], f"racso@{HOST}")
        self.assertEqual(self.remote_mock.call_args.args[0], f"racso@{HOST}")
        self.state.unlink()
        self.exported = False
        self.remote_mock.side_effect = ["", "1\n", ports()]
        self.capture()
        self.assertEqual(self.detect_mock.call_args.args[0], HOST)
        self.assertIsNone(service.read_state(self.state)["ssh_user"])

    def test_release_uses_saved_account_and_platform_without_detecting(self):
        self.saved(info=LINUX, user="bob")
        self.exported = True
        self.remote_mock.side_effect = [ports(), ""]
        self.release()
        self.assertEqual(self.remote_mock.call_args.args[0], f"bob@{HOST}")
        self.assertEqual(self.remote_mock.call_args.args[1]["platform"], "linux")
        self.detect_mock.assert_not_called()

    def test_linux_port_zero(self):
        self.detect_mock.return_value = LINUX
        self.remote_mock.side_effect = ["", "", ports(0)]
        self.capture()
        self.assertEqual(service.read_state(self.state)["port"], 0)
        self.assertNotIn("--once", self.remote_mock.call_args_list[1].args[2])

    def test_duplicate_capture_does_not_attach(self):
        self.saved()
        self.exported = True
        self.remote_mock.return_value = ports()
        self.capture()
        self.assertEqual(self.remote_mock.call_count, 1)
        self.assertEqual(self.remote_mock.call_args.args[2], ["port"])
        self.detect_mock.assert_not_called()

    def test_failed_probe_does_not_bind(self):
        self.detect_mock.side_effect = RuntimeError("SSH failed")
        with self.assertRaisesRegex(RuntimeError, "SSH failed"):
            self.capture()
        self.assertEqual([c.args[0] for c in self.device_mock.call_args_list], ["status"])
        self.assertFalse(self.state.exists())

    def test_offline_port_probe_does_not_bind(self):
        self.remote_mock.side_effect = RuntimeError("SSH failed")
        with self.assertRaisesRegex(RuntimeError, "SSH failed"):
            self.capture()
        self.assertEqual([c.args[0] for c in self.device_mock.call_args_list], ["status"])
        self.assertFalse(self.state.exists())

    def test_failed_attach_has_no_retry_or_recovery(self):
        self.remote_mock.side_effect = ["", RuntimeError("attach failed")]
        with self.assertRaisesRegex(RuntimeError, "attach failed"):
            self.capture()
        self.assertTrue(self.exported)
        self.assertEqual(self.remote_mock.call_count, 2)
        self.assertIsNone(service.read_state(self.state)["port"])
        self.assertNotIn("unbind", [c.args[0] for c in self.device_mock.call_args_list])

    def test_failed_detach_still_unbinds_locally(self):
        self.saved()
        self.exported = True
        self.remote_mock.side_effect = [ports(), RuntimeError("detach failed")]
        with self.assertRaisesRegex(RuntimeError, "remote detach"):
            self.release()
        self.assertFalse(self.exported)
        self.assertTrue(self.state.exists())

    def test_offline_release_still_unbinds_locally(self):
        self.saved()
        self.exported = True
        self.remote_mock.side_effect = subprocess.TimeoutExpired("ssh", 20)
        with self.assertRaisesRegex(RuntimeError, "remote detach"):
            self.release()
        self.assertFalse(self.exported)

    def test_force_release_clears_state_despite_unreachable_host(self):
        self.saved()
        self.exported = True
        self.remote_mock.side_effect = subprocess.TimeoutExpired("ssh", 20)
        self.release(force=True)
        self.assertFalse(self.exported)
        self.assertFalse(self.state.exists())

    def test_force_release_still_reports_local_unbind_failure(self):
        self.saved()
        self.remote_mock.side_effect = subprocess.TimeoutExpired("ssh", 20)
        self.device_mock.side_effect = [
            {"busid": BUSID, "exported": True}, RuntimeError("unbind failed"),
        ]
        with self.assertRaisesRegex(RuntimeError, "local unbind"):
            self.release(force=True)
        self.assertTrue(self.state.exists())

    def test_force_release_still_honors_topology_change(self):
        self.saved()
        self.busid = "3-2"
        self.remote_mock.side_effect = subprocess.TimeoutExpired("ssh", 20)
        with self.assertRaisesRegex(RuntimeError, "topology changed"):
            self.release(force=True)
        self.assertNotIn("unbind", [c.args[0] for c in self.device_mock.call_args_list])
        self.assertTrue(self.state.exists())

    def test_stale_port_never_detaches_other_device(self):
        self.saved()
        self.exported = True
        self.remote_mock.return_value = ports(busid="3-2")
        with self.assertRaisesRegex(RuntimeError, "belongs to another"):
            self.release()
        self.assertEqual(self.remote_mock.call_count, 1)
        self.assertFalse(self.exported)

    def test_pending_attach_can_be_explicitly_released(self):
        self.saved(None)
        self.exported = True
        self.remote_mock.side_effect = [ports(7), ""]
        self.release()
        self.assertEqual(self.remote_mock.call_args.args[2], ["detach", "-p", "7"])

    def test_already_detached_release(self):
        self.saved()
        self.remote_mock.return_value = ""
        self.release()
        self.assertEqual(self.remote_mock.call_count, 1)
        self.assertFalse(self.state.exists())

    def test_duplicate_release(self):
        self.release()
        self.release()
        self.remote_mock.assert_not_called()
        self.assertFalse(self.exported)

    def test_host_switch_requires_release_when_old_host_is_reachable(self):
        self.saved()
        self.remote_mock.return_value = ports()
        with self.assertRaisesRegex(RuntimeError, "previous host"):
            self.capture(host="192.168.1.62")
        self.assertFalse(self.exported)
        self.assertEqual(self.remote_mock.call_count, 1)
        self.assertEqual(self.remote_mock.call_args.args[2], ["port"])

    def test_host_switch_requires_release_when_remote_probe_errors(self):
        self.saved()
        self.exported = True
        self.remote_mock.side_effect = RuntimeError("permission denied")
        with self.assertRaisesRegex(RuntimeError, "previous host"):
            self.capture(host="192.168.1.62")
        # A non-connectivity error doesn't prove the old host is gone, so the
        # device must stay bound rather than risk a double attach.
        self.assertTrue(self.exported)
        self.assertEqual(self.remote_mock.call_count, 1)

    def test_host_switch_auto_recovers_when_old_host_is_unreachable(self):
        self.saved()
        self.exported = True
        self.remote_mock.side_effect = [
            service.HostUnreachable("no route to host"),  # pre-check probe
            service.HostUnreachable("no route to host"),  # release()'s own probe
            "",  # fresh capture: port probe on the new host
            "1\n",  # fresh capture: attach
            ports(),  # fresh capture: port verify
        ]
        self.capture(host="192.168.1.62")
        saved = service.read_state(self.state)
        self.assertEqual(saved["host"], "192.168.1.62")
        self.assertEqual(self.remote_mock.call_count, 5)

    def test_stale_capture_requires_explicit_release(self):
        self.saved()
        self.remote_mock.return_value = ""
        with self.assertRaisesRegex(RuntimeError, "stale handoff"):
            self.capture()
        self.assertEqual(self.remote_mock.call_count, 1)

    def test_unowned_export_is_rejected(self):
        self.exported = True
        self.remote_mock.return_value = ""
        with self.assertRaisesRegex(RuntimeError, "outside this handoff"):
            self.capture()
        self.assertFalse(self.state.exists())

    def test_topology_change_does_not_unbind_new_device(self):
        self.saved()
        self.busid = "3-2"
        self.remote_mock.return_value = ""
        with self.assertRaisesRegex(RuntimeError, "topology changed"):
            self.release()
        self.assertNotIn("unbind", [c.args[0] for c in self.device_mock.call_args_list])

    def test_local_unbind_failure_is_reported(self):
        self.device_mock.side_effect = [
            {"busid": BUSID, "exported": True}, RuntimeError("unbind failed"),
        ]
        with self.assertRaisesRegex(RuntimeError, "local unbind"):
            self.release()

    def test_invalid_port_response_preserves_pending_state(self):
        self.remote_mock.side_effect = ["", "success?"]
        with self.assertRaisesRegex(ValueError, "unexpected attach"):
            self.capture()
        self.assertIsNone(service.read_state(self.state)["port"])

    def test_incomplete_state_is_rejected(self):
        service.save_state(self.state, {
            "host": HOST, "server": SERVER, "busid": BUSID, "port": 1,
        })
        with self.assertRaises(KeyError):
            service.read_state(self.state)


class PlatformTests(unittest.TestCase):
    def ssh(self, *results):
        return patch.object(
            service, "ssh_raw",
            side_effect=[subprocess.CompletedProcess([], *r) for r in results],
        )

    def test_linux_root_and_user(self):
        with self.ssh((0, "Linux\n0\nnone\n", "")):
            self.assertEqual(service.detect_platform("h"), LINUX)
        with self.ssh((0, "Linux\n1000\nnone\n", "")):
            self.assertEqual(
                service.detect_platform("h"), {"platform": "linux", "sudo": True, "agent": False}
            )

    def test_linux_agent_is_detected_for_non_root_only(self):
        with self.ssh((0, "Linux\n1000\nagent\n", "")) as ssh:
            self.assertEqual(
                service.detect_platform("h"), {"platform": "linux", "sudo": True, "agent": True}
            )
        self.assertIn(service.LINUX_AGENT, ssh.call_args.args[1])
        with self.ssh((0, "Linux\n0\nagent\n", "")):
            self.assertFalse(service.detect_platform("h")["agent"])

    def test_windows_is_detected_after_uname_fails(self):
        with self.ssh((1, "", "not recognized"), (0, "", "")) as ssh:
            self.assertEqual(service.detect_platform("racso@h"), WINDOWS)
        self.assertEqual(ssh.call_args.args[1], service.WINDOWS_USBIP + " port")

    def test_unsupported_platform(self):
        with self.ssh((1, "", ""), (1, "", "")):
            with self.assertRaisesRegex(RuntimeError, "neither Linux nor Windows"):
                service.detect_platform("h")
        with self.ssh((0, "Darwin\n501\nnone\n", "")):
            with self.assertRaisesRegex(ValueError, "unrecognized platform"):
                service.detect_platform("h")

    def test_ssh_failure_is_reported_not_misread(self):
        with self.ssh((255, "", "Permission denied")):
            with self.assertRaisesRegex(RuntimeError, "SSH to h failed: Permission denied"):
                service.detect_platform("h")
        with self.ssh((1, "", ""), (255, "", "timeout")):
            with self.assertRaisesRegex(RuntimeError, "SSH to h failed"):
                service.detect_platform("h")


class CommandTests(unittest.TestCase):
    def test_windows_remote_uses_noninteractive_ssh(self):
        with patch.object(service, "run_raw", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            service.remote_usbip(
                f"racso@{HOST}", WINDOWS,
                ["attach", "-r", SERVER, "-b", BUSID, "--once", "--terse"],
            )
        args = run.call_args.args[0]
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("BatchMode=yes", args)
        self.assertEqual(args[-2], f"racso@{HOST}")
        self.assertEqual(args[-1], (
            f"{service.WINDOWS_USBIP} attach -r {SERVER} -b {BUSID} --once --terse"
        ))

    def test_linux_remote_sudo_only_for_non_root(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(service, "run_raw", return_value=result) as run:
            service.remote_usbip(HOST, LINUX, ["detach", "-p", "0"])
            self.assertEqual(run.call_args.args[0][-1], "LC_ALL=C /usr/bin/usbip detach -p 0")
            service.remote_usbip(HOST, {"platform": "linux", "sudo": True}, ["detach", "-p", "0"])
            self.assertEqual(
                run.call_args.args[0][-1], "LC_ALL=C sudo -n /usr/bin/usbip detach -p 0"
            )

    def test_linux_agent_replaces_sudo(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        info = {"platform": "linux", "sudo": True, "agent": True}
        with patch.object(service, "run_raw", return_value=result) as run:
            service.remote_usbip(HOST, info, ["attach", "-r", SERVER, "-b", BUSID])
        self.assertEqual(
            run.call_args.args[0][-1],
            f"{service.LINUX_AGENT} attach -r {SERVER} -b {BUSID}",
        )

    def test_remote_failure_is_reported(self):
        result = subprocess.CompletedProcess([], 1, "", "sudo: a password is required")
        with patch.object(service, "run_raw", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "password is required"):
                service.remote_usbip(HOST, {"platform": "linux", "sudo": True}, ["port"])

    def test_subprocess_timeout_and_failure(self):
        result = subprocess.CompletedProcess(["ssh"], 1, "output", "error")
        with patch.object(service.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, r"failed \(1\)"):
                service.run(["ssh"])
        self.assertEqual(run.call_args.kwargs["timeout"], 20)

    def test_port_output_supports_linux_and_preserves_unrelated_ports(self):
        output = ports(0).replace("device in use", "<Port in Use>")
        output += ports(3, "3-2")
        self.assertEqual(service.imported_ports(output), {
            0: (SERVER, 3240, BUSID), 3: (SERVER, 3240, "3-2"),
        })

    def test_unknown_in_use_format_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unrecognized"):
            service.imported_ports("Port 01: device in use\nunsupported format")

    def test_ssh_runs_as_configured_local_user(self):
        account = pwd.getpwnam(LOCAL_USER)
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(service.subprocess, "run", return_value=result) as run:
            service.run(["/usr/bin/ssh", "host", "whoami"], ssh_account=account)
        options = run.call_args.kwargs
        self.assertEqual(options["user"], account.pw_uid)
        self.assertEqual(options["group"], account.pw_gid)
        self.assertEqual(options["env"]["HOME"], account.pw_dir)
        self.assertNotIn("SSH_AUTH_SOCK", options["env"])

    def test_local_address_is_derived_from_route(self):
        self.assertRegex(service.local_address("127.0.0.1"), r"^127\.")


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.device = self.root / BUSID
        self.device.mkdir()
        self.driver = self.root / "usb"
        self.driver.mkdir()
        (self.device / "driver").symlink_to(self.driver)
        patcher = patch.object(service, "SYSFS", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.listing = f"busid={BUSID}#usbid=046d:c547#\n"

    def operate(self, action):
        return service.device(action, "046d", "c547")

    def test_status_and_idempotent_unbind(self):
        with patch.object(service, "run", return_value=self.listing) as run:
            self.assertEqual(self.operate("status"), {"busid": BUSID, "exported": False})
            self.operate("unbind")
            self.assertTrue(all(call.args[0][1] == "list" for call in run.call_args_list))

    def test_missing_vendor_product_is_rejected(self):
        with patch.object(service, "run", return_value=self.listing.replace("c547", "0000")) as run:
            with self.assertRaisesRegex(RuntimeError, "found 0"):
                self.operate("bind")
            self.assertEqual(run.call_count, 1)

    def test_duplicate_vendor_product_is_rejected(self):
        with patch.object(service, "run", return_value=self.listing * 2) as run:
            with self.assertRaisesRegex(RuntimeError, "found 2"):
                self.operate("bind")
            self.assertEqual(run.call_count, 1)

    def test_missing_driver_is_not_reported_as_local(self):
        (self.device / "driver").unlink()
        with patch.object(service, "run", return_value=self.listing):
            with self.assertRaisesRegex(RuntimeError, "no bound"):
                self.operate("status")

    def test_bind_checks_driver_transition(self):
        with patch.object(service, "run", side_effect=[self.listing, ""]) as run:
            with self.assertRaisesRegex(RuntimeError, "unexpected driver"):
                self.operate("bind")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/usbip", "bind", "-b", BUSID])

    def test_successful_bind_and_unbind(self):
        exported_driver = self.root / "usbip-host"
        exported_driver.mkdir()

        def transition(args):
            if args[1] == "list":
                return self.listing
            driver = self.device / "driver"
            driver.unlink()
            driver.symlink_to(exported_driver if args[1] == "bind" else self.driver)
            return ""

        with patch.object(service, "run", side_effect=transition):
            self.assertTrue(self.operate("bind")["exported"])
            self.assertFalse(self.operate("unbind")["exported"])

    def test_unbind_without_reprobe_is_reported(self):
        exported_driver = self.root / "usbip-host"
        exported_driver.mkdir()
        (self.device / "driver").unlink()
        (self.device / "driver").symlink_to(exported_driver)

        def unbind(args):
            if args[1] == "list":
                return self.listing
            (self.device / "driver").unlink()
            return ""

        with patch.object(service, "run", side_effect=unbind):
            with self.assertRaisesRegex(RuntimeError, "no bound USB driver after unbind"):
                self.operate("unbind")


class ListTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = patch.object(service, "SYSFS", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        for busid in (BUSID, "3-2", "3-3"):
            (self.root / busid).mkdir()
        (self.root / BUSID / "manufacturer").write_text("Logitech\n")
        (self.root / BUSID / "product").write_text("USB Receiver\n")
        exported = self.root / "usbip-host"
        exported.mkdir()
        (self.root / "3-2" / "driver").symlink_to(exported)
        self.listing = (
            f"busid={BUSID}#usbid=046d:c547#\nbusid=3-2#usbid=320f:5088#\n"
            "busid=3-3#usbid=320f:5088#\n"
        )
        self.names = (
            " - busid 3-2 (320f:5088)\n   Compx 2.4G Keyboard (320f:5088)\n\n"
        )

    def fake_run(self, argv):
        return self.listing if "-p" in argv else self.names

    def test_list_reports_name_state_and_duplicates(self):
        with patch.object(service, "run", side_effect=self.fake_run):
            devices = {d["busid"]: d for d in service.list_devices()}
        self.assertEqual(devices[BUSID]["name"], "Logitech USB Receiver")
        self.assertEqual((devices[BUSID]["vendor"], devices[BUSID]["product"]), ("046d", "c547"))
        self.assertEqual(devices[BUSID]["count"], 1)
        self.assertFalse(devices[BUSID]["exported"])
        self.assertEqual(devices["3-2"]["count"], 2)
        self.assertTrue(devices["3-2"]["exported"])
        self.assertEqual(devices["3-2"]["name"], "Compx 2.4G Keyboard")
        self.assertEqual(devices["3-3"]["name"], "")

    def test_hubs_are_not_listed(self):
        (self.root / "3-3" / "bDeviceClass").write_text("09\n")
        with patch.object(service, "run", side_effect=self.fake_run):
            devices = {d["busid"] for d in service.list_devices()}
        self.assertEqual(devices, {BUSID, "3-2"})


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.controller = service.Controller(LOCAL_USER, self.path)

    def request(self, vendor="046d", product="c547", action="capture", host=HOST, **extra):
        return {"action": action, "host": host, "vendor": vendor, "product": product, **extra}

    def test_generic_ids_have_separate_state_and_normalize_case(self):
        with patch.object(service, "capture") as call:
            self.controller.execute(self.request())
            self.controller.execute(self.request("054C", "09CC"))
        first, second = call.call_args_list
        self.assertEqual(first.args[2], self.path / "046d-c547.json")
        self.assertEqual(second.args[2], self.path / "054c-09cc.json")
        self.assertEqual(second.args[3:5], ("054c", "09cc"))

    def test_any_valid_host_is_accepted_without_allowlist(self):
        with patch.object(service, "capture") as call:
            self.controller.execute(self.request(host="venator"))
        self.assertEqual(call.call_args.args[0], "venator")

    def test_invalid_hosts_rejected_before_any_command(self):
        for host in ("-oProxyCommand=x", "a b", "a;b", "", "-", 5):
            with self.subTest(host=host), patch.object(service, "capture") as call:
                with self.assertRaisesRegex(ValueError, "invalid host"):
                    self.controller.execute(self.request(host=host))
                call.assert_not_called()

    def test_invalid_ids_rejected_before_privileged_operations(self):
        with patch.object(service, "capture") as call:
            for vendor in ("../x", 5, None):
                with self.assertRaisesRegex(ValueError, "hexadecimal"):
                    self.controller.execute(self.request(vendor))
        call.assert_not_called()

    def test_unknown_action_and_extra_fields_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown device command"):
            self.controller.execute(self.request(action="shell"))
        extra = self.request()
        extra["command"] = "id"
        with self.assertRaisesRegex(ValueError, "expected action"):
            self.controller.execute(extra)
        with self.assertRaisesRegex(ValueError, "no parameters"):
            self.controller.execute({"action": "list", "host": HOST})

    def test_status_does_not_bind_or_attach(self):
        with patch.object(service, "device", return_value={"exported": False}) as device:
            result = self.controller.execute(self.request(action="status"))
        device.assert_called_once_with("status", "046d", "c547")
        self.assertFalse(result["exported"])

    def test_ssh_user_is_optional_and_empty_means_default(self):
        with patch.object(service, "capture") as call:
            self.controller.execute(self.request())
            self.controller.execute(self.request(user=""))
            self.controller.execute(self.request(user="racso"))
        self.assertEqual([c.args[1] for c in call.call_args_list], [None, None, "racso"])

    def test_force_defaults_false_and_is_forwarded_to_release(self):
        with patch.object(service, "release") as call:
            self.controller.execute(self.request(action="release"))
            self.controller.execute(self.request(action="release", force=True))
        self.assertEqual([c.kwargs["force"] for c in call.call_args_list], [False, True])

    def test_force_is_rejected_for_capture(self):
        with patch.object(service, "capture") as call:
            with self.assertRaisesRegex(ValueError, "only valid for release"):
                self.controller.execute(self.request(action="capture", force=True))
        call.assert_not_called()

    def test_non_boolean_force_is_rejected(self):
        with patch.object(service, "release") as call:
            with self.assertRaisesRegex(ValueError, "force must be a boolean"):
                self.controller.execute(self.request(action="release", force="yes"))
        call.assert_not_called()

    def test_invalid_ssh_user_rejected_before_any_command(self):
        for user in ("-oProxyCommand=x", "a b", "x@y", "a;b", 5, ["x"]):
            with self.subTest(user=user), patch.object(service, "capture") as call:
                with self.assertRaisesRegex(ValueError, "invalid SSH user"):
                    self.controller.execute(self.request(user=user))
                call.assert_not_called()

    def test_unknown_local_user_rejected(self):
        with self.assertRaises(KeyError):
            service.Controller("no-such-user-xyz", self.path)
        with self.assertRaises(ValueError):
            service.Controller("a b", self.path)

    def test_serve_rejects_non_root(self):
        with patch.object(service.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(PermissionError, "must run as root"):
                service.serve(LOCAL_USER, self.path / "socket", self.path)

    def with_server(self, check, handler=service.CommandHandler):
        socket_path = self.path / "control.sock"
        with socketserver.UnixStreamServer(str(socket_path), handler) as server:
            server.controller = self.controller
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                check(socket_path)
            finally:
                server.shutdown()
                thread.join()

    def test_real_socket_round_trip(self):
        with patch.object(service, "capture") as call:
            self.with_server(lambda path: self.assertEqual(
                send(path, self.request())["action"], "capture"
            ))
        self.assertEqual(call.call_args.args[3:5], ("046d", "c547"))

    def test_list_over_socket(self):
        devices = [{"vendor": "046d", "product": "c547", "name": "x", "busid": BUSID,
                    "exported": False, "count": 1}]
        with patch.object(service, "list_devices", return_value=devices):
            self.with_server(lambda path: self.assertEqual(
                send(path, {"action": "list"}), {"devices": devices}
            ))

    def test_errors_propagate_over_socket(self):
        def check(path):
            with self.assertRaisesRegex(RuntimeError, "hexadecimal"):
                send(path, self.request(product="bad"))
        with self.assertLogs(level="ERROR"):
            self.with_server(check)

    def test_unauthorized_peer_cannot_execute_commands(self):
        class WrongPeerHandler(service.CommandHandler):
            def handle(self):
                with patch.object(service.struct, "unpack", return_value=(123, 987654, 987654)):
                    super().handle()

        def check(path):
            with patch.object(self.controller, "execute") as execute:
                with self.assertRaisesRegex(RuntimeError, "not authorized"):
                    send(path, self.request())
                execute.assert_not_called()

        with self.assertLogs(level="ERROR"):
            self.with_server(check, WrongPeerHandler)

    def test_oversized_command_is_rejected(self):
        def check(path):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(path))
                connection.sendall(b"x" * 4097 + b"\n")
                with connection.makefile("rb") as response:
                    self.assertFalse(json.loads(response.readline())["ok"])
        with self.assertLogs(level="ERROR"):
            self.with_server(check)

    def test_client_that_disconnects_does_not_break_the_service(self):
        def check(path):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(path))
                connection.sendall(json.dumps(self.request()).encode() + b"\n")
            time.sleep(0.1)
            self.assertEqual(send(path, self.request())["action"], "capture")
        with patch.object(service, "capture"):
            self.with_server(check)

    def test_service_serializes_commands(self):
        active = 0
        maximum = 0
        errors = []

        def execute(request):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            time.sleep(0.02)
            active -= 1
            return {}

        def check(path):
            def send_one():
                try:
                    send(path, self.request())
                except (OSError, ValueError, RuntimeError) as error:
                    errors.append(error)
            clients = [threading.Thread(target=send_one) for _ in range(2)]
            for client in clients:
                client.start()
            for client in clients:
                client.join()

        with patch.object(self.controller, "execute", side_effect=execute):
            self.with_server(check)
        self.assertEqual(errors, [])
        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()
