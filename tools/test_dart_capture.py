#!/usr/bin/env python3
"""
Test Dart Capture
==================
Captures test frames with inserted darts across candidate ISP settings on /dev/video0.
Saves frames to /home/autodarts/.local/share/suit/dart_tests/
"""

import os
import sys
import time
import cv2
import numpy as np

CANDIDATES = [
    {
        "name": "cand_A_opt_sharp90_c40_gam220",
        "gain": 50, "sharpness": 90, "contrast": 40, "gamma": 220, "exposure": 300
    },
    {
        "name": "cand_B_mod_sharp70_c40_gam220",
        "gain": 50, "sharpness": 70, "contrast": 40, "gamma": 220, "exposure": 300
    },
    {
        "name": "cand_C_baseline_s40_c30_gam300",
        "gain": 60, "sharpness": 40, "contrast": 30, "gamma": 300, "exposure": 300
    },
    {
        "name": "cand_D_low_contrast_s90_c20_gam100",
        "gain": 110, "sharpness": 90, "contrast": 20, "gamma": 100, "exposure": 300
    },
]

OUT_DIR = "/home/autodarts/.local/share/suit/dart_tests"
DEVICE = "/dev/video0"

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cap = cv2.VideoCapture(DEVICE, cv2.CAP_V4L2)
    if not cap.isOpened():
        print(f"Error opening {DEVICE}", file=sys.stderr)
        sys.exit(1)

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 960)
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
    cap.set(cv2.CAP_PROP_BRIGHTNESS, 0)
    cap.set(cv2.CAP_PROP_SATURATION, 68)

    # Warmup
    for _ in range(15):
        cap.read()

    print("Capturing candidate frames with inserted darts...")
    for c in CANDIDATES:
        name = c["name"]
        cap.set(cv2.CAP_PROP_EXPOSURE, c["exposure"])
        cap.set(cv2.CAP_PROP_GAIN, c["gain"])
        cap.set(cv2.CAP_PROP_SHARPNESS, c["sharpness"])
        cap.set(cv2.CAP_PROP_CONTRAST, c["contrast"])
        cap.set(cv2.CAP_PROP_GAMMA, c["gamma"])

        # Flush 4 frames to settle completely
        for _ in range(4):
            cap.read()

        # Capture 5 frames and average to remove thermal noise
        frames = []
        for _ in range(5):
            ret, f = cap.read()
            if ret:
                frames.append(f.astype(np.float32))
        avg_frame = np.mean(np.stack(frames, axis=0), axis=0).astype(np.uint8)

        fpath = os.path.join(OUT_DIR, f"{name}.jpg")
        cv2.imwrite(fpath, avg_frame, [cv2.IMWRITE_JPEG_QUALITY, 98])
        print(f"  -> Saved {fpath}")

    # Restore defaults
    cap.set(cv2.CAP_PROP_GAIN, 64)
    cap.set(cv2.CAP_PROP_SHARPNESS, 40)
    cap.set(cv2.CAP_PROP_CONTRAST, 34)
    cap.set(cv2.CAP_PROP_GAMMA, 300)
    cap.release()
    print("Done. Restored defaults.")

if __name__ == "__main__":
    main()
