"""Razer Control — a GTK4 / libadwaita app for Razer devices."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .buttons_ui import ButtonsPage  # noqa: E402
from .protocol import (  # noqa: E402
    EFFECT_LABELS,
    EFFECTS_NEED,
    DeviceAsleep,
    RazerDevice,
    RazerError,
    Zone,
    device_db,
    open_devices,
)

APP_ID = "io.github.voidrlm.RazerControl"
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "razer-control")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
UDEV_RULE_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "70-razer-control.rules")
BATTERY_REFRESH_S = 60

KIND_ICONS = {
    "mouse": "input-mouse-symbolic",
    "keyboard": "input-keyboard-symbolic",
    "keypad": "input-gaming-symbolic",
    "mousemat": "input-touchpad-symbolic",
    "accessory": "applications-system-symbolic",
    "headset": "audio-headset-symbolic",
}
KIND_LABELS = {
    "mouse": "Mouse", "keyboard": "Keyboard", "keypad": "Keypad", "mousemat": "Mouse mat",
    "accessory": "Accessory", "headset": "Headset",
}


# ---------------------------------------------------------------------------
# helpers


def load_settings() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def hex_to_rgb(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    return tuple(int(s[i : i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def rgba_from_hex(s: str) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.parse(s)
    return c


def rgba_to_rgb(c: Gdk.RGBA) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(v * 255))) for v in (c.red, c.green, c.blue))


def idle_call(fn, *args):
    def _run():
        fn(*args)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(_run)


def icon_or(name: str, fallback: str) -> str:
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    return name if name and theme.has_icon(name) else fallback


class Worker:
    """Single background thread so device I/O never blocks the UI and stays in order."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, fn, done=None, error=None):
        self.q.put((fn, done, error))

    def _loop(self):
        while True:
            fn, done, error = self.q.get()
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                if error:
                    idle_call(error, e)
                continue
            if done:
                idle_call(done, res)


class Debounce:
    def __init__(self, ms: int, fn):
        self.ms, self.fn, self.src = ms, fn, 0

    def __call__(self, *_):
        if self.src:
            GLib.source_remove(self.src)
        self.src = GLib.timeout_add(self.ms, self._fire)

    @property
    def pending(self) -> bool:
        return bool(self.src)

    def _fire(self):
        self.src = 0
        self.fn()
        return GLib.SOURCE_REMOVE


def battery_icon(level: int, charging: bool) -> str:
    step = max(0, min(100, int(round(level / 10.0)) * 10))
    return f"battery-level-{step}{'-charging' if charging else ''}-symbolic"


# ---------------------------------------------------------------------------
# lighting section (one per zone)


class LightingSection:
    SPEEDS = ["Fast", "Medium", "Slow", "Slowest"]

    def __init__(self, view: "DeviceView", zone: Zone):
        self.view, self.zone = view, zone
        self.key = f"{zone.dev.pid:04x}:{zone.key}"
        settings = view.win.settings.setdefault("lighting", {})
        saved = settings.get(self.key) or settings.get(f"{zone.dev.pid:04x}", {})
        self.effects = list(zone.effects)
        eff = saved.get("effect", "static")
        self.state = {
            "effect": eff if eff in self.effects else self.effects[0],
            "color1": saved.get("color1", "#00ff40"),
            "color2": saved.get("color2", "#0060ff"),
            "speed": saved.get("speed", 2),
            "direction": saved.get("direction", 0),
        }
        self.loading = True

        self.group = Adw.PreferencesGroup(title=zone.label)

        self.effect_row = Adw.ComboRow(title="Effect", model=Gtk.StringList.new([EFFECT_LABELS[e] for e in self.effects]))
        self.effect_row.set_selected(self.effects.index(self.state["effect"]))
        self.effect_row.connect("notify::selected", self._changed)
        self.group.add(self.effect_row)

        self.color1_btn = self._color_button(self.state["color1"])
        self.color1_row = Adw.ActionRow(title="Color")
        self.color1_row.add_suffix(self.color1_btn)
        self.color1_row.set_activatable_widget(self.color1_btn)
        self.group.add(self.color1_row)

        self.color2_btn = self._color_button(self.state["color2"])
        self.color2_row = Adw.ActionRow(title="Second color")
        self.color2_row.add_suffix(self.color2_btn)
        self.color2_row.set_activatable_widget(self.color2_btn)
        self.group.add(self.color2_row)

        self.speed_row = Adw.ComboRow(title="Speed", model=Gtk.StringList.new(self.SPEEDS))
        self.speed_row.set_selected(max(0, min(3, int(self.state["speed"]) - 1)))
        self.speed_row.connect("notify::selected", self._changed)
        self.group.add(self.speed_row)

        self.dir_row = Adw.ComboRow(title="Direction", model=Gtk.StringList.new(["Left to right", "Right to left"]))
        self.dir_row.set_selected(max(0, min(1, int(self.state["direction"]))))
        self.dir_row.connect("notify::selected", self._changed)
        self.group.add(self.dir_row)

        self.bright_row = None
        if zone.has_brightness:
            self.bright_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
            self.bright_scale.set_hexpand(True)
            self.bright_scale.set_valign(Gtk.Align.CENTER)
            self.bright_scale.set_size_request(180, -1)
            self.bright_scale.set_draw_value(True)
            self.bright_scale.set_value_pos(Gtk.PositionType.RIGHT)
            self.bright_scale.set_format_value_func(lambda _s, v: f"{int(v)}%")
            self.bright_row = Adw.ActionRow(title="Brightness")
            self.bright_row.add_suffix(self.bright_scale)
            self.group.add(self.bright_row)
            self.apply_brightness = Debounce(200, self._apply_brightness)
            self.bright_scale.connect("value-changed", lambda *_: None if self.loading else self.apply_brightness())

        self.apply_effect = Debounce(250, self._apply_effect)
        self._update_visibility()
        self.loading = False

    def _color_button(self, hex_color: str) -> Gtk.ColorDialogButton:
        b = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False, title="Choose color"))
        b.set_rgba(rgba_from_hex(hex_color))
        b.set_valign(Gtk.Align.CENTER)
        b.connect("notify::rgba", self._changed)
        return b

    def _effect(self) -> str:
        return self.effects[self.effect_row.get_selected()]

    def _update_visibility(self):
        eff = self._effect()
        self.color1_row.set_visible(eff in EFFECTS_NEED["color1"])
        self.color2_row.set_visible(eff in EFFECTS_NEED["color2"])
        self.speed_row.set_visible(eff in EFFECTS_NEED["speed"])
        self.dir_row.set_visible(eff in EFFECTS_NEED["direction"])
        if self.bright_row:
            self.bright_row.set_sensitive(eff != "off")

    def _changed(self, *_):
        if self.loading:
            return
        self._update_visibility()
        self.state.update(
            effect=self._effect(),
            color1=rgb_to_hex(rgba_to_rgb(self.color1_btn.get_rgba())),
            color2=rgb_to_hex(rgba_to_rgb(self.color2_btn.get_rgba())),
            speed=self.speed_row.get_selected() + 1,
            direction=self.dir_row.get_selected(),
        )
        self.view.win.settings["lighting"][self.key] = dict(self.state)
        self.view.win.persist()
        self.apply_effect()

    def _apply_effect(self):
        s, zone = dict(self.state), self.zone
        dirs = zone.dev.model.wave_dirs
        direction = dirs[min(s["direction"], len(dirs) - 1)]
        self.view.run(lambda: zone.apply(s["effect"], hex_to_rgb(s["color1"]), hex_to_rgb(s["color2"]), s["speed"], direction))

    def _apply_brightness(self):
        v, zone = int(self.bright_scale.get_value()), self.zone
        self.view.run(lambda: zone.set_brightness(v))

    def set_brightness(self, v: int):
        if self.bright_row:
            self.loading = True
            self.bright_scale.set_value(v)
            self.loading = False


# ---------------------------------------------------------------------------
# one device's pages


class DeviceView:
    def __init__(self, win: "MainWindow", dev: RazerDevice):
        self.win, self.dev, self.model = win, dev, dev.model
        self.stack = Adw.ViewStack()
        self.lighting: list[LightingSection] = []
        self.stages: list[int] = []
        self.active_stage = 1
        self.loading = False
        self.asleep_retry = 0
        self.stage_apply = Debounce(500, self._apply_stages)
        self.power_apply = Debounce(600, self._apply_power)
        self.dpi_apply = Debounce(400, self._apply_single_dpi)
        f = self.model.features
        self.has_stages = bool(f.get("dpi_stages"))
        self.has_dpi = bool(f.get("dpi") or f.get("dpi_byte"))
        self.has_power = bool(f.get("idle") or f.get("low_battery"))
        self.buttons_page = None

        self.stack.add_titled_with_icon(self._build_device_page(), "device", "Device",
                                        KIND_ICONS.get(self.model.kind, "input-mouse-symbolic"))
        if self.has_dpi or self.model.polling_rates:
            self.stack.add_titled_with_icon(self._build_performance_page(), "performance", "Performance",
                                            "power-profile-performance-symbolic")
        if self.model.kind == "mouse":
            self.buttons_page = ButtonsPage(self, dev)
            self.stack.add_titled_with_icon(self.buttons_page.widget, "buttons", "Buttons",
                                            "preferences-desktop-keyboard-shortcuts-symbolic")
        self.lighting_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.lighting_stack.add_named(Adw.StatusPage(title="Detecting lighting…", paintable=Adw.SpinnerPaintable()), "loading")
        self.lighting_stack.add_named(
            Adw.StatusPage(icon_name="display-brightness-symbolic", title="No Controllable Lighting",
                           description="This device has no lighting that Razer Control can drive."),
            "none",
        )
        self.lighting_page_added = False
        if self.model.has_lighting or not self.model.known:
            self.stack.add_titled_with_icon(self.lighting_stack, "lighting", "Lighting", "display-brightness-symbolic")
            self.lighting_page_added = True
        if self.has_power:
            self.stack.add_titled_with_icon(self._build_power_page(), "power", "Power", "battery-level-80-symbolic")

    # used by ButtonsPage
    def toast(self, msg):
        self.win.toast(msg)

    def run(self, fn, done=None):
        self.win.run(fn, done)

    # ---- pages -----------------------------------------------------------

    def _build_device_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12, margin_bottom=6)
        icon = Gtk.Image(icon_name=icon_or(KIND_ICONS.get(self.model.kind, ""), "input-mouse-symbolic"), pixel_size=96)
        icon.add_css_class("dim-label")
        hero.append(icon)
        title = Gtk.Label(label=self.dev.name, wrap=True, justify=Gtk.Justification.CENTER)
        title.add_css_class("title-1")
        hero.append(title)

        has_batt = self.model.has_battery
        batt_box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=12, visible=has_batt)
        self.batt_icon = Gtk.Image(icon_name="battery-missing-symbolic", pixel_size=24)
        self.batt_label = Gtk.Label(label="—")
        self.batt_label.add_css_class("title-2")
        batt_box.append(self.batt_icon)
        batt_box.append(self.batt_label)
        hero.append(batt_box)
        self.batt_bar = Gtk.LevelBar(min_value=0, max_value=100, margin_start=48, margin_end=48, visible=has_batt)
        self.batt_bar.add_offset_value("low", 15)
        self.batt_bar.add_offset_value("high", 50)
        self.batt_bar.add_offset_value("full", 100)
        hero.append(self.batt_bar)
        self.batt_sub = Gtk.Label(label="")
        self.batt_sub.add_css_class("dim-label")
        hero.append(self.batt_sub)
        hg = Adw.PreferencesGroup()
        hg.add(hero)
        page.add(hg)

        details = Adw.PreferencesGroup(title="Details")
        rows = [("Type", KIND_LABELS.get(self.model.kind, "Device"))]
        if self.model.kind in ("mouse", "keyboard"):
            rows.append(("Connection", "Wireless" if self.model.wireless else "USB cable"))
        for t, sub in rows:
            r = Adw.ActionRow(title=t, subtitle=sub)
            r.add_css_class("property")
            details.add(r)
        self.dpi_now_row = None
        if self.has_dpi:
            self.dpi_now_row = Adw.ActionRow(title="Current DPI", subtitle="—")
            self.dpi_now_row.add_css_class("property")
            details.add(self.dpi_now_row)
        self.fw_row = Adw.ActionRow(title="Firmware", subtitle="—")
        self.serial_row = Adw.ActionRow(title="Serial number", subtitle="—")
        for r in (self.fw_row, self.serial_row):
            r.add_css_class("property")
            details.add(r)
        usb = Adw.ActionRow(title="USB ID", subtitle=f"1532:{self.dev.pid:04x} · interface {self.dev.node.interface}")
        usb.add_css_class("property")
        details.add(usb)
        if self.model.verified:
            support = "Verified on this hardware"
        elif self.model.known:
            support = "Known model (from the openrazer device database)"
        else:
            support = "Unknown model — basic support, features detected automatically"
        sr = Adw.ActionRow(title="Support", subtitle=support)
        sr.add_css_class("property")
        details.add(sr)
        page.add(details)
        return page

    def _build_performance_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        if self.has_stages:
            self.stage_group = Adw.PreferencesGroup(
                title="DPI Stages", description="Use the DPI button to cycle stages. Changes apply instantly."
            )
            add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add stage", valign=Gtk.Align.CENTER)
            add_btn.add_css_class("flat")
            add_btn.connect("clicked", self._add_stage)
            self.stage_add_btn = add_btn
            self.stage_group.set_header_suffix(add_btn)
            self.stage_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            self.stage_list.add_css_class("boxed-list")
            self.stage_group.add(self.stage_list)
            page.add(self.stage_group)

            presets = Adw.PreferencesGroup(title="Presets")
            box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4, column_spacing=6, row_spacing=6)
            for label, vals in (
                ("Office", [800, 1200, 1600]),
                ("Gaming", [400, 800, 1600, 3200]),
                ("Precision", [400, 800, 1200]),
                ("Default", [400, 800, 1600, 3200, 6400]),
            ):
                vals = [v for v in vals if v <= (self.model.max_dpi or 20000)]
                b = Gtk.Button(label=label)
                b.connect("clicked", self._preset, vals)
                box.append(b)
            presets.add(box)
            page.add(presets)
        elif self.has_dpi:
            g = Adw.PreferencesGroup(title="Sensitivity")
            self.dpi_row = Adw.SpinRow.new_with_range(self.model.min_dpi, self.model.max_dpi or 20000, 50)
            self.dpi_row.set_title("DPI")
            self.dpi_row.connect("notify::value", lambda *_: None if self.loading else self.dpi_apply())
            g.add(self.dpi_row)
            page.add(g)

        if self.model.polling_rates:
            poll = Adw.PreferencesGroup(title="Report Rate")
            self.poll_row = Adw.ComboRow(
                title="Polling rate",
                subtitle="Higher is smoother but uses more CPU and battery",
                model=Gtk.StringList.new([f"{hz} Hz" for hz in self.model.polling_rates]),
            )
            self.poll_row.connect("notify::selected", self._poll_changed)
            poll.add(self.poll_row)
            page.add(poll)
        return page

    def _fill_lighting(self, zones: list[Zone]):
        self.lighting = []
        if not zones:
            self.lighting_stack.set_visible_child_name("none")
            return
        page = Adw.PreferencesPage()
        for z in zones:
            s = LightingSection(self, z)
            self.lighting.append(s)
            page.add(s.group)
        page.add(Adw.PreferencesGroup(description="Effects are stored on the device and persist without this app running."))
        old = self.lighting_stack.get_child_by_name("zones")
        if old:
            self.lighting_stack.remove(old)
        self.lighting_stack.add_named(page, "zones")
        self.lighting_stack.set_visible_child_name("zones")

    def _build_power_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Battery Saving")
        f = self.model.features
        self.idle_row = Adw.SpinRow.new_with_range(1, 15, 1)
        self.idle_row.set_title("Sleep after")
        self.idle_row.set_subtitle("Minutes of inactivity before the device sleeps")
        self.idle_row.connect("notify::value", self._power_changed)
        self.idle_row.set_visible(bool(f.get("idle")))
        g.add(self.idle_row)
        self.lowbat_row = Adw.SpinRow.new_with_range(5, 30, 1)
        self.lowbat_row.set_title("Low battery warning")
        self.lowbat_row.set_subtitle("Lighting blinks red below this percentage")
        self.lowbat_row.connect("notify::value", self._power_changed)
        self.lowbat_row.set_visible(bool(f.get("low_battery")))
        g.add(self.lowbat_row)
        page.add(g)
        return page

    # ---- DPI -------------------------------------------------------------

    def _rebuild_stage_rows(self):
        self.loading = True
        while (row := self.stage_list.get_row_at_index(0)) is not None:
            self.stage_list.remove(row)
        group = None
        for i, val in enumerate(self.stages):
            row = Adw.SpinRow.new_with_range(self.model.min_dpi, self.model.max_dpi or 20000, 50)
            row.set_title(f"Stage {i + 1}")
            row.set_value(val)
            radio = Gtk.CheckButton(valign=Gtk.Align.CENTER, tooltip_text="Make active")
            if group:
                radio.set_group(group)
            else:
                group = radio
            radio.set_active(i + 1 == self.active_stage)
            radio.connect("toggled", self._stage_radio, i)
            row.add_prefix(radio)
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Remove stage")
            rm.add_css_class("flat")
            rm.set_sensitive(len(self.stages) > 1)
            rm.connect("clicked", self._remove_stage, i)
            row.add_suffix(rm)
            row.connect("notify::value", self._stage_value, i)
            self.stage_list.append(row)
        self.stage_add_btn.set_sensitive(len(self.stages) < 5)
        self.loading = False
        self._update_dpi_now()

    def _update_dpi_now(self):
        if self.dpi_now_row and self.stages and 1 <= self.active_stage <= len(self.stages):
            self.dpi_now_row.set_subtitle(f"{self.stages[self.active_stage - 1]} (stage {self.active_stage})")

    def _stage_value(self, row, _pspec, i):
        if self.loading:
            return
        self.stages[i] = int(row.get_value())
        self._update_dpi_now()
        self.stage_apply()

    def _stage_radio(self, radio, i):
        if self.loading or not radio.get_active():
            return
        self.active_stage = i + 1
        self._update_dpi_now()
        self.stage_apply()

    def _add_stage(self, *_):
        if len(self.stages) >= 5:
            return
        self.stages.append(min(self.model.max_dpi or 20000, (self.stages[-1] * 2) if self.stages else 800))
        self._rebuild_stage_rows()
        self.stage_apply()

    def _remove_stage(self, _btn, i):
        if len(self.stages) <= 1:
            return
        del self.stages[i]
        if self.active_stage > len(self.stages) or self.active_stage == i + 1:
            self.active_stage = max(1, min(self.active_stage, len(self.stages)))
        elif self.active_stage > i + 1:
            self.active_stage -= 1
        self._rebuild_stage_rows()
        self.stage_apply()

    def _preset(self, _btn, vals):
        if not vals:
            return
        self.stages = list(vals)
        self.active_stage = min(2, len(vals))
        self._rebuild_stage_rows()
        self.stage_apply()

    def _apply_stages(self):
        active, stages, dev = self.active_stage, [(v, v) for v in self.stages], self.dev
        self.run(lambda: dev.set_dpi_stages(active, stages))

    def _apply_single_dpi(self):
        v, dev = int(self.dpi_row.get_value()), self.dev
        if self.dpi_now_row:
            self.dpi_now_row.set_subtitle(str(v))
        self.run(lambda: dev.set_dpi(v))

    # ---- polling / power -------------------------------------------------

    def _poll_changed(self, *_):
        if self.loading:
            return
        hz, dev = self.model.polling_rates[self.poll_row.get_selected()], self.dev
        self.run(lambda: dev.set_polling_rate(hz), lambda _r: self.toast(f"Polling rate set to {hz} Hz"))

    def _power_changed(self, *_):
        if not self.loading:
            self.power_apply()

    def _apply_power(self):
        idle, low, dev, f = int(self.idle_row.get_value()) * 60, int(self.lowbat_row.get_value()), self.dev, self.model.features

        def work():
            if f.get("idle"):
                dev.set_idle_time(idle)
            if f.get("low_battery"):
                dev.set_low_battery_threshold(low)

        self.run(work)

    # ---- reading state ---------------------------------------------------

    def refresh_all(self):
        dev, f = self.dev, self.model.features
        need_zones = self.lighting_page_added and dev.zones is None

        def work():
            out = {"errors": []}

            def get(key, fn):
                if "asleep" in out:
                    return
                try:
                    out[key] = fn()
                except DeviceAsleep as e:
                    out["asleep"] = str(e)
                except RazerError as e:
                    out["errors"].append(f"{key}: {e}")

            get("firmware", dev.firmware)
            get("serial", dev.serial)
            if self.has_stages:
                get("stages", dev.dpi_stages)
            elif self.has_dpi:
                get("dpi", dev.dpi)
            if self.model.polling_rates:
                get("poll", dev.polling_rate)
            if f.get("battery"):
                get("battery", dev.battery)
            if f.get("charging"):
                get("charging", dev.charging)
            if f.get("idle"):
                get("idle", dev.idle_time)
            if f.get("low_battery"):
                get("lowbat", dev.low_battery_threshold)
            if self.lighting_page_added:
                get("zones", dev.probe_zones)
                for z in dev.zones or []:
                    if z.has_brightness:
                        get(f"bright:{z.key}", z.get_brightness)
            return out

        self.run(work, lambda out: self._apply_all(out, need_zones))
        if self.buttons_page:
            self.buttons_page.load()

    def _apply_all(self, out: dict, build_zones: bool = False):
        self.loading = True
        try:
            if "asleep" in out:
                self.batt_label.set_label("Asleep")
                self.batt_icon.set_from_icon_name("weather-clear-night-symbolic")
                self.batt_sub.set_label("Move or press the device to wake it")
                if not self.asleep_retry:
                    self.asleep_retry = GLib.timeout_add_seconds(5, self._asleep_retry)
            if "firmware" in out:
                self.fw_row.set_subtitle(out["firmware"])
            if "serial" in out:
                self.serial_row.set_subtitle(out["serial"] or "—")
            if "stages" in out and not self.stage_apply.pending:
                self.active_stage, st = out["stages"]
                self.stages = [x for x, _y in st] or [800]
                self.active_stage = max(1, min(self.active_stage, len(self.stages)))
                self._rebuild_stage_rows()
                self.loading = True
            if "dpi" in out and not self.dpi_apply.pending:
                self.dpi_row.set_value(out["dpi"][0])
                if self.dpi_now_row:
                    self.dpi_now_row.set_subtitle(str(out["dpi"][0]))
            if "poll" in out and out["poll"] in self.model.polling_rates:
                self.poll_row.set_selected(self.model.polling_rates.index(out["poll"]))
            self._apply_battery(out)
            if "idle" in out:
                self.idle_row.set_value(max(1, min(15, round(out["idle"] / 60))))
            if "lowbat" in out:
                self.lowbat_row.set_value(max(5, min(30, out["lowbat"])))
            if "zones" in out and (build_zones or not self.lighting):
                self._fill_lighting(out["zones"])
            for s in self.lighting:
                if f"bright:{s.zone.key}" in out:
                    s.set_brightness(out[f"bright:{s.zone.key}"])
        finally:
            self.loading = False
        for e in out["errors"]:
            print(f"razer-control [{self.dev.name}]:", e, file=sys.stderr)

    def _asleep_retry(self):
        self.asleep_retry = 0
        if self.win.current is self:
            self.refresh_all()
        return GLib.SOURCE_REMOVE

    def refresh_status(self):
        dev, f = self.dev, self.model.features
        if not (f.get("battery") or self.has_stages):
            return

        def work():
            out = {"errors": []}
            try:
                if f.get("battery"):
                    out["battery"] = dev.battery()
                if f.get("charging"):
                    out["charging"] = dev.charging()
                if self.has_stages:
                    out["stages"] = dev.dpi_stages()
            except DeviceAsleep as e:
                out["asleep"] = str(e)
            except RazerError as e:
                out["errors"].append(str(e))
            return out

        def done(out):
            if "asleep" not in out:
                self._apply_all(out)

        self.run(work, done)

    def _apply_battery(self, out: dict):
        if "battery" not in out:
            return
        lvl, chg = out["battery"], out.get("charging", False)
        self.batt_label.set_label(f"{lvl}%")
        self.batt_bar.set_value(lvl)
        self.batt_icon.set_from_icon_name(battery_icon(lvl, chg))
        if chg:
            self.batt_sub.set_label("Charging" if lvl < 100 else "Fully charged")
        else:
            self.batt_sub.set_label("On battery" + (" — charge soon" if lvl <= 15 else ""))


# ---------------------------------------------------------------------------
# main window


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Razer Control")
        self.set_default_size(820, 760)
        self.set_size_request(360, 420)
        self.settings = load_settings()
        self.worker = Worker()
        self.devices: list[RazerDevice] = []
        self.views: dict[int, DeviceView] = {}
        self.current: DeviceView | None = None
        self.discover_src = 0
        self.battery_src = 0
        self.discovering = False

        self.toasts = Adw.ToastOverlay()
        self.toolbar = Adw.ToolbarView()
        self.toasts.set_child(self.toolbar)
        self.set_content(self.toasts)

        self.header = Adw.HeaderBar()
        self.toolbar.add_top_bar(self.header)
        self.banner = Adw.Banner(revealed=False)
        self.toolbar.add_top_bar(self.banner)

        self.device_model = Gtk.StringList()
        self.device_picker = Gtk.DropDown(model=self.device_model, visible=False, tooltip_text="Choose device")
        self.device_picker.set_factory(self._label_factory(ellipsize=True))
        self.device_picker.set_list_factory(self._label_factory(ellipsize=False))
        self.device_picker.connect("notify::selected", self._on_pick)
        self.header.pack_start(self.device_picker)

        menu = Gio.Menu()
        menu.append("Supported Devices", "app.supported")
        menu.append("About Razer Control", "app.about")
        menu.append("Quit", "app.quit")
        self.header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main menu"))
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan devices")
        self.refresh_btn.connect("clicked", lambda *_: self.discover())
        self.header.pack_end(self.refresh_btn)

        self.switcher = Adw.ViewSwitcher(policy=Adw.ViewSwitcherPolicy.WIDE)
        self.switcher_bar = Adw.ViewSwitcherBar()
        self.toolbar.add_bottom_bar(self.switcher_bar)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 780sp"))
        bp.add_setter(self.switcher_bar, "reveal", True)
        bp.add_setter(self.switcher, "visible", False)
        self.add_breakpoint(bp)

        self.outer = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toolbar.set_content(self.outer)
        self.outer.add_named(Adw.StatusPage(title="Looking for Razer devices…", paintable=Adw.SpinnerPaintable()), "searching")
        self.missing_page = Adw.StatusPage(
            icon_name="input-mouse-symbolic",
            title="No Razer Device Found",
            description="Plug in a Razer mouse, keyboard or accessory. Searching…",
        )
        self.outer.add_named(self.missing_page, "missing")
        self.perm_page = Adw.StatusPage(
            icon_name="dialog-password-symbolic",
            title="Permission Needed",
            description=(
                "Razer Control needs access to your Razer devices.\n"
                "Run this once in a terminal, then replug the device:\n\n"
                f"<tt>sudo install -m644 {GLib.markup_escape_text(UDEV_RULE_SRC)} /etc/udev/rules.d/ "
                "&amp;&amp; sudo udevadm control --reload &amp;&amp; sudo udevadm trigger</tt>"
            ),
        )
        self.outer.add_named(self.perm_page, "permission")
        self.view_holder = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.outer.add_named(self.view_holder, "main")

        self.connect("notify::is-active", self._on_focus)
        self.connect("close-request", self._on_close)
        self.discover()

    @staticmethod
    def _label_factory(ellipsize: bool) -> Gtk.SignalListItemFactory:
        from gi.repository import Pango

        f = Gtk.SignalListItemFactory()

        def setup(_f, item):
            lbl = Gtk.Label(xalign=0)
            if ellipsize:
                lbl.set_ellipsize(Pango.EllipsizeMode.END)
                lbl.set_max_width_chars(16)
            item.set_child(lbl)

        def bind(_f, item):
            text = item.get_item().get_string()
            item.get_child().set_label(text.removeprefix("Razer ") if ellipsize else text)

        f.connect("setup", setup)
        f.connect("bind", bind)
        return f

    # ---- plumbing --------------------------------------------------------

    def persist(self):
        try:
            save_settings(self.settings)
        except OSError as e:
            self.toast(f"Could not save settings: {e}")

    def toast(self, msg: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg), timeout=3))

    def run(self, fn, done=None):
        self.worker.submit(fn, done, self.on_error)

    def on_error(self, e: Exception):
        if isinstance(e, RazerError):  # includes DeviceAsleep
            self.toast(str(e))
        elif isinstance(e, OSError):
            self.toast("Lost connection to a device")
            self.discover()
        else:
            self.toast(f"Error: {e}")
            raise e

    def _on_focus(self, *_):
        if self.is_active() and self.current and not self.current.stage_apply.pending:
            self.current.refresh_status()

    def _on_close(self, *_):
        for d in self.devices:
            d.close()
        return False

    # ---- discovery -------------------------------------------------------

    def discover(self):
        if self.discovering:
            return
        self.discovering = True
        if not self.devices:
            self.outer.set_visible_child_name("searching")
        old = list(self.devices)

        def work():
            for d in old:
                d.close()
            return open_devices()

        def done(res):
            self.discovering = False
            self._discovered(*res)

        def fail(e):
            self.discovering = False
            self.on_error(e)

        self.worker.submit(work, done, fail)

    def _discovered(self, devices, errors):
        prev_pid = self.current.dev.pid if self.current else None
        self.devices = devices
        for view in list(self.views.values()):
            self.view_holder.remove(view.stack)
        self.views, self.current = {}, None
        if self.battery_src:
            GLib.source_remove(self.battery_src)
            self.battery_src = 0

        unsupported = list(dict.fromkeys(e.split(":", 2)[1] for e in errors if e.startswith("unsupported:")))
        for e in errors:
            print("razer-control:", e, file=sys.stderr)
        self.banner.set_title(f"Detected but not controllable: {', '.join(unsupported)}" if unsupported else "")
        self.banner.set_revealed(bool(unsupported and devices))

        if not devices:
            self.device_picker.set_visible(False)
            self.header.set_title_widget(None)
            if any(e.startswith("permission:") for e in errors):
                self.outer.set_visible_child_name("permission")
            else:
                desc = "Plug in a Razer mouse, keyboard or accessory. Searching…"
                if unsupported:
                    desc += "\n\nDetected but not controllable: " + GLib.markup_escape_text(", ".join(unsupported))
                self.missing_page.set_description(desc)
                self.outer.set_visible_child_name("missing")
            if not self.discover_src:
                self.discover_src = GLib.timeout_add_seconds(3, self._rediscover)
            return
        if self.discover_src:
            GLib.source_remove(self.discover_src)
            self.discover_src = 0

        self.device_model.splice(0, self.device_model.get_n_items(), [d.name for d in devices])
        self.device_picker.set_visible(len(devices) > 1)
        idx = next((i for i, d in enumerate(devices) if d.pid == prev_pid), 0)
        self.outer.set_visible_child_name("main")
        self.header.set_title_widget(self.switcher)
        self.device_picker.set_selected(idx)
        self._select(idx)
        self.battery_src = GLib.timeout_add_seconds(BATTERY_REFRESH_S, self._battery_tick)

    def _rediscover(self):
        self.discover_src = 0
        self.discover()
        return GLib.SOURCE_REMOVE

    def _battery_tick(self):
        if self.current:
            self.current.refresh_status()
        return GLib.SOURCE_CONTINUE

    def _on_pick(self, *_):
        idx = self.device_picker.get_selected()
        if 0 <= idx < len(self.devices) and (not self.current or self.current.dev is not self.devices[idx]):
            self._select(idx)

    def _select(self, idx: int):
        dev = self.devices[idx]
        view = self.views.get(idx)
        fresh = view is None
        if fresh:
            view = DeviceView(self, dev)
            self.views[idx] = view
            self.view_holder.add_named(view.stack, f"dev{idx}")
        self.current = view
        self.view_holder.set_visible_child(view.stack)
        self.switcher.set_stack(view.stack)
        self.switcher_bar.set_stack(view.stack)
        if fresh:
            view.refresh_all()
        else:
            view.refresh_status()


class SupportedDialog(Adw.Dialog):
    def __init__(self):
        super().__init__(title="Supported Devices", content_width=520, content_height=640)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        search = Gtk.SearchEntry(placeholder_text="Search devices", margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        tv.add_top_bar(search)
        db = device_db()
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, margin_start=12, margin_end=12, margin_bottom=12)
        lb.add_css_class("boxed-list")
        self.search_text = {}
        for pid, e in sorted(db.items(), key=lambda kv: (kv[1]["type"], kv[1]["name"])):
            sub = f"{KIND_LABELS.get(e['type'], e['type'])} · 1532:{pid:04x}"
            if e.get("protocol") != "razer":
                sub += " · not supported (headset protocol)"
            r = Adw.ActionRow(title=GLib.markup_escape_text(e["name"]), subtitle=sub)
            r.add_prefix(Gtk.Image(icon_name=KIND_ICONS.get(e["type"], "input-mouse-symbolic")))
            self.search_text[r] = f"{e['name']} {e['type']} {pid:04x}".lower()
            lb.append(r)
        lb.set_filter_func(lambda row: search.get_text().lower() in self.search_text.get(row, ""))
        search.connect("search-changed", lambda *_: lb.invalidate_filter())
        supported = sum(1 for e in db.values() if e.get("protocol") == "razer")
        head = Gtk.Label(label=f"{supported} supported models · device data from the openrazer project",
                         margin_top=6, margin_bottom=6, wrap=True)
        head.add_css_class("dim-label")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(head)
        box.append(lb)
        tv.set_content(Gtk.ScrolledWindow(child=box, vexpand=True))
        self.set_child(tv)


class RazerControlApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.connect("activate", self._on_activate)
        for name, cb in (("about", self._about), ("supported", self._supported), ("quit", lambda *_: self.quit())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def _on_activate(self, *_):
        win = self.get_active_window() or MainWindow(self)
        win.present()

    def _supported(self, *_):
        SupportedDialog().present(self.get_active_window())

    def _about(self, *_):
        d = Adw.AboutDialog(
            application_name="Razer Control",
            application_icon="input-mouse-symbolic",
            developer_name="Arjun Vasu",
            website="https://github.com/voidrlm/razer-control",
            issue_url="https://github.com/voidrlm/razer-control/issues",
            version="2.0",
            comments="Configure DPI, polling rate, buttons, lighting and power settings for Razer devices — no kernel driver required.",
            license_type=Gtk.License.GPL_2_0,
        )
        d.add_credit_section("Device data", ["openrazer project (GPL-2.0)"])
        d.add_credit_section("Button protocol notes", ["OpenSnek"])
        d.present(self.get_active_window())


def main():
    return RazerControlApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
