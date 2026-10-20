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
from typing import Dict, Optional, Sequence, Tuple

logger = logging.getLogger("suit.camera_profiles")

SELECTION_FILE = Path.home() / ".config" / "suit" / "camera_profile.json"


@dataclasses.dataclass(frozen=True)
class CameraProfile:
    key: str
    name: str  # what the user picks
    description: str

    # How the exposure is found:
    #   "sensor_knee": the knee where white saturates in the sensor, then the AE lock (or written
    #                  exposure) nearest past it
    #   "ae_target":   auto exposure with backlight_compensation as its brightness target, bisected
    #                  until the white sisal reaches white_target, then locked
    exposure_strategy: str

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

    # Auto exposure with hysteresis lands one step higher when it comes from a brighter state; a lock
    # first meters this much lower on backlight_compensation, so it always comes from below (0 = off)
    ae_approach_below: int = 0

    # Tone controls set to a fixed value instead of the camera default (None = default)
    contrast: Optional[int] = None
    sharpness: Optional[int] = None

    # Control ranges assumed when the camera does not report one: name -> (min, max, default)
    fallback_ranges: Dict[str, Tuple[int, int, int]] = dataclasses.field(default_factory=dict)
    usb_ids: Tuple[str, ...] = ()  # "vid:pid" of the cameras this profile was measured on

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
    exposure_strategy="sensor_knee",
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
    usb_ids=("0bdc:2710",),
)

# GXI "Autodarts DIY Cam" (USB 0c45:6230, Sonix bridge), measured on a 3-camera rig at 1920x1080 MJPEG:
#   * white sisal does NOT saturate in the sensor: it rises until the 8-bit output clips, so there is
#     no knee; the stream holds 30 fps up to exposure 333 (1/30 s), 27.7 fps at 360
#   * auto exposure keeps a hidden sensor gain that stays active in manual mode and is not reset by
#     gain or exposure writes or a stream reopen: exposure 100 at gain 0 read 78, 82, 89, 125 or 126
#     DN depending on the AE run before (126 after power-on). A written exposure is not repeatable.
#   * auto exposure with backlight_compensation (0..160, default 55) as its target IS repeatable:
#     blc 100 -> white 204, blc 110 -> 218 from any earlier state, settled within ~1.7 s; the lock
#     holds to 0.3 DN and survives gain and gamma writes (a stream reopen drops it, as on HBV)
#   * the lock is as noisy as the written exposure of the same brightness (no HBV-style advantage)
#   * AE moves in steps (~4-14 DN of white sisal) and has one step of hysteresis (blc 60: 224 coming
#     from below, 235 from above); the white sisal spreads ~20 DN to its p99.9.
#   * gamma 72..500 with default 100; gamma 400 lifts the black sisal from 16 to 145 DN, so it stays
#     at the default
#   * opening the stream in auto exposure and switching to manual makes the driver refuse exposure
#     writes (EACCES) until auto exposure is toggled once more (V4L2Device.set_control retries)
#   * tip CNR on 9 dart tips (3 darts x 3 cameras, frozen ROIs, each setting measured twice
#     interleaved with the default): sharpness is the strongest control, monotonic from 0 (+17 %) to
#     14 (-19 %): the default 4 mostly sharpens noise. Sharpness 0 + contrast 40: +30 % on all tips
#     (repeats +30.4 / +29.9 %), weakest tip +27 %, faint points on black +38 %, black p2 still 9-15 DN.
#     Gamma 80..200: within +-2 %. Gain instead of exposure: -9 to -46 %. Contrast 48 crushed the
#     blacks (p2 1-2 DN).
#   * brightness on top of that (GUI run, 3 new darts, 5 candidates x 2 rounds against the default):
#     white median 220 (blc 49) lost 10 % tip contrast and gave +22 %; blc 62 + contrast 36 kept the
#     tip contrast (+3 %) and gave +37 % (weakest tip +46 %, dark points ON WHITE +39 %) with white
#     p99.9 at 250-254. GXI has no in-sensor knee, so a clipped white flank did not cost the points
#     on it: the p99.9 clip guard is off and the white median target bounds the brightness instead.
#     238 also covers the AE landing one step higher (~+14 DN) when it is re-locked.
GXI = CameraProfile(
    key="gxi",
    name="GXI",
    description="GXIVISION Autodarts DIY Cam",
    exposure_strategy="ae_target",
    max_exposure=333,
    knee_frac=0.98,
    knee_overshoot=1.15,
    saturated_tol=1.5,
    ae_lock=True,
    lock_range=(0.85, 1.25),
    response_steps=(0.5, 0.7, 0.85, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0),
    white_target=238.0,
    gamma_lift=0.0,
    black_floor_p2=6.0,
    black_max_mean=70.0,
    clip_p999=256.0,  # off: see above
    contrast_step=2,
    gamma_step=25,
    flush=4,  # changes show from the 3rd frame on, as on HBV
    ae_approach_below=15,  # Autodarts' start left 2 of 3 cameras one step (~20 % frame level) above calibration
    contrast=36,
    sharpness=0,
    fallback_ranges={
        "exposure_time_absolute": (1, 5000, 78),
        "gain": (0, 100, 0),
        "gamma": (72, 500, 100),
        "contrast": (0, 64, 32),
        "sharpness": (0, 14, 4),
        "brightness": (-64, 64, 0),
    },
    usb_ids=("0c45:6230",),
)

PROFILES: Dict[str, CameraProfile] = {p.key: p for p in (HBV, GXI)}
DEFAULT_PROFILE = HBV


def get_profile(key: Optional[str]) -> CameraProfile:
    return PROFILES.get(key or "", DEFAULT_PROFILE)


def profile_for_usb(ids: Sequence[str]) -> Optional[CameraProfile]:
    """The profile measured on these "vid:pid" cameras, when exactly one matches."""
    hits = {p.key: p for p in PROFILES.values() for i in ids if i in p.usb_ids}
    return next(iter(hits.values())) if len(hits) == 1 else None


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
