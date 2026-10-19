"""Step 2 (Find darts): full-resolution burst, dart tip detection, frozen tip ROIs, banner."""

from __future__ import annotations
import dataclasses
import logging
import os
import threading
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

import numpy as np

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from core.dart_tip_detector import (
    WARN_HIGH_RESIDUAL,
    WARN_NEED_REFERENCE,
    WARN_NEED_THREE_CAMERAS,
    TipDetection,
    darts_close_together,
    detect_dart_tips,
)
import dataclasses as _dc
from core.metrics import TipROI, tip_box, tip_metrics, tip_rois_for_camera, _as_stack, _tip_polarity
from modules_gtk.cam_calibration import widgets as W
from modules_gtk.cam_calibration.camera_source import cam_key

if TYPE_CHECKING:
    from modules_gtk.cam_calibration_view import CamCalibrationView

logger = logging.getLogger("cam_librator.find")

BURST_FRAMES = 3
ZOOM_MIN_W = 480            # px at capture resolution; never zoom in further than this
ZOOM_PAD = 70

# Debug hook for screenshots and UI tests: forces one of the step-2 result states on top of
# the real detection (ok, hidden, close, two, one, none, blind, board).
DEBUG_ENV = "SUIT_OPTIMIZER_FIND_STATE"


@dataclasses.dataclass
class FindOutcome:
    kind: str                                   # banner kind: ok | warn | bad
    title: str
    body: str
    can_tune: bool
    board_only: bool = False
    rows: Dict[str, Tuple[str, str, str]] = dataclasses.field(default_factory=dict)  # key -> (subtitle, chip kind, chip text)


def _join_ids(ids: Sequence[int]) -> str:
    ids = [str(i) for i in ids]
    return ids[0] if len(ids) == 1 else ", ".join(ids[:-1]) + " and " + ids[-1]


def classify(det: Optional[TipDetection], rois: Dict[str, List[TipROI]], keys: Sequence[str],
             numbers: Dict[str, str]) -> FindOutcome:
    """Map a detection to the banner, the buttons and the camera rows (spec 5.3 with D2 / D6)."""
    if det is None or WARN_NEED_THREE_CAMERAS in det.warnings:
        rows = {k: ("Tunes on the whole board", "neutral", "Board") for k in keys}
        return FindOutcome("warn", "Tuning on the whole board",
                           "Dart finding needs a picture from all 3 cameras.", True, True, rows)
    if WARN_NEED_REFERENCE in det.warnings:
        rows = {k: ("No picture of the empty board", "bad", "No picture") for k in keys}
        return FindOutcome("bad", "No picture of the empty board",
                           "Tap Retake empty board and take the darts out first. Then put them back "
                           "and search again.", False, rows=rows)

    n = len(det.tips)
    rows: Dict[str, Tuple[str, str, str]] = {}
    hidden_lines: List[str] = []
    blind: List[str] = []
    for k in keys:
        cam_rois = rois.get(k, [])
        seen = [r for r in cam_rois if r.visible]
        hidden = [r.dart_id for r in cam_rois if not r.visible]
        v = len(seen)
        if n == 0 or v == 0:
            rows[k] = ("Can't see any tips" if n else "No darts found", "bad", "No tips")
            if n:
                blind.append(numbers.get(k, "?"))
            continue
        if hidden:
            word = "tip" if len(hidden) == 1 else "tips"
            rows[k] = (f"Can't see {word} {_join_ids(hidden)} clearly", "warn", f"{v} of {n}")
            hidden_lines.append(f"Camera {numbers.get(k, '?')} can't see {word} {_join_ids(hidden)} clearly.")
        elif n >= 3:
            rows[k] = (f"All {n} tips visible", "ok", f"{n} tips")
        else:
            rows[k] = (f"Sees {n} {'tip' if n == 1 else 'tips'}", "warn", f"{n} {'tip' if n == 1 else 'tips'}")

    if n <= 1:
        title = "No darts found" if n == 0 else "Only 1 dart found"
        return FindOutcome("bad", title, "Put 3 darts in the 20 and make sure no hand blocks a camera. "
                           "Then tap Search again.", False, rows=rows)
    if blind:
        cams = _join_ids(blind)
        return FindOutcome("bad", f"Camera {cams} can't see any tips",
                           "Make sure nothing blocks the camera, then tap Search again.", False, rows=rows)
    if n == 2:
        return FindOutcome("warn", "Only 2 darts found",
                           "You can start tuning, or add the third dart and search again.", True, rows=rows)
    if darts_close_together(det.tips):
        return FindOutcome("warn", "Darts are close together",
                           "Results may be less precise. Put them in the three spots of step 1 "
                           "and search again.", True, rows=rows)
    if hidden_lines:
        body = " ".join(hidden_lines) + " It tunes with the tips it can see."
        return FindOutcome("ok", f"{n} darts found", body, True, rows=rows)
    return FindOutcome("ok", f"{n} darts found", "Check that each box sits on a tip.", True, rows=rows)


def apply_debug_state(mode: str, det: TipDetection, rois: Dict[str, List[TipROI]],
                      keys: Sequence[str]) -> Tuple[TipDetection, Dict[str, List[TipROI]]]:
    """Force a result state (debug hook, see DEBUG_ENV). Returns modified copies."""
    det = dataclasses.replace(det, tips=list(det.tips), warnings=list(det.warnings))
    rois = {k: [dataclasses.replace(r) for r in v] for k, v in rois.items()}

    def keep(ids):
        det.tips = [t for t in det.tips if t.id in ids]
        for k in rois:
            rois[k] = [r for r in rois[k] if r.dart_id in ids]

    if mode in ("ok", "close", "two", "one", "blind"):
        for v in rois.values():
            for r in v:
                r.visible, r.marginal, r.hidden_reason = True, False, None
    if mode == "hidden" and len(keys) > 1:
        for r in rois.get(keys[1], []):
            if r.dart_id == 1:
                r.visible, r.hidden_reason = False, "forced (debug)"
    elif mode == "close":
        det.tips = [dataclasses.replace(t, n_cams=2) if i < 2 else t for i, t in enumerate(det.tips)]
    elif mode == "two":
        keep({t.id for t in det.tips[:2]})
    elif mode == "one":
        keep({t.id for t in det.tips[:1]})
    elif mode == "none":
        keep(set())
    elif mode == "blind" and keys:
        for r in rois.get(keys[-1], []):
            r.visible, r.hidden_reason = False, "forced (debug)"
    elif mode == "board":
        det.tips = []
        det.warnings.append(WARN_NEED_THREE_CAMERAS)
        rois = {}
    return det, rois


def zoom_rect(points: Sequence[Tuple[float, float, float, float]], size: Tuple[int, int]) -> Optional[Tuple[float, float, float, float]]:
    """4:3 rect around boxes (x, y, w, h), padded, at least ZOOM_MIN_W wide, inside the frame."""
    if not points:
        return None
    fw, fh = size
    x0 = min(p[0] for p in points) - ZOOM_PAD
    y0 = min(p[1] for p in points) - ZOOM_PAD
    x1 = max(p[0] + p[2] for p in points) + ZOOM_PAD
    y1 = max(p[1] + p[3] for p in points) + ZOOM_PAD
    w = max(x1 - x0, (y1 - y0) * 4.0 / 3.0, ZOOM_MIN_W)
    w = min(w, fw, fh * 4.0 / 3.0)
    h = w * 3.0 / 4.0
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    x = max(0.0, min(fw - w, cx - w / 2.0))
    y = max(0.0, min(fh - h, cy - h / 2.0))
    return (x, y, w, h)


class FindStep:
    """Takes the step-2 burst, runs the detector and shows what it found."""

    def __init__(self, view: CamCalibrationView):
        self.view = view
        self.running = False
        self.worker: Optional[threading.Thread] = None
        self.outcome: Optional[FindOutcome] = None
        self.zoomed = True
        self.banner = W.ResultBanner("ok", "", "", compact=True)
        self.debug_state: Optional[str] = os.environ.get(DEBUG_ENV) or None
        # Saves what this step worked with as a zip for the SUIT team (training / test data)
        self.save_button = Gtk.Button(label="Save sample")
        self.save_button.add_css_class("flat")
        self.save_button.set_tooltip_text("Save the pictures and the tips SUIT found as a file "
                                          "you can send to the SUIT team")
        self.save_button.connect("clicked", lambda *_: self._save_sample())

        # Tap the large view to switch between the darts and the whole board
        click = Gtk.GestureClick()
        click.connect("released", self._on_view_clicked)
        view.camera_view.add_controller(click)

    # -- step protocol ------------------------------------------------------------------

    def enter(self, search: bool = True):
        v = self.view
        v.set_main(v.camera_view)
        v.camera_view.set_pip(None)
        v.camera_view.set_active(False)
        v.camera_view.set_dim(False)
        if search or v.detection is None:
            self._start_search()
        else:
            self._show_result()

    def leave(self):
        self.view.camera_view.set_tips_editable(None)

    def on_camera_selected(self, key: str):
        if not self.running:
            self._show_camera(key)

    # -- search -------------------------------------------------------------------------

    def _start_search(self):
        v = self.view
        self.running = True
        self.outcome = None
        self.zoomed = True
        v.heading.set_text("Looking for the darts", "This takes a few seconds. Keep your hands away from the board.")
        v.set_panels(None, None)
        v.camera_view.set_tips_editable(None)
        v.camera_view.set_tips([])
        v.camera_view.set_zoom(None)
        v.camera_view.set_hint(None)
        v.camera_view.set_status("working", "Looking")
        for cam in v.cameras:
            v.camera_list.set_row(cam_key(cam), "Looking for tips", spinning=True)
        v.action_bar.set_secondary("Search again", sensitive=False)
        v.action_bar.set_primary("Start tuning", sensitive=False)
        v.action_bar.set_center(None)
        prev = self.worker
        self.worker = threading.Thread(target=self._worker, args=(v.session, prev), daemon=True, name="optimizer-find")
        self.worker.start()

    def _worker(self, session: int, prev: Optional[threading.Thread] = None):
        v = self.view
        try:
            v.step_insert.stop_live_stream(wait=True)
            if prev is not None and prev.is_alive():
                prev.join(timeout=60.0)     # a search of an earlier session may still be reading
            v.wait_for_cameras()
            w, h = v.capture_size
            frames: Dict[str, List[np.ndarray]] = {}
            for cam in list(v.cameras):
                if session != v.session:
                    return
                try:
                    burst = v.source.capture_burst(cam, BURST_FRAMES, w, h)
                except Exception as e:
                    logger.error("Burst capture failed on %s: %s", cam.dev_node, e)
                    burst = []
                if burst:
                    frames[cam_key(cam)] = burst
            models = {k: m for k, m in v.models.items() if k in frames}
            rois: Dict[str, List[TipROI]] = {}
            if len(models) >= 3:
                refs = {k: v.reference_frames[k] for k in models if k in v.reference_frames}
                det = detect_dart_tips({k: frames[k] for k in models}, models, expected=3, references=refs)
                for k in models:
                    rois[k] = tip_rois_for_camera(det, k, frames[k])
            else:
                det = TipDetection(tips=[], rejected={}, runtime_s=0.0, warnings=[WARN_NEED_THREE_CAMERAS])
            if self.debug_state:
                det, rois = apply_debug_state(self.debug_state, det, rois, [cam_key(c) for c in v.cameras])
            for k, rs in rois.items():
                for r in rs:
                    if not r.visible:
                        logger.info("Tip %d hidden in %s: %s", r.dart_id, k, r.hidden_reason)
            v.ui(session, self._on_done, frames, det, rois)
        except Exception as e:
            logger.exception("Dart finding failed")
            v.ui(session, self._on_error, str(e))

    def _on_done(self, frames: Dict[str, List[np.ndarray]], det: TipDetection, rois: Dict[str, List[TipROI]]):
        v = self.view
        self.running = False
        v.find_frames = frames
        v.detection = det
        v.tip_rois = rois
        if WARN_HIGH_RESIDUAL in det.warnings:
            logger.warning("Dart axes miss each other by more than 3 mm: Autodarts calibration may be stale")
        for k, burst in frames.items():
            v.camera_list.set_thumbnail(k, burst[0])
        self._show_result()

    def _on_error(self, _msg: str):
        v = self.view
        self.running = False
        self.outcome = FindOutcome("bad", "Something went wrong", "The cameras did not answer. Tap Search again.",
                                   False, rows={cam_key(c): ("No picture", "bad", "Error") for c in v.cameras})
        self._show_result()

    # -- result -------------------------------------------------------------------------

    def _show_result(self):
        v = self.view
        keys = [cam_key(c) for c in v.cameras]
        numbers = {cam_key(c): str(c.index) for c in v.cameras}
        if self.outcome is None:
            self.outcome = classify(v.detection, v.tip_rois, keys, numbers)
        o = self.outcome
        # Step 2 is only reached with every camera calibrated; a failed capture still falls back
        v.board_only = o.board_only

        if o.board_only:
            v.heading.set_text("Ready to tune", "Each camera is tuned on the whole board.")
        else:
            v.heading.set_text("Check the darts SUIT found",
                               "Yellow boxes mark the tips. Drag a box onto its tip to correct it. "
                               "Tap a camera to check it. Something wrong? Tap Save sample and send "
                               "the file to the SUIT team.")
        self._apply_outcome(keys)
        v.select_camera(v.selected)
        self._show_camera(v.selected)

    def _apply_outcome(self, keys):
        """Banner, camera rows and buttons from self.outcome."""
        v = self.view
        o = self.outcome
        self.banner.set_result(o.kind, o.title, o.body)
        v.set_panels(self.banner, None)
        for k in keys:
            sub, kind, text = o.rows.get(k, ("No picture", "bad", "No tips"))
            v.camera_list.set_row(k, sub, kind, text)
        bad = not o.can_tune
        if v.detection is not None and WARN_NEED_REFERENCE in v.detection.warnings:
            v.action_bar.set_secondary("Retake empty board", suggested=True, callback=self._retake_reference)
        else:
            v.action_bar.set_secondary("Search again", suggested=bad, callback=self._start_search)
        v.action_bar.set_primary("Start tuning", sensitive=o.can_tune, suggested=not bad,
                                 callback=self._start_tuning)
        can_save = bool(v.find_frames) and bool(v.reference_frames) and not o.board_only
        v.action_bar.set_center(self.save_button if can_save else None)

    # -- sample export ------------------------------------------------------------------

    def _save_sample(self):
        v = self.view
        self.save_button.set_sensitive(False)
        self.save_button.set_label("Saving...")
        # Copies: the user may drag a tip or search again while the zip is written
        args = (list(v.cameras), dict(v.reference_frames), dict(v.find_frames), tuple(v.capture_size),
                v.detection, {k: list(rs) for k, rs in v.tip_rois.items()})
        threading.Thread(target=self._save_worker, args=(v.session, *args), daemon=True,
                         name="optimizer-sample").start()

    def _save_worker(self, session: int, cams, empty, darts, size, det, rois):
        try:
            path = self.view.source.export_sample(cams, empty, darts, size, detection=det, tip_rois=rois)
            self.view.ui(session, self._on_sample_saved, str(path))
        except Exception:
            logger.exception("Saving the dart sample failed")
            self.view.ui(session, self._on_sample_saved, None)

    def _on_sample_saved(self, path: Optional[str]):
        """The result stays in the banner: a toast is gone after 3 seconds."""
        self.save_button.set_sensitive(True)
        self.save_button.set_label("Save sample")
        if path:
            self.banner.set_result("ok", "Sample saved",
                                   f"{os.path.basename(path)} is in {os.path.dirname(path)}. "
                                   "Send this file to the SUIT team.")
        else:
            self.banner.set_result("bad", "Could not save the sample", "See the SUIT log for details.")

    def _retake_reference(self):
        """Back to step 1 without an empty-board picture: it opens on Take all darts out."""
        self.view.reference_frames = {}
        self.view.show_step("insert")

    def _on_tip_moved(self, dart_id: int, x: float, y: float):
        """A box was dragged: the tip of this dart in the selected camera is where the user put it."""
        v = self.view
        key = v.selected
        rois = v.tip_rois.get(key or "", [])
        burst = v.find_frames.get(key or "")
        idx = next((i for i, r in enumerate(rois) if r.dart_id == dart_id), None)
        if idx is None or not burst:
            return
        stack = _as_stack(burst)
        mean = stack.mean(axis=0)
        old = rois[idx]
        h, w = mean.shape[:2]
        tip = (float(min(w - 1, max(0.0, x))), float(min(h - 1, max(0.0, y))))
        new = _dc.replace(old, tip=tip, box=tip_box(tip, mean.shape), visible=True, marginal=False,
                          hidden_reason=None, faint=False, manual=True)
        new.polarity = _tip_polarity(mean, new)
        new.ref_contrast = float(tip_metrics(stack, new, mean).contrast)
        rois[idx] = new
        logger.info("Tip %d in %s moved by hand to (%.0f, %.0f)", dart_id, key, tip[0], tip[1])
        keys = [cam_key(c) for c in v.cameras]
        numbers = {cam_key(c): str(c.index) for c in v.cameras}
        self.outcome = classify(v.detection, v.tip_rois, keys, numbers)
        v.board_only = self.outcome.board_only
        self._apply_outcome(keys)
        v.camera_view.set_tips([(r.dart_id, r.tip, not r.visible) for r in rois], space=(w, h))

    def _show_camera(self, key: Optional[str]):
        v = self.view
        cv = v.camera_view
        if key is None or self.outcome is None:
            return
        cv.set_title(v.cam_title(key))
        burst = v.find_frames.get(key)
        frame = burst[0] if burst else None
        rois = v.tip_rois.get(key, [])
        sub, kind, text = self.outcome.rows.get(key, ("", "bad", "No tips"))
        cv.set_status(kind, text)
        if frame is None:
            cv.set_zoom(None)
            cv.set_tips([])
            cv.set_frame(None)
            cv.set_hint(None)
            return
        h, w = frame.shape[:2]
        boxes = []
        det = v.detection
        if det is not None:
            for t in det.tips:
                ct = t.per_cam.get(key)
                if ct is not None:
                    boxes.append(ct.ui_box)
        boxes += [(r.tip[0] - 20, r.tip[1] - 20, 40, 40) for r in rois]
        rect = zoom_rect(boxes, (w, h)) if self.zoomed else None
        # Explicit space: the view may still hold a 640x480 live frame from step 1
        cv.set_zoom(rect, space=(w, h))
        cv.set_frame(frame)
        cv.set_tips([(r.dart_id, r.tip, not r.visible) for r in rois], space=(w, h))
        cv.set_tips_editable(None if self.running or self.view.board_only else self._on_tip_moved)
        if rect is not None:
            cv.set_hint("Zoomed in · tap for whole board")
        elif boxes:
            cv.set_hint("Tap to zoom in on the darts")
        else:
            cv.set_hint(None)

    def _on_view_clicked(self, *_args):
        if self.view.current_step != "find" or self.running:
            return
        self.zoomed = not self.zoomed
        self._show_camera(self.view.selected)

    def _start_tuning(self):
        if self.outcome is not None and self.outcome.can_tune:
            self.view.show_step("tune")
