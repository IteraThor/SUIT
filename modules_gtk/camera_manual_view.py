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

        clamp = Adw.Clamp(maximum_size=960, tightening_threshold=720)
        clamp.set_margin_top(14)
        clamp.set_margin_bottom(20)
        clamp.set_margin_start(14)
        clamp.set_margin_end(14)
        scrolled.set_child(clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        clamp.set_child(self.main_box)

        # Header card
        self._build_header_card()

        # Camera selection tabs
        self._build_camera_tabs()

        # Live Preview Box
        self._build_preview_canvas()

        # Control sliders group
        self._build_controls_group()

        # Startup boot group
        self._build_boot_group()

        # Footer action buttons
        self._build_footer_actions()

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

    def _build_header_card(self):
        header_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        header_card.add_css_class("card")
        header_card.set_margin_bottom(4)

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        inner.set_margin_top(12)
        inner.set_margin_bottom(12)
        inner.set_margin_start(16)
        inner.set_margin_end(16)
        inner.set_hexpand(True)

        icon = Gtk.Image.new_from_icon_name("camera-web-symbolic")
        icon.set_pixel_size(36)
        icon.set_valign(Gtk.Align.CENTER)
        inner.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)

        self.lbl_title = Gtk.Label(label="Camera Manual Mode", xalign=0)
        self.lbl_title.add_css_class("heading")
        text_box.append(self.lbl_title)

        self.lbl_status = Gtk.Label(label="Live sensor adjustment", xalign=0)
        self.lbl_status.add_css_class("dim-label")
        text_box.append(self.lbl_status)
        inner.append(text_box)

        header_card.append(inner)
        self.main_box.append(header_card)

    def _build_camera_tabs(self):
        tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tab_box.set_homogeneous(True)
        tab_box.set_margin_bottom(4)

        self.cam_buttons = []
        for i, name in enumerate(CAMERA_NAMES):
            btn = Gtk.Button(label=name)
            btn.add_css_class("touch-btn")
            btn.connect("clicked", lambda b, idx=i: self._on_select_camera(idx))
            tab_box.append(btn)
            self.cam_buttons.append(btn)

        self.main_box.append(tab_box)
        self._update_tab_buttons(0)

    def _update_tab_buttons(self, active_idx: int):
        for i, btn in enumerate(self.cam_buttons):
            if i == active_idx:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

    def _build_preview_canvas(self):
        preview_frame = Gtk.Frame()
        preview_frame.add_css_class("card")

        self.preview_drawing_area = Gtk.DrawingArea()
        self.preview_drawing_area.set_draw_func(self._on_draw_preview)
        self.preview_drawing_area.set_size_request(-1, 300)
        self.preview_drawing_area.set_hexpand(True)
        preview_frame.set_child(self.preview_drawing_area)

        self.main_box.append(preview_frame)

    def _build_controls_group(self):
        self.ctrl_group = Adw.PreferencesGroup()
        self.ctrl_group.set_title("Hardware Sensor Controls")
        self.main_box.append(self.ctrl_group)

        # 1. Exposure Mode Switch
        self.row_exp_mode = Adw.ActionRow(title="Automatic Exposure")
        self.row_exp_mode.set_subtitle("Disable for manual shutter speed control")
        self.switch_exp_auto = Gtk.Switch()
        self.switch_exp_auto.set_valign(Gtk.Align.CENTER)
        self.switch_exp_auto.connect("notify::active", self._on_exposure_mode_changed)
        self.row_exp_mode.add_suffix(self.switch_exp_auto)
        self.ctrl_group.add(self.row_exp_mode)

        # 2. Exposure Time Scale
        self.row_exp_val = Adw.ActionRow(title="Exposure Time")
        self.scale_exp_val = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 5000, 10)
        self.scale_exp_val.set_hexpand(True)
        self.scale_exp_val.set_draw_value(True)
        self.scale_exp_val.connect("value-changed", lambda s: self._queue_control_update("exposure_time_absolute", int(s.get_value())))
        self.row_exp_val.add_suffix(self.scale_exp_val)
        self.ctrl_group.add(self.row_exp_val)

        # 3. Gain Scale
        self.row_gain = Adw.ActionRow(title="Sensor Gain")
        self.scale_gain = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale_gain.set_hexpand(True)
        self.scale_gain.set_draw_value(True)
        self.scale_gain.connect("value-changed", lambda s: self._queue_control_update("gain", int(s.get_value())))
        self.row_gain.add_suffix(self.scale_gain)
        self.ctrl_group.add(self.row_gain)

        # 4. Brightness Scale
        self.row_bright = Adw.ActionRow(title="Brightness")
        self.scale_bright = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -64, 64, 1)
        self.scale_bright.set_hexpand(True)
        self.scale_bright.set_draw_value(True)
        self.scale_bright.connect("value-changed", lambda s: self._queue_control_update("brightness", int(s.get_value())))
        self.row_bright.add_suffix(self.scale_bright)
        self.ctrl_group.add(self.row_bright)

        # 5. Contrast Scale
        self.row_contrast = Adw.ActionRow(title="Contrast")
        self.scale_contrast = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 64, 1)
        self.scale_contrast.set_hexpand(True)
        self.scale_contrast.set_draw_value(True)
        self.scale_contrast.connect("value-changed", lambda s: self._queue_control_update("contrast", int(s.get_value())))
        self.row_contrast.add_suffix(self.scale_contrast)
        self.ctrl_group.add(self.row_contrast)

        # 6. Auto White Balance Switch
        self.row_wb_mode = Adw.ActionRow(title="Auto White Balance")
        self.switch_wb_auto = Gtk.Switch()
        self.switch_wb_auto.set_valign(Gtk.Align.CENTER)
        self.switch_wb_auto.connect("notify::active", self._on_wb_mode_changed)
        self.row_wb_mode.add_suffix(self.switch_wb_auto)
        self.ctrl_group.add(self.row_wb_mode)

        # 7. White Balance Temperature Scale
        self.row_wb_temp = Adw.ActionRow(title="Color Temperature (K)")
        self.scale_wb_temp = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 2800, 6500, 50)
        self.scale_wb_temp.set_hexpand(True)
        self.scale_wb_temp.set_draw_value(True)
        self.scale_wb_temp.connect("value-changed", lambda s: self._queue_control_update("white_balance_temperature", int(s.get_value())))
        self.row_wb_temp.add_suffix(self.scale_wb_temp)
        self.ctrl_group.add(self.row_wb_temp)

    def _build_boot_group(self):
        self.boot_group = Adw.PreferencesGroup()
        self.row_boot = Adw.ActionRow(title="Load on boot")
        self.row_boot.set_subtitle("Apply camera values at startup")
        self.switch_boot = Gtk.Switch()
        self.switch_boot.set_valign(Gtk.Align.CENTER)
        self.switch_boot.connect("notify::active", self._on_boot_toggled)
        self.row_boot.add_suffix(self.switch_boot)
        self.boot_group.add(self.row_boot)
        self.main_box.append(self.boot_group)

    def _build_footer_actions(self):
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        action_box.set_margin_top(4)

        self.btn_reset = Gtk.Button(label="Reset Defaults")
        self.btn_reset.add_css_class("flat")
        self.btn_reset.add_css_class("touch-btn")
        self.btn_reset.set_hexpand(True)
        self.btn_reset.connect("clicked", self._on_reset_clicked)
        action_box.append(self.btn_reset)

        self.main_box.append(action_box)

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
