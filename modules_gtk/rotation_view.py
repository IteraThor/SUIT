import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from core.logger import get_logger
from core.display_service import DisplayService, normalize_rotation
from modules_gtk.async_utils import run_async

logger = get_logger("rotation_view")

class RotationView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Screen & Touch Manager", tag="rotation")
        self.window = window

        self.staged_rotations = {}
        self.staged_touch = {}
        self.live_rotations = {}
        self.live_touch = {}

        self._confirm_dialog = None
        self._confirm_timer_id = None
        self._remaining_seconds = 15

        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_kinetic_scrolling(True)
        self.set_child(scrolled)

        clamp = Adw.Clamp(maximum_size=880, tightening_threshold=660)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)
        scrolled.set_child(clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        clamp.set_child(self.main_box)

        # Header Info Card
        info_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        info_card.add_css_class("tile-card")
        self.main_box.append(info_card)

        icon_img = Gtk.Image.new_from_icon_name("video-display-symbolic")
        icon_img.set_pixel_size(36)
        icon_img.set_valign(Gtk.Align.CENTER)
        info_card.append(icon_img)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_box.set_hexpand(True)
        info_card.append(text_box)

        lbl_h = Gtk.Label(label="Display & Touch", xalign=0)
        lbl_h.add_css_class("title-3")
        text_box.append(lbl_h)

        lbl_desc = Gtk.Label(
            label="Rotate connected Wayland monitors and calibrate touchscreen alignment.",
            xalign=0,
            wrap=True
        )
        lbl_desc.add_css_class("dim-label")
        text_box.append(lbl_desc)

        # Warning Notice Banner
        warning_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        warning_card.add_css_class("warning-card")
        self.main_box.append(warning_card)

        warn_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        warn_icon.set_pixel_size(24)
        warn_icon.set_valign(Gtk.Align.CENTER)
        warning_card.append(warn_icon)

        warn_lbl = Gtk.Label(
            label="Only use this tool if your device has issues rotating touch input using default system settings.",
            xalign=0,
            wrap=True
        )
        warn_lbl.add_css_class("warning-text")
        warn_lbl.set_hexpand(True)
        warn_lbl.set_valign(Gtk.Align.CENTER)
        warning_card.append(warn_lbl)

        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions_box.set_valign(Gtk.Align.CENTER)

        self.btn_clear_config = Gtk.Button()
        self.btn_clear_config.add_css_class("secondary-btn")
        self.btn_clear_config.set_valign(Gtk.Align.CENTER)
        self.btn_clear_config.set_tooltip_text("Clear SUIT rotation overrides and remove boot autostart")
        clear_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        clear_content.append(Gtk.Image.new_from_icon_name("edit-clear-symbolic"))
        clear_content.append(Gtk.Label(label="Clear Config"))
        self.btn_clear_config.set_child(clear_content)
        self.btn_clear_config.connect("clicked", self._on_clear_config_clicked)
        actions_box.append(self.btn_clear_config)

        self.btn_default_settings = Gtk.Button()
        self.btn_default_settings.add_css_class("secondary-btn")
        self.btn_default_settings.set_valign(Gtk.Align.CENTER)
        self.btn_default_settings.set_tooltip_text("Open GNOME Display Settings")

        btn_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_content.append(Gtk.Image.new_from_icon_name("preferences-desktop-display-symbolic"))
        btn_content.append(Gtk.Label(label="Display Settings"))
        self.btn_default_settings.set_child(btn_content)
        self.btn_default_settings.connect("clicked", lambda b: DisplayService.open_display_settings())
        actions_box.append(self.btn_default_settings)

        warning_card.append(actions_box)

        # Dynamic Monitors Group
        self.monitors_group = Adw.PreferencesGroup(title="Connected Displays")
        self.monitors_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.monitors_group.add(self.monitors_container)
        self.main_box.append(self.monitors_group)

        self.connect("map", lambda w: self.refresh())
        self.connect("unmap", lambda w: self._cleanup_confirmation())

    def refresh(self):
        def worker():
            mons = DisplayService.get_monitors()
            touch = DisplayService.get_touchscreens()
            cfg = DisplayService.load_config()
            return {"monitors": mons, "touchscreens": touch, "config": cfg}

        def on_done(res):
            self._render_monitors(res["monitors"], res["touchscreens"], res["config"])

        run_async(worker, on_done=on_done)

    def _render_monitors(self, monitors, touchscreens, config):
        while child := self.monitors_container.get_first_child():
            self.monitors_container.remove(child)

        if not monitors:
            empty_row = Adw.ActionRow(
                title="No monitors detected via GNOME Mutter",
                subtitle="Check display connections."
            )
            self.monitors_container.append(empty_row)
            return

        rot_names = {0: "Normal (0°)", 1: "Right (90°)", 2: "Inverted (180°)", 3: "Left (270°)"}

        for mon in monitors:
            name = mon["name"]
            res = mon["res"]
            live_rot = mon["rotation"]
            is_pri = mon["is_primary"]

            saved_cfg = config.get(name, {})
            saved_rot = normalize_rotation(saved_cfg.get("rotation", live_rot))
            saved_touch = saved_cfg.get("touch_device", "None")

            self.live_rotations[name] = live_rot
            self.live_touch[name] = saved_touch

            # Initialize staged state if not present
            if name not in self.staged_rotations:
                self.staged_rotations[name] = saved_rot
            if name not in self.staged_touch:
                # Auto-select the only touchscreen when none is configured yet.
                # Prevents "screen rotated but touch didn't" when the user hasn't
                # manually assigned a device before hitting Apply.
                if saved_touch in ("None", "", None) and len(touchscreens) == 1:
                    self.staged_touch[name] = touchscreens[0]
                else:
                    self.staged_touch[name] = saved_touch

            current_staged_rot = self.staged_rotations[name]

            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            card.add_css_class("tile-card")
            card.set_margin_bottom(8)

            # 1. Header: Display Name, Badges, and Current Rotation Status Pill
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            card.append(header)

            icon = Gtk.Image.new_from_icon_name("video-display-symbolic")
            icon.set_pixel_size(24)
            header.append(icon)

            title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            title_box.set_hexpand(True)
            header.append(title_box)

            lbl_t = Gtk.Label(label=f"{name} ({res})", xalign=0)
            lbl_t.add_css_class("heading")
            title_box.append(lbl_t)

            if is_pri:
                pri_pill = Gtk.Label(label="Primary")
                pri_pill.add_css_class("status-pill-checking")
                title_box.append(pri_pill)

            pill = Gtk.Label(label=rot_names.get(live_rot, "Unknown"))
            pill.add_css_class("status-pill-checking")
            header.append(pill)

            # 2. Orientation Selector: 4 Touch-friendly Buttons
            angle_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.append(angle_section)

            lbl_angle = Gtk.Label(label="Target Orientation", xalign=0)
            lbl_angle.add_css_class("dim-label")
            angle_section.append(lbl_angle)

            btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn_box.set_homogeneous(True)
            angle_section.append(btn_box)

            options = [
                (0, "0° Normal"),
                (1, "90° Right"),
                (2, "180° Inverted"),
                (3, "270° Left")
            ]

            angle_buttons = []

            def make_angle_handler(btn_target_rot):
                def handler(b):
                    self.staged_rotations[name] = btn_target_rot
                    for b_rot, b_obj in angle_buttons:
                        if b_rot == btn_target_rot:
                            b_obj.add_css_class("suggested-action")
                        else:
                            b_obj.remove_css_class("suggested-action")
                return handler

            for r_val, r_label in options:
                btn = Gtk.Button(label=r_label)
                btn.add_css_class("touch-btn")
                btn.add_css_class("angle-touch-btn")
                btn.set_size_request(-1, 52)
                if r_val == current_staged_rot:
                    btn.add_css_class("suggested-action")
                btn.connect("clicked", make_angle_handler(r_val))
                btn_box.append(btn)
                angle_buttons.append((r_val, btn))

            # 3. Touchscreen Assignment Row
            touch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            touch_box.set_valign(Gtk.Align.CENTER)
            card.append(touch_box)

            touch_icon = Gtk.Image.new_from_icon_name("input-tablet-symbolic")
            touch_icon.set_pixel_size(22)
            touch_box.append(touch_icon)

            touch_lbl = Gtk.Label(label="Assigned Touchscreen", xalign=0)
            touch_lbl.add_css_class("body")
            touch_lbl.set_hexpand(True)
            touch_box.append(touch_lbl)

            touch_options = ["None"] + touchscreens
            touch_dd = Gtk.DropDown.new_from_strings(touch_options)
            touch_dd.set_valign(Gtk.Align.CENTER)
            touch_dd.set_size_request(260, 46)

            # Pre-select active/staged touch device
            staged_dev = self.staged_touch.get(name, "None")
            if staged_dev in touch_options:
                touch_dd.set_selected(touch_options.index(staged_dev))
            else:
                touch_dd.set_selected(0)

            def make_touch_handler(dd_widget, conn_name, opts):
                def handler(*args):
                    idx = dd_widget.get_selected()
                    chosen = opts[idx] if idx < len(opts) else "None"
                    self.staged_touch[conn_name] = chosen
                return handler

            touch_dd.connect("notify::selected", make_touch_handler(touch_dd, name, touch_options))
            touch_box.append(touch_dd)

            # 4. Action Row: Apply Changes & Calibrate Touch
            action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            action_box.set_margin_top(4)
            card.append(action_box)

            # Apply Button (Prominent, Suggested Action)
            btn_apply = Gtk.Button()
            btn_apply.add_css_class("touch-btn")
            btn_apply.add_css_class("suggested-action")
            btn_apply.set_size_request(-1, 52)
            btn_apply.set_hexpand(True)

            apply_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            apply_content.set_halign(Gtk.Align.CENTER)
            apply_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
            apply_icon.set_pixel_size(20)
            apply_content.append(apply_icon)
            apply_lbl = Gtk.Label(label="Apply Changes")
            apply_content.append(apply_lbl)
            btn_apply.set_child(apply_content)

            btn_apply.connect("clicked", lambda b, c=name: self._apply_settings(c))
            action_box.append(btn_apply)

            self.monitors_container.append(card)

    def _cleanup_timer(self):
        if hasattr(self, "_confirm_timer_id") and self._confirm_timer_id is not None:
            try:
                GLib.source_remove(self._confirm_timer_id)
            except Exception:
                pass
            self._confirm_timer_id = None

    def _cleanup_confirmation(self):
        self._cleanup_timer()
        if hasattr(self, "_confirm_dialog") and self._confirm_dialog is not None:
            dialog = self._confirm_dialog
            self._confirm_dialog = None
            if hasattr(self, "_confirm_response_handler_id") and self._confirm_response_handler_id:
                try:
                    dialog.disconnect(self._confirm_response_handler_id)
                except Exception:
                    pass
                self._confirm_response_handler_id = None
            try:
                dialog.close()
            except Exception:
                pass

    def _show_confirmation_dialog(self, connector, target_rot, target_touch, prev_rot, prev_touch):
        self._cleanup_timer()

        self._remaining_seconds = 15
        self._confirm_dialog = Adw.AlertDialog(
            heading="Keep Display Settings?",
            body=f"Reverting to previous settings in {self._remaining_seconds} seconds."
        )
        self._confirm_dialog.add_response("revert", "Revert")
        self._confirm_dialog.add_response("keep", "Keep Changes")
        self._confirm_dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
        self._confirm_dialog.set_response_appearance("revert", Adw.ResponseAppearance.DESTRUCTIVE)
        self._confirm_dialog.set_default_response("keep")
        self._confirm_dialog.set_close_response("revert")

        def _revert_action():
            self._cleanup_timer()
            self._confirm_dialog = None
            self.window.show_toast(f"Reverting display orientation for {connector}...")

            def revert_worker():
                return DisplayService.apply_display_and_touch(
                    connector, prev_rot, prev_touch, persist=True,
                    prev_touch_device_name=target_touch
                )

            def on_revert_done(ok):
                self.staged_rotations[connector] = prev_rot
                self.staged_touch[connector] = prev_touch
                self.live_rotations[connector] = prev_rot
                self.live_touch[connector] = prev_touch
                self.window.show_toast("Reverted to previous display settings.")
                self.refresh()

            run_async(revert_worker, on_done=on_revert_done)

        def _keep_action():
            self._cleanup_timer()
            self._confirm_dialog = None

            def keep_worker():
                return DisplayService.persist_display_config(connector, target_rot, target_touch)

            def on_keep_done(ok):
                self.live_rotations[connector] = target_rot
                self.live_touch[connector] = target_touch
                self.window.show_toast("Display settings confirmed.")
                self.refresh()

            run_async(keep_worker, on_done=on_keep_done)

        def on_response(dialog, response):
            if response == "keep":
                _keep_action()
            else:
                _revert_action()

        self._confirm_response_handler_id = self._confirm_dialog.connect("response", on_response)

        def on_tick():
            self._remaining_seconds -= 1
            if self._remaining_seconds <= 0:
                self._confirm_timer_id = None
                if getattr(self, "_confirm_dialog", None) is not None:
                    try:
                        self._confirm_dialog.close()
                    except Exception:
                        _revert_action()
                else:
                    _revert_action()
                return GLib.SOURCE_REMOVE

            if getattr(self, "_confirm_dialog", None) is not None:
                self._confirm_dialog.set_body(
                    f"Reverting to previous settings in {self._remaining_seconds} seconds."
                )
            return GLib.SOURCE_CONTINUE

        self._confirm_timer_id = GLib.timeout_add_seconds(1, on_tick)

        parent = self.get_root() if hasattr(self, "get_root") and self.get_root() else self.window
        if isinstance(parent, Gtk.Window):
            self._confirm_dialog.present(parent)
        elif hasattr(self._confirm_dialog, "present"):
            try:
                self._confirm_dialog.present(None)
            except Exception:
                pass

    def _apply_settings(self, connector):
        target_rot = self.staged_rotations.get(connector, 0)
        target_touch = self.staged_touch.get(connector, "None")
        prev_rot = self.live_rotations.get(connector, 0)
        prev_touch = self.live_touch.get(connector, "None")

        # If settings didn't change from currently confirmed state, persist directly
        if target_rot == prev_rot and target_touch == prev_touch:
            self.window.show_toast(f"Saving display settings for {connector}...")

            def worker():
                return DisplayService.apply_display_and_touch(
                    connector, target_rot, target_touch, persist=True
                )

            def on_done(ok):
                if ok:
                    self.window.show_toast("Display settings saved.")
                else:
                    self.window.show_toast("Failed saving display settings.")
                self.refresh()

            run_async(worker, on_done=on_done)
            return

        self.window.show_toast(f"Applying orientation and touch settings to {connector}...")

        def worker():
            # Apply with persist=False so unexpected power loss / reboot restores previous settings
            return DisplayService.apply_display_and_touch(
                connector, target_rot, target_touch, persist=False,
                prev_touch_device_name=prev_touch
            )

        def on_done(ok):
            if ok:
                self._show_confirmation_dialog(
                    connector, target_rot, target_touch, prev_rot, prev_touch
                )
            else:
                self.window.show_toast("Failed applying display orientation.")
                self.refresh()

        run_async(worker, on_done=on_done)

    def _on_clear_config_clicked(self, btn):
        self.window.show_toast("Clearing rotation overrides...")

        def worker():
            return DisplayService.clear_config()

        def on_done(ok):
            self.staged_rotations.clear()
            self.staged_touch.clear()
            self.live_rotations.clear()
            self.live_touch.clear()
            if ok:
                self.window.show_toast("Cleared rotation overrides and autostart.")
            else:
                self.window.show_toast("Failed clearing configuration.")
            self.refresh()

        run_async(worker, on_done=on_done)


