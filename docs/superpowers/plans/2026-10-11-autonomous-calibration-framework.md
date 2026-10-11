# Autonomous Multi-Agent Camera Calibration Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an autonomous, closed-loop multi-agent calibration testbench that optimizes OV2710 camera parameters directly on the remote hardware (`192.168.2.103`) against reverse-engineered Autodarts computer vision benchmarks.

**Architecture:** A three-agent architecture comprising Builder (`core/optimizer.py` / `core/metrics.py`), Vision Critic (`core/vision_critic.py`), and Recognition Arbiter (`core/recognition_arbiter.py`), coordinated by an autonomous testbench loop (`tools/agentic_autocalib_loop.py`) that sweeps remote cameras over SSH, inspects image artifacts, and pauses at candidate milestones.

**Tech Stack:** Python 3.10+, OpenCV 4.x (`cv2`), NumPy, Libadwaita/GTK4, V4L2 (`v4l2-ctl`), SSH (`sshpass`).

**Spec:** [docs/superpowers/specs/2026-10-11-autonomous-calibration-framework-design.md](file:///home/iterathor/SUIT-Fedora/docs/superpowers/specs/2026-10-11-autonomous-calibration-framework-design.md)

## Global Constraints

- **Physical Darts Invariant:** The 3 physical darts remain in Sector 20 throughout all tests; no manual dart removal is required.
- **Decompiled Autodarts Compliance:** Evaluates against `FUN_002fcfe0` (motion binarization), `FUN_01173410` (Hough line convergence), and `HoughLinesP` shaft extraction from `/home/iterathor/ghidra/AUTODARTS_ARCHITECTURE.md`.
- **Remote Synchronization:** Any code pushed to remote must sync to both `/home/autodarts/SUIT-Fedora/` and `/home/autodarts/Downloads/SUIT-Fedora/` on `192.168.2.103` and compile cleanly with `py_compile`.
- **Human UI Design Rules:** Strict no-emoji policy; concise and human-friendly copy; clear pause/resume prompts.

---

### Task 1: Vision Critic Module (`core/vision_critic.py`)

**Files:**
- Create: `core/vision_critic.py`
- Test: `tests/test_vision_critic.py`

**Interfaces:**
- Produces:
  - `VisionCritic.extract_hough_shaft_lines(frame: np.ndarray, roi: BoardROI) -> List[Tuple[int, int, int, int]]`
  - `VisionCritic.calculate_specular_blooming(frame: np.ndarray, roi: BoardROI) -> float`
  - `VisionCritic.calculate_quantile_tone_match_diff(cand_frame: np.ndarray, base_frame: np.ndarray, roi: BoardROI) -> Tuple[np.ndarray, float]`
  - `VisionCritic.evaluate_frame_quality(cand_frame: np.ndarray, base_frame: np.ndarray, roi: BoardROI) -> CriticMetrics`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vision_critic.py
import unittest
import numpy as np
import cv2
from core.board_detector import BoardDetector
from core.vision_critic import VisionCritic, CriticMetrics


class TestVisionCritic(unittest.TestCase):
    def setUp(self):
        self.detector = BoardDetector()
        self.critic = VisionCritic()

        # Create a synthetic dartboard image (1000x1000)
        self.base_frame = np.full((1000, 1000, 3), 160, dtype=np.uint8)
        cv2.circle(self.base_frame, (500, 500), 350, (230, 230, 230), -1)
        self.roi = self.detector.detect_board(self.base_frame)

        # Dart frame with inserted darts
        self.dart_frame = self.base_frame.copy()
        # Draw dart 1 (T20 shaft)
        cv2.line(self.dart_frame, (500, 320), (500, 420), (20, 20, 20), 4)
        # Draw dart 2 (S20 outer shaft)
        cv2.line(self.dart_frame, (500, 200), (500, 280), (20, 20, 20), 4)

    def test_extract_hough_shaft_lines(self):
        lines = self.critic.extract_hough_shaft_lines(self.dart_frame, self.roi)
        self.assertGreater(len(lines), 0)

    def test_specular_blooming_detection(self):
        # Clean frame without specular blowout
        clean_bloom = self.critic.calculate_specular_blooming(self.dart_frame, self.roi)
        self.assertEqual(clean_bloom, 0.0)

        # Bloomed frame with saturated metallic glints
        bloomed = self.dart_frame.copy()
        cv2.rectangle(bloomed, (495, 300), (505, 350), (255, 255, 255), -1)
        bloomed_score = self.critic.calculate_specular_blooming(bloomed, self.roi)
        self.assertGreater(bloomed_score, 0.0)

    def test_quantile_tone_match_diff(self):
        # Simulate candidate frame with higher gamma (lifted midtones)
        gamma_cand = np.clip(np.power(self.base_frame.astype(np.float32) / 255.0, 0.7) * 255.0, 0, 255).astype(np.uint8)
        _, raw_diff = self.critic.calculate_quantile_tone_match_diff(gamma_cand, self.base_frame, self.roi, apply_matching=False)
        _, matched_diff = self.critic.calculate_quantile_tone_match_diff(gamma_cand, self.base_frame, self.roi, apply_matching=True)
        self.assertLess(matched_diff, raw_diff)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_vision_critic.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.vision_critic'`

- [ ] **Step 3: Implement `core/vision_critic.py`**

```python
"""Vision Critic: Empirical image analysis based on decompiled Autodarts pipeline."""

from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple, Optional
import cv2
import numpy as np

from core.board_detector import BoardROI


@dataclass
class CriticMetrics:
    hough_line_count: int
    longest_shaft_len: float
    specular_clip_ratio: float
    halo_overshoot: float
    sisal_difference_floor: float
    needle_contrast_peak: float


class VisionCritic:
    """Evaluates image bursts using Hough line extraction and specular analysis."""

    def extract_hough_shaft_lines(
        self,
        frame: np.ndarray,
        roi: BoardROI,
        min_line_len: int = 40,
        max_line_gap: int = 15,
    ) -> List[Tuple[int, int, int, int]]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        if roi and roi.board_mask is not None and roi.board_mask.shape == gray.shape:
            masked = cv2.bitwise_and(gray, gray, mask=roi.board_mask)
        else:
            masked = gray

        edges = cv2.Canny(masked, 50, 150, apertureSize=3)
        raw_lines = cv2.HoughLinesP(
            edges,
            rho=1,
            theta=np.pi / 180,
            threshold=40,
            minLineLength=min_line_len,
            maxLineGap=max_line_gap,
        )
        if raw_lines is None:
            return []
        return [tuple(line[0]) for line in raw_lines]

    def calculate_specular_blooming(self, frame: np.ndarray, roi: BoardROI) -> float:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        if roi and roi.board_mask is not None and roi.board_mask.shape == gray.shape:
            board_px = gray[roi.board_mask > 0]
        else:
            board_px = gray.ravel()

        if len(board_px) == 0:
            return 0.0
        return float(np.sum(board_px >= 253)) / float(len(board_px))

    def calculate_quantile_tone_match_diff(
        self,
        cand_frame: np.ndarray,
        base_frame: np.ndarray,
        roi: BoardROI,
        apply_matching: bool = True,
    ) -> Tuple[np.ndarray, float]:
        gray_c = cv2.cvtColor(cand_frame, cv2.COLOR_BGR2GRAY) if len(cand_frame.shape) == 3 else cand_frame
        gray_b = cv2.cvtColor(base_frame, cv2.COLOR_BGR2GRAY) if len(base_frame.shape) == 3 else base_frame

        if roi and roi.board_mask is not None and roi.board_mask.shape == gray_c.shape:
            mask = roi.board_mask > 0
        else:
            mask = np.ones_like(gray_c, dtype=bool)

        if apply_matching:
            b_vals = gray_b[mask]
            c_vals = gray_c[mask]
            if len(b_vals) > 100 and len(c_vals) > 100:
                b_quantiles = np.percentile(b_vals, np.linspace(0, 100, 256))
                c_quantiles = np.percentile(c_vals, np.linspace(0, 100, 256))
                lut = np.interp(np.arange(256), b_quantiles, c_quantiles).astype(np.uint8)
                matched_b = cv2.LUT(gray_b, lut)
            else:
                matched_b = gray_b
        else:
            matched_b = gray_b

        diff = cv2.absdiff(gray_c, matched_b)
        board_diff = diff[mask]
        mean_diff = float(np.mean(board_diff)) if len(board_diff) > 0 else 0.0
        return diff, round(mean_diff, 2)

    def evaluate_frame_quality(
        self,
        cand_frame: np.ndarray,
        base_frame: Optional[np.ndarray],
        roi: BoardROI,
    ) -> CriticMetrics:
        lines = self.extract_hough_shaft_lines(cand_frame, roi)
        longest = 0.0
        for x1, y1, x2, y2 in lines:
            length = float(np.hypot(x2 - x1, y2 - y1))
            if length > longest:
                longest = length

        bloom = self.calculate_specular_blooming(cand_frame, roi)

        if base_frame is not None:
            diff, floor = self.calculate_quantile_tone_match_diff(cand_frame, base_frame, roi, apply_matching=True)
            peak = float(np.percentile(diff[roi.board_mask > 0], 98)) if roi.board_mask is not None else float(np.percentile(diff, 98))
        else:
            floor = 0.0
            peak = 0.0

        return CriticMetrics(
            hough_line_count=len(lines),
            longest_shaft_len=round(longest, 1),
            specular_clip_ratio=round(bloom, 4),
            halo_overshoot=0.0,
            sisal_difference_floor=floor,
            needle_contrast_peak=round(peak, 1),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_vision_critic.py`
Expected: `Ran 3 tests in ... OK`

- [ ] **Step 5: Commit**

```bash
git add core/vision_critic.py tests/test_vision_critic.py
git commit -m "feat(critic): add VisionCritic for Hough shaft extraction and tone matching"
```

---

### Task 2: Recognition Arbiter Module (`core/recognition_arbiter.py`)

**Files:**
- Create: `core/recognition_arbiter.py`
- Test: `tests/test_recognition_arbiter.py`

**Interfaces:**
- Consumes: `CriticMetrics` from `core/vision_critic.py`
- Produces:
  - `RecognitionArbiter.evaluate_verdict(cam_metrics_map: Dict[str, CriticMetrics], baseline_map: Dict[str, CriticMetrics]) -> ArbiterVerdict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recognition_arbiter.py
import unittest
from core.vision_critic import CriticMetrics
from core.recognition_arbiter import RecognitionArbiter, ArbiterVerdict


class TestRecognitionArbiter(unittest.TestCase):
    def setUp(self):
        self.arbiter = RecognitionArbiter()

        self.baseline_map = {
            "/dev/video0": CriticMetrics(hough_line_count=2, longest_shaft_len=120.0, specular_clip_ratio=0.05, halo_overshoot=0.01, sisal_difference_floor=5.0, needle_contrast_peak=140.0),
            "/dev/video2": CriticMetrics(hough_line_count=3, longest_shaft_len=180.0, specular_clip_ratio=0.04, halo_overshoot=0.01, sisal_difference_floor=4.5, needle_contrast_peak=180.0),
            "/dev/video4": CriticMetrics(hough_line_count=1, longest_shaft_len=80.0, specular_clip_ratio=0.03, halo_overshoot=0.01, sisal_difference_floor=6.0, needle_contrast_peak=100.0),
        }

    def test_approve_improved_candidate(self):
        candidate_map = {
            "/dev/video0": CriticMetrics(hough_line_count=3, longest_shaft_len=150.0, specular_clip_ratio=0.0, halo_overshoot=0.005, sisal_difference_floor=3.2, needle_contrast_peak=190.0),
            "/dev/video2": CriticMetrics(hough_line_count=3, longest_shaft_len=195.0, specular_clip_ratio=0.0, halo_overshoot=0.004, sisal_difference_floor=3.0, needle_contrast_peak=220.0),
            "/dev/video4": CriticMetrics(hough_line_count=2, longest_shaft_len=95.0, specular_clip_ratio=0.0, halo_overshoot=0.006, sisal_difference_floor=4.0, needle_contrast_peak=130.0),
        }
        verdict = self.arbiter.evaluate_verdict(candidate_map, self.baseline_map)
        self.assertTrue(verdict.approved)

    def test_reject_regressed_candidate(self):
        # Candidate with blown highlights and high sisal floor
        regressed_map = {
            "/dev/video0": CriticMetrics(hough_line_count=0, longest_shaft_len=0.0, specular_clip_ratio=0.08, halo_overshoot=0.05, sisal_difference_floor=18.0, needle_contrast_peak=80.0),
            "/dev/video2": CriticMetrics(hough_line_count=1, longest_shaft_len=50.0, specular_clip_ratio=0.07, halo_overshoot=0.04, sisal_difference_floor=15.0, needle_contrast_peak=90.0),
            "/dev/video4": CriticMetrics(hough_line_count=0, longest_shaft_len=0.0, specular_clip_ratio=0.09, halo_overshoot=0.06, sisal_difference_floor=20.0, needle_contrast_peak=60.0),
        }
        verdict = self.arbiter.evaluate_verdict(regressed_map, self.baseline_map)
        self.assertFalse(verdict.approved)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_recognition_arbiter.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.recognition_arbiter'`

- [ ] **Step 3: Implement `core/recognition_arbiter.py`**

```python
"""Recognition Arbiter: Validates camera tuning against decompiled Autodarts criteria."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List
from core.vision_critic import CriticMetrics


@dataclass
class ArbiterVerdict:
    approved: bool
    composite_gain: float
    reasons: List[str]


class RecognitionArbiter:
    """Evaluates multi-camera candidate metrics against Autodarts recognition gates."""

    def evaluate_verdict(
        self,
        candidate_map: Dict[str, CriticMetrics],
        baseline_map: Dict[str, CriticMetrics],
    ) -> ArbiterVerdict:
        reasons = []
        approved_cams = 0
        total_cams = len(candidate_map)

        for dev, cand in candidate_map.items():
            base = baseline_map.get(dev)
            if not base:
                continue

            # Gate 1: Highlight blowout must not increase
            if cand.specular_clip_ratio > base.specular_clip_ratio + 0.005:
                reasons.append(f"{dev}: Specular clipping increased ({cand.specular_clip_ratio*100:.1f}%)")
                continue

            # Gate 2: Sisal floor noise must stay low (< 10 DN)
            if cand.sisal_difference_floor > 10.0 and cand.sisal_difference_floor > base.sisal_difference_floor:
                reasons.append(f"{dev}: Sisal floor noise high ({cand.sisal_difference_floor:.1f} DN)")
                continue

            # Gate 3: Needle contrast peak or Hough line count should improve
            contrast_improved = cand.needle_contrast_peak >= base.needle_contrast_peak
            hough_improved = cand.hough_line_count >= base.hough_line_count

            if contrast_improved or hough_improved:
                approved_cams += 1
            else:
                reasons.append(f"{dev}: Neither Hough lines nor needle contrast improved")

        # Require majority approval across cameras (at least 2 of 3 cameras)
        is_approved = approved_cams >= max(1, total_cams * 2 // 3)
        gain = (approved_cams / max(1, total_cams)) * 100.0

        return ArbiterVerdict(
            approved=is_approved,
            composite_gain=round(gain, 1),
            reasons=reasons,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_recognition_arbiter.py`
Expected: `Ran 2 tests in ... OK`

- [ ] **Step 5: Commit**

```bash
git add core/recognition_arbiter.py tests/test_recognition_arbiter.py
git commit -m "feat(arbiter): add RecognitionArbiter for Autodarts multi-camera validation"
```

---

### Task 3: Autonomous Testbench Orchestrator (`tools/agentic_autocalib_loop.py`)

**Files:**
- Create: `tools/agentic_autocalib_loop.py`
- Test: `tests/test_autocalib_loop.py`

**Interfaces:**
- Consumes: `VisionCritic` (`core/vision_critic.py`), `RecognitionArbiter` (`core/recognition_arbiter.py`)
- Produces: `tools/autocalib_ledger.json`

- [ ] **Step 1: Write unit tests for orchestrator state and ledger**

```python
# tests/test_autocalib_loop.py
import unittest
import os
import tempfile
from tools.agentic_autocalib_loop import CalibrationLedger, LedgerEntry


class TestCalibrationLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.ledger = CalibrationLedger(self.tmp.name)

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def test_record_and_load_entry(self):
        entry = LedgerEntry(
            iteration=1,
            hypothesis="Test gamma 460 with quantile tone matching",
            parameters={"exposure": 300, "gain": 40, "contrast": 30, "sharpness": 80, "gamma": 460},
            approved=True,
            composite_gain=85.0,
            notes="Zero highlight clipping, clean Hough lines",
        )
        self.ledger.record(entry)
        entries = self.ledger.load_all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].iteration, 1)
        self.assertTrue(entries[0].approved)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_autocalib_loop.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools.agentic_autocalib_loop'`

- [ ] **Step 3: Implement `tools/agentic_autocalib_loop.py`**

```python
"""Autonomous Multi-Agent Calibration Loop Orchestrator."""

from __future__ import annotations
import json
import os
import subprocess
import time
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional

from core.vision_critic import VisionCritic
from core.recognition_arbiter import RecognitionArbiter


@dataclass
class LedgerEntry:
    iteration: int
    hypothesis: str
    parameters: Dict[str, Any]
    approved: bool
    composite_gain: float
    notes: str


class CalibrationLedger:
    def __init__(self, ledger_path: str = "tools/autocalib_ledger.json"):
        self.ledger_path = ledger_path

    def record(self, entry: LedgerEntry):
        entries = self.load_all()
        entries.append(entry)
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
    def __init__(
        self,
        remote_host: str = "192.168.2.103",
        user: str = "autodarts",
        password: str = "autodarts",
    ):
        self.remote_host = remote_host
        self.user = user
        self.password = password
        self.critic = VisionCritic()
        self.arbiter = RecognitionArbiter()
        self.ledger = CalibrationLedger()

    def run_remote_command(self, cmd: str) -> str:
        full_cmd = f"sshpass -p {self.password} ssh -o StrictHostKeyChecking=no {self.user}@{self.remote_host} \"{cmd}\""
        res = subprocess.run(full_cmd, shell=True, capture_output=True, text=True)
        return res.stdout.strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_autocalib_loop.py`
Expected: `Ran 1 test in ... OK`

- [ ] **Step 5: Commit**

```bash
git add tools/agentic_autocalib_loop.py tests/test_autocalib_loop.py
git commit -m "feat(orchestrator): add AutoCalibOrchestrator and CalibrationLedger"
```

---

### Task 4: Integrate Quantile Tone-Matching into Core CAM-librator (`core/metrics.py`, `core/optimizer.py`)

**Files:**
- Modify: `core/metrics.py:255-295`
- Modify: `core/optimizer.py:330-360`
- Test: `tests/test_metrics.py`, `tests/test_optimizer.py`

- [ ] **Step 1: Write test for quantile tone matching in `tests/test_metrics.py`**

Verify that cross-gamma evaluations use quantile LUT matching and maintain low sisal glow noise floor:
```python
    def test_cross_gamma_sisal_glow_remains_low(self):
        roi = self.detector.detect_board(self.frame)
        gamma_lifted = np.clip(np.power(self.frame.astype(np.float32) / 255.0, 0.75) * 255.0, 0, 255).astype(np.uint8)
        _, glow, _ = self.evaluator.calculate_dart_difference_metrics(gamma_lifted, self.frame, roi)
        self.assertLess(glow, 6.0)
```

- [ ] **Step 2: Run test to verify current behavior**

Run: `python3 -m unittest tests/test_metrics.py`

- [ ] **Step 3: Update `core/metrics.py`**

Replace linear scalar scaling with 256-element quantile LUT tone curve matching.

- [ ] **Step 4: Verify test passes**

Run: `python3 -m unittest tests/test_metrics.py tests/test_optimizer.py`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add core/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): add 256-element quantile tone-curve matching for cross-gamma invariance"
```

---

### Task 5: Execute End-to-End Autonomous Calibration Loop on Remote Device

**Files:**
- Sync: `core/`, `modules_gtk/`, `tools/` to `192.168.2.103`
- Output: `tools/autocalib_ledger.json`

- [ ] **Step 1: Sync updated codebase to remote device**

```bash
sshpass -p autodarts rsync -avz --exclude '.git' core modules_gtk tests tools style_gtk.css autodarts@192.168.2.103:/home/autodarts/SUIT-Fedora/
sshpass -p autodarts rsync -avz --exclude '.git' core modules_gtk tests tools style_gtk.css autodarts@192.168.2.103:/home/autodarts/Downloads/SUIT-Fedora/
```

- [ ] **Step 2: Verify remote syntax compilation**

```bash
sshpass -p autodarts ssh autodarts@192.168.2.103 "python3 -m py_compile /home/autodarts/Downloads/SUIT-Fedora/core/*.py /home/autodarts/Downloads/SUIT-Fedora/tools/*.py"
```

- [ ] **Step 3: Launch live calibration iteration across Cam 0, 2, 4**

Execute the orchestrator loop, evaluate Hough line continuity on the physical darts in Sector 20, check for zero highlight blooming, and record results in `autocalib_ledger.json`.

- [ ] **Step 4: Trigger Pause Checkpoint**

Present the leaderboard, metric deltas, and image artifacts to the user for sign-off or continued iteration.
