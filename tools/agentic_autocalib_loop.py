#!/usr/bin/env python3
"""Autonomous Multi-Agent Calibration Loop Orchestrator.

Coordinates Builder (ISP optimizer), Vision Critic (Hough/blooming analyzer),
and Recognition Arbiter (Autodarts binary gate) across all 3 cameras.
"""

from __future__ import annotations
import glob
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

from core.v4l2_device import V4L2Device
from core.optimizer import CameraOptimizer, OptimizationResult
from core.vision_critic import VisionCritic, CriticMetrics
from core.recognition_arbiter import RecognitionArbiter, ArbiterVerdict
from core.session_exporter import SessionExporter


@dataclass
class LedgerEntry:
    iteration: int
    hypothesis: str
    approved: bool
    composite_gain: float
    notes: str
    timestamp: str = ""
    parameters: Optional[Dict[str, Any]] = None
    camera_results: Optional[Dict[str, Any]] = None


class CalibrationLedger:
    def __init__(self, ledger_path: str = "tools/autocalib_ledger.json"):
        self.ledger_path = ledger_path

    def record(self, entry: LedgerEntry):
        entries = self.load_all()
        entries.append(entry)
        os.makedirs(os.path.dirname(os.path.abspath(self.ledger_path)), exist_ok=True)
        with open(self.ledger_path, "w") as f:
            json.dump([asdict(e) for e in entries], f, indent=2)

    def load_all(self) -> List[LedgerEntry]:
        if not os.path.exists(self.ledger_path):
            return []
        try:
            with open(self.ledger_path) as f:
                data = json.load(f)
                return [LedgerEntry(**d) for d in data]
        except Exception:
            return []


class AutoCalibOrchestrator:
    """Orchestrates multi-agent calibration sweep across all cameras."""

    def __init__(self, baseline_session_dir: Optional[str] = None):
        self.critic = VisionCritic()
        self.arbiter = RecognitionArbiter()
        self.ledger = CalibrationLedger()
        self.baseline_session_dir = baseline_session_dir or self._find_empty_baseline_dir()

    def _find_empty_baseline_dir(self) -> str:
        # Default to session_20261011_000123 where board was verified empty
        candidates = [
            "/home/autodarts/.local/share/suit/calibration_sessions/session_20261011_000123",
            "/home/autodarts/.local/share/suit/calibration_sessions/session_20261011_001651",
        ]
        for c in candidates:
            if os.path.isdir(c):
                return c
        return ""

    def run_iteration(
        self,
        iteration_num: int,
        hypothesis: str,
        precision_mode: str = "standard",
        cam_nodes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        cam_nodes = cam_nodes or ["/dev/video0", "/dev/video2", "/dev/video4"]
        print(f"\n=======================================================")
        print(f"=== AUTOCALIB ITERATION {iteration_num}: {hypothesis}")
        print(f"=== Cameras: {cam_nodes}")
        print(f"=== Empty Baseline Source: {self.baseline_session_dir}")
        print(f"=======================================================\n")

        # Map camera dev nodes to empty baseline frames
        node_map = {
            "/dev/video0": "cam0",
            "/dev/video2": "cam2",
            "/dev/video4": "cam4",
        }

        results: Dict[str, OptimizationResult] = {}
        critic_metrics_map: Dict[str, CriticMetrics] = {}
        baseline_critic_map: Dict[str, CriticMetrics] = {}

        for dev_node in cam_nodes:
            print(f"\n>>> Running Builder on {dev_node}...")
            cam_name = node_map.get(dev_node, "cam0")
            base_img_path = os.path.join(self.baseline_session_dir, f"{cam_name}_before.png")
            empty_ref = cv2.imread(base_img_path) if os.path.exists(base_img_path) else None

            vdev = V4L2Device(dev_node)
            optimizer = CameraOptimizer(vdev)

            def log_progress(stage: str, pct: float, details: str, frame=None):
                if int(pct * 100) % 20 == 0:
                    print(f"  [{stage} {pct*100:.0f}%] {details}")

            res = optimizer.run_optimization(
                precision_mode=precision_mode,
                progress_cb=log_progress,
                empty_baseline=empty_ref,
            )

            if res is not None:
                results[dev_node] = res
                # Agent 2 (Vision Critic): Evaluate optimized vs baseline
                print(f"  [Critic] Evaluating image optics on {dev_node}...")
                c_opt = self.critic.evaluate_frame_quality(res.optimized_frame, empty_ref, res.roi)
                c_base = self.critic.evaluate_frame_quality(res.initial_frame, empty_ref, res.roi)
                critic_metrics_map[dev_node] = c_opt
                baseline_critic_map[dev_node] = c_base
                print(f"  [Critic Result] Hough lines: {c_opt.hough_line_count}, Shaft len: {c_opt.longest_shaft_len:.0f}px, Bloom: {c_opt.specular_clip_ratio*100:.2f}%, Sisal floor: {c_opt.sisal_difference_floor:.1f} DN")

        # Export session artifacts
        exporter = SessionExporter()
        session_path = exporter.export_session(results)
        print(f"\n[Artifacts] Session exported to {session_path}")

        # Agent 3 (Arbiter): Judge candidate vs baseline
        print("\n>>> Running Recognition Arbiter...")
        verdict = self.arbiter.evaluate_verdict(critic_metrics_map, baseline_critic_map)
        print(f"[Arbiter Verdict] Approved: {verdict.approved} (Gain: {verdict.composite_gain}%)")
        for r in verdict.reasons:
            print(f"  - {r}")

        # Record in ledger
        cam_summary = {}
        for dev, res in results.items():
            opt_s = res.optimized_settings
            cam_summary[dev] = {
                "exposure": opt_s.exposure_absolute,
                "gain": opt_s.gain,
                "contrast": opt_s.contrast,
                "sharpness": opt_s.sharpness,
                "gamma": opt_s.gamma,
                "score": res.optimized_metrics.composite_score,
                "improvement": res.improvement_percentage,
            }

        entry = LedgerEntry(
            iteration=iteration_num,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            hypothesis=hypothesis,
            camera_results=cam_summary,
            approved=verdict.approved,
            composite_gain=verdict.composite_gain,
            notes="; ".join(verdict.reasons) if verdict.reasons else "Clean multi-camera pass",
        )
        self.ledger.record(entry)

        return {
            "session_path": session_path,
            "verdict": asdict(verdict),
            "camera_results": cam_summary,
        }


if __name__ == "__main__":
    orchestrator = AutoCalibOrchestrator()
    out = orchestrator.run_iteration(
        iteration_num=1,
        hypothesis="Test quantile tone matching with dart shaft Hough extraction",
        precision_mode="standard",
    )
    print("\nIteration 1 complete:", json.dumps(out, indent=2))
