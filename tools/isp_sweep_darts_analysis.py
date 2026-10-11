#!/usr/bin/env python3
"""
Dart Sweep Analysis
===================
Analyzes the 18,634-combination sweep with physical darts inserted.
Ranks settings by ground-truth Autodarts recognition:
- Dart Signal-to-Noise Ratio (SNR) in difference image
- Shaft & Tip Sobel edge steepness in difference image
- Clean background difference noise floor
- Anti-blooming specular highlight protection
- Segment dynamic range and sharpness
"""

import argparse
import csv
import os
import sys
import numpy as np


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze dart sweep CSV metrics.")
    parser.add_argument("--csv", type=str, default="data/isp_sweep_darts/isp_sweep_darts_results.csv")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--min-contrast", type=int, default=25, help="Min contrast (default: 25)")
    parser.add_argument("--max-contrast", type=int, default=60, help="Max contrast (default: 60)")
    parser.add_argument("--min-lum", type=float, default=45.0)
    parser.add_argument("--max-lum", type=float, default=95.0)
    parser.add_argument("--max-clip", type=float, default=0.03)
    parser.add_argument("--max-shadow", type=float, default=0.25)
    return parser.parse_args()


def main():
    args = parse_args()
    if not os.path.exists(args.csv):
        print(f"File not found: {args.csv}", file=sys.stderr)
        sys.exit(1)

    rows = []
    with open(args.csv, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                row = {
                    "gain": int(float(r["gain"])),
                    "sharpness": int(float(r["sharpness"])),
                    "contrast": int(float(r["contrast"])),
                    "gamma": int(float(r["gamma"])),
                    "dart_snr": float(r["dart_snr"]),
                    "dart_diff_peak": float(r["dart_diff_peak"]),
                    "dart_diff_mean": float(r["dart_diff_mean"]),
                    "dart_strong_px": int(float(r["dart_strong_px"])),
                    "shaft_sobel": float(r["shaft_sobel"]),
                    "barrel_blown_px": int(float(r["barrel_blown_px"])),
                    "bg_diff_noise": float(r["bg_diff_noise"]),
                    "lap_var": float(r["lap_var"]),
                    "lap_var_center": float(r["lap_var_center"]),
                    "sobel_energy": float(r["sobel_energy"]),
                    "temporal_noise": float(r["temporal_noise"]),
                    "mean_lum": float(r["mean_lum"]),
                    "dynamic_range": float(r["dynamic_range"]),
                    "highlight_clip_pct": float(r["highlight_clip_pct"]),
                    "shadow_clip_pct": float(r["shadow_clip_pct"]),
                    "filename": r["filename"],
                }
                rows.append(row)
            except Exception:
                continue

    total = len(rows)
    print(f"Loaded {total} sweep records.")

    # Apply filters
    valid = []
    for r in rows:
        if r["contrast"] < args.min_contrast or r["contrast"] > args.max_contrast:
            continue
        if r["mean_lum"] < args.min_lum or r["mean_lum"] > args.max_lum:
            continue
        if r["highlight_clip_pct"] > args.max_clip:
            continue
        if r["shadow_clip_pct"] > args.max_shadow:
            continue
        if r["dart_diff_peak"] < 80.0:  # Must have noticeable dart difference
            continue
        valid.append(r)

    print(f"Valid candidates within exposure & contrast bounds: {len(valid)} / {total} ({100.0*len(valid)/total:.1f}%)")
    if not valid:
        return

    # Normalize metrics
    def norm(arr):
        mn, mx = np.min(arr), np.max(arr)
        if mx == mn:
            return np.zeros_like(arr)
        return (arr - mn) / (mx - mn)

    v_snr = np.array([r["dart_snr"] for r in valid])
    v_shaft = np.array([r["shaft_sobel"] for r in valid])
    v_strong = np.array([r["dart_strong_px"] for r in valid])
    v_bg_noise = np.array([r["bg_diff_noise"] for r in valid])
    v_lap = np.array([r["lap_var_center"] for r in valid])
    v_dyn = np.array([r["dynamic_range"] for r in valid])
    v_barrel_blown = np.array([r["barrel_blown_px"] for r in valid])

    norm_snr = norm(v_snr)
    norm_shaft = norm(v_shaft)
    norm_strong = norm(v_strong)
    norm_bg_inv = 1.0 - norm(v_bg_noise)
    norm_lap = norm(v_lap)
    norm_dyn = norm(v_dyn)
    norm_barrel_inv = 1.0 - norm(v_barrel_blown)

    # Composite Autodarts Ground-Truth Score:
    # 35% Dart SNR (separating dart from background)
    # 25% Shaft & Tip Sobel edge steepness
    # 15% Strong dart pixel footprint
    # 10% Clean background diff noise
    # 10% Board Laplacian sharpness
    #  5% Anti-blooming (avoiding blown barrel glints)
    scores = (
        0.35 * norm_snr
        + 0.25 * norm_shaft
        + 0.15 * norm_strong
        + 0.10 * norm_bg_inv
        + 0.10 * norm_lap
        + 0.05 * norm_barrel_inv
    )

    for i, r in enumerate(valid):
        r["score"] = float(scores[i])

    ranked_idx = np.argsort(-scores)
    top_candidates = [valid[i] for i in ranked_idx[:args.top]]

    gains = [r["gain"] for r in top_candidates]
    sharps = [r["sharpness"] for r in top_candidates]
    contrasts = [r["contrast"] for r in top_candidates]
    gammas = [r["gamma"] for r in top_candidates]

    lines = []
    lines.append("=" * 88)
    lines.append("           GROUND-TRUTH DART INSERTION SWEEP ANALYSIS REPORT")
    lines.append("=" * 88)
    lines.append(f"Total Sweep Combinations: {total}")
    lines.append(f"Valid Viable Combinations: {len(valid)} ({100.0*len(valid)/total:.1f}%)")
    lines.append("-" * 88)
    lines.append(f"TOP {len(top_candidates)} PARAMETER DISTRIBUTIONS:")
    lines.append(f"  • Gain:      median={int(np.median(gains))}, range=[{min(gains)}, {max(gains)}], unique={sorted(set(gains))}")
    lines.append(f"  • Sharpness: median={int(np.median(sharps))}, range=[{min(sharps)}, {max(sharps)}], unique={sorted(set(sharps))}")
    lines.append(f"  • Contrast:  median={int(np.median(contrasts))}, range=[{min(contrasts)}, {max(contrasts)}], unique={sorted(set(contrasts))}")
    lines.append(f"  • Gamma:     median={int(np.median(gammas))}, range=[{min(gammas)}, {max(gammas)}], unique={sorted(set(gammas))}")
    lines.append("=" * 88)
    lines.append(
        f"{'#':<3} {'Gain':<5} {'Sharp':<6} {'Contr':<6} {'Gamma':<6} {'Score':<7} "
        f"{'DartSNR':<8} {'ShaftSob':<9} {'PeakDiff':<9} {'StrongPx':<9} {'BGNoise':<8} {'LapCtr':<8} Filename"
    )
    lines.append("-" * 88)

    for rank, r in enumerate(top_candidates, 1):
        lines.append(
            f"{rank:<3} {r['gain']:<5} {r['sharpness']:<6} {r['contrast']:<6} {r['gamma']:<6} "
            f"{r['score']:.4f}  {r['dart_snr']:<8.1f} {r['shaft_sobel']:<9.2f} {r['dart_diff_peak']:<9.0f} "
            f"{r['dart_strong_px']:<9d} {r['bg_diff_noise']:<8.2f} {r['lap_var_center']:<8.1f} {r['filename']}"
        )
    lines.append("=" * 88)

    winner = top_candidates[0]
    lines.append("\nRECOMMENDED GROUND-TRUTH WINNER:")
    lines.append(f"  --> Gain:       {winner['gain']}")
    lines.append(f"  --> Sharpness:  {winner['sharpness']}")
    lines.append(f"  --> Contrast:   {winner['contrast']}")
    lines.append(f"  --> Gamma:      {winner['gamma']}")
    lines.append(f"  --> Dart SNR:   {winner['dart_snr']:.2f}x above noise floor")
    lines.append(f"  --> Peak Diff:  {winner['dart_diff_peak']:.0f} DN")
    lines.append(f"  --> Shaft Sobel:{winner['shaft_sobel']:.2f}")
    lines.append(f"  --> Strong Px:  {winner['dart_strong_px']} pixels")
    lines.append(f"  --> Image:      {winner['filename']}")
    lines.append("=" * 88)

    report_text = "\n".join(lines)
    print(report_text)

    out_rep = "data/isp_sweep_darts/isp_sweep_darts_report.txt"
    with open(out_rep, "w", encoding="utf-8") as f:
        f.write(report_text + "\n")
    print(f"\nSaved report to {out_rep}")


if __name__ == "__main__":
    main()
