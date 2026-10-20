"""Camera profiles: the measured, brand-specific numbers the Camera Optimizer works with.

Every value here was measured on one camera model, and another brand's firmware can differ in any
of them (sensor ceiling, exposure units, AE lock, control ranges). The user picks their camera
brand when the optimizer opens; the choice is remembered. `python -m core.camera_probe` measures
what a new profile needs.
"""

from __future__ import annotations
import dataclasses
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger("suit.camera_profiles")

SELECTION_FILE = Path.home() / ".config" / "suit" / "camera_profile.json"


@dataclasses.dataclass(frozen=True)
class CameraProfile:
    key: str
    name: str  # what the user picks
    description: str

    # Exposure (V4L2 exposure_time_absolute units)
    max_exposure: int  # longest exposure the search uses (stream keeps its frame rate up to here)
    knee_frac: float  # knee: white-sisal median at this share of the sensor ceiling
    knee_overshoot: float  # final exposure past the knee
    saturated_tol: float  # DN; white median equal at max and 0.8 * max exposure = sensor saturated
    ae_lock: bool  # lock auto exposure (switch to manual without writing an exposure) instead of writing one
    lock_range: Tuple[float, float]  # a lock is used within this multiple of the knee
    response_steps: Tuple[float, ...]  # x knee: written-exposure response table for the lock

    # Sensitivity and tone (DN)
    white_target: float  # white-sisal median after gain
    gamma_lift: float  # gamma = default + this share of (max - default)
    black_floor_p2: float  # darkest 2 % of the black sisal above this, or contrast steps down
    black_max_mean: float  # black sisal mean above this = washed out: gamma steps down
    clip_p999: float  # white-sisal p99.9 below this, or gain steps down
    contrast_step: int
    gamma_step: int

    flush: int  # frames to drop after a control change

    # Control ranges assumed when the camera does not report one: name -> (min, max, default)
    fallback_ranges: Dict[str, Tuple[int, int, int]] = dataclasses.field(default_factory=dict)

    def fallback(self, name: str, lo: int, hi: int, default: int) -> Tuple[int, int, int]:
        return self.fallback_ranges.get(name, (lo, hi, default))


# HBV OV2710 (USB 0bdc:2710, "HBVCAM Camera"), measured on the 3-camera rig at 1280x960 MJPEG:
#   * white sisal saturates in the sensor at ~127 DN (gain 0) from exposure ~300; the stream holds
#     30 fps up to 600, frames got unstable at 700
#   * an AE lock is ~15-20 % less noisy than the written exposure of the same brightness and survives
#     gain/gamma writes; backlight_compensation 0/1/2 meters at ~0.8/1.0/1.4 x the knee
#   * past 1.25 x knee the dark groove of a wire on white fell below 40 DN
HBV = CameraProfile(
    key="hbv",
    name="HBV",
    description="HBVCAM OV2710 cameras",
    max_exposure=600,
    knee_frac=0.98,
    knee_overshoot=1.15,
    saturated_tol=1.5,
    ae_lock=True,
    lock_range=(0.85, 1.25),
    response_steps=(0.5, 0.7, 0.85, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0),
    white_target=231.0,
    gamma_lift=0.75,
    black_floor_p2=6.0,
    black_max_mean=70.0,
    clip_p999=250.0,
    contrast_step=2,
    gamma_step=25,
    flush=4,
    fallback_ranges={
        "exposure_time_absolute": (1, 1250, 166),
        "gain": (0, 128, 64),
        "gamma": (100, 500, 300),
        "contrast": (0, 100, 34),
        "sharpness": (0, 100, 40),
        "brightness": (-64, 64, 0),
    },
)

PROFILES: Dict[str, CameraProfile] = {p.key: p for p in (HBV,)}
DEFAULT_PROFILE = HBV


def get_profile(key: Optional[str]) -> CameraProfile:
    return PROFILES.get(key or "", DEFAULT_PROFILE)


def load_selected(path: Optional[Path] = None) -> Optional[CameraProfile]:
    """The profile the user picked last time, or None when nothing (valid) was picked yet."""
    try:
        key = json.loads((path or SELECTION_FILE).read_text(encoding="utf-8")).get("profile")
        return PROFILES.get(key)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Could not read the camera profile choice: %s", e)
        return None


def save_selected(profile: CameraProfile, path: Optional[Path] = None) -> None:
    path = path or SELECTION_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"profile": profile.key}), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not save the camera profile choice: %s", e)
