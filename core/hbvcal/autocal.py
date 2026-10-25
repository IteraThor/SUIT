"""Automatic calibration of one camera for a new setup (needs exclusive access: stop Autodarts first).

Strategy (all measured on a 3-camera HBV rig):
  * Exposure is sensor-referred: the OV2710 output saturates at a fixed level (~145 DN at digital gain 0).
    The best picture uses the longest exposure the frame rate allows **with analog gain 1×**, as long as the
    white sisal stays at ~92 % of that ceiling. Brightness is then raised with the ISP's digital gain
    (UVC `gain`) until the white segments hit a common target → all cameras end up matched.
  * Only if the scene is too dark at the longest exposure is sensor analog gain added.
  * Flicker: if a short exposure shows frame-to-frame brightness changes, exposure is quantised to the mains
    half-period (10 ms for 50 Hz, 8.33 ms for 60 Hz; the better one is detected).
  * Tone/denoise: sharpness 10, gamma 250, contrast 34, auto white balance (measured winners).
  * MJPEG Q is chosen by a bandwidth check over all cameras (calibrate_all): the lowest Q (= least compression)
    at which all cameras stream together cleanly, confirmed by a longer run.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import board, profiles
from .capture import START_DISCARD, burst, luma
from .settings import Settings, apply_prestream, apply_streaming, frame_budget_kb, snapshot
from .v4l2 import Camera
from .xu import XU

EXPOSURE_UNIT_S = 50e-6  # UVC exposure_time_absolute unit on these cameras (measured, not the 100 µs of the spec)
WHITE_FRACTION = 0.92  # white sisal level / sensor ceiling
WHITE_TARGET = 232.0  # output DN of the white segments after digital gain
TONE = {"sharpness": 10, "gamma": 250, "contrast": 34, "brightness": 0, "saturation": 68, "hue": 0,
        "white_balance_automatic": 1, "backlight_compensation": 0, "exposure_dynamic_framerate": 0}
DEFAULT_Q = 5
FLICKER_CV = 0.03  # frame-mean coefficient of variation (1 ms exposure) above which lighting counts as flickering
LINEAR_GAMMA = 100  # UVC gamma minimum ≈ linear response: used while searching the exposure


def analog_code(gain: float) -> int:
    """OV2710 0x350B code for an analog gain ≥ 1 (coarse ×2 steps in bits 4–6, fine n/16 in bits 0–3)."""
    gain = max(1.0, min(gain, 15.5))
    coarse = 0
    while gain >= 2.0 and coarse < 3:
        gain /= 2.0
        coarse += 1
    fine = int(round((gain - 1.0) * 16))
    fine = min(fine, 15)
    return ((1 << coarse) - 1) << 4 | fine


def analog_gain(code: int) -> float:
    coarse = bin((code >> 4) & 0x7).count("1")
    return (2 ** coarse) * (1 + (code & 0xF) / 16.0)


@dataclasses.dataclass
class Result:
    device: str
    settings: Settings
    meta: dict


class Session:
    """One camera, stream kept open, quick measurements of the board area."""

    def __init__(self, device: str, width: int = 1280, height: int = 960, fps: int = 30, log: Callable = print):
        self.device, self.w, self.h, self.fps, self.log = device, width, height, fps, log
        self.cam = Camera(device)
        self.xu = XU(device)
        self.H = board.load_homography(device, width, height)
        self.mask = None

    def __enter__(self):
        self.xu.open()
        self.cam.open()
        self.xu.unlock()
        self.cam.configure(self.w, self.h, "MJPG", self.fps)
        self.cam.start()
        burst(self.cam, 0, discard=START_DISCARD)
        if self.H is not None:
            self.mask = board.board_mask_image(self.H, (self.h, self.w))
        else:  # no Autodarts calibration yet: central ellipse
            yy, xx = np.mgrid[0:self.h, 0:self.w]
            self.mask = ((xx - self.w / 2) / (0.3 * self.w)) ** 2 + ((yy - self.h / 2) / (0.4 * self.h)) ** 2 <= 1
        return self

    def __exit__(self, *exc):
        try:
            self.cam.stop()
        finally:
            self.cam.close()
            self.xu.close()

    def apply(self, s: Settings, settle: int = 8):
        apply_streaming(self.cam, self.xu, s)
        burst(self.cam, 0, discard=settle)

    def frames(self, n: int = 6) -> np.ndarray:
        return np.stack([luma(f) for f in burst(self.cam, n).decode()])

    def white_level(self, n: int = 6) -> float:
        """Level of the white segments: 97th percentile of board-area luma (white sisal ≈ 25 % of the board)."""
        y = self.frames(n).mean(0)
        return float(np.percentile(y[self.mask], 97))

    def stats(self, n: int = 10) -> dict:
        y = self.frames(n)
        m = y.mean(0)[self.mask]
        return {"white": float(np.percentile(m, 97)), "black": float(np.percentile(m, 3)), "mean": float(m.mean()),
                "tnoise": float(np.median(y.std(0, ddof=1)[self.mask]))}


def exposure_max(fps: int) -> int:
    return int(0.98 / fps / EXPOSURE_UNIT_S)


def detect_flicker(ses: Session, base: Settings) -> dict:
    """Frame-mean variation at 1 ms exposure; if flickering, pick the mains period that cancels it."""
    def cv_at(exp: int, gain: int) -> float:
        ses.apply(base.merged(Settings(uvc={"exposure_time_absolute": exp, "gain": gain})), settle=4)
        fm = ses.frames(45).mean((1, 2))
        return float(fm.std() / max(fm.mean(), 1e-3))

    cv = cv_at(20, 128)
    res = {"flicker_cv_1ms": round(cv, 4), "flicker": cv > FLICKER_CV, "step": 1}
    if res["flicker"]:
        cv50, cv60 = cv_at(200, 64), cv_at(167, 64)
        res.update(cv_10ms=round(cv50, 4), cv_8ms=round(cv60, 4))
        res["mains_hz"] = 50 if cv50 <= cv60 else 60
        res["step"] = 200 if res["mains_hz"] == 50 else 167
    return res


def calibrate(device: str, fps: int = 30, width: int = 1280, height: int = 960, q: int = DEFAULT_Q,
              log: Callable = print) -> Result:
    t0 = time.time()
    base = Settings(uvc={"auto_exposure": 1, "power_line_frequency": 1, "gain": 0, **TONE}, sensor={0x350B: 0x00})
    with Session(device, width, height, fps, log) as ses:
        before = snapshot(ses.cam, ses.xu, width)
        ses.apply(base.merged(Settings(uvc={"exposure_time_absolute": 300})))
        fl = detect_flicker(ses, base)
        log(f"  flicker: {fl}")
        emax = exposure_max(fps)
        step = fl["step"]

        def quant(e: float) -> int:
            e = int(min(e, emax))
            return max(step, (e // step) * step) if step > 1 else max(2, e)

        # Exposure search in the (near-)linear domain: gamma at its minimum, no digital gain.
        lin = base.merged(Settings(uvc={"gamma": LINEAR_GAMMA}))
        # sensor ceiling: deliberately saturate (analog 2×, longest exposure)
        ses.apply(lin.merged(Settings(uvc={"exposure_time_absolute": emax}, sensor={0x350B: 0x10})))
        ceiling = ses.white_level()
        target = WHITE_FRACTION * ceiling
        ses.apply(lin.merged(Settings(uvc={"exposure_time_absolute": quant(emax)})))
        w_max = ses.white_level()
        if w_max <= target:  # dark scene: longest exposure + analog gain
            exp, acode = quant(emax), analog_code(target / max(w_max, 1e-3))
        else:  # bisection: largest exposure with white <= target
            unit = step if step > 1 else 10
            lo, hi = 1, int(quant(emax) // unit)
            while hi - lo > 1:
                mid = (lo + hi) // 2
                ses.apply(lin.merged(Settings(uvc={"exposure_time_absolute": mid * unit})), settle=5)
                if ses.white_level() <= target:
                    lo = mid
                else:
                    hi = mid
            exp, acode = lo * unit, 0x00
        s = base.merged(Settings(uvc={"exposure_time_absolute": exp}, sensor={0x350B: acode}))
        ses.apply(lin.merged(Settings(uvc={"exposure_time_absolute": exp}, sensor={0x350B: acode})))
        w_sensor = ses.white_level()
        log(f"  ceiling={ceiling:.1f} DN (linear)  white@max exposure={w_max:.1f}  -> exposure {exp} "
            f"({exp * EXPOSURE_UNIT_S * 1000:.1f} ms), analog {analog_gain(acode):.2f}x -> white {w_sensor:.1f} DN "
            f"({100 * w_sensor / ceiling:.0f} % of ceiling)")
        # digital gain: bisection to the common white target
        lo, hi = 0, 128
        for _ in range(7):
            mid = (lo + hi) // 2
            ses.apply(s.merged(Settings(uvc={"gain": mid})), settle=6)
            if ses.white_level() < WHITE_TARGET:
                lo = mid
            else:
                hi = mid
        gain = lo
        s = s.merged(Settings(uvc={"gain": gain}, mjpeg_q=q))
        ses.apply(s)
        st = ses.stats()
        log(f"  digital gain {gain} -> white {st['white']:.1f} black {st['black']:.1f} mean {st['mean']:.1f} tnoise {st['tnoise']:.2f}")
    meta = {"fps": fps, "resolution": f"{width}x{height}", "ceiling": round(ceiling, 1),
            "white_sensor": round(w_sensor, 1), "reference": {k: round(v, 2) for k, v in st.items()},
            "flicker": fl, "calibrated": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t0, 1),
            "previous": before.to_json(), "board_homography": ses.H is not None}
    return Result(device, s, meta)


Q_CANDIDATES = (1, 2, 3, 4, 5, 6, 7, 8, 11, 16, 22)  # least compression first; 22 = stock


def calibrate_all(devices: List[str], fps: int = 30, q_candidates=Q_CANDIDATES, check_s: float = 20,
                  confirm_s: float = 60, save: bool = True, log: Callable = print,
                  step: Callable[[str], None] = lambda title: None,
                  failed: Callable[[str], None] = lambda note: None) -> Dict[str, Result]:
    """Calibrate every camera, then pick the lowest MJPEG Q at which all cameras stream together cleanly:
    a short check per level, lowest first, and a longer confirmation run of the first level that passes
    (the lowest level has the least bandwidth margin). A failed confirmation moves on to the next level.
    step(title) / failed(note) report progress as checklist lines (a new line, or the current one failed)."""
    results = {}
    for d in devices:
        step(f"Calibrating exposure and gain: port {_port_no(d)}")
        log(f"calibrating {d}")
        results[d] = calibrate(d, fps=fps, log=log)
    chosen = None
    for i, q in enumerate(q_candidates):
        which = "lowest" if i == 0 else ("stock" if q == q_candidates[-1] else "next")
        step(f"Testing compression stability: level {q} ({which}, {check_s:.0f} s)")
        log(f"bandwidth check: {len(devices)} cameras at Q{q}, {fps} fps, {check_s:.0f} s")
        ok, why = _check_together(results, q, fps, check_s)
        if ok:
            step(f"Confirming level {q} ({confirm_s:.0f} s)")
            log(f"  Q{q} clean; confirming for {confirm_s:.0f} s")
            ok, why = _check_together(results, q, fps, confirm_s)
        if ok:
            chosen = q
            break
        failed(f"not stable ({why})")
        log(f"  Q{q} not stable: {why}")
    if chosen is None:
        raise RuntimeError("no MJPEG quality level streamed cleanly on all cameras")
    for d, r in results.items():
        r.settings = r.settings.merged(Settings(mjpeg_q=chosen))
        r.meta["mjpeg_q"] = chosen
        if save:
            profiles.put(d, r.settings, r.meta)
    log(f"done: Q{chosen}; profiles saved to {profiles.STORE}" if save else f"done: Q{chosen}")
    return results


def _port_no(device: str) -> str:
    """'...-usb-0:2:1.0-video-index0' -> '2'."""
    port = profiles.port_key(device)
    return port.split("usb-0:")[-1].split(":")[0] if "usb-0:" in port else port


def _check_together(results: Dict[str, Result], q: int, fps: int, seconds: float) -> Tuple[bool, str]:
    """Stream all cameras at once, each with its own calibrated settings at MJPEG Q `q`. (clean?, why not)"""
    import threading
    from .multistream import Worker, kernel_error_lines
    evt = threading.Event()
    budgets = {}
    for d in results:  # frame-size budget of each camera's firmware build (inf where unlimited)
        with XU(d) as xu:
            budgets[d] = frame_budget_kb(xu)
    k0 = kernel_error_lines()
    ws = [Worker(d, r.settings.merged(Settings(mjpeg_q=q)), 1280, 960, fps, seconds, evt) for d, r in results.items()]
    for w in ws:
        w.start()
    time.sleep(1.0)
    evt.set()
    problems = []
    for w in ws:
        w.join(seconds + 60)
        st, port = w.stats, f"port {_port_no(w.dev)}"
        if w.error or not st:
            problems.append(f"{port}: {w.error or 'no frames'}")
        elif st["dropped"] or st["broken"] or st["gaps_over_1.5x"] or abs(st["fps"] - fps) > 0.2:
            bad = [f"{st['fps']:.1f} fps" if abs(st["fps"] - fps) > 0.2 else "",
                   f"{st['dropped']} dropped" if st["dropped"] else "", f"{st['broken']} broken" if st["broken"] else "",
                   f"{st['gaps_over_1.5x']} gaps" if st["gaps_over_1.5x"] else ""]
            problems.append(f"{port}: " + ", ".join(b for b in bad if b))
        elif st["kb_max"] > budgets[w.dev]:
            problems.append(f"{port}: frames up to {st['kb_max']:.0f} KB, firmware budget {budgets[w.dev]:.0f} KB")
    kerr = kernel_error_lines() - k0
    if kerr:
        problems.append(f"{kerr} USB/UVC kernel error(s)")
    return not problems, "; ".join(problems)
