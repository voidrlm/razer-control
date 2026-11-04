#!/usr/bin/env bash
# Removes Razer Control's launcher, app-grid entry and udev rule. Settings in ~/.config/razer-control are kept
# unless you pass --purge. Settings stored on your devices are not touched.
set -euo pipefail
rm -f ~/.local/bin/razer-control ~/.local/bin/razer-control-cli
rm -f ~/.local/share/applications/io.github.voidrlm.RazerControl.desktop ~/.local/share/applications/io.github.arjun.RazerControl.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
gnome-extensions disable razer-battery@arjun 2>/dev/null || true
ext=~/.local/share/gnome-shell/extensions/razer-battery@arjun
[ -L "$ext" ] && rm -f "$ext"
if [ -f /etc/udev/rules.d/70-razer-control.rules ]; then
    cmd="rm -f /etc/udev/rules.d/70-razer-control.rules && udevadm control --reload"
    if [ -t 0 ] && command -v sudo >/dev/null; then sudo sh -c "$cmd"; else pkexec sh -c "$cmd"; fi
fi
[ "${1:-}" = "--purge" ] && rm -rf ~/.config/razer-control
echo "Razer Control removed."
