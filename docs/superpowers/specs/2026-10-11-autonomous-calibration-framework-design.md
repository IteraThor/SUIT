# Autonomous Multi-Agent Camera Calibration Framework Design Specification

**Date:** 2026-10-11  
**Status:** Approved  
**Author:** Pair programming session (IteraThor & Antigravity)  

---

## 1. Overview & Goals

The goal of this project is to build an autonomous, multi-agent evaluation and parameter-tuning testbench that continuously runs experiments on the physical OV2710 cameras connected to the remote device (`192.168.2.103`).

Rather than relying purely on abstract mathematical heuristics or human-subjective aesthetics, the agents optimize camera sensor settings directly against the computer vision requirements discovered from reverse-engineering the Autodarts binary (`/home/iterathor/ghidra/AUTODARTS_ARCHITECTURE.md`):

1. **Motion Binarization Noise Floor (`FUN_002fcfe0`)**: Background noise on the sisal must remain strictly below `motion.threshold` so illumination changes do not trigger false hand or bounce events.
2. **Shaft Line Extraction (`cv::HoughLinesP`)**: Inserted darts must produce continuous, high-gradient shaft lines with zero specular blooming on metallic barrels and zero double-line edge ringing.
3. **Triangulation Ray Convergence (`FUN_01173410`)**: The 3 ray vectors from the 120°-offset cameras must reliably intersect on the dartboard face ($Z=0$) with sub-millimeter precision.
4. **Autonomous Iteration with Pause Checkpoints**: The loop will run continuously, testing hypotheses, modifying code, pulling real images, and evaluating results. Whenever a winning milestone is achieved, the loop pauses and presents the findings and visual artifacts to the user, allowing them to either adopt the configuration or continue refining.

---

## 2. Physical Setup & Operational Constraints

1. **Permanent Physical Darts**: Three darts are physically inserted in Sector 20 (Triple 20, upper Single 20, and lower Single 20). The testbench operates with darts in the board and does not require manual dart removal.
2. **Cross-Angle Geometry (120° Offset)**:
   * **Cam 0 (`/dev/video0`)**: Positioned at ~2 o'clock (oblique side view of Sector 20).
   * **Cam 2 (`/dev/video2`)**: Positioned at ~10 o'clock (oblique side view of Sector 20).
   * **Cam 4 (`/dev/video4`)**: Positioned at 12 o'clock (axial view directly down the dart shaft/flights).
3. **Tone-Matched Difference Analysis**: Because the darts remain inserted, background sisal difference is evaluated using quantile histogram matching against the reference dataset, isolating needle tip contrast without non-linear gamma distortion.

---

## 3. Multi-Agent Architecture & Specialized Roles

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Autonomous Agentic Testbench                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌───────────────────────┐             ┌─────────────────────────────────┐  │
│  │  Agent 1: Builder     │             │   Agent 2: Vision Critic        │  │
│  │  (ISP & Optimization) │ ──────────> │   (Hough, Sobel, Image Review)  │  │
│  └───────────┬───────────┘             └────────────────┬────────────────┘  │
│              │                                          │                   │
│              │ Push & Execute Sweep                     │ Inspect Images    │
│              ▼                                          ▼                   │
│    Remote Device (192.168.2.103)               Analysis & Artifacts         │
│    3x Cameras (Darts in Board)                        │                     │
│              ▲                                        │                     │
│              │                                        ▼                     │
│              │                          ┌───────────────────────────────┐   │
│              └───────────────────────── │   Agent 3: Recognition Arbiter│   │
│                 Adopt Winning Code      │   (Autodarts Decompiled Gate) │   │
│                                         └───────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Agent 1: Code & Parameter Engineer ("The Builder")
* **Responsibility**: Explores the ISP parameter search space (Gain, Shutter, Contrast, Sharpness, Gamma) and maintains `core/optimizer.py` and `core/metrics.py`.
* **Actions**:
  1. Modifies optimization search bounds and candidate resolution grids.
  2. Syncs code to `/home/autodarts/Downloads/SUIT-Fedora` and `/home/autodarts/SUIT-Fedora` on `192.168.2.103`.
  3. Triggers multi-camera sweeps and captures frame bursts.

### Agent 2: Vision Critic ("The Eyes")
* **Responsibility**: Direct empirical inspection of actual camera frames and image crops.
* **Actions**:
  1. Downloads candidate frame bursts from `/home/autodarts/.local/share/suit/calibration_sessions/`.
  2. Computes **Hough Line Continuity**: Runs `cv2.HoughLinesP` to test whether the shaft produces a clean line vector without fragmentation.
  3. Computes **Barrel Specular Clipping**: Measures pixel count where $I \ge 253$ on the metallic darts.
  4. Computes **Halo Overshoot**: Measures Sobel edge ringing around wires and needle silhouettes.
  5. Computes **Sisal Difference Floor**: Measures residual thermal noise on the background sisal.

### Agent 3: Recognition Arbiter ("The Judge")
* **Responsibility**: Validates progress against the decompiled Autodarts binary requirements.
* **Actions**:
  1. Rejects "cheating" metrics (e.g. over-sharpening that looks visually punchy but creates double Hough lines).
  2. Scores multi-camera balance: ensures improvements on Cam 0 and Cam 2 do not degrade Cam 4.
  3. Decides whether an experiment passes, updates `autocalib_ledger.json`, and triggers the pause checkpoint.

---

## 4. Execution Workflow & Iteration Loop

The autonomous loop executes in `tools/agentic_autocalib_loop.py`:

```
┌─────────────────────────────────────────────────────────┐
│ 1. Builder Formulates Hypothesis                        │
│    (e.g., test gamma grid 420-460 with quantile LUT)    │
└────────────────────────────┬────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│ 2. Remote Hardware Execution                            │
│    - Sync code to remote device                         │
│    - Execute calibration sweep across Cam 0, 2, 4       │
│    - Export high-res frame bursts                       │
└────────────────────────────┬────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│ 3. Vision Critic Direct Image Analysis                  │
│    - Download burst images & crops                      │
│    - Extract Hough lines from dart shafts               │
│    - Measure specular blowout & halo ringing            │
└────────────────────────────┬────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│ 4. Recognition Arbiter Gate                             │
│    - Compare against previous best baseline             │
│    - If strictly improved: Record in ledger & commit    │
│    - If regressed: Revert hypothesis & log findings     │
└────────────────────────────┬────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│ 5. Pause Checkpoint                                     │
│    - Present leaderboard & image inspection to user     │
│    - User Options:                                      │
│      [A] Adopt current winner & finalize                │
│      [B] Continue iterating for higher precision        │
└─────────────────────────────────────────────────────────┘
```

---

## 5. Artifacts & Logging

1. **`tools/autocalib_ledger.json`**: Chronological log of every iteration (hypothesis, parameters tested, Hough metrics, clipping ratio, verdict).
2. **`brain/.../autocalib_artifacts/`**: High-resolution before/after comparisons, difference heatmaps, and Hough line visualizations for user inspection at each pause checkpoint.

---

## 6. Verification & Safety

* **Hardware Safety**: Hardware controls (`exposure_time_absolute`, `gain`, `contrast`, `sharpness`, `gamma`) remain within V4L2 device safe bounds ($1 \le \text{gain} \le 128$, $100 \le \text{gamma} \le 500$).
* **Rollback Protection**: Before each hypothesis, the last known good configuration is snapshotted. If a run fails or crashes, state is restored immediately.
* **Remote Sync Integrity**: Changes are always synced to both remote directories (`/home/autodarts/SUIT-Fedora` and `/home/autodarts/Downloads/SUIT-Fedora`) and verified with `python3 -m py_compile`.
