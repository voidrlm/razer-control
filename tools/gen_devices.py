#!/usr/bin/env python3
"""Generate razer_control/devices.json from an openrazer source checkout.

Usage: python3 -I tools/gen_devices.py /path/to/openrazer [out.json]

Reads the openrazer daemon hardware definitions (device names, PIDs, features)
with `ast` (never imports/executes them) and scans the kernel driver C sources
for per-device protocol details (lighting command family, LED id, transaction
id, USB interface, wait time). Only factual device data is extracted.
"""

import ast
import json
import os
import re
import sys
from collections import Counter, defaultdict

ZONES = ["logo", "scroll", "backlight", "left", "right", "charging", "fast_charging", "fully_charged"]
EFFECT_NAMES = [
    "none", "static", "spectrum", "reactive", "wave", "wheel", "breath_single", "breath_dual", "breath_random",
    "breath_mono", "breath_triple", "starlight_single", "starlight_dual", "starlight_random", "ripple", "blinking", "pulsate",
    "on", "active",
]
TYPE_BY_FILE = {
    "mouse.py": "mouse", "keyboards.py": "keyboard", "mouse_mat.py": "mousemat", "headsets.py": "headset",
    "accessory.py": "accessory", "core.py": "accessory", "monitor.py": "accessory",
}
LED_CONSTS = {
    "ZERO_LED": 0x00, "SCROLL_WHEEL_LED": 0x01, "BATTERY_LED": 0x03, "LOGO_LED": 0x04, "BACKLIGHT_LED": 0x05,
    "MACRO_LED": 0x07, "GAME_LED": 0x08, "RIGHT_SIDE_LED": 0x10, "LEFT_SIDE_LED": 0x11, "CHARGING_LED": 0x20,
    "FAST_CHARGING_LED": 0x21, "FULLY_CHARGED_LED": 0x22,
}


# ---------------------------------------------------------------- daemon (python AST)


class Evaluator:
    def __init__(self, classes):
        self.classes = classes  # name -> {"bases": [...], "attrs": {name: ast node}, ...}
        self.cache = {}

    def attr(self, cls, name, depth=0):
        key = (cls, name)
        if key in self.cache:
            return self.cache[key]
        if depth > 30 or cls not in self.classes:
            return None
        c = self.classes[cls]
        if name in c["attrs"]:
            try:
                val = self.eval(c["attrs"][name], cls, depth)
            except Exception:  # noqa: BLE001
                val = None
        else:
            val = None
            for b in c["bases"]:
                val = self.attr(b, name, depth + 1)
                if val is not None:
                    break
        self.cache[key] = val
        return val

    def eval(self, node, cls, depth):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            out = []
            for e in node.elts:
                if isinstance(e, ast.Starred):
                    out.extend(self.eval(e.value, cls, depth) or [])
                else:
                    out.append(self.eval(e, cls, depth))
            return out
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            a, b = self.eval(node.left, cls, depth), self.eval(node.right, cls, depth)
            if a is None or b is None:
                return a if b is None else b
            return list(a) + list(b)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return self.attr(node.value.id, node.attr, depth + 1)
        if isinstance(node, ast.Name):
            return self.attr(cls, node.id, depth + 1)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -self.eval(node.operand, cls, depth)
        if isinstance(node, ast.ListComp):
            # [m for m in X.METHODS if m not in (...)]  (best effort)
            gen = node.generators[0]
            src = self.eval(gen.iter, cls, depth) or []
            res = []
            for item in src:
                ok = True
                for cond in gen.ifs:
                    if isinstance(cond, ast.Compare) and len(cond.ops) == 1:
                        coll = self.eval(cond.comparators[0], cls, depth) or []
                        if isinstance(cond.ops[0], ast.NotIn):
                            ok &= item not in coll
                        elif isinstance(cond.ops[0], ast.In):
                            ok &= item in coll
                res.append(item) if ok else None
            return res
        return None


def parse_daemon(root):
    hw = os.path.join(root, "daemon", "openrazer_daemon", "hardware")
    classes, file_of = {}, {}
    for fn in sorted(os.listdir(hw)):
        if not fn.endswith(".py") or fn in ("__init__.py",):
            continue
        tree = ast.parse(open(os.path.join(hw, fn), encoding="utf-8").read())
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            attrs = {}
            for st in node.body:
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    attrs[st.targets[0].id] = st.value
            bases = [b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases]
            doc = ast.get_docstring(node) or ""
            classes[node.name] = {"bases": bases, "attrs": attrs, "doc": doc}
            file_of[node.name] = fn
    ev = Evaluator(classes)

    def inherits(cls, target, depth=0):
        if depth > 30 or cls not in classes:
            return False
        return any(b == target or inherits(b, target, depth + 1) for b in classes[cls]["bases"])

    devices = {}
    for name, c in classes.items():
        if "USB_PID" not in c["attrs"]:
            continue
        pid = ev.attr(name, "USB_PID")
        vid = ev.attr(name, "USB_VID") or 0x1532
        if not isinstance(pid, int) or vid != 0x1532:
            continue
        methods = ev.attr(name, "METHODS") or []
        methods = [m for m in methods if isinstance(m, str)]
        m = re.search(r"Class for the (Razer .+?)\s*$", c["doc"].strip().splitlines()[0] if c["doc"] else "")
        label = m.group(1).strip() if m else re.sub(r"(?<!^)(?=[A-Z])", " ", name)
        dtype = TYPE_BY_FILE.get(file_of[name], "accessory")
        for t in ("mouse", "keyboard", "keypad", "mousemat", "headset", "accessory", "core"):
            if f"get_device_type_{t}" in methods:
                dtype = {"core": "accessory"}.get(t, t)
        devices[pid] = {
            "class": name,
            "name": label,
            "type": dtype,
            "methods": methods,
            "max_dpi": ev.attr(name, "DPI_MAX"),
            "poll_rates": ev.attr(name, "POLL_RATES"),
            "available_dpi": ev.attr(name, "AVAILABLE_DPI"),
            "wave_dirs": ev.attr(name, "WAVE_DIRS"),
            "matrix_brightness": inherits(name, "_RazerDeviceBrightnessSuspend") or "set_brightness" in methods,
        }
    return devices


# ---------------------------------------------------------------- kernel (C text)


def parse_pid_defines(root):
    pid_of, driver_of = {}, {}
    for fn in os.listdir(os.path.join(root, "driver")):
        if not fn.endswith(".h"):
            continue
        for m in re.finditer(r"#define\s+USB_DEVICE_ID_(RAZER_\w+)\s+0x([0-9A-Fa-f]+)", open(os.path.join(root, "driver", fn)).read()):
            pid_of[m.group(1)] = int(m.group(2), 16)
            driver_of[int(m.group(2), 16)] = fn.replace("_driver.h", "")
    return pid_of, driver_of


FUNC_RE = re.compile(r"^(?:static\s+)?[\w\s\*]+?\b(\w+)\s*\(([^;{]*)\)\s*\{", re.M)


def iter_functions(src):
    for m in FUNC_RE.finditer(src):
        start = m.end()
        depth, i = 1, start
        while depth and i < len(src):
            ch = src[i]
            depth += ch == "{"
            depth -= ch == "}"
            i += 1
        yield m.group(1), src[start:i]


CALL_RE = re.compile(r"\b(razer_(?:chroma|naga)_\w+)\s*\(([^;]*)\)\s*;")
TID_RE = re.compile(r"transaction_id\.id\s*=\s*(0x[0-9A-Fa-f]+)")


def case_groups(body):
    """Yield (pid_names | ['default'], text) for each case group in switch statements on the pid."""
    if "switch" not in body or "USB_DEVICE_ID_" not in body:
        return
    groups, cur_names, cur_text, in_stmt = [], [], [], False
    for line in body.splitlines():
        s = line.strip()
        cm = re.match(r"case\s+USB_DEVICE_ID_(RAZER_\w+)\s*:", s)
        if cm or s.startswith("default:"):
            if in_stmt and cur_names:
                groups.append((cur_names, "\n".join(cur_text)))
                cur_names, cur_text = [], []
            in_stmt = False
            cur_names.append(cm.group(1) if cm else "default")
            continue
        if cur_names:
            cur_text.append(s)
            if s and not s.startswith("//"):
                in_stmt = True
    if cur_names:
        groups.append((cur_names, "\n".join(cur_text)))
    return groups


def family_of(call):
    if call.startswith("razer_chroma_extended_matrix_effect_") or call.startswith("razer_chroma_extended_matrix_set_custom"):
        return "extended"
    if call.startswith("razer_chroma_mouse_extended_matrix_effect_"):
        return "mouse_extended"
    if call.startswith("razer_chroma_standard_matrix_effect_"):
        return "standard_matrix"
    if call in ("razer_chroma_standard_set_led_effect", "razer_chroma_standard_set_led_rgb", "razer_chroma_standard_set_led_state"):
        return "standard_led"
    if call.startswith("razer_naga_trinity_effect_"):
        return "naga_trinity"
    return None


def bright_family(call):
    return {
        "razer_chroma_extended_matrix_brightness": "extended",
        "razer_chroma_standard_set_led_brightness": "standard",
        "razer_chroma_misc_set_blade_brightness": "blade",
        "razer_chroma_misc_set_dock_brightness": "dock",
    }.get(call)


def led_arg(args):
    for tok in re.split(r"[,\s()]+", args):
        if tok in LED_CONSTS:
            return LED_CONSTS[tok]
    return None


def parse_kernel(root, pid_of, driver_of):
    info = defaultdict(lambda: {"zone_fam": Counter(), "matrix_fam": Counter(), "matrix_led": Counter(), "bright": Counter(),
                                "tid": Counter(), "tid_explicit": Counter(), "misc": set(), "iface": None, "wait": None, "zone_leds": set()})
    for drv in ("razermouse", "razerkbd", "razeraccessory"):
        path = os.path.join(root, "driver", drv + "_driver.c")
        src = open(path, encoding="utf-8", errors="replace").read()
        drv_pids = {p for p, d in driver_of.items() if d == drv}
        for fname, body in iter_functions(src):
            groups = case_groups(body)
            if not groups:
                continue
            is_matrix = "matrix_effect" in fname and not any(f"_{z}_" in fname for z in ZONES)
            explicit = set()
            parsed = []
            for names, text in groups:
                pids = [pid_of[n] for n in names if n in pid_of]
                explicit.update(pids)
                parsed.append((names, pids, text))
            for names, pids, text in parsed:
                if "default" in names:
                    if "not supported" in text or "dev_warn" in text and not CALL_RE.search(text):
                        continue
                    pids = pids + sorted(drv_pids - explicit)
                    weight = 0.25  # defaults are weaker evidence than explicit cases
                else:
                    weight = 1.0
                calls = CALL_RE.findall(text)
                tids = TID_RE.findall(text)
                for pid in pids:
                    d = info[pid]
                    for t in tids:
                        d["tid"][int(t, 16)] += weight
                        if weight == 1.0:
                            d["tid_explicit"][int(t, 16)] += 1
                    for call, args in calls:
                        fam = family_of(call)
                        if fam:
                            if is_matrix:
                                d["matrix_fam"][fam] += weight
                                led = led_arg(args)
                                if led is not None:
                                    d["matrix_led"][led] += weight
                            else:
                                d["zone_fam"][fam] += weight
                                led = led_arg(args)
                                if led is not None:
                                    d["zone_leds"].add(led)
                        bf = bright_family(call)
                        if bf:
                            d["bright"][bf] += weight
                        if call == "razer_chroma_misc_set_polling_rate2":
                            d["misc"].add("poll2")
                            if "0x01" in args:
                                d["misc"].add("poll2_double")
                        if call == "razer_chroma_misc_set_dpi_xy_byte":
                            d["misc"].add("dpi_byte")
            # transport parameters
            if fname in ("razer_get_report_params", "razer_get_report"):
                for names, pids, text in parsed:
                    idx = re.search(r"report_index\s*=\s*(0x[0-9A-Fa-f]+)|index\s*=\s*(0x[0-9A-Fa-f]+)", text)
                    wait = re.search(r"(RAZER_\w+_WAIT_US)", text)
                    targets = pids if "default" not in names else sorted(drv_pids - explicit)
                    for pid in targets:
                        if idx:
                            info[pid]["iface"] = int(idx.group(1) or idx.group(2), 16)
                        if wait:
                            info[pid]["wait"] = wait.group(1)
        # driver default interface
        for pid in drv_pids:
            if info[pid]["iface"] is None:
                info[pid]["iface"] = {"razerkbd": 0x01}.get(drv, 0x00)
    waits = {}
    for fn in os.listdir(os.path.join(root, "driver")):
        if fn.endswith(".h"):
            for m in re.finditer(r"#define\s+(RAZER_\w+_WAIT_US)\s+(\d+)", open(os.path.join(root, "driver", fn)).read()):
                waits[m.group(1)] = int(m.group(2))
    return info, waits


# ---------------------------------------------------------------- merge


def zones_from_methods(methods):
    zones = {}
    for z in ZONES:
        effects = [e for e in EFFECT_NAMES if f"set_{z}_{e}" in methods]
        if effects or f"set_{z}_brightness" in methods:
            zones[z] = {"effects": effects, "brightness": f"set_{z}_brightness" in methods}
    matrix = [e for e in EFFECT_NAMES if f"set_{e}_effect" in methods]
    if "set_ripple_effect" in methods:
        matrix.append("ripple")
    if matrix or "set_brightness" in methods:
        zones["matrix"] = {"effects": sorted(set(matrix), key=EFFECT_NAMES.index), "brightness": "set_brightness" in methods}
    return zones


def main():
    root = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "..", "razer_control", "devices.json")
    daemon = parse_daemon(root)
    pid_of, driver_of = parse_pid_defines(root)
    kernel, waits = parse_kernel(root, pid_of, driver_of)

    db = {}
    for pid in sorted(set(daemon) | set(driver_of)):
        d = daemon.get(pid)
        k = kernel.get(pid)
        drv = driver_of.get(pid)
        if d is None:
            # in the kernel but not in the daemon: name from the #define
            name = next((n for n, p in pid_of.items() if p == pid), f"RAZER_{pid:04X}")
            d = {"name": name.replace("_", " ").title().replace("Razer", "Razer"), "type": {"razermouse": "mouse", "razerkbd": "keyboard"}.get(drv, "accessory"),
                 "methods": [], "max_dpi": None, "poll_rates": None, "available_dpi": None, "wave_dirs": None}
        m = d["methods"]
        entry = {"name": d["name"], "type": d["type"]}
        if drv == "razerkraken" or d["type"] == "headset" and drv not in ("razermouse", "razerkbd", "razeraccessory"):
            entry["protocol"] = "kraken"  # different protocol, not supported
        else:
            entry["protocol"] = "razer"
        feats = {}
        if "max_dpi" in m or "set_dpi_xy" in m:
            feats["dpi"] = True
        if "set_dpi_xy_byte" in m or (k and "dpi_byte" in k["misc"]):
            feats["dpi_byte"] = True
            feats.pop("dpi", None)
        if "set_dpi_stages" in m:
            feats["dpi_stages"] = True
        if "set_poll_rate" in m:
            feats["poll"] = True
            if k and "poll2" in k["misc"]:
                feats["poll2"] = True
            if k and "poll2_double" in k["misc"]:
                feats["poll2_double"] = True
        if "get_battery" in m:
            feats["battery"] = True
        if "is_charging" in m:
            feats["charging"] = True
        if "set_idle_time" in m:
            feats["idle"] = True
        if "set_low_battery_threshold" in m:
            feats["low_battery"] = True
        if feats:
            entry["features"] = feats
        if d.get("max_dpi"):
            entry["max_dpi"] = d["max_dpi"]
        if d.get("available_dpi"):
            entry["available_dpi"] = d["available_dpi"]
        if d.get("poll_rates"):
            entry["poll_rates"] = d["poll_rates"]
        zones = zones_from_methods(m)
        if "matrix" in zones and d.get("matrix_brightness"):
            zones["matrix"]["brightness"] = True
        if zones:
            entry["zones"] = zones
        if d.get("wave_dirs"):
            entry["wave_dirs"] = d["wave_dirs"]
        if k:
            lt = {}
            if k["zone_fam"]:
                lt["zone"] = k["zone_fam"].most_common(1)[0][0]
            if k["matrix_fam"]:
                lt["matrix"] = k["matrix_fam"].most_common(1)[0][0]
            if k["matrix_led"]:
                lt["matrix_led"] = k["matrix_led"].most_common(1)[0][0]
            if k["bright"]:
                lt["brightness"] = k["bright"].most_common(1)[0][0]
            if lt:
                entry["lighting"] = lt
            if k["tid_explicit"]:
                entry["tid"] = k["tid_explicit"].most_common(1)[0][0]
            elif k["tid"]:
                entry["tid"] = k["tid"].most_common(1)[0][0]
            entry["iface"] = k["iface"] if k["iface"] is not None else 0
            if k["wait"] and k["wait"] in waits:
                entry["wait_us"] = waits[k["wait"]]
        db[f"{pid:04x}"] = entry

    with open(out, "w") as f:
        json.dump({"source": "openrazer", "devices": db}, f, indent=1, sort_keys=True)
    types = Counter(e["type"] for e in db.values())
    print(f"wrote {len(db)} devices to {out}: {dict(types)}")


if __name__ == "__main__":
    main()
