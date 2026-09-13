"""Camera Focus Tool GTK4 / Libadwaita presentation view.

Guides users through a 4-step wizard to optically focus Autodarts cameras,
featuring real-time Cairo HUD overlays (Target Box & Dynamic Focal Line),
sharpness score gauge, peak memory, trend warnings, and frequency-modulated audio.
"""

import threading
import time
import numpy as np
import cairo

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk

from core.logger import get_logger
from core.camera_focus_service import CameraFocusService, FocusTrend, FocusAnalysis
from core.audio_tone_service import AudioToneService
from core.autodarts_service import read_cam_config
from core.systemd_service import SystemdService

logger = get_logger("camera_focus_view")


CAMERA_NAMES = ["Camera 1", "Camera 2", "Camera 3"]
CAMERA_DESCRIPTIONS = [(name, "") for name in CAMERA_NAMES]


class CameraFocusView(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Camera Focus Tool", tag="camera_focus")
        self.window = window

        self.focus_service = CameraFocusService()
        self.audio_service = AudioToneService()

        self.current_step = 0  # 0: Cam 1, 1: Cam 2, 2: Cam 3
        self.configured_cams = ["/dev/video0", "/dev/video2", "/dev/video4"]
        self.camera_results = [
            {"score": 0.0, "peak": 0.0, "snapshot": None},
            {"score": 0.0, "peak": 0.0, "snapshot": None},
            {"score": 0.0, "peak": 0.0, "snapshot": None},
        ]
        self.was_autodarts_active = False

        # Zoom / Magnifier settings (locked to optimal 2.5x)
        self.zoom_factor = 2.5

        # Live capture state
        self._stop_capture = threading.Event()
        self._capture_thread = None
        self._current_frame: np.ndarray | None = None
        self._current_analysis: FocusAnalysis | None = None
        self._lock = threading.Lock()
        self._is_switching_camera = False

        self._preview_surface: cairo.ImageSurface | None = None
        self._preview_bgra: np.ndarray | None = None
        self._zoom_surface: cairo.ImageSurface | None = None
        self._zoom_bgra: np.ndarray | None = None

        # Scrolled container for responsive displays
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_kinetic_scrolling(True)
        self.set_child(scrolled)

        clamp = Adw.Clamp(maximum_size=980, tightening_threshold=720)
        clamp.set_margin_top(14)
        clamp.set_margin_bottom(20)
        clamp.set_margin_start(14)
        clamp.set_margin_end(14)
        scrolled.set_child(clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        clamp.set_child(self.main_box)

        # Header Bar Container
        self._build_header_card()

        # Camera Tab Selector Bar (Direct 1-tap camera switching)
        self._build_camera_tab_bar()

        # ViewStack for Wizard (Focus Screen vs Summary Screen)
        self.stack = Adw.ViewStack()
        self.main_box.append(self.stack)

        # Page 1: Interactive Focus Tuning Page
        self.focus_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.stack.add_named(self.focus_page, "cam_focus")

        # Page 2: Summary Page
        self.summary_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.stack.add_named(self.summary_page, "summary")

        self._build_focus_screen()
        self._build_summary_screen()

    @property
    def _accum_zoom_crop(self) -> np.ndarray | None:
        return self.focus_service._accum_zoom_crop

    @_accum_zoom_crop.setter
    def _accum_zoom_crop(self, val: np.ndarray | None):
        self.focus_service._accum_zoom_crop = val

    def _build_header_card(self):
        """Build top navigation and step indicator bar."""
        header_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_card.add_css_class("tile-card")
        self.main_box.append(header_card)

        step_icon = Gtk.Image.new_from_icon_name("camera-photo-symbolic")
        step_icon.set_pixel_size(32)
        step_icon.set_valign(Gtk.Align.CENTER)
        header_card.append(step_icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        header_card.append(text_box)

        self.lbl_step_title = Gtk.Label(label="Camera 1", xalign=0)
        self.lbl_step_title.add_css_class("title-3")
        text_box.append(self.lbl_step_title)

        self.lbl_step_desc = Gtk.Label(
            label="Adjust focus by twisting the camera lens slowly and focus on the furthest double field. Rotate until the score reaches a peak — that peak is your target focus value.",
            xalign=0,
            wrap=True,
        )
        self.lbl_step_desc.add_css_class("dim-label")
        text_box.append(self.lbl_step_desc)

        # Audio mute toggle button (muted by default)
        self.btn_mute = Gtk.Button.new_from_icon_name("audio-volume-muted-symbolic")
        self.btn_mute.add_css_class("flat")
        self.btn_mute.set_size_request(46, 44)
        self.btn_mute.set_valign(Gtk.Align.CENTER)
        self.btn_mute.set_tooltip_text("Unmute Audio Guidance")
        self.btn_mute.connect("clicked", self._on_mute_clicked)
        header_card.append(self.btn_mute)

    def _build_camera_tab_bar(self):
        """Build prominent direct-switching camera tabs."""
        self.cam_tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.cam_tab_bar.add_css_class("camera-tab-bar")
        self.cam_tab_bar.set_homogeneous(True)
        self.main_box.append(self.cam_tab_bar)

        self.cam_tab_buttons = []
        for i, cam_name in enumerate(CAMERA_NAMES):
            btn = Gtk.Button(label=cam_name)
            btn.add_css_class("flat")
            btn.add_css_class("camera-tab-btn")
            if i == 0:
                btn.add_css_class("active-tab")
            btn.connect("clicked", lambda b, idx=i: self._on_cam_tab_clicked(idx))
            self.cam_tab_bar.append(btn)
            self.cam_tab_buttons.append(btn)

    def _build_focus_screen(self):
        """Build the video preview canvas with overlay spinner and focus gauge card."""
        content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        content_box.set_vexpand(True)
        self.focus_page.append(content_box)

        # Left: Video Feed with Cairo HUD Overlay & Async Loading Spinner
        video_frame = Gtk.Frame()
        video_frame.set_hexpand(True)
        video_frame.set_vexpand(True)
        video_frame.add_css_class("focus-preview-area")
        content_box.append(video_frame)

        self.video_overlay = Gtk.Overlay()
        video_frame.set_child(self.video_overlay)

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_draw_func(self._on_draw_preview)
        self.drawing_area.set_size_request(520, 400)
        self.video_overlay.set_child(self.drawing_area)

        # Loading Spinner & Message Overlay
        self.loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.loading_box.add_css_class("focus-loading-overlay")
        self.loading_box.set_valign(Gtk.Align.CENTER)
        self.loading_box.set_halign(Gtk.Align.CENTER)
        self.loading_box.set_margin_top(40)
        self.loading_box.set_margin_bottom(40)
        self.loading_box.set_margin_start(40)
        self.loading_box.set_margin_end(40)

        self.loading_spinner = Gtk.Spinner()
        self.loading_spinner.set_size_request(48, 48)
        self.loading_box.append(self.loading_spinner)

        self.loading_lbl = Gtk.Label(label="Stopping Autodarts & Opening Camera 1...")
        self.loading_lbl.add_css_class("title-3")
        self.loading_box.append(self.loading_lbl)

        self.video_overlay.add_overlay(self.loading_box)

        # Right: Sidebar containing Zoom Magnifier Card & Sharpness Gauge Card
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        sidebar_box.set_size_request(300, -1)
        content_box.append(sidebar_box)

        # 1. Zoom Section (Magnifier on Furthest Double Field, locked to 2.5x)
        zoom_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        zoom_card.add_css_class("focus-zoom-card")
        sidebar_box.append(zoom_card)

        zoom_frame = Gtk.Frame()
        zoom_frame.add_css_class("focus-zoom-frame")
        zoom_card.append(zoom_frame)

        self.zoom_drawing_area = Gtk.DrawingArea()
        self.zoom_drawing_area.set_draw_func(self._on_draw_zoom)
        self.zoom_drawing_area.set_size_request(280, 180)
        self.zoom_drawing_area.set_hexpand(True)
        zoom_frame.set_child(self.zoom_drawing_area)

        # 2. Gauges & Controls Card
        gauge_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        gauge_box.add_css_class("focus-gauge-card")
        gauge_box.set_vexpand(True)
        sidebar_box.append(gauge_box)

        self.gauge_lbl = Gtk.Label(label="0.0", xalign=0)
        self.gauge_lbl.add_css_class("focus-gauge-score")
        self.gauge_lbl.add_css_class("score-poor")
        gauge_box.append(self.gauge_lbl)

        # Progress bar
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_fraction(0.0)
        gauge_box.append(self.progress_bar)

        # Trend & Peak Info Box
        info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        info_box.set_margin_top(2)
        gauge_box.append(info_box)

        self.trend_lbl = Gtk.Label(label="Tuning")
        self.trend_lbl.add_css_class("focus-trend-badge")
        self.trend_lbl.add_css_class("stable")
        info_box.append(self.trend_lbl)

        self.peak_lbl = Gtk.Label(label="Peak: 0.0", xalign=1)
        self.peak_lbl.add_css_class("dim-label")
        self.peak_lbl.set_hexpand(True)
        self.peak_lbl.set_tooltip_text("Tap to reset peak")
        info_box.append(self.peak_lbl)

        # Tap peak label to reset peak without needing a dedicated button
        peak_gesture = Gtk.GestureClick()
        peak_gesture.connect("released", lambda *args: self.focus_service.reset_peak())
        self.peak_lbl.add_controller(peak_gesture)

        # Spacer to push finish button to bottom
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        gauge_box.append(spacer)

        # Finish Action Button
        btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        gauge_box.append(btn_box)

        self.btn_finish = Gtk.Button(label="Finish")
        self.btn_finish.add_css_class("suggested-action")
        self.btn_finish.add_css_class("touch-btn")
        self.btn_finish.set_size_request(-1, 50)
        self.btn_finish.connect("clicked", self._on_finish_clicked)
        btn_box.append(self.btn_finish)

    def _build_summary_screen(self):
        """Build the final 3-camera summary verification card."""
        summary_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.summary_page.append(summary_box)

        header_banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header_banner.add_css_class("tile-card")
        summary_box.append(header_banner)

        lbl_sum_h = Gtk.Label(label="Focus Calibration Summary", xalign=0)
        lbl_sum_h.add_css_class("title-2")
        header_banner.append(lbl_sum_h)

        lbl_sum_desc = Gtk.Label(
            label="Review final focus scores across all 3 cameras before resuming Autodarts.",
            xalign=0,
        )
        lbl_sum_desc.add_css_class("dim-label")
        header_banner.append(lbl_sum_desc)

        # 3 Result Tiles
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

            title = Gtk.Label(label=cam_name, xalign=0)
            title.add_css_class("title-3")
            card.append(title)

            score_lbl = Gtk.Label(label="Score: 0.0", xalign=0)
            score_lbl.add_css_class("title-4")
            card.append(score_lbl)

            btn_refocus = Gtk.Button(label="Refocus")
            btn_refocus.add_css_class("secondary-btn")
            btn_refocus.connect("clicked", lambda b, idx=i: self._jump_to_cam(idx))
            card.append(btn_refocus)

            self.summary_cards_box.append(card)
            self.sum_tiles.append((score_lbl, btn_refocus))

        # Bottom Finish Action Button in Summary
        self.btn_sum_finish = Gtk.Button(label="Finish")
        self.btn_sum_finish.add_css_class("suggested-action")
        self.btn_sum_finish.add_css_class("touch-btn")
        self.btn_sum_finish.set_size_request(-1, 54)
        self.btn_sum_finish.set_margin_top(12)
        self.btn_sum_finish.connect("clicked", self._on_finish_clicked)
        summary_box.append(self.btn_sum_finish)

    def on_page_opened(self):
        """Lifecycle hook when page is navigated into: opens instantly and inits in background."""
        logger.info("CameraFocusView opened: showing instant UI and preparing hardware asynchronously")
        self.stack.set_visible_child_name("cam_focus")
        self.loading_box.set_visible(True)
        self.loading_spinner.start()
        self.loading_lbl.set_label("Stopping Autodarts & Opening Camera 1...")

        def _bg_init():
            # 1. Stop autodarts.service to release V4L2 device locks in background
            try:
                self.was_autodarts_active = SystemdService.is_unit_active("autodarts.service")
                if self.was_autodarts_active:
                    logger.info("Stopping autodarts.service to release V4L2 device locks")
                    SystemdService.stop_unit("autodarts.service")
            except Exception:
                logger.exception("Error querying/stopping autodarts.service")

            # 2. Discover configured cameras and available V4L2 devices
            cfg = read_cam_config()
            self.configured_cams = [c for c in cfg.get("cams", []) if c and c.strip()]
            if not self.configured_cams:
                self.configured_cams = ["/dev/video0", "/dev/video2", "/dev/video4"]

            # 3. Start audio tone service
            self.audio_service.start()

            # 4. Open first camera
            dev_path = self.configured_cams[0]
            self.focus_service.start_camera(dev_path)

            GLib.idle_add(self._on_init_completed, dev_path)

        threading.Thread(target=_bg_init, daemon=True).start()

    def _on_init_completed(self, dev_path: str):
        """Called on main thread once background init finishes."""
        self.current_step = 0
        self._update_tab_buttons(0)

        self.loading_spinner.stop()
        self.loading_box.set_visible(False)
        self._start_capture_thread()

    def on_page_closed(self):
        """Lifecycle hook when navigating away from view."""
        logger.info("CameraFocusView closed: releasing hardware and restoring services")
        self._stop_capture_thread()
        self.focus_service.stop_camera()
        self.audio_service.stop()

        # Restore Autodarts service asynchronously so UI navigation pops immediately
        if self.was_autodarts_active:
            def _bg_restore():
                try:
                    logger.info("Restoring autodarts.service in background")
                    SystemdService.start_unit("autodarts.service")
                except Exception:
                    logger.exception("Error restarting autodarts.service")
            threading.Thread(target=_bg_restore, daemon=True).start()

    def _update_tab_buttons(self, active_idx: int):
        """Update active style on camera tabs."""
        for i, btn in enumerate(self.cam_tab_buttons):
            if i == active_idx:
                btn.add_css_class("active-tab")
            else:
                btn.remove_css_class("active-tab")

    def _on_cam_tab_clicked(self, idx: int):
        """Switch active camera directly when a camera tab is tapped."""
        if idx == self.current_step and not self._is_switching_camera:
            return

        self._is_switching_camera = True
        self.current_step = idx
        self._update_tab_buttons(idx)
        self.focus_service.reset_peak()
        self._accum_zoom_crop = None

        cam_name = CAMERA_NAMES[idx]
        self.lbl_step_title.set_label(cam_name)

        dev_path = (
            self.configured_cams[idx]
            if idx < len(self.configured_cams)
            else f"/dev/video{idx * 2}"
        )

        # Show camera switching spinner overlay
        self.loading_lbl.set_label(f"Opening {cam_name} ({dev_path})...")
        self.loading_box.set_visible(True)
        self.loading_spinner.start()

        def _bg_switch():
            self._stop_capture_thread()
            self.focus_service.stop_camera()
            self.focus_service.start_camera(dev_path)
            GLib.idle_add(self._on_cam_switch_done)

        threading.Thread(target=_bg_switch, daemon=True).start()

    def _on_cam_switch_done(self):
        """Called on main thread once background camera switch completes."""
        self._is_switching_camera = False
        self.loading_spinner.stop()
        self.loading_box.set_visible(False)
        self._start_capture_thread()

    def _load_camera_step(self, step_idx: int):
        """Backwards-compatible camera step loader; delegates to direct tab switching."""
        if step_idx < len(self.cam_tab_buttons):
            self._on_cam_tab_clicked(step_idx)

    def _start_capture_thread(self):
        """Spawn background worker thread for V4L2 frame acquisition and CV analysis."""
        self._stop_capture.clear()

        def _worker():
            while not self._stop_capture.is_set():
                ret, frame = self.focus_service.read_frame()
                if ret and frame is not None:
                    analysis = self.focus_service.analyze_frame(frame)

                    fh, fw = frame.shape[:2]
                    bgra_frame = self.focus_service.frame_to_bgra(frame)
                    preview_surf = cairo.ImageSurface.create_for_data(
                        bgra_frame.data, cairo.FORMAT_ARGB32, fw, fh, fw * 4
                    )

                    # Precompute zoom crop with temporal accumulation and focus peaking
                    zw = self.zoom_drawing_area.get_width() if hasattr(self, "zoom_drawing_area") else 280
                    zh = self.zoom_drawing_area.get_height() if hasattr(self, "zoom_drawing_area") else 180
                    if zw <= 1 or zh <= 1:
                        zw, zh = 280, 180

                    bgra_zoom = self.focus_service.process_zoom_crop(
                        frame, analysis.target_roi, zoom_factor=self.zoom_factor, out_size=(zw, zh)
                    )
                    zoom_surf = None
                    if bgra_zoom is not None:
                        zoom_surf = cairo.ImageSurface.create_for_data(
                            bgra_zoom.data, cairo.FORMAT_ARGB32, zw, zh, zw * 4
                        )

                    with self._lock:
                        self._current_frame = frame
                        self._current_analysis = analysis
                        self._preview_bgra = bgra_frame
                        self._preview_surface = preview_surf
                        self._zoom_bgra = bgra_zoom
                        self._zoom_surface = zoom_surf

                    # Modulate parking sensor audio guidance
                    self.audio_service.set_score(analysis.score)

                    GLib.idle_add(self._update_ui_state, analysis)
                time.sleep(0.033)  # ~30 FPS loop

        self._capture_thread = threading.Thread(target=_worker, daemon=True)
        self._capture_thread.start()

    def _stop_capture_thread(self):
        """Stop background worker thread."""
        self._stop_capture.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=0.8)
        self._capture_thread = None

    def _update_ui_state(self, analysis: FocusAnalysis):
        """Update GTK4 widgets from background analysis result."""
        score = analysis.score
        peak = analysis.peak_score
        trend = analysis.trend

        # Update Score Label & CSS Classes
        self.gauge_lbl.set_label(f"{score:.1f}")
        self.gauge_lbl.remove_css_class("score-good")
        self.gauge_lbl.remove_css_class("score-fair")
        self.gauge_lbl.remove_css_class("score-poor")

        if score >= 85.0:
            self.gauge_lbl.add_css_class("score-good")
        elif score >= 55.0:
            self.gauge_lbl.add_css_class("score-fair")
        else:
            self.gauge_lbl.add_css_class("score-poor")

        # Update active camera tab button with live score
        if self.current_step < len(self.cam_tab_buttons):
            cam_name = CAMERA_NAMES[self.current_step]
            self.cam_tab_buttons[self.current_step].set_label(f"{cam_name} ({score:.0f}%)")

        # Update Progress Bar & Peak Label
        self.progress_bar.set_fraction(score / 100.0)
        self.peak_lbl.set_label(f"Peak: {peak:.1f}")

        # Update Trend Badge
        self.trend_lbl.remove_css_class("sharpening")
        self.trend_lbl.remove_css_class("stable")
        self.trend_lbl.remove_css_class("overshot")

        if trend == FocusTrend.SHARPENING:
            self.trend_lbl.set_label("Sharpening")
            self.trend_lbl.add_css_class("sharpening")
        elif trend == FocusTrend.OVERSHOT:
            self.trend_lbl.set_label("Overshot — Turn Back")
            self.trend_lbl.add_css_class("overshot")
        else:
            if score >= 95.0:
                self.trend_lbl.set_label("In Focus")
            else:
                self.trend_lbl.set_label("Tuning")
            self.trend_lbl.add_css_class("stable")

        # Trigger canvas redraws
        self.drawing_area.queue_draw()
        if hasattr(self, "zoom_drawing_area"):
            self.zoom_drawing_area.queue_draw()
        return False

    def _on_draw_preview(self, drawing_area, cr: cairo.Context, width: int, height: int):
        """Cairo draw function rendering live video frame with Target Box and Dynamic Focal Line."""
        with self._lock:
            surface = self._preview_surface
            analysis = self._current_analysis
            frame = self._current_frame

        # Fallback if draw called before background worker runs (e.g. in test fixtures)
        if surface is None and frame is not None:
            fh, fw = frame.shape[:2]
            bgra = self.focus_service.frame_to_bgra(frame)
            surface = cairo.ImageSurface.create_for_data(
                bgra.data, cairo.FORMAT_ARGB32, fw, fh, fw * 4
            )
            with self._lock:
                self._preview_bgra = bgra
                self._preview_surface = surface

        # Clear background
        cr.set_source_rgb(0.04, 0.05, 0.06)
        cr.paint()

        if surface is None or analysis is None:
            cr.set_source_rgb(0.6, 0.6, 0.6)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(16)
            cr.move_to(width / 2 - 80, height / 2)
            cr.show_text("Waiting for camera feed...")
            return

        fw = surface.get_width()
        fh = surface.get_height()
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

        # 1. Draw Sweet Spot Target Box on Furthest Double Field
        tx, ty, tw, th = analysis.target_roi
        is_locked = analysis.score >= 85.0

        if is_locked:
            cr.set_source_rgb(0.18, 0.76, 0.49)  # Emerald green lock
            cr.set_line_width(3.5 / scale)
            cr.set_dash([])
        elif analysis.score >= 55.0:
            cr.set_source_rgba(0.96, 0.83, 0.18, 0.9)  # Amber
            cr.set_line_width(2.5 / scale)
            cr.set_dash([8.0 / scale, 6.0 / scale])
        else:
            cr.set_source_rgba(0.2, 0.52, 0.89, 0.85)  # Blue bracket
            cr.set_line_width(2.0 / scale)
            cr.set_dash([8.0 / scale, 6.0 / scale])

        cr.rectangle(tx, ty, tw, th)
        cr.stroke()

        # Center reticle inside target box
        cr.set_dash([])
        tcx, tcy = tx + tw // 2, ty + th // 2
        cr.set_line_width(1.5 / scale)
        cr.move_to(tcx - 14 / scale, tcy); cr.line_to(tcx + 14 / scale, tcy)
        cr.move_to(tcx, tcy - 10 / scale); cr.line_to(tcx, tcy + 10 / scale)
        cr.stroke()

        # Clean status label above target box when locked
        if is_locked:
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(12 / scale)
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.75)
            cr.rectangle(tx, ty - int(24 / scale), int(105 / scale), int(20 / scale))
            cr.fill()

            cr.set_source_rgb(0.18, 0.76, 0.49)
            cr.move_to(tx + int(6 / scale), ty - int(9 / scale))
            cr.show_text("SWEET SPOT")

        cr.restore()

    def _on_toggle_zoom(self, button=None):
        """Maintained for backwards-compatibility; zoom locked to 2.5x."""
        self.zoom_factor = 2.5
        self.zoom_drawing_area.queue_draw()

    def _on_draw_zoom(self, drawing_area, cr: cairo.Context, width: int, height: int):
        """Cairo draw function rendering digitally magnified target sweet-spot with Focus Peaking."""
        with self._lock:
            surface = self._zoom_surface
            analysis = self._current_analysis
            frame = self._current_frame

        # Fallback if draw called before background worker runs (e.g. in test fixtures)
        if surface is None and frame is not None and analysis is not None:
            bgra = self.focus_service.process_zoom_crop(
                frame, analysis.target_roi, zoom_factor=self.zoom_factor, out_size=(width, height)
            )
            if bgra is not None:
                surface = cairo.ImageSurface.create_for_data(
                    bgra.data, cairo.FORMAT_ARGB32, width, height, width * 4
                )
                with self._lock:
                    self._zoom_bgra = bgra
                    self._zoom_surface = surface

        # Clear background
        cr.set_source_rgb(0.04, 0.05, 0.06)
        cr.paint()

        if surface is None or analysis is None:
            cr.set_source_rgb(0.5, 0.5, 0.5)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(12)
            cr.move_to(width / 2 - 35, height / 2)
            cr.show_text("Waiting...")
            return

        sw = surface.get_width()
        sh = surface.get_height()

        # Center the magnified surface precisely so that (sw/2, sh/2) aligns with (width/2, height/2)
        scale = max(width / sw, height / sh)
        ox = (width - sw * scale) / 2.0
        oy = (height - sh * scale) / 2.0

        cr.save()
        cr.rectangle(0, 0, width, height)
        cr.clip()

        cr.translate(ox, oy)
        cr.scale(scale, scale)
        cr.set_source_surface(surface, 0, 0)
        cr.paint()
        cr.restore()

        # Center reticle / crosshair
        mx, my = width / 2.0, height / 2.0
        cr.set_line_width(1.5)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.65)
        cr.move_to(mx - 14, my)
        cr.line_to(mx - 4, my)
        cr.move_to(mx + 4, my)
        cr.line_to(mx + 14, my)
        cr.move_to(mx, my - 14)
        cr.line_to(mx, my - 4)
        cr.move_to(mx, my + 4)
        cr.line_to(mx, my + 14)
        cr.stroke()

        # Status border indicator
        score = analysis.score
        if score >= 85.0:
            cr.set_source_rgba(0.18, 0.76, 0.49, 0.95)  # Emerald green
            cr.set_line_width(3.0)
        elif score >= 55.0:
            cr.set_source_rgba(0.96, 0.83, 0.18, 0.85)  # Amber
            cr.set_line_width(2.0)
        else:
            cr.set_source_rgba(0.88, 0.11, 0.14, 0.45)  # Subtle red
            cr.set_line_width(1.5)

        cr.rectangle(1.5, 1.5, width - 3, height - 3)
        cr.stroke()

    def _on_next_clicked(self, button):
        """Handle proceeding to the next camera or summary."""
        with self._lock:
            if self._current_analysis:
                self.camera_results[self.current_step]["score"] = self._current_analysis.score
                self.camera_results[self.current_step]["peak"] = self._current_analysis.peak_score
                self.camera_results[self.current_step]["snapshot"] = (
                    self._current_frame.copy() if self._current_frame is not None else None
                )

        self._load_camera_step(self.current_step + 1)

    def _update_summary_screen(self):
        """Update summary cards with recorded camera scores."""
        for i, (score_lbl, _) in enumerate(self.sum_tiles):
            res = self.camera_results[i]
            s = res["score"]
            status = "Optimal" if s >= 80.0 else ("Fair" if s >= 50.0 else "Needs Adjustment")
            score_lbl.set_label(f"Score: {s:.1f} ({status})")

    def _jump_to_cam(self, idx: int):
        """Jump back to refocus a specific camera."""
        self._load_camera_step(idx)

    def _on_finish_clicked(self, button):
        """Finish focus calibration and return to main dashboard."""
        self.on_page_closed()
        if self.window and hasattr(self.window, "nav_view"):
            self.window.nav_view.pop()

    def _on_mute_clicked(self, button):
        """Toggle audio guidance mute."""
        muted = not self.audio_service.is_muted()
        self.audio_service.set_muted(muted)
        icon_name = "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"
        self.btn_mute.set_icon_name(icon_name)
        self.btn_mute.set_tooltip_text("Unmute Audio Tone" if muted else "Mute Audio Tone")
