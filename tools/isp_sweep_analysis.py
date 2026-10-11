#!/usr/bin/env python3
"""
ISP Sweep Analysis
===================
Analyzes the results of the ISP characterization sweep CSV.
Filters out clipped / unviable images, computes normalized multi-objective
scores prioritizing edge sharpness and gradient while penalizing noise,
computes the Pareto-optimal frontier, and ranks the best ISP parameter configurations.

Usage:
    python3 tools/isp_sweep_analysis.py [--csv data/isp_sweep_results.csv] [--top 20]
"""

import argparse
import csv
import os
import sys
import numpy as np


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze ISP sweep CSV metrics.")
    parser.add_argument(
        "--csv",
        type=str,
        default="data/isp_sweep_results.csv",
        help="Path to the sweep results CSV file.",
    )
    parser.add_argument(
        "--output-report",
        type=str,
        default="data/isp_sweep_report.txt",
        help="Path to save the summary report text file.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of top candidates to display.",
    )
    # Weights for scoring
    parser.add_argument("--w-lap", type=float, default=0.40, help="Weight for Center Laplacian variance (sharpness)")
    parser.add_argument("--w-sobel", type=float, default=0.25, help="Weight for Center Sobel energy (gradients)")
    parser.add_argument("--w-noise", type=float, default=0.20, help="Weight for Temporal Noise suppression")
    parser.add_argument("--w-dyn", type=float, default=0.10, help="Weight for Dynamic Range (p90 - p10)")
    parser.add_argument("--w-clip", type=float, default=0.05, help="Weight for Highlight clipping avoidance")
    # Filters
    parser.add_argument("--min-contrast", type=int, default=30, help="Min allowed contrast (default: 30 to avoid washed out / milky images)")
    parser.add_argument("--max-contrast", type=int, default=60, help="Max allowed contrast (default: 60)")
    parser.add_argument("--max-clip", type=float, default=0.03, help="Max allowed highlight clipping ratio (default: 0.03)")
    parser.add_argument("--max-shadow", type=float, default=0.20, help="Max allowed shadow clipping ratio (default: 0.20)")
    parser.add_argument("--min-lum", type=float, default=45.0, help="Min allowed mean luminance (default: 45.0)")
    parser.add_argument("--max-lum", type=float, default=95.0, help="Max allowed mean luminance (default: 95.0)")

    return parser.parse_args()


def load_csv(csv_path):
    if not os.path.exists(csv_path):
        print(f"Error: CSV file not found at {csv_path}", file=sys.stderr)
        sys.exit(1)

    rows = []
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                row_dict = {
                    "gain": int(float(r["gain"])),
                    "sharpness": int(float(r["sharpness"])),
                    "contrast": int(float(r["contrast"])),
                    "gamma": int(float(r["gamma"])),
                    "lap_var": float(r["lap_var"]),
                    "lap_var_center": float(r["lap_var_center"]),
                    "sobel_energy": float(r["sobel_energy"]),
                    "sobel_energy_center": float(r["sobel_energy_center"]),
                    "temporal_noise": float(r["temporal_noise"]),
                    "mean_lum": float(r["mean_lum"]),
                    "mean_lum_center": float(r["mean_lum_center"]),
                    "p90": float(r["p90"]),
                    "p10": float(r["p10"]),
                    "dynamic_range": float(r["dynamic_range"]),
                    "highlight_clip_pct": float(r["highlight_clip_pct"]),
                    "shadow_clip_pct": float(r["shadow_clip_pct"]),
                    "filename": r["filename"],
                }
                rows.append(row_dict)
            except (ValueError, KeyError) as e:
                continue

    return rows


def find_pareto_frontier(objectives):
    """
    Finds Pareto optimal points where HIGHER is better for all objective columns.
    objectives: np.ndarray of shape (N, D)
    Returns: boolean mask of shape (N,)
    """
    n_points = objectives.shape[0]
    is_pareto = np.ones(n_points, dtype=bool)

    # Sort primarily by the first objective descending to prune faster
    order = np.argsort(-objectives[:, 0])
    sorted_objs = objectives[order]
    sorted_pareto = np.ones(n_points, dtype=bool)

    for i in range(n_points):
        if not sorted_pareto[i]:
            continue
        curr = sorted_objs[i]
        # Any point dominated by curr is not Pareto optimal
        # point j is dominated by curr if curr >= j in all dimensions and curr > j in at least one
        # Because i comes before j in order, curr[0] >= j[0] is already guaranteed for j > i
        sub = sorted_objs[i + 1 :]
        if len(sub) == 0:
            break
        dominated = np.all(curr >= sub, axis=1) & np.any(curr > sub, axis=1)
        # Update the boolean mask in sorted_pareto
        sorted_pareto[i + 1 :][dominated] = False

    # Invert back to original indices
    is_pareto[order] = sorted_pareto
    return is_pareto


def analyze(args):
    raw_data = load_csv(args.csv)
    total_count = len(raw_data)
    if total_count == 0:
        print("No valid data rows found in CSV.")
        return

    # 1. Apply validity filters
    valid_data = []
    for d in raw_data:
        if d["contrast"] < args.min_contrast or d["contrast"] > args.max_contrast:
            continue
        if d["highlight_clip_pct"] > args.max_clip:
            continue
        if d["shadow_clip_pct"] > args.max_shadow:
            continue
        if d["mean_lum"] < args.min_lum or d["mean_lum"] > args.max_lum:
            continue
        valid_data.append(d)

    valid_count = len(valid_data)
    filtered_out = total_count - valid_count

    if valid_count == 0:
        print("All records were filtered out by threshold constraints.")
        return

    # Convert metrics to numpy arrays for vector math
    lap_center = np.array([d["lap_var_center"] for d in valid_data])
    sobel_center = np.array([d["sobel_energy_center"] for d in valid_data])
    noise = np.array([d["temporal_noise"] for d in valid_data])
    dyn_range = np.array([d["dynamic_range"] for d in valid_data])
    clip = np.array([d["highlight_clip_pct"] for d in valid_data])

    def min_max_norm(arr):
        mn, mx = np.min(arr), np.max(arr)
        if mx == mn:
            return np.zeros_like(arr)
        return (arr - mn) / (mx - mn)

    norm_lap = min_max_norm(lap_center)
    norm_sobel = min_max_norm(sobel_center)
    norm_noise_inv = 1.0 - min_max_norm(noise)
    norm_dyn = min_max_norm(dyn_range)
    norm_clip_inv = 1.0 - min_max_norm(clip)

    composite_scores = (
        args.w_lap * norm_lap
        + args.w_sobel * norm_sobel
        + args.w_noise * norm_noise_inv
        + args.w_dyn * norm_dyn
        + args.w_clip * norm_clip_inv
    )

    # 2. Compute Pareto Frontier on 4 key objectives (all higher is better):
    # - lap_var_center (sharpness)
    # - sobel_energy_center (gradient)
    # - -temporal_noise (noise suppression)
    # - dynamic_range (contrast)
    objectives = np.column_stack([
        lap_center,
        sobel_center,
        -noise,
        dyn_range,
    ])
    pareto_mask = find_pareto_frontier(objectives)
    pareto_count = int(np.sum(pareto_mask))

    for idx, d in enumerate(valid_data):
        d["score"] = float(composite_scores[idx])
        d["is_pareto"] = bool(pareto_mask[idx])

    # 3. Sort by composite score descending
    ranked_indices = np.argsort(-composite_scores)
    top_indices = ranked_indices[: args.top]

    # Parameter distribution statistics in top candidates
    top_candidates = [valid_data[i] for i in top_indices]
    gains = [d["gain"] for d in top_candidates]
    sharps = [d["sharpness"] for d in top_candidates]
    contrasts = [d["contrast"] for d in top_candidates]
    gammas = [d["gamma"] for d in top_candidates]

    lines = []
    lines.append("=" * 80)
    lines.append("                    AUTODARTS ISP SWEEP ANALYSIS REPORT")
    lines.append("=" * 80)
    lines.append(f"Total Sweep Samples Evaluated: {total_count}")
    lines.append(f"Valid Samples (within exposure & clipping bounds): {valid_count} ({100.0 * valid_count / total_count:.1f}%)")
    lines.append(f"Rejected Samples (clipped/underexposed): {filtered_out} ({100.0 * filtered_out / total_count:.1f}%)")
    lines.append(f"Pareto-Optimal Candidates: {pareto_count} ({100.0 * pareto_count / valid_count:.1f}% of valid)")
    lines.append("-" * 80)
    lines.append("SCORING WEIGHTS:")
    lines.append(f"  • Center Laplacian (Sharpness):    {args.w_lap * 100:.1f}%")
    lines.append(f"  • Center Sobel (Gradient Energy):  {args.w_sobel * 100:.1f}%")
    lines.append(f"  • Noise Suppression (absdiff low): {args.w_noise * 100:.1f}%")
    lines.append(f"  • Dynamic Range (White-Black):     {args.w_dyn * 100:.1f}%")
    lines.append(f"  • Highlight Preservation:          {args.w_clip * 100:.1f}%")
    lines.append("-" * 80)
    lines.append(f"TOP {len(top_candidates)} PARAMETER DISTRIBUTIONS:")
    lines.append(f"  • Gain:      median={int(np.median(gains))}, range=[{min(gains)}, {max(gains)}], unique={sorted(set(gains))}")
    lines.append(f"  • Sharpness: median={int(np.median(sharps))}, range=[{min(sharps)}, {max(sharps)}], unique={sorted(set(sharps))}")
    lines.append(f"  • Contrast:  median={int(np.median(contrasts))}, range=[{min(contrasts)}, {max(contrasts)}], unique={sorted(set(contrasts))}")
    lines.append(f"  • Gamma:     median={int(np.median(gammas))}, range=[{min(gammas)}, {max(gammas)}], unique={sorted(set(gammas))}")
    lines.append("=" * 80)
    lines.append(
        f"{'#':<3} {'Gain':<5} {'Sharp':<6} {'Contr':<6} {'Gamma':<6} {'Score':<7} "
        f"{'LapCtr':<8} {'SobelCtr':<9} {'Noise':<7} {'DynRng':<7} {'MeanLum':<8} {'Pareto':<7} Filename"
    )
    lines.append("-" * 80)

    for rank, idx in enumerate(top_indices, 1):
        d = valid_data[idx]
        pareto_str = "YES" if d["is_pareto"] else " no"
        lines.append(
            f"{rank:<3} {d['gain']:<5} {d['sharpness']:<6} {d['contrast']:<6} {d['gamma']:<6} "
            f"{d['score']:.4f}  {d['lap_var_center']:<8.1f} {d['sobel_energy_center']:<9.2f} "
            f"{d['temporal_noise']:<7.2f} {d['dynamic_range']:<7.0f} {d['mean_lum']:<8.1f} {pareto_str:<7} {d['filename']}"
        )
    lines.append("=" * 80)

    # Winner recommendation
    winner = valid_data[top_indices[0]]
    lines.append("\nRECOMMENDED OPTIMAL ISP CONFIGURATION:")
    lines.append(f"  --> Gain:       {winner['gain']}")
    lines.append(f"  --> Sharpness:  {winner['sharpness']}")
    lines.append(f"  --> Contrast:   {winner['contrast']}")
    lines.append(f"  --> Gamma:      {winner['gamma']}")
    lines.append(f"  --> Score:      {winner['score']:.4f}")
    lines.append(f"  --> LapVar:     {winner['lap_var_center']:.2f}")
    lines.append(f"  --> Sobel:      {winner['sobel_energy_center']:.2f}")
    lines.append(f"  --> Noise:      {winner['temporal_noise']:.2f} DN")
    lines.append(f"  --> DynRange:   {winner['dynamic_range']:.1f}")
    lines.append(f"  --> Image:      {winner['filename']}")
    lines.append("=" * 80)

    report_text = "\n".join(lines)
    print(report_text)

    # Ensure output directory exists and write report
    out_dir = os.path.dirname(args.output_report)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output_report, "w", encoding="utf-8") as f:
        f.write(report_text + "\n")
    print(f"\nReport written to: {args.output_report}")


def main():
    args = parse_args()
    analyze(args)


if __name__ == "__main__":
    main()
