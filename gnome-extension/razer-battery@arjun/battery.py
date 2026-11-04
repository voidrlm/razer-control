"""Prints the battery of every connected Razer device that has one, as JSON.

Uses the razer_control package from Razer Control, talking to the device over
hidraw. The package is found next to this extension when it's installed from
the repo (a symlink), otherwise through the razer-control-cli launcher.
"""
import json
import os
import sys

here = os.path.dirname(os.path.realpath(__file__))
home = os.path.expanduser("~")
for cand in (os.path.join(here, "..", "..", "razer-control-cli"),
             os.path.join(home, ".local/bin/razer-control-cli"),
             os.path.join(home, "razer-control/razer-control-cli")):
    if os.path.exists(cand):
        sys.path.insert(0, os.path.dirname(os.path.realpath(cand)))
        break

from razer_control.protocol import DeviceAsleep, RazerDevice, RazerError, find_nodes  # noqa: E402

groups = {}
for n in find_nodes():
    groups.setdefault((n.usb_path, n.pid), []).append(n)

out = []
for nodes in groups.values():
    dev = RazerDevice(nodes)
    if not dev.model.supported or not dev.model.has_battery:
        continue
    entry = {"pid": f"{dev.pid:04x}", "name": dev.name, "kind": dev.model.kind,
             "battery": None, "charging": False, "asleep": False}
    try:
        dev.connect()
        entry["battery"] = dev.battery()
        if dev.model.features.get("charging"):
            entry["charging"] = dev.charging()
    except DeviceAsleep:
        entry["asleep"] = True
    except (RazerError, OSError) as e:
        entry["error"] = str(e)
    finally:
        entry["asleep"] = entry["asleep"] or dev.asleep
        dev.close()
    out.append(entry)

print(json.dumps(out))
