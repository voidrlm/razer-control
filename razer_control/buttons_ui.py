"""Buttons tab: view and remap the mouse's buttons."""

from __future__ import annotations

from gi.repository import Adw, Gdk, GLib, Gtk

from .buttons import (
    EVDEV_MODIFIERS,
    EVDEV_TO_HID,
    KEYS,
    LOCKED_SLOTS,
    MODIFIERS,
    MOUSE_BUTTONS,
    MAIN_SLOTS,
    SIDE_SLOTS,
    SLOT_ORDER,
    Action,
    default_action,
    shortcut_label,
    slot_name,
)
from .protocol import RazerDevice, RazerError


KINDS = [
    ("mouse", "Mouse button"),
    ("key", "Keyboard key / shortcut"),
    ("dpi_cycle", "Cycle DPI stages"),
    ("dpi_clutch", "DPI clutch (sniper, while held)"),
    ("disabled", "Disabled"),
]


def _searchable(row: Adw.ComboRow):
    row.set_enable_search(True)
    row.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))


class ButtonDialog(Adw.Dialog):
    def __init__(self, slot: int, current: Action, on_save, on_reset, pid: int = 0):
        super().__init__(title=slot_name(slot, pid), content_width=440)
        self.slot, self.current, self.on_save, self.on_reset = slot, current, on_save, on_reset
        self.recording = False

        self.kinds = list(KINDS)
        if current.kind == "custom":
            self.kinds.insert(0, ("custom", f"Keep current — {current.describe()}"))

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label="Apply")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save)
        header.pack_end(save)
        tv.add_top_bar(header)
        page = Adw.PreferencesPage()
        tv.set_content(page)
        self.set_child(tv)

        g = Adw.PreferencesGroup()
        self.kind_row = Adw.ComboRow(title="Action", model=Gtk.StringList.new([n for _, n in self.kinds]))
        self.kind_row.set_selected(next((i for i, (k, _) in enumerate(self.kinds) if k == current.kind), 0))
        self.kind_row.connect("notify::selected", lambda *_: self._sync())
        g.add(self.kind_row)
        page.add(g)

        # mouse button
        self.mouse_group = Adw.PreferencesGroup()
        self.mouse_row = Adw.ComboRow(title="Acts as", model=Gtk.StringList.new([n for _, n in MOUSE_BUTTONS]))
        codes = [c for c, _ in MOUSE_BUTTONS]
        cur_btn = current.data[0] if current.kind == "mouse" else (default_action(slot, pid) or Action.mouse(4)).data[0]
        self.mouse_row.set_selected(codes.index(cur_btn) if cur_btn in codes else 0)
        self.mouse_group.add(self.mouse_row)
        page.add(self.mouse_group)

        # keyboard
        self.key_group = Adw.PreferencesGroup(
            description="Sent as a real keystroke from the mouse, so it works everywhere — even without this app."
        )
        self.record_row = Adw.ActionRow(title="Record shortcut", subtitle="Click, then press a key combination")
        self.record_btn = Gtk.Button(icon_name="media-record-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Record")
        self.record_btn.add_css_class("flat")
        self.record_btn.connect("clicked", self._toggle_record)
        self.record_row.add_suffix(self.record_btn)
        self.record_row.set_activatable_widget(self.record_btn)
        self.key_group.add(self.record_row)

        self.key_row = Adw.ComboRow(title="Key", model=Gtk.StringList.new([label for _, label, _ in KEYS]))
        _searchable(self.key_row)
        hids = [h for h, _, _ in KEYS]
        if current.kind == "key" and current.key_hid in hids:
            self.key_row.set_selected(hids.index(current.key_hid))
        self.key_row.connect("notify::selected", lambda *_: self._update_preview())
        self.key_group.add(self.key_row)

        self.mod_row = Adw.ActionRow(title="Modifiers")
        box = Gtk.Box(valign=Gtk.Align.CENTER)
        box.add_css_class("linked")
        self.mod_buttons = {}
        for bit, name in MODIFIERS:
            b = Gtk.ToggleButton(label=name)
            b.set_active(current.kind == "key" and bool(current.key_mods & (bit | bit << 4)))
            b.connect("toggled", lambda *_: self._update_preview())
            box.append(b)
            self.mod_buttons[bit] = b
        self.mod_row.add_suffix(box)
        self.key_group.add(self.mod_row)
        page.add(self.key_group)

        # clutch
        self.clutch_group = Adw.PreferencesGroup(description="Temporarily switches to this DPI while the button is held.")
        self.clutch_row = Adw.SpinRow.new_with_range(100, 20000, 50)
        self.clutch_row.set_title("DPI while held")
        self.clutch_row.set_value(current.clutch_dpi if current.kind == "dpi_clutch" else 400)
        self.clutch_group.add(self.clutch_row)
        page.add(self.clutch_group)

        # reset
        if default_action(slot, pid) is not None and not current.is_default:
            rg = Adw.PreferencesGroup()
            reset = Adw.ButtonRow(title=f"Restore default ({default_action(slot, pid).describe()})")
            reset.add_css_class("destructive-action")
            reset.connect("activated", self._reset)
            rg.add(reset)
            page.add(rg)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self._sync()

    # ---- state -----------------------------------------------------------

    def _kind(self) -> str:
        return self.kinds[self.kind_row.get_selected()][0]

    def _mods(self) -> int:
        return sum(bit for bit, b in self.mod_buttons.items() if b.get_active())

    def _sync(self):
        k = self._kind()
        self.mouse_group.set_visible(k == "mouse")
        self.key_group.set_visible(k == "key")
        self.clutch_group.set_visible(k == "dpi_clutch")
        if k != "key" and self.recording:
            self._toggle_record()
        self._update_preview()

    def _update_preview(self):
        if not self.recording:
            hid = KEYS[self.key_row.get_selected()][0]
            self.record_row.set_subtitle(shortcut_label(hid, self._mods()))

    def _toggle_record(self, *_):
        self.recording = not self.recording
        if self.recording:
            self.record_row.set_subtitle("Press a key combination… (Esc to cancel)")
            self.record_btn.add_css_class("destructive-action")
            self.record_btn.remove_css_class("flat")
        else:
            self.record_btn.remove_css_class("destructive-action")
            self.record_btn.add_css_class("flat")
            self._update_preview()

    def _on_key(self, _ctl, _keyval, keycode, state):
        if not self.recording:
            return False
        ev = keycode - 8
        if ev in EVDEV_MODIFIERS:
            return True  # wait for the non-modifier key
        mods = 0
        if state & Gdk.ModifierType.CONTROL_MASK:
            mods |= 0x01
        if state & Gdk.ModifierType.SHIFT_MASK:
            mods |= 0x02
        if state & Gdk.ModifierType.ALT_MASK:
            mods |= 0x04
        if state & Gdk.ModifierType.SUPER_MASK:
            mods |= 0x08
        if ev == 1 and not mods:  # Esc cancels
            self._toggle_record()
            return True
        hid = EVDEV_TO_HID.get(ev)
        if hid is None:
            self.record_row.set_subtitle("That key isn't supported — pick one from the list")
            return True
        self.key_row.set_selected([h for h, _, _ in KEYS].index(hid))
        for bit, b in self.mod_buttons.items():
            b.set_active(bool(mods & bit))
        self._toggle_record()
        return True

    def _build(self) -> Action | None:
        k = self._kind()
        if k == "custom":
            return None
        if k == "mouse":
            return Action.mouse(MOUSE_BUTTONS[self.mouse_row.get_selected()][0])
        if k == "key":
            return Action.key(KEYS[self.key_row.get_selected()][0], self._mods())
        if k == "dpi_cycle":
            return Action.dpi_cycle()
        if k == "dpi_clutch":
            return Action.dpi_clutch(int(self.clutch_row.get_value()))
        return Action.disabled()

    def _save(self, *_):
        act = self._build()
        if act is not None:
            self.on_save(act)
        self.close()

    def _reset(self, *_):
        self.on_reset()
        self.close()


class ButtonsPage:
    def __init__(self, win, dev: RazerDevice):
        self.win, self.dev = win, dev
        self.actions: dict[int, Action] = {}
        self.rows: dict[int, Adw.ActionRow] = {}
        self.groups: list[Adw.PreferencesGroup] = []
        self.widget = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        spinner = Adw.StatusPage(title="Reading buttons…", paintable=Adw.SpinnerPaintable())
        self.widget.add_named(spinner, "loading")
        self.widget.add_named(
            Adw.StatusPage(
                icon_name="input-mouse-symbolic",
                title="Remapping Not Available",
                description="This device doesn't support onboard button remapping, or its firmware doesn't report its buttons.",
            ),
            "unavailable",
        )
        self.page = Adw.PreferencesPage()
        self.widget.add_named(self.page, "buttons")

    def _parent(self):
        return getattr(self.win, "win", self.win)  # DeviceView -> its window

    def name(self, slot: int) -> str:
        return slot_name(slot, self.dev.pid)

    # ---- device I/O -------------------------------------------------------

    def load(self):
        dev = self.dev

        def work():
            try:
                slots = dev.button_slots()
            except RazerError:
                return None
            out = {}
            for s in slots:
                try:
                    out[s] = dev.get_button(s)
                except RazerError:
                    continue  # some slots are read-only or hidden
            return out

        self.win.run(work, self._loaded)

    def _build_rows(self, slots):
        for g in self.groups:
            self.page.remove(g)
        self.groups, self.rows = [], {}
        order = {s: i for i, s in enumerate(SLOT_ORDER)}
        slots = sorted(slots, key=lambda s: order.get(s, 1000 + s))
        sections = [
            ("Main Buttons", [s for s in slots if s in MAIN_SLOTS]),
            ("Side Buttons", [s for s in slots if s in SIDE_SLOTS]),
            ("Other Buttons", [s for s in slots if s not in MAIN_SLOTS and s not in SIDE_SLOTS]),
        ]
        for title, members in sections:
            if not members:
                continue
            g = Adw.PreferencesGroup(title=title)
            if title == "Side Buttons":
                g.set_description("Mappings are stored on the mouse and work on any computer.")
            for slot in members:
                row = Adw.ActionRow(title=self.name(slot), subtitle="…")
                if slot in LOCKED_SLOTS:
                    row.set_subtitle("Left click (can't be remapped)")
                    lock = Gtk.Image(icon_name="changes-prevent-symbolic")
                    lock.add_css_class("dim-label")
                    row.add_suffix(lock)
                else:
                    row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                    row.set_activatable(True)
                    row.connect("activated", self._edit, slot)
                g.add(row)
                self.rows[slot] = row
            self.page.add(g)
            self.groups.append(g)
        rg = Adw.PreferencesGroup()
        reset = Adw.ButtonRow(title="Restore All Defaults")
        reset.add_css_class("destructive-action")
        reset.connect("activated", self._confirm_reset_all)
        rg.add(reset)
        self.page.add(rg)
        self.groups.append(rg)

    def _loaded(self, actions):
        if not actions:
            self.widget.set_visible_child_name("unavailable")
            return
        if set(actions) != set(self.rows):
            self._build_rows(list(actions))
        self.actions = actions
        for slot, row in self.rows.items():
            act = actions.get(slot)
            if act is None or slot in LOCKED_SLOTS:
                continue
            row.set_subtitle(act.describe() + ("" if act.is_default else "  ·  customized"))
        self.widget.set_visible_child_name("buttons")

    def _apply(self, slot: int, act: Action):
        dev = self.dev

        def work():
            dev.set_button(slot, act)
            return dev.get_button(slot)

        def done(readback: Action):
            self.actions[slot] = readback
            self._loaded(self.actions)
            self.win.toast(f"{self.name(slot)}: {readback.describe()}")

        self.win.run(work, done)

    def _reset_slot(self, slot: int):
        dev = self.dev

        def work():
            dev.reset_button(slot)
            return dev.get_button(slot)

        def done(readback):
            self.actions[slot] = readback
            self._loaded(self.actions)
            self.win.toast(f"{self.name(slot)} restored to default")

        self.win.run(work, done)

    # ---- UI --------------------------------------------------------------

    def _edit(self, _row, slot):
        cur = self.actions.get(slot)
        if cur is None:
            return
        dlg = ButtonDialog(slot, cur, lambda act: self._apply(slot, act), lambda: self._reset_slot(slot), self.dev.pid)
        dlg.present(self._parent())

    def _resettable(self):
        return [s for s in self.actions if s not in LOCKED_SLOTS and default_action(s, self.dev.pid) is not None]

    def _confirm_reset_all(self, *_):
        custom = [self.name(s) for s in self._resettable() if not self.actions[s].is_default]
        body = "Every button with a known factory function goes back to it."
        if custom:
            body += "\n\nCustomized now: " + ", ".join(custom) + "."
        d = Adw.AlertDialog(heading="Restore all button defaults?", body=body)
        d.add_response("cancel", "Cancel")
        d.add_response("reset", "Restore")
        d.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        d.connect("response", lambda _d, r: r == "reset" and self._reset_all())
        d.present(self._parent())

    def _reset_all(self):
        dev = self.dev
        slots = self._resettable()
        all_slots = list(self.actions)

        def work():
            for s in slots:
                dev.reset_button(s)
            return {s: dev.get_button(s) for s in all_slots}

        def done(actions):
            self._loaded(actions)
            self.win.toast("All buttons restored to defaults")

        self.win.run(work, done)
