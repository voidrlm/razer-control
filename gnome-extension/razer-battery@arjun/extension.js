import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GUdev from 'gi://GUdev';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

Gio._promisify(Gio.Subprocess.prototype, 'communicate_utf8_async');

// Matches the pill height of other top-bar pills.
const PILL_HEIGHT = 22;
const WARN = 40;
const LOW = 20;
const COLORS = {ok: [0.34, 0.89, 0.54], warn: [0.96, 0.83, 0.18], low: [1, 0.42, 0.37]};
// How often to re-read the battery while a Razer device is plugged in.
const REFRESH_SECONDS = 60;
// hidraw nodes come and go in bursts when the dongle is plugged in.
const SETTLE_MS = 1500;
const RAZER_HID = /^HID_ID=0003:00001532:/m;
const APP_ID = 'io.github.voidrlm.RazerControl.desktop';

const KIND_ICONS = {
    mouse: 'input-mouse-symbolic',
    keyboard: 'input-keyboard-symbolic',
    keypad: 'input-gaming-symbolic',
    headset: 'audio-headset-symbolic',
};

function levelOf(pct) {
    return pct <= LOW ? 'low' : pct <= WARN ? 'warn' : 'ok';
}

// Any Razer USB device with a hidraw node, checked without spawning Python.
function razerPresent() {
    let dir;
    try {
        dir = Gio.File.new_for_path('/sys/class/hidraw').enumerate_children('standard::name', 0, null);
    } catch {
        return false;
    }
    let info;
    while ((info = dir.next_file(null))) {
        try {
            const [, bytes] = GLib.file_get_contents(`/sys/class/hidraw/${info.get_name()}/device/uevent`);
            if (RAZER_HID.test(new TextDecoder().decode(bytes))) return true;
        } catch {}
    }
    return false;
}

function batteryRing(dev, size, lineWidth, iconSize) {
    const bin = new St.Widget({
        layout_manager: new Clutter.BinLayout(),
        width: size,
        height: size,
        y_align: Clutter.ActorAlign.CENTER,
        opacity: dev.asleep ? 140 : 255,
    });
    const area = new St.DrawingArea({width: size, height: size});
    const frac = Math.max(0, Math.min(1, dev.battery / 100));
    const [r, g, b] = COLORS[dev.charging ? 'ok' : levelOf(dev.battery)];
    area.connect('repaint', a => {
        const cr = a.get_context();
        const [w, h] = a.get_surface_size();
        const lw = lineWidth * w / size;
        const radius = (Math.min(w, h) - lw) / 2;
        cr.setLineWidth(lw);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setSourceRGBA(1, 1, 1, 0.16);
        cr.arc(w / 2, h / 2, radius, 0, 2 * Math.PI);
        cr.stroke();
        if (frac > 0) {
            cr.setSourceRGBA(r, g, b, 1);
            cr.arc(w / 2, h / 2, radius, -Math.PI / 2, -Math.PI / 2 + 2 * Math.PI * frac);
            cr.stroke();
        }
        cr.$dispose();
    });
    bin.add_child(area);
    bin.add_child(new St.Icon({
        icon_name: dev.charging ? 'battery-level-100-charged-symbolic' : KIND_ICONS[dev.kind] ?? 'battery-symbolic',
        icon_size: iconSize,
        x_align: Clutter.ActorAlign.CENTER,
        y_align: Clutter.ActorAlign.CENTER,
    }));
    return bin;
}

export default class RazerBattery extends Extension {
    enable() {
        // pid -> last good reading {pid, name, kind, battery, charging, asleep}
        this._readings = new Map();
        this._cancellable = new Gio.Cancellable();

        this._button = new PanelMenu.Button(0.5, this.metadata.name);
        this._button.add_style_class_name('rzbat-panel');
        this._panelBox = new St.BoxLayout({
            style_class: 'rzbat-pill',
            height: PILL_HEIGHT,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._button.add_child(this._panelBox);
        this._button.visible = false;
        this._button.menu.box.add_style_class_name('rzbat-menu');
        this._section = new PopupMenu.PopupMenuSection();
        this._button.menu.addMenuItem(this._section);
        this._button.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._button.menu.addAction('Open Razer Control', () => {
            Shell.AppSystem.get_default().lookup_app(APP_ID)?.activate();
        });
        this._button.menu.connect('open-state-changed', (m, open) => {
            if (open) this._refresh();
        });

        Main.panel.addToStatusArea(this.uuid, this._button, 0, 'right');
        this._place();
        Main.panel._rightBox.connectObject('child-added', () => {
            if (this._placeId) return;
            this._placeId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                this._placeId = 0;
                this._place();
                return GLib.SOURCE_REMOVE;
            });
        }, this);

        this._udev = new GUdev.Client({subsystems: ['hidraw']});
        this._udev.connectObject('uevent', () => this._queueRefresh(SETTLE_MS), this);

        this._refresh();
        this._refreshId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, REFRESH_SECONDS, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    // Directly left of the Audio Battery or Now Playing pill when those are
    // installed; otherwise it stays where GNOME put it.
    _place() {
        const box = Main.panel._rightBox;
        const sa = Main.panel.statusArea;
        const anchor = (sa['audio-battery@arjun'] ?? sa['now-playing@arjun'])?.container;
        const me = this._button?.container;
        if (!me || !anchor || anchor.get_parent() !== box) return;
        const kids = box.get_children();
        const ai = kids.indexOf(anchor), ci = kids.indexOf(me);
        const want = ci < ai ? ai - 1 : ai;
        if (ci !== want) box.set_child_at_index(me, want);
    }

    _queueRefresh(ms) {
        if (this._queueId) GLib.source_remove(this._queueId);
        this._queueId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._queueId = 0;
            this._refresh();
            return GLib.SOURCE_REMOVE;
        });
    }

    async _refresh() {
        if (this._refreshing) {
            this._refreshAgain = true;
            return;
        }
        this._refreshing = true;
        try {
            let list = [];
            if (razerPresent()) {
                const proc = Gio.Subprocess.new(['python3', '-I', `${this.path}/battery.py`],
                    Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
                const [out, err] = await proc.communicate_utf8_async(null, this._cancellable);
                if (!proc.get_successful()) throw new Error(err.trim() || 'battery helper failed');
                list = JSON.parse(out);
            }
            if (!this._button) return;
            const seen = new Set();
            for (const d of list) {
                seen.add(d.pid);
                const old = this._readings.get(d.pid);
                if (d.battery !== null) {
                    this._readings.set(d.pid, d);
                } else if (old) {
                    // Asleep or busy: keep the last reading, shown dimmed.
                    old.asleep = true;
                    old.charging = false;
                }
            }
            for (const pid of [...this._readings.keys()]) {
                if (!seen.has(pid)) this._readings.delete(pid);
            }
            this._sync();
        } catch (e) {
            if (this._button && !e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                console.error(`${this.uuid}: ${e}`);
        } finally {
            this._refreshing = false;
            if (this._refreshAgain && this._button) {
                this._refreshAgain = false;
                this._refresh();
            }
        }
    }

    _sync() {
        const devs = [...this._readings.values()];
        this._button.visible = devs.length > 0;

        this._panelBox.destroy_all_children();
        for (const d of devs) {
            this._panelBox.add_child(batteryRing(d, 18, 2, 10));
            this._panelBox.add_child(new St.Label({
                text: `${d.battery}%`,
                style_class: 'rzbat-pct',
                y_align: Clutter.ActorAlign.CENTER,
                opacity: d.asleep ? 140 : 255,
            }));
        }

        this._section.removeAll();
        for (const d of devs) {
            const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
            const row = new St.BoxLayout({x_expand: true, style_class: 'rzbat-device'});
            row.add_child(batteryRing(d, 44, 3.5, 20));
            const text = new St.BoxLayout({vertical: true, y_align: Clutter.ActorAlign.CENTER});
            text.add_child(new St.Label({text: d.name.replace(/^Razer /, ''), style_class: 'rzbat-name'}));
            const state = d.charging ? 'charging' : d.asleep ? 'asleep · last reading' : null;
            text.add_child(new St.Label({
                text: state ? `${d.battery}% · ${state}` : `${d.battery}%`,
                style_class: 'rzbat-sub',
            }));
            row.add_child(text);
            item.add_child(row);
            this._section.addMenuItem(item);
        }
    }

    disable() {
        for (const id of ['_refreshId', '_queueId', '_placeId']) {
            if (this[id]) {
                GLib.source_remove(this[id]);
                this[id] = 0;
            }
        }
        this._cancellable?.cancel();
        this._udev?.disconnectObject(this);
        Main.panel._rightBox.disconnectObject(this);
        this._button?.destroy();
        this._button = this._panelBox = this._section = this._udev = null;
        this._readings = this._cancellable = null;
        this._refreshing = this._refreshAgain = false;
    }
}
