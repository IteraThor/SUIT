"""Camera Focus Tool GTK4 / Libadwaita presentation view.

Implements the Oscilloscope Focus Method for Autodarts cameras:
- Automatic target selection (Furthest Double, Triple 20, or custom tap).
- Real-time Cairo Oscilloscope HUD with 100-sample relative timeline and peak latch.
- 2.5x Loupe PIP overlay with zero clutter.
- Overshoot / Turn Back detection and instant camera switching.
"""

import math
import threading
import time
import numpy as np
import cairo

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from core.logger import get_logger
from core.camera_focus_service import (
    CameraFocusService,
    FocusTrend,
    FocusAnalysis,
    LightingState,
    TargetMode,
)
from core.audio_tone_service import AudioToneService
from core.autodarts_service import read_cam_config
from modules_gtk.ui_helpers import build_page_shell
from core.systemd_service import SystemdService, SystemdManager
from core.i18n import tr

logger = get_logger("camera_focus_view")

CAMERA_NAMES = ["Camera 1", "Camera 2", "Camera 3"]


class CameraFocusView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title=tr("Camera Focus Tool"), tag="camera_focus")
        self.window = window

        self.focus_service = CameraFocusService()
        self.audio_service = AudioToneService()

        self.current_step = 0  # 0: Cam 1, 1: Cam 2, 2: Cam 3
        self.configured_cams = ["/dev/video0", "/dev/video2", "/dev/video4"]
        self.raw_cams = []
        self.camera_results = [
            {"score": 0.0, "peak": 0.0, "target": "Unset"},
            {"score": 0.0, "peak": 0.0, "target": "Unset"},
            {"score": 0.0, "peak": 0.0, "target": "Unset"},
        ]
        self.was_autodarts_active = False

        self.zoom_factor = 2.5

        # Live capture state
        self._stop_capture = threading.Event()
        self._capture_thread = None
        self._current_frame: np.ndarray | None = None
        self._current_analysis: FocusAnalysis | None = None
        self._lock = threading.Lock()
        self._is_switching_camera = False
        # Camera stays closed until the user picks a target (or "Pick on Video")
        self._camera_open = False
        self._pending_target = None

        self._preview_bgra: np.ndarray | None = None
        self._zoom_bgra: np.ndarray | None = None
        self._was_maximized = False
        self.peaking_enabled = True

        # Container
        scrolled, self.main_box = build_page_shell(max_width=1380)
        self.main_box.set_spacing(10)  # dense live-view layout
        self.set_child(scrolled)

        # Header Bar
        self._build_header_card()

        # Camera Tab Selector Bar
        self._build_camera_tab_bar()

        # ViewStack
        self.stack = Adw.ViewStack()
        self.main_box.append(self.stack)

        # Page 1: Focus Tuning Page
        self.focus_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.stack.add_named(self.focus_page, "cam_focus")

        # Page 2: Summary Page
        self.summary_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.stack.add_named(self.summary_page, "summary")

        self._build_focus_screen()
        self._build_summary_screen()

    def _build_header_card(self):
        """Build top navigation and title bar."""
        header_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header_card.add_css_class("tile-card-compact")
        self.main_box.append(header_card)

        step_icon = Gtk.Image.new_from_icon_name("camera-photo-symbolic")
        step_icon.set_pixel_size(24)
        step_icon.set_valign(Gtk.Align.CENTER)
        header_card.append(step_icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_hexpand(True)
        header_card.append(text_box)

        self.lbl_step_title = Gtk.Label(label=tr("Camera 1"), xalign=0)
        self.lbl_step_title.add_css_class("title-4")
        text_box.append(self.lbl_step_title)

        self.lbl_step_desc = Gtk.Label(
            label=tr("Optical focus tuning with real-time oscilloscope tracking."),
            xalign=0,
            wrap=True,
        )
        self.lbl_step_desc.add_css_class("dim-label")
        text_box.append(self.lbl_step_desc)

        # Audio mute toggle button (muted by default)
        self.btn_mute = Gtk.Button.new_from_icon_name("audio-volume-muted-symbolic")
        self.btn_mute.add_css_class("flat")
        self.btn_mute.set_size_request(40, 36)
        self.btn_mute.set_valign(Gtk.Align.CENTER)
        self.btn_mute.set_tooltip_text(tr("Unmute Audio Guidance"))
        self.btn_mute.connect("clicked", self._on_mute_clicked)
        header_card.append(self.btn_mute)

    def _build_camera_tab_bar(self):
        """Build camera selector tabs."""
        self.cam_tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.cam_tab_bar.add_css_class("camera-tab-bar")
        self.cam_tab_bar.set_homogeneous(True)
        self.main_box.append(self.cam_tab_bar)

        self.cam_tab_buttons = []
        for i, cam_name in enumerate(CAMERA_NAMES):
            btn = Gtk.Button(label=tr(cam_name))
            btn.add_css_class("flat")
            btn.add_css_class("camera-tab-btn")
            if i == 0:
                btn.add_css_class("active-tab")
            btn.connect("clicked", lambda b, idx=i: self._on_cam_tab_clicked(idx))
            self.cam_tab_bar.append(btn)
            self.cam_tab_buttons.append(btn)

    def _build_focus_screen(self):
        """Build target selection popup overlay, video preview area, and oscilloscope HUD."""
        # 1. Active Focus Status Banner (Shown at top when target is actively calibrated)
        self.active_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.active_banner.add_css_class("tile-card-compact")
        self.active_banner.set_visible(False)
        self.focus_page.append(self.active_banner)

        banner_text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        banner_text_box.set_hexpand(True)
        self.active_banner.append(banner_text_box)

        self.active_banner_title = Gtk.Label(
            label=tr("Rotate focus ring slowly. Look for highest peak."),
            xalign=0,
            wrap=True,
        )
        self.active_banner_title.add_css_class("title-4")
        banner_text_box.append(self.active_banner_title)

        banner_info_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        banner_text_box.append(banner_info_row)

        self.target_pill = Gtk.Label(label=tr("Target: Furthest Double"), xalign=0)
        self.target_pill.add_css_class("usb-path-tag")
        banner_info_row.append(self.target_pill)

        self.trend_badge = Gtk.Label(label=tr("Tuning"), xalign=0)
        self.trend_badge.add_css_class("focus-trend-badge")
        self.trend_badge.add_css_class("stable")
        banner_info_row.append(self.trend_badge)

        # Focus Peaking / Contour Filter Toggle Button
        self.btn_toggle_peaking = Gtk.Button(label=tr("Peaking: ON"))
        self.btn_toggle_peaking.add_css_class("suggested-action")
        self.btn_toggle_peaking.set_valign(Gtk.Align.CENTER)
        self.btn_toggle_peaking.set_tooltip_text(tr("Toggle Green Focus Peaking Contour Overlay"))
        self.btn_toggle_peaking.connect("clicked", lambda b: self._on_toggle_peaking())
        self.active_banner.append(self.btn_toggle_peaking)

        self.btn_change_target = Gtk.Button(label=tr("Change Target"))
        self.btn_change_target.add_css_class("flat")
        self.btn_change_target.set_valign(Gtk.Align.CENTER)
        self.btn_change_target.connect("clicked", lambda b: self._show_target_prompt())
        self.active_banner.append(self.btn_change_target)

        # 2. Main Live Video Preview Frame
        video_frame = Gtk.Frame()
        video_frame.set_hexpand(True)
        video_frame.set_vexpand(True)
        video_frame.add_css_class("focus-preview-area")
        self.focus_page.append(video_frame)

        self.video_overlay = Gtk.Overlay()
        video_frame.set_child(self.video_overlay)

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_draw_func(self._on_draw_preview)
        self.drawing_area.set_size_request(320, 240)
        self.drawing_area.set_hexpand(True)
        self.drawing_area.set_vexpand(True)

        # Interactive click gesture to place ROI anywhere on the video
        click_gesture = Gtk.GestureClick()
        click_gesture.connect("released", self._on_preview_clicked)
        self.drawing_area.add_controller(click_gesture)

        self.video_overlay.set_child(self.drawing_area)

        # 3. Target Selection Popup Overlay on Video Feed
        self.target_prompt_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.target_prompt_card.add_css_class("focus-target-overlay-card")
        self.target_prompt_card.set_halign(Gtk.Align.CENTER)
        self.target_prompt_card.set_valign(Gtk.Align.CENTER)
        self.target_prompt_card.set_margin_start(16)
        self.target_prompt_card.set_margin_end(16)
        self.target_prompt_card.set_margin_top(16)
        self.target_prompt_card.set_margin_bottom(16)

        prompt_title = Gtk.Label(label=tr("Select Focus Target"), xalign=0.5)
        prompt_title.add_css_class("title-3")
        self.target_prompt_card.append(prompt_title)

        prompt_sub = Gtk.Label(
            label=tr("Choose where to focus. The camera only starts after you pick:"),
            xalign=0.5,
            justify=Gtk.Justification.CENTER,
            wrap=True,
        )
        prompt_sub.add_css_class("dim-label")
        self.target_prompt_card.append(prompt_sub)

        # Target selection action buttons
        target_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        target_btn_row.set_margin_top(6)
        target_btn_row.set_homogeneous(True)
        self.target_prompt_card.append(target_btn_row)

        self.btn_target_furthest = Gtk.Button(label=tr("Furthest Double (Recommended)"))
        self.btn_target_furthest.add_css_class("suggested-action")
        self.btn_target_furthest.add_css_class("touch-btn")
        self.btn_target_furthest.set_size_request(-1, 42)
        self.btn_target_furthest.connect("clicked", lambda b: self._select_target_mode(TargetMode.FURTHEST_DOUBLE))
        target_btn_row.append(self.btn_target_furthest)

        self.btn_target_t20 = Gtk.Button(label=tr("Triple 20 (T20)"))
        self.btn_target_t20.add_css_class("secondary-btn")
        self.btn_target_t20.add_css_class("touch-btn")
        self.btn_target_t20.set_size_request(-1, 42)
        self.btn_target_t20.connect("clicked", lambda b: self._select_target_mode(TargetMode.T20))
        target_btn_row.append(self.btn_target_t20)

        self.btn_target_pick = Gtk.Button(label=tr("Pick Point on Video"))
        self.btn_target_pick.add_css_class("secondary-btn")
        self.btn_target_pick.add_css_class("touch-btn")
        self.btn_target_pick.set_size_request(-1, 42)
        self.btn_target_pick.connect("clicked", lambda b: self._select_pick_mode())
        self.target_prompt_card.append(self.btn_target_pick)

        tip_lbl = Gtk.Label(
            label=tr("Tip: Insert a dart into the target segment for maximum edge contrast."),
            xalign=0.5,
            justify=Gtk.Justification.CENTER,
            wrap=True,
        )
        tip_lbl.add_css_class("dim-label")
        tip_lbl.set_margin_top(4)
        self.target_prompt_card.append(tip_lbl)

        self.video_overlay.add_overlay(self.target_prompt_card)

        # 4. Loading Spinner Overlay
        self.loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.loading_box.add_css_class("focus-loading-overlay")
        self.loading_box.set_valign(Gtk.Align.CENTER)
        self.loading_box.set_halign(Gtk.Align.CENTER)
        self.loading_box.set_margin_top(20)
        self.loading_box.set_margin_bottom(20)
        self.loading_box.set_margin_start(20)
        self.loading_box.set_margin_end(20)

        self.loading_spinner = Gtk.Spinner()
        self.loading_spinner.set_size_request(36, 36)
        self.loading_box.append(self.loading_spinner)

        self.loading_lbl = Gtk.Label(label=tr("Stopping Autodarts & Opening Camera 1..."))
        self.loading_lbl.add_css_class("title-4")
        self.loading_box.append(self.loading_lbl)

        self.video_overlay.add_overlay(self.loading_box)

        # 4. Oscilloscope HUD Card (Waveform graph, peak line, and live metrics)
        self.osc_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.osc_card.add_css_class("focus-zoom-card")
        self.osc_card.set_visible(False)
        self.focus_page.append(self.osc_card)

        osc_header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.osc_card.append(osc_header_row)

        osc_title = Gtk.Label(label=tr("Relative Sharpness Timeline"), xalign=0)
        osc_title.add_css_class("title-4")
        osc_header_row.append(osc_title)

        self.osc_peak_lbl = Gtk.Label(label=tr("Peak: 0.0%"), xalign=1)
        self.osc_peak_lbl.add_css_class("dim-label")
        self.osc_peak_lbl.set_hexpand(True)
        self.osc_peak_lbl.set_tooltip_text(tr("Click to reset peak"))
        osc_header_row.append(self.osc_peak_lbl)

        peak_click = Gtk.GestureClick()
        peak_click.connect("released", lambda *args: self.focus_service.reset_peak())
        self.osc_peak_lbl.add_controller(peak_click)

        self.osc_drawing_area = Gtk.DrawingArea()
        self.osc_drawing_area.set_draw_func(self._on_draw_oscilloscope)
        self.osc_drawing_area.set_size_request(-1, 110)
        self.osc_drawing_area.set_hexpand(True)
        self.osc_card.append(self.osc_drawing_area)

        # 5. Bottom Navigation / Finish Controls
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bottom_bar.set_margin_top(4)
        self.focus_page.append(bottom_bar)

        self.btn_reset_peak = Gtk.Button(label=tr("Reset Peak"))
        self.btn_reset_peak.add_css_class("secondary-btn")
        self.btn_reset_peak.connect("clicked", lambda b: self.focus_service.reset_peak())
        bottom_bar.append(self.btn_reset_peak)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bottom_bar.append(spacer)

        self.btn_finish = Gtk.Button(label=tr("Finish"))
        self.btn_finish.add_css_class("suggested-action")
        self.btn_finish.add_css_class("touch-btn")
        self.btn_finish.set_size_request(130, 42)
        self.btn_finish.connect("clicked", self._on_finish_clicked)
        bottom_bar.append(self.btn_finish)

    def _build_summary_screen(self):
        """Build summary verification screen."""
        summary_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.summary_page.append(summary_box)

        header_banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header_banner.add_css_class("tile-card")
        summary_box.append(header_banner)

        lbl_sum_h = Gtk.Label(label=tr("Focus Calibration Summary"), xalign=0)
        lbl_sum_h.add_css_class("title-2")
        header_banner.append(lbl_sum_h)

        lbl_sum_desc = Gtk.Label(
            label=tr("Review recorded sharpness across all 3 cameras before resuming Autodarts."),
            xalign=0,
        )
        lbl_sum_desc.add_css_class("dim-label")
        header_banner.append(lbl_sum_desc)

        self.summary_cards_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.summary_cards_box.set_homogeneous(True)
        summary_box.append(self.summary_cards_box)

        self.sum_tiles = []
        for i, cam_name in enumerate(CAMERA_NAMES):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.add_css_class("card")
            card.set_margin_top(8)
            card.set_margin_bottom(8)
            card.set_margin_start(8)
            card.set_margin_end(8)

            title = Gtk.Label(label=tr(cam_name), xalign=0)
            title.add_css_class("title-3")
            card.append(title)

            score_lbl = Gtk.Label(label=tr("Status: Not tuned"), xalign=0)
            score_lbl.add_css_class("title-4")
            card.append(score_lbl)

            btn_refocus = Gtk.Button(label=tr("Refocus"))
            btn_refocus.add_css_class("secondary-btn")
            btn_refocus.connect("clicked", lambda b, idx=i: self._jump_to_cam(idx))
            card.append(btn_refocus)

            self.summary_cards_box.append(card)
            self.sum_tiles.append((score_lbl, btn_refocus))

        self.btn_sum_finish = Gtk.Button(label=tr("Finish"))
        self.btn_sum_finish.add_css_class("suggested-action")
        self.btn_sum_finish.add_css_class("touch-btn")
        self.btn_sum_finish.set_size_request(-1, 44)
        self.btn_sum_finish.set_margin_top(8)
        self.btn_sum_finish.connect("clicked", self._on_finish_clicked)
        summary_box.append(self.btn_sum_finish)

    def _on_toggle_peaking(self):
        """Toggle green focus peaking edge contour overlay."""
        self.peaking_enabled = not self.peaking_enabled
        if hasattr(self, "btn_toggle_peaking"):
            self.btn_toggle_peaking.set_label(tr("Peaking: ON") if self.peaking_enabled else tr("Peaking: OFF"))
            if self.peaking_enabled:
                self.btn_toggle_peaking.add_css_class("suggested-action")
                self.btn_toggle_peaking.remove_css_class("flat")
            else:
                self.btn_toggle_peaking.remove_css_class("suggested-action")
                self.btn_toggle_peaking.add_css_class("flat")
        self.drawing_area.queue_draw()

    def _select_target_mode(self, mode: TargetMode, custom_point: tuple[int, int] | None = None):
        """Transition into active focus mode with the chosen target."""
        if not self._camera_open:
            self._open_camera_then(("target", mode, custom_point))
            return

        with self._lock:
            frame = self._current_frame
            f_shape = frame.shape[:2] if frame is not None else (960, 1280)

        self.focus_service.set_target_mode(mode, custom_point=custom_point, frame_shape=f_shape)

        self.target_prompt_card.set_visible(False)
        self.active_banner.set_visible(True)
        self.osc_card.set_visible(True)

        self.active_banner_title.set_label(tr("Rotate focus ring slowly. Look for highest peak."))
        self.target_pill.set_label(tr("Target: {target}").format(target=tr(self.focus_service.target_name)))
        self.drawing_area.queue_draw()
        self.osc_drawing_area.queue_draw()

    def _select_pick_mode(self):
        """Start the preview so the user can click the focus point on the video."""
        if not self._camera_open:
            self._open_camera_then(("pick",))
            return
        self._enter_pick_ui()

    def _enter_pick_ui(self):
        self.focus_service.set_target_mode(TargetMode.NONE)
        self.target_prompt_card.set_visible(False)
        self.active_banner.set_visible(True)
        self.osc_card.set_visible(False)
        self.active_banner_title.set_label(tr("Click on the video where you want to focus."))
        self.target_pill.set_label(tr("Target: not set"))
        self.drawing_area.queue_draw()

    def _open_camera_then(self, pending):
        """Stop Autodarts and open the current camera, then apply the pending selection."""
        if self._is_switching_camera:
            return
        self._is_switching_camera = True
        self._pending_target = pending
        idx = self.current_step
        cam_name = tr(CAMERA_NAMES[idx])
        self.target_prompt_card.set_visible(False)
        self.loading_lbl.set_label(tr("Stopping Autodarts & Opening {camera}...").format(camera=cam_name))
        self.loading_box.set_visible(True)
        self.loading_spinner.start()

        def _bg_open():
            if not SystemdManager._autodarts_paused_by_suit and not self.was_autodarts_active:
                try:
                    self.was_autodarts_active = SystemdManager.pause_autodarts()
                    if self.was_autodarts_active:
                        time.sleep(0.4)
                except Exception:
                    logger.exception("Error querying/stopping autodarts.service")
                try:
                    import subprocess
                    subprocess.run(["pkill", "-TERM", "-f", r"(^|/)(ad|autodarts)(\s|$)"], timeout=2)
                    time.sleep(0.3)
                except Exception:
                    pass

            from core.autodarts_service import parse_cam_device
            cfg = read_cam_config()
            raw_cams = cfg.get("cams", [])
            self.configured_cams = [parse_cam_device(c) for c in raw_cams if c and c.strip()]
            self.raw_cams = [c for c in cfg.get("devices", []) if c and c.strip()]
            if not self.configured_cams:
                self.configured_cams = ["/dev/video0", "/dev/video2", "/dev/video4"]

            self.audio_service.start()

            dev_path = (
                self.configured_cams[idx] if idx < len(self.configured_cams) else f"/dev/video{idx * 2}"
            )
            cam_uri = self.raw_cams[idx] if idx < len(self.raw_cams) else None
            self.focus_service.start_camera(dev_path, uri=cam_uri)
            GLib.idle_add(self._on_camera_opened)

        threading.Thread(target=_bg_open, daemon=True).start()

    def _on_camera_opened(self):
        self._is_switching_camera = False
        self._camera_open = True
        self.loading_spinner.stop()
        self.loading_box.set_visible(False)
        self._start_capture_thread()
        pending, self._pending_target = self._pending_target, None
        if pending and pending[0] == "target":
            self._select_target_mode(pending[1], custom_point=pending[2])
        elif pending and pending[0] == "pick":
            self._enter_pick_ui()
        return False

    def _show_target_prompt(self):
        """Switch UI back to target selection prompt."""
        self.focus_service.set_target_mode(TargetMode.NONE)
        self.target_prompt_card.set_visible(True)
        self.active_banner.set_visible(False)
        self.osc_card.set_visible(False)
        self.drawing_area.queue_draw()

    def _on_preview_clicked(self, gesture, n_press, x, y):
        """Handle user clicking directly on the camera preview or Loupe PIP."""
        with self._lock:
            bgra = self._preview_bgra
        if bgra is None:
            return

        w = self.drawing_area.get_width()
        h = self.drawing_area.get_height()

        # Check if click was inside Loupe PIP to toggle peaking
        pip_w, pip_h = 240, 240
        margin = 16
        pip_x = w - pip_w - margin
        pip_y = h - pip_h - margin
        if self._zoom_bgra is not None and pip_x <= x <= (pip_x + pip_w) and pip_y <= y <= (pip_y + pip_h):
            self._on_toggle_peaking()
            return

        fh, fw = bgra.shape[:2]

        scale = min(w / fw, h / fh)
        dw = int(fw * scale)
        dh = int(fh * scale)
        ox = (w - dw) // 2
        oy = (h - dh) // 2

        if ox <= x <= (ox + dw) and oy <= y <= (oy + dh) and scale > 0:
            frame_x = int((x - ox) / scale)
            frame_y = int((y - oy) / scale)
            self._select_target_mode(TargetMode.CUSTOM, custom_point=(frame_x, frame_y))

    def on_page_opened(self):
        """Lifecycle hook when page opens: maximize window for optimal calibration workspace."""
        logger.info("CameraFocusView opened: maximizing window and initializing hardware asynchronously")
        if self.window and hasattr(self.window, "is_maximized"):
            self._was_maximized = self.window.is_maximized()
            if hasattr(self.window, "maximize"):
                self.window.maximize()

        self.stack.set_visible_child_name("cam_focus")
        self.loading_box.set_visible(False)
        self.current_step = 0
        self._update_tab_buttons(0)
        self._show_target_prompt()

    def on_page_closed(self):
        """Lifecycle hook when navigating away: restore previous window maximization state."""
        logger.info("CameraFocusView closed: releasing hardware and restoring window state")
        self._stop_capture_thread()
        self.focus_service.stop_camera()
        self.audio_service.stop()
        self._camera_open = False
        self._pending_target = None

        if self.window and not self._was_maximized and hasattr(self.window, "unmaximize"):
            self.window.unmaximize()

        with self._lock:
            self._preview_bgra = None
            self._zoom_bgra = None
            self._current_frame = None
            self._current_analysis = None

        if getattr(self, "was_autodarts_active", False) or SystemdManager._autodarts_paused_by_suit:
            def _bg_restore():
                try:
                    logger.info("Restoring autodarts.service")
                    SystemdManager.resume_autodarts()
                except Exception:
                    logger.exception("Error restarting autodarts.service")
            t = threading.Thread(target=_bg_restore, daemon=False)
            t.start()
            self._restore_thread = t
            self.was_autodarts_active = False

    def _update_tab_buttons(self, active_idx: int):
        """Update active style on camera tabs."""
        for i, btn in enumerate(self.cam_tab_buttons):
            if i == active_idx:
                btn.add_css_class("active-tab")
            else:
                btn.remove_css_class("active-tab")

    def _on_cam_tab_clicked(self, idx: int):
        """Switch active camera tab."""
        if idx == self.current_step and not self._is_switching_camera:
            return

        if self._is_switching_camera:
            return
        self.current_step = idx
        self._update_tab_buttons(idx)
        self.lbl_step_title.set_label(tr(CAMERA_NAMES[idx]))

        # Release the current camera; the next one opens once a target is picked
        was_open = self._camera_open
        self._camera_open = False
        self._pending_target = None
        with self._lock:
            self._preview_bgra = None
            self._zoom_bgra = None
            self._current_frame = None
            self._current_analysis = None
        self._show_target_prompt()

        if was_open:
            def _bg_close():
                self._stop_capture_thread()
                self.focus_service.stop_camera()
            threading.Thread(target=_bg_close, daemon=True).start()

    def _start_capture_thread(self):
        """Spawn worker thread for frame acquisition and analysis."""
        self._stop_capture.clear()

        def _worker():
            while not self._stop_capture.is_set():
                ret, frame = self.focus_service.read_frame()
                if ret and frame is not None and frame.size > 0:
                    analysis = self.focus_service.analyze_frame(frame)
                    bgra_frame = self.focus_service.frame_to_bgra(frame)

                    bgra_zoom = None
                    if analysis.target_mode != TargetMode.NONE:
                        bgra_zoom = self.focus_service.process_zoom_crop(
                            frame,
                            analysis.target_roi,
                            zoom_factor=self.zoom_factor,
                            out_size=(240, 240),
                            peaking_enabled=self.peaking_enabled,
                        )

                    with self._lock:
                        self._current_frame = frame
                        self._current_analysis = analysis
                        self._preview_bgra = np.ascontiguousarray(bgra_frame, dtype=np.uint8)
                        self._zoom_bgra = np.ascontiguousarray(bgra_zoom, dtype=np.uint8) if bgra_zoom is not None else None

                    if analysis.target_mode != TargetMode.NONE:
                        self.audio_service.set_score(analysis.score)

                    GLib.idle_add(self._update_ui_state, analysis)
                time.sleep(0.033)

        self._capture_thread = threading.Thread(target=_worker, daemon=True)
        self._capture_thread.start()

    def _stop_capture_thread(self):
        """Stop worker thread."""
        self._stop_capture.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=0.8)
        self._capture_thread = None

    def _update_ui_state(self, analysis: FocusAnalysis):
        """Update GTK widgets with latest analysis."""
        if analysis.target_mode == TargetMode.NONE:
            self.drawing_area.queue_draw()
            return False

        score = analysis.score
        peak = analysis.peak_score
        trend = analysis.trend

        self.osc_peak_lbl.set_label(tr("Peak: {peak:.0f}%").format(peak=peak))

        if self.current_step < len(self.cam_tab_buttons):
            cam_name = tr(CAMERA_NAMES[self.current_step])
            self.cam_tab_buttons[self.current_step].set_label(f"{cam_name} ({score:.0f}%)")

        self.trend_badge.remove_css_class("sharpening")
        self.trend_badge.remove_css_class("stable")
        self.trend_badge.remove_css_class("overshot")

        if trend == FocusTrend.OVERSHOT:
            self.trend_badge.set_label(tr("Turn Back — Overshot"))
            self.trend_badge.add_css_class("overshot")
        elif trend == FocusTrend.SHARPENING:
            self.trend_badge.set_label(tr("Sharpening"))
            self.trend_badge.add_css_class("sharpening")
        else:
            self.trend_badge.set_label(tr("In Focus") if score >= 85.0 else tr("Tuning"))
            self.trend_badge.add_css_class("stable")

        self.drawing_area.queue_draw()
        self.osc_drawing_area.queue_draw()
        return False

    def _on_draw_preview(self, drawing_area, cr: cairo.Context, width: int, height: int):
        """Render live camera feed with Target ROI Reticle and 2.5x Loupe PIP."""
        with self._lock:
            bgra = self._preview_bgra
            zoom_bgra = self._zoom_bgra
            analysis = self._current_analysis

        cr.set_source_rgb(0.04, 0.05, 0.06)
        cr.paint()

        if bgra is None:
            cr.set_source_rgb(0.6, 0.6, 0.6)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(15)
            msg = tr("Waiting for camera feed...")
            cr.move_to(max(10, width / 2 - 90), height / 2)
            cr.show_text(msg)
            return

        fh, fw = bgra.shape[:2]
        surface = cairo.ImageSurface.create_for_data(
            bgra.data, cairo.FORMAT_ARGB32, fw, fh, fw * 4
        )

        scale = min(width / fw, height / fh)
        dw = int(fw * scale)
        dh = int(fh * scale)
        ox = (width - dw) // 2
        oy = (height - dh) // 2

        cr.save()
        cr.translate(ox, oy)
        cr.scale(scale, scale)
        cr.set_source_surface(surface, 0, 0)
        cr.paint()

        # 1. Render Reticle on Target ROI if target is selected
        if analysis and analysis.target_mode != TargetMode.NONE:
            tx, ty, tw, th = analysis.target_roi
            cr.set_source_rgba(0.0, 0.9, 0.95, 0.9)
            cr.set_line_width(2.0 / scale)

            # Corner brackets
            blen = 16.0 / scale
            # Top-left
            cr.move_to(tx, ty + blen); cr.line_to(tx, ty); cr.line_to(tx + blen, ty)
            # Top-right
            cr.move_to(tx + tw - blen, ty); cr.line_to(tx + tw, ty); cr.line_to(tx + tw, ty + blen)
            # Bottom-left
            cr.move_to(tx, ty + th - blen); cr.line_to(tx, ty + th); cr.line_to(tx + blen, ty + th)
            # Bottom-right
            cr.move_to(tx + tw - blen, ty + th); cr.line_to(tx + tw, ty + th); cr.line_to(tx + tw, ty + th - blen)
            cr.stroke()

            # Center crosshair
            cx, cy = tx + tw // 2, ty + th // 2
            cr.set_line_width(1.2 / scale)
            cr.move_to(cx - 8 / scale, cy); cr.line_to(cx + 8 / scale, cy)
            cr.move_to(cx, cy - 8 / scale); cr.line_to(cx, cy + 8 / scale)
            cr.stroke()

        cr.restore()

        # 2. Render 2.5x Loupe PIP in bottom-right corner (Expanded size: 240x240)
        if zoom_bgra is not None and analysis and analysis.target_mode != TargetMode.NONE:
            zh, zw = zoom_bgra.shape[:2]
            zoom_surface = cairo.ImageSurface.create_for_data(
                zoom_bgra.data, cairo.FORMAT_ARGB32, zw, zh, zw * 4
            )

            pip_w, pip_h = 240, 240
            margin = 16
            pip_x = width - pip_w - margin
            pip_y = height - pip_h - margin

            cr.save()
            # Rounded rectangle clip for Loupe PIP
            r = 14.0
            cr.new_sub_path()
            cr.arc(pip_x + pip_w - r, pip_y + r, r, -math.pi / 2, 0)
            cr.arc(pip_x + pip_w - r, pip_y + pip_h - r, r, 0, math.pi / 2)
            cr.arc(pip_x + r, pip_y + pip_h - r, r, math.pi / 2, math.pi)
            cr.arc(pip_x + r, pip_y + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()
            cr.clip()

            # Draw magnified surface
            z_scale = max(pip_w / zw, pip_h / zh)
            cr.translate(pip_x, pip_y)
            cr.scale(z_scale, z_scale)
            cr.set_source_surface(zoom_surface, 0, 0)
            cr.paint()
            cr.restore()

            # Loupe border
            cr.save()
            cr.new_sub_path()
            cr.arc(pip_x + pip_w - r, pip_y + r, r, -math.pi / 2, 0)
            cr.arc(pip_x + pip_w - r, pip_y + pip_h - r, r, 0, math.pi / 2)
            cr.arc(pip_x + r, pip_y + pip_h - r, r, math.pi / 2, math.pi)
            cr.arc(pip_x + r, pip_y + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()

            if analysis.score >= 85.0:
                cr.set_source_rgba(0.18, 0.85, 0.55, 0.95)
                cr.set_line_width(2.5)
            elif analysis.trend == FocusTrend.OVERSHOT:
                cr.set_source_rgba(0.95, 0.35, 0.25, 0.95)
                cr.set_line_width(2.0)
            else:
                cr.set_source_rgba(0.0, 0.85, 0.95, 0.85)
                cr.set_line_width(2.0)
            cr.stroke()

            # Loupe badge at top-left
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(10)
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.75)
            cr.rectangle(pip_x + 8, pip_y + 8, 120, 18)
            cr.fill()
            cr.set_source_rgba(0.0, 0.85, 0.95, 0.95)
            cr.move_to(pip_x + 14, pip_y + 21)
            peaking_text = tr("PEAKING ON") if self.peaking_enabled else tr("2.5× LOUPE")
            cr.show_text(f"2.5× • {peaking_text}")

            # Center crosshair inside loupe
            lcx = pip_x + pip_w / 2.0
            lcy = pip_y + pip_h / 2.0
            cr.set_line_width(1.0)
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.6)
            cr.move_to(lcx - 12, lcy); cr.line_to(lcx - 3, lcy)
            cr.move_to(lcx + 3, lcy); cr.line_to(lcx + 12, lcy)
            cr.move_to(lcx, lcy - 12); cr.line_to(lcx, lcy - 3)
            cr.move_to(lcx, lcy + 3); cr.line_to(lcx, lcy + 12)
            cr.stroke()
            cr.restore()

    def _on_draw_oscilloscope(self, drawing_area, cr: cairo.Context, width: int, height: int):
        """Render live Cairo Oscilloscope timeline with peak latch."""
        # Dark glass background
        cr.set_source_rgba(0.04, 0.05, 0.07, 0.95)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        # Grid lines (25%, 50%, 75%)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.08)
        cr.set_line_width(1.0)
        for frac in [0.25, 0.50, 0.75]:
            gy = height * (1.0 - frac)
            cr.move_to(0, gy)
            cr.line_to(width, gy)
            cr.stroke()

        with self._lock:
            analysis = self._current_analysis

        if analysis is None or analysis.target_mode == TargetMode.NONE:
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.4)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(13)
            cr.move_to(max(10, width / 2 - 100), height / 2 + 4)
            cr.show_text(tr("Select target to activate oscilloscope"))
            return

        history = analysis.history
        if not history or len(history) < 2:
            return

        n = len(history)
        pad_bottom = 10
        pad_top = 22
        graph_h = max(10, height - pad_top - pad_bottom)

        def get_pt(idx, val):
            x = (idx / max(1, n - 1)) * width
            y = height - pad_bottom - (val / 100.0) * graph_h
            return x, y

        # 1. Dotted cyan horizontal line for peak
        peak_val = analysis.peak_score
        if peak_val > 0:
            py = height - pad_bottom - (peak_val / 100.0) * graph_h
            cr.set_source_rgba(0.0, 0.85, 0.95, 0.6)
            cr.set_line_width(1.5)
            cr.set_dash([4.0, 4.0])
            cr.move_to(0, py)
            cr.line_to(width, py)
            cr.stroke()
            cr.set_dash([])

            # Peak marker pill
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(10)
            cr.set_source_rgba(0.0, 0.85, 0.95, 0.9)
            cr.move_to(max(8, width - 75), max(12, py - 4))
            cr.show_text(f"PEAK {peak_val:.0f}%")

        # 2. Gradient fill under curve
        cr.move_to(0, height)
        x0, y0 = get_pt(0, history[0])
        cr.line_to(x0, y0)
        for i in range(1, n):
            xi, yi = get_pt(i, history[i])
            cr.line_to(xi, yi)
        cr.line_to(width, height)
        cr.close_path()

        pat = cairo.LinearGradient(0, pad_top, 0, height)
        pat.add_color_stop_rgba(0.0, 0.0, 0.9, 0.7, 0.35)
        pat.add_color_stop_rgba(1.0, 0.0, 0.9, 0.7, 0.02)
        cr.set_source(pat)
        cr.fill()

        # 3. Live curve line
        cr.set_source_rgba(0.1, 0.95, 0.7, 0.95)
        cr.set_line_width(2.5)
        x0, y0 = get_pt(0, history[0])
        cr.move_to(x0, y0)
        for i in range(1, n):
            xi, yi = get_pt(i, history[i])
            cr.line_to(xi, yi)
        cr.stroke()

        # 4. Glowing head at current value
        cx, cy = get_pt(n - 1, history[-1])
        cr.set_source_rgba(0.1, 0.95, 0.7, 0.4)
        cr.arc(cx, cy, 6.0, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(1.0, 1.0, 1.0)
        cr.arc(cx, cy, 3.0, 0, 2 * math.pi)
        cr.fill()

        # 5. Live metrics (FPS & Relative score)
        cr.select_font_face("Monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(11)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.85)
        cr.move_to(10, 16)
        cr.show_text(f"{analysis.fps:.0f} FPS  |  {tr('Score')}: {analysis.score:.1f}%")

    def _jump_to_cam(self, idx: int):
        """Jump to refocus a specific camera."""
        self.stack.set_visible_child_name("cam_focus")
        self._on_cam_tab_clicked(idx)

    def _on_finish_clicked(self, button):
        """Record results and pop navigation view."""
        with self._lock:
            if self._current_analysis:
                self.camera_results[self.current_step]["score"] = self._current_analysis.score
                self.camera_results[self.current_step]["peak"] = self._current_analysis.peak_score
                self.camera_results[self.current_step]["target"] = self._current_analysis.target_name

        self.on_page_closed()
        if self.window and hasattr(self.window, "nav_view"):
            self.window.nav_view.pop()

    def _on_mute_clicked(self, button):
        """Toggle audio feedback."""
        muted = not self.audio_service.is_muted()
        self.audio_service.set_muted(muted)
        icon_name = "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"
        self.btn_mute.set_icon_name(icon_name)
        self.btn_mute.set_tooltip_text(tr("Unmute Audio Tone") if muted else tr("Mute Audio Tone"))
