"""Manual corrections of the dart finding: which dart is where.

A dart tip sits on the board surface, so a tip the user puts in one camera is a point on the board
plane: the camera's homography turns it into a board position, and every other camera's box
follows from that position. Putting the same dart in two cameras averages the two board points. A small correction (a few mm)
only moves the box in that camera: the finder's tips in the other cameras were fine.
Dart ids are the same in every camera because they belong to the board position, not to a
per-camera guess.

Pure numpy / OpenCV, no GTK. The editor changes the TipDetection it is given in place and rebuilds
the per-camera TipROIs; a tip the user placed is always visible and flagged `manual`.
"""

from __future__ import annotations
import dataclasses
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from core.dart_tip_detector import (
    DEFAULT_WIDTH_PX, HIGH_RESIDUAL_MM, UNITS_PER_MM,
    WARN_DARTS_CLOSE, WARN_FEWER_DARTS, WARN_HIGH_RESIDUAL, WARN_NEED_THREE_CAMERAS,
    CameraModel, CamTip, DartTip, TipDetection,
    _point_len_px, _prepare, _ui_box, darts_close_together, h_apply, ring_name,
)
from core.metrics import TipROI, _as_stack, _tip_polarity, tip_box, tip_metrics, tip_rois_for_camera

MAX_DARTS = 3
NEW_DART_OFFSET_PX = 35.0       # a new dart's box appears this far from the others in the camera
NUDGE_MM = 6.0                  # a placement this close to the old position only corrects that camera


class TipEditor:
    def __init__(self):
        self.models: Dict[str, CameraModel] = {}
        self.manual: Dict[Tuple[int, str], Tuple[float, float]] = {}   # (dart id, camera key) -> tip px put by the user
        self.touched: Set[int] = set()                                  # dart ids the user added, moved or removed

    @property
    def has_edits(self) -> bool:
        return bool(self.touched)

    def reset(self) -> None:
        self.manual.clear()
        self.touched.clear()

    # -- edits --------------------------------------------------------------------------

    def move(self, det: TipDetection, key: str, dart_id: int, px: Sequence[float]) -> bool:
        """The tip of dart `dart_id` is at `px` in camera `key`."""
        if key not in self.models or self._tip(det, dart_id) is None:
            return False
        self.manual[(dart_id, key)] = (float(px[0]), float(px[1]))
        self.touched.add(dart_id)
        self._update(det, dart_id)
        return True

    def add(self, det: TipDetection, key: str, near: Optional[Sequence[Tuple[float, float]]] = None) -> Optional[int]:
        """A dart the finder missed. Its box starts next to `near` (the camera's other tips) as a
        placeholder, hidden until the user puts it on the tip; returns its id, None when full."""
        free = [i for i in range(1, MAX_DARTS + 1) if i not in {t.id for t in det.tips}]
        if not free or key not in self.models:
            return None
        model = self.models[key]
        if near:
            at = (float(np.mean([p[0] for p in near])) + NEW_DART_OFFSET_PX,
                  float(np.mean([p[1] for p in near])) + NEW_DART_OFFSET_PX)
        else:
            at = (model.size[0] / 2.0, model.size[1] / 2.0)
        X = h_apply(model.H_img2board, [at])[0]
        det.tips.append(DartTip(id=free[0], board=(float(X[0]), float(X[1])), board_mm=(0.0, 0.0), ring_hint="",
                                confidence=0.0, n_cams=0, line_residual_mm=0.0, per_cam={}))
        det.tips.sort(key=lambda t: t.id)
        self.touched.add(free[0])
        self._update(det, free[0])
        return free[0]

    def remove(self, det: TipDetection, dart_id: int) -> bool:
        if self._tip(det, dart_id) is None:
            return False
        det.tips[:] = [t for t in det.tips if t.id != dart_id]
        for k in [k for k in self.manual if k[0] == dart_id]:
            del self.manual[k]
        self.touched.add(dart_id)
        self._refresh_warnings(det)
        return True

    # -- results ------------------------------------------------------------------------

    def rois(self, det: TipDetection, key: str, frames) -> List[TipROI]:
        """TipROIs of one camera: the finder's, except where the user put the tip by hand."""
        stack = _as_stack(frames)
        mean = stack.mean(axis=0)
        h, w = mean.shape[:2]
        out: List[TipROI] = []
        for r in tip_rois_for_camera(det, key, stack):
            px = self.manual.get((r.dart_id, key))
            if px is not None:
                tip = (min(w - 1.0, max(0.0, px[0])), min(h - 1.0, max(0.0, px[1])))
                r = dataclasses.replace(r, tip=tip, box=tip_box(tip, mean.shape), visible=True, marginal=False,
                                        hidden_reason=None, faint=False, manual=True)
                r.polarity = _tip_polarity(mean, r)
                r.ref_contrast = float(tip_metrics(stack, r, mean).contrast)
            out.append(r)
        return out

    # -- internals ----------------------------------------------------------------------

    @staticmethod
    def _tip(det: TipDetection, dart_id: int) -> Optional[DartTip]:
        return next((t for t in det.tips if t.id == dart_id), None)

    def _update(self, det: TipDetection, dart_id: int) -> None:
        """Board position of the dart from the user's placements, and every camera's box from it."""
        tip = self._tip(det, dart_id)
        placed = {k: px for (i, k), px in self.manual.items() if i == dart_id}
        if placed:
            pts = [h_apply(self.models[k].H_img2board, [px])[0] for k, px in placed.items()]
            X = np.mean(pts, axis=0)
            spread = max(float(np.linalg.norm(p - X)) for p in pts) / UNITS_PER_MM
        else:
            X, spread = np.asarray(tip.board, float), 0.0
        # A nudge keeps the finder's tip in the cameras the user did not touch; a larger move says the
        # dart is somewhere else, so those cameras follow the new board position.
        nudge = bool(placed) and bool(tip.per_cam) and float(np.linalg.norm(X - np.asarray(tip.board))) <= NUDGE_MM * UNITS_PER_MM
        per_cam: Dict[str, CamTip] = {}
        for key, model in self.models.items():
            c = _prepare(model)
            if key not in placed and nudge and key in tip.per_cam and tip.per_cam[key].seen_axis:
                per_cam[key] = tip.per_cam[key]
                continue
            if key in placed:
                p = np.asarray(placed[key], float)
            else:
                p = h_apply(c["Hinv"], [X])[0]
            old = tip.per_cam.get(key)
            d = np.asarray(old.direction if old is not None else c["tip_dir"], float)
            d = d / (np.linalg.norm(d) or 1.0)
            L = _point_len_px(c, X)
            per_cam[key] = CamTip(px=(float(p[0]), float(p[1])), direction=(float(d[0]), float(d[1])), point_len=float(L),
                                  ui_box=_ui_box(p, d, L, DEFAULT_WIDTH_PX, model.size), seen_axis=key in placed,
                                  occluded=False, point_confirmed=key in placed)
        tip.board = (float(X[0]), float(X[1]))
        tip.board_mm = ((X[0] - 500.0) / UNITS_PER_MM, (500.0 - X[1]) / UNITS_PER_MM)
        tip.ring_hint = ring_name(X)
        tip.confidence = 1.0 if placed else 0.0
        tip.n_cams = len(placed)
        tip.line_residual_mm = float(spread)
        tip.per_cam = per_cam
        self._refresh_warnings(det)

    @staticmethod
    def _refresh_warnings(det: TipDetection) -> None:
        """The finder's warnings describe its own result: after an edit only these still apply."""
        warnings = [w for w in det.warnings if w == WARN_NEED_THREE_CAMERAS]
        if len(det.tips) < MAX_DARTS:
            warnings.append(WARN_FEWER_DARTS)
        if darts_close_together(det.tips):
            warnings.append(WARN_DARTS_CLOSE)
        if any(t.line_residual_mm > HIGH_RESIDUAL_MM for t in det.tips):
            warnings.append(WARN_HIGH_RESIDUAL)
        det.warnings[:] = warnings
