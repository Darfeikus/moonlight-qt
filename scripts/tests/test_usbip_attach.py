import importlib.util
import os
from pathlib import Path
import pwd
import socketserver
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = load("usbip_attach", "usbip-attach.py")
ALLOWED = [
    ["port"],
    ["attach", "-r", "192.168.1.100", "-b", "8-1.4.2.2.4"],
    ["attach", "-r", "pc.local", "-b", "1-2"],
    ["detach", "-p", "0"],
    ["detach", "-p", "255"],
]
REJECTED = [
    [], ["list", "-l"], ["port", "-a"], ["bind", "-b", "1-2"], ["detach", "--all"],
    ["detach", "-p", "256"], ["detach", "-p", "-1"], ["detach", "-p", "1;id"],
    ["attach", "-r", "-oFoo", "-b", "1-2"], ["attach", "-r", "a b", "-b", "1-2"],
    ["attach", "-r", "h", "-b", "1-2", "--once"], ["attach", "-b", "1-2", "-r", "h"],
    ["attach", "-r", "h", "-b", "../x"], ["attach", "-r", "h", "-b", "1-2;id"],
    "port", None, [1], [["port"]],
]


class ArgumentTests(unittest.TestCase):
    def test_allowed_forms(self):
        for args in ALLOWED:
            with self.subTest(args=args):
                self.assertEqual(agent.usbip_arguments(args), args)

    def test_everything_else_is_rejected(self):
        for args in REJECTED:
            with self.subTest(args=args), self.assertRaises(ValueError):
                agent.usbip_arguments(args)

    def test_execute_runs_fixed_binary_with_clean_environment(self):
        result = subprocess.CompletedProcess([], 0, "out\n", "")
        with patch.object(agent.subprocess, "run", return_value=result) as run:
            self.assertEqual(agent.execute(["detach", "-p", "3"]), "out\n")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/usbip", "detach", "-p", "3"])
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
        self.assertNotIn("HOME", run.call_args.kwargs["env"])

    def test_failure_includes_usbip_output(self):
        result = subprocess.CompletedProcess([], 1, "", "no such device")
        with patch.object(agent.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "no such device"):
                agent.execute(["port"])


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.patches = [
            patch.object(agent, "INSTALL_PATH", root / "usbip-attach"),
            patch.object(agent, "UNIT_PATH", root / "usbip-attach.service"),
            patch.object(agent.os, "chown"),
            patch.object(agent.os, "geteuid", return_value=0),
            patch.object(agent.os, "access", return_value=True),
            patch.dict(agent.os.environ, {"SUDO_USER": pwd.getpwuid(os.getuid()).pw_name}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_install_writes_agent_and_unit_then_starts_service(self):
        with patch.object(agent, "systemctl") as systemctl:
            agent.install()
        self.assertEqual(agent.INSTALL_PATH.stat().st_mode & 0o777, 0o755)
        self.assertEqual(agent.INSTALL_PATH.read_text(), Path(agent.__file__).read_text())
        unit = agent.UNIT_PATH.read_text()
        self.assertIn(f"serve --user {pwd.getpwuid(os.getuid()).pw_name}", unit)
        self.assertIn(f"-I {agent.INSTALL_PATH} serve", unit)
        self.assertEqual(
            [c.args[0] for c in systemctl.call_args_list],
            ["daemon-reload", "enable", "restart", "is-active"],
        )

    def test_install_needs_sudo_user_and_root(self):
        with patch.dict(agent.os.environ, {"SUDO_USER": ""}):
            with self.assertRaisesRegex(ValueError, "SUDO_USER"):
                agent.install()
        with patch.dict(agent.os.environ, {"SUDO_USER": "root"}):
            with self.assertRaisesRegex(ValueError, "SUDO_USER"):
                agent.install()
        with patch.object(agent.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(PermissionError, "sudo"):
                agent.install()
        self.assertFalse(agent.INSTALL_PATH.exists())

    def test_missing_tool_is_reported_before_writing(self):
        with patch.object(agent.os, "access", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "is missing"):
                agent.install()
        self.assertFalse(agent.INSTALL_PATH.exists())

    def test_uninstall_removes_files(self):
        with patch.object(agent, "systemctl") as systemctl:
            agent.install()
            systemctl.reset_mock()
            agent.uninstall()
        self.assertFalse(agent.INSTALL_PATH.exists() or agent.UNIT_PATH.exists())
        self.assertEqual(
            [c.args[:2] for c in systemctl.call_args_list],
            [("disable", "--now"), ("daemon-reload",)],
        )


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "control.sock"
        self.fake = Path(self.directory.name) / "usbip"
        self.fake.write_text('#!/bin/sh\necho "args: $@"\n')
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)

    def with_server(self, account, check):
        with socketserver.UnixStreamServer(str(self.path), agent.Handler) as server:
            server.account = account
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            try:
                check()
            finally:
                thread.join(5)

    def me(self):
        return pwd.getpwuid(os.getuid())

    def test_client_round_trip(self):
        def check():
            self.assertEqual(
                agent.request(["detach", "-p", "7"], self.path), "args: detach -p 7\n"
            )
        with patch.object(agent, "USBIP", str(self.fake)):
            self.with_server(self.me(), check)

    def test_invalid_request_is_an_error_not_executed(self):
        def check():
            with self.assertRaisesRegex(RuntimeError, "only port"):
                agent.request(["bind", "-b", "1-2"], self.path)
        with patch.object(agent, "USBIP", str(self.fake)), patch.object(
            agent.subprocess, "run"
        ) as run:
            self.with_server(self.me(), check)
        run.assert_not_called()

    def test_other_account_is_rejected(self):
        other = pwd.struct_passwd(("x", "*", os.getuid() + 12345, 0, "", "/", "/bin/sh"))

        def check():
            with self.assertRaisesRegex(RuntimeError, "not authorized"):
                agent.request(["port"], self.path)
        if os.getuid() == 0:
            self.skipTest("root is always authorized")
        with patch.object(agent.subprocess, "run") as run:
            self.with_server(other, check)
        run.assert_not_called()

    def test_serve_requires_root_and_valid_account(self):
        with patch.object(agent.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(PermissionError, "must run as root"):
                agent.serve("someone", self.path)
        with patch.object(agent.os, "geteuid", return_value=0):
            with self.assertRaises(ValueError):
                agent.serve("a b", self.path)

    def test_client_reports_unreachable_agent(self):
        with self.assertRaises(OSError):
            agent.request(["port"], self.path)


if __name__ == "__main__":
    unittest.main()
