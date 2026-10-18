"""Step 3 (Tune): one camera after another, progress card, cancel, end-of-run dart check."""

from __future__ import annotations
import logging
import math
import threading
import time
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

import numpy as np

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw

from core.dart_tip_detector import compare_tips, detect_dart_tips
from core.metrics import TipROI
from core.optimizer import OptimizationResult
from modules_gtk.cam_calibration import widgets as W
from modules_gtk.cam_calibration.camera_source import cam_key

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.tune")

STAGE_TITLES = {
    "brightness": "Setting brightness",
    "sensitivity": "Setting sensitivity",
    "detail": "Adjusting detail",
    "check": "Final check",
}


def eta_text(seconds: Optional[float]) -> str:
    """Plain time estimate: "About 40 s left", "About 2 min left"."""
    if seconds is None or not math.isfinite(seconds):
        return ""
    if seconds < 10:
        return "A few seconds left"
    if seconds < 60:
        return f"About {int(round(seconds / 5.0) * 5)} s left"
    return f"About {max(1, int(round(seconds / 60.0)))} min left"


def tip_crop(frame: Optional[np.ndarray], roi: TipROI, size: int = 150) -> Optional[np.ndarray]:
    """Square close-up of a tip with the steel point and barrel end in view."""
    if frame is None:
        return None
    dx, dy = roi.direction
    # Centre a little behind the tip (towards the flight) so the point fills the crop
    back = min(size * 0.22, roi.point_len * 0.8)
    return W.crop_square(frame, (roi.tip[0] - dx * back, roi.tip[1] - dy * back), size)


def tip_crop_marked(frame: Optional[np.ndarray], roi: TipROI, size: int = 150):
    """(crop, marker) like tip_crop, plus the tip's position inside the crop as fractions (x, y)."""
    crop = tip_crop(frame, roi, size)
    if crop is None:
        return None, None
    dx, dy = roi.direction
    back = min(size * 0.22, roi.point_len * 0.8)
    x0, y0, side = W.crop_origin(frame.shape, (roi.tip[0] - dx * back, roi.tip[1] - dy * back), size)
    return crop, ((roi.tip[0] - x0) / side, (roi.tip[1] - y0) / side)


def result_row(res: Optional[OptimizationResult]):
    """(subtitle, chip kind, chip text) of a finished camera in the tuning list."""
    if res is None:
        return "Could not be tuned", "bad", "Failed"
    if res.darts_moved:
        return "Nothing changed", "warn", "Darts moved"
    if res.is_improvement:
        return ("Clearer picture" if res.fallback_board_only else "Tips clearer"), "ok", "Done"
    return "Already well tuned", "neutral", "Done"


class TuneStep:
    """Sequential per-camera optimizer worker with stage chips and a time estimate."""

    def __init__(self, view: CamCalibrationView):
        self.view = view
        self.card = W.ProgressCard()
        self.worker: Optional[threading.Thread] = None
        self.running = False
        self.stopping = False
        self.active_key: Optional[str] = None
        self.last_frames: Dict[str, np.ndarray] = {}
        self._order: List[str] = []
        self._t0 = 0.0
        self._on_stopped: Optional[Callable[[], None]] = None
        self._dialog: Optional[Adw.AlertDialog] = None

    # -- step protocol ------------------------------------------------------------------

    def enter(self):
        v = self.view
        v.set_main(v.camera_view)
        v.heading.set_text("Tuning the cameras", "Leave the darts where they are.")
        self.card.set_title("Getting ready")
        self.card.set_eta("")
        self.card.set_fraction(0.0)
        self.card.set_stage(None)
        v.set_panels(None, self.card)
        v.action_bar.set_secondary("Cancel", callback=lambda: self.ask_cancel())
        v.action_bar.set_primary(visible=False)
        cv = v.camera_view
        cv.set_zoom(None)
        cv.set_hint(None)
        cv.set_tips([])
        cv.set_pip(None)

        cams = list(v.cameras)
        self._order = [cam_key(c) for c in cams]
        self.last_frames = {k: b[0] for k, b in v.find_frames.items() if b}
        v.results.clear()
        v.failed.clear()
        v.moved_after_run = []
        for i, key in enumerate(self._order):
            v.camera_list.set_row(key, "Waits its turn", "neutral", "Next")
        self.active_key = None
        self.running = True
        self.stopping = False
        self._on_stopped = None
        v.cancel_event = threading.Event()
        self._set_can_pop(False)
        self._t0 = time.monotonic()
        self.worker = threading.Thread(target=self._worker,
                                       args=(v.session, v.cancel_event, cams, v.board_only, self.worker),
                                       daemon=True, name="optimizer-tune")
        self.worker.start()

    def leave(self):
        self._close_dialog()

    def reset(self):
        """New session: forget a run the previous session abandoned (it restores on its own)."""
        self.running = False
        self.stopping = False
        self._on_stopped = None
        self._close_dialog()
        self._set_can_pop(True)

    def on_camera_selected(self, key: str):
        self._show_camera(key)

    # -- worker -------------------------------------------------------------------------

    def _worker(self, session: int, cancel: threading.Event, cams: list, board_only: bool,
                prev: Optional[threading.Thread]):
        v = self.view
        results: Dict[str, OptimizationResult] = {}
        moved: List[int] = []
        try:
            # A run of an earlier session may still be restoring its camera
            if prev is not None and prev.is_alive():
                prev.join(timeout=60.0)
            # Board-only runs come straight from the live preview: release those streams first
            v.step_insert.stop_live_stream(wait=True)
            v.wait_for_cameras()
            total = len(cams)
            for idx, cam in enumerate(cams):
                if cancel.is_set() or session != v.session:
                    break
                key = cam_key(cam)
                v.ui(session, self._on_cam_start, key, idx, total)
                model = v.models.get(key)
                tips = None if board_only else v.tip_rois.get(key)

                def cb(stage, fraction, text, frame=None, _key=key, _idx=idx):
                    v.ui(session, self._on_progress, _key, _idx, total, stage, fraction, text, frame)

                try:
                    optimizer = v.source.optimizer(cam)
                    res = optimizer.run_optimization(progress_cb=cb, cancel_event=cancel, tips=tips,
                                                     homography=model.H_img2board if model is not None else None)
                except Exception as e:
                    logger.exception("Tuning failed on %s", cam.dev_node)
                    v.ui(session, self._on_cam_failed, key, str(e))
                    continue
                if res is None:
                    if cancel.is_set():
                        break
                    v.ui(session, self._on_cam_failed, key, "no frames from the camera")
                    continue
                results[key] = res
                v.ui(session, self._on_cam_done, key, res)

            if results and not cancel.is_set() and not board_only:
                moved = self._final_check(session, cams)

            if results and not cancel.is_set():
                log = "\n".join(f"[{cam_key(c)}]\n" + "\n".join(results[cam_key(c)].details)
                                for c in cams if cam_key(c) in results)
                v.source.export_session({r.camera_node: r for r in results.values()}, log)
        except Exception:
            logger.exception("Tuning worker failed")
        finally:
            v.ui(session, self._on_finished, cancel.is_set(), moved)

    def _final_check(self, session: int, cams: list) -> List[int]:
        """Three-camera re-detection after the last camera; ids of tips that moved since step 2."""
        v = self.view
        det0 = v.detection
        if det0 is None or not det0.tips:
            return []
        v.ui(session, self._on_final_check_start)
        try:
            w, h = v.capture_size
            frames = {}
            for cam in cams:
                burst = v.source.capture_burst(cam, 3, w, h)
                if burst:
                    frames[cam_key(cam)] = burst
            models = {k: m for k, m in v.models.items() if k in frames}
            if len(models) < 3:
                return []
            det1 = detect_dart_tips({k: frames[k] for k in models}, models, expected=len(det0.tips))
            moved = compare_tips(det0, det1)
            if moved:
                logger.warning("End-of-run check: tips %s moved or are missing", moved)
            return moved
        except Exception as e:
            logger.error("End-of-run dart check failed: %s", e)
            return []

    # -- UI updates (GTK thread) --------------------------------------------------------

    def _on_cam_start(self, key: str, idx: int, total: int):
        v = self.view
        self.active_key = key
        num = v.cam_number(key)
        v.camera_list.set_row(key, "Starting", spinning=True)
        for k in self._order[idx + 1:]:
            v.camera_list.set_row(k, f"Waits for camera {num}", "neutral", "Next")
        self.card.set_title(f"Camera {idx + 1} of {total} · Starting")
        v.select_camera(key)
        self._show_camera(key)

    def _on_progress(self, key: str, idx: int, total: int, stage: str, fraction: float, text: str,
                     frame: Optional[np.ndarray]):
        v = self.view
        if self.stopping:
            return
        overall = (idx + max(0.0, min(1.0, fraction))) / max(1, total)
        self.card.set_fraction(overall)
        self.card.set_stage(stage)
        self.card.set_title(f"Camera {idx + 1} of {total} · {STAGE_TITLES.get(stage, 'Working')}")
        elapsed = time.monotonic() - self._t0
        self.card.set_eta(eta_text(elapsed * (1.0 - overall) / overall) if overall > 0.04 else "")
        if text:
            v.camera_list.set_row(key, text, spinning=True)
        if frame is not None:
            self.last_frames[key] = frame
            if v.selected == key:
                self._show_camera(key)

    def _on_cam_done(self, key: str, res: OptimizationResult):
        v = self.view
        v.results[key] = res
        self.last_frames[key] = res.optimized_frame
        v.camera_list.set_row(key, *result_row(res))
        v.camera_list.set_thumbnail(key, res.optimized_frame)
        if v.selected == key:
            self._show_camera(key)

    def _on_cam_failed(self, key: str, err: str):
        v = self.view
        v.failed[key] = err
        v.camera_list.set_row(key, *result_row(None))
        if v.selected == key:
            self._show_camera(key)

    def _on_final_check_start(self):
        self.card.set_title("Checking the darts once more")
        self.card.set_stage(None, all_done=True)
        self.card.set_eta("A few seconds left")

    def _on_finished(self, cancelled: bool, moved: List[int]):
        v = self.view
        self.running = False
        self._set_can_pop(True)
        self._close_dialog()
        if self._on_stopped is not None:
            cb, self._on_stopped = self._on_stopped, None
            cb()
            return
        if cancelled:
            v.results.clear()
            for k, burst in v.find_frames.items():
                v.camera_list.set_thumbnail(k, burst[0])
            v.toast("Tuning stopped. The cameras keep their settings.")
            # Back to the step that started the run: the search result when there is one (also a
            # whole-board fallback after a failed burst, so Search again stays reachable)
            if v.detection is not None:
                v.show_step("find", search=False)
            else:
                v.show_step("insert")
            return
        self.card.set_fraction(1.0)
        self.card.set_stage(None, all_done=True)
        v.moved_after_run = list(moved)
        v.show_step("review")

    def _show_camera(self, key: Optional[str]):
        v = self.view
        if key is None or v.current_step != "tune":
            return
        cv = v.camera_view
        cv.set_title(v.cam_title(key))
        active = key == self.active_key and self.running
        done = key in v.results or key in v.failed
        cv.set_active(active)
        cv.set_dim(not active and not done)
        if active:
            cv.set_status("working", "Working")
        elif key in v.results:
            _sub, kind, text = result_row(v.results[key])
            cv.set_status(kind, text)
        elif key in v.failed:
            cv.set_status("bad", "Failed")
        else:
            cv.set_status("neutral", "Next")
        frame = self.last_frames.get(key)
        cv.set_frame(frame)
        rois = [] if v.board_only else [r for r in v.tip_rois.get(key, []) if r.visible]
        if frame is not None and rois and active:
            r = rois[0]
            cv.set_tips([(r.dart_id, r.tip, False)], space=(frame.shape[1], frame.shape[0]))
            cv.set_pip(tip_crop(frame, r), f"Tip {r.dart_id}")
        else:
            cv.set_tips([])
            cv.set_pip(None)

    # -- cancel -------------------------------------------------------------------------

    def ask_cancel(self, on_stopped: Optional[Callable[[], None]] = None):
        """Confirm, then stop. on_stopped runs instead of going back to step 1 / 2 (leaving)."""
        if not self.running or self.stopping:
            return
        dialog = Adw.AlertDialog(
            heading="Stop tuning?",
            body="The cameras go back to the settings they had before. Nothing is changed.",
        )
        dialog.add_response("continue", "Keep tuning")
        dialog.add_response("stop", "Stop tuning")
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("continue")
        dialog.set_close_response("continue")
        dialog.connect("response", lambda _d, r: self._on_cancel_response(r, on_stopped))
        self._dialog = dialog
        dialog.present(self.view)

    def _on_cancel_response(self, response: str, on_stopped: Optional[Callable[[], None]]):
        self._dialog = None
        if response != "stop" or not self.running:
            return
        self.stop(on_stopped)

    def stop(self, on_stopped: Optional[Callable[[], None]] = None):
        v = self.view
        self.stopping = True
        if v.cancel_event is not None:
            v.cancel_event.set()
        if on_stopped is not None:
            # Leaving: the session ends now and restores the cameras in the background
            on_stopped()
            return
        self.card.set_title("Stopping")
        self.card.set_eta("Restoring the cameras")
        v.camera_view.set_status("working", "Stopping")
        v.action_bar.set_secondary("Cancel", sensitive=False)

    def _close_dialog(self):
        if self._dialog is not None:
            self._dialog.force_close()
            self._dialog = None

    def _set_can_pop(self, can_pop: bool):
        page = self.view.get_ancestor(Adw.NavigationPage)
        if page is not None:
            page.set_can_pop(can_pop)
