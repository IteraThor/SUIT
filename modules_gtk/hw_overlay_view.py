import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from core.system_service import SystemService
from modules_gtk.async_utils import run_async
from modules_gtk.camera_widgets import segmented
from modules_gtk.ui_helpers import (
    PAGE_MAX_WIDTH, PAGE_TIGHTEN_AT, build_page_shell, create_button_with_icon,
)
from core.i18n import tr

__all__ = ["HwOverlayView", "summarize_hw_overlay"]

SYSTEM_METRICS = [
    ("cpu", "CPU Usage and Clock", ""),
    ("temp", "CPU Temperature", ""),
    ("ram", "Memory", ""),
    ("gpu", "Graphics (AMD GPUs only)", ""),
    ("disk", "Disk Space", ""),
    ("load", "Load Average", ""),
    ("uptime", "Uptime", ""),
]
NETWORK_METRICS = [
    ("net", "Network Speed", ""),
    ("ip_local", "Local Network IP", ""),
    ("ip_tailscale", "Tailscale IP", ""),
]
AUTODARTS_METRICS = [
    ("ad_state", "Engine State and Version", ""),
    ("ad_fps", "Average FPS", ""),
    ("ad_cams", "Per-Camera FPS", ""),
    ("ad_proc", "Autodarts Process Usage", ""),
]
# Clickable controls the overlay can show, same actions as the buttons on Autodarts' own page
AUTODARTS_BUTTONS = [
    ("ad_toggle", "Start / Stop Button"),
    ("ad_reset", "Reset Button"),
    ("ad_dump", "Debug Snapshot Button (saves to Downloads)"),
    ("ad_update", "Update Button (when an update is available)"),
]
POSITION_LABELS = {
    "top-left": "Top Left", "top-right": "Top Right",
    "bottom-left": "Bottom Left", "bottom-right": "Bottom Right",
}


def summarize_hw_overlay(enabled: bool, cfg: dict) -> str:
    """One-line state for the System page row, e.g. 'On · 9 metrics · Top Right'."""
    if not enabled:
        return tr("Off. Show live system and camera stats on the wallpaper.")
    count = sum(cfg["metrics"].values())
    metrics = tr("1 metric") if count == 1 else tr("{n} metrics").format(n=count)
    return tr("On · {metrics} · {position}").format(metrics=metrics, position=tr(POSITION_LABELS[cfg["position"]]))



class HwOverlayView(Adw.NavigationPage):
    """Submenu for the desktop Hardware Overlay: on/off, its look, and which metrics it shows.

    Every change is saved at once; the shell extension watches its config file and redraws
    live, so the wallpaper itself is the preview.
    """

    def __init__(self, window, on_changed=None):
        super().__init__(title=tr("Hardware Overlay"), tag="hw_overlay")
        self.window = window
        self._on_changed = on_changed
        self._updating = False
        self._save_id = 0
        self.config = SystemService.get_hw_overlay_config()

        # On/off and Reset stay pinned above three tabs, so every tab fits one screen
        grp_overlay = Adw.PreferencesGroup(title=tr("Overlay"))
        btn_reset = create_button_with_icon("edit-undo-symbolic", "Reset", "secondary-btn compact-btn", height=36)
        btn_reset.set_tooltip_text(tr("Reset every overlay setting to its default"))
        btn_reset.connect("clicked", self._on_reset_clicked)
        grp_overlay.set_header_suffix(btn_reset)

        self.row_enabled = Adw.SwitchRow(title=tr("Show on Desktop"), subtitle=tr("Checking..."))
        self.row_enabled.connect("notify::active", self._on_enabled_toggled)
        grp_overlay.add(self.row_enabled)

        top = Adw.Clamp(maximum_size=PAGE_MAX_WIDTH, tightening_threshold=PAGE_TIGHTEN_AT)
        top.set_margin_top(12)
        top.set_margin_start(16)
        top.set_margin_end(16)
        top.set_child(grp_overlay)

        scrolled_look, look_box = build_page_shell()
        scrolled_sys, sys_box = build_page_shell()
        scrolled_net, net_box = build_page_shell()
        scrolled_ad, ad_box = build_page_shell()
        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.add_titled(scrolled_look, "look", tr("Look"))
        self.stack.add_titled(scrolled_sys, "system", tr("System"))
        self.stack.add_titled(scrolled_net, "network", tr("Network"))
        self.stack.add_titled(scrolled_ad, "autodarts", "Autodarts")

        switcher = Adw.InlineViewSwitcher(stack=self.stack, display_mode=Adw.InlineViewSwitcherDisplayMode.LABELS)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_margin_top(4)

        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page_box.append(top)
        page_box.append(switcher)
        page_box.append(self.stack)
        self.set_child(page_box)

        # Look: one group, every row a single line
        grp_look = Adw.PreferencesGroup()
        look_box.append(grp_look)
        self.seg_style = self._add_choice(grp_look, "Style", "style", [("card", "Card"), ("minimal", "Text Only")])
        self.seg_position = self._add_choice(grp_look, "Position", "position", list(POSITION_LABELS.items()))
        self.seg_size = self._add_choice(grp_look, "Size", "size",
                                         [("small", "Small"), ("medium", "Medium"), ("large", "Large")])
        self.seg_accent = self._add_choice(grp_look, "Meter Color", "accent",
                                           [("blue", "Blue"), ("teal", "Teal"), ("green", "Green"),
                                            ("purple", "Purple"), ("white", "White")])
        self.seg_interval = self._add_choice(grp_look, "Refresh Every", "interval",
                                             [(1, "1 s"), (2, "2 s"), (5, "5 s")])

        self.row_opacity = Adw.ActionRow(title=tr("Card Opacity"))
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

        self.row_meters = self._add_switch(grp_look, "Usage Meters", "",
                                           lambda: self.config["meters"], lambda v: self._set("meters", v))
        self.row_headings = self._add_switch(grp_look, "Section Titles", "",
                                             lambda: self.config["headings"], lambda v: self._set("headings", v))
        self.row_button_colors = self._add_switch(
            grp_look, "Color Action Buttons", "Start green, Stop red, Reset yellow, Debug blue",
            lambda: self.config["button_colors"], lambda v: self._set("button_colors", v))

        # Metrics: one tab per source
        self.metric_rows = {}
        for box, metrics in ((sys_box, SYSTEM_METRICS), (net_box, NETWORK_METRICS), (ad_box, AUTODARTS_METRICS)):
            grp = Adw.PreferencesGroup(title=tr("Show"))
            box.append(grp)
            for key, name, hint in metrics:
                self.metric_rows[key] = self._add_switch(
                    grp, name, hint,
                    lambda k=key: self.config["metrics"][k],
                    lambda v, k=key: self._set_metric(k, v))

        grp_buttons = Adw.PreferencesGroup(title=tr("Buttons"))
        ad_box.append(grp_buttons)
        self.button_rows = {}
        for key, name in AUTODARTS_BUTTONS:
            self.button_rows[key] = self._add_switch(
                grp_buttons, name, "",
                lambda k=key: self.config["buttons"][k],
                lambda v, k=key: self._set_button(k, v))

        self._sync_sensitivity()

    # ------------------------------------------------------------------ builders

    def _add_choice(self, group, title, key, options):
        row = Adw.ActionRow(title=tr(title))
        seg = segmented(options, self.config[key], lambda v, k=key: self._set(k, v))
        row.add_suffix(seg)
        group.add(row)
        return seg

    def _add_switch(self, group, title, subtitle, getter, setter):
        row = Adw.SwitchRow(title=tr(title), subtitle=tr(subtitle))
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
                    tr("Overlay updated. Log out and back in once so these settings apply live."))
            else:
                self.row_enabled.set_subtitle(
                    tr("Shown on the wallpaper.") if enabled else tr("Hidden. The settings below are kept."))
            for seg, key in ((self.seg_style, "style"), (self.seg_position, "position"), (self.seg_size, "size"),
                             (self.seg_accent, "accent"), (self.seg_interval, "interval")):
                seg.set_active_name(str(cfg[key]))
            self.scale_opacity.set_value(cfg["opacity"])
            self.row_meters.set_active(cfg["meters"])
            self.row_headings.set_active(cfg["headings"])
            self.row_button_colors.set_active(cfg["button_colors"])
            for key, row in self.metric_rows.items():
                row.set_active(cfg["metrics"][key])
            for key, row in self.button_rows.items():
                row.set_active(cfg["buttons"][key])
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

    def _set_button(self, key, value):
        self.config["buttons"][key] = value
        self._save()

    def _save(self):
        try:
            self.config = SystemService.save_hw_overlay_config(self.config)
        except OSError as e:
            self.window.show_toast(tr("Could not save overlay settings: {error}").format(error=e))
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
        self.window.show_toast(tr("Overlay settings reset to defaults."))
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
