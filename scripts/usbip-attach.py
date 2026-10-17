#!/usr/bin/env python3
"""Root USB/IP attach agent for a Linux streaming PC.

`usbip attach` and `usbip detach` need root. This agent runs as root and accepts
requests from one local account (the account the other PC logs in with over
SSH), so that account needs no sudo rule. It only runs /usr/bin/usbip with these
exact argument forms:

  usbip-attach port
  usbip-attach attach -r ADDRESS -b BUSID
  usbip-attach detach -p PORT

This one file is everything to deploy. Copy it to the streaming PC and run it:

  sudo python3 usbip-attach.py install      (from the SSH login account)
  sudo python3 usbip-attach.py uninstall

`serve --user NAME` is the root daemon (started by the systemd unit that `install`
writes). Any other arguments make it the client: it forwards them to the daemon
and prints the output of usbip.
"""

import argparse
import json
import logging
import os
from pathlib import Path
import pwd
import re
import socket
import socketserver
import struct
import subprocess
import sys

ADDRESS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
ACCOUNT = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
BUSID = re.compile(r"[0-9]+-[0-9]+(?:\.[0-9]+)*")
PORT = re.compile(r"[0-9]{1,3}")
SOCKET_PATH = Path("/run/usbip-attach/control.sock")
USBIP = "/usr/bin/usbip"
INSTALL_PATH = Path("/usr/local/bin/usbip-attach")
UNIT_PATH = Path("/etc/systemd/system/usbip-attach.service")
UNIT = """[Unit]
Description=USB/IP attach agent for Moonlight
After=local-fs.target

[Service]
Type=simple
User=root
ExecStartPre=/usr/bin/modprobe vhci_hcd
ExecStart=/usr/bin/python3 -I {path} serve --user {user}
RuntimeDirectory=usbip-attach
RuntimeDirectoryMode=0755

[Install]
WantedBy=multi-user.target
"""
ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired)


def usbip_arguments(args):
    """Return the validated usbip arguments, or raise ValueError."""
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError("invalid arguments")
    if args == ["port"]:
        return args
    if (
        len(args) == 5 and args[0] == "attach" and args[1] == "-r" and args[3] == "-b"
        and ADDRESS.fullmatch(args[2]) and BUSID.fullmatch(args[4])
    ):
        return args
    if (
        len(args) == 3 and args[0] == "detach" and args[1] == "-p"
        and PORT.fullmatch(args[2]) and int(args[2]) <= 255
    ):
        return args
    raise ValueError("only port, attach -r ADDRESS -b BUSID and detach -p PORT are allowed")


def execute(args):
    result = subprocess.run(
        [USBIP, *usbip_arguments(args)], capture_output=True, text=True, timeout=20,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    if result.returncode:
        raise RuntimeError(
            f"usbip {args[0]} failed ({result.returncode}): "
            f"{result.stderr.strip()} {result.stdout.strip()}"
        )
    return result.stdout


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(25)
        try:
            credentials = self.connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _, uid, _ = struct.unpack("3i", credentials)
            if uid not in (0, self.server.account.pw_uid):
                raise PermissionError("this account is not authorized to attach USB devices")
            line = self.rfile.readline(1025)
            if len(line) > 1024 or not line.endswith(b"\n"):
                raise ValueError("invalid or oversized request")
            request = json.loads(line)
            if not isinstance(request, dict) or set(request) != {"args"}:
                raise ValueError("expected only args")
            response = {"ok": True, "output": execute(request["args"])}
        except ERRORS as error:
            logging.error("request failed: %s", error)
            response = {"ok": False, "error": str(error)}
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except OSError:
            pass


def serve(user, socket_path):
    if os.geteuid() != 0:
        raise PermissionError("the agent must run as root")
    if not ACCOUNT.fullmatch(user):
        raise ValueError("invalid account")
    account = pwd.getpwnam(user)
    os.umask(0o077)
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if socket_path.parent.is_symlink() or socket_path.parent.stat().st_uid != 0:
        raise PermissionError("socket directory must be root-owned and not a symlink")
    socket_path.parent.chmod(0o755)
    with socketserver.UnixStreamServer(str(socket_path), Handler) as server:
        server.account = account
        os.chown(socket_path, account.pw_uid, account.pw_gid)
        socket_path.chmod(0o600)
        logging.info("listening on %s for %s", socket_path, account.pw_name)
        try:
            server.serve_forever()
        finally:
            socket_path.unlink(missing_ok=True)


def request(args, socket_path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30)
        connection.connect(str(socket_path))
        connection.sendall(json.dumps({"args": args}).encode() + b"\n")
        data = connection.makefile("rb").readline()
    response = json.loads(data)
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "request failed"))
    return response["output"]


def systemctl(*args):
    subprocess.run(["/usr/bin/systemctl", *args], check=True, timeout=60)


def write_file(path, content, mode):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content)
    temporary.chmod(mode)
    os.chown(temporary, 0, 0)
    temporary.replace(path)


def install():
    if os.geteuid() != 0:
        raise PermissionError("run this with sudo")
    user = os.environ.get("SUDO_USER", "")
    if not user or user == "root" or not ACCOUNT.fullmatch(user):
        raise ValueError(
            "run it as 'sudo python3 usbip-attach.py install' from the account "
            "the Moonlight PC logs in with (SUDO_USER is not set)"
        )
    pwd.getpwnam(user)
    for tool in (USBIP, "/usr/bin/python3", "/usr/bin/modprobe", "/usr/bin/systemctl"):
        if not os.access(tool, os.X_OK):
            raise RuntimeError(f"{tool} is missing (Arch: sudo pacman -S usbip python)")
    write_file(INSTALL_PATH, Path(__file__).read_text(), 0o755)
    write_file(UNIT_PATH, UNIT.format(path=INSTALL_PATH, user=user), 0o644)
    systemctl("daemon-reload")
    systemctl("enable", "usbip-attach.service")
    systemctl("restart", "usbip-attach.service")
    systemctl("is-active", "--quiet", "usbip-attach.service")
    print(f"usbip-attach.service is running for '{user}' (socket {SOCKET_PATH}).")


def uninstall():
    if os.geteuid() != 0:
        raise PermissionError("run this with sudo")
    if UNIT_PATH.exists():
        systemctl("disable", "--now", "usbip-attach.service")
    UNIT_PATH.unlink(missing_ok=True)
    INSTALL_PATH.unlink(missing_ok=True)
    systemctl("daemon-reload")
    print("Removed usbip-attach.service. Devices that are still attached were not detached.")


def main():
    if len(sys.argv) == 2 and sys.argv[1] in ("install", "uninstall"):
        try:
            install() if sys.argv[1] == "install" else uninstall()
        except (*ERRORS, subprocess.CalledProcessError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        parser = argparse.ArgumentParser(prog="usbip-attach serve")
        parser.add_argument("serve")
        parser.add_argument("--user", required=True, help="account that logs in over SSH")
        parser.add_argument("--socket", type=Path, default=SOCKET_PATH)
        args = parser.parse_args()
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        try:
            serve(args.user, args.socket)
        except ERRORS as error:
            logging.error("%s", error)
            return 1
        return 0
    try:
        sys.stdout.write(request(sys.argv[1:], SOCKET_PATH))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"usbip-attach: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
