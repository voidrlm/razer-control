# Razer Control

A native GNOME app (GTK4 + libadwaita) for configuring Razer mice, keyboards, keypads, mouse mats and
accessories on Linux. You can change DPI, polling rate, button mappings, lighting effects and battery settings.

It talks to devices directly over `/dev/hidraw` using Razer's USB protocol, so it needs **no kernel module,
no DKMS and no background daemon**. Settings are saved on the device itself, so they keep working on other
computers and when the app isn't running.

## Features

| Tab | What you can do |
|---|---|
| **Device** | Battery level and charging state, firmware, serial number, current DPI. Switch between devices when more than one is connected |
| **Performance** | Up to 5 DPI stages with an active-stage picker and presets, or a single DPI value; polling rate (125–8000 Hz depending on model) |
| **Buttons** | Remap mouse buttons to another mouse button, a keyboard key or shortcut (with a recorder), DPI cycle, DPI clutch (sniper), or disable them |
| **Lighting** | Per-zone effects (logo, scroll wheel, underglow, keyboard, dock…): static, breathing (1/2/random colors), spectrum, wave, wheel, reactive, starlight, off, plus brightness |
| **Power** | Sleep timeout and low-battery warning threshold for wireless devices |

There's also a full command-line tool, `razer-control-cli`, and on GNOME a top-bar battery indicator
(see [GNOME top-bar battery](#gnome-top-bar-battery)).

## Supported devices

The built-in device database covers **267 Razer USB models**: 117 mice, 115 keyboards (including Razer
Blade laptop keyboards), 7 keypads, 8 mouse mats and 19 accessories (docks, headset stands, Chroma hubs…).
To see the full list, open **Menu → Supported Devices** in the app or run `razer-control-cli supported`.

| Support level | Meaning |
|---|---|
| **Verified** | Tested on real hardware: Viper Ultimate (wired and wireless) and the Razer Mouse Dock |
| **Known** | In the database. Features, lighting zones, DPI range and polling rates come from the device database. The USB interface, protocol ID and LED IDs are confirmed by probing the device at runtime |
| **Unknown** | Any other Razer USB device. The app probes it and enables whatever responds |

Not supported:
- **Kraken headsets**, which use a different protocol.
- **Bluetooth connections.** Use the USB cable or dongle instead.
- **Per-key custom lighting** and software-animated effects such as ripple. Only effects built into the device are available.

If your device is "Known" but something doesn't work, please
[open an issue](https://github.com/voidrlm/razer-control/issues) and include the output of `razer-control-cli status`.

## Requirements

- Linux with a desktop session (GNOME is the best fit, but any desktop works)
- Python 3.9 or newer
- PyGObject, GTK 4.10+ and libadwaita 1.6+

Install the dependencies for your distribution:

```sh
# Fedora 41+
sudo dnf install python3-gobject gtk4 libadwaita git

# Debian 13+ / Ubuntu 25.04+
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 git

# Arch Linux / Manjaro
sudo pacman -S python-gobject gtk4 libadwaita git

# openSUSE Tumbleweed
sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 git
```

Ubuntu 24.04 ships libadwaita 1.5, which is too old for the GUI, but the command-line tool still works there.

## Installation

```sh
git clone https://github.com/voidrlm/razer-control.git
cd razer-control
./install.sh
```

The installer:
1. Checks the dependencies above and tells you what's missing.
2. Links `razer-control` and `razer-control-cli` into `~/.local/bin`.
3. Adds **Razer Control** to your app grid.
4. On GNOME, installs and enables the **Razer Battery** top-bar extension.
5. Installs a udev rule, `/etc/udev/rules.d/70-razer-control.rules`, that gives the logged-in user access to
   Razer devices. This is the only step that needs your admin password, and it's asked for once.

Then **unplug and replug your Razer device (or its wireless dongle) once** and open **Razer Control** from the
app grid, or run `razer-control`.

Keep the cloned folder: the installed launchers point into it. To update later:

```sh
cd razer-control
git pull
./install.sh
```

### Running without installing

```sh
./razer-control          # GUI
./razer-control-cli -h   # CLI
```

You still need the udev rule (or root) for device access. You can install just the rule with:

```sh
sudo install -m644 70-razer-control.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

## Command-line usage

```sh
razer-control-cli devices                      # list connected devices
razer-control-cli status                       # show all settings
razer-control-cli supported basilisk           # search the device database

razer-control-cli dpi 1600
razer-control-cli stages 2 400,800,1600,3200   # active stage 2
razer-control-cli poll 1000

razer-control-cli light static '#ff0040'
razer-control-cli light breathing_dual '#ff0000' '#0000ff'
razer-control-cli light spectrum --zone logo
razer-control-cli -d keyboard light wave --direction 2
razer-control-cli --dock light spectrum
razer-control-cli brightness 60

razer-control-cli idle 300                     # sleep after 5 minutes

razer-control-cli buttons                      # list button mappings
razer-control-cli button 0x05 key:ctrl+c
razer-control-cli button 0x04 mouse:forward
razer-control-cli button 0x05 clutch:400
razer-control-cli button 0x05 default
```

Use `-d` to pick a device by index (from `devices`), USB product ID (`007b`) or type (`mouse`, `keyboard`,
`keypad`, `mousemat`, `accessory`).

## GNOME top-bar battery

On GNOME Shell 45 or newer, `install.sh` also installs the **Razer Battery** extension
(`gnome-extension/razer-battery@arjun`). While a wireless Razer device's dongle or cable is plugged in, the
extension shows a pill in the top bar with a ring and the battery percentage. The ring turns yellow at 40% and
red at 20%, and a charging icon appears while the device charges. If the device is asleep, the pill keeps its
last reading and dims it. Click the pill to see each device's name and state, or to open Razer Control.

The pill appears or disappears within a couple of seconds of plugging or unplugging, and the battery is read
again every 60 seconds. The extension reads the battery with `gnome-extension/razer-battery@arjun/battery.py`,
which uses the same `razer_control` code as the app.

On Wayland, GNOME only loads a newly installed extension at login, so log out and back in once after the first
install. Later updates through `git pull` also take effect at your next login.

## Troubleshooting

**"Permission Needed" / "No permission for /dev/hidrawN"**: the udev rule isn't active yet. Run
`./install.sh` again, then unplug and replug the device. Check with `ls -l /dev/hidraw*`: the device nodes
should show a `+` (ACL) after the permissions.

**"Device is not responding"**: wireless mice go to sleep. Move or click the mouse and press refresh.

**Device not listed**: run `razer-control-cli devices`. Bluetooth connections and Kraken headsets aren't
supported. For other devices, open an issue and include the output of `lsusb | grep 1532`.

**openrazer is also installed**: Razer Control works alongside openrazer, but if both change settings
they'll overwrite each other. Stop the openrazer daemon (`systemctl --user stop openrazer-daemon`) while
using Razer Control.

**Keyboard shortcut mapped to a button does nothing**: the mouse sends a real keystroke, so check whether
your desktop already uses that shortcut. F13–F24 are good choices for custom shortcuts in GNOME Settings →
Keyboard → Custom Shortcuts.

## Uninstall

```sh
./uninstall.sh          # removes launchers, app-grid entry, GNOME extension and udev rule
./uninstall.sh --purge  # also deletes ~/.config/razer-control
```

Settings stored on your devices are left as they are.

## How it works

Razer devices accept 90-byte HID feature reports on one of their USB interfaces. The app finds each device's
hidraw nodes, works out which interface and transaction ID the device responds to, and sends commands with
`HIDIOCSFEATURE` / `HIDIOCGFEATURE` ioctls. Lighting is driven with whichever of Razer's four command families
the model uses. Per-model details live in `razer_control/devices.json`, and hardware-verified corrections live
in `VERIFIED` in `razer_control/protocol.py`.

```
razer_control/
  protocol.py     device discovery, Razer report protocol, lighting/DPI/battery commands
  buttons.py      button-mapping data model and key tables
  app.py          GTK4/libadwaita GUI
  buttons_ui.py   Buttons tab and remap dialog
  cli.py          command-line tool
  devices.json    generated device database
gnome-extension/
  razer-battery@arjun/   GNOME Shell top-bar battery indicator (extension.js + battery.py helper)
tools/
  gen_devices.py  rebuilds devices.json from an openrazer checkout
```

### Updating the device database

```sh
git clone --depth 1 https://github.com/openrazer/openrazer /tmp/openrazer
python3 -I tools/gen_devices.py /tmp/openrazer razer_control/devices.json
```

The generator reads openrazer's device definitions with Python's `ast` module, so it never runs their code. It
also parses the per-device `switch` statements in openrazer's kernel drivers to get each model's protocol details.

## Credits

- Device data comes from the [openrazer](https://github.com/openrazer/openrazer) project (GPL-2.0).
- The button-remapping protocol notes come from [OpenSnek](https://github.com/gh123man/opensnek).

This project isn't affiliated with or endorsed by Razer Inc. "Razer" and product names are trademarks of their
respective owners.

## License

[GPL-2.0-or-later](LICENSE)
