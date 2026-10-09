"""Camera Calibration GTK4 / Libadwaita View Coordinator.

Coordinates the streamlined multi-step fullscreen wizard:
1. Dashboard (Table of standard vs calibrated values + master toggle switch + start button)
2. Pre-Check (Simultaneous 3-camera live focus check on furthest double field)
3. Baseline (Untouched auto-exposure snapshot capture)
4. Setup (Scope: all vs single, Precision: standard vs deep)
5. Sweep (Simultaneous 3-camera live calibration preview with progress and abort)
6. Review (Simultaneous 3-camera A/B comparison with edge & contrast filters)
"""

from __future__ import annotations
import logging
import threading
from typing import Dict, List, Optional, Tuple
import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from core.board_detector import BoardDetector
from core.cv_utils import (
    apply_difference_filter,
    apply_edge_contrast_filter,
    apply_noise_inspector_filter,
    detect_dart_tip,
    draw_sweet_spot_overlay_suit,
    process_zoom_crop_suit,
)
from core.diagnostics import HardwareDiagnostics
from core.metrics import QualityEvaluator
from core.optimizer import OptimizationResult
from core.persistence_service import PersistenceService
from core.systemd_service import SystemdManager
from core.v4l2_device import V4L2Device
from core.v4l2_topology import CameraDevice, V4L2Topology
from modules_gtk.cam_calibration.step_baseline import BaselineStep
from modules_gtk.cam_calibration.step_dashboard import DashboardStep
from modules_gtk.cam_calibration.step_focus import FocusStep
from modules_gtk.cam_calibration.step_review import ReviewStep
from modules_gtk.cam_calibration.step_setup import SetupStep
from modules_gtk.cam_calibration.step_sweep import SweepStep
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


def compute_suit_sharpness(patch: np.ndarray) -> float:
    """Compute normalized sharpness score (0-100) tuned for dartboard blade wire optics."""
    return QualityEvaluator.calculate_focus_score(patch)


class CamCalibrationView(Adw.Bin):
    """Multi-Step Fullscreen Camera Calibration Wizard Coordinator."""

    def __init__(self, on_back_callback=None):
        super().__init__()
        self.on_back = on_back_callback
        self.topology = V4L2Topology()
        self.persistence = PersistenceService()
        self.detector = BoardDetector()
        self.diagnostics = HardwareDiagnostics(self.detector)

        self.cameras: List[CameraDevice] = []
        self.baseline_frames: Dict[str, np.ndarray] = {}
        self.results: Dict[str, OptimizationResult] = {}

        # Wizard configuration state
        self.selected_scope: str = "all"  # "all" or dev_node
        self.selected_precision: str = "deep"  # "standard" or "deep"

        # Background worker & service state
        self.cancel_event: Optional[threading.Event] = None
        self._autodarts_was_running: bool = False

        # Step controllers
        self.step_dashboard = DashboardStep(self)
        self.step_focus = FocusStep(self)
        self.step_baseline = BaselineStep(self)
        self.step_setup = SetupStep(self)
        self.step_sweep = SweepStep(self)
        self.step_review = ReviewStep(self)

        # Build UI layout
        self._build_ui()
        self.refresh_cameras()

    def _build_ui(self):
        """Construct the Adw.ViewStack and assemble wizard step pages."""
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)

        self.page_dashboard = self.step_dashboard.build_page()
        self.stack.add_named(self.page_dashboard, "dashboard")

        self.page_focus = self.step_focus.build_page()
        self.stack.add_named(self.page_focus, "focus")

        self.page_baseline = self.step_baseline.build_page()
        self.stack.add_named(self.page_baseline, "baseline")

        self.page_setup = self.step_setup.build_page()
        self.stack.add_named(self.page_setup, "setup")

        self.page_sweep = self.step_sweep.build_page()
        self.stack.add_named(self.page_sweep, "sweep")

        self.page_review = self.step_review.build_page()
        self.stack.add_named(self.page_review, "review")

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.stack)
        self.set_child(self.toast_overlay)
        self.stack.set_visible_child_name("dashboard")

    # =========================================================================
    # NAVIGATION HANDLERS & STEP COORDINATION
    # =========================================================================
    def _go_to_dashboard(self):
        self._stop_focus_stream()
        calibrations = self.persistence.load_calibrations()
        is_enabled = self.persistence.is_calibration_enabled()
        for cam in self.cameras:
            try:
                vdev = V4L2Device(cam.dev_node)
                if is_enabled and cam.by_path in calibrations:
                    vdev.apply_settings(calibrations[cam.by_path])
                else:
                    vdev.reset_to_defaults()
            except Exception as e:
                logger.warning("Failed to restore camera %s on return to dashboard: %s", cam.dev_node, e)
        self.stack.set_visible_child_name("dashboard")
        self.step_dashboard.update_dashboard()

    def _go_to_focus(self):
        if SystemdManager.is_autodarts_active():
            self._autodarts_was_running = True
            SystemdManager.stop_autodarts()
        self.detector.unlock_target()
        # Reset all cameras to standard factory auto mode for Focus Check & Baseline
        for cam in self.cameras:
            try:
                vdev = V4L2Device(cam.dev_node)
                vdev.reset_to_defaults()
            except Exception as e:
                logger.warning("Failed to reset camera %s on focus start: %s", cam.dev_node, e)
        self.stack.set_visible_child_name("focus")
        self.step_focus.setup_focus_columns()
        self.step_focus.start_focus_stream()

    def _go_to_baseline(self):
        if SystemdManager.is_autodarts_active():
            self._autodarts_was_running = True
            SystemdManager.stop_autodarts()
        self._stop_focus_stream()
        self.stack.set_visible_child_name("baseline")
        self.step_baseline.capture_and_display_baseline()

    def _go_to_setup(self):
        self._stop_focus_stream()
        self.stack.set_visible_child_name("setup")
        self.step_setup.setup_scope_buttons()

    def _start_calibration_sweep(self):
        self.step_sweep.start_calibration_sweep()

    def _go_to_review(self):
        self._stop_focus_stream()
        self.stack.set_visible_child_name("review")
        self.step_review.setup_review_columns()

    def _stop_focus_stream(self):
        self.step_focus.stop_focus_stream()

    def _apply_and_finish(self):
        self.step_review._apply_and_finish()

    # =========================================================================
    # PUBLIC API / LIFECYCLE
    # =========================================================================
    def refresh_cameras(self):
        """Rescan system cameras and update dashboard."""
        self.cameras = self.topology.get_autodarts_cameras()
        self.cameras.sort(key=lambda c: c.index)
        self.step_dashboard.update_dashboard()

    def on_app_close(self):
        """Cleanup running threads on application close."""
        self._stop_focus_stream()
        if self.cancel_event:
            self.cancel_event.set()
        if getattr(self, "_autodarts_was_running", False):
            SystemdManager.start_autodarts()
            self._autodarts_was_running = False


class CamCalibrationPage(Adw.NavigationPage):
    """Navigation page wrapper for CAM-librator inside SUIT."""

    def __init__(self, window):
        super().__init__(title="Camera Optimizer (Experimental)", tag="cam_calibration")
        self.window = window

        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_kinetic_scrolling(True)
        self.set_child(scrolled)

        clamp = Adw.Clamp(maximum_size=960, tightening_threshold=780)
        clamp.set_margin_top(6)
        clamp.set_margin_bottom(8)
        clamp.set_margin_start(10)
        clamp.set_margin_end(10)
        scrolled.set_child(clamp)

        self.view = CamCalibrationView(on_back_callback=self._on_back)
        clamp.set_child(self.view)

    def _on_back(self):
        if hasattr(self.window, "nav_view"):
            self.window.nav_view.pop()

    def on_page_opened(self):
        if hasattr(self.view, "refresh_cameras"):
            self.view.refresh_cameras()

    def on_page_closed(self):
        if hasattr(self.view, "on_app_close"):
            self.view.on_app_close()
