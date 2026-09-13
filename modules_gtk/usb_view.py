import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from pathlib import Path

from core.logger import get_logger
from core.usb_service import UsbService, AutodartsLiveMonitorService
from core.autodarts_service import (
    read_cam_config,
    save_cam_config,
    get_supported_resolutions,
    DEFAULT_HOST,
    DEFAULT_PORT,
    is_port_open
)
from modules_gtk.async_utils import run_async

logger = get_logger("usb_view")

FPS_OPTIONS = [15, 20, 25, 30]


class UsbView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="USB Bandwidth Analyzer", tag="usb")
        self.window = window

        self.live_monitor = AutodartsLiveMonitorService(DEFAULT_HOST, DEFAULT_PORT)
        self._cam_widgets: dict[str, dict] = {}
        self._res_options: list[tuple[int, int]] = [(1280, 960), (1280, 720), (1920, 1080), (640, 480)]
        self._target_fps = 25.0
        self._target_res = (1280, 960)
        self._updating_ui = False

        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_kinetic_scrolling(True)
        self.set_child(scrolled)

        clamp = Adw.Clamp(maximum_size=820, tightening_threshold=640)
        clamp.set_margin_top(14)
        clamp.set_margin_bottom(20)
        clamp.set_margin_start(14)
        clamp.set_margin_end(14)
        scrolled.set_child(clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        clamp.set_child(self.main_box)

        # 1. Top Control Bar (Resolution & FPS selectors + Live badge)
        self._build_top_controls()

        # 2. Compact Status & Health Banner
        self._build_status_banner()

        # 4. Camera Stream Cards Container
        self.cams_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.main_box.append(self.cams_container)

        self.connect("map", lambda w: self.refresh())

    def _build_top_controls(self):
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        card.add_css_class("tile-card")
        card.set_valign(Gtk.Align.CENTER)

        # Resolution DropDown
        box_res = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        lbl_res = Gtk.Label(label="Resolution", xalign=0)
        lbl_res.add_css_class("dim-label")
        box_res.append(lbl_res)

        self.combo_res = Gtk.DropDown.new_from_strings(["1280x960", "1280x720", "1920x1080", "640x480"])
        self.combo_res.set_size_request(160, 44)
        self.combo_res.connect("notify::selected", self._on_settings_changed)
        box_res.append(self.combo_res)
        card.append(box_res)

        # FPS DropDown (strictly Autodarts options: 15, 20, 25, 30)
        box_fps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        lbl_fps = Gtk.Label(label="FPS", xalign=0)
        lbl_fps.add_css_class("dim-label")
        box_fps.append(lbl_fps)

        self.combo_fps = Gtk.DropDown.new_from_strings(["15", "20", "25", "30"])
        self.combo_fps.set_size_request(110, 44)
        self.combo_fps.connect("notify::selected", self._on_settings_changed)
        box_fps.append(self.combo_fps)
        card.append(box_fps)

        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        card.append(spacer)

        # Live telemetry badge
        box_live = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box_live.set_valign(Gtk.Align.CENTER)
        lbl_live_title = Gtk.Label(label="Telemetry", xalign=1)
        lbl_live_title.add_css_class("dim-label")
        box_live.append(lbl_live_title)

        self.lbl_live_badge = Gtk.Label(label="Live")
        self.lbl_live_badge.add_css_class("status-pill-running")
        box_live.append(self.lbl_live_badge)
        card.append(box_live)

        self.main_box.append(card)

    def _build_status_banner(self):
        self.status_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.status_card.add_css_class("tile-card-compact")
        self.status_card.set_valign(Gtk.Align.CENTER)

        self.img_status = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        self.img_status.set_pixel_size(20)
        self.status_card.append(self.img_status)

        self.lbl_status_desc = Gtk.Label(
            label="Connecting to camera feeds...",
            xalign=0,
            wrap=True
        )
        self.lbl_status_desc.set_hexpand(True)
        self.status_card.append(self.lbl_status_desc)

        self.lbl_status_pill = Gtk.Label(label="Checking...")
        self.lbl_status_pill.add_css_class("status-pill-checking")
        self.status_card.append(self.lbl_status_pill)

        self.main_box.append(self.status_card)

    def refresh(self):
        def worker():
            cfg = read_cam_config()
            topo = UsbService.analyze_bandwidth()
            supported_res = get_supported_resolutions()
            return {"cfg": cfg, "topo": topo, "res": supported_res}

        def on_done(data):
            self._render_static_data(data)
            # Check if Autodarts engine is reachable
            if is_port_open(DEFAULT_HOST, DEFAULT_PORT):
                self.lbl_live_badge.set_label("Live (Autodarts)")
                self.lbl_live_badge.remove_css_class("status-pill-stopped")
                self.lbl_live_badge.add_css_class("status-pill-running")
                self.live_monitor.start_monitoring(self._on_metrics_received)
            else:
                self.lbl_live_badge.set_label("Engine Offline")
                self.lbl_live_badge.remove_css_class("status-pill-running")
                self.lbl_live_badge.add_css_class("status-pill-stopped")
                self.live_monitor.stop_monitoring()
                self.lbl_status_desc.set_text("Autodarts is stopped.")
                self.lbl_status_pill.set_label("Offline")

        run_async(worker, on_done=on_done)

    def _render_static_data(self, data):
        self._updating_ui = True
        try:
            cfg = data.get("cfg", {})
            topo = data.get("topo", {})
            res_list = data.get("res", [])

            if res_list:
                self._res_options = res_list

            # Populate Resolution DropDown
            res_strings = [f"{w}x{h}" for w, h in self._res_options]
            self.combo_res.set_model(Gtk.StringList.new(res_strings))

            cur_w = cfg.get("width", 1280)
            cur_h = cfg.get("height", 960)
            self._target_res = (cur_w, cur_h)
            if (cur_w, cur_h) in self._res_options:
                self.combo_res.set_selected(self._res_options.index((cur_w, cur_h)))

            # Populate FPS DropDown (15, 20, 25, 30)
            cur_fps = cfg.get("fps", 25)
            self._target_fps = float(cur_fps)
            if cur_fps in FPS_OPTIONS:
                self.combo_fps.set_selected(FPS_OPTIONS.index(cur_fps))

            # Build camera cards
            while child := self.cams_container.get_first_child():
                self.cams_container.remove(child)
            self._cam_widgets.clear()

            cameras = topo.get("cameras", [])
            if not cameras:
                empty = Adw.ActionRow(
                    title="No video cameras detected",
                    subtitle="Ensure Autodarts cameras are plugged into USB ports."
                )
                self.cams_container.append(empty)
            else:
                for idx, cam in enumerate(cameras, start=1):
                    self._create_camera_card(idx, cam)
        finally:
            self._updating_ui = False

    def _create_camera_card(self, index: int, cam: dict):
        dev_path = cam.get("dev_path", f"/dev/{cam.get('dev', '')}")
        hub_name = cam.get("hub_name", "Root Port")
        devpath = cam.get("devpath", "")
        mode = cam.get("transfer_mode", "Bulk")
        path_str = f"Port {devpath} ({hub_name}) • {mode}"

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("tile-card")
        card.set_margin_top(2)
        card.set_margin_bottom(2)

        # Line 1: Icon + Title + Hardware Tag + Live FPS + Status Pill
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row1.set_valign(Gtk.Align.CENTER)

        cam_icon = Gtk.Image.new_from_icon_name("camera-web-symbolic")
        cam_icon.set_pixel_size(20)
        row1.append(cam_icon)

        lbl_title = Gtk.Label(label=f"Camera {index} ({dev_path})", xalign=0)
        lbl_title.add_css_class("heading")
        row1.append(lbl_title)

        tag = Gtk.Label(label=path_str)
        tag.add_css_class("usb-path-tag")
        row1.append(tag)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row1.append(spacer)

        lbl_fps = Gtk.Label(label="-- FPS")
        lbl_fps.add_css_class("usb-stat-large")
        row1.append(lbl_fps)

        status_pill = Gtk.Label(label="Starting...")
        status_pill.add_css_class("status-pill-checking")
        row1.append(status_pill)
        card.append(row1)

        # Line 2: Progress Meter
        bar = Gtk.ProgressBar()
        bar.set_fraction(0.0)
        bar.set_size_request(-1, 5)
        card.append(bar)

        self._cam_widgets[dev_path] = {
            "fps_label": lbl_fps,
            "status_pill": status_pill,
            "progress_bar": bar,
            "path_tag": tag
        }

        self.cams_container.append(card)

    def _on_settings_changed(self, dropdown, param):
        if self._updating_ui:
            return

        res_idx = self.combo_res.get_selected()
        fps_idx = self.combo_fps.get_selected()

        if res_idx < len(self._res_options) and fps_idx < len(FPS_OPTIONS):
            w, h = self._res_options[res_idx]
            fps = FPS_OPTIONS[fps_idx]
            self._target_res = (w, h)
            self._target_fps = float(fps)

            cfg = read_cam_config()
            cams = cfg.get("cams", ["/dev/video0", "/dev/video2", "/dev/video4"])
            save_cam_config(cams, w, h, fps)
            self.window.show_toast(f"Autodarts set to {w}x{h} @ {fps} FPS")

    def _on_metrics_received(self, snapshot: dict):
        GLib.idle_add(self._apply_metrics, snapshot)

    def _apply_metrics(self, snapshot: dict):
        overall = snapshot.get("overall_status", "Optimal")
        pill_text = snapshot.get("status_pill", overall)
        rec = snapshot.get("recommendation", "")
        self.lbl_status_desc.set_text(rec)

        if overall == "Optimal":
            self.lbl_status_pill.set_label(pill_text)
            self.lbl_status_pill.remove_css_class("status-pill-fault")
            self.lbl_status_pill.remove_css_class("status-pill-checking")
            self.lbl_status_pill.add_css_class("status-pill-running")
            self.img_status.set_from_icon_name("emblem-ok-symbolic")
        elif overall in ("Warning", "CPU Limit", "Throttled"):
            self.lbl_status_pill.set_label(pill_text)
            self.lbl_status_pill.remove_css_class("status-pill-running")
            self.lbl_status_pill.remove_css_class("status-pill-checking")
            self.lbl_status_pill.add_css_class("status-pill-fault")
            self.img_status.set_from_icon_name("dialog-warning-symbolic")
        else:
            self.lbl_status_pill.set_label(pill_text)
            self.lbl_status_pill.remove_css_class("status-pill-running")
            self.lbl_status_pill.remove_css_class("status-pill-fault")
            self.lbl_status_pill.add_css_class("status-pill-checking")
            self.img_status.set_from_icon_name("dialog-information-symbolic")

        cams_data = snapshot.get("cameras", {})
        target_fps = snapshot.get("target_fps", self._target_fps)

        for dev_path, stats in cams_data.items():
            widgets = self._cam_widgets.get(dev_path)
            if not widgets:
                continue

            fps = stats.get("delivered_fps", 0.0)
            status = stats.get("status", "Optimal")

            widgets["fps_label"].set_label(f"{fps:.1f} FPS" if fps > 0 else "-- FPS")

            fraction = min(1.0, max(0.0, fps / max(1.0, target_fps)))
            widgets["progress_bar"].set_fraction(fraction)

            pill = widgets["status_pill"]
            pill.set_label(status)
            pill.remove_css_class("status-pill-running")
            pill.remove_css_class("status-pill-fault")
            pill.remove_css_class("status-pill-stopped")
            pill.remove_css_class("status-pill-checking")

            if status == "Optimal":
                pill.add_css_class("status-pill-running")
            elif status in ("Throttled", "CPU Limit"):
                pill.add_css_class("status-pill-fault")
            elif status == "Starting":
                pill.add_css_class("status-pill-checking")
            else:
                pill.add_css_class("status-pill-stopped")

        return False

    def on_page_closed(self):
        """Called automatically when navigating away from this page."""
        if self.live_monitor.is_running:
            self.live_monitor.stop_monitoring()
