#!/bin/bash
# Usage: sudo bash install.sh [user]
set -euo pipefail
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
	echo "Run as root: sudo $0" >&2
	exit 1
fi
U="${1:-${SUDO_USER:-}}"
if [ -z "$U" ] && [ -n "${PKEXEC_UID:-}" ]; then U="$(id -nu "$PKEXEC_UID")"; fi
if [ -z "$U" ] || [ "$U" = root ]; then
	echo "Pass your user name: sudo $0 <user>" >&2
	exit 1
fi
id "$U" >/dev/null
UHOME="$(getent passwd "$U" | cut -d: -f6)"

echo "==> Installing program files"
install -d -m 755 /usr/local/lib/buskill-light
install -m 755 "$SRC/buskill_lightd.py" /usr/local/lib/buskill-light/buskill_lightd.py
install -m 755 "$SRC/buskill-light" /usr/local/bin/buskill-light
install -m 644 "$SRC/buskill-light.service" /etc/systemd/system/buskill-light.service

echo "==> Configuration"
if [ -f /etc/buskill-light/config.ini ]; then
	/usr/bin/python3 -I /usr/local/lib/buskill-light/buskill_lightd.py --refresh-config
else
	/usr/bin/python3 -I /usr/local/lib/buskill-light/buskill_lightd.py --write-default-config "$U"
fi

echo "==> System service"
systemctl daemon-reload
systemctl enable buskill-light.service
systemctl restart buskill-light.service

echo "==> Menu entry and autostart for $U"
runuser -u "$U" -- mkdir -p "$UHOME/.local/share/applications" "$UHOME/.config/autostart"
runuser -u "$U" -- install -m 644 "$SRC/org.buskill.Light.desktop" "$UHOME/.local/share/applications/org.buskill.Light.desktop"
runuser -u "$U" -- install -m 644 "$SRC/org.buskill.Light-autostart.desktop" "$UHOME/.config/autostart/org.buskill.Light.desktop"

sleep 2
echo
systemctl --no-pager --lines=8 status buskill-light.service || true
echo
/usr/local/bin/buskill-light check || true
echo
echo "Done. Open 'BusKill Light' from the menu (it also starts automatically on login)."
