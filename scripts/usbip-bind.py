#!/usr/bin/env python3
"""Root USB/IP command service used by Moonlight to hand devices to a streaming PC.

Moonlight talks to this service directly over a local Unix socket. The service
binds/unbinds the requested USB device on this machine and runs `usbip
attach/detach` on the streaming PC over SSH. It accepts only capture, release,
status and list requests.
"""

import argparse
import json
import logging
import os
from pathlib import Path
import pwd
import re
import shlex
import socket
import socketserver
import struct
import subprocess
import sys


ADDRESS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
ACCOUNT = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
BUSID = re.compile(r"[0-9]+-[0-9]+(?:\.[0-9]+)*")
WINDOWS_USBIP = r'"C:\Program Files\USBip\usbip.exe"'
LINUX_AGENT = "/usr/local/bin/usbip-attach"
USB_ID = re.compile(r"[0-9a-fA-F]{4}")
SOCKET_PATH = Path("/run/usbip-bind/control.sock")
STATE_DIRECTORY = Path("/var/lib/usbip-bind")
SYSFS = Path("/sys/bus/usb/devices")
SSH_FAILURE = 255
ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired)


def run_raw(argv, ssh_account=None):
    options = {}
    if ssh_account is not None:
        environment = dict(os.environ)
        environment.update(
            HOME=ssh_account.pw_dir, USER=ssh_account.pw_name,
            LOGNAME=ssh_account.pw_name,
        )
        environment.pop("SSH_AUTH_SOCK", None)
        options.update(
            user=ssh_account.pw_uid, group=ssh_account.pw_gid,
            cwd=ssh_account.pw_dir, env=environment,
        )
        if os.geteuid() == 0:
            options["extra_groups"] = os.getgrouplist(
                ssh_account.pw_name, ssh_account.pw_gid
            )
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=20, **options
    )


def run(argv, ssh_account=None):
    result = run_raw(argv, ssh_account)
    if result.returncode:
        raise RuntimeError(
            f"{shlex.join(argv)} failed ({result.returncode}): "
            f"{result.stderr.strip()} {result.stdout.strip()}"
        )
    return result.stdout


def ssh_target(address, user):
    return f"{user}@{address}" if user else address


def ssh_raw(target, command, ssh_account=None):
    return run_raw([
        "/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=1", target, command,
    ], ssh_account)


def detect_platform(target, ssh_account=None):
    """Identify the remote OS over SSH; Sunshine does not report it."""
    # Run through sh so a non-POSIX login shell (fish, csh) still works.
    probe = f'sh -c "uname -s && id -u && {{ test -x {LINUX_AGENT} && echo agent || echo none; }}"'
    first = ssh_raw(target, probe, ssh_account)
    if first.returncode == SSH_FAILURE:
        raise RuntimeError(f"SSH to {target} failed: {first.stderr.strip()}")
    if first.returncode == 0:
        fields = first.stdout.split()
        if len(fields) == 3 and fields[0] == "Linux" and fields[1].isdigit() and fields[2] in ("agent", "none"):
            return {
                "platform": "linux", "sudo": fields[1] != "0",
                "agent": fields[1] != "0" and fields[2] == "agent",
            }
        raise ValueError(f"unrecognized platform response from {target}: {first.stdout!r}")
    second = ssh_raw(target, WINDOWS_USBIP + " port", ssh_account)
    if second.returncode == SSH_FAILURE:
        raise RuntimeError(f"SSH to {target} failed: {second.stderr.strip()}")
    if second.returncode == 0:
        return {"platform": "windows", "sudo": False, "agent": False}
    raise RuntimeError(f"{target} is neither Linux nor Windows with USBip installed")


def remote_usbip(target, info, args, ssh_account=None):
    if info["platform"] == "windows":
        command = WINDOWS_USBIP + " " + " ".join(args)
    elif info.get("agent"):
        # The root attach agent on the streaming PC needs no sudo rule.
        command = LINUX_AGENT + " " + shlex.join(args)
    else:
        prefix = "sudo -n " if info["sudo"] else ""
        command = "LC_ALL=C " + prefix + "/usr/bin/usbip " + shlex.join(args)
    result = ssh_raw(target, command, ssh_account)
    if result.returncode:
        raise RuntimeError(
            f"usbip {args[0]} on {target} failed ({result.returncode}): "
            f"{result.stderr.strip()} {result.stdout.strip()}"
        )
    return result.stdout


def local_address(address):
    """Address of this machine that the target would use to reach us."""
    family, kind, protocol, _, endpoint = socket.getaddrinfo(
        address, 3240, socket.AF_INET, socket.SOCK_DGRAM
    )[0]
    with socket.socket(family, kind, protocol) as probe:
        probe.connect(endpoint)
        return probe.getsockname()[0]


def imported_ports(output):
    ports = {}
    for block in re.split(r"(?m)^(?=Port\s+[0-9]+:)", output):
        header = re.match(r"Port\s+([0-9]+):", block)
        if not header:
            continue
        location = re.search(
            r"usbip://([A-Za-z0-9_.-]+):([0-9]+)/([0-9]+-[0-9.]+)", block
        )
        if location:
            port = int(header[1])
            if port in ports:
                raise ValueError(f"duplicate imported port {port}")
            ports[port] = (location[1], int(location[2]), location[3])
        elif "in use" in block.lower():
            raise ValueError("unrecognized USB/IP imported port output")
    return ports


def local_devices():
    devices = []
    for line in run(["/usr/bin/usbip", "list", "-p", "-l"]).splitlines():
        entry = re.match(r"busid=([^#]+)#usbid=([0-9a-fA-F]{4}):([0-9a-fA-F]{4})#", line)
        if entry:
            if not BUSID.fullmatch(entry[1]):
                raise ValueError("invalid bus ID in local USB/IP device list")
            devices.append((entry[1], entry[2].lower(), entry[3].lower()))
    return devices


def device(action, vendor, product):
    matches = [
        busid for busid, found_vendor, found_product in local_devices()
        if (found_vendor, found_product) == (vendor, product)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {vendor}:{product} device, found {len(matches)}"
        )
    busid = matches[0]
    driver = SYSFS / busid / "driver"
    if not driver.exists():
        raise RuntimeError("selected device has no bound USB driver")
    exported = driver.resolve().name == "usbip-host"
    if (action == "bind" and not exported) or (action == "unbind" and exported):
        run(["/usr/bin/usbip", action, "-b", busid])
        if not driver.exists():
            raise RuntimeError(f"selected device has no bound USB driver after {action}")
        exported = driver.resolve().name == "usbip-host"
        if exported != (action == "bind"):
            raise RuntimeError(f"unexpected driver after {action}")
    return {"busid": busid, "exported": exported}


def usbip_names(output):
    return {
        entry[1]: entry[2] for entry in re.finditer(
            r"(?m)^ - busid (\S+) \([0-9a-f:]+\)\n\s+(.*?) \([0-9a-f:]+\)$", output
        )
    }


def sysfs_text(busid, attribute):
    try:
        return (SYSFS / busid / attribute).read_text().strip()
    except OSError:
        return ""


def list_devices():
    found = local_devices()
    try:
        fallback = usbip_names(run(["/usr/bin/usbip", "list", "-l"]))
    except ERRORS:
        fallback = {}
    counts = {}
    for _, vendor, product in found:
        counts[(vendor, product)] = counts.get((vendor, product), 0) + 1
    devices = []
    for busid, vendor, product in found:
        if sysfs_text(busid, "bDeviceClass") == "09":
            continue
        name = " ".join(filter(None, (
            sysfs_text(busid, "manufacturer"), sysfs_text(busid, "product"),
        ))) or fallback.get(busid, "")
        driver = SYSFS / busid / "driver"
        devices.append({
            "vendor": vendor, "product": product, "name": name, "busid": busid,
            "exported": driver.exists() and driver.resolve().name == "usbip-host",
            "count": counts[(vendor, product)],
        })
    return devices


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state) + "\n")
    temporary.replace(path)


def read_state(path):
    if not path.exists():
        return None
    state = json.loads(path.read_text())
    if (
        not ADDRESS.fullmatch(state["host"])
        or not ADDRESS.fullmatch(state["server"])
        or not BUSID.fullmatch(state["busid"])
        or state["platform"] not in ("windows", "linux")
        or type(state["sudo"]) is not bool
        or type(state.get("agent", False)) is not bool
        or (state["ssh_user"] is not None and (
            not isinstance(state["ssh_user"], str)
            or not ACCOUNT.fullmatch(state["ssh_user"])
        ))
        or (state["port"] is not None and (
            type(state["port"]) is not int or not 0 <= state["port"] <= 255
        ))
    ):
        raise ValueError("invalid saved attachment state")
    return state


def capture(address, user, path, vendor, product, ssh_account):
    state = read_state(path)
    local = device("status", vendor, product)
    if state:
        if state["host"] != address:
            raise RuntimeError("release the previous host before capturing another")
        target = ssh_target(state["host"], state["ssh_user"])
        ports = imported_ports(remote_usbip(target, state, ["port"], ssh_account))
        expected = (state["server"], 3240, local["busid"])
        if (
            state["busid"] == local["busid"]
            and state["port"] is not None
            and ports.get(state["port"]) == expected
            and local["exported"]
        ):
            logging.info("device already attached to %s port %s", address, state["port"])
            return
        raise RuntimeError("unfinished or stale handoff; run release before capturing")

    target = ssh_target(address, user)
    # Every remote probe happens before binding, so a failure leaves the device local.
    info = detect_platform(target, ssh_account)
    server = local_address(address)
    ports = imported_ports(remote_usbip(target, info, ["port"], ssh_account))
    expected = (server, 3240, local["busid"])
    if expected in ports.values() or local["exported"]:
        raise RuntimeError("device is already exported or attached outside this handoff")
    state = {
        "host": address, "server": server, "busid": local["busid"], "port": None,
        "platform": info["platform"], "sudo": info["sudo"],
        "agent": info.get("agent", False), "ssh_user": user,
    }
    save_state(path, state)
    exported = device("bind", vendor, product)
    if exported["busid"] != state["busid"] or not exported["exported"]:
        raise RuntimeError("device did not bind as expected")
    args = ["attach", "-r", server, "-b", state["busid"]]
    if info["platform"] == "windows":
        args += ["--once", "--terse"]
    output = remote_usbip(target, info, args, ssh_account)
    if info["platform"] == "windows":
        if not re.fullmatch(r"\s*[0-9]+\s*", output):
            raise ValueError(f"unexpected attach port response: {output!r}")
        port = int(output)
        if not 1 <= port <= 255:
            raise ValueError(f"invalid Windows attachment port: {port}")
        state["port"] = port
        save_state(path, state)
    ports = imported_ports(remote_usbip(target, info, ["port"], ssh_account))
    matches = [port for port, location in ports.items() if location == expected]
    if len(matches) != 1 or (
        state["port"] is not None and state["port"] != matches[0]
    ):
        raise RuntimeError("could not verify the device's imported port")
    state["port"] = matches[0]
    save_state(path, state)
    logging.info("attached device to %s port %s", address, state["port"])


def release(path, vendor, product, ssh_account):
    state = read_state(path)
    errors = []
    if state:
        try:
            target = ssh_target(state["host"], state["ssh_user"])
            ports = imported_ports(remote_usbip(target, state, ["port"], ssh_account))
            expected = (state["server"], 3240, state["busid"])
            port = state["port"]
            if port is None:
                matches = [p for p, location in ports.items() if location == expected]
                if len(matches) > 1:
                    raise RuntimeError("ambiguous imported device; refusing detach")
                port = matches[0] if matches else None
            if port is not None and port in ports:
                if ports[port] != expected:
                    raise RuntimeError("saved port belongs to another device; refusing detach")
                remote_usbip(target, state, ["detach", "-p", str(port)], ssh_account)
        except ERRORS as error:
            errors.append(f"remote detach: {error}")
    try:
        local = device("status", vendor, product)
        if state and local["busid"] != state["busid"]:
            raise RuntimeError("device topology changed; inspect the previous export")
        local = device("unbind", vendor, product)
        if local["exported"]:
            raise RuntimeError("device remains exported after unbind")
    except ERRORS as error:
        errors.append(f"local unbind: {error}")
    if errors:
        raise RuntimeError("; ".join(errors))
    path.unlink(missing_ok=True)
    logging.info("device returned locally")


class Controller:
    def __init__(self, user, state_directory):
        if not ACCOUNT.fullmatch(user):
            raise ValueError("invalid local user")
        self.account = pwd.getpwnam(user)
        self.state_directory = state_directory

    def execute(self, request):
        if not isinstance(request, dict) or "action" not in request:
            raise ValueError("expected an action")
        action = request["action"]
        if action == "list":
            if set(request) != {"action"}:
                raise ValueError("list takes no parameters")
            return {"devices": list_devices()}
        required = {"action", "host", "vendor", "product"}
        if not required <= set(request) <= required | {"user"}:
            raise ValueError("expected action, host, vendor, product and optionally user")
        if action not in ("capture", "release", "status"):
            raise ValueError("unknown device command")
        vendor, product = request["vendor"], request["product"]
        if (
            not isinstance(vendor, str) or not isinstance(product, str)
            or not USB_ID.fullmatch(vendor) or not USB_ID.fullmatch(product)
        ):
            raise ValueError("vendor and product must be four hexadecimal digits")
        vendor, product = vendor.lower(), product.lower()
        address = request["host"]
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise ValueError("invalid host address")
        # An empty or missing user means plain `ssh HOST`, i.e. the account's own SSH defaults.
        ssh_user = request.get("user") or None
        if ssh_user is not None and (
            not isinstance(ssh_user, str) or not ACCOUNT.fullmatch(ssh_user)
        ):
            raise ValueError("invalid SSH user")
        path = self.state_directory / f"{vendor}-{product}.json"
        if action == "status":
            return {
                **device("status", vendor, product),
                "attachment": read_state(path),
            }
        logging.info("%s %s:%s on %s", action, vendor, product, address)
        if action == "capture":
            capture(address, ssh_user, path, vendor, product, self.account)
        else:
            release(path, vendor, product, self.account)
        return {"action": action, "vendor": vendor, "product": product}


class CommandHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(10)
        try:
            credentials = self.connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _, uid, _ = struct.unpack("3i", credentials)
            if uid not in (0, self.server.controller.account.pw_uid):
                raise PermissionError("this account is not authorized to control USB devices")
            line = self.rfile.readline(4097)
            if len(line) > 4096 or not line.endswith(b"\n"):
                raise ValueError("invalid or oversized command")
            result = self.server.controller.execute(json.loads(line))
            response = {"ok": True, "result": result}
        except ERRORS as error:
            logging.error("USB/IP command failed: %s", error)
            response = {"ok": False, "error": str(error)}
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
        except OSError:
            # The client may already be gone, for example Moonlight quitting.
            pass


def serve(user, socket_path, state_directory):
    if os.geteuid() != 0:
        raise PermissionError("the service must run as root")
    os.umask(0o077)
    controller = Controller(user, state_directory)
    state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (
        state_directory.is_symlink()
        or state_directory.stat().st_uid != 0
        or state_directory.stat().st_mode & 0o022
    ):
        raise PermissionError("state directory must be root-owned and not writable by others")
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if socket_path.parent.is_symlink() or socket_path.parent.stat().st_uid != 0:
        raise PermissionError("socket directory must be root-owned and not a symlink")
    socket_path.parent.chmod(0o755)
    with socketserver.UnixStreamServer(str(socket_path), CommandHandler) as server:
        server.controller = controller
        os.chown(socket_path, controller.account.pw_uid, controller.account.pw_gid)
        socket_path.chmod(0o600)
        logging.info("listening on %s for %s", socket_path, controller.account.pw_name)
        try:
            server.serve_forever()
        finally:
            socket_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    service = commands.add_parser("serve", help="run the root command service")
    service.add_argument("--user", required=True, help="account that runs Moonlight")
    service.add_argument("--state-directory", type=Path, default=STATE_DIRECTORY)
    service.add_argument("--socket", type=Path, default=SOCKET_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        serve(args.user, args.socket, args.state_directory)
    except ERRORS as error:
        logging.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
