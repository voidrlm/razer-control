"""Command-line interface for Razer Control. Run `razer-control-cli -h` for usage."""

import argparse
import sys

from .buttons import LOCKED_SLOTS, parse_action, slot_name
from .protocol import EFFECT_LABELS, DeviceAsleep, RazerError, device_db, open_devices


def hex_rgb(s: str):
    s = s.lstrip("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError("colors look like #ff0040")
    return tuple(int(s[i : i + 2], 16) for i in (0, 2, 4))


def main(argv=None):
    p = argparse.ArgumentParser(prog="razer-control-cli", description="Control Razer devices from the terminal")
    p.add_argument("-d", "--device", default=None,
                   help="device to control: index from 'devices', a USB product id (e.g. 007b), or a type "
                        "(mouse, keyboard, keypad, mousemat, accessory). Default: first device")
    p.add_argument("--dock", action="store_true", help="shortcut for --device accessory")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="list connected devices")
    s = sub.add_parser("supported", help="list every model in the device database")
    s.add_argument("filter", nargs="?", default="")
    sub.add_parser("status", help="show settings of connected devices")
    s = sub.add_parser("dpi"); s.add_argument("value", type=int)
    s = sub.add_parser("stages"); s.add_argument("active", type=int); s.add_argument("values", help="comma list e.g. 400,800,1600")
    s = sub.add_parser("poll"); s.add_argument("hz", type=int)
    s = sub.add_parser("light", help="set a lighting effect")
    s.add_argument("effect", choices=list(EFFECT_LABELS))
    s.add_argument("color1", nargs="?", default="#00ff00", type=hex_rgb)
    s.add_argument("color2", nargs="?", default="#0000ff", type=hex_rgb)
    s.add_argument("--zone", help="logo, scroll, matrix, left, right, backlight, … (default: all zones)")
    s.add_argument("--speed", type=int, default=2)
    s.add_argument("--direction", type=int, default=None)
    s = sub.add_parser("brightness"); s.add_argument("percent", type=int); s.add_argument("--zone")
    s = sub.add_parser("idle"); s.add_argument("seconds", type=int)
    sub.add_parser("buttons", help="list button mappings")
    s = sub.add_parser("button", help="remap a button",
                       description="ACTION: disabled | dpi-cycle | clutch:400 | mouse:back | mouse:forward | key:ctrl+c | key:f13 | key:volume up | default")
    s.add_argument("slot", help="slot id, e.g. 0x06 or 6 (see 'buttons')")
    s.add_argument("action")
    a = p.parse_args(argv)

    if a.cmd == "supported":
        db = device_db()
        rows = [(e["type"], e["name"], pid, e.get("protocol") == "razer") for pid, e in db.items()]
        rows = [r for r in rows if a.filter.lower() in f"{r[0]} {r[1]} {r[2]:04x}".lower()]
        for typ, name, pid, ok in sorted(rows):
            print(f"1532:{pid:04x}  {typ:<10} {name}{'' if ok else '   (not supported: headset protocol)'}")
        print(f"\n{sum(r[3] for r in rows)} supported, {sum(not r[3] for r in rows)} recognised but unsupported")
        return 0

    devices, errors = open_devices()
    for e in errors:
        if e.startswith("permission:"):
            print(f"No permission for {e.split(':')[1]} — install the udev rule (see README).", file=sys.stderr)
        elif e.startswith("unsupported:"):
            _, name, why = e.split(":", 2)
            print(f"Skipping {name}: {why}", file=sys.stderr)
    if not devices:
        print("No controllable Razer device found.", file=sys.stderr)
        return 1

    if a.cmd == "devices":
        for i, d in enumerate(devices):
            tag = "verified" if d.model.verified else ("known" if d.model.known else "unknown model")
            print(f"[{i}] 1532:{d.pid:04x}  {d.model.kind:<10} {d.name}  ({d.node.path}, iface {d.node.interface}, {tag})")
        return 0

    want = "accessory" if a.dock else a.device
    dev = devices[0]
    if want is not None:
        if want.isdigit() and int(want) < len(devices):
            dev = devices[int(want)]
        else:
            match = [d for d in devices if d.model.kind == want or f"{d.pid:04x}" == want.lower().removeprefix("0x")]
            if not match:
                print(f"No connected device matches {want!r} (see 'devices').", file=sys.stderr)
                return 1
            dev = match[0]

    def show(label, fn):
        try:
            print(f"  {label:<22}{fn()}")
        except RazerError as e:
            print(f"  {label:<22}({e})")

    try:
        if a.cmd == "status":
            for d in devices if want is None else [dev]:
                f = d.model.features
                print(f"== {d.name}  [1532:{d.pid:04x} {d.node.path} iface {d.node.interface} tid {d.tid:#04x}]")
                show("Firmware", d.firmware)
                show("Serial", d.serial)
                if f.get("battery"):
                    show("Battery", lambda: f"{d.battery()}%")
                if f.get("charging"):
                    show("Charging", d.charging)
                if f.get("idle"):
                    show("Idle sleep (s)", d.idle_time)
                if f.get("low_battery"):
                    show("Low battery alert", lambda: f"{d.low_battery_threshold()}%")
                if f.get("dpi") or f.get("dpi_byte"):
                    show("DPI", d.dpi)
                if f.get("dpi_stages"):
                    show("DPI stages", d.dpi_stages)
                if d.model.polling_rates:
                    show("Polling rate", lambda: f"{d.polling_rate()} Hz")
                try:
                    for z in d.probe_zones():
                        b = f", brightness {z.get_brightness()}%" if z.has_brightness else ""
                        print(f"  {'Light: ' + z.key:<22}{z.family} led {z.led:#04x}{b}; effects: {', '.join(z.effects)}")
                except RazerError as e:
                    print(f"  {'Lighting':<22}({e})")
        elif a.cmd == "dpi":
            dev.set_dpi(a.value)
        elif a.cmd == "stages":
            vals = [int(v) for v in a.values.split(",")]
            dev.set_dpi_stages(a.active, [(v, v) for v in vals])
        elif a.cmd == "poll":
            if a.hz not in dev.model.polling_rates:
                print(f"Supported rates: {', '.join(map(str, dev.model.polling_rates)) or 'none'}", file=sys.stderr)
                return 1
            dev.set_polling_rate(a.hz)
        elif a.cmd == "light":
            zones = [dev.zone(a.zone)] if a.zone else dev.probe_zones()
            if not zones:
                raise RazerError("This device has no controllable lighting")
            done = 0
            for z in zones:
                if a.effect in z.effects:
                    z.apply(a.effect, a.color1, a.color2, a.speed, a.direction)
                    done += 1
                elif a.zone:
                    raise RazerError(f"{z.key} supports: {', '.join(z.effects)}")
            if not done:
                raise RazerError(f"No zone supports {a.effect}")
        elif a.cmd == "brightness":
            zones = [dev.zone(a.zone)] if a.zone else [z for z in dev.probe_zones() if z.has_brightness]
            for z in zones:
                z.set_brightness(a.percent)
        elif a.cmd == "idle":
            dev.set_idle_time(a.seconds)
        elif a.cmd == "buttons":
            for slot in dev.button_slots():
                try:
                    act = dev.get_button(slot)
                except RazerError as e:
                    print(f"{slot:#04x}  {slot_name(slot, dev.pid):<28}({e})")
                    continue
                flag = "" if act.is_default else "  (customized)"
                print(f"{slot:#04x}  {slot_name(slot, dev.pid):<28}{act.describe()}{flag}")
        elif a.cmd == "button":
            slot = int(a.slot, 0)
            if slot in LOCKED_SLOTS:
                print("Left click can't be remapped.", file=sys.stderr)
                return 1
            if a.action.lower() == "default":
                dev.reset_button(slot)
            else:
                try:
                    dev.set_button(slot, parse_action(a.action))
                except ValueError as e:
                    print(e, file=sys.stderr)
                    return 1
            print(f"{slot:#04x}  {slot_name(slot, dev.pid)}: {dev.get_button(slot).describe()}")
    except DeviceAsleep as e:
        print(e, file=sys.stderr)
        return 2
    except RazerError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
