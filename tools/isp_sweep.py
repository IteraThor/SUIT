#!/usr/bin/env python3
"""
ISP Characterization Sweep
===========================
Exhaustive sweep of camera ISP parameters (gain, sharpness, contrast, gamma)
with step-10 granularity. Captures a JPEG and computes quality metrics for
every combination. Results are saved to CSV for offline analysis.

Total combinations: 14 × 11 × 11 × 11 = 18,634
Estimated runtime: ~40 minutes on /dev/video0

Usage:
    PYTHONPATH=/home/autodarts/SUIT-Fedora python3 tools/isp_sweep.py

Output:
    /home/autodarts/.local/share/suit/isp_sweep/isp_sweep_results.csv
    /home/autodarts/.local/share/suit/isp_sweep/sweep_g*.jpg
"""

import csv
import os
import sys
import time

import cv2
import numpy as np

# ── Parameter grid ──────────────────────────────────────────────────────────
GAIN_VALUES      = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 128]
SHARPNESS_VALUES = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
CONTRAST_VALUES  = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
GAMMA_VALUES     = [100, 140, 180, 220, 260, 300, 340, 380, 420, 460, 500]

TOTAL = len(GAIN_VALUES) * len(SHARPNESS_VALUES) * len(CONTRAST_VALUES) * len(GAMMA_VALUES)

# ── Fixed controls ──────────────────────────────────────────────────────────
EXPOSURE       = 300
BRIGHTNESS     = 0
SATURATION     = 68

# ── Output ──────────────────────────────────────────────────────────────────
OUTPUT_DIR = "/home/autodarts/.local/share/suit/isp_sweep"
CSV_FILE   = os.path.join(OUTPUT_DIR, "isp_sweep_results.csv")
JPEG_QUALITY = 95

# ── Camera ──────────────────────────────────────────────────────────────────
DEVICE     = "/dev/video0"
WIDTH      = 1280
HEIGHT     = 960

# ── Crop regions (fixed geometry, no board detection needed) ────────────────
# Center crop: 640×480 from frame center — covers the bulk of the dartboard
CENTER_X = (WIDTH - 640) // 2    # 320
CENTER_Y = (HEIGHT - 480) // 2   # 240
# Corner crop: 200×200 from top-left — typically outside the lit board, used
# for temporal noise measurement
CORNER_X = 0
CORNER_Y = 0

# ── CSV columns ─────────────────────────────────────────────────────────────
CSV_COLUMNS = [
    "gain", "sharpness", "contrast", "gamma",
    "lap_var", "lap_var_center",
    "sobel_energy", "sobel_energy_center",
    "temporal_noise",
    "mean_lum", "mean_lum_center",
    "p90", "p10", "dynamic_range",
    "highlight_clip_pct", "shadow_clip_pct",
    "filename",
]


def open_camera():
    """Open camera stream with fixed resolution and codec."""
    cap = cv2.VideoCapture(DEVICE, cv2.CAP_V4L2)
    if not cap.isOpened():
        print(f"ERROR: Cannot open {DEVICE}", file=sys.stderr)
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)

    # Fixed controls
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)   # manual exposure mode
    cap.set(cv2.CAP_PROP_EXPOSURE, EXPOSURE)
    cap.set(cv2.CAP_PROP_BRIGHTNESS, BRIGHTNESS)
    cap.set(cv2.CAP_PROP_SATURATION, SATURATION)

    # Warm up: read and discard 15 frames
    for _ in range(15):
        cap.read()

    return cap


def set_controls(cap, gain, sharpness, contrast, gamma):
    """Apply ISP controls and flush 2 frames to let the pipeline settle."""
    cap.set(cv2.CAP_PROP_GAIN, gain)
    cap.set(cv2.CAP_PROP_SHARPNESS, sharpness)
    cap.set(cv2.CAP_PROP_CONTRAST, contrast)
    cap.set(cv2.CAP_PROP_GAMMA, gamma)
    # Flush 2 frames — ISP has 1-frame pipeline latency, 2 gives safety margin
    cap.read()
    cap.read()


def capture_burst(cap, n=3):
    """Capture n consecutive frames and return as list of BGR arrays."""
    frames = []
    for _ in range(n):
        ret, f = cap.read()
        if not ret:
            print("ERROR: Frame capture failed", file=sys.stderr)
            sys.exit(1)
        frames.append(f)
    return frames


def compute_metrics(frames):
    """Compute all quality metrics from a 3-frame burst.

    Uses frame[0] for spatial metrics.
    Uses all 3 frames for temporal noise.
    """
    # Convert all frames to grayscale
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    gray = grays[0]  # primary measurement frame

    # Crops
    gray_center = gray[CENTER_Y:CENTER_Y + 480, CENTER_X:CENTER_X + 640]

    # ── Spatial metrics ─────────────────────────────────────────────────
    lap_full = cv2.Laplacian(gray, cv2.CV_64F)
    lap_var = float(lap_full.var())

    lap_center = cv2.Laplacian(gray_center, cv2.CV_64F)
    lap_var_center = float(lap_center.var())

    sobel_full = cv2.Sobel(gray, cv2.CV_64F, 1, 1)
    sobel_energy = float(np.mean(np.abs(sobel_full)))

    sobel_center = cv2.Sobel(gray_center, cv2.CV_64F, 1, 1)
    sobel_energy_center = float(np.mean(np.abs(sobel_center)))

    # ── Luminance metrics ───────────────────────────────────────────────
    mean_lum = float(np.mean(gray))
    mean_lum_center = float(np.mean(gray_center))

    flat = gray.ravel()
    p90 = float(np.percentile(flat, 90))
    p10 = float(np.percentile(flat, 10))
    dynamic_range = p90 - p10

    highlight_clip_pct = float(np.sum(flat >= 253)) / flat.size
    shadow_clip_pct = float(np.sum(flat <= 5)) / flat.size

    # ── Temporal noise (3-frame burst on corner crop) ───────────────────
    corner_crops = [
        g[CORNER_Y:CORNER_Y + 200, CORNER_X:CORNER_X + 200].astype(np.float32)
        for g in grays
    ]
    stack = np.stack(corner_crops, axis=0)          # (3, 200, 200)
    temporal_noise = float(np.mean(np.std(stack, axis=0)))

    return {
        "lap_var": lap_var,
        "lap_var_center": lap_var_center,
        "sobel_energy": sobel_energy,
        "sobel_energy_center": sobel_energy_center,
        "temporal_noise": temporal_noise,
        "mean_lum": mean_lum,
        "mean_lum_center": mean_lum_center,
        "p90": p90,
        "p10": p10,
        "dynamic_range": dynamic_range,
        "highlight_clip_pct": highlight_clip_pct,
        "shadow_clip_pct": shadow_clip_pct,
    }


def make_filename(gain, sharpness, contrast, gamma):
    return f"sweep_g{gain:03d}_s{sharpness:03d}_c{contrast:03d}_gam{gamma:03d}.jpg"


def restore_defaults(cap):
    """Restore factory default ISP settings."""
    cap.set(cv2.CAP_PROP_GAIN, 64)
    cap.set(cv2.CAP_PROP_SHARPNESS, 40)
    cap.set(cv2.CAP_PROP_CONTRAST, 34)
    cap.set(cv2.CAP_PROP_GAMMA, 300)
    cap.set(cv2.CAP_PROP_BRIGHTNESS, 0)
    cap.set(cv2.CAP_PROP_SATURATION, 68)
    print("Restored factory defaults.")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"ISP Characterization Sweep")
    print(f"==========================")
    print(f"Device:       {DEVICE}")
    print(f"Resolution:   {WIDTH}×{HEIGHT}")
    print(f"Exposure:     {EXPOSURE} (locked)")
    print(f"Grid:         {len(GAIN_VALUES)}×{len(SHARPNESS_VALUES)}×{len(CONTRAST_VALUES)}×{len(GAMMA_VALUES)} = {TOTAL} combinations")
    print(f"Output:       {OUTPUT_DIR}")
    print(f"JPEG quality: {JPEG_QUALITY}")
    print()

    cap = open_camera()
    print(f"Camera opened. Starting sweep...")
    print()

    t_start = time.time()
    count = 0

    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for gain in GAIN_VALUES:
            for sharpness in SHARPNESS_VALUES:
                for contrast in CONTRAST_VALUES:
                    for gamma in GAMMA_VALUES:
                        # Apply controls and settle
                        set_controls(cap, gain, sharpness, contrast, gamma)

                        # Capture 3-frame burst
                        frames = capture_burst(cap, n=3)

                        # Compute metrics
                        metrics = compute_metrics(frames)

                        # Save JPEG
                        fname = make_filename(gain, sharpness, contrast, gamma)
                        fpath = os.path.join(OUTPUT_DIR, fname)
                        cv2.imwrite(fpath, frames[0],
                                    [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])

                        # Write CSV row
                        row = {
                            "gain": gain,
                            "sharpness": sharpness,
                            "contrast": contrast,
                            "gamma": gamma,
                            "filename": fname,
                        }
                        for k, v in metrics.items():
                            row[k] = f"{v:.6f}"
                        writer.writerow(row)

                        count += 1

                        # Progress every 100 combinations
                        if count % 100 == 0 or count == TOTAL:
                            elapsed = time.time() - t_start
                            rate = count / elapsed
                            remaining = (TOTAL - count) / rate if rate > 0 else 0
                            pct = 100.0 * count / TOTAL
                            print(f"  [{count:6d}/{TOTAL}] {pct:5.1f}%  "
                                  f"elapsed={elapsed:.0f}s  "
                                  f"ETA={remaining:.0f}s  "
                                  f"({rate:.1f} combos/s)  "
                                  f"current: G={gain} S={sharpness} C={contrast} Gam={gamma}")
                            # Flush CSV to disk periodically
                            f.flush()

    elapsed_total = time.time() - t_start
    print()
    print(f"Sweep complete: {count} combinations in {elapsed_total:.0f}s "
          f"({elapsed_total / 60:.1f} min)")
    print(f"CSV:    {CSV_FILE}")
    print(f"Images: {OUTPUT_DIR}/")

    # Restore factory defaults
    restore_defaults(cap)
    cap.release()
    print("Camera released. Done.")


if __name__ == "__main__":
    main()
