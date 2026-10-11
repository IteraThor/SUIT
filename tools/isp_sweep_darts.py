#!/usr/bin/env python3
"""
ISP Sweep With Inserted Darts
==============================
Exhaustive 18,634-combination sweep on /dev/video0 with physical darts inserted.
For every combination:
1. Applies (gain, sharpness, contrast, gamma)
2. Flushes 2 frames, captures 3-frame burst (for temporal noise)
3. Loads the corresponding empty board frame from /home/autodarts/.local/share/suit/isp_sweep/
4. Computes ground-truth dart difference metrics:
   - Dart Signal (peak & mean diff)
   - Dart-to-Background Signal-to-Noise Ratio (SNR)
   - Dart shaft gradient energy in difference image
   - Strong dart pixel count (>30 DN)
   - Barrel specular highlight clipping
5. Saves JPEG to /home/autodarts/.local/share/suit/isp_sweep_darts/
6. Writes row to isp_sweep_darts_results.csv

Runtime: ~50 minutes
"""

import csv
import os
import sys
import time

import cv2
import numpy as np

# Parameter grid (step-10)
GAIN_VALUES      = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 128]
SHARPNESS_VALUES = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
CONTRAST_VALUES  = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
GAMMA_VALUES     = [100, 140, 180, 220, 260, 300, 340, 380, 420, 460, 500]

TOTAL = len(GAIN_VALUES) * len(SHARPNESS_VALUES) * len(CONTRAST_VALUES) * len(GAMMA_VALUES)

# Camera & controls
DEVICE     = "/dev/video0"
WIDTH      = 1280
HEIGHT     = 960
EXPOSURE   = 300
BRIGHTNESS = 0
SATURATION = 68

# Output & Empty Reference Directories
EMPTY_DIR   = "/home/autodarts/.local/share/suit/isp_sweep"
OUTPUT_DIR  = "/home/autodarts/.local/share/suit/isp_sweep_darts"
CSV_FILE    = os.path.join(OUTPUT_DIR, "isp_sweep_darts_results.csv")
JPEG_QUALITY = 95

# Dart ROI coordinates on Camera 0 (the 20 segment with inserted darts)
DART_Y1, DART_Y2 = 180, 520
DART_X1, DART_X2 = 680, 1050

# Corner crop for temporal noise
CORNER_X, CORNER_Y = 0, 0

CSV_COLUMNS = [
    "gain", "sharpness", "contrast", "gamma",
    "dart_snr", "dart_diff_peak", "dart_diff_mean", "dart_strong_px",
    "shaft_sobel", "barrel_blown_px", "bg_diff_noise",
    "lap_var", "lap_var_center", "sobel_energy", "temporal_noise",
    "mean_lum", "dynamic_range", "highlight_clip_pct", "shadow_clip_pct",
    "filename",
]


def open_camera():
    cap = cv2.VideoCapture(DEVICE, cv2.CAP_V4L2)
    if not cap.isOpened():
        print(f"ERROR: Cannot open {DEVICE}", file=sys.stderr, flush=True)
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
    cap.set(cv2.CAP_PROP_EXPOSURE, EXPOSURE)
    cap.set(cv2.CAP_PROP_BRIGHTNESS, BRIGHTNESS)
    cap.set(cv2.CAP_PROP_SATURATION, SATURATION)

    for _ in range(15):
        cap.read()

    return cap


def set_controls(cap, gain, sharpness, contrast, gamma):
    cap.set(cv2.CAP_PROP_GAIN, gain)
    cap.set(cv2.CAP_PROP_SHARPNESS, sharpness)
    cap.set(cv2.CAP_PROP_CONTRAST, contrast)
    cap.set(cv2.CAP_PROP_GAMMA, gamma)
    cap.read()
    cap.read()


def capture_burst(cap, n=3):
    frames = []
    for _ in range(n):
        ret, f = cap.read()
        if not ret:
            print("ERROR: Capture failed", file=sys.stderr, flush=True)
            sys.exit(1)
        frames.append(f)
    return frames


def restore_defaults(cap):
    cap.set(cv2.CAP_PROP_GAIN, 64)
    cap.set(cv2.CAP_PROP_SHARPNESS, 40)
    cap.set(cv2.CAP_PROP_CONTRAST, 34)
    cap.set(cv2.CAP_PROP_GAMMA, 300)
    cap.set(cv2.CAP_PROP_BRIGHTNESS, 0)
    cap.set(cv2.CAP_PROP_SATURATION, 68)
    print("Restored factory defaults.", flush=True)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("ISP Sweep With Inserted Darts", flush=True)
    print("=============================", flush=True)
    print(f"Total combinations: {TOTAL}", flush=True)
    print(f"Empty reference library: {EMPTY_DIR}", flush=True)
    print(f"Output directory: {OUTPUT_DIR}", flush=True)

    cap = open_camera()
    print("Camera initialized. Starting sweep...\n", flush=True)

    t_start = time.time()
    count = 0

    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for gain in GAIN_VALUES:
            for sharpness in SHARPNESS_VALUES:
                for contrast in CONTRAST_VALUES:
                    for gamma in GAMMA_VALUES:
                        set_controls(cap, gain, sharpness, contrast, gamma)
                        frames = capture_burst(cap, n=3)
                        dart_frame = frames[0]

                        # File naming
                        fname_empty = f"sweep_g{gain:03d}_s{sharpness:03d}_c{contrast:03d}_gam{gamma:03d}.jpg"
                        fname_dart = f"sweep_darts_g{gain:03d}_s{sharpness:03d}_c{contrast:03d}_gam{gamma:03d}.jpg"

                        # Save dart frame JPEG
                        fpath_dart = os.path.join(OUTPUT_DIR, fname_dart)
                        cv2.imwrite(fpath_dart, dart_frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])

                        # Grayscale conversions
                        gray_dart = cv2.cvtColor(dart_frame, cv2.COLOR_BGR2GRAY)
                        grays_burst = [cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY) for fr in frames]

                        # Load corresponding empty frame
                        fpath_empty = os.path.join(EMPTY_DIR, fname_empty)
                        f_empty = cv2.imread(fpath_empty, cv2.IMREAD_GRAYSCALE)

                        # Standard Camera Metrics
                        lap_var = float(cv2.Laplacian(gray_dart, cv2.CV_64F).var())
                        lap_var_center = float(cv2.Laplacian(gray_dart[240:720, 320:960], cv2.CV_64F).var())
                        sobel_energy = float(np.mean(np.abs(cv2.Sobel(gray_dart, cv2.CV_64F, 1, 1))))

                        # Temporal noise
                        corners = [g[CORNER_Y:CORNER_Y+200, CORNER_X:CORNER_X+200].astype(np.float32) for g in grays_burst]
                        temporal_noise = float(np.mean(np.std(np.stack(corners, axis=0), axis=0)))

                        flat = gray_dart.ravel()
                        mean_lum = float(np.mean(flat))
                        p90 = float(np.percentile(flat, 90))
                        p10 = float(np.percentile(flat, 10))
                        dynamic_range = p90 - p10
                        highlight_clip = float(np.sum(flat >= 253)) / flat.size
                        shadow_clip = float(np.sum(flat <= 5)) / flat.size

                        # Ground-Truth Dart Difference Metrics
                        if f_empty is not None and f_empty.shape == gray_dart.shape:
                            diff = cv2.absdiff(f_empty, gray_dart)

                            # Dart region diff
                            dart_diff_crop = diff[DART_Y1:DART_Y2, DART_X1:DART_X2]
                            dart_gray_crop = gray_dart[DART_Y1:DART_Y2, DART_X1:DART_X2]

                            dart_diff_peak = float(np.max(dart_diff_crop))
                            dart_sig_mask = dart_diff_crop > 18
                            dart_diff_mean = float(np.mean(dart_diff_crop[dart_sig_mask])) if np.any(dart_sig_mask) else 0.0
                            dart_strong_px = int(np.sum(dart_diff_crop > 30))

                            # Shaft edge gradient in difference image
                            shaft_sobel = float(np.mean(np.abs(cv2.Sobel(dart_diff_crop, cv2.CV_64F, 1, 1))))
                            barrel_blown = int(np.sum(dart_gray_crop >= 253))

                            # Background noise outside dart region (center 640x480 excluding dart crop)
                            bg_center = diff[240:720, 320:960].copy()
                            # zero out dart region
                            bg_center[DART_Y1-240:DART_Y2-240, DART_X1-320:DART_X2-320] = 0
                            bg_pixels = bg_center[bg_center > 0]
                            bg_noise = float(np.mean(bg_pixels)) if len(bg_pixels) > 0 else 1.0
                            bg_std = float(np.std(bg_pixels)) if len(bg_pixels) > 0 else 1.0

                            dart_snr = float((dart_diff_mean - bg_noise) / max(0.1, bg_std))
                        else:
                            dart_snr = 0.0
                            dart_diff_peak = 0.0
                            dart_diff_mean = 0.0
                            dart_strong_px = 0
                            shaft_sobel = 0.0
                            barrel_blown = 0
                            bg_noise = 0.0

                        # CSV row
                        row = {
                            "gain": gain,
                            "sharpness": sharpness,
                            "contrast": contrast,
                            "gamma": gamma,
                            "dart_snr": f"{dart_snr:.4f}",
                            "dart_diff_peak": f"{dart_diff_peak:.1f}",
                            "dart_diff_mean": f"{dart_diff_mean:.2f}",
                            "dart_strong_px": dart_strong_px,
                            "shaft_sobel": f"{shaft_sobel:.4f}",
                            "barrel_blown_px": barrel_blown,
                            "bg_diff_noise": f"{bg_noise:.2f}",
                            "lap_var": f"{lap_var:.2f}",
                            "lap_var_center": f"{lap_var_center:.2f}",
                            "sobel_energy": f"{sobel_energy:.4f}",
                            "temporal_noise": f"{temporal_noise:.4f}",
                            "mean_lum": f"{mean_lum:.2f}",
                            "dynamic_range": f"{dynamic_range:.1f}",
                            "highlight_clip_pct": f"{highlight_clip:.6f}",
                            "shadow_clip_pct": f"{shadow_clip:.6f}",
                            "filename": fname_dart,
                        }
                        writer.writerow(row)
                        count += 1

                        if count % 100 == 0 or count == TOTAL:
                            elapsed = time.time() - t_start
                            rate = count / elapsed
                            remaining = (TOTAL - count) / rate if rate > 0 else 0
                            pct = 100.0 * count / TOTAL
                            print(f"  [{count:5d}/{TOTAL}] {pct:5.1f}%  "
                                  f"elapsed={elapsed:.0f}s  "
                                  f"ETA={remaining:.0f}s  "
                                  f"({rate:.1f} combos/s)  "
                                  f"G={gain} S={sharpness} C={contrast} Gam={gamma}  "
                                  f"SNR={dart_snr:.1f}x Peak={dart_diff_peak:.0f}",
                                  flush=True)
                            f.flush()

    elapsed_total = time.time() - t_start
    print(f"\nDart sweep complete: {count} combinations in {elapsed_total:.0f}s ({elapsed_total/60:.1f} min)", flush=True)
    restore_defaults(cap)
    cap.release()
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
