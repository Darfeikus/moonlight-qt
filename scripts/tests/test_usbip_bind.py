import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import socket
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
OTHER_HOST = "192.168.1.62"
LOCAL_USER = pwd.getpwuid(os.getuid()).pw_name


def ports(port=1, busid=BUSID, server=SERVER):
    return (
        "Imported USB devices\n====================\n"
        f"Port {port:02}: device in use at Full Speed\n"
        f"  USB Receiver\n  -> usbip://{server}:3240/{busid}\n"
        "  -> remote bus/dev: 008/052\n"
    )


class Connection:
    """A client connection that, like Moonlight, can carry several requests."""

    def __init__(self, socket_path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(10)
        self.socket.connect(str(socket_path))
        self.reader = self.socket.makefile("rb")

    def send(self, request):
        self.socket.sendall(json.dumps(request).encode() + b"\n")
        reply = json.loads(self.reader.readline())
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def close(self):
        self.reader.close()
        self.socket.close()


def send(socket_path, request):
    connection = Connection(socket_path)
    try:
        return connection.send(request)
    finally:
        connection.close()


def eventually(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not met in time")
        time.sleep(0.01)


class FakeHost:
    """A streaming PC's USB/IP imports, reached through remote_usbip()."""

    def __init__(self, platform="windows"):
        self.platform = platform
        self.imports = {}
        self.errors = {}
        self.calls = []

    def listing(self):
        return "Imported USB devices\n====================\n" + "".join(
            f"Port {port:02}: device in use at Full Speed\n"
            f"  USB Receiver\n  -> usbip://{server}:3240/{busid}\n"
            "  -> remote bus/dev: 008/052\n"
            for port, (server, _, busid) in sorted(self.imports.items())
        )

    def __call__(self, target, platform, commands, ssh_account=None):
        self.calls.append((target, platform, [args[0] for args in commands]))
        outputs = []
        for args in commands:
            if platform != self.platform:
                raise RuntimeError("'sh' is not recognized as an internal or external command")
            if args[0] in self.errors:
                raise self.errors[args[0]]
            if args[0] == "port":
                outputs.append(self.listing())
            elif args[0] == "attach":
                port = 1 if platform == "windows" else 0
                while port in self.imports:
                    port += 1
                self.imports[port] = (args[2], 3240, args[4])
                outputs.append(f"{port}\r\n" if platform == "windows" else "")
            elif args[0] == "detach":
                self.imports.pop(int(args[2]), None)
                outputs.append("")
        return outputs

    def commands(self):
        return [name for _, _, names in self.calls for name in names]


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "046d-c547.json"
        self.present = True
        self.busid = BUSID
        self.exported = False
        self.events = []
        self.host = FakeHost()
        self.remote_mock = Mock(side_effect=self.remote)
        self.detect_mock = Mock(side_effect=lambda target, account=None: self.host.platform)
        for name, mock in (
            ("device", Mock(side_effect=self.device)),
            ("set_export", Mock(side_effect=self.set_export)),
            ("same_device", Mock(side_effect=self.same_device)),
            ("remote_usbip", self.remote_mock),
            ("detect_platform", self.detect_mock),
            ("local_address", Mock(return_value=SERVER)),
        ):
            patcher = patch.object(service, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    def remote(self, *args):
        self.events.append("remote")
        return self.host(*args)

    def device(self, action, vendor, product):
        if not self.present:
            raise RuntimeError(f"expected one {vendor}:{product} device, found 0")
        return self.set_export(self.busid, action)

    def set_export(self, busid, action):
        if not self.present or busid != self.busid:
            raise RuntimeError("selected device has no bound USB driver")
        if action in ("bind", "unbind"):
            self.events.append(action)
            self.exported = action == "bind"
        return {"busid": busid, "exported": self.exported}

    def same_device(self, busid, vendor, product):
        return self.present and busid == self.busid

    def saved(self, port=1, platform="windows", user="racso", host=HOST, **extra):
        service.save_state(self.state, {
            "host": host, "server": SERVER, "busid": BUSID, "port": port,
            "platform": platform, "ssh_user": user, **extra,
        })

    def attached_elsewhere(self, port=1):
        """A live handoff saved for HOST, as a previous capture leaves it."""
        self.saved(port)
        self.exported = True
        self.host.imports[port] = (SERVER, 3240, BUSID)

    def capture(self, user="racso", host=HOST, platform=None):
        return service.capture(host, user, self.state, "046d", "c547", None, platform)

    def release(self, force=False):
        service.release(self.state, "046d", "c547", None, force=force)

    def test_windows_capture_and_release(self):
        self.assertEqual(self.capture(), "windows")
        self.assertEqual(self.host.imports, {1: (SERVER, 3240, BUSID)})
        self.assertTrue(self.exported)
        attach = self.remote_mock.call_args_list[-1].args[2]
        self.assertEqual(attach, [
            ["attach", "-r", SERVER, "-b", BUSID, "--once", "--terse"], ["port"],
        ])
        saved = service.read_state(self.state)
        self.assertEqual((saved["port"], saved["platform"], saved["ssh_user"]), (1, "windows", "racso"))
        self.events.clear()
        self.release()
        self.assertEqual(self.host.imports, {})
        self.assertFalse(self.exported)
        self.assertFalse(self.state.exists())
        # The device comes back before any round trip to the streaming PC.
        self.assertEqual(self.events[0], "unbind")

    def test_capture_takes_two_round_trips_when_the_os_is_known(self):
        self.capture(platform="windows")
        self.detect_mock.assert_not_called()
        self.assertEqual(self.remote_mock.call_count, 2)

    def test_user_uses_user_at_host_and_default_uses_plain_host(self):
        self.capture("racso")
        self.assertEqual(self.detect_mock.call_args.args[0], f"racso@{HOST}")
        self.assertEqual(self.remote_mock.call_args.args[0], f"racso@{HOST}")
        self.release()
        self.remote_mock.reset_mock()
        self.capture(None)
        self.assertEqual(self.detect_mock.call_args.args[0], HOST)
        self.assertEqual(self.remote_mock.call_args.args[0], HOST)
        self.assertIsNone(service.read_state(self.state)["ssh_user"])

    def test_wrong_saved_os_is_detected_again(self):
        self.host.platform = "linux"
        self.assertEqual(self.capture(platform="windows"), "linux")
        self.detect_mock.assert_called_once()
        self.assertEqual(service.read_state(self.state)["platform"], "linux")
        self.assertEqual(self.host.imports, {0: (SERVER, 3240, BUSID)})

    def test_real_failure_with_correct_saved_os_is_reported(self):
        self.host.errors["port"] = RuntimeError("sudo: a password is required")
        with self.assertRaisesRegex(RuntimeError, "password is required"):
            self.capture(platform="windows")
        self.detect_mock.assert_called_once()
        self.assertEqual(self.events.count("bind"), 0)
        self.assertFalse(self.state.exists())

    def test_unreachable_host_is_not_detected_again(self):
        self.host.errors["port"] = service.HostUnreachable("no route to host")
        with self.assertRaises(service.HostUnreachable):
            self.capture(platform="windows")
        self.detect_mock.assert_not_called()
        self.assertFalse(self.exported)

    def test_linux_port_zero(self):
        self.host.platform = "linux"
        self.assertEqual(self.capture(), "linux")
        self.assertEqual(service.read_state(self.state)["port"], 0)
        attach = self.remote_mock.call_args.args[2][0]
        self.assertNotIn("--once", attach)

    def test_duplicate_capture_does_not_attach(self):
        self.attached_elsewhere()
        self.assertEqual(self.capture(), "windows")
        self.assertEqual(self.host.commands(), ["port"])
        self.detect_mock.assert_not_called()

    def test_failed_probe_does_not_bind(self):
        self.detect_mock.side_effect = service.HostUnreachable("SSH failed")
        with self.assertRaisesRegex(RuntimeError, "SSH failed"):
            self.capture()
        self.assertNotIn("bind", self.events)
        self.assertFalse(self.state.exists())

    def test_failed_attach_is_rolled_back(self):
        self.host.errors["attach"] = RuntimeError("attach failed")
        with self.assertRaisesRegex(RuntimeError, "attach failed"):
            self.capture()
        self.assertFalse(self.exported)
        self.assertFalse(self.state.exists())

    def test_attach_timeout_is_rolled_back_and_import_detached(self):
        def attach_then_hang(target, platform, commands, ssh_account=None):
            outputs = self.remote(target, platform, commands, ssh_account)
            if commands[0][0] == "attach":
                raise subprocess.TimeoutExpired("ssh", 6)
            return outputs

        self.remote_mock.side_effect = attach_then_hang
        with self.assertRaises(subprocess.TimeoutExpired):
            self.capture()
        self.assertFalse(self.exported)
        self.assertEqual(self.host.imports, {})
        self.assertFalse(self.state.exists())

    def test_unverified_import_is_rolled_back(self):
        def misreport(target, platform, commands, ssh_account=None):
            outputs = self.remote(target, platform, commands, ssh_account)
            return ["7\n", outputs[1]] if commands[0][0] == "attach" else outputs

        self.remote_mock.side_effect = misreport
        with self.assertRaisesRegex(RuntimeError, "could not verify"):
            self.capture()
        self.assertFalse(self.exported)
        self.assertEqual(self.host.imports, {})

    def test_invalid_port_response_is_rolled_back(self):
        def garble(target, platform, commands, ssh_account=None):
            outputs = self.remote(target, platform, commands, ssh_account)
            return ["success?", outputs[1]] if commands[0][0] == "attach" else outputs

        self.remote_mock.side_effect = garble
        with self.assertRaisesRegex(ValueError, "unexpected attach"):
            self.capture()
        self.assertFalse(self.exported)
        self.assertFalse(self.state.exists())

    def test_failed_rollback_keeps_state_for_the_next_capture(self):
        self.host.errors["attach"] = RuntimeError("attach failed")
        real = self.set_export

        def stuck(busid, action):
            if action == "unbind":
                raise RuntimeError("unbind failed")
            return real(busid, action)

        service.set_export.side_effect = stuck
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "attach failed"):
            self.capture()
        self.assertTrue(self.state.exists())

    def test_release_returns_device_even_if_detach_fails(self):
        self.attached_elsewhere()
        self.host.errors["detach"] = RuntimeError("detach failed")
        with self.assertLogs(level="WARNING"):
            self.release()
        self.assertFalse(self.exported)
        self.assertFalse(self.state.exists())

    def test_release_returns_device_even_if_host_is_offline(self):
        self.attached_elsewhere()
        self.host.errors["port"] = subprocess.TimeoutExpired("ssh", 6)
        with self.assertLogs(level="WARNING"):
            self.release()
        self.assertFalse(self.exported)
        self.assertFalse(self.state.exists())

    def test_force_release_does_not_contact_the_host(self):
        self.attached_elsewhere()
        self.release(force=True)
        self.remote_mock.assert_not_called()
        self.assertFalse(self.exported)
        self.assertFalse(self.state.exists())

    def test_local_unbind_failure_keeps_state(self):
        self.attached_elsewhere()
        service.set_export.side_effect = RuntimeError("unbind failed")
        with self.assertRaisesRegex(RuntimeError, "unbind failed"):
            self.release()
        self.assertTrue(self.state.exists())
        self.remote_mock.assert_not_called()

    def test_unplugged_device_state_is_flushed(self):
        self.attached_elsewhere()
        self.present = False
        with self.assertLogs(level="WARNING"):
            self.release()
        self.assertFalse(self.state.exists())
        self.assertNotIn("unbind", self.events)
        self.assertEqual(self.host.imports, {})

    def test_moved_device_is_flushed_without_touching_the_new_port(self):
        self.attached_elsewhere()
        self.busid = "3-2"
        self.host.errors["port"] = RuntimeError("permission denied")
        with self.assertLogs(level="WARNING"):
            self.release()
        self.assertFalse(self.state.exists())
        self.assertNotIn("unbind", self.events)

    def test_capture_after_moving_the_device_starts_fresh(self):
        self.attached_elsewhere()
        self.busid = "3-2"
        self.exported = False
        with self.assertLogs(level="WARNING"):
            self.capture()
        self.assertEqual(service.read_state(self.state)["busid"], "3-2")
        self.assertEqual(self.host.imports, {1: (SERVER, 3240, "3-2")})

    def test_stale_port_never_detaches_other_device(self):
        self.saved()
        self.exported = True
        self.host.imports[1] = (SERVER, 3240, "3-2")
        with self.assertLogs(level="WARNING"):
            self.release()
        self.assertNotIn("detach", self.host.commands())
        self.assertEqual(self.host.imports, {1: (SERVER, 3240, "3-2")})
        self.assertFalse(self.exported)

    def test_pending_attach_is_found_by_location(self):
        self.saved(None)
        self.exported = True
        self.host.imports[7] = (SERVER, 3240, BUSID)
        self.release()
        self.assertEqual(self.host.imports, {})

    def test_release_without_state_makes_the_device_local(self):
        self.exported = True
        self.release()
        self.release()
        self.remote_mock.assert_not_called()
        self.assertFalse(self.exported)

    def test_new_host_wins(self):
        self.attached_elsewhere()
        self.capture(host=OTHER_HOST)
        targets = [c.args[0] for c in self.remote_mock.call_args_list]
        self.assertIn(f"racso@{HOST}", targets)
        saved = service.read_state(self.state)
        self.assertEqual(saved["host"], OTHER_HOST)
        self.assertTrue(self.exported)
        # Old import detached, then the same fake PC took a fresh one.
        self.assertEqual(self.host.imports, {1: (SERVER, 3240, BUSID)})
        self.assertEqual(self.host.commands().count("detach"), 1)

    def test_new_host_wins_when_the_old_one_is_unreachable(self):
        self.attached_elsewhere()

        def old_host_gone(target, platform, commands, ssh_account=None):
            if target.endswith(HOST):
                raise service.HostUnreachable("no route to host")
            return self.remote(target, platform, commands, ssh_account)

        self.remote_mock.side_effect = old_host_gone
        with self.assertLogs(level="WARNING"):
            self.capture(host=OTHER_HOST)
        self.assertEqual(service.read_state(self.state)["host"], OTHER_HOST)

    def test_new_ssh_account_on_the_same_address_wins(self):
        self.attached_elsewhere()
        self.host.platform = "linux"
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.capture("darfeikus"), "linux")
        saved = service.read_state(self.state)
        self.assertEqual((saved["ssh_user"], saved["platform"]), ("darfeikus", "linux"))

    def test_stale_handoff_on_the_same_host_is_replaced(self):
        self.saved()
        self.exported = True
        self.capture()
        self.assertEqual(self.host.imports, {1: (SERVER, 3240, BUSID)})
        self.assertEqual(self.events.count("unbind"), 1)
        self.assertEqual(self.events.count("bind"), 1)

    def test_unreachable_same_host_still_returns_the_device(self):
        self.attached_elsewhere()
        self.host.errors["port"] = service.HostUnreachable("no route to host")
        with self.assertRaises(service.HostUnreachable):
            self.capture()
        self.assertFalse(self.exported)
        self.assertFalse(self.state.exists())
        # Verifying found the host gone, so only the fresh capture's probe tries
        # it again; the cleanup does not wait on it.
        self.assertEqual(self.host.commands(), ["port", "port"])

    def test_leftover_import_and_export_are_cleared_before_attaching(self):
        self.exported = True
        self.host.imports[3] = (SERVER, 3240, BUSID)
        self.capture()
        self.assertEqual(self.host.imports, {1: (SERVER, 3240, BUSID)})
        self.assertEqual(self.events.count("unbind"), 1)
        self.assertTrue(self.exported)

    def test_incomplete_state_is_rejected(self):
        service.save_state(self.state, {
            "host": HOST, "server": SERVER, "busid": BUSID, "port": 1,
        })
        with self.assertRaises(KeyError):
            service.read_state(self.state)

    def test_state_from_the_previous_version_is_read(self):
        self.saved(sudo=False, agent=False)
        self.assertEqual(service.read_state(self.state)["platform"], "windows")


class PlatformTests(unittest.TestCase):
    def ssh(self, returncode, stdout, stderr=""):
        return patch.object(
            service, "ssh_raw",
            return_value=subprocess.CompletedProcess([], returncode, stdout, stderr),
        )

    def test_linux_and_windows_in_one_round_trip(self):
        with self.ssh(0, "Linux\n") as ssh:
            self.assertEqual(service.detect_platform("h"), "linux")
        self.assertEqual(ssh.call_args.args[1], "uname -s || ver")
        windows = (
            "'uname' is not recognized as an internal or external command\r\n\r\n"
            "Microsoft Windows [Version 10.0.26100.4652]\r\n"
        )
        with self.ssh(0, windows):
            self.assertEqual(service.detect_platform("racso@h"), "windows")

    def test_unsupported_platform(self):
        with self.ssh(0, "Darwin\n"):
            with self.assertRaisesRegex(RuntimeError, "neither Linux nor Windows"):
                service.detect_platform("h")

    def test_ssh_failure_is_reported_not_misread(self):
        with self.ssh(255, "", "Permission denied"):
            with self.assertRaisesRegex(service.HostUnreachable, "SSH to h failed: Permission denied"):
                service.detect_platform("h")


class CommandTests(unittest.TestCase):
    def test_windows_commands_share_one_noninteractive_ssh(self):
        output = "1\r\n--usbip-bind-- \r\n" + ports()
        result = subprocess.CompletedProcess([], 0, output, "")
        with patch.object(service, "run_raw", return_value=result) as run:
            outputs = service.remote_usbip(
                f"racso@{HOST}", "windows",
                [["attach", "-r", SERVER, "-b", BUSID, "--once", "--terse"], ["port"]],
            )
        self.assertEqual(outputs, ["1\r\n", ports()])
        args = run.call_args.args[0]
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("BatchMode=yes", args)
        self.assertIn(f"ConnectTimeout={service.SSH_CONNECT_TIMEOUT}", args)
        self.assertEqual(run.call_args.args[2], service.SSH_TIMEOUT)
        self.assertEqual(args[-2], f"racso@{HOST}")
        self.assertEqual(args[-1], (
            f"{service.WINDOWS_USBIP} attach -r {SERVER} -b {BUSID} --once --terse"
            f" && echo --usbip-bind-- && {service.WINDOWS_USBIP} port"
        ))

    def run_linux(self, commands, agent=True):
        """Run the Linux command line through sh, with stand-ins for the agent and sudo."""
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake"
            fake.write_text('#!/bin/sh\necho "$(basename "$0") $*"\n')
            fake.chmod(0o755)
            agent_path = Path(directory) / "agent"
            if agent:
                agent_path.symlink_to(fake)
            sudo_path = Path(directory) / "sudo"
            sudo_path.symlink_to(fake)
            command = service.remote_command("linux", commands)
            command = command.replace(service.LINUX_AGENT, str(agent_path))
            command = command.replace("sudo -n", str(sudo_path) + " -n")
            result = subprocess.run(
                ["sh", "-c", command], capture_output=True, text=True, timeout=10, check=True,
            )
        return re.split(r"(?m)^--usbip-bind--\n", result.stdout)

    @unittest.skipIf(os.geteuid() == 0, "the root branch runs usbip directly")
    def test_linux_uses_the_agent_when_installed(self):
        self.assertEqual(
            self.run_linux([["attach", "-r", SERVER, "-b", BUSID], ["port"]]),
            [f"agent attach -r {SERVER} -b {BUSID}\n", "agent port\n"],
        )

    @unittest.skipIf(os.geteuid() == 0, "the root branch runs usbip directly")
    def test_linux_falls_back_to_sudo(self):
        self.assertEqual(
            self.run_linux([["detach", "-p", "0"]], agent=False),
            ["sudo -n /usr/bin/usbip detach -p 0\n"],
        )

    def test_linux_command_survives_non_posix_login_shells(self):
        command = service.remote_command("linux", [["port"]])
        self.assertTrue(command.startswith("sh -c '"))
        self.assertEqual(command.count("'"), 2)

    def test_remote_failure_is_reported(self):
        result = subprocess.CompletedProcess([], 1, "", "sudo: a password is required")
        with patch.object(service, "run_raw", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "password is required"):
                service.remote_usbip(HOST, "linux", [["port"]])

    def test_ssh_failure_is_host_unreachable(self):
        result = subprocess.CompletedProcess([], 255, "", "Connection refused")
        with patch.object(service, "run_raw", return_value=result):
            with self.assertRaises(service.HostUnreachable):
                service.remote_usbip(HOST, "windows", [["port"]])

    def test_missing_output_section_is_rejected(self):
        result = subprocess.CompletedProcess([], 0, "1\n", "")
        with patch.object(service, "run_raw", return_value=result):
            with self.assertRaisesRegex(ValueError, "unexpected usbip output"):
                service.remote_usbip(HOST, "windows", [["attach"], ["port"]])

    def test_ssh_timeout_closes_a_shared_connection(self):
        calls = []

        def hang(argv, ssh_account=None, timeout=None):
            calls.append(argv)
            if "-O" not in argv:
                raise subprocess.TimeoutExpired("ssh", timeout)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch.object(service, "run_raw", side_effect=hang):
            with self.assertRaises(subprocess.TimeoutExpired):
                service.ssh_raw(HOST, "true")
        self.assertEqual(calls[1], ["/usr/bin/ssh", "-O", "exit", HOST])

    def test_subprocess_timeout_and_failure(self):
        result = subprocess.CompletedProcess(["ssh"], 1, "output", "error")
        with patch.object(service.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, r"failed \(1\)"):
                service.run(["ssh"])
        self.assertEqual(run.call_args.kwargs["timeout"], service.LOCAL_TIMEOUT)

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

    def test_saved_device_is_matched_by_bus_id_and_ids(self):
        (self.device / "idVendor").write_text("046d\n")
        (self.device / "idProduct").write_text("C547\n")
        self.assertTrue(service.same_device(BUSID, "046d", "c547"))
        self.assertFalse(service.same_device(BUSID, "046d", "c548"))
        self.assertFalse(service.same_device("3-2", "046d", "c547"))

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

    def patched(self, capture_result="windows"):
        """Patch capture and release; return their mocks."""
        capture = patch.object(service, "capture", return_value=capture_result)
        release = patch.object(service, "release")
        self.addCleanup(capture.stop)
        self.addCleanup(release.stop)
        return capture.start(), release.start()

    def test_generic_ids_have_separate_state_and_normalize_case(self):
        capture, _ = self.patched()
        self.controller.execute(self.request())
        self.controller.execute(self.request("054C", "09CC"))
        first, second = capture.call_args_list
        self.assertEqual(first.args[2], self.path / "046d-c547.json")
        self.assertEqual(second.args[2], self.path / "054c-09cc.json")
        self.assertEqual(second.args[3:5], ("054c", "09cc"))

    def test_any_valid_host_is_accepted_without_allowlist(self):
        capture, _ = self.patched()
        self.controller.execute(self.request(host="venator"))
        self.assertEqual(capture.call_args.args[0], "venator")

    def test_invalid_hosts_rejected_before_any_command(self):
        capture, _ = self.patched()
        for host in ("-oProxyCommand=x", "a b", "a;b", "", "-", 5):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "invalid host"):
                    self.controller.execute(self.request(host=host))
        capture.assert_not_called()

    def test_invalid_ids_rejected_before_privileged_operations(self):
        capture, _ = self.patched()
        for vendor in ("../x", 5, None):
            with self.assertRaisesRegex(ValueError, "hexadecimal"):
                self.controller.execute(self.request(vendor))
        capture.assert_not_called()

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
        capture, _ = self.patched()
        self.controller.execute(self.request())
        self.controller.execute(self.request(user=""))
        self.controller.execute(self.request(user="racso"))
        self.assertEqual([c.args[1] for c in capture.call_args_list], [None, None, "racso"])

    def test_platform_is_forwarded_and_the_result_reported(self):
        capture, _ = self.patched("linux")
        result = self.controller.execute(self.request(platform="windows"))
        self.controller.execute(self.request(platform=""))
        self.controller.execute(self.request())
        self.assertEqual(result["platform"], "linux")
        self.assertEqual([c.args[6] for c in capture.call_args_list], ["windows", None, None])

    def test_invalid_platform_is_rejected(self):
        capture, release = self.patched()
        for platform in ("macos", "Windows", 5, True):
            with self.subTest(platform=platform):
                with self.assertRaisesRegex(ValueError, "windows or linux"):
                    self.controller.execute(self.request(platform=platform))
        with self.assertRaisesRegex(ValueError, "only valid for capture"):
            self.controller.execute(self.request(action="release", platform="windows"))
        capture.assert_not_called()
        release.assert_not_called()

    def test_force_defaults_false_and_is_forwarded_to_release(self):
        _, release = self.patched()
        self.controller.execute(self.request(action="release"))
        self.controller.execute(self.request(action="release", force=True))
        self.assertEqual([c.kwargs["force"] for c in release.call_args_list], [False, True])

    def test_force_is_rejected_for_capture(self):
        capture, _ = self.patched()
        with self.assertRaisesRegex(ValueError, "only valid for release"):
            self.controller.execute(self.request(action="capture", force=True))
        capture.assert_not_called()

    def test_non_boolean_force_is_rejected(self):
        _, release = self.patched()
        with self.assertRaisesRegex(ValueError, "force must be a boolean"):
            self.controller.execute(self.request(action="release", force="yes"))
        release.assert_not_called()

    def test_invalid_ssh_user_rejected_before_any_command(self):
        capture, _ = self.patched()
        for user in ("-oProxyCommand=x", "a b", "x@y", "a;b", 5, ["x"]):
            with self.subTest(user=user):
                with self.assertRaisesRegex(ValueError, "invalid SSH user"):
                    self.controller.execute(self.request(user=user))
        capture.assert_not_called()

    def test_unknown_local_user_rejected(self):
        with self.assertRaises(KeyError):
            service.Controller("no-such-user-xyz", self.path)
        with self.assertRaises(ValueError):
            service.Controller("a b", self.path)

    def test_serve_rejects_non_root(self):
        with patch.object(service.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(PermissionError, "must run as root"):
                service.serve(LOCAL_USER, self.path / "socket", self.path)

    def test_recover_returns_leftover_devices_without_ssh(self):
        _, release = self.patched()
        for name in ("046d-c547.json", "054c-09cc.json", "notes.json", "046d-c547.tmp"):
            (self.path / name).write_text("{}\n")
        with self.assertLogs(level="INFO"):
            self.controller.recover()
        self.assertEqual(
            [(c.args[1:3], c.kwargs["force"]) for c in release.call_args_list],
            [(("046d", "c547"), True), (("054c", "09cc"), True)],
        )

    def with_server(self, check, handler=service.CommandHandler):
        socket_path = self.path / "control.sock"
        with service.Server(str(socket_path), handler) as server:
            server.controller = self.controller
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                check(socket_path)
            finally:
                server.shutdown()
                thread.join()

    def test_real_socket_round_trip(self):
        capture, _ = self.patched()
        self.with_server(lambda path: self.assertEqual(
            send(path, self.request())["platform"], "windows"
        ))
        self.assertEqual(capture.call_args.args[3:5], ("046d", "c547"))

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

    def test_one_connection_carries_many_requests(self):
        capture, release = self.patched()

        def check(path):
            connection = Connection(path)
            try:
                connection.send(self.request())
                connection.send(self.request(action="release"))
                connection.send(self.request("054c", "09cc"))
            finally:
                connection.close()
            eventually(lambda: release.call_count == 2)

        with self.assertLogs(level="INFO"):
            self.with_server(check)
        self.assertEqual(capture.call_count, 2)
        # The explicit release, then the disconnect releasing what was still captured.
        self.assertEqual([c.args[1:3] for c in release.call_args_list],
                         [("046d", "c547"), ("054c", "09cc")])

    def test_disconnect_releases_only_what_that_connection_captured(self):
        _, release = self.patched()

        def check(path):
            moonlight = Connection(path)
            other = Connection(path)
            try:
                moonlight.send(self.request())
                other.send(self.request("054c", "09cc"))
            finally:
                moonlight.close()
            eventually(lambda: release.call_count == 1)
            self.assertEqual(release.call_args.args[1:3], ("046d", "c547"))
            self.assertFalse(release.call_args.kwargs.get("force", False))
            other.close()
            eventually(lambda: release.call_count == 2)

        with self.assertLogs(level="INFO"):
            self.with_server(check)

    def test_capture_finishing_after_the_client_left_is_released(self):
        started = threading.Event()
        _, release = self.patched()

        def slow_capture(*args):
            started.set()
            time.sleep(0.2)
            return "windows"

        service.capture.side_effect = slow_capture

        def check(path):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(path))
                connection.sendall(json.dumps(self.request()).encode() + b"\n")
                started.wait(5)
            eventually(lambda: release.call_count == 1)
            # The service keeps working for the next client.
            self.assertEqual(send(path, self.request(action="release"))["action"], "release")

        with self.assertLogs(level="INFO"):
            self.with_server(check)

    def test_failed_capture_is_not_released_on_disconnect(self):
        _, release = self.patched()
        service.capture.side_effect = RuntimeError("attach failed")

        def check(path):
            with self.assertRaisesRegex(RuntimeError, "attach failed"):
                send(path, self.request())
            time.sleep(0.1)

        with self.assertLogs(level="ERROR"):
            self.with_server(check)
        release.assert_not_called()

    def test_failed_release_is_retried_on_disconnect(self):
        _, release = self.patched()
        release.side_effect = [RuntimeError("unbind failed"), None]

        def check(path):
            connection = Connection(path)
            try:
                connection.send(self.request())
                with self.assertRaisesRegex(RuntimeError, "unbind failed"):
                    connection.send(self.request(action="release"))
            finally:
                connection.close()
            eventually(lambda: release.call_count == 2)

        with self.assertLogs(level="INFO"):
            self.with_server(check)

    def test_service_serializes_commands_across_connections(self):
        active = 0
        maximum = 0
        errors = []
        self.patched()

        def capture(*args):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            time.sleep(0.02)
            active -= 1
            return "windows"

        service.capture.side_effect = capture

        def check(path):
            def send_one():
                try:
                    send(path, self.request())
                except (OSError, ValueError, RuntimeError) as error:
                    errors.append(error)
            clients = [threading.Thread(target=send_one) for _ in range(3)]
            for client in clients:
                client.start()
            for client in clients:
                client.join()

        with self.assertLogs(level="INFO"):
            self.with_server(check)
        self.assertEqual(errors, [])
        self.assertEqual(maximum, 1)

if __name__ == "__main__":
    unittest.main()
