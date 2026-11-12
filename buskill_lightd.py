#!/usr/bin/python3 -I
"""
BusKill Light daemon.

Runs as root under systemd. While armed, unplugging the selected USB device
powers the machine off (or locks the screen).

Removal is detected in two independent ways: kernel uevents over netlink and
polling sysfs every 0.25 s. Every `interval_seconds` a self-test verifies that
the device is present, that kernel events actually reach our socket, and that
systemd/logind respond. Problems are reported as desktop notifications.
"""

import configparser
import errno
import json
import os
import pwd
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time
import traceback
import uuid

VERSION = "1.0.0"

CONFIG_PATH = os.environ.get("BUSKILL_LIGHT_CONFIG", "/etc/buskill-light/config.ini")
RUN_DIR = os.environ.get("BUSKILL_LIGHT_RUN_DIR", "/run/buskill-light")
SOCK_PATH = os.path.join(RUN_DIR, "control.sock")
STATE_PATH = os.path.join(RUN_DIR, "armed.json")
SYSFS = os.environ.get("BUSKILL_LIGHT_SYSFS", "/sys")
DRY_RUN = os.environ.get("BUSKILL_LIGHT_DRY_RUN") == "1"
NO_NOTIFY = os.environ.get("BUSKILL_LIGHT_NO_NOTIFY") == "1"

NETLINK_KOBJECT_UEVENT = 15
SO_RCVBUFFORCE = 33
POLL_INTERVAL = 0.25
PING_TIMEOUT = 3.0
FIRE_RETRY = 10.0
NOTIFY_ICON = "buskill"

ACTIONS = {
    "soft-shutdown": "powered off",
    "lock-screen": "locked",
}

CONFIG_TEMPLATE = """\
# BusKill Light configuration
# Apply manual changes with: sudo systemctl restart buskill-light

[device]
# USB device whose removal fires the trigger (vendor:product as shown by lsusb)
vendor_id = {vid}
product_id = {pid}
# empty = any serial number
serial = {serial}
# only used to pick one if several identical devices are plugged in
port = {port}
description = {desc}

[trigger]
# soft-shutdown or lock-screen
action = {action}

[check]
# seconds between self-tests
interval_seconds = {interval}
# after resume from suspend, how long to wait for the device to come back
resume_grace_seconds = {grace}

[access]
# users allowed to arm/disarm; they also receive notifications
users = {users}
"""


def log(prio, msg):
    # <N> prefix = syslog priority, understood by journald
    for line in str(msg).splitlines() or [""]:
        print("<%d>%s" % (prio, line), flush=True)


def sd_notify(msg):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError as e:
        log(4, "sd_notify failed: %s" % e)


def read_attr(path, name):
    try:
        with open(os.path.join(path, name), "rb") as f:
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return None


def suspend_offset():
    # BOOTTIME keeps counting during suspend, MONOTONIC does not
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()


def run_cmd(cmd, timeout=4):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timed out after %ds" % timeout
    except OSError as e:
        return -1, str(e)


def disk_labels():
    """Map of real sysfs path of a block device -> filesystem label."""
    out = {}
    base = "/dev/disk/by-label"
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        try:
            dev = os.path.basename(os.path.realpath(os.path.join(base, name)))
            sysp = os.path.realpath(os.path.join("/sys/class/block", dev))
            label = re.sub(rb"\\x([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]),
                           name.encode()).decode("utf-8", "replace")
            out[sysp] = label
        except Exception:
            pass
    return out


def usb_device_at(devpath, labels=None):
    path = SYSFS + devpath
    vid = read_attr(path, "idVendor")
    if vid is None:
        return None
    info = {
        "devpath": devpath,
        "port": os.path.basename(devpath),
        "vid": vid.lower(),
        "pid": (read_attr(path, "idProduct") or "").lower(),
        "serial": read_attr(path, "serial") or "",
        "manufacturer": read_attr(path, "manufacturer") or "",
        "product": read_attr(path, "product") or "",
        "busnum": read_attr(path, "busnum") or "",
        "devnum": read_attr(path, "devnum") or "",
        "is_hub": read_attr(path, "bDeviceClass") == "09",
        "removable": read_attr(path, "removable") or "",
        "labels": [],
    }
    if labels is not None:
        prefix = "/sys" + devpath + "/"
        info["labels"] = sorted({lab for p, lab in labels.items() if p.startswith(prefix)})
    return info


def list_usb_devices():
    base = os.path.join(SYSFS, "bus/usb/devices")
    labels = disk_labels()
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        if ":" in name or name.startswith("usb"):
            continue  # interfaces and root hubs
        real = os.path.realpath(os.path.join(base, name))
        if not real.startswith(SYSFS + "/"):
            continue
        info = usb_device_at(real[len(SYSFS):], labels)
        if info:
            out.append(info)
    return out


def describe(dev):
    name = " ".join(x for x in (dev.get("manufacturer"), dev.get("product")) if x) or "USB device"
    if dev.get("labels"):
        name += " [" + ", ".join(dev["labels"]) + "]"
    return "%s (%s:%s, port %s)" % (name, dev.get("vid"), dev.get("pid"), dev.get("port"))


class Config:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.vid = self.pid = self.serial = self.port = self.desc = ""
        self.action = "soft-shutdown"
        self.interval = 60
        self.grace = 10
        self.users = []
        self.error = None
        self.load()

    def load(self):
        cp = configparser.ConfigParser(interpolation=None)
        try:
            if not cp.read(self.path, encoding="utf-8"):
                self.error = "config file %s not found" % self.path
                return
            self.vid = cp.get("device", "vendor_id", fallback="").strip().lower()
            self.pid = cp.get("device", "product_id", fallback="").strip().lower()
            self.serial = cp.get("device", "serial", fallback="").strip()
            self.port = cp.get("device", "port", fallback="").strip()
            self.desc = cp.get("device", "description", fallback="").strip()
            self.action = cp.get("trigger", "action", fallback="soft-shutdown").strip()
            if self.action not in ACTIONS:
                self.action = "soft-shutdown"
            self.interval = max(10, cp.getint("check", "interval_seconds", fallback=60))
            self.grace = max(0, cp.getint("check", "resume_grace_seconds", fallback=10))
            self.users = [u for u in cp.get("access", "users", fallback="").replace(",", " ").split() if u]
            self.error = None if (self.vid and self.pid) else "no BusKill device selected"
        except Exception as e:
            self.error = "config error: %s" % e

    def render(self):
        return CONFIG_TEMPLATE.format(
            vid=self.vid, pid=self.pid, serial=self.serial, port=self.port, desc=self.desc,
            action=self.action, interval=self.interval, grace=self.grace, users=" ".join(self.users))

    def save(self, **changes):
        for key, value in changes.items():
            setattr(self, key, value)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(self.render())
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)
        self.load()

    def find_device(self):
        if self.error:
            return None, self.error
        devs = [d for d in list_usb_devices()
                if d["vid"] == self.vid and d["pid"] == self.pid
                and (not self.serial or d["serial"] == self.serial)]
        if len(devs) > 1 and self.port:
            same_port = [d for d in devs if d["port"] == self.port]
            if same_port:
                devs = same_port
        if not devs:
            return None, "BusKill device (%s:%s) not found - plug in the cable" % (self.vid, self.pid)
        if len(devs) > 1:
            return None, "several %s:%s devices are plugged in - select the right one in the app" % (
                self.vid, self.pid)
        return devs[0], None

    def uids(self):
        out = []
        for u in self.users:
            try:
                out.append(pwd.getpwnam(u).pw_uid)
            except KeyError:
                pass
        return out


class Daemon:
    def __init__(self):
        self.cfg = Config()
        self.armed = None
        self.nl = None
        self.ctl = None
        self.children = []
        self.notify_ids = {}         # (uid, key) -> notification id, so we can replace it
        self.health = {"ok": None, "checked_at": None, "checks": [], "problems": []}
        self.last_check = -1e9
        self.last_wd = 0.0
        self.wd_usec = int(os.environ.get("WATCHDOG_USEC", "0") or 0)
        self.pending_ping = None
        self.ping_seen = False
        self.last_offset = suspend_offset()
        self.resumed_at = -1e9
        self.grace_pending = False
        self.last_fire = -1e9
        self.fire_count = 0
        self.fired = None
        self.last_test = None
        self.events_seen = 0
        self.overflows = 0
        self.started_at = time.time()

    # sockets

    def open_netlink(self):
        if self.nl is not None:
            try:
                self.nl.close()
            except OSError:
                pass
            self.nl = None
        s = socket.socket(socket.AF_NETLINK,
                          socket.SOCK_RAW | socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK,
                          NETLINK_KOBJECT_UEVENT)
        try:
            s.setsockopt(socket.SOL_SOCKET, SO_RCVBUFFORCE, 16 * 1024 * 1024)
        except OSError:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        s.bind((0, 1))  # group 1 = raw kernel events, no dependency on udevd
        self.nl = s
        log(6, "kernel uevent socket opened")

    def open_control(self):
        os.makedirs(RUN_DIR, exist_ok=True)
        try:
            os.unlink(SOCK_PATH)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX,
                          socket.SOCK_STREAM | socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK)
        s.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o666)  # access is checked per connection via SO_PEERCRED
        s.listen(16)
        self.ctl = s

    # kernel events

    def drain_netlink(self):
        while True:
            try:
                data, addr = self.nl.recvfrom(65536)
            except BlockingIOError:
                return
            except OSError as e:
                if e.errno == errno.ENOBUFS:
                    # events were dropped, check the device directly
                    self.overflows += 1
                    log(4, "uevent buffer overflow - checking device directly")
                    self.poll_device()
                    continue
                log(3, "netlink socket error (%s) - reopening" % e)
                self.open_netlink()
                self.poll_device()
                return
            if addr and addr[0] != 0:
                continue  # not from the kernel
            self.handle_uevent(data)

    def handle_uevent(self, data):
        parts = data.split(b"\0")
        if not parts or b"@" not in parts[0]:
            return
        env = {}
        for p in parts[1:]:
            k, sep, v = p.partition(b"=")
            if sep:
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        self.events_seen += 1
        if self.pending_ping and env.get("SYNTH_UUID") == self.pending_ping:
            self.ping_seen = True
        if env.get("ACTION") == "remove" and self.armed:
            devpath = env.get("DEVPATH", "")
            mine = self.armed["devpath"]
            if devpath and (devpath == mine or mine.startswith(devpath + "/")):
                self.device_gone("kernel event: %s removed" % devpath)

    def wait(self, timeout, control=True):
        socks = [self.nl] + ([self.ctl] if control else [])
        try:
            ready, _, _ = select.select(socks, [], [], max(0.0, timeout))
        except InterruptedError:
            return
        self.check_resume()
        self.watchdog()
        if self.nl in ready:
            self.drain_netlink()
        if control and self.ctl in ready:
            self.handle_control()

    # suspend / resume

    def check_resume(self):
        off = suspend_offset()
        if off - self.last_offset > 1.5:
            log(5, "resumed from suspend (slept %.0f s)" % (off - self.last_offset))
            self.resumed_at = time.monotonic()
        self.last_offset = off

    def in_grace(self):
        return time.monotonic() - self.resumed_at < self.cfg.grace

    # removal detection

    def device_matches(self, cur, strict=True):
        a = self.armed
        if not cur:
            return False
        same = cur["vid"] == a["vid"] and cur["pid"] == a["pid"] and cur["serial"] == a["serial"]
        if strict:
            same = same and cur["devnum"] == a["devnum"] and cur["busnum"] == a["busnum"]
        return same

    def poll_device(self):
        if not self.armed:
            return
        cur = usb_device_at(self.armed["devpath"])
        if self.device_matches(cur):
            return
        if self.in_grace():
            if not self.grace_pending:
                log(5, "device not visible right after resume - waiting %d s" % self.cfg.grace)
            self.grace_pending = True
            return
        if self.grace_pending:
            self.grace_pending = False
            if self.device_matches(cur, strict=False):
                # same device on the same port, just re-enumerated after resume
                self.armed["devnum"] = cur["devnum"]
                self.armed["busnum"] = cur["busnum"]
                self.save_state()
                log(5, "device is back after resume - still armed")
                return
        if cur is None:
            self.device_gone("device disappeared (sysfs poll)")
        else:
            self.device_gone("device was unplugged and plugged back in (new device number)")

    def device_gone(self, reason):
        if self.in_grace():
            self.grace_pending = True
            log(5, "%s - just resumed, waiting to see if it comes back" % reason)
            return
        self.fire(reason)

    # trigger

    def fire(self, reason):
        if not self.armed:
            return
        now = time.monotonic()
        if now - self.last_fire < FIRE_RETRY:
            return
        self.last_fire = now
        self.fire_count += 1
        mode = self.armed["mode"]
        action = self.armed.get("action", self.cfg.action)
        log(2, "BUSKILL TRIGGERED (%s, %s): %s" % (mode, action, reason))

        if mode == "test":
            self.last_test = {"at": time.time(), "reason": reason, "action": action}
            uid = self.armed.get("uid")
            self.disarm("test finished")
            self.notify("TEST PASSED - BusKill Light fired",
                        "Removal detected: %s.\nIn normal mode the computer would have been %s.\n"
                        "Plug the cable back in and click Start." % (reason, ACTIONS[action]),
                        urgency="critical", key="test", extra_uid=uid)
            return

        self.fired = {"at": time.time(), "reason": reason, "action": action}
        ok = self.do_lock() if action == "lock-screen" else self.do_shutdown()
        if action == "lock-screen" and ok:
            self.disarm("screen locked")  # one-shot, otherwise it would keep re-locking
        if not ok:
            self.notify("BusKill Light: FAILED TO " +
                        ("LOCK THE SCREEN" if action == "lock-screen" else "POWER OFF"),
                        "See: journalctl -u buskill-light. Retrying.",
                        urgency="critical", key="fire")

    def do_shutdown(self):
        if DRY_RUN:
            log(2, "[DRY RUN] would power off now")
            return True
        # lock right away in case the shutdown takes a while
        self.spawn(["loginctl", "lock-sessions"])
        attempts = [
            ["systemctl", "poweroff", "--check-inhibitors=no"],
            ["systemctl", "start", "poweroff.target", "--job-mode=replace-irreversibly", "--no-block"],
            ["systemctl", "poweroff", "--force"],
        ]
        # still running FIRE_RETRY seconds after a "successful" attempt -> escalate
        for cmd in attempts[min(self.fire_count - 1, len(attempts)):]:
            rc, out = run_cmd(cmd, timeout=15)
            log(2 if rc == 0 else 3, "%s -> %s %s" % (" ".join(cmd), rc, out.strip()))
            if rc == 0:
                return True
        try:
            os.sync()
            with open("/proc/sysrq-trigger", "w") as f:
                f.write("o")
            return True
        except OSError as e:
            log(2, "sysrq poweroff failed: %s" % e)
        return False

    def do_lock(self):
        if DRY_RUN:
            log(2, "[DRY RUN] would lock the screen now")
            return True
        rc, out = run_cmd(["loginctl", "lock-sessions"], timeout=10)
        log(2 if rc == 0 else 3, "loginctl lock-sessions -> %s %s" % (rc, out.strip()))
        return rc == 0

    # arming

    def arm(self, mode, uid):
        self.cfg.load()
        if self.armed:
            if self.armed["mode"] == mode:
                return None
            self.disarm("mode change")
        dev, err = self.cfg.find_device()
        if not dev:
            return err
        self.armed = {
            "mode": mode, "uid": uid, "since": time.time(), "action": self.cfg.action,
            "devpath": dev["devpath"], "port": dev["port"], "vid": dev["vid"], "pid": dev["pid"],
            "serial": dev["serial"], "busnum": dev["busnum"], "devnum": dev["devnum"],
            "desc": describe(dev),
        }
        self.fired = None
        self.fire_count = 0
        self.grace_pending = False
        self.save_state()
        log(5, "ARMED (%s): %s, action: %s" % (mode, describe(dev), self.cfg.action))
        self.health_check(notify_ok=False)
        return None

    def disarm(self, reason="requested"):
        if self.armed:
            log(5, "DISARMED (%s)" % reason)
        self.armed = None
        self.grace_pending = False
        try:
            os.unlink(STATE_PATH)
        except FileNotFoundError:
            pass
        self.close_notification("health")

    def save_state(self):
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.armed, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_PATH)

    def restore_state(self):
        try:
            with open(STATE_PATH) as f:
                st = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log(3, "corrupted state file (%s) - staying disarmed" % e)
            return
        if isinstance(st, dict) and st.get("devpath"):
            self.armed = st
            log(4, "daemon restarted - restoring ARMED state (%s)" % st.get("desc"))
            # if the device went away while we were down, fire now
            self.poll_device()

    # self-test

    def ping(self, devpath):
        """Send a synthetic 'change' uevent to the device and wait for the kernel to echo it."""
        token = str(uuid.uuid4())
        try:
            with open(SYSFS + devpath + "/uevent", "w") as f:
                f.write("change %s" % token)
        except OSError as e:
            return False, "cannot send test event (%s)" % e.strerror
        self.pending_ping, self.ping_seen = token, False
        deadline = time.monotonic() + PING_TIMEOUT
        try:
            while not self.ping_seen:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.wait(min(left, POLL_INTERVAL), control=False)
                self.poll_device()
        finally:
            self.pending_ping = None
        if self.ping_seen:
            return True, ""
        return False, "test event did not come back within %.0f s" % PING_TIMEOUT

    def health_check(self, notify_ok=True):
        self.last_check = time.monotonic()
        self.cfg.load()
        checks = []
        notes = []

        def add(name, ok, text=""):
            checks.append({"name": name, "ok": ok, "text": text})

        if os.geteuid() == 0:
            add("privileges", True, "running as root")
        elif DRY_RUN:
            add("privileges", True, "DRY RUN mode")
        else:
            add("privileges", False, "not running as root - cannot power off")

        dev = None
        if self.armed:
            cur = usb_device_at(self.armed["devpath"])
            if self.device_matches(cur):
                dev = cur
                add("device", True, "connected: " + self.armed["desc"])
            elif self.grace_pending or self.in_grace():
                add("device", True, "temporarily missing after resume - waiting")
            else:
                add("device", False, "BusKill device not connected")
        else:
            dev, err = self.cfg.find_device()
            add("device", dev is not None, ("connected: " + describe(dev)) if dev else err)

        if dev is not None:
            ok, err = self.ping(dev["devpath"])
            if not ok:
                log(4, "uevent self-test failed (%s) - reopening socket" % err)
                try:
                    self.open_netlink()
                    ok, err = self.ping(dev["devpath"])
                except OSError as e:
                    ok, err = False, str(e)
                if ok:
                    notes.append("USB event listener stopped responding - fixed automatically")
            if ok:
                add("USB events", True, "kernel delivers events (self-test OK)")
            else:
                add("USB events", False, err + " - only the 0.25 s fallback poll is working")
        else:
            add("USB events", None, "cannot test without the device connected")

        add("0.25 s poll", True, "running")

        rc, out = run_cmd(["systemctl", "show", "-P", "LoadState", "poweroff.target"])
        _, state = run_cmd(["systemctl", "is-system-running"])
        state = state.strip()
        if rc != 0 or out.strip() != "loaded":
            add("power-off (systemd)", False,
                "systemd not responding or poweroff.target unavailable (%s)" % out.strip())
        elif state in ("stopping", "offline"):
            add("power-off (systemd)", False, "system state is '%s'" % state)
        else:
            add("power-off (systemd)", True, "systemd responds (%s)" % state)

        rc, out = run_cmd(["loginctl", "list-sessions", "--no-legend"])
        need_lock = self.cfg.action == "lock-screen"
        if rc == 0:
            add("screen lock (logind)", True, "logind responds")
        else:
            add("screen lock (logind)", False if need_lock else None,
                "logind not responding (%s)" % out.strip()[:120])

        problems = [c["name"] + ": " + c["text"] for c in checks if c["ok"] is False]
        if self.cfg.error and not any(c["name"] == "device" and c["ok"] is False for c in checks):
            problems.append(self.cfg.error)
        was_ok = self.health.get("ok")
        self.health = {"ok": not problems, "checked_at": time.time(), "checks": checks,
                       "problems": problems, "notes": notes}

        if problems:
            log(3, "SELF-TEST: PROBLEM - " + "; ".join(problems))
        else:
            log(7, "self-test OK")
        for n in notes:
            log(4, n)

        if self.armed:
            if problems:
                self.notify("BusKill Light might NOT fire!",
                            "\n".join("• " + p for p in problems) +
                            "\n\nWhat to do: unplug and replug the BusKill cable, then click Stop and Start. "
                            "If that does not help: sudo systemctl restart buskill-light",
                            urgency="critical", key="health")
            elif was_ok is False and notify_ok:
                self.notify("BusKill Light is working again", "Self-test OK.",
                            urgency="normal", key="health")
            elif notes:
                self.notify("BusKill Light: problem fixed", "\n".join(notes),
                            urgency="normal", key="health")

    # desktop notifications

    def notify_targets(self, extra_uid=None):
        uids = []
        if self.armed and self.armed.get("uid") is not None:
            uids.append(self.armed["uid"])
        if extra_uid is not None:
            uids.append(extra_uid)
        uids += self.cfg.uids()
        if not uids and os.geteuid() != 0:
            uids.append(os.geteuid())
        seen, out = set(), []
        for u in uids:
            if u not in seen and u != 0:
                seen.add(u)
                out.append(u)
        return out

    def user_env(self, uid):
        pw = pwd.getpwuid(uid)
        rt = "/run/user/%d" % uid
        env = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=%s/bus" % rt, "XDG_RUNTIME_DIR": rt,
               "HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name,
               "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": os.environ.get("LANG", "C.UTF-8")}
        return pw, env

    def notify(self, title, body, urgency="normal", key="main", extra_uid=None):
        if NO_NOTIFY:
            log(6, "[notification %s] %s | %s" % (urgency, title, body.replace("\n", " / ")))
            return
        for uid in self.notify_targets(extra_uid):
            if not os.path.exists("/run/user/%d/bus" % uid):
                continue
            try:
                pw, env = self.user_env(uid)
                cmd = ["notify-send", "--app-name=BusKill Light", "--urgency=" + urgency,
                       "--icon=" + NOTIFY_ICON, "--print-id"]
                old = self.notify_ids.get((uid, key))
                if old:
                    cmd.append("--replace-id=%s" % old)
                cmd += [title, body]
                self.spawn(cmd, uid=uid, pw=pw, env=env, tag=(uid, key))
            except Exception as e:
                log(4, "notification failed for uid %s: %s" % (uid, e))

    def close_notification(self, key):
        for (uid, k), nid in list(self.notify_ids.items()):
            if k != key:
                continue
            del self.notify_ids[(uid, k)]
            try:
                pw, env = self.user_env(uid)
                self.spawn(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                            "--object-path", "/org/freedesktop/Notifications", "--method",
                            "org.freedesktop.Notifications.CloseNotification", str(nid)],
                           uid=uid, pw=pw, env=env)
            except Exception:
                pass

    def spawn(self, cmd, uid=None, pw=None, env=None, tag=None):
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                  cwd="/", start_new_session=True, env=env)
        if uid is not None and os.geteuid() == 0:
            kw.update(user=uid, group=pw.pw_gid, extra_groups=[])
        try:
            p = subprocess.Popen(cmd, **kw)
            self.children.append((p, tag, time.monotonic()))
        except OSError as e:
            log(4, "cannot run %s: %s" % (cmd[0], e))

    def reap(self):
        alive = []
        for p, tag, t0 in self.children:
            if p.poll() is None:
                if time.monotonic() - t0 > 30:
                    p.kill()
                alive.append((p, tag, t0))
                continue
            try:
                out = p.stdout.read().decode().strip() if p.stdout else ""
                p.stdout.close()
            except Exception:
                out = ""
            if tag and p.returncode == 0 and out.isdigit():
                self.notify_ids[tag] = out
        self.children = alive

    # control socket

    def allowed(self, uid):
        if uid == 0 or uid == os.geteuid():
            return True
        return uid in self.cfg.uids()

    def handle_control(self):
        for _ in range(16):
            try:
                conn, _ = self.ctl.accept()
            except (BlockingIOError, InterruptedError):
                return
            with conn:
                try:
                    conn.setblocking(True)
                    conn.settimeout(1.0)
                    cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                    _, uid, _ = struct.unpack("3i", cred)
                    if not self.allowed(uid):
                        resp = {"ok": False, "error": "permission denied (add the user to [access] users)"}
                    else:
                        data = b""
                        while b"\n" not in data and len(data) < 65536:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            data += chunk
                        resp = self.dispatch(json.loads(data.decode() or "{}"), uid)
                    conn.sendall((json.dumps(resp) + "\n").encode())
                except Exception as e:
                    log(4, "control request failed: %s" % e)
                    try:
                        conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
                    except OSError:
                        pass

    def status(self):
        dev, err = (None, None)
        if not self.armed:
            dev, err = self.cfg.find_device()
        return {
            "ok": True,
            "version": VERSION,
            "armed": bool(self.armed),
            "mode": self.armed["mode"] if self.armed else None,
            "armed_since": self.armed["since"] if self.armed else None,
            "device": self.armed["desc"] if self.armed else (describe(dev) if dev else None),
            "device_error": None if (self.armed or dev) else err,
            "configured": {"vid": self.cfg.vid, "pid": self.cfg.pid, "serial": self.cfg.serial,
                           "port": self.cfg.port, "description": self.cfg.desc},
            "action": self.cfg.action,
            "interval": self.cfg.interval,
            "health": self.health,
            "last_test": self.last_test,
            "fired": self.fired,
            "dry_run": DRY_RUN,
            "events_seen": self.events_seen,
            "overflows": self.overflows,
            "uptime": time.time() - self.started_at,
        }

    def dispatch(self, req, uid):
        cmd = req.get("cmd")
        if cmd == "status":
            return self.status()
        if cmd in ("arm", "test"):
            err = self.arm("test" if cmd == "test" else "real", uid)
            st = self.status()
            if err:
                st.update(ok=False, error=err)
            return st
        if cmd == "disarm":
            self.disarm("requested by uid %d" % uid)
            return self.status()
        if cmd == "check":
            self.health_check()
            return self.status()
        if cmd == "devices":
            return {"ok": True, "devices": list_usb_devices()}
        if cmd == "set-device":
            if self.armed:
                return {"ok": False, "error": "click Stop first"}
            d = req.get("device") or {}
            for k in ("vid", "pid"):
                if not str(d.get(k, "")).strip():
                    return {"ok": False, "error": "missing %s" % k}
            self.cfg.save(vid=d["vid"], pid=d["pid"], serial=d.get("serial", ""),
                          port=d.get("port", ""), desc=describe(d))
            log(5, "device selected: %s" % describe(d))
            self.health_check()
            return self.status()
        if cmd == "set-action":
            if self.armed:
                return {"ok": False, "error": "click Stop first"}
            a = req.get("action")
            if a not in ACTIONS:
                return {"ok": False, "error": "unknown action"}
            self.cfg.save(action=a)
            log(5, "action on removal: %s" % a)
            return self.status()
        return {"ok": False, "error": "unknown command: %r" % cmd}

    def watchdog(self):
        if not self.wd_usec:
            return
        now = time.monotonic()
        if now - self.last_wd >= min(1.0, self.wd_usec / 4e6):
            sd_notify("WATCHDOG=1")
            self.last_wd = now

    def run(self):
        self.open_netlink()
        self.open_control()
        self.restore_state()
        sd_notify("READY=1\nSTATUS=listening")
        log(6, "BusKill Light %s ready (config: %s%s)" % (VERSION, CONFIG_PATH, ", DRY RUN" if DRY_RUN else ""))
        while True:
            try:
                self.wait(POLL_INTERVAL)
                self.poll_device()
                if time.monotonic() - self.last_check >= self.cfg.interval:
                    self.health_check()
                self.reap()
                self.watchdog()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                log(3, "main loop error:\n" + traceback.format_exc())
                try:
                    self.poll_device()
                except Exception:
                    pass
                time.sleep(0.1)


def write_default_config(users, path=CONFIG_PATH):
    cfg = Config(path)
    devs = list_usb_devices()
    pick = [d for d in devs if any(l.upper() == "BUSKILL" for l in d["labels"])]
    dev = pick[0] if len(pick) == 1 else None
    if dev:
        cfg.save(vid=dev["vid"], pid=dev["pid"], serial=dev["serial"], port=dev["port"],
                 desc=describe(dev), users=users)
    else:
        cfg.save(users=users)
    print("Wrote %s; device: %s" % (path, describe(dev) if dev else "NOT DETECTED - select it in the app"))


def refresh_config(path=CONFIG_PATH):
    """Rewrite an existing config from the current template, keeping its values."""
    cfg = Config(path)
    cfg.save()
    print("Updated %s (settings kept)" % path)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--write-default-config":
        write_default_config(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--refresh-config":
        refresh_config()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--list-devices":
        for d in list_usb_devices():
            print(describe(d), "serial=%r" % d["serial"])
        return

    d = Daemon()

    def on_term(signum, frame):
        log(5, "stopping (signal %d)%s" % (signum, " - armed state kept" if d.armed else ""))
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, lambda s, f: d.cfg.load())
    try:
        d.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
