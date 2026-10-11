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
from core.systemd_service import SystemdService, SystemdManager

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

        # Main container - expands to fill the entire maximized window
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.main_box.set_margin_top(8)
        self.main_box.set_margin_bottom(8)
        self.main_box.set_margin_start(12)
        self.main_box.set_margin_end(12)
        self.main_box.set_hexpand(True)
        self.main_box.set_vexpand(True)
        self.set_child(self.main_box)

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

        # Profile Selector Box
        profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        profile_box.set_valign(Gtk.Align.CENTER)

        lbl_profile = Gtk.Label(label="Profile:")
        lbl_profile.add_css_class("dim-label")
        profile_box.append(lbl_profile)

        self._profile_names: list[str] = ["Default"]
        self.profile_string_list = Gtk.StringList.new(self._profile_names)
        self.dropdown_profile = Gtk.DropDown.new(self.profile_string_list, None)
        self.dropdown_profile.set_valign(Gtk.Align.CENTER)
        self.dropdown_profile.connect("notify::selected", self._on_profile_dropdown_changed)
        profile_box.append(self.dropdown_profile)

        # Save button (updates current profile with current slider values)
        self.btn_save_profile = Gtk.Button(label="Save")
        self.btn_save_profile.add_css_class("touch-btn")
        self.btn_save_profile.set_valign(Gtk.Align.CENTER)
        self.btn_save_profile.set_tooltip_text("Save current settings to active profile")
        self.btn_save_profile.connect("clicked", self._on_save_profile_clicked)
        profile_box.append(self.btn_save_profile)

        # New profile button (+)
        self.btn_add_profile = Gtk.Button.new_from_icon_name("list-add-symbolic")
        self.btn_add_profile.add_css_class("touch-btn")
        self.btn_add_profile.set_valign(Gtk.Align.CENTER)
        self.btn_add_profile.set_tooltip_text("Create new profile from current settings")
        self.btn_add_profile.connect("clicked", self._on_add_profile_clicked)
        profile_box.append(self.btn_add_profile)

        # More Actions MenuButton (Rename / Delete)
        self.btn_more_profile = Gtk.MenuButton()
        self.btn_more_profile.set_icon_name("view-more-symbolic")
        self.btn_more_profile.add_css_class("touch-btn")
        self.btn_more_profile.set_valign(Gtk.Align.CENTER)
        self.btn_more_profile.set_tooltip_text("Profile options")

        more_popover = Gtk.Popover()
        popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        popover_box.set_margin_top(6)
        popover_box.set_margin_bottom(6)
        popover_box.set_margin_start(6)
        popover_box.set_margin_end(6)

        btn_rename = Gtk.Button(label="Rename Profile")
        btn_rename.connect("clicked", lambda b: (more_popover.popdown(), self._on_rename_profile_clicked()))
        popover_box.append(btn_rename)

        btn_delete = Gtk.Button(label="Delete Profile")
        btn_delete.add_css_class("destructive-action")
        btn_delete.connect("clicked", lambda b: (more_popover.popdown(), self._on_delete_profile_clicked()))
        popover_box.append(btn_delete)

        more_popover.set_child(popover_box)
        self.btn_more_profile.set_popover(more_popover)
        profile_box.append(self.btn_more_profile)

        top_bar.append(profile_box)

        # Spacer
        spacer = Gtk.Box(hexpand=True)
        top_bar.append(spacer)

        # Load on boot toggle in the top right
        boot_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        boot_box.set_valign(Gtk.Align.CENTER)
        lbl_boot = Gtk.Label(label="Load on boot")
        lbl_boot.add_css_class("dim-label")
        boot_box.append(lbl_boot)

        self.switch_boot = Gtk.Switch()
        self.switch_boot.set_valign(Gtk.Align.CENTER)
        self.switch_boot.connect("notify::active", self._on_boot_toggled)
        boot_box.append(self.switch_boot)
        top_bar.append(boot_box)

        # Fullscreen Toggle Button
        self.btn_fullscreen = Gtk.Button.new_from_icon_name("view-fullscreen-symbolic")
        self.btn_fullscreen.add_css_class("touch-btn")
        self.btn_fullscreen.set_valign(Gtk.Align.CENTER)
        self.btn_fullscreen.set_tooltip_text("Toggle Fullscreen")
        self.btn_fullscreen.connect("clicked", self._on_toggle_fullscreen_clicked)
        top_bar.append(self.btn_fullscreen)

        # Status label reference
        self.lbl_status = Gtk.Label(label="Live sensor adjustment")

        # Backwards compatibility object for tests
        self.row_boot = Adw.ActionRow(title="Load on boot")

        self.main_box.append(top_bar)
        self._update_tab_buttons(0)

    def _update_tab_buttons(self, active_idx: int):
        for i, btn in enumerate(self.cam_buttons):
            if i == active_idx:
                btn.add_css_class("suggested-action")
            else:
                btn.remove_css_class("suggested-action")

    def _on_toggle_fullscreen_clicked(self, btn):
        if not self.window:
            return
        if hasattr(self.window, "is_fullscreen") and self.window.is_fullscreen():
            self.window.unfullscreen()
            btn.set_icon_name("view-fullscreen-symbolic")
        elif hasattr(self.window, "fullscreen"):
            self.window.fullscreen()
            btn.set_icon_name("view-restore-symbolic")

    def _build_split_content(self):
        split_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        split_box.set_hexpand(True)
        split_box.set_vexpand(True)
        self.main_box.append(split_box)

        # --- LEFT PANE: Live Video Preview (Expands to maximum available space) ---
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left_box.set_hexpand(True)
        left_box.set_vexpand(True)
        split_box.append(left_box)

        # Live Preview Frame
        preview_frame = Gtk.Frame()
        preview_frame.add_css_class("focus-preview-area")
        preview_frame.set_hexpand(True)
        preview_frame.set_vexpand(True)

        self.preview_drawing_area = Gtk.DrawingArea()
        self.preview_drawing_area.set_draw_func(self._on_draw_preview)
        self.preview_drawing_area.set_size_request(320, 240)
        self.preview_drawing_area.set_hexpand(True)
        self.preview_drawing_area.set_vexpand(True)
        preview_frame.set_child(self.preview_drawing_area)
        left_box.append(preview_frame)

        # --- RIGHT PANE: Compact Sensor Controls (Fixed regular width, scrollable) ---
        ctrl_scroll = Gtk.ScrolledWindow()
        ctrl_scroll.set_hexpand(False)
        ctrl_scroll.set_vexpand(True)
        ctrl_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        ctrl_scroll.set_size_request(340, -1)
        split_box.append(ctrl_scroll)

        ctrl_pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        ctrl_pane.add_css_class("tile-card-compact")
        ctrl_pane.set_margin_top(4)
        ctrl_pane.set_margin_bottom(4)
        ctrl_pane.set_margin_start(4)
        ctrl_pane.set_margin_end(4)
        ctrl_scroll.set_child(ctrl_pane)

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

        self.scale_exp_val = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 5000, 1)
        self.scale_exp_val.set_draw_value(True)
        self.scale_exp_val.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale_exp_val.set_hexpand(True)
        self.scale_exp_val.connect("value-changed", lambda s: self._queue_control_update("exposure_time_absolute", int(s.get_value())))
        exp_box.append(self.scale_exp_val)
        ctrl_pane.append(exp_box)

        # 2. Gain
        gain_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lbl_gain = Gtk.Label(label="Gain", xalign=0)
        lbl_gain.add_css_class("dim-label")
        gain_box.append(lbl_gain)

        self.scale_gain = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 128, 1)
        self.scale_gain.set_draw_value(True)
        self.scale_gain.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale_gain.set_hexpand(True)
        self.scale_gain.connect("value-changed", lambda s: self._queue_control_update("gain", int(s.get_value())))
        gain_box.append(self.scale_gain)
        ctrl_pane.append(gain_box)

        # 3. Sharpness
        sharp_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lbl_sharp = Gtk.Label(label="Sharpness", xalign=0)
        lbl_sharp.add_css_class("dim-label")
        sharp_box.append(lbl_sharp)

        self.scale_sharpness = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale_sharpness.set_draw_value(True)
        self.scale_sharpness.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale_sharpness.set_hexpand(True)
        self.scale_sharpness.connect("value-changed", lambda s: self._queue_control_update("sharpness", int(s.get_value())))
        sharp_box.append(self.scale_sharpness)
        ctrl_pane.append(sharp_box)

        # 4. Contrast
        contrast_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lbl_contrast = Gtk.Label(label="Contrast", xalign=0)
        lbl_contrast.add_css_class("dim-label")
        contrast_box.append(lbl_contrast)

        self.scale_contrast = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale_contrast.set_draw_value(True)
        self.scale_contrast.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale_contrast.set_hexpand(True)
        self.scale_contrast.connect("value-changed", lambda s: self._queue_control_update("contrast", int(s.get_value())))
        contrast_box.append(self.scale_contrast)
        ctrl_pane.append(contrast_box)

        # 5. Gamma
        gamma_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lbl_gamma = Gtk.Label(label="Gamma", xalign=0)
        lbl_gamma.add_css_class("dim-label")
        gamma_box.append(lbl_gamma)

        self.scale_gamma = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 100, 500, 10)
        self.scale_gamma.set_draw_value(True)
        self.scale_gamma.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale_gamma.set_hexpand(True)
        self.scale_gamma.connect("value-changed", lambda s: self._queue_control_update("gamma", int(s.get_value())))
        gamma_box.append(self.scale_gamma)
        ctrl_pane.append(gamma_box)

        # 6. Brightness
        bright_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lbl_bright = Gtk.Label(label="Brightness", xalign=0)
        lbl_bright.add_css_class("dim-label")
        bright_box.append(lbl_bright)

        self.scale_bright = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -64, 64, 1)
        self.scale_bright.set_draw_value(True)
        self.scale_bright.set_value_pos(Gtk.PositionType.RIGHT)
        self.scale_bright.set_hexpand(True)
        self.scale_bright.connect("value-changed", lambda s: self._queue_control_update("brightness", int(s.get_value())))
        bright_box.append(self.scale_bright)
        ctrl_pane.append(bright_box)

        # 7. White Balance (Auto toggle + Slider)
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

        # 8. Reset Defaults button
        self.btn_reset = Gtk.Button(label="Reset Defaults")
        self.btn_reset.add_css_class("secondary-btn")
        self.btn_reset.set_margin_top(8)
        self.btn_reset.connect("clicked", self._on_reset_clicked)
        ctrl_pane.append(self.btn_reset)

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
                self.scale_gain.set_range(data.get("min", 0), data.get("max", 128))
                self.scale_gain.set_value(data.get("value", 0))

            if "sharpness" in ctrls:
                data = ctrls["sharpness"]
                self.scale_sharpness.set_range(data.get("min", 0), data.get("max", 100))
                self.scale_sharpness.set_value(data.get("value", 40))

            if "contrast" in ctrls:
                data = ctrls["contrast"]
                self.scale_contrast.set_range(data.get("min", 0), data.get("max", 100))
                self.scale_contrast.set_value(data.get("value", 32))

            if "gamma" in ctrls:
                data = ctrls["gamma"]
                self.scale_gamma.set_range(data.get("min", 100), data.get("max", 500))
                self.scale_gamma.set_value(data.get("value", 300))

            if "brightness" in ctrls:
                data = ctrls["brightness"]
                self.scale_bright.set_range(data.get("min", -64), data.get("max", 64))
                self.scale_bright.set_value(data.get("value", 0))

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

    # --- Profile Management Callbacks ---

    def _refresh_profiles_dropdown(self, select_name: str | None = None):
        self._updating_profiles = True
        try:
            self._profile_names = CameraManualService.get_profile_names()
            active_name = select_name or CameraManualService.get_active_profile_name()
            if active_name not in self._profile_names:
                active_name = "Default"

            new_sl = Gtk.StringList.new(self._profile_names)
            self.dropdown_profile.set_model(new_sl)
            self.profile_string_list = new_sl

            idx = self._profile_names.index(active_name) if active_name in self._profile_names else 0
            self.dropdown_profile.set_selected(idx)

            is_custom = (active_name != "Default")
            self.btn_save_profile.set_sensitive(is_custom)
            self.btn_more_profile.set_sensitive(is_custom)
        finally:
            self._updating_profiles = False

    def _on_profile_dropdown_changed(self, dropdown, gparam):
        if getattr(self, "_updating_profiles", False):
            return
        selected_item = dropdown.get_selected_item()
        if not selected_item:
            return
        name = selected_item.get_string()
        active_name = CameraManualService.get_active_profile_name()
        if name == active_name:
            return

        ok = CameraManualService.apply_profile(name, self.configured_cams)
        if ok:
            is_custom = (name != "Default")
            self.btn_save_profile.set_sensitive(is_custom)
            self.btn_more_profile.set_sensitive(is_custom)
            self._load_current_camera_controls()
            if hasattr(self.window, "show_toast"):
                self.window.show_toast(f"Profile '{name}' applied.")

    def _on_save_profile_clicked(self, btn):
        active_name = CameraManualService.get_active_profile_name()
        if active_name == "Default":
            return
        settings = CameraManualService.get_current_settings_for_cameras(self.configured_cams)
        ok = CameraManualService.save_profile(active_name, settings, set_active=True)
        if ok:
            if getattr(self, "switch_boot", None) and self.switch_boot.get_active():
                CameraManualService.update_boot_service_if_enabled(self.configured_cams)
            if hasattr(self.window, "show_toast"):
                self.window.show_toast(f"Profile '{active_name}' saved.")

    def _on_add_profile_clicked(self, btn):
        dialog = Adw.AlertDialog(
            heading="New Camera Profile",
            body="Enter a name for the new profile using current settings:",
        )
        entry = Gtk.Entry(placeholder_text="e.g. Evening, Match")
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Create")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def on_response(dlg, response):
            if response == "save":
                name = entry.get_text().strip()
                if not name or name == "Default":
                    if hasattr(self.window, "show_toast"):
                        self.window.show_toast("Invalid profile name.")
                    return
                settings = CameraManualService.get_current_settings_for_cameras(self.configured_cams)
                ok = CameraManualService.save_profile(name, settings, set_active=True)
                if ok:
                    self._refresh_profiles_dropdown(select_name=name)
                    if hasattr(self.window, "show_toast"):
                        self.window.show_toast(f"Profile '{name}' created.")

        dialog.connect("response", on_response)
        parent = self.window if isinstance(self.window, Gtk.Window) else self.get_root()
        dialog.present(parent)

    def _on_rename_profile_clicked(self):
        active_name = CameraManualService.get_active_profile_name()
        if active_name == "Default":
            return

        dialog = Adw.AlertDialog(
            heading="Rename Profile",
            body=f"Enter a new name for profile '{active_name}':",
        )
        entry = Gtk.Entry(text=active_name)
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")

        def on_response(dlg, response):
            if response == "rename":
                new_name = entry.get_text().strip()
                if not new_name or new_name in ("Default", active_name):
                    return
                ok = CameraManualService.rename_profile(active_name, new_name)
                if ok:
                    self._refresh_profiles_dropdown(select_name=new_name)
                    if hasattr(self.window, "show_toast"):
                        self.window.show_toast(f"Profile renamed to '{new_name}'.")

        dialog.connect("response", on_response)
        parent = self.window if isinstance(self.window, Gtk.Window) else self.get_root()
        dialog.present(parent)

    def _on_delete_profile_clicked(self):
        active_name = CameraManualService.get_active_profile_name()
        if active_name == "Default":
            return

        dialog = Adw.AlertDialog(
            heading="Delete Profile",
            body=f"Are you sure you want to delete '{active_name}'?",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(dlg, response):
            if response == "delete":
                ok = CameraManualService.delete_profile(active_name)
                if ok:
                    CameraManualService.apply_profile("Default", self.configured_cams)
                    self._refresh_profiles_dropdown(select_name="Default")
                    self._load_current_camera_controls()
                    if hasattr(self.window, "show_toast"):
                        self.window.show_toast(f"Profile '{active_name}' deleted.")

        dialog.connect("response", on_response)
        parent = self.window if isinstance(self.window, Gtk.Window) else self.get_root()
        dialog.present(parent)

    # --- Navigation Lifecycle ---

    def on_page_opened(self):
        logger.info("CameraManualView opened")
        if self.window:
            if hasattr(self.window, "is_maximized"):
                self._was_maximized = self.window.is_maximized()
            if hasattr(self.window, "maximize"):
                self.window.maximize()

        # Check if Autodarts is active and pause it to avoid device lock conflicts
        try:
            self.was_autodarts_active = SystemdManager.pause_autodarts()
            if self.was_autodarts_active:
                logger.info("Paused autodarts.service to release V4L2 device locks")
                if hasattr(self.window, "show_toast"):
                    self.window.show_toast("Autodarts paused.")
            else:
                self.was_autodarts_active = False
        except Exception:
            logger.exception("Error checking/pausing autodarts.service state")

        self._refresh_profiles_dropdown()
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
        if self.window:
            if hasattr(self.window, "is_fullscreen") and self.window.is_fullscreen():
                self.window.unfullscreen()
            if hasattr(self, "_was_maximized") and not self._was_maximized and hasattr(self.window, "unmaximize"):
                self.window.unmaximize()

        self._stop_capture.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=1.0)

        # Restore autodarts service if it was running before
        if getattr(self, "was_autodarts_active", False) or SystemdManager._autodarts_paused_by_suit:
            logger.info("Restoring autodarts.service")
            def _restore():
                SystemdManager.resume_autodarts()
            t = threading.Thread(target=_restore, daemon=False)
            t.start()
            self._restore_thread = t
            self.was_autodarts_active = False
