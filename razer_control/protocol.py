"""Userspace driver for Razer USB devices over Linux hidraw.

Speaks Razer's 90-byte feature-report protocol directly (the protocol openrazer
uses in its kernel modules), so no kernel driver is needed. Per-device details
come from devices.json (generated from openrazer by tools/gen_devices.py) and
are confirmed by probing the hardware at runtime.
"""

from __future__ import annotations

import errno
import fcntl
import glob
import json
import os
import threading
import time
from dataclasses import dataclass, field

RAZER_VID = 0x1532

STATUS_NEW, STATUS_BUSY, STATUS_OK, STATUS_FAIL, STATUS_TIMEOUT, STATUS_UNSUPPORTED = range(6)
STATUS_NAMES = {
    STATUS_NEW: "new",
    STATUS_BUSY: "busy",
    STATUS_OK: "ok",
    STATUS_FAIL: "failure",
    STATUS_TIMEOUT: "no response (device asleep?)",
    STATUS_UNSUPPORTED: "not supported",
}

VARSTORE, NOSTORE = 0x01, 0x00
ZERO_LED, SCROLL_WHEEL_LED, LOGO_LED, BACKLIGHT_LED = 0x00, 0x01, 0x04, 0x05
RIGHT_SIDE_LED, LEFT_SIDE_LED = 0x10, 0x11
CHARGING_LED, FAST_CHARGING_LED, FULLY_CHARGED_LED = 0x20, 0x21, 0x22

REPORT_LEN = 90
TID_CANDIDATES = (0x1F, 0x3F, 0xFF, 0x9F)

ZONE_INFO = {  # key: (label, led id)
    "logo": ("Logo", LOGO_LED),
    "scroll": ("Scroll wheel", SCROLL_WHEEL_LED),
    "backlight": ("Backlight", BACKLIGHT_LED),
    "left": ("Left side", LEFT_SIDE_LED),
    "right": ("Right side", RIGHT_SIDE_LED),
    "charging": ("While charging", CHARGING_LED),
    "fast_charging": ("While fast charging", FAST_CHARGING_LED),
    "fully_charged": ("When fully charged", FULLY_CHARGED_LED),
    "matrix": ("All lighting", BACKLIGHT_LED),
}

# Our effect keys, labels, and what each command family can do
EFFECT_LABELS = {
    "static": "Static",
    "breathing": "Breathing",
    "breathing_dual": "Breathing (two colors)",
    "breathing_random": "Breathing (random)",
    "spectrum": "Spectrum cycle",
    "wave": "Wave",
    "wheel": "Wheel",
    "reactive": "Reactive (on press)",
    "starlight": "Starlight",
    "starlight_dual": "Starlight (two colors)",
    "starlight_random": "Starlight (random)",
    "blinking": "Blinking",
    "on": "On",
    "off": "Off",
}
EFFECT_ORDER = list(EFFECT_LABELS)
DB_EFFECT_MAP = {
    "none": "off", "static": "static", "spectrum": "spectrum", "reactive": "reactive", "wave": "wave", "wheel": "wheel",
    "breath_single": "breathing", "breath_dual": "breathing_dual", "breath_random": "breathing_random",
    "starlight_single": "starlight", "starlight_dual": "starlight_dual", "starlight_random": "starlight_random",
    "blinking": "blinking", "pulsate": "breathing", "on": "on", "active": "on",
}
FAMILY_EFFECTS = {
    "extended": {"off", "static", "breathing", "breathing_dual", "breathing_random", "spectrum", "reactive", "wave",
                 "wheel", "starlight", "starlight_dual", "starlight_random"},
    "mouse_extended": {"off", "static", "breathing", "breathing_dual", "breathing_random", "spectrum", "reactive"},
    "standard_led": {"off", "on", "static", "breathing", "spectrum", "blinking"},
    "standard_matrix": {"off", "static", "breathing", "breathing_dual", "breathing_random", "spectrum", "reactive",
                        "wave", "starlight", "starlight_dual", "starlight_random"},
}
EFFECTS_NEED = {  # which parameters the UI should show
    "color1": {"static", "breathing", "breathing_dual", "reactive", "starlight", "starlight_dual", "blinking"},
    "color2": {"breathing_dual", "starlight_dual"},
    "speed": {"reactive", "starlight", "starlight_dual", "starlight_random"},
    "direction": {"wave", "wheel"},
}

# Back-compat for the old EFFECTS list (CLI choices)
EFFECTS = [(k, EFFECT_LABELS[k]) for k in EFFECT_ORDER]


def _ioc(direction: int, typ: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (typ << 8) | nr


def HIDIOCSFEATURE(size: int) -> int:  # noqa: N802
    return _ioc(3, ord("H"), 0x06, size)


def HIDIOCGFEATURE(size: int) -> int:  # noqa: N802
    return _ioc(3, ord("H"), 0x07, size)


class RazerError(Exception):
    pass


class DeviceAsleep(RazerError):
    pass


# ---------------------------------------------------------------- device database

_DB = None


def device_db() -> dict:
    global _DB
    if _DB is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.json")
        try:
            with open(path) as f:
                _DB = {int(k, 16): v for k, v in json.load(f)["devices"].items()}
        except (OSError, ValueError, KeyError):
            _DB = {}
    return _DB


# Overrides verified on real hardware (take precedence over the generated db)
VERIFIED = {
    0x007A: {"led_overrides": {"logo": LOGO_LED}},
    0x007B: {"led_overrides": {"logo": LOGO_LED}},
    0x007E: {"led_overrides": {"matrix": BACKLIGHT_LED}, "zone_labels": {"matrix": "Dock lighting"}},
}


@dataclass
class Model:
    pid: int
    name: str
    kind: str  # mouse | keyboard | keypad | mousemat | accessory | headset
    known: bool = True
    supported: bool = True
    wireless: bool = False
    max_dpi: int = 0
    min_dpi: int = 100
    features: dict = field(default_factory=dict)
    polling_rates: tuple = ()
    zones: dict = field(default_factory=dict)  # key -> {"effects": [...], "brightness": bool}
    lighting: dict = field(default_factory=dict)  # zone/matrix family, matrix_led, brightness family
    wave_dirs: tuple = (1, 2)
    tid: int | None = None
    iface: int = 0
    wait: float = 0.035

    @property
    def has_battery(self) -> bool:
        return bool(self.features.get("battery"))

    @property
    def has_lighting(self) -> bool:
        return bool(self.zones)

    @property
    def verified(self) -> bool:
        return self.pid in VERIFIED


def model_for(pid: int, hid_name: str = "") -> Model:
    e = device_db().get(pid)
    if e is None:
        low = hid_name.lower()
        kind = "keyboard" if any(w in low for w in ("keyboard", "blackwidow", "huntsman", "ornata", "cynosa", "deathstalker", "blade")) else (
            "mouse" if any(w in low for w in ("mouse", "dongle", "receiver", "viper", "deathadder", "basilisk", "naga")) else "accessory")
        return Model(pid, hid_name.replace("Razer Razer", "Razer") or f"Razer device {pid:04x}", kind, known=False,
                     wireless=any(w in low for w in ("wireless", "dongle", "receiver")))
    feats = dict(e.get("features", {}))
    if feats.get("poll2"):
        rates = tuple(sorted(e.get("poll_rates") or (125, 500, 1000, 2000, 4000, 8000)))
    elif feats.get("poll"):
        rates = tuple(sorted(e.get("poll_rates") or (125, 500, 1000)))
    else:
        rates = ()
    name = e["name"]
    m = Model(
        pid=pid,
        name=name,
        kind=e["type"],
        supported=e.get("protocol") == "razer",
        wireless=feats.get("battery", False) or any(w in name for w in ("Wireless", "Receiver", "HyperSpeed", "Dongle")),
        max_dpi=e.get("max_dpi") or (0 if not (feats.get("dpi") or feats.get("dpi_byte")) else 16000),
        features=feats,
        polling_rates=rates,
        zones=e.get("zones", {}),
        lighting=e.get("lighting", {}),
        wave_dirs=tuple(e.get("wave_dirs") or (1, 2)),
        tid=e.get("tid"),
        iface=e.get("iface", 0),
        wait=max(0.002, min(0.45, e.get("wait_us", 600) / 1e6 + 0.001)),
    )
    if feats.get("dpi_byte"):
        m.max_dpi = min(m.max_dpi or 6750, 6750)
    return m


# ---------------------------------------------------------------- hidraw discovery


@dataclass
class HidrawNode:
    path: str
    pid: int
    name: str
    interface: int
    usb_path: str


def find_nodes() -> list[HidrawNode]:
    nodes = []
    for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(sys_path, "device", "uevent")) as f:
                info = dict(line.strip().split("=", 1) for line in f if "=" in line)
        except OSError:
            continue
        try:
            bus, vid, pid = (int(x, 16) for x in info.get("HID_ID", "").split(":"))
        except ValueError:
            continue
        if vid != RAZER_VID or bus != 0x03:  # USB only (Bluetooth uses a different transport)
            continue
        phys = info.get("HID_PHYS", "")
        usb_path, _, inp = phys.rpartition("/input")
        try:
            iface = int(inp)
        except ValueError:
            iface = -1
        nodes.append(HidrawNode("/dev/" + os.path.basename(sys_path), pid, info.get("HID_NAME", ""), iface, usb_path or phys))
    return nodes


# ---------------------------------------------------------------- reports


@dataclass
class Report:
    command_class: int
    command_id: int
    data_size: int
    args: bytearray = field(default_factory=lambda: bytearray(80))
    transaction_id: int = 0xFF
    status: int = STATUS_NEW

    def pack(self) -> bytes:
        buf = bytearray(REPORT_LEN)
        buf[0] = self.status
        buf[1] = self.transaction_id
        buf[5] = self.data_size
        buf[6] = self.command_class
        buf[7] = self.command_id
        buf[8:88] = bytes(self.args[:80]).ljust(80, b"\0")
        crc = 0
        for b in buf[2:88]:
            crc ^= b
        buf[88] = crc
        return bytes(buf)

    @classmethod
    def unpack(cls, buf: bytes) -> "Report":
        return cls(buf[6], buf[7], buf[5], bytearray(buf[8:88]), buf[1], buf[0])


def _transact(fd: int, report: Report, wait: float) -> Report:
    out = bytearray(b"\0" + report.pack())
    fcntl.ioctl(fd, HIDIOCSFEATURE(len(out)), out)
    deadline = time.monotonic() + wait + 0.6
    time.sleep(wait)
    while True:
        inp = bytearray(REPORT_LEN + 1)
        fcntl.ioctl(fd, HIDIOCGFEATURE(len(inp)), inp)
        resp = Report.unpack(bytes(inp[1:]))
        if resp.status != STATUS_BUSY or time.monotonic() > deadline:
            return resp
        time.sleep(0.015)


# ---------------------------------------------------------------- lighting zones


def _rgb(c):
    return [int(c[0]) & 0xFF, int(c[1]) & 0xFF, int(c[2]) & 0xFF]


class Zone:
    def __init__(self, dev: "RazerDevice", key: str, family: str, led: int, effects: list[str], brightness: str | None):
        self.dev, self.key, self.family, self.led = dev, key, family, led
        self.effects = effects
        self.brightness_family = brightness  # extended | standard | blade | dock | None
        label = ZONE_INFO.get(key, (key.title(), led))[0]
        if key == "matrix":
            label = {"keyboard": "Keyboard", "keypad": "Keypad", "mousemat": "Mouse mat", "mouse": "All zones"}.get(dev.model.kind, "Lighting")
        self.label = VERIFIED.get(dev.pid, {}).get("zone_labels", {}).get(key, label)

    @property
    def has_brightness(self) -> bool:
        return self.brightness_family is not None

    # ---- brightness
    def get_brightness(self) -> int:
        f = self.brightness_family
        if f == "extended":
            raw = self.dev.cmd(0x0F, 0x84, 0x03, [VARSTORE, self.led, 0])[2]
        elif f == "standard":
            raw = self.dev.cmd(0x03, 0x83, 0x03, [VARSTORE, self.led, 0])[2]
        elif f == "blade":
            raw = self.dev.cmd(0x0E, 0x84, 0x02, [0x01, 0])[1]
        elif f == "dock":
            raw = self.dev.cmd(0x07, 0x82, 0x01)[0]
        else:
            raise RazerError("Brightness not supported")
        return round(raw * 100 / 255)

    def set_brightness(self, percent: int):
        v = max(0, min(255, round(int(percent) * 255 / 100)))
        f = self.brightness_family
        if f == "extended":
            self.dev.cmd(0x0F, 0x04, 0x03, [VARSTORE, self.led, v])
        elif f == "standard":
            self.dev.cmd(0x03, 0x03, 0x03, [VARSTORE, self.led, v])
        elif f == "blade":
            self.dev.cmd(0x0E, 0x04, 0x02, [0x01, v])
        elif f == "dock":
            self.dev.cmd(0x07, 0x02, 0x01, [v])
        else:
            raise RazerError("Brightness not supported")

    # ---- effects
    def apply(self, effect: str, c1=(0, 255, 0), c2=(0, 0, 255), speed: int = 2, direction: int | None = None):
        if effect not in self.effects:
            raise RazerError(f"{EFFECT_LABELS.get(effect, effect)} isn't supported by {self.label.lower()}")
        if direction is None:
            direction = self.dev.model.wave_dirs[0]
        getattr(self, "_fx_" + self.family)(effect, _rgb(c1), _rgb(c2), int(speed), int(direction))

    def _fx_extended(self, e, c1, c2, speed, direction):
        led, cmd = self.led, (lambda size, args: self.dev.cmd(0x0F, 0x02, size, [VARSTORE, self.led, *args]))
        if e == "off":
            cmd(0x06, [0x00])
        elif e == "static":
            cmd(0x09, [0x01, 0x00, 0x00, 0x01, *c1])
        elif e == "breathing":
            cmd(0x09, [0x02, 0x01, 0x00, 0x01, *c1])
        elif e == "breathing_dual":
            cmd(0x0C, [0x02, 0x02, 0x00, 0x02, *c1, *c2])
        elif e == "breathing_random":
            cmd(0x06, [0x02])
        elif e == "spectrum":
            cmd(0x06, [0x03])
        elif e == "reactive":
            cmd(0x09, [0x05, 0x00, max(1, min(4, speed)), 0x01, *c1])
        elif e == "wave":
            cmd(0x06, [0x04, max(0, min(2, direction)), 0x28])
        elif e == "wheel":
            cmd(0x06, [0x0A, max(1, min(2, direction)), 0x28])
        elif e == "starlight":
            cmd(0x09, [0x07, 0x00, max(1, min(3, speed)), 0x01, *c1])
        elif e == "starlight_dual":
            cmd(0x0C, [0x07, 0x00, max(1, min(3, speed)), 0x02, *c1, *c2])
        elif e == "starlight_random":
            cmd(0x06, [0x07, 0x00, max(1, min(3, speed))])
        del led

    def _fx_mouse_extended(self, e, c1, c2, speed, direction):
        cmd = lambda size, args: self.dev.cmd(0x03, 0x0D, size, [VARSTORE, self.led, *args])  # noqa: E731
        if e == "off":
            cmd(0x03, [0x00])
        elif e == "static":
            cmd(0x06, [0x06, *c1])
        elif e == "breathing":
            cmd(0x0A, [0x03, 0x01, *c1])
        elif e == "breathing_dual":
            cmd(0x0A, [0x03, 0x02, *c1, *c2])
        elif e == "breathing_random":
            cmd(0x0A, [0x03, 0x03])
        elif e == "spectrum":
            cmd(0x03, [0x04])
        elif e == "reactive":
            cmd(0x07, [0x02, max(1, min(4, speed)), *c1])

    def _fx_standard_led(self, e, c1, c2, speed, direction):
        d, led = self.dev, self.led
        if e == "off":
            d.cmd(0x03, 0x00, 0x03, [VARSTORE, led, 0x00])
            return
        d.cmd(0x03, 0x00, 0x03, [VARSTORE, led, 0x01])
        if e == "on":
            return
        if e in ("static", "breathing", "blinking"):
            d.cmd(0x03, 0x01, 0x05, [VARSTORE, led, *c1])
        effect_id = {"static": 0x00, "blinking": 0x01, "breathing": 0x02, "spectrum": 0x04}[e]
        d.cmd(0x03, 0x02, 0x03, [VARSTORE, led, effect_id])

    def _fx_standard_matrix(self, e, c1, c2, speed, direction):
        cmd = lambda size, args: self.dev.cmd(0x03, 0x0A, size, args)  # noqa: E731
        if e == "off":
            cmd(0x01, [0x00])
        elif e == "wave":
            cmd(0x02, [0x01, max(1, min(2, direction))])
        elif e == "reactive":
            cmd(0x05, [0x02, max(1, min(4, speed)), *c1])
        elif e == "breathing":
            cmd(0x08, [0x03, 0x01, *c1])
        elif e == "breathing_dual":
            cmd(0x08, [0x03, 0x02, *c1, *c2])
        elif e == "breathing_random":
            cmd(0x08, [0x03, 0x03])
        elif e == "spectrum":
            cmd(0x01, [0x04])
        elif e == "static":
            cmd(0x04, [0x06, *c1])
        elif e == "starlight":
            cmd(0x01, [0x19, 0x01, max(1, min(3, speed)), *c1, 0, 0, 0])
        elif e == "starlight_dual":
            cmd(0x01, [0x19, 0x02, max(1, min(3, speed)), *c1, *c2])
        elif e == "starlight_random":
            cmd(0x01, [0x19, 0x03, max(1, min(3, speed))])


# ---------------------------------------------------------------- device


class RazerDevice:
    """One Razer USB device, controlled through the hidraw node of its control interface."""

    def __init__(self, nodes: list[HidrawNode]):
        self.nodes = sorted(nodes, key=lambda n: n.interface)
        first = self.nodes[0]
        self.pid = first.pid
        self.usb_path = first.usb_path
        self.model = model_for(first.pid, first.name)
        self.lock = threading.Lock()
        self.fd: int | None = None
        self.node: HidrawNode | None = None
        self.tid: int | None = None
        self.asleep = False
        self.zones: list[Zone] | None = None
        self.led = None  # kept for CLI compatibility

    @property
    def name(self) -> str:
        return self.model.name

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    # ---- connection / probing ------------------------------------------

    def connect(self):
        """Find the interface and transaction id this device answers on."""
        ifaces = sorted(self.nodes, key=lambda n: (n.interface != self.model.iface, n.interface))
        tids = [t for t in ([self.model.tid] if self.model.tid else []) + list(TID_CANDIDATES)]
        tids = list(dict.fromkeys(tids))
        perm_error = None
        for node in ifaces:
            try:
                fd = os.open(node.path, os.O_RDWR)
            except PermissionError as e:
                perm_error = e
                continue
            except OSError:
                continue
            ok = False
            for tid in tids:
                try:
                    r = _transact(fd, Report(0x00, 0x81, 0x02, transaction_id=tid), self.model.wait)
                except OSError:
                    break  # this interface doesn't take feature reports
                if r.status == STATUS_OK and r.command_class == 0x00 and r.command_id == 0x81:
                    self.tid, ok = tid, True
                    break
                if r.status == STATUS_TIMEOUT and node.interface == self.model.iface:
                    self.tid, ok, self.asleep = tid, True, True
                    break
            if ok:
                self.fd, self.node = fd, node
                return
            os.close(fd)
        if perm_error:
            raise perm_error
        raise RazerError("Device did not respond to the Razer protocol on any interface")

    # ---- low level -------------------------------------------------------

    def cmd(self, command_class: int, command_id: int, data_size: int, args=b"") -> bytearray:
        with self.lock:
            if self.fd is None:
                raise RazerError("Device not connected")
            a = bytearray(80)
            a[: len(args)] = bytes(args)
            try:
                resp = _transact(self.fd, Report(command_class, command_id, data_size, a, transaction_id=self.tid), self.model.wait)
            except OSError as e:
                if e.errno in (errno.EPIPE, errno.EPROTO):
                    raise RazerError(f"Command {command_class:02x}:{command_id:02x} rejected by device") from e
                raise
            if resp.status == STATUS_TIMEOUT:
                self.asleep = True
                raise DeviceAsleep("Device is not responding — move or press it to wake it up")
            self.asleep = False
            if resp.status != STATUS_OK:
                raise RazerError(
                    f"Command {command_class:02x}:{command_id:02x} failed: {STATUS_NAMES.get(resp.status, hex(resp.status))}"
                )
            if resp.command_class != command_class or resp.command_id != command_id:
                raise RazerError(f"Mismatched response for {command_class:02x}:{command_id:02x}")
            return resp.args

    # ---- info ------------------------------------------------------------

    def firmware(self) -> str:
        a = self.cmd(0x00, 0x81, 0x02)
        return f"v{a[0]}.{a[1]}"

    def serial(self) -> str:
        a = self.cmd(0x00, 0x82, 0x16)
        return bytes(a[:22]).split(b"\0", 1)[0].decode("ascii", "replace").strip()

    # ---- battery / power -------------------------------------------------

    def battery(self) -> int:
        a = self.cmd(0x07, 0x80, 0x02)
        return round(a[1] * 100 / 255)

    def charging(self) -> bool:
        return bool(self.cmd(0x07, 0x84, 0x02)[1])

    def idle_time(self) -> int:
        a = self.cmd(0x07, 0x83, 0x02)
        return (a[0] << 8) | a[1]

    def set_idle_time(self, seconds: int):
        seconds = max(60, min(900, int(seconds)))
        self.cmd(0x07, 0x03, 0x02, [seconds >> 8, seconds & 0xFF])

    def low_battery_threshold(self) -> int:
        return round(self.cmd(0x07, 0x81, 0x01)[0] * 100 / 255)

    def set_low_battery_threshold(self, percent: int):
        raw = max(0x0C, min(0x4D, round(int(percent) * 255 / 100)))  # ~5%..30%
        self.cmd(0x07, 0x01, 0x01, [raw])

    # ---- performance -----------------------------------------------------

    def _clamp_dpi(self, v: int) -> int:
        return max(self.model.min_dpi, min(self.model.max_dpi or 20000, int(v)))

    def dpi(self) -> tuple[int, int]:
        if self.model.features.get("dpi_byte"):
            a = self.cmd(0x04, 0x81, 0x03)
            return round(a[0] / 255 * 6750), round(a[1] / 255 * 6750)
        a = self.cmd(0x04, 0x85, 0x07, [NOSTORE])
        return (a[1] << 8) | a[2], (a[3] << 8) | a[4]

    def set_dpi(self, x: int, y: int | None = None):
        y = x if y is None else y
        x, y = self._clamp_dpi(x), self._clamp_dpi(y)
        if self.model.features.get("dpi_byte"):
            self.cmd(0x04, 0x01, 0x03, [round(x / 6750 * 255), round(y / 6750 * 255), 0])
            return
        self.cmd(0x04, 0x05, 0x07, [VARSTORE, x >> 8, x & 0xFF, y >> 8, y & 0xFF, 0, 0])

    def dpi_stages(self) -> tuple[int, list[tuple[int, int]]]:
        a = self.cmd(0x04, 0x86, 0x26, [VARSTORE])
        active, count = a[1], a[2]
        stages = []
        for i in range(min(count, 5)):
            o = 3 + i * 7
            stages.append(((a[o + 1] << 8) | a[o + 2], (a[o + 3] << 8) | a[o + 4]))
        return active, stages

    def set_dpi_stages(self, active: int, stages: list[tuple[int, int]]):
        stages = stages[:5]
        if not stages:
            raise ValueError("need at least one DPI stage")
        active = max(1, min(len(stages), int(active)))
        args = [VARSTORE, active, len(stages)]
        for i, (x, y) in enumerate(stages):
            x, y = self._clamp_dpi(x), self._clamp_dpi(y)
            args += [i, x >> 8, x & 0xFF, y >> 8, y & 0xFF, 0, 0]
        self.cmd(0x04, 0x06, 0x26, args)

    POLL_V2 = {8000: 0x01, 4000: 0x02, 2000: 0x04, 1000: 0x08, 500: 0x10, 250: 0x20, 125: 0x40}

    def polling_rate(self) -> int:
        if self.model.features.get("poll2"):
            a = self.cmd(0x00, 0xC0, 0x01)
            inv = {v: k for k, v in self.POLL_V2.items()}
            return inv.get(a[1], inv.get(a[0], 0))
        a = self.cmd(0x00, 0x85, 0x01)
        return {0x01: 1000, 0x02: 500, 0x08: 125}.get(a[0], 0)

    def set_polling_rate(self, hz: int):
        hz = int(hz)
        if self.model.features.get("poll2"):
            code = self.POLL_V2[hz]
            self.cmd(0x00, 0x40, 0x02, [0x00, code])
            if self.model.features.get("poll2_double"):
                self.cmd(0x00, 0x40, 0x02, [0x01, code])
            return
        self.cmd(0x00, 0x05, 0x01, [{1000: 0x01, 500: 0x02, 125: 0x08}[hz]])

    # ---- lighting --------------------------------------------------------

    def probe_zones(self) -> list[Zone]:
        """Work out which lighting zones exist and how to drive them. Does device I/O."""
        if self.zones is not None:
            return self.zones
        lt = self.model.lighting
        zones = []
        db_zones = dict(self.model.zones)
        if not self.model.known:  # unknown device: look for extended-protocol LEDs
            db_zones = {}
            for key in ("logo", "scroll", "backlight", "left", "right"):
                try:
                    self.cmd(0x0F, 0x84, 0x03, [VARSTORE, ZONE_INFO[key][1], 0])
                    db_zones[key] = {"effects": ["none", "static", "spectrum", "breath_single", "breath_random"], "brightness": True}
                except DeviceAsleep:
                    raise
                except RazerError:
                    pass
            lt = {"zone": "extended", "brightness": "extended"}
        overrides = VERIFIED.get(self.pid, {}).get("led_overrides", {})
        for key, z in db_zones.items():
            family = lt.get("matrix" if key == "matrix" else "zone") or lt.get("zone") or lt.get("matrix")
            if family not in FAMILY_EFFECTS:
                continue
            effects = [DB_EFFECT_MAP[e] for e in z.get("effects", []) if e in DB_EFFECT_MAP]
            effects = [e for e in EFFECT_ORDER if e in effects and e in FAMILY_EFFECTS[family]]
            if not effects:
                continue
            if key == "matrix":
                candidates = [lt.get("matrix_led", BACKLIGHT_LED), BACKLIGHT_LED, ZERO_LED]
            else:
                candidates = [ZONE_INFO[key][1]]
            if key in overrides:
                candidates = [overrides[key]]
            candidates = list(dict.fromkeys(c for c in candidates if c is not None))
            bfam = lt.get("brightness") if z.get("brightness") else None
            if family == "standard_matrix" and bfam not in ("blade", "standard"):
                bfam = "standard" if z.get("brightness") else None
            led = candidates[0]
            if bfam in ("extended", "standard") and len(candidates) > 1:
                for c in candidates:  # confirm the LED id by reading its brightness
                    try:
                        Zone(self, key, family, c, effects, bfam).get_brightness()
                        led = c
                        break
                    except DeviceAsleep:
                        raise
                    except RazerError:
                        continue
            zones.append(Zone(self, key, family, led, effects, bfam))
        order = list(ZONE_INFO)
        zones.sort(key=lambda z: order.index(z.key) if z.key in order else 99)
        self.zones = zones
        if zones:
            self.led = zones[0].led
        return zones

    def zone(self, key: str | None = None) -> Zone:
        zones = self.probe_zones()
        if not zones:
            raise RazerError("This device has no controllable lighting")
        if key is None:
            return zones[0]
        for z in zones:
            if z.key == key:
                return z
        raise RazerError(f"No lighting zone {key!r} (have: {', '.join(z.key for z in zones)})")

    # simple wrappers (first zone) for the CLI / old callers
    def brightness(self) -> int:
        return self.zone().get_brightness()

    def set_brightness(self, percent: int):
        self.zone().set_brightness(percent)

    def apply_effect(self, effect: str, color1=(0, 255, 0), color2=(0, 0, 255), speed: int = 2, zone: str | None = None,
                     direction: int | None = None):
        self.zone(zone).apply(effect, color1, color2, speed, direction)

    # ---- button mapping ---------------------------------------------------

    def button_slots(self) -> list[int]:
        a = self.cmd(0x02, 0x84, 0x0A, [0x01])
        return [s for s in a[1 : 1 + min(a[0], 40)] if s]

    def get_button(self, slot: int):
        from .buttons import Action

        a = self.cmd(0x02, 0x8C, 0x0A, [0x01, slot, 0x00])
        if a[1] != slot:
            raise RazerError(f"Stale response reading button {slot:#04x}")
        n = min(a[4], 5)
        return Action(a[3], bytes(a[5 : 5 + n]), is_default=bool(a[2]))

    def set_button(self, slot: int, action):
        block = action.block()
        for profile in (0x01, 0x00):
            self.cmd(0x02, 0x0C, 0x0A, [profile, slot, 0x00, *block])

    def reset_button(self, slot: int):
        from .buttons import default_action

        act = default_action(slot, self.pid)
        if act is None:
            raise RazerError(f"No known default for button {slot:#04x}")
        self.set_button(slot, act)
        for profile in (0x01, 0x00):
            self.cmd(0x02, 0x0C, 0x0A, [profile, slot, 0x01, *act.block()])


@dataclass
class Unsupported:
    name: str
    pid: int
    reason: str


def open_devices() -> tuple[list[RazerDevice], list[str]]:
    """Find and connect every Razer USB device. Returns (devices, errors).

    Errors are strings; "permission:<path>:<name>" means the udev rule is missing,
    "unsupported:<name>:<reason>" means the device was recognised but can't be driven.
    """
    groups: dict[tuple, list[HidrawNode]] = {}
    for n in find_nodes():
        groups.setdefault((n.usb_path, n.pid), []).append(n)
    devices, errors = [], []
    for (_path, pid), nodes in groups.items():
        dev = RazerDevice(nodes)
        if not dev.model.supported:
            errors.append(f"unsupported:{dev.name}:uses a different (headset) protocol")
            continue
        try:
            dev.connect()
            devices.append(dev)
        except PermissionError:
            errors.append(f"permission:{nodes[0].path}:{dev.name}")
        except (RazerError, OSError) as e:
            errors.append(f"unsupported:{dev.name}:{e}")
    kind_order = ["mouse", "keyboard", "keypad", "mousemat", "accessory", "headset"]
    devices.sort(key=lambda d: (kind_order.index(d.model.kind) if d.model.kind in kind_order else 9, d.pid))
    return devices, errors
