# Autodarts Hardware & Camera Setup Optimization Guide

This guide details the optimal physical geometry, mounting positions, and camera sensor configurations for maximum precision in Autodarts, specifically tailored for **100° undistorted (low-distortion) lenses**.

---

## 1. Optimal Mounting Dimensions & Distance

For a standard 16:9 camera sensor with a 100° Diagonal Field-of-View (DFOV):

| Measurement | Optimal Value | Description |
| :--- | :--- | :--- |
| **Mounting Radius (r)** | **32 cm to 35 cm** | Horizontal distance from the Bullseye center out to the camera mount arm. |
| **Mounting Height (h)** | **26 cm to 28 cm** | Vertical height of the camera lens above the dartboard face plane. |
| **Line-of-Sight Distance (D)** | **42 cm to 45 cm** | Direct straight-line distance from the camera lens to the Bullseye center. |

```
                       [Camera]
                          │
                  Height: │ 26 – 28 cm
                          │
                          ▼
 ───────────────────► [ 32 – 35 cm ] ◄───────────────────
 Board Plane Radius (from Bullseye to camera mount arm)
```

### Why this provides maximum accuracy:
- At **720p resolution (1280x720)**, the active 340 mm dartboard spider spans **~820 pixels** across the sensor frame.
- **Pixel Density:** **~2.4 pixels per millimeter**.
- The narrow **8 mm Triple ring** occupies **~19.2 pixels**, giving the Hough line detector plenty of continuous edge pixels to interpolate the dart tip position with sub-millimeter precision.

---

## 2. Optimal Elevation Angle (38° to 42°)

The elevation angle is the angle the camera looks down toward the board face.

```
     Elevation: ~40°
       [Camera]
          \
           \  Apparent Line: ~180-250 px (Ideal for Hough Line Detector)
            \  ─────────────────────────
             \
              ▼  Dart Shaft: 140 mm
        ══════════════════════════════════ (Dartboard Surface)
```

### Why 38° to 42° is optimal:
- **At ~40° elevation:** A standard 140 mm dart appears as a distinct **180 to 250 pixel line** on the sensor.
- **If too shallow (< 30°):** Darts already in the board create wide blind spots and physically block the camera from seeing the point of subsequent darts.
- **If too steep (> 55°):** The dart shaft appears foreshortened (looks like a small point instead of a line). The line detector fails because line length drops below the internal detection thresholds (`min_contour_area` and `hough_threshold`).

---

## 3. Best Camera Clock Positions (Azimuth Alignment)

To prevent darts grouped in the **Triple 20** or **Triple 19** beds from blocking each other's view, mount the 3 cameras at these positions:

```
                          12 o'clock (20)
                         ┌──────────────┐
       [Camera 1]        │              │        [Camera 2]
    (At 10 o'clock:      │    (0,0)     │      (At 2 o'clock:
     Between 11 & 14)    │              │       Between 1 & 18)
                         └──────┬───────┘
                                │
                                │
                            [Camera 3]
                          (At 6 o'clock:
                          Between 7 & 19)
```

- **Camera 1:** Positioned at **10 o'clock** (pointed between sectors 11 and 14).
- **Camera 2:** Positioned at **2 o'clock** (pointed between sectors 1 and 18).
- **Camera 3:** Positioned at **6 o'clock** (pointed between sectors 7 and 19).

### Advantage:
When two darts already sit tightly inside the Triple 20 bed, Camera 1 and Camera 2 still maintain an unobstructed side-angle view of the third dart entering the bed.

---

## 4. Camera Sensor & Driver Settings

From the decompiled image processing loop, the engine relies on pixel difference calculations and stationary frame stability. Configure your camera driver with:

| Setting | Recommended Value | Reason in Algorithm |
| :--- | :--- | :--- |
| **Exposure (Shutter)** | **Manual (Fixed) @ 1/150s to 1/200s** | Eliminates micro-vibration blur when the dart impacts the sisal. |
| **Auto-Exposure (AE)** | **DISABLED (OFF)** | Prevents brightness shifts when hands enter from corrupting the background model. |
| **Auto-White Balance** | **DISABLED / Locked (5000K-6000K)**| Prevents color shifts from triggering false motion difference. |
| **Gain / ISO** | **Lowest possible (e.g. ISO 100)** | Minimizes pixel noise so differences stay below `motion.threshold`. |
| **Contrast** | **+10% to +20%** | Accentuates edge gradients between dark/metallic dart barrels and sisal. |
| **Sharpness** | **Default / Medium** | Prevents artificial edge halos in Sobel filtering. |
| **Resolution & FPS** | **1280x720 (720p) @ 60 FPS** | 60 FPS satisfies `stable_num_frames` faster, giving <80ms score confirmation. |

---

## 5. Lighting Requirements

- **360° Circular Ring Light:** Mandatory. Directional lighting causes the dart barrel to cast a long dark shadow across the board. The Hough line filter will latch onto the shadow ray instead of the physical shaft, pulling the calculated intersection point away from the true tip.
- **Color Temperature:** Neutral to Cool White (**5000K to 6500K**) with high color rendering index (CRI >= 90) for high wire-to-sisal contrast.

---

## 6. Summary Setup Checklist

1. **Lenses:** 3x low-distortion / undistorted **100° DFOV M12 lenses**.
2. **Mounting Radius:** **33 cm** outward from the Bullseye.
3. **Mounting Height:** **27 cm** above the board face.
4. **Clock Angles:** **10 o'clock, 2 o'clock, and 6 o'clock**.
5. **Lighting:** 360° shadow-free LED surround ring.
6. **Camera Driver:** Fixed 1/160s shutter, locked white balance, 720p @ 60 FPS.
