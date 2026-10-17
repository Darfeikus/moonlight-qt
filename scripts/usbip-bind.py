#!/usr/bin/env python3
"""Root USB/IP command service used by Moonlight to hand devices to a streaming PC.

Moonlight talks to this service directly over a local Unix socket. The service
binds/unbinds the requested USB device on this machine and runs `usbip
attach/detach` on the streaming PC over SSH. It accepts only capture, release,
status and list requests.

Install it, from the account that runs Moonlight, with:
    sudo python3 usbip-bind.py install
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
import threading
import time


ADDRESS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
ACCOUNT = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
BUSID = re.compile(r"[0-9]+-[0-9]+(?:\.[0-9]+)*")
WINDOWS_USBIP = r'"C:\Program Files\USBip\usbip.exe"'
LINUX_AGENT = "/usr/local/bin/usbip-attach"
# Picks root, the attach agent or `sudo -n` on a Linux streaming PC within the same
# SSH command, so only the OS has to be known in advance.
LINUX_RUNNER = (
    'if [ "$(id -u)" = 0 ]; then u() { LC_ALL=C /usr/bin/usbip "$@"; }; '
    'elif [ -x ' + LINUX_AGENT + ' ]; then u() { ' + LINUX_AGENT + ' "$@"; }; '
    'else u() { LC_ALL=C sudo -n /usr/bin/usbip "$@"; }; fi; '
)
MARKER = "--usbip-bind--"
# Linux lists an imported device only once it has been enumerated, a moment after
# attach returns, so the port check right after attach retries for up to about 3s.
PORT_WAIT_TRIES = 15
PLATFORMS = ("windows", "linux")
USB_ID = re.compile(r"[0-9a-fA-F]{4}")
SOCKET_PATH = Path("/run/usbip-bind/control.sock")
STATE_DIRECTORY = Path("/var/lib/usbip-bind")
SYSFS = Path("/sys/bus/usb/devices")
STATE_NAME = re.compile(r"([0-9a-f]{4})-([0-9a-f]{4})\.json")
SSH_FAILURE = 255
SSH_CONNECT_TIMEOUT = 3
SSH_TIMEOUT = 6
LOCAL_TIMEOUT = 20
ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.TimeoutExpired)
INSTALL_PATH = Path("/usr/local/lib/usbip-bind.py")
UNIT_PATH = Path("/etc/systemd/system/usbip-bind.service")
# Arch and Debian name the USB/IP server unit usbipd.service; Fedora and Bazzite
# name it usbip-server.service.
USBIPD_UNITS = ("usbipd.service", "usbip-server.service")
REQUIRED_TOOLS = (
    "/usr/bin/usbip", "/usr/bin/usbipd", "/usr/bin/ssh", "/usr/bin/python3",
    "/usr/bin/modprobe", "/usr/bin/systemctl",
)
UNIT = """[Unit]
Description=USB/IP device service for Moonlight
Requires={usbipd}
After={usbipd}

[Service]
Type=simple
User=root
ExecStartPre=/usr/bin/modprobe usbip_host
ExecStart=/usr/bin/python3 -I {path} serve --user {user}
RuntimeDirectory=usbip-bind
RuntimeDirectoryMode=0755
StateDirectory=usbip-bind
StateDirectoryMode=0700

[Install]
WantedBy=multi-user.target
"""


def run_raw(argv, ssh_account=None, timeout=LOCAL_TIMEOUT):
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
        argv, capture_output=True, text=True, timeout=timeout, **options
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
    try:
        return run_raw([
            "/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}", "-o", "ServerAliveInterval=2",
            "-o", "ServerAliveCountMax=2", target, command,
        ], ssh_account, SSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        # A shared connection (ControlMaster) to a PC that slept or rebooted can
        # hang every command; close it so the next attempt connects afresh.
        try:
            run_raw(["/usr/bin/ssh", "-O", "exit", target], ssh_account, SSH_CONNECT_TIMEOUT)
        except ERRORS:
            pass
        raise


class HostUnreachable(RuntimeError):
    """SSH itself could not reach or authenticate to the remote host, as opposed
    to the remote usbip command running and failing for some other reason."""


def detect_platform(target, ssh_account=None):
    """Identify the remote OS over SSH; Sunshine does not report it."""
    # `||` works in POSIX shells, fish and cmd.exe, so one round trip covers both.
    result = ssh_raw(target, "uname -s || ver", ssh_account)
    if result.returncode == SSH_FAILURE:
        raise HostUnreachable(f"SSH to {target} failed: {result.stderr.strip()}")
    if re.search(r"(?m)^Linux\s*$", result.stdout):
        return "linux"
    if "Microsoft Windows" in result.stdout:
        return "windows"
    raise RuntimeError(
        f"{target} is neither Linux nor Windows: {result.stdout.strip()!r}"
    )


def linux_step(args):
    """One usbip command; `port --until TEXT` repeats `port` until TEXT shows up."""
    if args[:2] != ["port", "--until"]:
        return "u " + shlex.join(args)
    return (
        '{ i=0; while o=$(u port) || exit; do '
        'case "$o" in *' + shlex.quote(args[2]) + '*) break;; esac; '
        'i=$((i+1)); [ "$i" -ge ' + str(PORT_WAIT_TRIES) + ' ] && break; sleep 0.1; '
        "done; printf '%s\\n' \"$o\"; }"
    )


def remote_command(platform, commands):
    """One remote command line that runs each usbip command in order."""
    separator = f" && echo {MARKER} && "
    if platform == "windows":
        return separator.join(WINDOWS_USBIP + " " + " ".join(args) for args in commands)
    script = LINUX_RUNNER + separator.join(linux_step(args) for args in commands)
    # Run through sh so a non-POSIX login shell (fish, csh) still works.
    return "sh -c " + shlex.quote(script)


def remote_usbip(target, platform, commands, ssh_account=None):
    """Run usbip commands on the streaming PC in one SSH session; return each output."""
    result = ssh_raw(target, remote_command(platform, commands), ssh_account)
    if result.returncode:
        message = (
            f"usbip {' + '.join(args[0] for args in commands)} on {target} failed "
            f"({result.returncode}): {result.stderr.strip()} {result.stdout.strip()}"
        )
        if result.returncode == SSH_FAILURE:
            raise HostUnreachable(message)
        raise RuntimeError(message)
    outputs = re.split(
        r"(?m)^" + re.escape(MARKER) + r"[ \t\r]*(?:\n|$)", result.stdout
    )
    if len(outputs) != len(commands):
        raise ValueError(f"unexpected usbip output from {target}: {result.stdout!r}")
    return outputs


def remote_one(target, platform, args, ssh_account=None):
    return remote_usbip(target, platform, [args], ssh_account)[0]


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
    return set_export(matches[0], action)


def set_export(busid, action):
    """Report, bind (export) or unbind one bus ID and confirm the driver changed."""
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


def same_device(busid, vendor, product):
    """Whether the device saved at busid is still plugged in there."""
    return (
        sysfs_text(busid, "idVendor").lower(), sysfs_text(busid, "idProduct").lower()
    ) == (vendor, product)


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
        or state["platform"] not in PLATFORMS
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


def attached(state, vendor, product, ssh_account):
    """Whether the saved attachment is still exported here and imported there."""
    busid = state["busid"]
    if (
        state["port"] is None or not same_device(busid, vendor, product)
        or not set_export(busid, "status")["exported"]
    ):
        return False
    target = ssh_target(state["host"], state["ssh_user"])
    ports = imported_ports(remote_one(target, state["platform"], ["port"], ssh_account))
    return ports.get(state["port"]) == (state["server"], 3240, busid)


def capture(address, user, path, vendor, product, ssh_account, platform=None):
    """Attach the device to address and return the streaming PC's OS.

    platform is the OS Moonlight saved for that PC, or None to detect it."""
    state = read_state(path)
    if state:
        reachable = True
        if (state["host"], state["ssh_user"]) == (address, user):
            try:
                if attached(state, vendor, product, ssh_account):
                    logging.info("device already attached to %s port %s", address, state["port"])
                    return state["platform"]
            except (HostUnreachable, subprocess.TimeoutExpired):
                reachable = False
            except ERRORS as error:
                logging.warning("could not verify the existing attachment: %s", error)
        # A different PC or account always wins, and a stale or broken handoff is
        # cleared the same way, so capture never needs a manual release first.
        logging.info("replacing the previous attachment to %s", state["host"])
        release(path, vendor, product, ssh_account, force=not reachable)

    local = device("status", vendor, product)
    busid = local["busid"]
    target = ssh_target(address, user)
    known = platform is not None
    if not known:
        platform = detect_platform(target, ssh_account)
    server = local_address(address)
    # Every remote probe happens before binding, so a failure leaves the device local.
    try:
        ports = remote_ports(target, platform, server, busid, ssh_account)
    except (HostUnreachable, subprocess.TimeoutExpired):
        raise
    except ERRORS:
        if not known:
            raise
        # The OS Moonlight saved for this PC may be out of date (a reinstall, say).
        detected = detect_platform(target, ssh_account)
        if detected == platform:
            raise
        logging.info("%s runs %s, not %s", address, detected, platform)
        platform = detected
        ports = remote_ports(target, platform, server, busid, ssh_account)
    expected = (server, 3240, busid)
    leftovers = [port for port, location in ports.items() if location == expected]
    state = {
        "host": address, "server": server, "busid": busid, "port": None,
        "platform": platform, "ssh_user": user,
    }
    save_state(path, state)
    try:
        for port in leftovers:
            # A dead import of this same device from an earlier handoff.
            remote_one(target, platform, ["detach", "-p", str(port)], ssh_account)
        if local["exported"]:
            # Export afresh so no earlier USB/IP connection survives.
            set_export(busid, "unbind")
        if not set_export(busid, "bind")["exported"]:
            raise RuntimeError("device did not bind as expected")
        args = ["attach", "-r", server, "-b", busid]
        port_args = ["port"]
        if platform == "windows":
            args += ["--once", "--terse"]
        else:
            port_args += ["--until", f"usbip://{server}:3240/{busid}"]
        attach_output, port_output = remote_usbip(
            target, platform, [args, port_args], ssh_account
        )
        if platform == "windows":
            if not re.fullmatch(r"\s*[0-9]+\s*", attach_output):
                raise ValueError(f"unexpected attach port response: {attach_output!r}")
            state["port"] = int(attach_output)
            if not 1 <= state["port"] <= 255:
                raise ValueError(f"invalid Windows attachment port: {state['port']}")
        matches = [
            port for port, location in imported_ports(port_output).items()
            if location == expected
        ]
        if len(matches) != 1 or (
            state["port"] is not None and state["port"] != matches[0]
        ):
            raise RuntimeError("could not verify the device's imported port")
        state["port"] = matches[0]
        save_state(path, state)
    except ERRORS:
        # Never leave the device exported without a verified attachment.
        try:
            release(path, vendor, product, ssh_account)
        except ERRORS as error:
            logging.error("could not undo the failed capture: %s", error)
        raise
    logging.info("attached device to %s port %s", address, state["port"])
    return platform


def remote_ports(target, platform, server, busid, ssh_account):
    """The streaming PC's imported ports, after ending any Windows attempts to
    re-attach this device. usbip-win2 keeps retrying an attachment whose
    connection dropped (--once only covers the first connection), and a retry
    could take the device as soon as it is bound again."""
    commands = [["port"]]
    if platform == "windows":
        commands.insert(0, ["attach", "-r", server, "-b", busid, "--stop"])
    return imported_ports(remote_usbip(target, platform, commands, ssh_account)[-1])


def detach_remote(state, ssh_account):
    target = ssh_target(state["host"], state["ssh_user"])
    ports = remote_ports(
        target, state["platform"], state["server"], state["busid"], ssh_account
    )
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
        remote_one(target, state["platform"], ["detach", "-p", str(port)], ssh_account)


def release(path, vendor, product, ssh_account, force=False):
    """Return the device here first, so it is usable again at once, then tidy up
    the streaming PC. force skips the streaming PC entirely."""
    state = read_state(path)
    if not state:
        # Nothing recorded; Moonlight is the only USB/IP user, so just make sure it is local.
        if device("unbind", vendor, product)["exported"]:
            raise RuntimeError("device remains exported after unbind")
        logging.info("device returned locally")
        return
    busid = state["busid"]
    if same_device(busid, vendor, product):
        if set_export(busid, "unbind")["exported"]:
            raise RuntimeError("device remains exported after unbind")
        logging.info("device returned locally")
    else:
        # Unplugged or moved: its export went with it, and whatever is at that
        # bus ID now is another device that must not be touched.
        logging.warning(
            "%s:%s is no longer at %s; forgetting its attachment", vendor, product, busid
        )
    path.unlink(missing_ok=True)
    if force:
        return
    try:
        detach_remote(state, ssh_account)
    except ERRORS as error:
        # Unbinding already ended the USB/IP connection, so the streaming PC
        # drops the device by itself. A Windows PC keeps trying to re-attach
        # until the next capture there stops it.
        logging.warning("could not detach on %s: %s", state["host"], error)


class Controller:
    def __init__(self, user, state_directory):
        if not ACCOUNT.fullmatch(user):
            raise ValueError("invalid local user")
        self.account = pwd.getpwnam(user)
        self.state_directory = state_directory
        # Requests from every connection run one at a time.
        self.lock = threading.Lock()
        # (vendor, product) -> the connection whose capture it follows
        self.owners = {}

    def state_path(self, vendor, product):
        return self.state_directory / f"{vendor}-{product}.json"

    def execute(self, request, owner=None):
        with self.lock:
            return self._execute(request, owner)

    def _execute(self, request, owner):
        if not isinstance(request, dict) or "action" not in request:
            raise ValueError("expected an action")
        action = request["action"]
        if action == "list":
            if set(request) != {"action"}:
                raise ValueError("list takes no parameters")
            return {"devices": list_devices()}
        required = {"action", "host", "vendor", "product"}
        if not required <= set(request) <= required | {"user", "force", "platform"}:
            raise ValueError(
                "expected action, host, vendor, product and optionally user, force, platform"
            )
        if action not in ("capture", "release", "status"):
            raise ValueError("unknown device command")
        force = request.get("force", False)
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        if force and action != "release":
            raise ValueError("force is only valid for release")
        # Empty or missing means unknown, so the service detects it.
        platform = request.get("platform") or None
        if "platform" in request and action != "capture":
            raise ValueError("platform is only valid for capture")
        if platform is not None and platform not in PLATFORMS:
            raise ValueError("platform must be windows or linux")
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
        path = self.state_path(vendor, product)
        if action == "status":
            return {
                **device("status", vendor, product),
                "attachment": read_state(path),
            }
        logging.info("%s %s:%s on %s%s", action, vendor, product, address, " (forced)" if force else "")
        key = (vendor, product)
        if action == "capture":
            # A failed capture leaves the device local, so it follows no one.
            self.owners.pop(key, None)
            platform = capture(address, ssh_user, path, vendor, product, self.account, platform)
            self.owners[key] = owner
            return {"action": action, "vendor": vendor, "product": product, "platform": platform}
        release(path, vendor, product, self.account, force=force)
        self.owners.pop(key, None)
        return {"action": action, "vendor": vendor, "product": product}

    def disconnected(self, owner):
        """Release whatever this connection captured: Moonlight quit, crashed or was killed."""
        with self.lock:
            for key in [key for key, found in self.owners.items() if found is owner]:
                del self.owners[key]
                vendor, product = key
                logging.info("Moonlight disconnected; releasing %s:%s", vendor, product)
                try:
                    release(self.state_path(vendor, product), vendor, product, self.account)
                except ERRORS as error:
                    logging.error("could not release %s:%s: %s", vendor, product, error)

    def recover(self):
        """Return devices a previous run left exported; no Moonlight holds them now.

        Unbinding ends their USB/IP connections, so the streaming PCs are not
        contacted; a later capture clears any import left there."""
        with self.lock:
            for path in sorted(self.state_directory.glob("*.json")):
                name = STATE_NAME.fullmatch(path.name)
                if not name:
                    continue
                logging.info("returning %s:%s left over from a previous run", name[1], name[2])
                try:
                    release(path, name[1], name[2], self.account, force=True)
                except ERRORS as error:
                    logging.error("could not return %s:%s: %s", name[1], name[2], error)


class CommandHandler(socketserver.StreamRequestHandler):
    """One client connection carrying newline-delimited JSON requests.

    Moonlight keeps its connection open; devices it captured are released as
    soon as it closes, however Moonlight ends."""

    def handle(self):
        controller = self.server.controller
        try:
            try:
                credentials = self.connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                _, uid, _ = struct.unpack("3i", credentials)
                if uid not in (0, controller.account.pw_uid):
                    raise PermissionError("this account is not authorized to control USB devices")
            except ERRORS as error:
                logging.error("USB/IP command failed: %s", error)
                self.reply({"ok": False, "error": str(error)})
                return
            while True:
                try:
                    line = self.rfile.readline(4097)
                except OSError:
                    return
                if not line:
                    return
                try:
                    if len(line) > 4096 or not line.endswith(b"\n"):
                        raise ValueError("invalid or oversized command")
                    result = controller.execute(json.loads(line), owner=self)
                    response = {"ok": True, "result": result}
                except ERRORS as error:
                    logging.error("USB/IP command failed: %s", error)
                    response = {"ok": False, "error": str(error)}
                # A client that is gone, or a line we cannot resynchronize after, ends it.
                if not self.reply(response) or not line.endswith(b"\n"):
                    return
        finally:
            controller.disconnected(self)

    def reply(self, response):
        try:
            self.wfile.write(json.dumps(response).encode() + b"\n")
            return True
        except OSError:
            return False


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    # Moonlight holds its connection for as long as it runs.
    daemon_threads = True


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
    controller.recover()
    with Server(str(socket_path), CommandHandler) as server:
        server.controller = controller
        os.chown(socket_path, controller.account.pw_uid, controller.account.pw_gid)
        socket_path.chmod(0o600)
        logging.info("listening on %s for %s", socket_path, controller.account.pw_name)
        try:
            server.serve_forever()
        finally:
            socket_path.unlink(missing_ok=True)


def reset(vendor, product, socket_path):
    """Force-release a device over the running service's socket, bypassing the
    requirement to reach the host it was last attached to. Use this when that
    host is gone for good (decommissioned, reinstalled, address reused)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(90)
        connection.connect(str(socket_path))
        request = {
            "action": "release", "host": "localhost",
            "vendor": vendor, "product": product, "force": True,
        }
        connection.sendall(json.dumps(request).encode() + b"\n")
        with connection.makefile("rb") as response:
            reply = json.loads(response.readline())
    if not reply["ok"]:
        raise RuntimeError(reply["error"])
    logging.info("cleared any stale attachment for %s:%s", vendor, product)


def systemctl(*args):
    subprocess.run(["/usr/bin/systemctl", *args], check=True, timeout=60)


def write_file(path, content, mode):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content)
    temporary.chmod(mode)
    os.chown(temporary, 0, 0)
    temporary.replace(path)


def usbipd_unit():
    for unit in USBIPD_UNITS:
        found = subprocess.run(
            ["/usr/bin/systemctl", "cat", unit],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
        )
        if found.returncode == 0:
            return unit
    raise RuntimeError(f"neither {' nor '.join(USBIPD_UNITS)} was found; install the usbip package")


def install():
    """Install this file as the service, for the account that ran sudo."""
    if os.geteuid() != 0:
        raise PermissionError("run this with sudo")
    user = os.environ.get("SUDO_USER", "")
    if not user or user == "root" or not ACCOUNT.fullmatch(user):
        raise ValueError(
            "run it as 'sudo python3 usbip-bind.py install' from the account "
            "that runs Moonlight (SUDO_USER is not set)"
        )
    pwd.getpwnam(user)
    for tool in REQUIRED_TOOLS:
        if not os.access(tool, os.X_OK):
            raise RuntimeError(f"{tool} is missing (Arch: sudo pacman -S usbip openssh python)")
    unit = UNIT.format(usbipd=usbipd_unit(), path=INSTALL_PATH, user=user)
    INSTALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_file(INSTALL_PATH, Path(__file__).read_text(), 0o644)
    write_file(UNIT_PATH, unit, 0o644)
    systemctl("daemon-reload")
    systemctl("enable", "usbip-bind.service")
    systemctl("restart", "usbip-bind.service")
    time.sleep(1)
    try:
        systemctl("is-active", "--quiet", "usbip-bind.service")
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "usbip-bind.service did not start; see: journalctl -u usbip-bind.service"
        ) from None
    print(f"usbip-bind.service is running for '{user}' (socket {SOCKET_PATH}).")


def uninstall(state_directory=STATE_DIRECTORY):
    if os.geteuid() != 0:
        raise PermissionError("run this with sudo")
    if state_directory.is_dir() and any(state_directory.glob("*.json")):
        raise RuntimeError(
            f"devices are still captured ({state_directory} has saved attachments); "
            "release them from Moonlight first"
        )
    if UNIT_PATH.exists():
        systemctl("disable", "--now", "usbip-bind.service")
    UNIT_PATH.unlink(missing_ok=True)
    INSTALL_PATH.unlink(missing_ok=True)
    systemctl("daemon-reload")
    print(f"Removed usbip-bind.service. Left in place: {state_directory}.")
    print("The USB/IP server unit and the usbip packages were not touched.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    service_cmd = commands.add_parser("serve", help="run the root command service")
    service_cmd.add_argument("--user", required=True, help="account that runs Moonlight")
    service_cmd.add_argument("--state-directory", type=Path, default=STATE_DIRECTORY)
    service_cmd.add_argument("--socket", type=Path, default=SOCKET_PATH)
    reset_cmd = commands.add_parser(
        "reset",
        help="force-release a device stuck on an unreachable host (requires the service to be running)",
    )
    reset_cmd.add_argument("vendor", help="four hex digit USB vendor ID")
    reset_cmd.add_argument("product", help="four hex digit USB product ID")
    reset_cmd.add_argument("--socket", type=Path, default=SOCKET_PATH)
    commands.add_parser(
        "install", help="install this file as usbip-bind.service for the account that ran sudo"
    )
    commands.add_parser("uninstall", help="remove usbip-bind.service")
    args = parser.parse_args()
    if args.action in ("install", "uninstall"):
        try:
            install() if args.action == "install" else uninstall()
        except (*ERRORS, subprocess.CalledProcessError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.action == "serve":
            serve(args.user, args.socket, args.state_directory)
        else:
            reset(args.vendor, args.product, args.socket)
    except ERRORS as error:
        logging.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
