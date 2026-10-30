import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from core.system_service import SystemService
from modules_gtk.async_utils import run_async
from modules_gtk.camera_widgets import segmented
from modules_gtk.ui_helpers import create_button_with_icon

__all__ = ["HwOverlayView", "summarize_hw_overlay"]

SYSTEM_METRICS = [
    ("cpu", "CPU Usage", "Load and clock speed."),
    ("temp", "CPU Temperature", "Package temperature."),
    ("ram", "Memory", "Used and total RAM."),
    ("gpu", "Graphics", "Load and temperature (AMD GPUs only)."),
    ("disk", "Disk Space", "Used share of the system disk."),
    ("net", "Network", "Download and upload speed."),
    ("load", "Load Average", "1, 5 and 15 minute averages."),
    ("uptime", "Uptime", "Time since the last boot."),
]
AUTODARTS_METRICS = [
    ("ad_state", "Engine State", "Running, standby or offline, and the version."),
    ("ad_fps", "Average FPS", "Large readout with the 1-minute low and resolution."),
    ("ad_cams", "Per-Camera FPS", "One tile per camera."),
    ("ad_proc", "Process Usage", "CPU and memory of the Autodarts process."),
]
POSITION_LABELS = {
    "top-left": "Top Left", "top-right": "Top Right",
    "bottom-left": "Bottom Left", "bottom-right": "Bottom Right",
}


def summarize_hw_overlay(enabled: bool, cfg: dict) -> str:
    """One-line state for the System page row, e.g. 'On · 9 metrics · Top Right'."""
    if not enabled:
        return "Off. Show live system and camera stats on the wallpaper."
    count = sum(cfg["metrics"].values())
    return f"On · {count} metric{'s' if count != 1 else ''} · {POSITION_LABELS[cfg['position']]}"


from modules_gtk.ui_helpers import build_page_shell

class HwOverlayView(Adw.NavigationPage):
    """Submenu for the desktop Hardware Overlay: on/off, its look, and which metrics it shows.

    Every change is saved at once; the shell extension watches its config file and redraws
    live, so the wallpaper itself is the preview.
    """

    def __init__(self, window, on_changed=None):
        super().__init__(title="Hardware Overlay", tag="hw_overlay")
        self.window = window
        self._on_changed = on_changed
        self._updating = False
        self._save_id = 0
        self.config = SystemService.get_hw_overlay_config()

        scrolled, main_box = build_page_shell()
        self.set_child(scrolled)

        # =========================================================================
        # Group 1: On / Off
        # =========================================================================
        grp_overlay = Adw.PreferencesGroup(
            title="Overlay",
            description="Live system and camera stats drawn on the desktop wallpaper. Changes show up instantly."
        )
        main_box.append(grp_overlay)

        self.row_enabled = Adw.SwitchRow(title="Show on Desktop", subtitle="Checking...")
        self.row_enabled.connect("notify::active", self._on_enabled_toggled)
        grp_overlay.add(self.row_enabled)

        # =========================================================================
        # Group 2: Appearance
        # =========================================================================
        grp_look = Adw.PreferencesGroup(title="Appearance")
        main_box.append(grp_look)

        self.seg_style = self._add_choice(grp_look, "Style", "Card with a background, or text only.", "style",
                                          [("card", "Card"), ("minimal", "Text Only")])
        self.seg_position = self._add_choice(grp_look, "Position", None, "position", list(POSITION_LABELS.items()))
        self.seg_size = self._add_choice(grp_look, "Size", None, "size",
                                         [("small", "Small"), ("medium", "Medium"), ("large", "Large")])
        self.seg_accent = self._add_choice(grp_look, "Meter Color", "Warnings always show amber or red.", "accent",
                                           [("blue", "Blue"), ("teal", "Teal"), ("green", "Green"),
                                            ("purple", "Purple"), ("white", "White")])

        self.row_opacity = Adw.ActionRow(title="Background Opacity", subtitle="Card style only.")
        self.scale_opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.scale_opacity.set_size_request(240, -1)
        self.scale_opacity.set_valign(Gtk.Align.CENTER)
        self.scale_opacity.set_draw_value(True)
        self.scale_opacity.set_value_pos(Gtk.PositionType.LEFT)
        self.scale_opacity.set_format_value_func(lambda s, v: f"{v:.0f} %")
        self.scale_opacity.set_value(self.config["opacity"])
        self.scale_opacity.connect("value-changed", self._on_opacity_changed)
        self.row_opacity.add_suffix(self.scale_opacity)
        grp_look.add(self.row_opacity)

        self.row_meters = self._add_switch(grp_look, "Usage Meters", "Thin bars under CPU, temperature, RAM, GPU and disk.",
                                           lambda: self.config["meters"], lambda v: self._set("meters", v))
        self.row_headings = self._add_switch(grp_look, "Section Titles", "Show the SYSTEM and AUTODARTS titles.",
                                             lambda: self.config["headings"], lambda v: self._set("headings", v))
        self.seg_interval = self._add_choice(grp_look, "Refresh", None, "interval",
                                             [(1, "1 s"), (2, "2 s"), (5, "5 s")])

        # =========================================================================
        # Group 3 + 4: Metrics
        # =========================================================================
        self.metric_rows = {}
        for title, metrics in (("System Metrics", SYSTEM_METRICS), ("Autodarts", AUTODARTS_METRICS)):
            grp = Adw.PreferencesGroup(title=title)
            main_box.append(grp)
            for key, name, hint in metrics:
                self.metric_rows[key] = self._add_switch(
                    grp, name, hint,
                    lambda k=key: self.config["metrics"][k],
                    lambda v, k=key: self._set_metric(k, v))

        btn_reset = create_button_with_icon("edit-undo-symbolic", "Reset to Defaults", "secondary-btn", height=38)
        btn_reset.set_halign(Gtk.Align.CENTER)
        btn_reset.set_margin_top(6)
        btn_reset.connect("clicked", self._on_reset_clicked)
        main_box.append(btn_reset)

        self._sync_sensitivity()

    # ------------------------------------------------------------------ builders

    def _add_choice(self, group, title, subtitle, key, options):
        row = Adw.ActionRow(title=title, subtitle=subtitle or "")
        seg = segmented(options, self.config[key], lambda v, k=key: self._set(k, v))
        row.add_suffix(seg)
        group.add(row)
        return seg

    def _add_switch(self, group, title, subtitle, getter, setter):
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_active(getter())
        row.connect("notify::active", lambda r, p: not self._updating and setter(r.get_active()))
        group.add(row)
        return row

    # ------------------------------------------------------------------ state

    def refresh(self):
        def worker():
            return (SystemService.is_hw_overlay_enabled(), SystemService.get_hw_overlay_config(),
                    SystemService.hw_overlay_needs_relogin())

        def on_done(res):
            enabled, cfg, needs_relogin = res
            self.config = cfg
            self._load_into_widgets(enabled, needs_relogin)

        run_async(worker, on_done=on_done)

    def _load_into_widgets(self, enabled: bool, needs_relogin: bool = False):
        cfg = self.config
        self._updating = True
        try:
            self.row_enabled.set_active(enabled)
            if needs_relogin:
                self.row_enabled.set_subtitle(
                    "Overlay updated. Log out and back in once so these settings apply live.")
            else:
                self.row_enabled.set_subtitle(
                    "Shown on the wallpaper." if enabled else "Hidden. The settings below are kept.")
            for seg, key in ((self.seg_style, "style"), (self.seg_position, "position"), (self.seg_size, "size"),
                             (self.seg_accent, "accent"), (self.seg_interval, "interval")):
                seg.set_active_name(str(cfg[key]))
            self.scale_opacity.set_value(cfg["opacity"])
            self.row_meters.set_active(cfg["meters"])
            self.row_headings.set_active(cfg["headings"])
            for key, row in self.metric_rows.items():
                row.set_active(cfg["metrics"][key])
        finally:
            self._updating = False
        self._sync_sensitivity()

    def _sync_sensitivity(self):
        self.row_opacity.set_sensitive(self.config["style"] == "card")

    def _set(self, key, value):
        if self._updating or self.config.get(key) == value:
            return
        self.config[key] = value
        self._sync_sensitivity()
        self._save()

    def _set_metric(self, key, value):
        self.config["metrics"][key] = value
        self._save()

    def _save(self):
        try:
            self.config = SystemService.save_hw_overlay_config(self.config)
        except OSError as e:
            self.window.show_toast(f"Could not save overlay settings: {e}")
            return
        if self._on_changed:
            self._on_changed()

    def _on_opacity_changed(self, scale):
        if self._updating:
            return
        # Dragging fires many changes; save once the slider settles
        if self._save_id:
            GLib.source_remove(self._save_id)

        def save():
            self._save_id = 0
            self._set("opacity", int(scale.get_value()))
            return GLib.SOURCE_REMOVE

        self._save_id = GLib.timeout_add(250, save)

    def _on_reset_clicked(self, btn):
        self.config = SystemService.save_hw_overlay_config({})
        self._load_into_widgets(self.row_enabled.get_active())
        self.window.show_toast("Overlay settings reset to defaults.")
        if self._on_changed:
            self._on_changed()

    def _on_enabled_toggled(self, row, param):
        if self._updating:
            return
        active = row.get_active()
        row.set_sensitive(False)

        def worker():
            return SystemService.set_hw_overlay(active)

        def on_done(res):
            row.set_sensitive(True)
            success, msg = res
            self.window.show_toast(msg)
            self.refresh()
            if self._on_changed:
                self._on_changed()

        run_async(worker, on_done=on_done)
