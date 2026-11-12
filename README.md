# BusKill Light

A minimal, Linux-only USB dead man's switch for [BusKill](https://www.buskill.in/) cables.
It has one button, **START / STOP**. While armed, pulling the cable powers the computer off
(or locks the screen). A self-test runs every minute and you get a desktop notification
if BusKill would not fire.

## Why

The original BusKill app detects the removal and runs the shutdown from inside its own GUI
process, in the user session. Nothing checks whether that listener is still alive. In my
case it sometimes showed "armed" but did nothing when the cable was pulled.

BusKill Light moves all of that into a small root daemon. It does not depend on the desktop
session, the lock screen, screen blanking, or the window being open.

| | Original BusKill | BusKill Light |
|---|---|---|
| Runs in | GUI app, user session | systemd service (root) + small GTK window |
| Detection | libusb hotplug only | kernel uevents (instant) **and** sysfs polling every 0.25 s |
| Watches | any USB removal | the selected device, incl. a quick unplug/replug |
| Power off | `shutdown` as a normal user (can be blocked by polkit/inhibitors) | `systemctl poweroff` as root, ignoring inhibitors, with fallbacks |
| Health check | none | every 60 s: device present, kernel events reach the daemon (synthetic uevent round-trip), systemd/logind respond |

Other details:
- Closing the window does not disarm. The tray icon shows the current state.
- After a reboot BusKill is disarmed and waits for START, like the original.
- If the service crashes or hangs, systemd restarts it and it re-arms itself. If the cable
  was pulled in the meantime, it fires immediately.
- After resume from suspend there is a short grace period, so a device that re-enumerates
  during resume does not cause a false trigger.

## Requirements

Linux with systemd, Python 3.9+, GTK 3 via PyGObject, and `notify-send`.
The tray icon is optional: it needs AyatanaAppIndicator3, and on GNOME the AppIndicator extension.
Developed on Ubuntu with GNOME on Wayland.

## Install

```bash
git clone <this repo>
cd buskill-light
sudo bash install.sh
```

The installer selects the USB drive labelled `BUSKILL` automatically. For any other device,
use **Change…** in the window.

Uninstall with `sudo bash uninstall.sh`.

## Usage

- **START / STOP**: arm or disarm.
- **Test (no shutdown)**: arm in test mode. Pull the cable and you should get a
  "TEST PASSED" notification. Then plug it back in and click START.
- **Check now**: run the self-test immediately.

Command line:

```
buskill-light status | start | stop | test | check | devices
journalctl -u buskill-light -f
```

Configuration lives in `/etc/buskill-light/config.ini`.

Note: while armed, "Safely remove" on the BusKill drive counts as a removal and will fire.
