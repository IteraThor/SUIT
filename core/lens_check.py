"""Does this camera's lens bend the picture? Measured on the empty board, no chessboard needed.

A pinhole camera images every circle on the board as an exact ellipse, and two concentric circles
(here the outer edges of the treble ring, r = 107 mm, and the double ring, r = 170 mm) give a
matrix C_double^-1 C_treble with a repeated eigenvalue. Lens distortion splits that pair. The
split is dimensionless: ~0.002-0.007 on pinhole-like OV2710 rigs, 0.02 on a clearly distorted one;
adding known barrel distortion to a pinhole picture gave 0.007 for k1 = 0.10 and 0.013 for 0.20.

The check is indicative: it needs both colour rings in view and returns None when it cannot find
them (a red or green surround touching the rings, dark pictures). Pure numpy / OpenCV, no GTK.
"""

from __future__ import annotations
import dataclasses
import logging
from typing import List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

TREBLE_MM, DOUBLE_MM = 107.0, 170.0
RATIO = TREBLE_MM / DOUBLE_MM            # radius ratio of the two rings on the board
RATIO_TOL = 0.06                         # a fit whose ratio is further off found the wrong rings
SPLIT_MILD = 0.008                       # below: no visible distortion
SPLIT_STRONG = 0.015                     # above: clear distortion
REF_WIDTH = 1280.0                       # pixel constants below are for this image width
THICK_PX = 26                            # colour blobs with a core deeper than this are surrounds, not rings
EDGE_SPAN_PX = 12                        # search window around a coarse ring edge
EDGE_POINTS = 720


@dataclasses.dataclass
class LensCheck:
    split: float                         # 0 = pinhole; grows with distortion
    verdict: str                         # "none" | "mild" | "strong"
    double_width_px: float               # fitted double ring width, to judge the resolution of the measurement
    edge_noise_px: float                 # scatter of the ring edge around the fit

    def to_dict(self) -> dict:
        return {"split": round(self.split, 4), "verdict": self.verdict,
                "double_width_px": round(self.double_width_px), "edge_noise_px": round(self.edge_noise_px, 2)}


def lens_check(frame: np.ndarray) -> Optional[LensCheck]:
    """Distortion of the camera that took `frame` (BGR, empty board); None when the rings are not found."""
    try:
        return _lens_check(frame)
    except Exception as e:                                    # a diagnostic must never break the wizard
        logger.debug("Lens check failed: %s", e)
        return None


def _lens_check(frame: np.ndarray) -> Optional[LensCheck]:
    scale = frame.shape[1] / REF_WIDTH
    thin, score = _ring_score(frame, scale)
    coarse = _coarse_rings(thin, frame.shape, scale)
    if len(coarse) < 2:
        return None
    fits, noise = [], []
    for e in coarse:
        pts = _edge_points(score, e, scale)
        if len(pts) < 200:
            return None
        ellipse, resid, keep = _robust_fit(pts)
        fits.append(ellipse)
        noise.append(float(np.std(resid[keep])))
    split, ratio = _eigen_split(_conic(fits[0]), _conic(fits[1]))
    if abs(ratio - RATIO) > RATIO_TOL:
        return None
    verdict = "none" if split <= SPLIT_MILD else "mild" if split <= SPLIT_STRONG else "strong"
    return LensCheck(float(split), verdict, float(max(fits[0][1])), float(np.mean(noise)))


def _ring_score(img: np.ndarray, scale: float) -> Tuple[np.ndarray, np.ndarray]:
    """The red / green rings as a smooth 0..1 map; thick coloured areas (surrounds) are dropped."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    red = cv2.inRange(hsv, (0, 70, 50), (12, 255, 255)) | cv2.inRange(hsv, (165, 70, 50), (180, 255, 255))
    green = cv2.inRange(hsv, (40, 60, 40), (90, 255, 255))
    m = cv2.morphologyEx(red | green, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    # Colour blobs thicker than a ring are surrounds (cloth, wall); a surround that touches the
    # rings through a thin fringe takes them with it, and the check then finds no rings
    n, lab = cv2.connectedComponents(m)
    dt = cv2.distanceTransform(m, cv2.DIST_L2, 5)
    thick = np.zeros(n, np.float32)
    np.maximum.at(thick, lab.ravel(), dt.ravel())
    big = (np.isin(lab, np.where(thick > THICK_PX * scale)[0]) & (lab > 0)).astype(np.uint8) * 255
    thin = m & ~big
    return thin, cv2.GaussianBlur((thin > 0).astype(np.float32), (0, 0), 1.2 * scale)


def _coarse_rings(thin: np.ndarray, shape, scale: float) -> List[tuple]:
    """The two biggest ring-shaped colour contours as ellipses (double ring first)."""
    k = max(3, int(round(11 * scale)) | 1)
    m = cv2.morphologyEx(thin, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    cnts, hier = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    rings = []
    for i, c in enumerate(cnts):
        if hier[0][i][3] != -1 or hier[0][i][2] == -1 or len(c) < 200:     # an outer contour with a hole
            continue
        e = cv2.fitEllipse(c)
        area = cv2.contourArea(c)
        if area < 0.01 * shape[0] * shape[1] or abs(area / (np.pi * e[1][0] * e[1][1] / 4) - 1) > 0.06:
            continue
        rings.append(e)
    return sorted(rings, key=lambda e: -max(e[1]))[:2]


def _edge_points(score: np.ndarray, e: tuple, scale: float) -> np.ndarray:
    """Outer edge of the ring along a coarse ellipse: where the colour map falls through 0.5."""
    (cx, cy), (a, b), ang = e
    t = np.radians(ang)
    R = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
    s = np.linspace(0, 2 * np.pi, EDGE_POINTS, endpoint=False)
    P = (R @ np.vstack([a / 2 * np.cos(s), b / 2 * np.sin(s)])).T + [cx, cy]
    D = P - [cx, cy]
    D /= np.linalg.norm(D, axis=1, keepdims=True)
    offs = np.arange(-EDGE_SPAN_PX * scale, EDGE_SPAN_PX * scale + 0.01, 0.25)
    Q = P[:, None, :] + offs[None, :, None] * D[:, None, :]
    prof = cv2.remap(score, Q[..., 0].astype(np.float32), Q[..., 1].astype(np.float32), cv2.INTER_LINEAR)
    pts = []
    for k in range(len(P)):
        p = prof[k]
        cross = np.where((p[:-1] >= 0.5) & (p[1:] < 0.5))[0]
        if len(cross) == 0 or (p[0] < 0.5 and p.max() < 0.8):
            continue
        j = cross[-1]                                                      # outermost crossing
        pts.append(P[k] + (offs[j] + (p[j] - 0.5) / (p[j] - p[j + 1]) * (offs[1] - offs[0])) * D[k])
    return np.array(pts)


def _robust_fit(pts: np.ndarray):
    """Ellipse through edge points with outliers (wires, numbers) rejected; (ellipse, residuals, inliers)."""
    keep = np.ones(len(pts), bool)
    for _ in range(5):
        ellipse = cv2.fitEllipseDirect(pts[keep].astype(np.float32))
        r = _signed_distance(pts, ellipse)
        sigma = 1.4826 * np.median(np.abs(r[keep] - np.median(r[keep])))
        keep = np.abs(r) < max(1.0, 3.5 * sigma)
    return ellipse, r, keep


def _signed_distance(pts: np.ndarray, ellipse: tuple) -> np.ndarray:
    """Distance of points to an ellipse; positive outside."""
    (cx, cy), (a, b), ang = ellipse
    t = np.radians(ang)
    X = pts - [cx, cy]
    u = X @ np.array([np.cos(t), np.sin(t)])
    v = X @ np.array([-np.sin(t), np.cos(t)])
    s = np.linspace(0, 2 * np.pi, 3600, endpoint=False)
    E = np.c_[a / 2 * np.cos(s), b / 2 * np.sin(s)]
    d2 = (u[:, None] - E[None, :, 0]) ** 2 + (v[:, None] - E[None, :, 1]) ** 2
    sign = np.sign((u / (a / 2)) ** 2 + (v / (b / 2)) ** 2 - 1)
    return np.sqrt(d2.min(axis=1)) * sign


def _conic(e: tuple) -> np.ndarray:
    (cx, cy), (a, b), ang = e
    t = np.radians(ang)
    c, s = np.cos(t), np.sin(t)
    A = np.array([[c, s], [-s, c]])
    Q = A.T @ np.diag([1 / (a / 2) ** 2, 1 / (b / 2) ** 2]) @ A
    x0 = np.array([cx, cy])
    C = np.zeros((3, 3))
    C[:2, :2] = Q
    C[:2, 2] = C[2, :2] = -Q @ x0
    C[2, 2] = x0 @ Q @ x0 - 1
    return C


def _eigen_split(c_outer: np.ndarray, c_inner: np.ndarray) -> Tuple[float, float]:
    """(split of the repeated eigenvalue, radius ratio from the distinct one) of C_outer^-1 C_inner."""
    w = np.linalg.eigvals(np.linalg.inv(c_outer) @ c_inner).real
    k = int(np.argmax([min(abs(w[i] - w[j]) for j in range(3) if j != i) for i in range(3)]))
    pair = [w[j] for j in range(3) if j != k]
    return abs(pair[0] - pair[1]) / abs(np.mean(pair)), float(np.sqrt(abs(w[k] / np.mean(pair))))
