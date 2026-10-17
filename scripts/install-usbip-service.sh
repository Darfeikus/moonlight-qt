#!/bin/bash
# Installs (or removes) the root USB/IP service that Moonlight talks to.
#
#   sudo scripts/install-usbip-service.sh
#   sudo scripts/install-usbip-service.sh --uninstall
#
# The service runs as root and only accepts requests from the account that ran
# sudo.

set -euo pipefail

DAEMON=/usr/local/lib/usbip-bind.py
UNIT=/etc/systemd/system/usbip-bind.service
STATE=/var/lib/usbip-bind
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "error: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this script with sudo"

uninstall=0
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) uninstall=1 ;;
        -h|--help) sed -n "2,7p" "$0"; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

if [ "$uninstall" -eq 1 ]; then
    if compgen -G "$STATE/*.json" >/dev/null; then
        die "devices are still captured ($STATE has saved attachments); release them from Moonlight first"
    fi
    if systemctl cat usbip-bind.service >/dev/null 2>&1; then
        systemctl disable --now usbip-bind.service
    fi
    rm -f "$UNIT" "$DAEMON"
    systemctl daemon-reload
    echo "Removed usbip-bind.service. Left in place: $STATE."
    echo "usbipd.service and the usbip packages were not touched."
    exit 0
fi

user="${SUDO_USER:-}"
{ [ -n "$user" ] && [ "$user" != root ]; } ||
    die "run as 'sudo $0' from the account that runs Moonlight (SUDO_USER is not set)"
[[ "$user" =~ ^[A-Za-z_][A-Za-z0-9_.-]*$ ]] || die "unsupported account name: $user"
id "$user" >/dev/null || die "unknown account: $user"
for tool in /usr/bin/usbip /usr/bin/usbipd /usr/bin/ssh /usr/bin/python3; do
    [ -x "$tool" ] || die "$tool is missing (Arch: sudo pacman -S usbip openssh python)"
done
systemctl cat usbipd.service >/dev/null 2>&1 || die "usbipd.service not found; install the usbip package"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

install -o root -g root -m 0644 "$SOURCE/usbip-bind.py" "$DAEMON"
sed "s/@USER@/$user/" "$SOURCE/usbip-bind.service" > "$work/unit"
install -o root -g root -m 0644 "$work/unit" "$UNIT"

systemctl daemon-reload
systemctl enable usbip-bind.service
systemctl restart usbip-bind.service ||
    die "usbip-bind.service failed to start; see: journalctl -u usbip-bind.service"
sleep 1
systemctl is-active --quiet usbip-bind.service ||
    die "usbip-bind.service did not start; see: journalctl -u usbip-bind.service"
echo "usbip-bind.service is running for '$user' (socket /run/usbip-bind/control.sock)."
