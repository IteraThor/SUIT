"""Multi-camera dart tip detector (no empty-board reference needed).

Pipeline:
 1. per-pixel median of the burst per camera
 2. warp every camera onto the board plane (full 1000x1000 board grid, cached remap tables) and
    match the cameras photometrically (per-channel quantile mapping in Lab)
 3. 3-camera consensus: a board pixel where camera i disagrees with BOTH others while the other
    two agree is "off-plane in camera i" -> dart evidence (wires, numbers and segments cancel)
 4. back-project the evidence into camera i and find narrow runs along the camera's tip
    direction -> dart axes; the barrel end is the lowest point of each run
 5. every image axis maps to a line on the board plane that passes through the tip (true for any
    dart tilt). Intersect the axes across cameras (3 cameras preferred, 2 allowed), gated by a
    pose-based prior of the visible point length -> board tip
 6. project the board tip into every camera -> pixel tip, axis direction, point length, UI box

Pure numpy / OpenCV, no GTK. Board coordinates use the Autodarts 1000-unit space (bull at 500,500,
1.9 units per mm, y grows away from the 20).
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import math
import time
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

UNITS_PER_MM = 1.9
MERGE_END_PX = 10.0                     # collinear-axis merge: end / top offset from the kept axis line
MERGE_TOP_PX = 12.0
BOARD_UNITS = 1000
# Board-space working grid (px). 500 was tried: it halves the runtime but drops axes on the rig
# fixtures (tips matched in only 2 cameras), so the grid stays at the calibrated 1000-unit scale.
WORK_SIZE = 1000
WORK_SCALE = WORK_SIZE / BOARD_UNITS
EVIDENCE_R = 470.0                      # board units; consensus is evaluated inside this radius
SEARCH_R = 440.0                        # board units; evidence outside is ignored in the image
ON_BOARD_R = 345.0                      # board units; a tip prior beyond this is off the board
POINT_MM = 20.0                         # assumed visible steel-point length (pose prior)
DEFAULT_WIDTH_PX = 13.0                 # barrel width used when a camera saw no axis

# Warning keys in TipDetection.warnings (the UI maps them to plain-language text).
WARN_NEED_THREE_CAMERAS = "need_three_cameras"
WARN_FEWER_DARTS = "fewer_darts"
WARN_DARTS_CLOSE = "darts_close"
WARN_HIGH_RESIDUAL = "high_residual"

SIL_MIN_LEN = 50                        # silhouette: a barrel + shaft is straight inside the mask for this long (px)
SIL_MAX_WIDTH = 32.0                    # widest barrel accepted (px)
REF_MIN_DIFF = 12.0                     # Lab units; reference mode never thresholds below this
REF_NOISE_K = 6.0                       # ... nor below this multiple of the median change on the board
REF_TIP_LOW = 0.45                      # tip walk: the needle continues while the change stays above this part of thr
REF_TIP_MAX_PX = 40                     # tip walk: never further than this past the mask end

CLOSE_TIPS_MM = 8.0                     # tips nearer than this are touching / robin-hood
HIGH_RESIDUAL_MM = 3.0                  # axis lines missing each other by more: stale homography?


@dataclasses.dataclass
class CameraModel:
    id: str                                  # stable camera id (USB port by-path link)
    H_img2board: np.ndarray                  # 3x3, at capture resolution
    tip_dir: Tuple[float, float]             # unit vector flight -> tip in the image
    focal: float                             # px, from calibration.json "undistorted"
    size: Tuple[int, int] = (1280, 960)      # (width, height) of the capture
    dev_node: Optional[str] = None
    _cache: dict = dataclasses.field(default_factory=dict, repr=False, compare=False)


@dataclasses.dataclass
class CamTip:
    px: Tuple[float, float]                  # tip in image px (board point projected)
    direction: Tuple[float, float]           # dart axis flight -> tip in this camera (fallback: tip_dir)
    point_len: float                         # expected visible steel length in px (pose prior)
    ui_box: Tuple[int, int, int, int]        # x, y, w, h around tip + point + barrel end (overlays)
    seen_axis: bool                          # this camera contributed an axis to the match
    occluded: bool                           # geometric: projected tip lies behind another dart's barrel
    point_confirmed: bool                    # diagnostic only, never gates anything


@dataclasses.dataclass
class DartTip:
    id: int                                  # 1..n by distance from the bull, outermost first
    board: Tuple[float, float]               # 1000-unit board space
    board_mm: Tuple[float, float]            # mm from the bull (x right, y up toward the 20)
    ring_hint: str
    confidence: float
    n_cams: int
    line_residual_mm: float
    per_cam: Dict[str, CamTip] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class TipDetection:
    tips: List[DartTip]
    rejected: Dict[str, list]                # per camera: unmatched axes {"end", "top", "width"}
    runtime_s: float
    warnings: List[str]
    debug: Optional[Dict[str, np.ndarray]] = None   # evidence masks, only when debug=True


# ----------------------------------------------------------------------------------------------
# Camera models
# ----------------------------------------------------------------------------------------------

def load_camera_models(cams: list, width: int, height: int, detector=None) -> Dict[str, CameraModel]:
    """Build a CameraModel per camera from Autodarts' calibration.json.

    `cams` are CameraDevice-like objects (by_path, dev_node, index). A camera without a homography
    at a compatible resolution is omitted.
    """
    if detector is None:
        from core.board_detector import BoardDetector
        detector = BoardDetector()
    models: Dict[str, CameraModel] = {}
    for cam in cams:
        by_path = getattr(cam, "by_path", None)
        dev_node = getattr(cam, "dev_node", None) or getattr(cam, "current_dev_node", None)
        cam_id = by_path or dev_node
        entry = detector.load_calibration_entry(width, height, dev_node=dev_node, by_path=by_path,
                                                cam_idx=getattr(cam, "index", None))
        if not entry or cam_id is None:
            logger.info("No calibration homography for %s at %dx%d", cam_id, width, height)
            continue
        tip_dir = entry.get("tip_dir") or (0.0, 1.0)
        n = math.hypot(*tip_dir) or 1.0
        focal = float(entry.get("undistorted", {}).get("focal") or 0.685 * width)
        models[cam_id] = CameraModel(id=cam_id, H_img2board=np.asarray(entry["homography"], np.float64),
                                     tip_dir=(tip_dir[0] / n, tip_dir[1] / n), focal=focal,
                                     size=(int(width), int(height)), dev_node=dev_node)
    return models


def h_apply(H: np.ndarray, pts) -> np.ndarray:
    pts = np.atleast_2d(np.asarray(pts, np.float64))
    q = np.c_[pts, np.ones(len(pts))] @ H.T
    return q[:, :2] / q[:, 2:3]


def _prepare(model: CameraModel) -> dict:
    """Per-camera geometry that only depends on the homography: remap tables, pose, search crop."""
    c = model._cache
    key = (model.H_img2board.tobytes(), model.size, model.focal)
    if c.get("key") == key:
        return c
    c.clear()
    c["key"] = key
    W, H = model.size
    Hm = np.asarray(model.H_img2board, np.float64)
    Hinv = np.linalg.inv(Hm)
    c["H"], c["Hinv"] = Hm, Hinv

    # Board work grid -> image coordinates (replaces warpPerspective onto the board plane).
    vv, uu = np.mgrid[0:WORK_SIZE, 0:WORK_SIZE].astype(np.float64)
    src = h_apply(Hinv, np.c_[uu.ravel() / WORK_SCALE, vv.ravel() / WORK_SCALE]).reshape(WORK_SIZE, WORK_SIZE, 2)
    mx, my = src[..., 0].astype(np.float32), src[..., 1].astype(np.float32)
    c["valid"] = (mx >= 0) & (mx <= W - 1) & (my >= 0) & (my <= H - 1)
    c["to_board"] = cv2.convertMaps(mx, my, cv2.CV_16SC2)

    # Image crop that covers the board search radius, with its image -> work-grid tables.
    yy, xx = np.mgrid[0:H, 0:W]
    b = h_apply(Hm, np.c_[xx.ravel(), yy.ravel()]).reshape(H, W, 2)
    r_img = np.hypot(b[..., 0] - 500.0, b[..., 1] - 500.0)
    inside = r_img <= SEARCH_R + 20
    ys, xs = np.where(inside)
    if len(xs) == 0:
        x0, y0, x1, y1 = 0, 0, W, H
    else:
        x0, y0 = max(0, int(xs.min()) - 8), max(0, int(ys.min()) - 8)
        x1, y1 = min(W, int(xs.max()) + 9), min(H, int(ys.max()) + 9)
    c["crop"] = (x0, y0, x1, y1)
    bc = b[y0:y1, x0:x1] * WORK_SCALE
    c["to_image"] = cv2.convertMaps(bc[..., 0].astype(np.float32), bc[..., 1].astype(np.float32), cv2.CV_16SC2)
    c["far"] = r_img[y0:y1, x0:x1] > SEARCH_R

    # Pose from the homography (K from the undistorted calibration, distortion ignored).
    K = np.array([[model.focal, 0, W / 2.0], [0, model.focal, H / 2.0], [0, 0, 1.0]])
    M = np.linalg.inv(K) @ Hinv
    M = M / np.sqrt(np.linalg.norm(M[:, 0]) * np.linalg.norm(M[:, 1]))
    if M[2, 2] < 0:
        M = -M
    r1, r2, t = M[:, 0], M[:, 1], M[:, 2]
    r3 = np.cross(r1 / np.linalg.norm(r1), r2 / np.linalg.norm(r2))
    R = np.column_stack([r1, r2, r3])
    c["K"], c["R"], c["t"], c["C"] = K, R, t, -R.T @ t
    td = np.asarray(model.tip_dir, np.float64)
    c["tip_dir"] = td / (np.linalg.norm(td) or 1.0)
    return c


def _point_len_px(c: dict, b) -> float:
    """Projected length in px of a POINT_MM steel point standing on board point b."""
    def proj(X):
        q = c["K"] @ (c["R"] @ X + c["t"])
        return q[:2] / q[2]
    h = np.sign(c["C"][2]) * POINT_MM * UNITS_PER_MM
    return float(np.linalg.norm(proj(np.r_[b, h]) - proj(np.r_[b, 0.0])))


# ----------------------------------------------------------------------------------------------
# Evidence
# ----------------------------------------------------------------------------------------------

def _median(frames: Sequence[np.ndarray]) -> np.ndarray:
    if len(frames) == 1:
        return frames[0]
    if len(frames) == 3:
        a, b, c = frames
        return np.maximum(np.minimum(a, b), np.minimum(np.maximum(a, b), c))
    return np.median(np.stack(frames), axis=0).astype(np.uint8)


def _tol_diff(a: np.ndarray, b: np.ndarray, k: np.ndarray) -> np.ndarray:
    """Colour distance that tolerates a small misregistration (dilate/erode envelope)."""
    d = np.zeros(a.shape[:2], np.float32)
    for ch in range(3):
        x, y = a[..., ch], b[..., ch]
        d1 = np.maximum(0, np.maximum(x - cv2.dilate(y, k), cv2.erode(y, k) - x))
        d2 = np.maximum(0, np.maximum(y - cv2.dilate(x, k), cv2.erode(x, k) - y))
        d += np.minimum(d1, d2) ** 2
    return np.sqrt(d)


def _anomaly_maps(meds: List[np.ndarray], preps: List[dict]) -> List[np.ndarray]:
    labs, vals = [], []
    for med, c in zip(meds, preps):
        w = cv2.remap(cv2.GaussianBlur(med, (0, 0), 1.0), *c["to_board"], cv2.INTER_LINEAR)
        labs.append(cv2.cvtColor(w.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab))
        vals.append(c["valid"])
    yy, xx = np.mgrid[0:WORK_SIZE, 0:WORK_SIZE]
    half = WORK_SIZE / 2.0
    m = ((xx - half) ** 2 + (yy - half) ** 2) < (EVIDENCE_R * WORK_SCALE) ** 2
    for v in vals:
        m &= v
    if m.sum() < 1000:
        return [np.zeros((WORK_SIZE, WORK_SIZE), np.float32) for _ in labs]
    # Photometric normalisation: per-channel piecewise-linear quantile mapping to the cross-camera mean.
    qs = np.linspace(1, 99, 25)
    srcq = [[np.percentile(l[..., ch][m], qs) for ch in range(3)] for l in labs]
    refq = np.mean(srcq, axis=0)
    for l, sq in zip(labs, srcq):
        for ch in range(3):
            l[..., ch] = np.interp(l[..., ch], sq[ch], refq[ch]).astype(np.float32)
    # Misregistration tolerance of ~3.5 board units (homography error, board relief).
    kk = max(3, int(round(7 * WORK_SCALE)) | 1)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kk, kk))
    D = {}
    for i in range(3):
        for j in range(i + 1, 3):
            D[i, j] = D[j, i] = _tol_diff(labs[i], labs[j], k)
    out = []
    for i in range(3):
        j, k2 = [x for x in range(3) if x != i]
        s = np.clip(np.minimum(D[i, j], D[i, k2]) - D[j, k2], 0, None)
        s[~m] = 0
        out.append(s.astype(np.float32))
    return out


def _reference_evidence(ref: np.ndarray, cur: np.ndarray) -> np.ndarray:
    """Colour change (Lab distance) between the empty-board reference and the frame with darts.

    Same camera, same settings, so nothing but the darts (and their shadows) changes; a 3x3
    tolerance absorbs sub-pixel shake on the wires and number ring.
    """
    labs = [cv2.cvtColor(cv2.GaussianBlur(f, (0, 0), 0.8).astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)
            for f in (ref, cur)]
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    return _tol_diff(labs[0], labs[1], k)


def _reference_mask(E: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Binary dart mask from reference evidence: a noise-relative threshold, then cleanup."""
    vals = E[valid]
    noise = float(np.median(vals)) if vals.size else 1.0
    thr = max(REF_MIN_DIFF, REF_NOISE_K * noise)
    E = np.where(valid, E, 0)
    return _dart_mask(E, thr)


def _dart_mask(E: np.ndarray, thr: float = 7.0) -> np.ndarray:
    m = (E > thr).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 11)))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 15)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(m)
    small = np.where(st[:, cv2.CC_STAT_AREA] < 120)[0]
    small = small[small > 0]
    if len(small):
        m[np.isin(lab, small)] = 0
    return m


def _axes_from_mask(m: np.ndarray, tip_dir: np.ndarray, band: int = 13, up: int = 140, max_dev: float = 40,
                    corridor: bool = False) -> List[dict]:
    """Dart axes in a binary evidence mask: narrow runs along tip_dir, ending at the barrel end."""
    ang = math.degrees(math.atan2(tip_dir[0], tip_dir[1]))      # rotate so tip_dir -> +y
    Hh, Ww = m.shape
    Mrot = cv2.getRotationMatrix2D((Ww / 2.0, Hh / 2.0), -ang, 1.0)
    # Expand the canvas so nothing is rotated out of view.
    cos, sin = abs(Mrot[0, 0]), abs(Mrot[0, 1])
    nW, nH = int(Hh * sin + Ww * cos) + 2, int(Hh * cos + Ww * sin) + 2
    Mrot[0, 2] += nW / 2.0 - Ww / 2.0
    Mrot[1, 2] += nH / 2.0 - Hh / 2.0
    mr = cv2.warpAffine(m, Mrot, (nW, nH), flags=cv2.INTER_NEAREST)
    Minv = cv2.invertAffineTransform(Mrot)
    Hh, Ww = mr.shape
    on = mr > 0
    B = on & np.vstack([~on[1:], np.ones((1, Ww), bool)])         # lowest pixel of each vertical run
    Bd = cv2.dilate(B.astype(np.uint8), np.ones((3, 7), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(Bd)
    cands = []
    for k in range(1, n):
        if st[k, cv2.CC_STAT_WIDTH] > 44:
            continue
        x, y, w, h = st[k, cv2.CC_STAT_LEFT], st[k, cv2.CC_STAT_TOP], st[k, cv2.CC_STAT_WIDTH], st[k, cv2.CC_STAT_HEIGHT]
        yy, xx = np.where((lab[y:y + h, x:x + w] == k) & B[y:y + h, x:x + w])
        if len(xx) < 3:
            continue
        cands.append((int(round(xx.mean())) + x, float(yy.max() + y)))
    axes = []
    for xc, yb in cands:
        # Contiguous run upward in the column band.
        x0, x1 = max(0, xc - band), min(Ww, xc + band + 1)
        ytop = int(max(0, yb - up))
        sub = on[ytop:int(yb) + 1, x0:x1]
        centre = xc - x0
        pts, widths = [], []
        for r in range(sub.shape[0] - 1, -1, -1):
            row = sub[r]
            cols = np.flatnonzero(row)
            if len(cols) == 0:
                if pts and (sub.shape[0] - 1 - r) > len(pts) + 6:
                    break
                continue
            # Keep the run of columns nearest the centre.
            c0 = int(cols[np.argmin(np.abs(cols - centre))])
            lo = c0
            while lo > 0 and row[lo - 1]:
                lo -= 1
            hi = c0
            while hi < len(row) - 1 and row[hi + 1]:
                hi += 1
            widths.append(hi - lo + 1)
            pts.append((x0 + (lo + hi) / 2.0, ytop + r))
            centre = (lo + hi) / 2.0       # follow a dart that leans away from the tip direction
        if len(pts) < 35:
            continue
        P = np.array(pts, float)
        wmed = float(np.median(widths))
        if wmed > 32 or wmed < 4:
            continue
        # Robust line fit x = a*y + b on the lowest 100 px (the barrel).
        P = P[P[:, 1] > yb - 100]
        A = np.c_[P[:, 1], np.ones(len(P))]
        coef = np.linalg.lstsq(A, P[:, 0], rcond=None)[0]
        res = np.abs(A @ coef - P[:, 0])
        keep = res < max(2.5, np.percentile(res, 80))
        coef = np.linalg.lstsq(A[keep], P[keep, 0], rcond=None)[0]
        if abs(math.degrees(math.atan(coef[0]))) > max_dev:
            continue
        end = np.array([coef[0] * yb + coef[1], yb])
        top = np.array([coef[0] * (yb - len(pts)) + coef[1], yb - len(pts)])
        e = Minv @ np.r_[end, 1]
        tp = Minv @ np.r_[top, 1]
        d = (e - tp) / np.linalg.norm(e - tp)
        axes.append(dict(end=e, top=tp, d=d, width=wmed, length=float(len(pts))))
    # Merge collinear axes (barrel grooves split the mask): keep the lowest end, longest support.
    out: List[dict] = []
    for a in sorted(axes, key=lambda a: -(a["end"] @ tip_dir)):
        dup = False
        for b in out:
            nb = np.array([-b["d"][1], b["d"][0]])
            if abs((a["end"] - b["end"]) @ nb) < MERGE_END_PX and abs((a["top"] - b["end"]) @ nb) < MERGE_TOP_PX \
                    and (b["end"] - a["end"]) @ b["d"] >= -3:
                b["length"] = max(b["length"], a["length"] + float((b["end"] - a["end"]) @ b["d"]))
                dup = True
                break
            # A complete silhouette (reference mode) also yields "ends" on the barrel rings and the
            # flight: any end inside a longer dart's body corridor is part of that dart.
            behind = (b["end"] - a["end"]) @ b["d"]
            if corridor and abs((a["end"] - b["end"]) @ nb) < b["width"] / 2 + 4 and 0 < behind < b["length"] + 20:
                dup = True
                break
        if not dup:
            out.append(a)
    return out


def _axes_from_silhouette(m: np.ndarray, tip_dir: np.ndarray, max_dev: float = 45.0) -> List[dict]:
    """Dart axes in a complete dart silhouette (reference mode).

    Every pixel on the lower boundary of the mask (no mask just ahead of it along the tip
    direction) is a candidate barrel end. From each candidate, rays are cast back toward the flight
    at angles within max_dev of the camera's tip direction; a ray scores the length it stays deep
    inside the mask (centre and +-3 px both set). The best (end, angle) pairs are the darts: this
    holds for leaning darts and for darts that touch or overlap in this view, because each barrel
    is its own straight strip. The axis is then refit sub-pixel on the barrel's perpendicular run
    centres and the end is walked forward to the last mask pixel on the axis.
    """
    H, W = m.shape
    on = m > 0
    if on.sum() < 50:
        return []
    td = tip_dir / (np.linalg.norm(tip_dir) or 1.0)

    def sample(pts):
        q = np.round(pts).astype(np.int64)
        ok = (q[..., 0] >= 0) & (q[..., 0] < W) & (q[..., 1] >= 0) & (q[..., 1] < H)
        out = np.zeros(q.shape[:-1], bool)
        out[ok] = on[q[..., 1][ok], q[..., 0][ok]]
        return out

    # Candidate ends: mask pixels with nothing 3 px ahead along the tip direction, thinned to a 4 px grid.
    ys, xs = np.nonzero(on)
    P = np.stack([xs, ys], axis=1).astype(np.float64)
    ahead = sample(P + 3 * td)
    P = P[~ahead]
    if not len(P):
        return []
    _, idx = np.unique((P // 4).astype(np.int64), axis=0, return_index=True)
    P = P[idx]

    base = math.atan2(td[1], td[0])
    phis = np.radians(np.arange(-max_dev, max_dev + 0.1, 2.0))
    D = np.stack([np.cos(base + phis), np.sin(base + phis)], axis=1)          # flight -> tip
    N = np.stack([-D[:, 1], D[:, 0]], axis=1)
    ts = np.arange(4.0, 124.0, 2.0)
    # pts[c, a, t] = P[c] - ts[t] * D[a] (+- 3 px sideways)
    back = P[:, None, None, :] - ts[None, None, :, None] * D[None, :, None, :]
    inside = sample(back) & sample(back + 3 * N[None, :, None, :]) & sample(back - 3 * N[None, :, None, :])
    # Run length from the end: first miss (one missing sample tolerated for barrel grooves).
    miss = ~inside
    miss2 = miss & np.concatenate([miss[..., 1:], np.ones(miss.shape[:-1] + (1,), bool)], axis=-1)
    first = np.where(miss2.any(axis=-1), miss2.argmax(axis=-1), len(ts))
    score = first * 2.0                                                        # px
    score -= 0.05 * np.abs(np.degrees(phis))[None, :]                         # prefer the tip direction on ties

    def perp_run(c, d):
        n = np.array([-d[1], d[0]])
        offs = np.arange(-24, 25)
        v = sample(c[None, :] + offs[:, None] * n[None, :])
        c0 = 24
        if not v[c0]:
            return None
        lo = c0
        while lo > 0 and v[lo - 1]:
            lo -= 1
        hi = c0
        while hi < len(v) - 1 and v[hi + 1]:
            hi += 1
        return (offs[lo] + offs[hi]) / 2.0, float(hi - lo + 1)

    axes: List[dict] = []
    flat = score.ravel().copy()
    order = np.argsort(-flat)
    for o in order[:4000]:
        if flat[o] < SIL_MIN_LEN or len(axes) >= 12:
            break
        ci, ai = divmod(int(o), len(phis))
        p, d = P[ci], D[ai]
        n = np.array([-d[1], d[0]])
        if any(abs((p - b["end"]) @ np.array([-b["d"][1], b["d"][0]])) < b["width"] / 2 + 5
               and -12 < (b["end"] - p) @ b["d"] < b["length"] + 10 for b in axes):
            continue                                                           # part of a dart already found
        cs, ws = [], []
        for t in np.arange(6.0, min(96.0, flat[o]), 2.0):
            pr = perp_run(p - t * d, d)
            if pr is not None:
                cs.append((t, pr[0]))
                ws.append(pr[1])
        if len(cs) < 10:
            continue
        width = float(np.median(ws))
        if width > SIL_MAX_WIDTH:
            continue
        T = np.array(cs)
        k1, k0 = np.polyfit(T[:, 0], T[:, 1], 1)
        res = np.abs(T[:, 1] - (k1 * T[:, 0] + k0))
        keep = res <= max(1.5, float(np.percentile(res, 75)))
        if keep.sum() >= 6:
            k1, k0 = np.polyfit(T[keep, 0], T[keep, 1], 1)
        # offset(t) = k1 * t + k0 along -d: the refit axis through p + k0 * n with direction d - k1 * n
        d2 = d - k1 * n
        d2 = d2 / np.linalg.norm(d2)
        q = p + k0 * n
        # Walk forward to the last mask pixel on the axis (the barrel end, or the needle when visible).
        fw = np.arange(0.0, 40.0, 1.0)
        hit = sample(q[None, :] + fw[:, None] * d2[None, :])
        k = 0
        while k + 1 < len(fw) and (hit[k + 1] or (k + 2 < len(fw) and hit[k + 2])):
            k += 1
        end = q + fw[k] * d2
        length = float(flat[o]) + fw[k]
        axes.append(dict(end=end, top=end - min(140.0, length) * d2, d=d2, width=width, length=length))
    return axes


def _trace_point(g: np.ndarray, start: np.ndarray, d: np.ndarray, max_len: int = 70) -> Tuple[np.ndarray, float]:
    """From the barrel end, walk along d while a thin contrasting ridge persists (diagnostic only)."""
    n = np.array([-d[1], d[0]])
    ks = np.arange(0, max(1, max_len), 1.0)
    offs = np.arange(-9, 10)
    pts = start[None, None, :] + ks[:, None, None] * d[None, None, :] + offs[None, :, None] * n[None, None, :]
    prof = cv2.remap(g, pts[..., 0].astype(np.float32), pts[..., 1].astype(np.float32), cv2.INTER_LINEAR)
    best = np.zeros(len(ks), np.float32)
    for o in range(6, 13):
        core = prof[:, o - 1:o + 2].mean(axis=1)
        side = np.median(np.concatenate([prof[:, max(o - 7, 0):o - 3], prof[:, o + 4:o + 8]], axis=1), axis=1)
        val = np.where(side > core, side - core, 0.5 * (core - side))
        best = np.maximum(best, val)
    sm = np.convolve(best, np.ones(5) / 5, "same")
    on = sm > max(10, 0.35 * np.percentile(sm[:15], 80)) if len(sm) > 15 else sm > 10
    # Tip = last index of the first continuous "on" run (gaps up to 4 allowed).
    last, gap = 0, 0
    for k, v in enumerate(on):
        if v:
            last, gap = k, 0
        else:
            gap += 1
            if gap > 4:
                break
    return start + last * d, float(last)


def _dist_to_segment(x: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    ab = b - a
    t = float(np.clip((x - a) @ ab / max(ab @ ab, 1e-9), 0.0, 1.0))
    return float(np.linalg.norm(x - (a + t * ab)))


def _ls_intersect(lines) -> Optional[np.ndarray]:
    A = np.zeros((2, 2))
    b = np.zeros(2)
    for p, u in lines:
        n = np.array([-u[1], u[0]])
        A += np.outer(n, n)
        b += np.outer(n, n) @ p
    if np.linalg.cond(A) > 1e3:
        return None
    return np.linalg.solve(A, b)


def ring_name(b) -> str:
    r = math.hypot(b[0] - 500, b[1] - 500) / UNITS_PER_MM
    for lim, name in [(6.35, "bull"), (16, "outer bull"), (99, "inner single"), (107, "treble"),
                      (162, "outer single"), (170, "double")]:
        if r <= lim:
            return name
    return "off board"


def _ui_box(tip, d, L, w, size) -> Tuple[int, int, int, int]:
    pts = [tip + 8 * d, tip - (L + 14) * d]
    hw = max(20.0, 0.9 * w + 8)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = int(min(xs) - hw), int(max(xs) + hw)
    y0, y1 = int(min(ys) - 6), int(max(ys) + 6)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(size[0] - 1, x1), min(size[1] - 1, y1)
    return (x0, y0, max(0, x1 - x0), max(0, y1 - y0))


# ----------------------------------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------------------------------

def _burst_median(burst: Sequence[np.ndarray], size: Tuple[int, int]) -> np.ndarray:
    fr = [f if f.ndim == 3 else cv2.cvtColor(f, cv2.COLOR_GRAY2BGR) for f in burst]
    fr = [f if (f.shape[1], f.shape[0]) == size else cv2.resize(f, size, interpolation=cv2.INTER_AREA) for f in fr]
    return _median(fr)


def detect_dart_tips(frames: Dict[str, List[np.ndarray]], cams: Dict[str, CameraModel],
                     expected: int = 3, debug: bool = False,
                     references: Optional[Dict[str, List[np.ndarray]]] = None) -> TipDetection:
    """Find the dart tips seen by exactly three calibrated cameras.

    `frames` maps camera id -> burst (BGR, capture resolution; 1..N frames, 3 recommended).
    `references` (same keys) are bursts of the EMPTY board taken with the same settings. With
    them every camera finds its darts on its own by change detection (robust to leaning,
    overlapping and dark darts) and the tip is measured in the image; without them the darts are
    found by 3-camera consensus on the board plane.
    Fewer than 3 usable cameras gives an empty result with WARN_NEED_THREE_CAMERAS.
    """
    t0 = time.perf_counter()
    ids = [cid for cid in cams if frames.get(cid)]
    if len(ids) < 3:
        return TipDetection([], {}, time.perf_counter() - t0, [WARN_NEED_THREE_CAMERAS])
    if len(ids) > 3:
        logger.warning("Tip detection uses 3 cameras, ignoring %s", ids[3:])
        ids = ids[:3]
    models = [cams[cid] for cid in ids]
    preps = [_prepare(m) for m in models]
    meds = [_burst_median(frames[m.id], m.size) for m in models]
    use_ref = references is not None and all(references.get(m.id) for m in models)
    if use_ref:
        A = [_reference_evidence(_burst_median(references[m.id], m.size), med) for m, med in zip(models, meds)]
    else:
        A = _anomaly_maps(meds, preps)
    dbg: Optional[Dict[str, np.ndarray]] = {} if debug else None

    per: List[List[dict]] = []
    ref_thr: List[float] = []
    for i, (m, c, med) in enumerate(zip(models, preps, meds)):
        x0, y0, x1, y1 = c["crop"]
        if use_ref:
            Aimg = A[i][y0:y1, x0:x1]
            mask = _reference_mask(Aimg, ~c["far"])
            vals = Aimg[~c["far"]]
            ref_thr.append(max(REF_MIN_DIFF, REF_NOISE_K * float(np.median(vals)) if vals.size else REF_MIN_DIFF))
        else:
            Aimg = cv2.remap(A[i], *c["to_image"], cv2.INTER_LINEAR)
            Aimg[c["far"]] = 0
            mask = _dart_mask(Aimg)
        if dbg is not None:
            full = np.zeros((m.size[1], m.size[0]), np.uint8)
            full[y0:y1, x0:x1] = mask
            dbg[f"mask:{m.id}"] = full
            dbg[f"evidence:{m.id}"] = A[i]
        axes = _axes_from_silhouette(mask, c["tip_dir"]) if use_ref else _axes_from_mask(mask, c["tip_dir"])
        g = cv2.GaussianBlur(cv2.cvtColor(med, cv2.COLOR_BGR2GRAY).astype(np.float32), (0, 0), 0.8)
        off = np.array([x0, y0], float)
        for ax in axes:
            ax["end"] = ax["end"] + off
            ax["top"] = ax["top"] + off
            L = _point_len_px(c, h_apply(c["H"], [ax["end"]])[0])
            ax["prior_tip"] = ax["end"] + L * ax["d"]
            ax["trace_tip"], _ = _trace_point(g, ax["end"], ax["d"], max_len=int(1.8 * L))
            ax["anchor"] = h_apply(c["H"], [ax["prior_tip"]])[0]
            # The evidence mask often stops above the barrel end (steel barrels are dark and low in
            # contrast), so the tip may lie anywhere between the mask end and the traced point plus
            # the pose prior: the anchor is that segment, not a single point.
            ax["anchor_far"] = h_apply(c["H"], [ax["trace_tip"] + L * ax["d"]])[0]
            ax["anchor_near"] = h_apply(c["H"], [ax["end"]])[0]
            pb = h_apply(c["H"], [ax["end"], ax["end"] - 60 * ax["d"]])
            u = pb[1] - pb[0]
            ax["p"], ax["u"] = pb[0], u / np.linalg.norm(u)        # board line, direction tip -> flight
            ax["cam"] = i
            ax["on_board"] = bool(np.hypot(*(ax["anchor"] - 500)) < ON_BOARD_R)
        per.append(axes)

    # Cross-camera association: every 3- and 2-camera combination of axes is a tip hypothesis.
    allax = [a for axes in per for a in axes if a["on_board"]]
    hyps = []
    for k in (3, 2):
        for comb in itertools.combinations(range(len(allax)), k):
            if len({allax[j]["cam"] for j in comb}) < k:
                continue
            X = _ls_intersect([(allax[j]["p"], allax[j]["u"]) for j in comb])
            if X is None:
                continue
            dl = [abs((X - allax[j]["p"]) @ np.array([-allax[j]["u"][1], allax[j]["u"][0]])) for j in comb]
            da = [_dist_to_segment(X, allax[j]["anchor_near"], allax[j]["anchor_far"]) for j in comb]
            along = [(X - allax[j]["p"]) @ allax[j]["u"] for j in comb]
            if max(dl) > 12 or max(da) > 40 or max(along) > 8:
                continue
            hyps.append(dict(X=X, members=comb, cost=np.mean(dl) + 0.15 * np.mean(da) - (6 if k == 3 else 0),
                             dl=max(dl), da=max(da)))
    hyps.sort(key=lambda h: h["cost"])
    used: set = set()
    chosen = []
    for h in hyps:
        if used & set(h["members"]):
            continue
        chosen.append(h)
        used |= set(h["members"])
    chosen = chosen[:expected]

    tips: List[DartTip] = []
    for n, h in enumerate(chosen):
        X = h["X"]
        members = {allax[j]["cam"]: allax[j] for j in h["members"]}
        pc: Dict[str, CamTip] = {}
        for i, (m, c) in enumerate(zip(models, preps)):
            p = h_apply(c["Hinv"], [X])[0]
            ax = members.get(i)
            if ax is not None:
                d, w = ax["d"], ax["width"]
                if use_ref:
                    # The silhouette axis is exact in this camera; the board point carries the
                    # homography error, so keep only its position along the axis.
                    p = ax["end"] + ((p - ax["end"]) @ d) * d
            else:
                d, w = c["tip_dir"], DEFAULT_WIDTH_PX
            L = _point_len_px(c, X)
            occl = False
            for other in per[i]:
                if other is ax:
                    continue
                nb = np.array([-other["d"][1], other["d"][0]])
                along = (p - other["end"]) @ other["d"]
                if abs((p - other["end"]) @ nb) < other["width"] / 2 + 2 and -other["length"] < along < -2:
                    occl = True
            terr = float(np.linalg.norm(ax["trace_tip"] - p)) if ax is not None else float("inf")
            pc[m.id] = CamTip(px=(float(p[0]), float(p[1])), direction=(float(d[0]), float(d[1])),
                              point_len=float(L), ui_box=_ui_box(p, d, L, w, m.size),
                              seen_axis=ax is not None, occluded=occl, point_confirmed=terr < 6)
        conf = (0.9 if len(h["members"]) == 3 else 0.65) - 0.02 * h["dl"] - 0.003 * h["da"]
        tips.append(DartTip(id=n + 1, board=(float(X[0]), float(X[1])),
                            board_mm=((X[0] - 500) / UNITS_PER_MM, (500 - X[1]) / UNITS_PER_MM),
                            ring_hint=ring_name(X), confidence=float(np.clip(conf, 0, 1)),
                            n_cams=len(h["members"]), line_residual_mm=float(h["dl"] / UNITS_PER_MM), per_cam=pc))

    matched = {id(allax[j]) for h in chosen for j in h["members"]}
    rejected = {m.id: [dict(end=tuple(map(float, a["end"])), top=tuple(map(float, a["top"])), width=a["width"])
                       for a in per[i] if id(a) not in matched] for i, m in enumerate(models)}

    warnings: List[str] = []
    if len(tips) < expected:
        warnings.append(WARN_FEWER_DARTS)
    if darts_close_together(tips):
        warnings.append(WARN_DARTS_CLOSE)
    if any(t.line_residual_mm > HIGH_RESIDUAL_MM for t in tips):
        warnings.append(WARN_HIGH_RESIDUAL)
    dt = time.perf_counter() - t0
    logger.info("Dart tip detection: %d tips in %.2fs (%s)", len(tips), dt,
                ", ".join(f"#{t.id} {t.ring_hint} conf={t.confidence:.2f} cams={t.n_cams} res={t.line_residual_mm:.2f}mm"
                          for t in tips))
    return TipDetection(tips=tips, rejected=rejected, runtime_s=dt, warnings=warnings, debug=dbg)


def darts_close_together(tips: Sequence[DartTip], min_mm: float = CLOSE_TIPS_MM) -> bool:
    """True for touching / robin-hood tips, or more than one tip matched in only two cameras.

    Three darts in one segment (T20 plus the singles above and below) are the expected
    arrangement and do not count as close.
    """
    if sum(1 for t in tips if t.n_cams < 3) > 1:
        return True
    for a, b in itertools.combinations(tips, 2):
        if math.hypot(a.board_mm[0] - b.board_mm[0], a.board_mm[1] - b.board_mm[1]) < min_mm:
            return True
    return False


def compare_tips(a: TipDetection, b: TipDetection, tol_mm: float = 3.0) -> List[int]:
    """Ids of tips in `a` that moved more than tol_mm, or are missing, in `b`."""
    out = []
    for t in a.tips:
        dists = [math.hypot(t.board_mm[0] - s.board_mm[0], t.board_mm[1] - s.board_mm[1]) for s in b.tips]
        if not dists or min(dists) > tol_mm:
            out.append(t.id)
    return out


def draw_tip_overlay(frame: np.ndarray, detection: TipDetection, cam_id: str) -> np.ndarray:
    """Diagnostic overlay (Details dialog): tip crosses, UI boxes, rejected axes in red."""
    im = frame.copy() if frame.ndim == 3 else cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if detection.debug and f"mask:{cam_id}" in detection.debug:
        mask = detection.debug[f"mask:{cam_id}"]
        if mask.shape == im.shape[:2]:
            cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(im, cnts, -1, (180, 0, 180), 1)
    for a in detection.rejected.get(cam_id, []):
        e = tuple(int(round(v)) for v in a["end"])
        cv2.line(im, tuple(int(round(v)) for v in a["top"]), e, (0, 0, 255), 1, cv2.LINE_AA)
        cv2.drawMarker(im, e, (0, 0, 255), cv2.MARKER_TILTED_CROSS, 8, 1)
    for t in detection.tips:
        ct = t.per_cam.get(cam_id)
        if ct is None:
            continue
        x, y, w, h = ct.ui_box
        ok = ct.seen_axis and not ct.occluded
        col = (74, 217, 255) if ok else (0, 165, 255)
        if ok:
            cv2.rectangle(im, (x, y), (x + w, y + h), col, 1, cv2.LINE_AA)
        else:
            for xa, ya, xb, yb in ((x, y, x + w, y), (x, y + h, x + w, y + h), (x, y, x, y + h), (x + w, y, x + w, y + h)):
                n = max(1, int(math.hypot(xb - xa, yb - ya) // 6))
                for k in range(0, n, 2):
                    p0 = (int(xa + (xb - xa) * k / n), int(ya + (yb - ya) * k / n))
                    p1 = (int(xa + (xb - xa) * (k + 1) / n), int(ya + (yb - ya) * (k + 1) / n))
                    cv2.line(im, p0, p1, col, 1)
        p = (int(round(ct.px[0])), int(round(ct.px[1])))
        cv2.drawMarker(im, p, col, cv2.MARKER_CROSS, 14, 2)
        cv2.putText(im, f"{t.id} {t.confidence:.2f}", (x + w + 3, y + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)
    return im
