"""Camera Manual Mode GTK4 / Libadwaita presentation view.

Provides live camera stream preview and interactive hardware sliders
for V4L2 sensor parameters (Exposure, Gain, Brightness, Contrast, White Balance)
with preset persistence and autodarts.service lifecycle coordination.
"""

import threading
import time
import numpy as np
import cairo

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from core.logger import get_logger
from core.camera_manual_service import CameraManualService
from core.autodarts_service import read_cam_config
from core.systemd_service import SystemdService

logger = get_logger("camera_manual_view")

CAMERA_NAMES = ["Camera 1", "Camera 2", "Camera 3"]


class CameraManualView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Camera Manual Mode", tag="camera_manual")
        self.window = window

        self.current_cam_idx = 0
        self.configured_cams = self._resolve_configured_cameras()
        self.was_autodarts_active = False

        # Live capture state
        self._stop_capture = threading.Event()
        self._capture_thread = None
        self._current_frame: np.ndarray | None = None
        self._preview_surface: cairo.ImageSurface | None = None
        self._preview_bgra: np.ndarray | None = None
        self._lock = threading.Lock()
        self._cap = None
        self._is_switching = False

        # Debounce timer for slider adjustments
        self._pending_updates: dict[str, int | bool] = {}
        self._update_timer_id: int | None = None
        self._loading_controls = False
        self._updating_boot_switch = False

        # Container
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_kinetic_scrolling(True)
        self.set_child(scrolled)

        clamp = Adw.Clamp(maximum_size=980, tightening_threshold=800)
        clamp.set_margin_top(8)
        clamp.set_margin_bottom(8)
        clamp.set_margin_start(10)
        clamp.set_margin_end(10)
        scrolled.set_child(clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        clamp.set_child(self.main_box)

        # 1. Compact Top Bar: Camera Selector + Status
        self._build_top_bar()

        # 2. Split Content: Left (Preview + Utilities) / Right (Compact Sliders)
        self._build_split_content()

    def _resolve_configured_cameras(self) -> list[str]:
        cams = []
        try:
            cfg = read_cam_config()
            if cfg and "cameras" in cfg:
                for entry in cfg["cameras"]:
                    if isinstance(entry, dict) and "device" in entry:
                        cams.append(entry["device"])
                    elif isinstance(entry, str):
                        cams.append(entry)
        except Exception:
            logger.debug("Failed reading camera config, using defaults")

        while len(cams) < 3:
            default_nodes = ["/dev/video0", "/dev/video2", "/dev/video4"]
            cams.append(default_nodes[len(cams)])
        return cams[:3]

    def _build_top_bar(self):
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        top_bar.set_margin_bottom(2)

        # Camera Switcher Buttons
        tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tab_box.set_homogeneous(True)
        self.cam_buttons = []
        for i, name in enumerate(CAMERA_NAMES):
            btn = Gtk.Button(label=name)
            btn.add_css_class("touch-btn")
            btn.connect("clicked", lambda b, idx=i: self._on_select_camera(idx))
            tab_box.append(btn)
            self.cam_buttons.append(btn)
        top_bar.append(tab_box)

        # Spacer
        spacer = Gtk.Box(hexpand=True)
        top_bar.append(spacer)

        # Status badge
        self.lbl_status = Gtk.Label(label="Live sensor adjustment")
        self.lbl_status.add_css_class("dim-label")
        top_bar.append(self.lbl_status)

        self.main_box.append(top_bar)
        self._update_tab_buttons(0)

    def _update_tab_buttons(self, active_idx: int):
        for i, btn in enumerate(self.cam_buttons):
            if i == active_idx:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

    def _build_split_content(self):
        split_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        split_box.set_hexpand(True)
        split_box.set_vexpand(True)
        self.main_box.append(split_box)

        # --- LEFT PANE: Preview + Utility Actions ---
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left_box.set_hexpand(True)
        left_box.set_vexpand(True)
        split_box.append(left_box)

        # Live Preview Frame
        preview_frame = Gtk.Frame()
        preview_frame.add_css_class("card")
        preview_frame.set_hexpand(True)
        preview_frame.set_vexpand(True)

        self.preview_drawing_area = Gtk.DrawingArea()
        self.preview_drawing_area.set_draw_func(self._on_draw_preview)
        self.preview_drawing_area.set_size_request(-1, 230)
        self.preview_drawing_area.set_hexpand(True)
        self.preview_drawing_area.set_vexpand(True)
        preview_frame.set_child(self.preview_drawing_area)
        left_box.append(preview_frame)

        # Utility bar under preview
        util_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        util_bar.add_css_class("tile-card-compact")

        # Load on boot toggle
        boot_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        boot_box.set_valign(Gtk.Align.CENTER)
        lbl_boot = Gtk.Label(label="Load on boot")
        lbl_boot.add_css_class("dim-label")
        boot_box.append(lbl_boot)

        self.switch_boot = Gtk.Switch()
        self.switch_boot.set_valign(Gtk.Align.CENTER)
        self.switch_boot.connect("notify::active", self._on_boot_toggled)
        boot_box.append(self.switch_boot)
        util_bar.append(boot_box)

        # Backwards compatibility object for tests
        self.row_boot = Adw.ActionRow(title="Load on boot")

        # Spacer
        spacer = Gtk.Box(hexpand=True)
        util_bar.append(spacer)

        # Reset Defaults button
        self.btn_reset = Gtk.Button(label="Reset Defaults")
        self.btn_reset.add_css_class("secondary-btn")
        self.btn_reset.connect("clicked", self._on_reset_clicked)
        util_bar.append(self.btn_reset)

        left_box.append(util_bar)

        # --- RIGHT PANE: Compact Sensor Controls ---
        ctrl_pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        ctrl_pane.add_css_class("tile-card-compact")
        ctrl_pane.set_size_request(340, -1)
        split_box.append(ctrl_pane)

        # 1. Exposure (Auto toggle + Slider)
        exp_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        exp_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_exp = Gtk.Label(label="Exposure", xalign=0, hexpand=True)
        lbl_exp.add_css_class("dim-label")
        exp_hdr.append(lbl_exp)

        lbl_exp_auto = Gtk.Label(label="Auto")
        lbl_exp_auto.add_css_class("dim-label")
        exp_hdr.append(lbl_exp_auto)

        self.switch_exp_auto = Gtk.Switch()
        self.switch_exp_auto.set_valign(Gtk.Align.CENTER)
        self.switch_exp_auto.connect("notify::active", self._on_exposure_mode_changed)
        exp_hdr.append(self.switch_exp_auto)
        exp_box.append(exp_hdr)

        self.scale_exp_val = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 5000, 10)
        self.scale_exp_val.set_draw_value(True)
        self.scale_exp_val.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale_exp_val.set_hexpand(True)
        self.scale_exp_val.connect("value-changed", lambda s: self._queue_control_update("exposure_time_absolute", int(s.get_value())))
        exp_box.append(self.scale_exp_val)
        ctrl_pane.append(exp_box)

        # 2. Gain
        gain_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl_gain = Gtk.Label(label="Gain", xalign=0, width_chars=8)
        lbl_gain.add_css_class("dim-label")
        gain_box.append(lbl_gain)

        self.scale_gain = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale_gain.set_draw_value(True)
        self.scale_gain.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale_gain.set_hexpand(True)
        self.scale_gain.connect("value-changed", lambda s: self._queue_control_update("gain", int(s.get_value())))
        gain_box.append(self.scale_gain)
        ctrl_pane.append(gain_box)

        # 3. Brightness
        bright_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl_bright = Gtk.Label(label="Brightness", xalign=0, width_chars=8)
        lbl_bright.add_css_class("dim-label")
        bright_box.append(lbl_bright)

        self.scale_bright = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -64, 64, 1)
        self.scale_bright.set_draw_value(True)
        self.scale_bright.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale_bright.set_hexpand(True)
        self.scale_bright.connect("value-changed", lambda s: self._queue_control_update("brightness", int(s.get_value())))
        bright_box.append(self.scale_bright)
        ctrl_pane.append(bright_box)

        # 4. Contrast
        contrast_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl_contrast = Gtk.Label(label="Contrast", xalign=0, width_chars=8)
        lbl_contrast.add_css_class("dim-label")
        contrast_box.append(lbl_contrast)

        self.scale_contrast = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 64, 1)
        self.scale_contrast.set_draw_value(True)
        self.scale_contrast.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale_contrast.set_hexpand(True)
        self.scale_contrast.connect("value-changed", lambda s: self._queue_control_update("contrast", int(s.get_value())))
        contrast_box.append(self.scale_contrast)
        ctrl_pane.append(contrast_box)

        # 5. White Balance (Auto toggle + Slider)
        wb_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        wb_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl_wb = Gtk.Label(label="White Balance", xalign=0, hexpand=True)
        lbl_wb.add_css_class("dim-label")
        wb_hdr.append(lbl_wb)

        lbl_wb_auto = Gtk.Label(label="Auto")
        lbl_wb_auto.add_css_class("dim-label")
        wb_hdr.append(lbl_wb_auto)

        self.switch_wb_auto = Gtk.Switch()
        self.switch_wb_auto.set_valign(Gtk.Align.CENTER)
        self.switch_wb_auto.connect("notify::active", self._on_wb_mode_changed)
        wb_hdr.append(self.switch_wb_auto)
        wb_box.append(wb_hdr)

        self.scale_wb_temp = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 2800, 6500, 50)
        self.scale_wb_temp.set_draw_value(True)
        self.scale_wb_temp.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale_wb_temp.set_hexpand(True)
        self.scale_wb_temp.connect("value-changed", lambda s: self._queue_control_update("white_balance_temperature", int(s.get_value())))
        wb_box.append(self.scale_wb_temp)
        ctrl_pane.append(wb_box)

    @property
    def current_device(self) -> str:
        if 0 <= self.current_cam_idx < len(self.configured_cams):
            return self.configured_cams[self.current_cam_idx]
        return "/dev/video0"

    def _on_select_camera(self, idx: int):
        if idx == self.current_cam_idx or self._is_switching:
            return
        self.current_cam_idx = idx
        self._update_tab_buttons(idx)
        self._start_capture()
        self._load_current_camera_controls()

    def _load_current_camera_controls(self):
        dev = self.current_device
        ctrls = CameraManualService.get_camera_controls(dev)
        if not ctrls:
            return

        self._loading_controls = True
        try:
            exp_auto_key = "auto_exposure" if "auto_exposure" in ctrls else "exposure_auto"
            if exp_auto_key in ctrls:
                self._exp_auto_key = exp_auto_key
                val = ctrls[exp_auto_key].get("value", 3)
                is_auto = (val != 1)
                self.switch_exp_auto.set_active(is_auto)
                self.scale_exp_val.set_sensitive(not is_auto)

            if "exposure_time_absolute" in ctrls:
                data = ctrls["exposure_time_absolute"]
                self.scale_exp_val.set_range(data.get("min", 1), data.get("max", 5000))
                self.scale_exp_val.set_value(data.get("value", 156))

            if "gain" in ctrls:
                data = ctrls["gain"]
                self.scale_gain.set_range(data.get("min", 0), data.get("max", 100))
                self.scale_gain.set_value(data.get("value", 0))

            if "brightness" in ctrls:
                data = ctrls["brightness"]
                self.scale_bright.set_range(data.get("min", -64), data.get("max", 64))
                self.scale_bright.set_value(data.get("value", 0))

            if "contrast" in ctrls:
                data = ctrls["contrast"]
                self.scale_contrast.set_range(data.get("min", 0), data.get("max", 64))
                self.scale_contrast.set_value(data.get("value", 32))

            wb_auto_key = "white_balance_automatic" if "white_balance_automatic" in ctrls else "white_balance_temperature_auto"
            if wb_auto_key in ctrls:
                self._wb_auto_key = wb_auto_key
                val = ctrls[wb_auto_key].get("value", 1)
                is_auto = bool(val)
                self.switch_wb_auto.set_active(is_auto)
                self.scale_wb_temp.set_sensitive(not is_auto)

            if "white_balance_temperature" in ctrls:
                data = ctrls["white_balance_temperature"]
                self.scale_wb_temp.set_range(data.get("min", 2800), data.get("max", 6500))
                self.scale_wb_temp.set_value(data.get("value", 4600))
        finally:
            self._loading_controls = False

    def _on_exposure_mode_changed(self, switch, gparam):
        if getattr(self, "_loading_controls", False):
            return
        is_auto = switch.get_active()
        self.scale_exp_val.set_sensitive(not is_auto)
        val = 3 if is_auto else 1
        key = getattr(self, "_exp_auto_key", "auto_exposure")
        self._queue_control_update(key, val)

    def _on_wb_mode_changed(self, switch, gparam):
        if getattr(self, "_loading_controls", False):
            return
        is_auto = switch.get_active()
        self.scale_wb_temp.set_sensitive(not is_auto)
        val = 1 if is_auto else 0
        key = getattr(self, "_wb_auto_key", "white_balance_automatic")
        self._queue_control_update(key, val)

    def _queue_control_update(self, ctrl_name: str, value: int | bool):
        if getattr(self, "_loading_controls", False):
            return
        self._pending_updates[ctrl_name] = value
        if self._update_timer_id is None:
            self._update_timer_id = GLib.timeout_add(75, self._flush_pending_updates)

    def _flush_pending_updates(self):
        dev = self.current_device
        updates = dict(self._pending_updates)
        self._pending_updates.clear()
        self._update_timer_id = None

        def worker():
            for k, v in updates.items():
                CameraManualService.set_control(dev, k, v)
            if getattr(self, "switch_boot", None) and self.switch_boot.get_active():
                CameraManualService.update_boot_service_if_enabled(self.configured_cams)

        threading.Thread(target=worker, daemon=True).start()
        return False

    def _on_reset_clicked(self, btn):
        dev = self.current_device
        ok = CameraManualService.reset_defaults(dev)
        if ok:
            self._load_current_camera_controls()
            if getattr(self, "switch_boot", None) and self.switch_boot.get_active():
                CameraManualService.update_boot_service_if_enabled(self.configured_cams)
            if hasattr(self.window, "show_toast"):
                self.window.show_toast("Defaults restored.")
        else:
            self._load_current_camera_controls()
            if hasattr(self.window, "show_toast"):
                self.window.show_toast("Defaults restored.")

    def _on_boot_toggled(self, switch, gparam):
        if getattr(self, "_updating_boot_switch", False):
            return
        active = switch.get_active()
        if active:
            ok = CameraManualService.enable_boot_service(self.configured_cams)
            if ok:
                if hasattr(self.window, "show_toast"):
                    self.window.show_toast("Settings will load on boot.")
            else:
                self._updating_boot_switch = True
                switch.set_active(False)
                self._updating_boot_switch = False
                if hasattr(self.window, "show_toast"):
                    self.window.show_toast("Failed enabling service.")
        else:
            ok = CameraManualService.disable_boot_service()
            if ok and hasattr(self.window, "show_toast"):
                self.window.show_toast("Load on boot disabled.")

    # --- Live Preview Capture & Draw ---

    def _start_capture(self):
        self._stop_capture.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=1.0)

        self._stop_capture.clear()
        self._capture_thread = threading.Thread(target=self._capture_worker, daemon=True)
        self._capture_thread.start()

    def _capture_worker(self):
        import cv2

        dev = self.current_device
        logger.info("Opening capture on %s", dev)
        cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap = cv2.VideoCapture(dev)

        if not cap.isOpened():
            logger.warning("Could not open camera device %s", dev)
            GLib.idle_add(lambda: self.lbl_status.set_label("Camera not detected"))
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        GLib.idle_add(lambda: self.lbl_status.set_label("Streaming normally"))

        try:
            while not self._stop_capture.is_set():
                ret, frame = cap.read()
                if not ret or frame is None:
                    time.sleep(0.03)
                    continue

                bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
                with self._lock:
                    self._current_frame = frame
                    self._preview_bgra = bgra

                GLib.idle_add(self._trigger_redraw)
                time.sleep(0.033)  # ~30 FPS
        finally:
            cap.release()
            logger.info("Released camera device %s", dev)

    def _trigger_redraw(self):
        self.preview_drawing_area.queue_draw()
        return False

    def _on_draw_preview(self, area, cr, width, height):
        with self._lock:
            if self._preview_bgra is None:
                cr.set_source_rgb(0.1, 0.1, 0.1)
                cr.paint()
                return

            h, w = self._preview_bgra.shape[:2]
            stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, w)
            surface = cairo.ImageSurface.create_for_data(
                self._preview_bgra.data, cairo.FORMAT_ARGB32, w, h, stride
            )

            scale_w = width / float(w)
            scale_h = height / float(h)
            scale = min(scale_w, scale_h)

            offset_x = (width - w * scale) / 2.0
            offset_y = (height - h * scale) / 2.0

            cr.save()
            cr.set_source_rgb(0.08, 0.08, 0.08)
            cr.paint()

            cr.translate(offset_x, offset_y)
            cr.scale(scale, scale)
            cr.set_source_surface(surface, 0, 0)
            cr.paint()
            cr.restore()

    # --- Navigation Lifecycle ---

    def on_page_opened(self):
        logger.info("CameraManualView opened")
        # Check if Autodarts is active and pause it to avoid device lock conflicts
        try:
            if SystemdService.is_active("autodarts.service"):
                self.was_autodarts_active = True
                logger.info("Pausing autodarts.service to release V4L2 device locks")
                SystemdService.stop_service("autodarts.service")
                if hasattr(self.window, "show_toast"):
                    self.window.show_toast("Autodarts paused.")
            else:
                self.was_autodarts_active = False
        except Exception:
            logger.exception("Error checking autodarts.service state")

        self._start_capture()
        self._load_current_camera_controls()

        # Sync boot switch state
        is_boot = CameraManualService.is_boot_service_enabled()
        self._updating_boot_switch = True
        try:
            self.switch_boot.set_active(is_boot)
        finally:
            self._updating_boot_switch = False

    def on_page_closed(self):
        logger.info("CameraManualView closed: releasing camera")
        self._stop_capture.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=1.0)

        # Restore autodarts service if it was running before
        if self.was_autodarts_active:
            logger.info("Restoring autodarts.service")
            def _restore():
                SystemdService.start_service("autodarts.service")
            threading.Thread(target=_restore, daemon=True).start()
            self.was_autodarts_active = False
