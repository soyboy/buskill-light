#!/bin/bash
# Usage: sudo bash uninstall.sh [user]
set -uo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "Run as root: sudo $0" >&2; exit 1; fi
U="${1:-${SUDO_USER:-}}"
if [ -z "$U" ] && [ -n "${PKEXEC_UID:-}" ]; then U="$(id -nu "$PKEXEC_UID")"; fi
systemctl disable --now buskill-light.service 2>/dev/null
rm -f /etc/systemd/system/buskill-light.service /usr/local/bin/buskill-light
rm -rf /usr/local/lib/buskill-light /run/buskill-light /etc/buskill-light
systemctl daemon-reload
if [ -n "$U" ]; then
	UHOME="$(getent passwd "$U" | cut -d: -f6)"
	rm -f "$UHOME/.local/share/applications/org.buskill.Light.desktop" "$UHOME/.config/autostart/org.buskill.Light.desktop"
fi
echo "BusKill Light removed."
