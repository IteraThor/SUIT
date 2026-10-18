"""Camera Optimizer wizard (Layout B): shell, session state and Autodarts handling.

Four steps share one body: a large camera view (or the review tip grid) on the left and a
right column with the step heading, the camera list and one step panel.
1. Insert darts  (live 640x480 preview, where to put the 3 darts)
2. Find darts    (full-resolution burst -> detect_dart_tips -> frozen tip ROIs)
3. Tune          (one camera after another, run_optimization(tips=...))
4. Review        (before/after tip close-ups, Apply / Discard)

Autodarts is paused when the page opens and resumed on every way out (back, close, discard,
apply, cancel, error). Camera hardware is only reached through `camera_source`.
"""

from __future__ import annotations
import logging
import threading
from typing import Callable, Dict, List, Optional

import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from core.cv_utils import (
    apply_difference_filter,
    apply_edge_contrast_filter,
    apply_noise_inspector_filter,
    detect_dart_tip,
    draw_sweet_spot_overlay_suit,
    process_zoom_crop_suit,
)
from core.dart_tip_detector import CameraModel, TipDetection
from core.metrics import QualityEvaluator, TipROI
from core.optimizer import OptimizationResult
from core.systemd_service import SystemdManager
from core.v4l2_topology import CameraDevice
from modules_gtk.cam_calibration import widgets as W
from modules_gtk.cam_calibration.camera_source import CameraSource, cam_key
from modules_gtk.cam_calibration.step_find import FindStep
from modules_gtk.cam_calibration.step_insert import InsertStep
from modules_gtk.cam_calibration.step_review import ReviewStep
from modules_gtk.cam_calibration.step_tune import TuneStep
from modules_gtk.cv_gtk_utils import extract_magnified_pixbuf, frame_to_pixbuf

# Re-export CV and utility functions for backward compatibility with existing tests
__all__ = [
    "CamCalibrationView",
    "CamCalibrationPage",
    "frame_to_pixbuf",
    "extract_magnified_pixbuf",
    "apply_difference_filter",
    "detect_dart_tip",
    "apply_edge_contrast_filter",
    "apply_noise_inspector_filter",
    "compute_suit_sharpness",
    "process_zoom_crop_suit",
    "draw_sweet_spot_overlay_suit",
]

logger = logging.getLogger("cam_librator.view")

STEP_NUMBERS = {"insert": 1, "find": 2, "tune": 3, "review": 4}


def compute_suit_sharpness(patch: np.ndarray) -> float:
    """Compute normalized sharpness score (0-100) tuned for dartboard blade wire optics."""
    return QualityEvaluator.calculate_focus_score(patch)


class CamCalibrationView(Adw.Bin):
    """Wizard shell: stepper, shared Layout B body, one action bar, session state."""

    def __init__(self, on_back_callback=None, on_open_manual_callback=None,
                 camera_source: Optional[CameraSource] = None,
                 on_subtitle: Optional[Callable[[str], None]] = None,
                 on_toast: Optional[Callable[[str], None]] = None):
        super().__init__()
        self.on_back = on_back_callback
        self.on_open_manual = on_open_manual_callback
        self.on_subtitle = on_subtitle
        self.on_toast = on_toast
        self.source = camera_source or CameraSource()

        # Session state (reset by start_session)
        self.cameras: List[CameraDevice] = []
        self.models: Dict[str, CameraModel] = {}       # kept between searches (remap caches)
        self.capture_size = (1280, 960)
        self.detection: Optional[TipDetection] = None
        self.find_frames: Dict[str, List[np.ndarray]] = {}
        self.tip_rois: Dict[str, List[TipROI]] = {}
        self.results: Dict[str, OptimizationResult] = {}
        self.failed: Dict[str, str] = {}               # cam key -> error (Details only)
        self.board_only = False                        # no dart finding: tune on the whole board
        self.board_only_reason: Optional[str] = None
        self.moved_after_run: List[int] = []           # tip ids the end-of-run check found moved
        self.selected: Optional[str] = None
        self.current_step: Optional[str] = None

        # Worker & service state
        self.session = 0
        self.cancel_event: Optional[threading.Event] = None
        self.restore_thread: Optional[threading.Thread] = None
        self._autodarts_was_running = False

        self._build_ui()

        # Step controllers (they share the body built above)
        self.step_insert = InsertStep(self)
        self.step_find = FindStep(self)
        self.step_tune = TuneStep(self)
        self.step_review = ReviewStep(self)
        self.steps = {"insert": self.step_insert, "find": self.step_find,
                      "tune": self.step_tune, "review": self.step_review}

    # =========================================================================
    # LAYOUT
    # =========================================================================
    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.stepper = W.Stepper()
        self.stepper.set_margin_start(24)
        self.stepper.set_margin_end(24)
        self.stepper.set_margin_top(12)
        self.stepper.set_margin_bottom(12)
        root.append(self.stepper)

        self.split = W.SplitLayout()
        self.split.set_margin_start(24)
        self.split.set_margin_end(24)
        self.split.set_margin_bottom(16)
        root.append(self.split)

        self.camera_view = W.CameraView("Camera 1")
        self.tip_grid = W.TipGrid()
        self.split.set_main(self.camera_view)

        self.heading = W.StepHeading()
        self.panel_top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.camera_list = W.CameraList()
        self.camera_list.connect("camera-selected", self._on_camera_selected)
        self.panel_bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for w in (self.heading, self.panel_top, self.camera_list, self.panel_bottom):
            self.split.side.append(w)

        self.action_bar = W.ActionBar()
        root.append(self.action_bar)

        # Wide screens (maximized): keep the composition together instead of stretching it
        clamp = Adw.Clamp(maximum_size=1480, tightening_threshold=1200)
        clamp.set_child(root)
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(clamp)
        self.set_child(self.toast_overlay)

    def set_panels(self, top: Optional[Gtk.Widget] = None, bottom: Optional[Gtk.Widget] = None):
        """Swap the step panels above and below the camera list."""
        for box, child in ((self.panel_top, top), (self.panel_bottom, bottom)):
            while (c := box.get_first_child()) is not None:
                box.remove(c)
            if child is not None:
                if child.get_parent() is not None:
                    child.unparent()
                box.append(child)
            box.set_visible(child is not None)

    def set_main(self, widget: Gtk.Widget):
        self.split.set_main(widget)

    # =========================================================================
    # NAVIGATION
    # =========================================================================
    def show_step(self, name: str, **kwargs):
        """Leave the current step and enter `name` (insert, find, tune, review)."""
        old = self.steps.get(self.current_step)
        if old is not None and self.current_step != name:
            old.leave()
        self.current_step = name
        n = STEP_NUMBERS[name]
        self.stepper.set_current(n)
        if self.on_subtitle:
            self.on_subtitle(f"Step {n} of 4 · {W.STEP_LABELS[n - 1]}")
        self.steps[name].enter(**kwargs)

    def _on_camera_selected(self, _list, key: str):
        self.selected = key
        step = self.steps.get(self.current_step)
        if step is not None:
            step.on_camera_selected(key)

    def select_camera(self, key: Optional[str]):
        """Select a camera row without re-entering the step's selection handler."""
        if key is None:
            return
        self.selected = key
        self.camera_list.select_camera(key)

    def leave_wizard(self):
        if self.on_back and callable(self.on_back):
            self.on_back()

    # =========================================================================
    # SESSION / LIFECYCLE
    # =========================================================================
    def refresh_cameras(self):
        """Rescan system cameras."""
        try:
            self.cameras = self.source.scan()
        except Exception as e:
            logger.error("Camera scan failed: %s", e)
            self.cameras = []

    def start_session(self):
        """Fresh wizard run: pause Autodarts, load calibration, show step 1."""
        self.session += 1
        self.cancel_event = None
        self.detection = None
        self.find_frames.clear()
        self.tip_rois.clear()
        self.results.clear()
        self.failed.clear()
        self.moved_after_run = []
        self.step_tune.reset()
        self.pause_autodarts()

        self.camera_list.set_cameras([(cam_key(c), f"Camera {c.index}") for c in self.cameras])
        self.selected = cam_key(self.cameras[0]) if self.cameras else None
        self._load_models()
        self.show_step("insert")

    def _load_models(self):
        try:
            self.capture_size = tuple(self.source.capture_resolution())
        except Exception:
            self.capture_size = (1280, 960)
        keys = [cam_key(c) for c in self.cameras]
        if not all(k in self.models for k in keys) or any(m.size != self.capture_size for m in self.models.values()):
            try:
                self.models = self.source.camera_models(self.cameras, *self.capture_size)
            except Exception as e:
                logger.error("Loading the Autodarts calibration failed: %s", e)
                self.models = {}
        missing = [c for c in self.cameras if cam_key(c) not in self.models]
        if len(self.cameras) < 3:
            self.board_only, self.board_only_reason = True, "cameras"
        elif missing:
            self.board_only, self.board_only_reason = True, "calibration"
            logger.info("No calibration homography for %s: board-only tuning", [c.dev_node for c in missing])
        else:
            self.board_only, self.board_only_reason = False, None

    def end_session(self):
        """Leave the wizard on any path: stop workers; cameras restore, Autodarts resumes."""
        self.session += 1
        ended = self.session
        self.step_insert.stop_live_stream(wait=False)
        if self.cancel_event is not None:
            self.cancel_event.set()
        busy = self.camera_workers()
        if busy:
            # The optimizer restores the camera in its finally block and a burst may still be
            # reading; start Autodarts only after every worker let go of the cameras, so both
            # never fight over them. A session opened meanwhile owns the pause from then on.
            def _resume_after_restore():
                for t in busy:
                    t.join(timeout=60.0)
                if self.session == ended:
                    self.resume_autodarts()
            self.restore_thread = threading.Thread(target=_resume_after_restore, daemon=True,
                                                   name="optimizer-restore")
            self.restore_thread.start()
        else:
            self.resume_autodarts()

    def camera_workers(self) -> List[threading.Thread]:
        """Worker threads that may still hold a camera (live preview, search burst, tuning)."""
        threads = (self.step_insert.thread, self.step_find.worker, self.step_tune.worker)
        me = threading.current_thread()
        return [t for t in threads if t is not None and t.is_alive() and t is not me]

    def wait_for_cameras(self, own: Optional[threading.Thread] = None, timeout: float = 60.0) -> None:
        """Worker threads only: wait until earlier workers released the cameras (a run of an
        earlier session may still be restoring its camera)."""
        for t in self.camera_workers():
            if t is not own:
                t.join(timeout=timeout)

    def pause_autodarts(self):
        try:
            if SystemdManager.is_autodarts_active():
                self._autodarts_was_running = True
                SystemdManager.pause_autodarts(settle=False)
        except Exception as e:
            logger.error("Could not pause Autodarts: %s", e)

    def resume_autodarts(self):
        if self._autodarts_was_running or SystemdManager._autodarts_paused_by_suit:
            try:
                SystemdManager.resume_autodarts()
            except Exception as e:
                logger.error("Could not resume Autodarts: %s", e)
            self._autodarts_was_running = False

    def on_app_close(self):
        """Cleanup running threads on page close / application close."""
        self.end_session()

    @property
    def tuning(self) -> bool:
        return self.step_tune.running

    # =========================================================================
    # HELPERS FOR THE STEPS
    # =========================================================================
    def ui(self, session: int, fn: Callable, *args):
        """Run fn(*args) on the GTK thread unless the session ended meanwhile."""
        def _call():
            if session == self.session:
                try:
                    fn(*args)
                except Exception:
                    logger.exception("Camera Optimizer UI update failed")
            return False
        GLib.idle_add(_call)

    def camera_for(self, key: str) -> Optional[CameraDevice]:
        return next((c for c in self.cameras if cam_key(c) == key or c.dev_node == key), None)

    def cam_title(self, key: str) -> str:
        cam = self.camera_for(key)
        return f"Camera {cam.index}" if cam else "Camera"

    def cam_number(self, key: str) -> str:
        cam = self.camera_for(key)
        return str(cam.index) if cam else "?"

    def toast(self, text: str):
        if self.on_toast:
            self.on_toast(text)
        else:
            self.toast_overlay.add_toast(Adw.Toast(title=text, timeout=3))

    def open_details(self):
        from modules_gtk.cam_calibration.details_dialog import DetailsDialog
        dialog = DetailsDialog(self)
        dialog.present(self)
        return dialog

    def confirm_leave(self, leave: Callable[[], None]) -> bool:
        """Header back while tuning: ask first. True when the request was handled here."""
        if not self.tuning:
            return False
        self.step_tune.ask_cancel(on_stopped=leave)
        return True


class CamCalibrationPage(Adw.NavigationPage):
    """Navigation page wrapper for Camera Optimizer inside SUIT."""

    def __init__(self, window, camera_source: Optional[CameraSource] = None):
        super().__init__(title="Camera Optimizer", tag="cam_calibration")
        self.window = window

        self.view = CamCalibrationView(
            on_back_callback=self._on_back,
            on_open_manual_callback=self._on_open_manual,
            camera_source=camera_source,
            on_subtitle=self._set_subtitle,
            on_toast=self._show_toast,
        )
        self.set_child(self.view)

        # Details lives in the window's header bar while this page is shown
        self.details_button = Gtk.Button.new_from_icon_name("help-about-symbolic")
        self.details_button.add_css_class("flat")
        self.details_button.set_tooltip_text("Details")
        self.details_button.set_size_request(46, 44)
        self.details_button.connect("clicked", lambda _b: self.view.open_details())
        self._details_packed = False

    @property
    def _restore_thread(self):
        # SuitWindow joins this on close so cameras are restored before the process ends
        return self.view.restore_thread

    def _header(self):
        hb = getattr(self.window, "header_bar", None)
        return hb if isinstance(hb, Adw.HeaderBar) else None

    def _set_subtitle(self, text: str):
        tw = getattr(self.window, "title_widget", None)
        if isinstance(tw, Adw.WindowTitle) and self.window.nav_view.get_visible_page() is self:
            tw.set_subtitle(text)

    def _show_toast(self, text: str):
        if hasattr(self.window, "show_toast") and callable(self.window.show_toast):
            self.window.show_toast(text)
        else:
            self.view.toast_overlay.add_toast(Adw.Toast(title=text, timeout=3))

    def _on_back(self):
        if hasattr(self.window, "nav_view"):
            self.window.nav_view.pop()

    def _on_open_manual(self):
        if hasattr(self.window, "open_camera_manual_page"):
            self.window.open_camera_manual_page()

    def confirm_leave(self, leave: Callable[[], None]) -> bool:
        return self.view.confirm_leave(leave)

    def on_page_opened(self):
        hb = self._header()
        if hb is not None and not self._details_packed:
            hb.pack_end(self.details_button)
            self._details_packed = True
        self.view.refresh_cameras()
        self.view.start_session()

    def on_page_closed(self):
        hb = self._header()
        if hb is not None and self._details_packed:
            hb.remove(self.details_button)
            self._details_packed = False
        self.view.on_app_close()
