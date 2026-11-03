"""Button-mapping data model for Razer mice (command 0x02:0x0C set / 0x02:0x8C get).

Wire format of the 10 argument bytes:
    [profile, slot, default_flag, fn_class, fn_len, fn_data(5)]
  profile       0x00 = live layer, 0x01 = onboard profile 1 (both are written)
  default_flag  reads back 0x01 for untouched factory buttons; a write with 0x01
                only sets the flag and does NOT change the action, so custom
                actions are written with 0x00
Verified on the Viper Ultimate by write/read-back. Protocol notes credit:
OpenSnek docs/protocol/USB_PROTOCOL.md.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---- function classes ----------------------------------------------------
FN_DISABLED = 0x00
FN_MOUSE = 0x01
FN_KEYBOARD = 0x02
FN_MACRO = 0x03
FN_DPI = 0x06

DPI_CYCLE = 0x06
DPI_CLUTCH = 0x05

# Generic slot names (ids are shared across Razer mice; see OpenSnek protocol notes)
SLOT_NAMES = {
    0x01: "Left click",
    0x02: "Right click",
    0x03: "Wheel click",
    0x09: "Wheel up",
    0x0A: "Wheel down",
    0x34: "Wheel tilt left",
    0x35: "Wheel tilt right",
    0x04: "Side button — back",
    0x05: "Side button — forward",
    0x06: "Extra button 6",
    0x07: "Extra button 7",
    0x0B: "Extra button 11",
    0x0C: "Extra button 12",
    0x0E: "Bottom button",
    0x0F: "DPI clutch button",
    0x39: "Extra button 57",
    0x60: "DPI button",
    0x6A: "Profile / wheel-mode button",
    0x6D: "Extra button 109",
    0x80: "Rear button",
}
for _i in range(12):
    SLOT_NAMES[0x40 + _i] = f"Side grid key {_i + 1}"
for _i in range(6):
    SLOT_NAMES[0x50 + _i] = f"Side ring key {_i + 1}"

# Per-device label overrides (pid -> {slot: name})
SLOT_OVERRIDES = {
    pid: {
        0x04: "Left side — rear", 0x05: "Left side — front",
        0x06: "Right side — rear", 0x07: "Right side — front", 0x60: "DPI button (underside)",
    }
    for pid in (0x0078, 0x007A, 0x007B)  # Viper / Viper Ultimate (ambidextrous)
}
SLOT_ORDER = [0x01, 0x02, 0x03, 0x09, 0x0A, 0x34, 0x35, 0x04, 0x05, 0x06, 0x07, *range(0x40, 0x4C), *range(0x50, 0x56),
              0x0B, 0x0C, 0x0E, 0x0F, 0x39, 0x60, 0x6A, 0x6D, 0x80]
MAIN_SLOTS = {0x01, 0x02, 0x03, 0x09, 0x0A, 0x34, 0x35}
SIDE_SLOTS = {0x04, 0x05, 0x06, 0x07, *range(0x40, 0x4C), *range(0x50, 0x56)}
LOCKED_SLOTS = {0x01}  # never remap primary click: you'd lose the ability to click


def slot_name(slot: int, pid: int = 0) -> str:
    return SLOT_OVERRIDES.get(pid, {}).get(slot) or SLOT_NAMES.get(slot) or f"Button {slot:#04x}"


MOUSE_BUTTONS = [
    (0x01, "Left click"),
    (0x02, "Right click"),
    (0x03, "Middle click"),
    (0x04, "Back"),
    (0x05, "Forward"),
    (0x09, "Scroll up"),
    (0x0A, "Scroll down"),
]
MOUSE_NAMES = dict(MOUSE_BUTTONS)

MOD_CTRL, MOD_SHIFT, MOD_ALT, MOD_SUPER = 0x01, 0x02, 0x04, 0x08
MODIFIERS = [(MOD_CTRL, "Ctrl"), (MOD_SHIFT, "Shift"), (MOD_ALT, "Alt"), (MOD_SUPER, "Super")]


def _build_keys():
    keys = []  # (hid, label, evdev)
    letters_ev = dict(zip("qwertyuiop", range(16, 26))) | dict(zip("asdfghjkl", range(30, 39))) | dict(zip("zxcvbnm", range(44, 51)))
    for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
        keys.append((0x04 + i, ch.upper(), letters_ev[ch]))
    for i, d in enumerate("1234567890"):
        keys.append((0x1E + i, d, 2 + i))
    keys += [
        (0x28, "Enter", 28), (0x29, "Escape", 1), (0x2A, "Backspace", 14), (0x2B, "Tab", 15), (0x2C, "Space", 57),
        (0x2D, "-", 12), (0x2E, "=", 13), (0x2F, "[", 26), (0x30, "]", 27), (0x31, "\\", 43), (0x33, ";", 39),
        (0x34, "'", 40), (0x35, "`", 41), (0x36, ",", 51), (0x37, ".", 52), (0x38, "/", 53), (0x39, "Caps Lock", 58),
    ]
    for i in range(12):
        keys.append((0x3A + i, f"F{i + 1}", (59 + i) if i < 10 else (87 + i - 10)))
    keys += [
        (0x46, "Print Screen", 99), (0x47, "Scroll Lock", 70), (0x48, "Pause", 119), (0x49, "Insert", 110),
        (0x4A, "Home", 102), (0x4B, "Page Up", 104), (0x4C, "Delete", 111), (0x4D, "End", 107),
        (0x4E, "Page Down", 109), (0x4F, "Right", 106), (0x50, "Left", 105), (0x51, "Down", 108), (0x52, "Up", 103),
        (0x53, "Num Lock", 69), (0x54, "Keypad /", 98), (0x55, "Keypad *", 55), (0x56, "Keypad -", 74),
        (0x57, "Keypad +", 78), (0x58, "Keypad Enter", 96),
    ]
    for i in range(9):
        keys.append((0x59 + i, f"Keypad {i + 1}", [79, 80, 81, 75, 76, 77, 71, 72, 73][i]))
    keys += [(0x62, "Keypad 0", 82), (0x63, "Keypad .", 83), (0x65, "Menu", 127)]
    for i in range(12):
        keys.append((0x68 + i, f"F{13 + i}", 183 + i))
    keys += [(0x7F, "Mute", 113), (0x80, "Volume up", 115), (0x81, "Volume down", 114)]
    return keys


KEYS = _build_keys()
KEY_NAMES = {hid: label for hid, label, _ in KEYS}
EVDEV_TO_HID = {ev: hid for hid, _label, ev in KEYS}
EVDEV_MODIFIERS = {29: MOD_CTRL, 97: MOD_CTRL, 42: MOD_SHIFT, 54: MOD_SHIFT, 56: MOD_ALT, 100: MOD_ALT, 125: MOD_SUPER, 126: MOD_SUPER}


@dataclass
class Action:
    fn_class: int
    data: bytes = b""
    is_default: bool = False

    # ---- constructors ----
    @classmethod
    def disabled(cls):
        return cls(FN_DISABLED, b"")

    @classmethod
    def mouse(cls, button: int):
        return cls(FN_MOUSE, bytes([button]))

    @classmethod
    def key(cls, hid: int, mods: int = 0):
        return cls(FN_KEYBOARD, bytes([mods & 0xFF, hid]))

    @classmethod
    def dpi_cycle(cls):
        return cls(FN_DPI, bytes([DPI_CYCLE]))

    @classmethod
    def dpi_clutch(cls, dpi: int):
        dpi = max(100, min(20000, int(dpi)))
        return cls(FN_DPI, bytes([DPI_CLUTCH, dpi >> 8, dpi & 0xFF, dpi >> 8, dpi & 0xFF]))

    # ---- inspection ----
    @property
    def kind(self) -> str:
        if self.fn_class == FN_DISABLED:
            return "disabled"
        if self.fn_class == FN_MOUSE and len(self.data) == 1:
            return "mouse"
        if self.fn_class == FN_KEYBOARD and len(self.data) >= 1:
            return "key"
        if self.fn_class == FN_DPI and self.data[:1] == bytes([DPI_CYCLE]):
            return "dpi_cycle"
        if self.fn_class == FN_DPI and self.data[:1] == bytes([DPI_CLUTCH]) and len(self.data) >= 3:
            return "dpi_clutch"
        return "custom"

    @property
    def key_hid(self) -> int:
        return self.data[-1] if self.data else 0

    @property
    def key_mods(self) -> int:
        return self.data[0] if len(self.data) >= 2 else 0

    @property
    def clutch_dpi(self) -> int:
        return (self.data[1] << 8) | self.data[2]

    def describe(self) -> str:
        k = self.kind
        if k == "disabled":
            return "Disabled"
        if k == "mouse":
            return MOUSE_NAMES.get(self.data[0], f"Mouse button {self.data[0]:#04x}")
        if k == "key":
            return "Key: " + shortcut_label(self.key_hid, self.key_mods)
        if k == "dpi_cycle":
            return "Cycle DPI stages"
        if k == "dpi_clutch":
            return f"DPI clutch — {self.clutch_dpi} DPI while held"
        if self.fn_class == FN_MACRO:
            return "Macro (set in Synapse)"
        return f"Custom function {self.fn_class:#04x}"

    def block(self) -> list[int]:
        return [self.fn_class, len(self.data), *self.data]


def shortcut_label(hid: int, mods: int) -> str:
    parts = [name for bit, name in MODIFIERS if mods & (bit | bit << 4)]
    parts.append(KEY_NAMES.get(hid, f"key {hid:#04x}"))
    return "+".join(parts)


def default_action(slot: int, pid: int = 0) -> Action | None:
    if slot in (0x01, 0x02, 0x03, 0x04, 0x05, 0x09, 0x0A):
        return Action.mouse(slot)
    if pid in (0x0078, 0x007A, 0x007B) and slot in (0x06, 0x07):
        return Action.mouse(0x04 if slot == 0x06 else 0x05)
    if slot == 0x60:
        return Action.dpi_cycle()
    return None


def parse_action(spec: str) -> Action:
    """CLI syntax: disabled | dpi-cycle | clutch:400 | mouse:back | key:ctrl+shift+t"""
    spec = spec.strip().lower()
    if spec in ("disabled", "off", "none"):
        return Action.disabled()
    if spec in ("dpi-cycle", "dpi"):
        return Action.dpi_cycle()
    kind, _, arg = spec.partition(":")
    if kind == "clutch":
        return Action.dpi_clutch(int(arg or 400))
    if kind == "mouse":
        for code, name in MOUSE_BUTTONS:
            if arg in (name.lower(), name.lower().split()[0], str(code)):
                return Action.mouse(code)
        raise ValueError(f"unknown mouse button {arg!r}")
    if kind == "key":
        *mods, key = arg.split("+")
        m = 0
        for mod in mods:
            m |= {"ctrl": MOD_CTRL, "control": MOD_CTRL, "shift": MOD_SHIFT, "alt": MOD_ALT, "super": MOD_SUPER, "win": MOD_SUPER, "meta": MOD_SUPER}[mod]
        lookup = {label.lower(): hid for hid, label, _ in KEYS}
        if key not in lookup:
            raise ValueError(f"unknown key {key!r}")
        return Action.key(lookup[key], m)
    raise ValueError(f"cannot parse action {spec!r}")
