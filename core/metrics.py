"""Image quality metrics for dartboard camera calibration.

Two scorers:
- QualityEvaluator.evaluate: board-wide wire edge steepness, temporal sensor noise,
  contrast-to-noise ratio (CNR) and dynamic range (fallback without dart tips).
- Tip-focused scoring (TipROI / TipScore / evaluate_tips): contrast, sharpness and continuity of
  the steel points of darts already in the board, plus board-wide guard terms and hard vetoes.
"""

from __future__ import annotations
import dataclasses
import math
from typing import List, Optional, Sequence, Tuple
import cv2
import numpy as np
from core.board_detector import BoardROI, board_mask_from_homography  # noqa: F401 (re-export)


@dataclasses.dataclass
class QualityMetrics:
    edge_steepness: float  # Mean gradient magnitude across wire edges
    temporal_noise: float  # Temporal stddev across static frame burst (lower is better)
    contrast_to_noise_ratio: float  # CNR (higher is better)
    mean_luminance: float  # Mean luminance in board ROI (0-255)
    highlight_clipping_ratio: float  # Fraction of clipped pixels (>253)
    shadow_clipping_ratio: float  # Fraction of crushed black pixels (<=8)
    composite_score: float  # Overall normalized score (0 - 100)
    wire_fwhm: float = 0.0  # Full-Width at Half-Maximum of wire gradient profile in px (lower is thinner/sharper)
    halo_overshoot: float = 0.0  # Digital unsharp ringing overshoot fraction (lower is cleaner)
    # Tip-focused scoring (filled when the optimizer scores against dart tips, see TipScore)
    tip_component: Optional[float] = None  # 0..1, None when scored on the whole board only
    board_component: Optional[float] = None  # 0..1 board guard score
    veto: Optional[str] = None  # hard-veto reason against the initial burst (diagnostic text)
    tips: Optional[List[Optional["TipMetrics"]]] = None  # per dart (TipROI order), None = hidden


class QualityEvaluator:
    """Computes objective optical metrics tailored to Autodarts segmentation."""

    @staticmethod
    def calculate_focus_score(
        patch: np.ndarray,
        target_roi: Optional[Tuple[int, int, int, int]] = None,
    ) -> float:
        """Compute normalized sharpness score (0–100) tuned for 2170 CMOS sensors on dartboard wires."""
        if patch is None or patch.size == 0:
            return 0.0

        h, w = patch.shape[:2]
        if target_roi:
            tx, ty, tw, th = target_roi
            crop = patch[max(0, ty):min(h, ty + th), max(0, tx):min(w, tx + tw)]
            if crop.size > 0:
                patch = crop

        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY) if len(patch.shape) == 3 else patch
        lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        ten = float(np.mean(gx**2 + gy**2))

        log_lap = math.log10(lap + 1e-6)
        log_ten = math.log10(ten + 1e-6)
        norm_lap = np.clip((log_lap - 1.2) / (3.35 - 1.2), 0.0, 1.0)
        norm_ten = np.clip((log_ten - 2.2) / (4.70 - 2.2), 0.0, 1.0)

        blended = 0.5 * norm_lap + 0.5 * norm_ten
        return float(np.clip(blended * 100.0, 0.0, 100.0))

    @staticmethod
    def calculate_edge_steepness(frame: np.ndarray, wire_mask: np.ndarray, roi: Optional[BoardROI] = None) -> float:
        """Calculate weighted gradient magnitude along dartboard segment wires, prioritizing Treble & Bullseye."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        grad_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        grad_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        magnitude = np.sqrt(grad_x ** 2 + grad_y ** 2)

        if roi and roi.center and roi.radius > 20:
            h, w = gray.shape[:2]
            cx, cy = roi.center
            r = roi.radius
            y_indices, x_indices = np.indices((h, w))
            dist_from_center = np.sqrt((x_indices - cx) ** 2 + (y_indices - cy) ** 2)

            # Sweet spot mask: Bullseye (0-18% radius) and Treble region (45-72% radius)
            sweet_mask = (wire_mask > 0) & (((dist_from_center <= r * 0.18)) | ((dist_from_center >= r * 0.45) & (dist_from_center <= r * 0.72)))
            outer_mask = (wire_mask > 0) & (~sweet_mask)

            sweet_vals = magnitude[sweet_mask]
            outer_vals = magnitude[outer_mask]

            if len(sweet_vals) > 0 and len(outer_vals) > 0:
                return float(0.65 * np.mean(sweet_vals) + 0.35 * np.mean(outer_vals))
            elif len(sweet_vals) > 0:
                return float(np.mean(sweet_vals))

        wire_pixels = magnitude[wire_mask > 0]
        if len(wire_pixels) == 0:
            return float(np.mean(magnitude))
        return float(np.mean(wire_pixels))

    @staticmethod
    def calculate_temporal_noise(frames: List[np.ndarray], uniform_mask: np.ndarray) -> float:
        """Calculate true temporal sensor noise across N consecutive frames.

        Isolates random thermal/shot noise from static MJPEG 8x8 DCT compression blocks.
        """
        if len(frames) < 2:
            return 1.0  # Baseline fallback

        # Convert all frames to grayscale float32
        gray_stack = []
        for f in frames:
            g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if len(f.shape) == 3 else f
            gray_stack.append(g.astype(np.float32))

        stack = np.stack(gray_stack, axis=0)  # Shape: (N, H, W)
        temporal_std = np.std(stack, axis=0)  # Stddev per pixel over time

        uniform_pixels = temporal_std[uniform_mask > 0]
        if len(uniform_pixels) == 0:
            return float(np.mean(temporal_std))
        return float(np.mean(uniform_pixels))

    @staticmethod
    def calculate_cnr(frame: np.ndarray, roi: BoardROI, temporal_noise: float) -> float:
        """Calculate Contrast-to-Noise Ratio (CNR) between light and dark segments."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        board_pixels = gray[roi.uniform_mask > 0]
        if len(board_pixels) < 100:
            return 10.0

        # High percentile (light segments) vs Low percentile (dark segments)
        p_high = np.percentile(board_pixels, 85)
        p_low = np.percentile(board_pixels, 15)
        contrast = max(0.0, float(p_high - p_low))

        noise = max(0.1, temporal_noise)
        return float(contrast / noise)

    @staticmethod
    def calculate_halo_overshoot(frame: np.ndarray, wire_mask: np.ndarray, uniform_mask: np.ndarray) -> float:
        """Calculate digital unsharp-mask ringing overshoot along wire borders."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        dilated_wire = cv2.dilate(wire_mask, kernel, iterations=2)
        halo_zone = cv2.bitwise_and(dilated_wire, cv2.bitwise_not(wire_mask))
        halo_zone = cv2.bitwise_and(halo_zone, uniform_mask)  # Only inspect in uniform sisal
        
        halo_pixels = gray[halo_zone > 0]
        sisal_pixels = gray[uniform_mask > 0]
        if len(halo_pixels) < 50 or len(sisal_pixels) < 50:
            return 0.0
            
        p95_halo = float(np.percentile(halo_pixels, 95))
        p90_sisal = float(np.percentile(sisal_pixels, 90))
        if p90_sisal <= 0:
            return 0.0
            
        overshoot = max(0.0, (p95_halo - p90_sisal) / p90_sisal)
        return float(overshoot)

    @staticmethod
    def calculate_wire_fwhm(frame: np.ndarray, roi: BoardROI) -> float:
        """Calculate mean Full-Width at Half-Maximum (FWHM) of wire edge profiles in pixels.

        Extracts 1D normal profiles across the Bullseye and Treble scoring rings to measure
        true physical edge thinness. Lower FWHM indicates a thinner, sharper wire edge.
        """
        if frame is None or frame.size == 0 or roi is None:
            return 3.5  # Baseline default

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        h, w = gray.shape[:2]

        cx, cy = roi.center if roi.center else (w // 2, h // 2)
        radius = roi.radius if (roi.radius and roi.radius > 30) else int(min(w, h) * 0.35)

        # Concentric radii representing critical wire boundaries:
        # Bullseye circle (~9% of radius), Treble inner/outer (~55-62%), Double (~96%)
        test_radii = [
            int(radius * 0.09),
            int(radius * 0.55),
            int(radius * 0.62),
            int(radius * 0.96),
        ]

        fwhm_measurements: List[float] = []
        half_win = 8  # profile length = 17 pixels

        for r in test_radii:
            if r <= 5:
                continue
            # Sample 24 angles around the ring
            for deg in range(0, 360, 15):
                theta = math.radians(deg)
                cos_t = math.cos(theta)
                sin_t = math.sin(theta)

                # Center of profile along the wire ring
                px = cx + r * cos_t
                py = cy + r * sin_t

                if px - half_win < 0 or px + half_win >= w or py - half_win < 0 or py + half_win >= h:
                    continue

                profile = []
                for s in range(-half_win, half_win + 1):
                    sx = int(round(px + s * cos_t))
                    sy = int(round(py + s * sin_t))
                    if 0 <= sx < w and 0 <= sy < h:
                        profile.append(float(gray[sy, sx]))
                    else:
                        break

                if len(profile) != (2 * half_win + 1):
                    continue

                arr = np.array(profile, dtype=np.float32)
                grad = np.abs(np.diff(arr))
                max_g = float(np.max(grad))

                if max_g >= 12.0:
                    half_max = max_g * 0.5
                    peak_idx = int(np.argmax(grad))

                    left_idx = peak_idx
                    while left_idx > 0 and grad[left_idx] >= half_max:
                        left_idx -= 1

                    right_idx = peak_idx
                    while right_idx < len(grad) - 1 and grad[right_idx] >= half_max:
                        right_idx += 1

                    left_pos = float(left_idx)
                    if left_idx < peak_idx and (grad[left_idx + 1] - grad[left_idx]) > 1e-4:
                        left_pos = left_idx + (half_max - grad[left_idx]) / (grad[left_idx + 1] - grad[left_idx])

                    right_pos = float(right_idx)
                    if right_idx > peak_idx and (grad[right_idx - 1] - grad[right_idx]) > 1e-4:
                        right_pos = right_idx - (half_max - grad[right_idx]) / (grad[right_idx - 1] - grad[right_idx])

                    fwhm_val = max(1.0, right_pos - left_pos)
                    if fwhm_val <= 10.0:
                        fwhm_measurements.append(fwhm_val)

        if not fwhm_measurements:
            return 3.5

        return float(np.percentile(fwhm_measurements, 35))

    def evaluate(
        self,
        frames: List[np.ndarray],
        roi: BoardROI,
        target_white_lum: Optional[float] = None,
        target_mean_lum: Optional[float] = None,
    ) -> QualityMetrics:
        """Board-wide evaluation of a single or multi-frame capture (fallback without dart tips)."""
        if not frames:
            return QualityMetrics(0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 3.5, 0.0)

        if len(frames) > 1:
            # Multi-frame temporal stacking: compute noise-free pixel average
            main_frame = np.mean(np.stack(frames, axis=0), axis=0).astype(np.uint8)
        else:
            main_frame = frames[0]

        gray = cv2.cvtColor(main_frame, cv2.COLOR_BGR2GRAY) if len(main_frame.shape) == 3 else main_frame

        # 1. Edge Steepness & Wire Thinness (FWHM) with Treble/Bull Sweet Spot Weighting
        steepness = self.calculate_edge_steepness(main_frame, roi.wire_mask, roi=roi)
        wire_fwhm = self.calculate_wire_fwhm(main_frame, roi)

        # 2. Temporal Noise
        t_noise = self.calculate_temporal_noise(frames, roi.uniform_mask)

        # 3. CNR & Halo Ringing
        cnr = self.calculate_cnr(main_frame, roi, t_noise)
        halo_overshoot = self.calculate_halo_overshoot(main_frame, roi.wire_mask, roi.uniform_mask)

        # 4. Luminance, Highlights, Dynamic Range, and Shadow Clipping
        board_pixels = gray[roi.board_mask > 0]
        if len(board_pixels) == 0:
            board_pixels = gray.flatten()

        uniform_pixels_lum = gray[roi.uniform_mask > 0]
        if len(uniform_pixels_lum) > 50:
            white_bed_lum = float(np.percentile(uniform_pixels_lum, 90))
        else:
            white_bed_lum = float(np.percentile(board_pixels, 90))

        mean_lum = float(np.mean(board_pixels))
        hl_clipped_pixels = np.sum(board_pixels >= 253)
        hl_clipping_ratio = float(hl_clipped_pixels / len(board_pixels))

        sh_clipped_pixels = np.sum(board_pixels <= 8)
        sh_clipping_ratio = float(sh_clipped_pixels / len(board_pixels))

        p_black_floor = float(np.percentile(board_pixels, 10))
        p_white_peak = float(np.percentile(board_pixels, 90))
        dynamic_range = max(0.0, p_white_peak - p_black_floor)

        # 5. Penalties and Composite Score Calculation (0 - 100)
        if target_white_lum is not None:
            lum_penalty = max(0.0, 1.0 - abs(white_bed_lum - target_white_lum) / 35.0)
        else:
            lum_penalty = max(0.0, 1.0 - abs(white_bed_lum - 205.0) / 60.0)

        # Highlight clipping penalty (specular wire burnout)
        hl_penalty = max(0.0, 1.0 - (hl_clipping_ratio * 18.0))

        # Shadow clipping penalty (crushed dart shadow triangulation)
        sh_penalty = max(0.0, 1.0 - (sh_clipping_ratio * 14.0))

        # Halo ringing penalty (strictly penalizes unsharp-mask edge doubling)
        halo_penalty = max(0.25, 1.0 - (halo_overshoot / 0.18) ** 2)

        # Black Floor Factor: healthy blacks down to 4.0 DN receive full score
        if 4.0 <= p_black_floor <= 45.0:
            black_floor_factor = 1.0
        elif p_black_floor < 4.0:
            black_floor_factor = max(0.40, (p_black_floor / 4.0) ** 2)
        else:
            black_floor_factor = max(0.40, 1.0 - (p_black_floor - 45.0) / 40.0)

        # White bed peak target: optimal 185-245 DN (accommodates high-gamma sisal boost)
        if 185.0 <= p_white_peak <= 245.0:
            white_peak_factor = 1.0
        elif p_white_peak > 245.0:
            white_peak_factor = max(0.20, 1.0 - (p_white_peak - 245.0) / 8.0)
        else:
            white_peak_factor = max(0.30, (p_white_peak / 185.0) ** 2)

        penalties = [
            max(0.30, hl_penalty),
            max(0.30, sh_penalty),
            halo_penalty,
            black_floor_factor,
            white_peak_factor,
        ]
        clip_penalty = float(np.prod(penalties) ** (1.0 / len(penalties)))

        # Normalized Components (scaled to real-world CMOS dartboard optics):
        steep_norm = float(np.clip((steepness - 180.0) / (420.0 - 180.0), 0.0, 1.0))
        fwhm_norm = float(np.clip((4.2 - wire_fwhm) / (4.2 - 1.6), 0.0, 1.0))
        dr_norm = float(np.clip((dynamic_range - 130.0) / (235.0 - 130.0), 0.0, 1.0))
        noise_norm = max(0.0, 1.0 - min(1.0, (t_noise - 0.5) / 3.0))

        # Composite score: 30% steepness, 25% wire thinness (FWHM), 25% dynamic range, 20% noise
        raw_score = (steep_norm * 30.0 + fwhm_norm * 25.0 + dr_norm * 25.0 + noise_norm * 20.0) * clip_penalty

        composite_score = round(max(0.0, min(100.0, raw_score)), 1)

        return QualityMetrics(
            edge_steepness=round(steepness, 2),
            temporal_noise=round(t_noise, 3),
            contrast_to_noise_ratio=round(cnr, 2),
            mean_luminance=round(mean_lum, 1),
            highlight_clipping_ratio=round(hl_clipping_ratio, 4),
            shadow_clipping_ratio=round(sh_clipping_ratio, 4),
            composite_score=composite_score,
            wire_fwhm=round(wire_fwhm, 2),
            halo_overshoot=round(halo_overshoot, 3),
        )

    def evaluate_tips(
        self,
        frames: List[np.ndarray],
        roi: Optional[BoardROI],
        tips: Sequence["TipROI"],
        reference: Optional["TipScore"] = None,
        board_mask: Optional[np.ndarray] = None,
        exclude_mask: Optional[np.ndarray] = None,
    ) -> "TipScore":
        """Tip-focused score of a burst against frozen TipROIs (see evaluate_tips below).

        `board_mask` defaults to the BoardROI's board mask; the optimizer passes the r=330 mask
        from the homography. With a reference (the initial burst at the SAME burst length) the
        hard vetoes apply, including the existing wire-halo veto.
        """
        if board_mask is None:
            if roi is None or roi.board_mask is None:
                raise ValueError("evaluate_tips needs a board mask")
            board_mask = roi.board_mask
        if exclude_mask is None:
            exclude_mask = make_dart_exclusion(board_mask.shape[:2], tips)
        score = evaluate_tips(frames, tips, board_mask, exclude_mask, reference)
        if reference is not None and score.veto is None and roi is not None and frames:
            mean = np.mean(np.stack(frames[:3], axis=0), axis=0).astype(np.uint8)
            halo = self.calculate_halo_overshoot(mean, roi.wire_mask, roi.uniform_mask)
            if halo > HALO_VETO:
                score.veto = f"wire halo ({halo * 100:.0f}%)"
        return score


# ----------------------------------------------------------------------------------------------
# Tip-focused scoring (no empty-board reference)
#
# Inputs per camera: a burst of N >= 3 BGR frames at one setting, the frozen TipROIs from the
# dart detector (tip, unit direction flight -> tip, stats box), and a board mask plus a
# dart-exclusion mask. Everything is relative to the initial burst of the same camera and the
# same burst length: sigma and p99.9 frame difference grow with N.
# ----------------------------------------------------------------------------------------------

@dataclasses.dataclass
class TipROI:
    tip: Tuple[float, float]  # image px, where the steel point enters the board
    direction: Tuple[float, float]  # unit vector flight -> tip (detector per-dart axis)
    box: Tuple[int, int, int, int]  # x, y, w, h (120x120), used for clip / noise stats
    visible: bool = True  # decided ONCE on the step-2 burst, then frozen
    point_len: float = 30.0  # px of bare steel point visible behind the tip (pose prior)
    dart_id: int = 0  # DartTip.id; list order is the same in every camera, never index by position alone
    marginal: bool = False  # visible but CNR in the margin band: counts in the mean, not in min()
    ref_contrast: float = 0.0  # tip contrast (DN) when frozen, for the darts-moved check
    hidden_reason: Optional[str] = None  # diagnostic: why the tip is not visible in this camera
    polarity: int = 0  # +1 bright point / -1 dark point vs BOTH flanks, frozen in step 2 (0 = decide per burst)
    faint: bool = False  # hidden only because the point is too faint (not occluded): worth probing while tuning
    est_contrast: float = 0.0  # contrast (DN) at the ESTIMATED position, no search; for the probe and Details
    est_cnr: float = 0.0


@dataclasses.dataclass
class TipMetrics:
    contrast: float  # DN, |point - local background| (median over profiles)
    cnr: float  # contrast / temporal noise sigma in ROI background
    edge_width: float  # px, contrast / max edge slope (lower = sharper)
    halo: float  # fraction of contrast, opposite-polarity overshoot beside the shaft
    line_cov: float  # 0..1, shaft continuity: profiles whose centre lies on one straight line
    spec_clip: float  # fraction of DART pixels (point/barrel zone) in the ROI >= 250
    shadow_clip: float  # fraction of ROI pixels <= 6
    bg_clip: float  # fraction of ROI background (sisal) pixels >= 250
    noise: float  # temporal sigma (DN) in ROI background
    valid_profiles: float  # fraction of profiles with contrast > 3 sigma
    hough_cov: float = 0.0  # diagnostic only (unstable frame to frame, not scored)
    score: float = 0.0  # 0..1


@dataclasses.dataclass
class BoardGuard:
    noise: float  # mean temporal sigma (DN) over board sisal (darts excluded)
    motion_p999: float  # 99.9th pct of |frame_i - frame_i+1| over board (false-motion proxy)
    hl_clip: float  # fraction of board pixels >= 253
    sh_clip: float  # fraction of board pixels <= 8
    mean_lum: float
    score: float = 0.0  # 0..1
    n_frames: int = 0  # burst length the noise / motion terms were measured at


@dataclasses.dataclass
class TipScore:
    total: float  # 0..100
    tip_component: float  # 0..1
    board_component: float  # 0..1
    tips: List[Optional[TipMetrics]]  # per TipROI, None for tips not visible in this camera
    board: BoardGuard
    veto: Optional[str] = None
    n_frames: int = 0
    board_only: bool = False  # no visible tip: total = 100 * board


PROFILE_HALF = 12  # px each side of the shaft centre line
PROFILE_START = 3  # px behind the tip (skip the board-entry pixel)
PROFILE_LEN = 30  # px of steel point sampled behind the tip
PROFILE_STEP = 2
CENTER_SEARCH = 4  # px, the shaft centre may be off by this much
FLANK_IN, FLANK_OUT = 7, 11

W_TIP, W_BOARD = 0.70, 0.30
TIP_WEIGHTS = dict(cnr=0.15, contrast=0.25, sharp=0.40, line=0.20)
BOARD_WEIGHTS = dict(noise=0.30, motion=0.20, hl=0.30, sh=0.20)

# Visibility (decided once in step 2): C >= 10 DN, >= 60 % valid profiles, CNR >= 6; CNR 6..8 is
# the margin band. Refinement may move the detector tip sideways by at most 5 px.
VIS_MIN_CONTRAST = 10.0
VIS_MIN_VALID = 0.6
VIS_MIN_CNR = 6.0
VIS_MARGINAL_CNR = 8.0
VIS_MAX_REFINE_SHIFT = 5.0
HALO_VETO = 0.22  # calculate_halo_overshoot on the board wires
TIP_BOX = (120, 120)  # stats box, tip at the bottom-centre third


def _lin(x: float, lo: float, hi: float) -> float:
    """Map lo->0, hi->1 (works for hi < lo too), clipped."""
    if hi == lo:
        return 0.0
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))


def _gray_stack(frames: Sequence[np.ndarray]) -> np.ndarray:
    return np.stack(
        [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if f.ndim == 3 else f for f in frames], axis=0
    ).astype(np.float32)


def _as_stack(frames_or_stack) -> np.ndarray:
    if isinstance(frames_or_stack, np.ndarray) and frames_or_stack.ndim == 3 and frames_or_stack.dtype == np.float32:
        return frames_or_stack
    return _gray_stack(frames_or_stack)


def sample_profiles(gray: np.ndarray, tip: Tuple[float, float], direction: Tuple[float, float], length: float = PROFILE_LEN) -> np.ndarray:
    """Perpendicular intensity profiles along the steel point, shape (n_profiles, 2*HALF+1)."""
    dx, dy = direction
    n = math.hypot(dx, dy) or 1.0
    dx, dy = dx / n, dy / n
    px, py = -dy, dx  # perpendicular
    t = np.arange(PROFILE_START, max(PROFILE_START + 2 * PROFILE_STEP, length), PROFILE_STEP, dtype=np.float32)
    s = np.arange(-PROFILE_HALF, PROFILE_HALF + 1, dtype=np.float32)
    T, Sg = np.meshgrid(t, s, indexing="ij")
    map_x = (tip[0] - T * dx + Sg * px).astype(np.float32)  # walk BACK from the tip, toward the flight
    map_y = (tip[1] - T * dy + Sg * py).astype(np.float32)
    return cv2.remap(gray, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def _profile_stats(p: np.ndarray) -> Tuple[float, float, float, int]:
    """(contrast, max_slope, halo_abs, centre_index) for one perpendicular profile."""
    c0 = PROFILE_HALF
    left = p[c0 - FLANK_OUT:c0 - FLANK_IN + 1]
    right = p[c0 + FLANK_IN:c0 + FLANK_OUT + 1]
    bl, br = float(np.median(left)), float(np.median(right))
    base = 0.5 * (bl + br)
    win = p[c0 - CENTER_SEARCH:c0 + CENTER_SEARCH + 1]
    k = int(np.argmax(np.abs(win - base)))
    ci = c0 - CENTER_SEARCH + k
    c = float(p[ci])
    contrast = 0.5 * (abs(c - bl) + abs(c - br))
    sign = 1.0 if c > base else -1.0  # +1: bright point on dark bed, -1: dark on bright
    g = np.abs(np.diff(p))
    lo, hi = max(0, ci - FLANK_IN), min(len(g), ci + FLANK_IN)
    max_slope = float(np.max(g[lo:hi])) if hi > lo else 0.0
    # Halo: mean opposite-polarity excursion 2..4 px beside the shaft centre (unsharp-mask
    # ringing lives there; the sisal texture averages out over 3 px and both sides).
    l_zone = p[max(0, ci - 4):max(0, ci - 1)]
    r_zone = p[ci + 2:ci + 5]
    ex = []
    if l_zone.size:
        ex.append(float(np.mean(-sign * (l_zone - bl))))
    if r_zone.size:
        ex.append(float(np.mean(-sign * (r_zone - br))))
    halo = max(0.0, float(np.mean(ex))) if ex else 0.0
    return contrast, max_slope, halo, ci


def _aligned_halo(prof: np.ndarray, cis: np.ndarray, contrast: float, polarity: int = 0) -> float:
    """Ringing on the median profile after aligning every profile on its own shaft centre.

    Per-profile maxima are dominated by sisal texture; the aligned median keeps the systematic
    unsharp-mask overshoot (2..5 px beside the shaft) and averages the texture away.
    """
    R = PROFILE_HALF - CENTER_SEARCH
    al = np.stack([p[ci - R:ci + R + 1] for p, ci in zip(prof, cis)])
    M = np.median(al, axis=0)
    c = R
    bl = float(np.median(M[:3]))  # 6..8 px left of the centre
    br = float(np.median(M[-3:]))  # 6..8 px right of the centre
    centre = float(M[c])
    # Ringing is an overshoot beyond each flank, away from the centre. Decide that direction PER
    # SIDE: a point on a segment boundary (red treble one side, black sisal the other) can be darker
    # than one flank and brighter than the other, and a single sign then flipped from burst to burst
    # and measured the point's own blurred shoulder as ringing. A side whose flank is too close to
    # the centre has no defined direction and is skipped. A frozen polarity overrides both sides.
    floor = 0.25 * max(contrast, 8.0)
    sides = []
    for seg, b in ((M[c - 5:c - 1], bl), (M[c + 2:c + 6], br)):
        if polarity:
            sign = float(polarity)
        elif abs(centre - b) >= floor:
            sign = 1.0 if centre > b else -1.0
        else:
            continue
        sides.append(float((-sign * (seg - b)).max()))
    # Normalise by the robust per-profile contrast (the aligned median can be near-flat on a
    # weak tip, which made the ratio explode), and cap at 1.
    return float(min(1.0, max([0.0] + sides) / max(contrast, 8.0)))


def tip_cross_section(frame_or_frames, roi: TipROI) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """(offsets in px, DN) of the steel point's cross-section, for plotting before against after.

    Perpendicular profiles along the frozen tip axis are aligned on each profile's own shaft centre
    and combined with the median (like the ringing measurement). The result is shifted so the
    flanks sit at 0 and flipped so the point always rises upward: the peak height is the tip
    contrast, the width is the edge width, and an undershoot beside it is ringing. Returns None
    when no profile has a usable point.
    """
    f = frame_or_frames
    single = isinstance(f, np.ndarray) and (f.ndim == 2 or (f.ndim == 3 and f.shape[2] in (1, 3) and f.dtype != np.float32))
    stack = _as_stack([f] if single else f)
    mean = stack.mean(axis=0)
    prof = sample_profiles(mean, roi.tip, roi.direction, min(PROFILE_LEN, 0.85 * roi.point_len))
    stats = [_profile_stats(p) for p in prof]
    cs = np.array([st[0] for st in stats])
    cis = np.array([st[3] for st in stats], int)
    if cs.size == 0 or float(np.median(cs)) <= 0:
        return None
    use = cs >= 0.5 * float(np.median(cs))
    if use.sum() < 3:
        return None
    R = PROFILE_HALF - CENTER_SEARCH
    aligned = np.stack([p[ci - R:ci + R + 1] for p, ci in zip(prof[use], cis[use])])
    m = np.median(aligned, axis=0)
    base = float(np.median(np.concatenate([m[:3], m[-3:]])))
    m = m - base
    if m[R] < 0:
        m = -m
    return np.arange(-R, R + 1, dtype=np.float32), m.astype(np.float32)


def _line_coverage(gray: np.ndarray, roi: TipROI) -> float:
    """Fraction of the sampled point length explained by Hough segments aligned with the shaft."""
    x, y, w, h = roi.box
    H, W = gray.shape
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    patch = np.clip(gray[y0:y1, x0:x1], 0, 255).astype(np.uint8)
    if patch.size == 0:
        return 0.0
    edges = cv2.Canny(cv2.GaussianBlur(patch, (3, 3), 0), 40, 120)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=12, minLineLength=10, maxLineGap=4)
    if lines is None:
        return 0.0
    dx, dy = roi.direction
    tx, ty = roi.tip[0] - x0, roi.tip[1] - y0
    covered = np.zeros(PROFILE_START + PROFILE_LEN + 1, dtype=bool)
    for (ax, ay, bx, by) in lines[:, 0, :]:
        vx, vy = bx - ax, by - ay
        L = math.hypot(vx, vy)
        if L < 1:
            continue
        if abs(vx * dx + vy * dy) / L < math.cos(math.radians(12)):
            continue
        # Perpendicular distance of both endpoints from the shaft axis through the tip.
        da = abs((ax - tx) * dy - (ay - ty) * dx)
        db = abs((bx - tx) * dy - (by - ty) * dx)
        if max(da, db) > 6:
            continue
        ta = -((ax - tx) * dx + (ay - ty) * dy)  # distance behind the tip
        tb = -((bx - tx) * dx + (by - ty) * dy)
        lo, hi = int(max(0, min(ta, tb))), int(min(len(covered) - 1, max(ta, tb)))
        if hi > lo:
            covered[lo:hi + 1] = True
    end = int(max(PROFILE_START + 4, min(PROFILE_START + PROFILE_LEN, 0.85 * roi.point_len)))
    return float(covered[PROFILE_START:end].mean())


def tip_metrics(stack: np.ndarray, roi: TipROI, mean: Optional[np.ndarray] = None) -> TipMetrics:
    """stack: (N, H, W) float32 grayscale burst; `mean` is its temporal mean (computed if omitted)."""
    if mean is None:
        mean = stack.mean(axis=0)
    H, W = mean.shape
    x, y, w, h = roi.box
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)

    # Temporal sigma in the ROI background (exclude a corridor around the dart axis and
    # everything behind the tip up to the box edge, i.e. the barrel / shaft).
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    dx, dy = roi.direction
    rel_x, rel_y = xx - roi.tip[0], yy - roi.tip[1]
    along = -(rel_x * dx + rel_y * dy)  # >0 behind the tip
    perp = np.abs(rel_x * dy - rel_y * dx)
    dart_zone = (along > -6) & (perp < 14 + np.clip(along, 0, None) * 0.15)
    sigma_map = stack[:, y0:y1, x0:x1].std(axis=0, ddof=1) if stack.shape[0] > 1 else np.ones((y1 - y0, x1 - x0))
    bg = ~dart_zone
    noise = float(np.mean(sigma_map[bg])) if bg.any() else float(np.mean(sigma_map))
    noise = max(noise, 0.25)

    prof = sample_profiles(mean, roi.tip, roi.direction, min(PROFILE_LEN, 0.85 * roi.point_len))
    stats = [_profile_stats(p) for p in prof]
    cs = np.array([s[0] for s in stats])
    slopes = np.array([s[1] for s in stats])
    cis = np.array([s[3] for s in stats], float)
    valid = cs > 3.0 * noise
    # Continuity: a shaft Autodarts can fit a line to has its centre on one straight line.
    continuity = 0.0
    if valid.sum() >= 3:
        tt = np.arange(len(cis), dtype=float)
        a, b = np.polyfit(tt[valid], cis[valid], 1)
        on_line = valid & (np.abs(cis - (a * tt + b)) <= 1.5)
        continuity = float(on_line.mean())
    contrast = float(np.median(cs))
    if valid.sum() >= 3:
        width = float(np.median(cs[valid] / np.maximum(slopes[valid], 1e-3)))
        halo = _aligned_halo(prof[valid], cis[valid].astype(int), float(np.median(cs[valid])), roi.polarity)
    else:
        width, halo = 6.0, 0.0

    patch = mean[y0:y1, x0:x1]
    spec = float(np.mean(patch[dart_zone] >= 250)) if dart_zone.any() else 0.0
    bg_clip = float(np.mean(patch[bg] >= 250)) if bg.any() else 0.0
    shadow = float(np.mean(patch <= 6)) if patch.size else 0.0
    hough = _line_coverage(mean, roi)

    return TipMetrics(
        contrast=round(contrast, 2), cnr=round(contrast / noise, 2), edge_width=round(width, 3),
        halo=round(halo, 3), line_cov=round(continuity, 3), spec_clip=round(spec, 4),
        shadow_clip=round(shadow, 4), bg_clip=round(bg_clip, 4), noise=round(noise, 3),
        valid_profiles=round(float(valid.mean()), 3), hough_cov=round(hough, 3),
    )


def score_tip(m: TipMetrics, ref: Optional[TipMetrics] = None) -> float:
    # CNR and contrast SATURATE: beyond ~25x noise / ~60 DN the tip already clears Autodarts'
    # absolute diff threshold, and an unsaturated noise term would reward blur (blur halves noise).
    cnr_n = _lin(math.log10(max(m.cnr, 1e-3)), math.log10(5.0), math.log10(25.0))
    con_n = _lin(m.contrast, 12.0, 60.0)
    sharp_n = _lin(m.edge_width, 2.6, 1.2)  # px; lower is better
    line_n = m.line_cov
    raw = (TIP_WEIGHTS["cnr"] * cnr_n + TIP_WEIGHTS["contrast"] * con_n
           + TIP_WEIGHTS["sharp"] * sharp_n + TIP_WEIGHTS["line"] * line_n)
    # Halo is scored as the INCREASE over the initial burst on the same frozen ROI: board wires and
    # ring edges 2..5 px beside a tip look like ringing, and that confound cancels in the delta.
    halo_delta = m.halo - (ref.halo if ref is not None else m.halo)
    halo_pen = 1.0 - 0.6 * _lin(halo_delta, 0.05, 0.30)
    spec_pen = 1.0 - 0.5 * _lin(m.spec_clip, 0.01, 0.08)
    bg_pen = 1.0 - 0.4 * _lin(m.bg_clip, 0.02, 0.25)
    shad_pen = 1.0 - 0.5 * _lin(m.shadow_clip, 0.01, 0.10)
    return float(raw * halo_pen * spec_pen * bg_pen * shad_pen)


def tip_visibility(m: TipMetrics) -> Tuple[bool, bool]:
    """(visible, marginal) from the photometric test; run once on the step-2 burst, then frozen."""
    visible = m.contrast >= VIS_MIN_CONTRAST and m.valid_profiles >= VIS_MIN_VALID and m.cnr >= VIS_MIN_CNR
    return visible, visible and m.cnr < VIS_MARGINAL_CNR


def is_tip_visible(m: TipMetrics) -> bool:
    """Photometric part of the visibility rule (C, valid profiles, CNR); see tip_visibility."""
    return tip_visibility(m)[0]


def board_guard(stack: np.ndarray, board_mask: np.ndarray, exclude_mask: Optional[np.ndarray] = None,
                reference: Optional[BoardGuard] = None) -> BoardGuard:
    """Board-wide safety terms, computed on the board's bounding box.

    Noise and false motion are scored RELATIVE to the initial burst and give no reward for being
    lower than it (otherwise softer ISP sharpening wins by halving noise); clipping is scored
    absolutely because fixing auto-exposure clipping is a real gain.
    """
    m = board_mask > 0
    if exclude_mask is not None:
        m &= exclude_mask == 0
    ys, xs = np.where(m)
    if len(xs) == 0:
        m = np.ones(stack.shape[1:], bool)
        ys, xs = np.where(m)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub = stack[:, y0:y1, x0:x1]
    mm = m[y0:y1, x0:x1]
    px = sub.mean(axis=0)[mm]
    if sub.shape[0] > 1:
        sigma = sub.std(axis=0, ddof=1)[mm]
        motion = float(np.percentile(np.abs(np.diff(sub, axis=0))[:, mm], 99.9))
    else:
        sigma = np.zeros_like(px)
        motion = 0.0
    g = BoardGuard(
        noise=round(float(sigma.mean()), 3), motion_p999=round(motion, 2),
        hl_clip=round(float(np.mean(px >= 253)), 5), sh_clip=round(float(np.mean(px <= 8)), 5),
        mean_lum=round(float(px.mean()), 1), n_frames=int(stack.shape[0]),
    )
    if reference is not None:
        noise_n = _lin(g.noise / max(reference.noise, 0.3), 1.35, 1.0)
        motion_n = _lin(g.motion_p999 / max(reference.motion_p999, 1.0), 1.30, 1.0)
    else:
        noise_n = motion_n = 1.0
    hl_n = _lin(g.hl_clip, 0.01, 0.0005)
    sh_n = _lin(g.sh_clip, 0.05, 0.002)
    g.score = round(BOARD_WEIGHTS["noise"] * noise_n + BOARD_WEIGHTS["motion"] * motion_n
                    + BOARD_WEIGHTS["hl"] * hl_n + BOARD_WEIGHTS["sh"] * sh_n, 4)
    return g


def board_veto(cand: BoardGuard, ref: BoardGuard) -> Optional[str]:
    """Hard vetoes, always relative to the INITIAL burst of the same camera (diagnostic text)."""
    if cand.hl_clip > max(0.01, ref.hl_clip + 0.005):
        return f"highlights clipped ({cand.hl_clip * 100:.2f}% of board)"
    if cand.sh_clip > max(0.03, ref.sh_clip + 0.01):
        return f"shadows crushed ({cand.sh_clip * 100:.1f}% of board)"
    if cand.noise > max(ref.noise * 1.35, ref.noise + 0.4):
        return f"sisal noise up ({cand.noise:.2f} vs {ref.noise:.2f} DN)"
    if cand.motion_p999 > max(25.0, ref.motion_p999 * 1.3):
        return f"false-motion risk (p99.9 frame diff {cand.motion_p999:.0f} DN)"
    if cand.mean_lum < ref.mean_lum * 0.65 or cand.mean_lum > ref.mean_lum * 1.45:
        return f"brightness drift ({cand.mean_lum:.0f} vs {ref.mean_lum:.0f} DN)"
    return None


def aggregate_tips(scores: Sequence[float], marginal: Optional[Sequence[bool]] = None) -> float:
    """60 % mean + 40 % worst tip: the weakest visible tip is what makes Autodarts miss a dart.

    Marginal tips (CNR in the margin band) count in the mean but not in the min(), so a
    near-invisible tip cannot dominate the score.
    """
    if not scores:
        return 0.0
    marginal = marginal or [False] * len(scores)
    firm = [s for s, mg in zip(scores, marginal) if not mg]
    worst = float(np.min(firm)) if firm else float(np.mean(scores))
    return 0.6 * float(np.mean(scores)) + 0.4 * worst


def evaluate_tips(
    frames: Sequence[np.ndarray],
    rois: Sequence[TipROI],
    board_mask: np.ndarray,
    exclude_mask: Optional[np.ndarray] = None,
    reference: Optional[TipScore] = None,
) -> TipScore:
    """total = 100 * (0.70 * tip_component + 0.30 * board), or 100 * board with no visible tip.

    A reference must be measured at the same burst length (burst-length rule).
    """
    stack = _as_stack(frames)
    n = int(stack.shape[0])
    if reference is not None and reference.n_frames and reference.n_frames != n:
        raise ValueError(f"burst length {n} compared against a reference measured at {reference.n_frames}")
    mean = stack.mean(axis=0)
    tips: List[Optional[TipMetrics]] = []
    tip_scores: List[float] = []
    marg: List[bool] = []
    for i, r in enumerate(rois):
        if not r.visible:
            tips.append(None)
            continue
        m = tip_metrics(stack, r, mean)
        ref_m = reference.tips[i] if reference is not None and i < len(reference.tips) else None
        m.score = round(score_tip(m, ref_m), 4)
        tips.append(m)
        tip_scores.append(m.score)
        marg.append(r.marginal)
    board = board_guard(stack, board_mask, exclude_mask, reference.board if reference is not None else None)
    tip_c = aggregate_tips(tip_scores, marg)
    if not tip_scores:  # no visible tip in this camera: board-only fallback
        total = 100.0 * board.score
    else:
        total = 100.0 * (W_TIP * tip_c + W_BOARD * board.score)
    veto = board_veto(board, reference.board) if reference is not None else None
    return TipScore(total=round(total, 2), tip_component=round(tip_c, 4), board_component=board.score,
                    tips=tips, board=board, veto=veto, n_frames=n, board_only=not tip_scores)


def make_dart_exclusion(shape: Tuple[int, int], rois: Sequence[TipROI], length: int = 420, half_w: int = 45) -> np.ndarray:
    """Mask (255) covering each dart body from the tip back toward the flight, for board stats."""
    mask = np.zeros(shape, np.uint8)
    for r in rois:
        dx, dy = r.direction
        px, py = -dy, dx
        tx, ty = r.tip
        bx, by = tx - dx * length, ty - dy * length
        poly = np.array([[tx + px * 12 + dx * 8, ty + py * 12 + dy * 8], [tx - px * 12 + dx * 8, ty - py * 12 + dy * 8],
                         [bx - px * half_w, by - py * half_w], [bx + px * half_w, by + py * half_w]], np.int32)
        cv2.fillConvexPoly(mask, poly, 255)
    return mask


def refine_tip_axis(frames_or_stack, roi: TipROI, max_shift: int = 8, max_angle_deg: float = 12.0) -> TipROI:
    """Snap the detector's tip axis onto the steel point (run ONCE per camera, then freeze).

    Searches a sideways shift (never along the axis) and a small rotation that maximise the median
    ridge contrast. The metric is very sensitive to a 2-3 px offset on a 4 px wide point, so the
    ROI used for the initial and every candidate burst must be this same frozen axis.
    """
    stack = _as_stack(frames_or_stack)
    mean = stack.mean(axis=0)
    dx, dy = roi.direction
    n = math.hypot(dx, dy) or 1.0
    dx, dy = dx / n, dy / n
    best = (-1.0, roi.tip, (dx, dy))
    length = min(PROFILE_LEN, 0.85 * roi.point_len)
    c0 = PROFILE_HALF
    for ang in np.arange(-max_angle_deg, max_angle_deg + 0.1, 3.0):
        ca, sa = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        ddx, ddy = dx * ca - dy * sa, dx * sa + dy * ca
        px, py = -ddy, ddx
        for sh in range(-max_shift, max_shift + 1):
            tip = (roi.tip[0] + sh * px, roi.tip[1] + sh * py)
            prof = sample_profiles(mean, tip, (ddx, ddy), length)
            centre = prof[:, c0 - 1:c0 + 2].mean(axis=1)
            bl = np.median(prof[:, c0 - FLANK_OUT:c0 - FLANK_IN + 1], axis=1)
            br = np.median(prof[:, c0 + FLANK_IN:c0 + FLANK_OUT + 1], axis=1)
            # Ridge/valley test: the point must differ from BOTH flanks with the same polarity, so a
            # segment boundary or a wire edge (one side equal to the centre) scores ~0.
            dl, dr = centre - bl, centre - br
            ridge = np.where(np.sign(dl) == np.sign(dr), np.minimum(np.abs(dl), np.abs(dr)), 0.0)
            score = float(np.median(ridge)) - 0.3 * abs(sh) - 0.2 * abs(ang)
            if score > best[0]:
                best = (score, tip, (ddx, ddy))
    return dataclasses.replace(roi, tip=(float(best[1][0]), float(best[1][1])), direction=best[2])


def _tip_polarity(mean: np.ndarray, roi: TipROI) -> int:
    """+1 if the steel point is brighter than BOTH flanks, -1 if darker, 0 if undecided.

    Same ridge test as refine_tip_axis: only profiles where the centre differs from both flanks with
    the same sign vote, so a segment boundary next to the point cannot decide it.
    """
    prof = sample_profiles(mean, roi.tip, roi.direction, min(PROFILE_LEN, 0.85 * roi.point_len))
    c0 = PROFILE_HALF
    centre = prof[:, c0 - 1:c0 + 2].mean(axis=1)
    bl = np.median(prof[:, c0 - FLANK_OUT:c0 - FLANK_IN + 1], axis=1)
    br = np.median(prof[:, c0 + FLANK_IN:c0 + FLANK_OUT + 1], axis=1)
    dl, dr = centre - bl, centre - br
    votes = np.sign(dl)[np.sign(dl) == np.sign(dr)]
    total = float(votes.sum())
    return int(np.sign(total)) if abs(total) >= max(2, 0.3 * len(prof)) else 0


def tip_box(tip: Tuple[float, float], shape: Tuple[int, int]) -> Tuple[int, int, int, int]:
    """120x120 stats box with the tip at the bottom-centre third, clamped to the frame (h, w)."""
    bw, bh = TIP_BOX
    H, W = shape[:2]
    x = int(round(tip[0] - bw / 2))
    y = int(round(tip[1] - 2 * bh / 3))
    x = max(0, min(W - bw, x))
    y = max(0, min(H - bh, y))
    return (x, y, min(bw, W), min(bh, H))


def tip_rois_for_camera(det, cam_id: str, stack) -> List[TipROI]:
    """Frozen TipROIs for one camera from a TipDetection, in DartTip.id order.

    Each detector tip is refined once on `stack` (frames or a float32 gray stack), then tested for
    visibility: seen axis and not occluded, refinement shift <= 5 px, C >= 10 DN, >= 60 % valid
    profiles and CNR >= 6 (CNR < 8 marks it marginal).
    """
    stack = _as_stack(stack)
    shape = stack.shape[1:]
    mean = stack.mean(axis=0)
    out: List[TipROI] = []
    for t in sorted(det.tips, key=lambda t: t.id):
        ct = t.per_cam.get(cam_id)
        if ct is None:
            continue
        raw = TipROI(tip=(float(ct.px[0]), float(ct.px[1])), direction=tuple(map(float, ct.direction)),
                     box=tip_box(ct.px, shape), point_len=float(ct.point_len), dart_id=int(t.id))
        r = refine_tip_axis(stack, raw)
        r = dataclasses.replace(r, box=tip_box(r.tip, shape), polarity=_tip_polarity(mean, r))
        shift = math.hypot(r.tip[0] - raw.tip[0], r.tip[1] - raw.tip[1])
        m = tip_metrics(stack, r, mean)
        visible, marginal = tip_visibility(m)
        # The refinement keeps the best of ~150 candidate positions, so on its own it also "finds" a
        # ridge (wire, segment edge, barrel shadow) near a tip that is not there: on plain board
        # spots it passed the test 40-65 % of the time. The detector's projected position is the
        # only place that is certain to be the tip, so the signal must also be there, unsearched.
        m_est = tip_metrics(stack, raw, mean)
        faint_at_est = m_est.contrast < VIS_MIN_CONTRAST or m_est.cnr < VIS_MIN_CNR
        faint = False
        reason = None
        if not ct.seen_axis or ct.occluded:
            reason = "occluded" if ct.occluded else "axis not seen"
        elif shift > VIS_MAX_REFINE_SHIFT:
            reason, faint = f"refined {shift:.1f} px away", True
        elif not visible:
            reason, faint = f"low contrast (C={m.contrast:.0f}, CNR={m.cnr:.1f}, valid={m.valid_profiles:.2f})", True
        elif faint_at_est:
            reason, faint = f"faint at the estimated position (C={m_est.contrast:.0f}, CNR={m_est.cnr:.1f})", True
        if reason is not None:
            # A hidden tip keeps the projected position (not the searched one), so its picture and the
            # probe look at the place where the tip should be.
            r = dataclasses.replace(raw, polarity=0)
        r.visible = reason is None
        r.marginal = r.visible and marginal
        r.ref_contrast = float(m.contrast if r.visible else m_est.contrast)
        r.hidden_reason = reason
        r.faint = faint
        r.est_contrast, r.est_cnr = float(m_est.contrast), float(m_est.cnr)
        out.append(r)
    return out
