"""Reusable GTK4/libadwaita widgets for the Camera Optimizer wizard (Layout B).

Layout B: one large selected camera view on the left and a narrow column on the right
with the step heading, a camera list and one step-specific panel. Everything here is
pure presentation; the step pages own the engine calls and feed these widgets.

Frames are OpenCV BGR numpy arrays. They are uploaded as Gdk.MemoryTexture without any
colour conversion, and thumbnails / close-ups are downscaled with cv2 first.
"""

from __future__ import annotations
import logging
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from core.cv_utils import edge_strength_pair

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango, PangoCairo

logger = logging.getLogger("cam_librator.widgets")

# Tip marker colour: stays readable on the board's red, green, black and white segments
TIP_RGB = (1.0, 0.851, 0.290)          # #ffd94a
TIP_DARK = (0.114, 0.114, 0.125)       # #1d1d20

STEP_LABELS = ("Insert darts", "Find darts", "Tune", "Review")

# Engine stage keys (optimizer progress callback) -> plain-language chip text
STAGES: Tuple[Tuple[str, str], ...] = (
    ("brightness", "Brightness"),
    ("sensitivity", "Sensitivity"),
    ("detail", "Detail & contrast"),
    ("check", "Final check"),
)

# Fixed dart placement (decision D2): all three darts in the 20 segment.
# Numbered like the detector numbers tips: by distance from the bull, outermost first,
# so the board drawing and the boxes in step 2 use the same numbers.
# (number, drawing radius in mm from the bull centre, legend text); the single-20 spots
# sit a little away from the treble so the three dots do not overlap in a small drawing.
PLACEMENT_SPOTS: Tuple[Tuple[int, float, str], ...] = (
    (1, 140.0, "Above the treble"),
    (2, 103.0, "Treble 20"),
    (3, 64.0, "Below the treble"),
)

CHIP_KINDS = ("ok", "warn", "bad", "accent", "live", "neutral")
BANNER_KINDS = ("ok", "warn", "bad")

TipSpec = Tuple[int, Tuple[float, float], bool]   # (tip id, (x, y) image px, hidden)


# --------------------------------------------------------------------------- frame helpers

def downscale(frame: np.ndarray, max_w: int, max_h: Optional[int] = None) -> np.ndarray:
    """Shrink a frame with INTER_AREA so it fits max_w x max_h. Never upscales."""
    if frame is None or frame.size == 0:
        return frame
    h, w = frame.shape[:2]
    max_h = max_h or max_w
    scale = min(max_w / w, max_h / h)
    if scale >= 1.0:
        return frame
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    return cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)


def frame_to_texture(frame: Optional[np.ndarray], max_w: Optional[int] = None,
                     max_h: Optional[int] = None) -> Optional[Gdk.Texture]:
    """Upload a BGR (or grayscale) numpy frame as a Gdk.MemoryTexture.

    No PIL and no colour conversion for BGR: GDK reads B8G8R8 directly.
    """
    if frame is None or frame.size == 0:
        return None
    if max_w:
        frame = downscale(frame, max_w, max_h)
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    elif frame.shape[2] == 4:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    if frame.dtype != np.uint8:
        frame = np.clip(frame, 0, 255).astype(np.uint8)
    frame = np.ascontiguousarray(frame)
    h, w = frame.shape[:2]
    return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.B8G8R8,
                                 GLib.Bytes.new(frame.tobytes()), w * 3)


def crop_origin(shape: Tuple[int, int], center: Tuple[float, float], size: int,
                anchor_y: float = 0.5) -> Tuple[int, int, int]:
    """(x0, y0, size) of the square crop crop_square takes, so callers can place markers in it."""
    h, w = shape[:2]
    size = int(max(8, min(size, w, h)))
    x0 = int(round(center[0] - size / 2))
    y0 = int(round(center[1] - size * anchor_y))
    return max(0, min(w - size, x0)), max(0, min(h - size, y0)), size


def crop_square(frame: np.ndarray, center: Tuple[float, float], size: int,
                anchor_y: float = 0.5) -> Optional[np.ndarray]:
    """Square crop of side `size` around `center`, shifted inside the frame at the edges.

    anchor_y places the centre vertically inside the crop (0.5 = middle).
    """
    if frame is None or frame.size == 0:
        return None
    x0, y0, size = crop_origin(frame.shape, center, size, anchor_y)
    return frame[y0:y0 + size, x0:x0 + size]


def _wrap_label(text: str = "", css: Sequence[str] = (), max_chars: int = 28,
                xalign: float = 0.0) -> Gtk.Label:
    # Wrapping label whose natural width stays small: it takes the column width it is
    # given instead of asking for its whole text on one line (keeps the window at 980 px).
    lbl = Gtk.Label(label=text, xalign=xalign, wrap=True,
                    wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=max_chars)
    lbl.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
    for c in css:
        lbl.add_css_class(c)
    return lbl


wrap_label = _wrap_label   # public name for the step pages


def _ellipsized_label(text: str = "", css: Sequence[str] = (), xalign: float = 0.0) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=xalign, ellipsize=Pango.EllipsizeMode.END,
                    single_line_mode=True)
    for c in css:
        lbl.add_css_class(c)
    return lbl


def _set_kind_class(widget: Gtk.Widget, prefix: str, kinds: Sequence[str], kind: Optional[str]) -> None:
    for k in kinds:
        widget.remove_css_class(f"{prefix}{k}")
    if kind in kinds:
        widget.add_css_class(f"{prefix}{kind}")


class FlexPicture(Gtk.Picture):
    """Gtk.Picture that never asks for the texture's size.

    A plain Gtk.Picture reports the frame size (1280 px) as its natural width, which
    widens the window. This one reports a small natural size and fills what it gets.
    """

    def __init__(self, nat_w: int = 160, nat_h: int = 120,
                 fit: Gtk.ContentFit = Gtk.ContentFit.COVER):
        super().__init__()
        self._nat = (nat_w, nat_h)
        self.set_content_fit(fit)
        self.set_can_shrink(True)

    def do_measure(self, orientation, for_size):
        nat = self._nat[0] if orientation == Gtk.Orientation.HORIZONTAL else self._nat[1]
        return 0, nat, -1, -1

    def set_frame(self, frame: Optional[np.ndarray], max_w: Optional[int] = None,
                  max_h: Optional[int] = None) -> None:
        self.set_paintable(frame_to_texture(frame, max_w, max_h))


# --------------------------------------------------------------------------- chips

class StatusChip(Gtk.Box):
    """Small rounded status chip: dot or spinner + label. Never expands.

    variant "osd": dark translucent chip for use on top of camera images (.osd-chip).
    variant "pill": tinted chip for cards and list rows (.status-chip), no dot.
    kind: ok | warn | bad | accent | live | neutral; spinning shows an Adw.Spinner.
    """

    def __init__(self, text: str = "", kind: str = "neutral", variant: str = "osd",
                 spinning: bool = False):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_hexpand(False)
        self.set_vexpand(False)
        self.set_halign(Gtk.Align.END)
        self.set_valign(Gtk.Align.CENTER)
        self._variant = variant
        self.add_css_class("osd-chip" if variant == "osd" else "status-chip")

        self._dot = Gtk.Box(valign=Gtk.Align.CENTER)
        self._dot.add_css_class("chip-dot")
        self._spinner = Adw.Spinner(valign=Gtk.Align.CENTER)
        self._spinner.set_size_request(14, 14)
        self._label = Gtk.Label(label=text, ellipsize=Pango.EllipsizeMode.END,
                                single_line_mode=True, max_width_chars=18)
        self.append(self._dot)
        self.append(self._spinner)
        self.append(self._label)
        self.set_status(kind, text, spinning)

    def set_status(self, kind: str, text: str, spinning: bool = False) -> None:
        _set_kind_class(self, "chip-", CHIP_KINDS, kind)
        self._label.set_label(text or "")
        self._spinner.set_visible(spinning)
        # Pills carry their colour in the background; the dot is for chips on images
        self._dot.set_visible(not spinning and self._variant == "osd")


class OsdLabel(Gtk.Label):
    """Plain text chip on top of an image ("Camera 1", "Tip 2")."""

    def __init__(self, text: str = ""):
        super().__init__(label=text, ellipsize=Pango.EllipsizeMode.END, single_line_mode=True,
                         max_width_chars=28)
        self.add_css_class("osd-chip")
        self.set_hexpand(False)


# --------------------------------------------------------------------------- tip overlay

class TipOverlay(Gtk.DrawingArea):
    """Draws numbered tip boxes over a COVER-fitted picture.

    Tips are given in normalised image coordinates (0..1 of the full frame); the
    visible source rectangle (whole frame or zoom rect) and the frame aspect are set by
    the owning CameraView so boxes line up with the cropped picture.
    """

    BOX = 22.0

    def __init__(self):
        super().__init__()
        self.set_can_target(False)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._tips: List[Tuple[int, float, float, bool]] = []
        self._src = (0.0, 0.0, 1.0, 1.0)      # visible rect, normalised
        self._frame_size = (4.0, 3.0)          # px, only the aspect matters
        self._on_moved: Optional[Callable[[int, float, float], None]] = None
        self._drag_idx: Optional[int] = None
        self._drag_start = (0.0, 0.0)
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

    def set_editable(self, on_moved: Optional[Callable[[int, float, float], None]]) -> None:
        """Let the tip boxes be dragged; on_moved(tip_id, u, v) gets the new normalised frame point.

        None turns dragging off. Only presses on a box are taken: everything else (a tap that
        zooms the view) still reaches the widgets underneath.
        """
        self._on_moved = on_moved
        self._drag_idx = None
        self.set_can_target(on_moved is not None)

    def to_frame(self, x: float, y: float, width: int, height: int) -> Tuple[float, float]:
        """Inverse of to_widget."""
        fw, fh = self._frame_size
        sx, sy, sw, sh = self._src
        src_w, src_h = sw * fw, sh * fh
        scale = max(width / src_w, height / src_h)
        off_x = (width - src_w * scale) / 2.0
        off_y = (height - src_h * scale) / 2.0
        return (x - off_x) / (fw * scale) + sx, (y - off_y) / (fh * scale) + sy

    def _hit(self, x: float, y: float) -> Optional[int]:
        w, h = self.get_width(), self.get_height()
        best, best_d = None, (self.BOX / 2 + 10) ** 2
        for i, (_tid, u, v, _hid) in enumerate(self._tips):
            tx, ty = self.to_widget(u, v, w, h)
            d = (tx - x) ** 2 + (ty - y) ** 2
            if d <= best_d:
                best, best_d = i, d
        return best

    def _on_drag_begin(self, gesture, x, y):
        idx = self._hit(x, y) if self._on_moved is not None else None
        if idx is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._drag_idx = idx
        _tid, u, v, _hid = self._tips[idx]
        self._drag_start = self.to_widget(u, v, self.get_width(), self.get_height())   # box centre, not the press point

    def _on_drag_update(self, _gesture, dx, dy):
        if self._drag_idx is None:
            return
        w, h = self.get_width(), self.get_height()
        u, v = self.to_frame(self._drag_start[0] + dx, self._drag_start[1] + dy, w, h)
        tid, _u, _v, hidden = self._tips[self._drag_idx]
        self._tips[self._drag_idx] = (tid, min(1.0, max(0.0, u)), min(1.0, max(0.0, v)), hidden)
        self.queue_draw()

    def _on_drag_end(self, _gesture, _dx, _dy):
        idx, self._drag_idx = self._drag_idx, None
        if idx is None or self._on_moved is None:
            return
        tid, u, v, _hidden = self._tips[idx]
        self._on_moved(tid, u, v)

    def configure(self, tips, src, frame_size) -> None:
        self._tips = list(tips)
        self._src = src
        self._frame_size = frame_size
        self.queue_draw()

    def to_widget(self, u: float, v: float, width: int, height: int) -> Tuple[float, float]:
        """Map a normalised frame point to widget coordinates, honouring COVER cropping."""
        fw, fh = self._frame_size
        sx, sy, sw, sh = self._src
        src_w, src_h = sw * fw, sh * fh
        scale = max(width / src_w, height / src_h)
        off_x = (width - src_w * scale) / 2.0
        off_y = (height - src_h * scale) / 2.0
        return (u - sx) * fw * scale + off_x, (v - sy) * fh * scale + off_y

    def _draw(self, _area, cr, width, height):
        if not self._tips:
            return
        b = self.BOX
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(10.5)
        for tip_id, u, v, hidden in self._tips:
            x, y = self.to_widget(u, v, width, height)
            if x < -b or y < -b or x > width + b or y > height + b:
                continue
            x0, y0 = x - b / 2, y - b / 2
            # Dark outline first so the box reads on white segments
            cr.set_dash([])
            _rounded_rect(cr, x0 - 1.5, y0 - 1.5, b + 3, b + 3, 7)
            cr.set_source_rgba(0, 0, 0, 0.7)
            cr.set_line_width(1.5)
            cr.stroke()
            _rounded_rect(cr, x0, y0, b, b, 6)
            if hidden:
                cr.set_source_rgba(1, 1, 1, 0.8)
                cr.set_dash([4.0, 3.0])
            else:
                cr.set_source_rgb(*TIP_RGB)
            cr.set_line_width(2.0)
            cr.stroke()
            cr.set_dash([])
            # Number tab above the box
            text = str(tip_id)
            ext = cr.text_extents(text)
            tw, th = max(ext.x_advance + 8, 15), 15
            tx, ty = x - tw / 2, y0 - th - 3
            if ty < 2:                          # no room above: put the tab below
                ty = y0 + b + 3
            _rounded_rect(cr, tx, ty, tw, th, 4)
            if hidden:
                cr.set_source_rgba(1, 1, 1, 0.85)
            else:
                cr.set_source_rgb(*TIP_RGB)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, 0.6)
            cr.set_line_width(1.0)
            cr.stroke()
            cr.set_source_rgb(*TIP_DARK)
            cr.move_to(x - ext.x_advance / 2, ty + th / 2 + ext.height / 2 - 0.5)
            cr.show_text(text)


def _rounded_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


# --------------------------------------------------------------------------- camera view

class CameraView(Gtk.AspectFrame):
    """Large 4:3 camera view: COVER picture, name chip, status chip, tip boxes, tip PiP.

    Tip and zoom coordinates are image pixels in `space` (w, h); by default the size of
    the last frame passed to set_frame, so 1280x960 detections can sit on 640x480 live
    frames by passing space=(1280, 960).
    """

    MAX_DISPLAY_W = 1280
    PIP_FRACTION = 0.34

    def __init__(self, title: str = "Camera", nat_width: int = 480):
        super().__init__(ratio=4.0 / 3.0, obey_child=False, xalign=0.0, yalign=0.0)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._frame_size: Tuple[int, int] = (1280, 960)
        self._frame: Optional[np.ndarray] = None
        self._tips: List[TipSpec] = []
        self._tips_space: Optional[Tuple[float, float]] = None
        self._zoom: Optional[Tuple[float, float, float, float]] = None   # normalised
        self._pip_side = 0

        self.overlay = Gtk.Overlay()
        self.overlay.add_css_class("cam-frame")
        self.overlay.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_child(self.overlay)

        self.picture = FlexPicture(nat_width, int(nat_width * 3 / 4))
        self.overlay.set_child(self.picture)

        self.tip_overlay = TipOverlay()
        self.tip_overlay.connect("resize", self._on_resize)
        self.overlay.add_overlay(self.tip_overlay)

        self.name_chip = OsdLabel(title)
        self._place(self.name_chip, Gtk.Align.START, Gtk.Align.START)
        self.overlay.add_overlay(self.name_chip)

        self.status_chip = StatusChip("", "neutral", "osd")
        self._place(self.status_chip, Gtk.Align.END, Gtk.Align.START)
        self.status_chip.set_visible(False)
        self.overlay.add_overlay(self.status_chip)

        self.hint_chip = OsdLabel("")
        self._place(self.hint_chip, Gtk.Align.START, Gtk.Align.END)
        self.hint_chip.set_visible(False)
        self.overlay.add_overlay(self.hint_chip)

        # Tip close-up (picture-in-picture), bottom right, ~34 % of the view width
        self.pip = Gtk.Overlay()
        self.pip.add_css_class("tip-pip")
        self.pip.set_overflow(Gtk.Overflow.HIDDEN)
        self._place(self.pip, Gtk.Align.END, Gtk.Align.END)
        self.pip_picture = FlexPicture(96, 96)
        self.pip.set_child(self.pip_picture)
        self.pip_label = OsdLabel("")
        self.pip_label.add_css_class("osd-chip-small")
        self.pip_label.set_halign(Gtk.Align.START)
        self.pip_label.set_valign(Gtk.Align.START)
        self.pip_label.set_margin_start(5)
        self.pip_label.set_margin_top(5)
        self.pip.add_overlay(self.pip_label)
        self.pip.set_visible(False)
        self.overlay.add_overlay(self.pip)

        # Accent ring for the active camera; drawn above the picture, never clipped away
        self.ring = Gtk.Box(can_target=False)
        self.ring.add_css_class("cam-ring")
        self.overlay.add_overlay(self.ring)

    @staticmethod
    def _place(w: Gtk.Widget, halign: Gtk.Align, valign: Gtk.Align, margin: int = 8) -> None:
        w.set_halign(halign)
        w.set_valign(valign)
        w.set_margin_start(margin)
        w.set_margin_end(margin)
        w.set_margin_top(margin)
        w.set_margin_bottom(margin)

    # -- public API -------------------------------------------------------------------

    def set_title(self, text: str) -> None:
        self.name_chip.set_label(text)

    def set_frame(self, frame: Optional[np.ndarray]) -> None:
        """Show a BGR frame (any resolution). None clears the picture."""
        self._frame = frame
        if frame is None or frame.size == 0:
            self.picture.set_paintable(None)
            return
        h, w = frame.shape[:2]
        self._frame_size = (w, h)
        shown = frame
        if self._zoom is not None:
            zx, zy, zw, zh = self._zoom
            x0, y0 = int(zx * w), int(zy * h)
            x1, y1 = int((zx + zw) * w), int((zy + zh) * h)
            shown = frame[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
        self.picture.set_paintable(frame_to_texture(shown, self.MAX_DISPLAY_W, self.MAX_DISPLAY_W))
        self._sync_overlay()

    def set_tips(self, tips: Sequence[TipSpec], space: Optional[Tuple[float, float]] = None) -> None:
        """tips: [(id, (x, y), hidden)] in image px of `space` (default: frame size)."""
        self._tips = list(tips or [])
        self._tips_space = space
        self._sync_overlay()

    def set_tips_editable(self, on_moved: Optional[Callable[[int, float, float], None]]) -> None:
        """Make the tip boxes draggable. on_moved(tip_id, x, y) gets the new tip in image px of
        the tips' space; None turns dragging off."""
        if on_moved is None:
            self.tip_overlay.set_editable(None)
            return

        def moved(tid: int, u: float, v: float) -> None:
            fw, fh = self._frame_size
            tw, th = self._tips_space or (fw, fh)
            on_moved(tid, u * tw, v * th)

        self.tip_overlay.set_editable(moved)

    def set_zoom(self, rect: Optional[Tuple[float, float, float, float]],
                 space: Optional[Tuple[float, float]] = None) -> None:
        """Show only rect = (x, y, w, h) in image px of `space`; None shows the whole frame."""
        if rect is None:
            self._zoom = None
        else:
            sw, sh = space or self._frame_size
            x, y, w, h = rect
            x0, y0 = max(0.0, x / sw), max(0.0, y / sh)
            x1, y1 = min(1.0, (x + w) / sw), min(1.0, (y + h) / sh)
            self._zoom = (x0, y0, max(1e-3, x1 - x0), max(1e-3, y1 - y0))
        if self._frame is not None:
            self.set_frame(self._frame)
        else:
            self._sync_overlay()

    def set_status(self, kind: Optional[str], text: str = "", spinning: bool = False) -> None:
        """kind None hides the chip. kind "working" is a shortcut for an accent spinner."""
        if kind is None:
            self.status_chip.set_visible(False)
            return
        if kind == "working":
            kind, spinning = "accent", True
        self.status_chip.set_status(kind, text, spinning)
        self.status_chip.set_visible(True)

    def set_hint(self, text: Optional[str]) -> None:
        """Bottom-left chip, e.g. "Zoomed in on the darts"."""
        self.hint_chip.set_label(text or "")
        self.hint_chip.set_visible(bool(text))

    def set_active(self, active: bool) -> None:
        if active:
            self.overlay.add_css_class("cam-frame-active")
        else:
            self.overlay.remove_css_class("cam-frame-active")

    def set_dim(self, dim: bool) -> None:
        if dim:
            self.overlay.add_css_class("cam-frame-dim")
        else:
            self.overlay.remove_css_class("cam-frame-dim")

    def set_pip(self, crop: Optional[np.ndarray], label: str = "") -> None:
        """Bottom-right tip close-up (already cropped, BGR). None hides it."""
        if crop is None or crop.size == 0:
            self.pip.set_visible(False)
            return
        self.pip_picture.set_frame(crop, 384, 384)
        self.pip_label.set_label(label)
        self.pip_label.set_visible(bool(label))
        self.pip.set_visible(True)

    # -- internals --------------------------------------------------------------------

    def _sync_overlay(self) -> None:
        fw, fh = self._frame_size
        tw, th = self._tips_space or (fw, fh)
        norm = [(tid, x / tw, y / th, bool(hidden)) for tid, (x, y), hidden in self._tips]
        self.tip_overlay.configure(norm, self._zoom or (0.0, 0.0, 1.0, 1.0), (float(fw), float(fh)))

    def _on_resize(self, _area, width, height):
        side = int(width * self.PIP_FRACTION)
        if side != self._pip_side:
            self._pip_side = side
            # Not during allocation: changing a size request there re-queues layout
            GLib.idle_add(self._apply_pip_size, side)

    def _apply_pip_size(self, side):
        if side == self._pip_side:
            self.pip.set_size_request(side, side)
        return False


# --------------------------------------------------------------------------- camera list

class CameraRow(Adw.ActionRow):
    """One camera in the list: thumbnail prefix, title, plain subtitle, chip/spinner suffix."""

    def __init__(self, cam_id: str, title: str):
        super().__init__(title=title)
        self.cam_id = cam_id
        self.add_css_class("camera-row")
        self.set_title_lines(1)
        self.set_subtitle_lines(1)
        self.set_activatable(True)

        self.thumb = FlexPicture(64, 48)
        self.thumb.set_size_request(64, 48)
        self.thumb.set_valign(Gtk.Align.CENTER)
        self.thumb.set_overflow(Gtk.Overflow.HIDDEN)
        self.thumb.add_css_class("camera-thumb")
        self.add_prefix(self.thumb)

        self.chip = StatusChip("", "neutral", "pill")
        self.chip.set_visible(False)
        self.spinner = Adw.Spinner(valign=Gtk.Align.CENTER)
        self.spinner.set_size_request(20, 20)
        self.spinner.set_visible(False)
        self.add_suffix(self.chip)
        self.add_suffix(self.spinner)

    def update(self, subtitle: Optional[str] = None, chip_kind: Optional[str] = None,
               chip_text: str = "", spinning: bool = False) -> None:
        if subtitle is not None:
            self.set_subtitle(GLib.markup_escape_text(subtitle))
        self.spinner.set_visible(spinning)
        show_chip = (not spinning) and chip_kind is not None and bool(chip_text)
        if show_chip:
            self.chip.set_status(chip_kind, chip_text)
        self.chip.set_visible(show_chip)


class CameraList(Gtk.ListBox):
    """Boxed list of camera rows with single selection. Emits camera-selected(cam_id)."""

    __gsignals__ = {
        "camera-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    THUMB_W, THUMB_H = 128, 96          # 2x the displayed 64x48 for HiDPI

    def __init__(self, cameras: Sequence[Tuple[str, str]] = ()):
        super().__init__()
        self.add_css_class("boxed-list")
        self.add_css_class("camera-list")
        self.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.set_activate_on_single_click(True)
        self.set_valign(Gtk.Align.START)
        self.rows: Dict[str, CameraRow] = {}
        self._quiet = False
        self.connect("row-selected", self._on_row_selected)
        self.set_cameras(cameras)

    def set_cameras(self, cameras: Sequence[Tuple[str, str]]) -> None:
        """cameras: [(cam_id, title)] in display order."""
        self._quiet = True
        try:
            for row in list(self.rows.values()):
                self.remove(row)
            self.rows.clear()
            for cam_id, title in cameras:
                row = CameraRow(cam_id, title)
                self.rows[cam_id] = row
                self.append(row)
        finally:
            self._quiet = False

    def set_thumbnail(self, cam_id: str, frame: Optional[np.ndarray]) -> None:
        row = self.rows.get(cam_id)
        if row is not None:
            row.thumb.set_frame(frame, self.THUMB_W, self.THUMB_H)

    def set_row(self, cam_id: str, subtitle: Optional[str] = None, chip_kind: Optional[str] = None,
                chip_text: str = "", spinning: bool = False) -> None:
        row = self.rows.get(cam_id)
        if row is not None:
            row.update(subtitle, chip_kind, chip_text, spinning)

    def select_camera(self, cam_id: str, emit: bool = False) -> None:
        row = self.rows.get(cam_id)
        if row is None:
            return
        self._quiet = not emit
        try:
            self.select_row(row)
        finally:
            self._quiet = False

    def get_selected_camera(self) -> Optional[str]:
        row = self.get_selected_row()
        return getattr(row, "cam_id", None)

    def _on_row_selected(self, _lb, row):
        if row is not None and not self._quiet:
            self.emit("camera-selected", row.cam_id)


# --------------------------------------------------------------------------- stepper

class Stepper(Gtk.Box):
    """Numbered steps with connecting lines: done (check), current (accent), todo."""

    def __init__(self, labels: Sequence[str] = STEP_LABELS):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_css_class("optimizer-stepper")
        self._steps: List[Tuple[Gtk.Box, Gtk.Label, Gtk.Image]] = []
        self._lines: List[Gtk.Box] = []
        for i, text in enumerate(labels):
            step = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            step.add_css_class("step")
            badge = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
            badge.add_css_class("step-num")
            num = Gtk.Label(label=str(i + 1), hexpand=True, xalign=0.5)
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_pixel_size(12)
            check.set_hexpand(True)
            badge.append(num)
            badge.append(check)
            lbl = Gtk.Label(label=text, single_line_mode=True)
            lbl.add_css_class("step-label")
            step.append(badge)
            step.append(lbl)
            self._steps.append((step, num, check))
            self.append(step)
            if i < len(labels) - 1:
                line = Gtk.Box(hexpand=True, valign=Gtk.Align.CENTER)
                line.add_css_class("step-line")
                self._lines.append(line)
                self.append(line)
        self.set_current(1)

    def set_current(self, step: int) -> None:
        """step is 1-based; steps before it are shown as done."""
        for i, (box, num, check) in enumerate(self._steps):
            n = i + 1
            for c in ("step-done", "step-current"):
                box.remove_css_class(c)
            if n < step:
                box.add_css_class("step-done")
            elif n == step:
                box.add_css_class("step-current")
            num.set_visible(n >= step)
            check.set_visible(n < step)
        for i, line in enumerate(self._lines):
            if i + 1 < step:
                line.add_css_class("step-line-done")
            else:
                line.remove_css_class("step-line-done")


# --------------------------------------------------------------------------- action bar

class ActionBar(Gtk.Box):
    """The wizard's single bottom bar: secondary button left, primary button right, and an
    optional widget (e.g. a picture switch) centred between them."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.add_css_class("optimizer-actionbar")
        self.secondary_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.primary_button = Gtk.Button(valign=Gtk.Align.CENTER)
        for b in (self.secondary_button, self.primary_button):
            b.add_css_class("optimizer-btn")
            b.set_visible(False)
        self.append(self.secondary_button)
        self.center = Gtk.Box(hexpand=True, halign=Gtk.Align.FILL)
        self.append(self.center)
        self.append(self.primary_button)
        self._handlers: Dict[Gtk.Button, Tuple[int, Optional[Callable]]] = {}

    def _setup(self, button: Gtk.Button, label: Optional[str], visible: bool, sensitive: bool,
               suggested: bool, destructive: bool, callback: Optional[Callable]) -> None:
        if label is not None:
            button.set_label(label)
        button.set_visible(visible)
        button.set_sensitive(sensitive)
        for cls, on in (("suggested-action", suggested), ("destructive-action", destructive)):
            if on:
                button.add_css_class(cls)
            else:
                button.remove_css_class(cls)
        old = self._handlers.pop(button, None)
        if old is not None:
            button.disconnect(old[0])
        if callback is not None:
            hid = button.connect("clicked", lambda _b: callback())
            self._handlers[button] = (hid, callback)

    def set_primary(self, label: Optional[str] = None, visible: bool = True, sensitive: bool = True,
                    suggested: bool = True, destructive: bool = False,
                    callback: Optional[Callable[[], None]] = None) -> None:
        """Configure the right button. callback replaces any previous one (None removes it)."""
        self._setup(self.primary_button, label, visible, sensitive, suggested, destructive, callback)

    def set_secondary(self, label: Optional[str] = None, visible: bool = True, sensitive: bool = True,
                      suggested: bool = False, destructive: bool = False,
                      callback: Optional[Callable[[], None]] = None) -> None:
        """Configure the left button. callback replaces any previous one (None removes it)."""
        self._setup(self.secondary_button, label, visible, sensitive, suggested, destructive, callback)

    def set_center(self, widget: Optional[Gtk.Widget] = None) -> None:
        """Show `widget` in the middle of the bar; None clears it."""
        while (child := self.center.get_first_child()) is not None:
            self.center.remove(child)
        if widget is not None:
            widget.set_halign(Gtk.Align.CENTER)
            widget.set_hexpand(True)
            widget.set_valign(Gtk.Align.CENTER)
            self.center.append(widget)

    def hide_all(self) -> None:
        self.set_center(None)
        self.set_primary(visible=False)
        self.set_secondary(visible=False, suggested=False)

    def set_primary_sensitive(self, sensitive: bool) -> None:
        self.primary_button.set_sensitive(sensitive)

    def set_secondary_sensitive(self, sensitive: bool) -> None:
        self.secondary_button.set_sensitive(sensitive)


# --------------------------------------------------------------------------- banner

class _BannerIcon(Gtk.DrawingArea):
    """Filled status icon (check / warning triangle / cross); never a blank theme icon."""

    def __init__(self, size: int = 28):
        super().__init__()
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_valign(Gtk.Align.START)
        self.kind = "ok"
        self.set_draw_func(self._draw)

    def _draw(self, _a, cr, w, h):
        s = min(w, h) / 28.0
        cr.scale(s, s)
        cr.set_line_cap(1)      # ROUND
        cr.set_line_join(1)
        if self.kind == "warn":
            cr.move_to(14, 3); cr.line_to(26, 24); cr.line_to(2, 24); cr.close_path()
            cr.set_source_rgb(0.898, 0.647, 0.039)
            cr.set_line_width(2)
            cr.fill_preserve(); cr.stroke()
            cr.set_source_rgb(*TIP_DARK)
            _rounded_rect(cr, 12.7, 10, 2.6, 7.5, 1.3); cr.fill()
            cr.arc(14, 20.6, 1.5, 0, 2 * math.pi); cr.fill()
            return
        cr.arc(14, 14, 13, 0, 2 * math.pi)
        cr.set_source_rgb(*((0.180, 0.761, 0.494) if self.kind == "ok" else (0.878, 0.106, 0.141)))
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(2.6)
        if self.kind == "ok":
            cr.move_to(8.5, 14.5); cr.line_to(12.1, 18.1); cr.line_to(19.5, 10.1)
        else:
            cr.move_to(9.5, 9.5); cr.line_to(18.5, 18.5)
            cr.move_to(18.5, 9.5); cr.line_to(9.5, 18.5)
        cr.stroke()


class ResultBanner(Gtk.Box):
    """Inline tinted banner: icon + bold title + wrapping body. kind ok | warn | bad."""

    def __init__(self, kind: str = "ok", title: str = "", body: str = "", compact: bool = False):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.add_css_class("result-banner")
        if compact:
            self.add_css_class("result-banner-compact")
        self.icon = _BannerIcon(24 if compact else 28)
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.title_label = _wrap_label(title, ("heading",), max_chars=24)
        self.body_label = _wrap_label(body, ("result-body",), max_chars=24)
        txt.append(self.title_label)
        txt.append(self.body_label)
        self.append(self.icon)
        self.append(txt)
        self.set_result(kind, title, body)

    def set_result(self, kind: str, title: str, body: str = "") -> None:
        _set_kind_class(self, "result-", BANNER_KINDS, kind)
        self.icon.kind = kind
        self.icon.queue_draw()
        self.title_label.set_label(title)
        self.body_label.set_label(body)
        self.body_label.set_visible(bool(body))


# --------------------------------------------------------------------------- progress card

class ProgressCard(Gtk.Box):
    """Tuning progress: title + time estimate, bar, and the four stage chips."""

    def __init__(self, title: str = "", eta: str = ""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("card")
        self.add_css_class("progress-card")

        self.title_label = _wrap_label(title, ("heading",), max_chars=24)
        self.title_label.set_hexpand(True)
        self.eta_label = _ellipsized_label(eta, ("dim-label", "numeric", "caption"))
        self.eta_label.set_xalign(1.0)
        self.bar = Gtk.ProgressBar(show_text=False, hexpand=True, valign=Gtk.Align.CENTER)
        self.bar.add_css_class("optimizer-progress")

        # Bar and time estimate share one line to keep the card short in the side column
        bar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar_row.append(self.bar)
        bar_row.append(self.eta_label)
        self.append(self.title_label)
        self.append(bar_row)

        if hasattr(Adw, "WrapBox"):
            chips = Adw.WrapBox(child_spacing=6, line_spacing=6)
            add = chips.append
        else:   # libadwaita < 1.7
            chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                                row_spacing=6, homogeneous=False, max_children_per_line=4)
            add = chips.append
        self._chips: Dict[str, Tuple[Gtk.Box, Gtk.Image, Adw.Spinner]] = {}
        for key, text in STAGES:
            chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.START)
            chip.add_css_class("stage-chip")
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_pixel_size(12)
            spin = Adw.Spinner()
            spin.set_size_request(12, 12)
            chip.append(check)
            chip.append(spin)
            chip.append(Gtk.Label(label=text, single_line_mode=True))
            add(chip)
            self._chips[key] = (chip, check, spin)
        self.append(chips)
        self.set_stage(None)

    def set_title(self, text: str) -> None:
        self.title_label.set_label(text)

    def set_eta(self, text: str) -> None:
        self.eta_label.set_label(text or "")
        self.eta_label.set_visible(bool(text))

    def set_fraction(self, fraction: float) -> None:
        self.bar.set_fraction(max(0.0, min(1.0, float(fraction))))

    def set_stage(self, key: Optional[str], all_done: bool = False) -> None:
        """Mark stages before `key` done and `key` current. key None + all_done marks all done."""
        keys = [k for k, _ in STAGES]
        cur = keys.index(key) if key in keys else (len(keys) if all_done else -1)
        for i, k in enumerate(keys):
            chip, check, spin = self._chips[k]
            done = i < cur
            current = i == cur
            for c, on in (("stage-done", done), ("stage-current", current)):
                if on:
                    chip.add_css_class(c)
                else:
                    chip.remove_css_class(c)
            check.set_visible(done)
            spin.set_visible(current)


# --------------------------------------------------------------------------- tip grid

class _VerticalLabel(Gtk.DrawingArea):
    """Row label rotated 90 degrees counter-clockwise (GTK4 labels cannot rotate)."""

    def __init__(self, text: str):
        super().__init__()
        self._text = text
        self.set_content_width(18)
        self.set_vexpand(True)
        self.add_css_class("tip-grid-label")
        self.set_draw_func(self._draw)

    def set_text(self, text: str) -> None:
        self._text = text
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        layout = self.create_pango_layout(self._text)
        tw, th = layout.get_pixel_size()
        color = self.get_color()
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        cr.translate(w / 2 - th / 2, h / 2 + tw / 2)
        cr.rotate(-math.pi / 2)
        PangoCairo.show_layout(cr, layout)


class TipCrop(Gtk.AspectFrame):
    """Square tip close-up with an optional chip label and the green "after" ring."""

    def __init__(self, label: str = "", after: bool = False, ratio: float = 1.0):
        super().__init__(ratio=ratio, obey_child=False, xalign=0.5, yalign=0.0)
        self.set_hexpand(True)
        self.overlay = Gtk.Overlay()
        self.overlay.add_css_class("tip-crop")
        self.overlay.set_overflow(Gtk.Overflow.HIDDEN)
        self.picture = FlexPicture(120, 120)
        self.overlay.set_child(self.picture)
        self.empty_label = Gtk.Label(label="Not visible", wrap=True, max_width_chars=10,
                                     justify=Gtk.Justification.CENTER)
        self.empty_label.add_css_class("dim-label")
        self.empty_label.add_css_class("caption")
        self.empty_label.set_visible(False)
        self.overlay.add_overlay(self.empty_label)
        self.chip = OsdLabel(label)
        self.chip.add_css_class("osd-chip-small")
        CameraView._place(self.chip, Gtk.Align.START, Gtk.Align.END, 6)
        self.chip.set_visible(bool(label))
        self.overlay.add_overlay(self.chip)
        self.marker: Optional[Tuple[float, float]] = None
        self.marker_area = Gtk.DrawingArea(can_target=False)
        self.marker_area.set_draw_func(self._draw_marker)
        self.marker_area.set_visible(False)
        self.overlay.add_overlay(self.marker_area)
        ring = Gtk.Box(can_target=False)
        ring.add_css_class("tip-crop-ring")
        self.overlay.add_overlay(ring)
        self.set_child(self.overlay)
        self.set_after(after)

    def set_marker(self, pos: Optional[Tuple[float, float]]) -> None:
        """Dashed ring at pos (x, y as fractions of the crop) marking where a tip should be."""
        self.marker = pos
        self.marker_area.set_visible(pos is not None)
        self.marker_area.queue_draw()

    def _draw_marker(self, _area, cr, w, h) -> None:
        if self.marker is None:
            return
        x, y, r = self.marker[0] * w, self.marker[1] * h, 0.09 * min(w, h)
        cr.set_dash([4.0, 3.0])
        # dark outline first, then the yellow ring, so it reads on light and dark pictures
        for width, color in ((4.0, (0.0, 0.0, 0.0, 0.55)), (2.0, (1.0, 0.85, 0.29, 1.0))):
            cr.set_line_width(width)
            cr.set_source_rgba(*color)
            cr.arc(x, y, r, 0, 6.2832)
            cr.stroke()

    def set_after(self, after: bool) -> None:
        if after:
            self.overlay.add_css_class("tip-crop-after")
        else:
            self.overlay.remove_css_class("tip-crop-after")

    def set_label(self, text: str) -> None:
        self.chip.set_label(text)
        self.chip.set_visible(bool(text))

    def set_crop(self, crop: Optional[np.ndarray], empty_text: str = "Not visible") -> None:
        has = crop is not None and crop.size > 0
        if has:
            self.picture.set_frame(crop, 384, 384)
        else:
            self.picture.set_paintable(None)
        self.empty_label.set_label(empty_text)
        self.empty_label.set_visible(not has)


class TipGrid(Gtk.Grid):
    """Review close-ups for one camera: 2 rows (Before / After or Current) x N tips."""

    def __init__(self, n_tips: int = 3):
        super().__init__(column_spacing=8, row_spacing=8, column_homogeneous=False)
        self.add_css_class("tip-grid")
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.START)
        self._n = n_tips
        self.before: List[TipCrop] = []
        self.after: List[TipCrop] = []
        self._headers: List[Gtk.Label] = []
        self._raw_before: List[Optional[np.ndarray]] = [None] * n_tips
        self._raw_after: List[Optional[np.ndarray]] = [None] * n_tips
        self._empty_text = "Not visible"
        self.edge_mode = False
        self.before_label = _VerticalLabel("Before")
        self.after_label = _VerticalLabel("After")
        self.attach(self.before_label, 0, 1, 1, 1)
        self.attach(self.after_label, 0, 2, 1, 1)
        for i in range(n_tips):
            hdr = _ellipsized_label(f"Tip {i + 1}", ("tip-grid-header",), xalign=0.5)
            hdr.set_hexpand(True)
            self._headers.append(hdr)
            self.attach(hdr, i + 1, 0, 1, 1)
            b = TipCrop()
            a = TipCrop(after=True)
            self.before.append(b)
            self.after.append(a)
            self.attach(b, i + 1, 1, 1, 1)
            self.attach(a, i + 1, 2, 1, 1)

    def set_tips(self, before: Sequence[Optional[np.ndarray]], after: Sequence[Optional[np.ndarray]],
                 kept: bool = False, tip_ids: Optional[Sequence[int]] = None,
                 empty_text: str = "Not visible", headers: Optional[Sequence[str]] = None,
                 estimated: Optional[Sequence[bool]] = None,
                 markers: Optional[Sequence[Optional[Tuple[float, float]]]] = None,
                 now_visible: Optional[Sequence[bool]] = None,
                 why: Optional[Sequence[str]] = None) -> None:
        """Fill the grid. kept=True labels the second row "Current" without the green ring.

        Missing entries (None) show `empty_text`; tip_ids renames the column headers
        ("Tip N"), headers replaces them with free text (e.g. board areas). estimated[i] labels
        column i "Estimated" (a tip that cannot be seen: the picture shows where it should be) and
        markers[i] draws a dashed ring at that spot in both rows; why[i] is the header note of such
        a column ("too faint" by default, e.g. "hidden" when another dart is in the way), and
        now_visible[i] replaces it with "visible now" (tuning made the tip visible).
        """
        self.after_label.set_text("Current" if kept else "After")
        # Columns past the given tips (e.g. only 2 darts) stay in the layout, so the crops keep
        # their size, but show nothing instead of an empty "Tip 3"
        used = len(headers) if headers is not None else len(tip_ids) if tip_ids is not None else self._n
        for i in range(self._n):
            shown = 1.0 if i < used else 0.0
            for w in (self._headers[i], self.before[i], self.after[i]):
                w.set_opacity(shown)
            self._raw_before[i] = before[i] if i < len(before) else None
            self._raw_after[i] = after[i] if i < len(after) else None
            self.after[i].set_after(not kept)
            est = bool(estimated[i]) if estimated is not None and i < len(estimated) else False
            mark = markers[i] if markers is not None and i < len(markers) else None
            seen_now = bool(now_visible[i]) if now_visible is not None and i < len(now_visible) else False
            note = why[i] if why is not None and i < len(why) and why[i] else "too faint"
            for crop in (self.before[i], self.after[i]):
                crop.set_label("Estimated" if est else "")
                crop.set_marker(mark if est else None)
            if headers is not None and i < len(headers):
                self._headers[i].set_label(headers[i])
            elif tip_ids is not None and i < len(tip_ids):
                self._headers[i].set_label(f"Tip {tip_ids[i]}" + ((" · visible now" if seen_now else f" · {note}") if est else ""))
            else:
                self._headers[i].set_label(f"Tip {i + 1}")
        self._empty_text = empty_text
        self._render()

    def set_edge_mode(self, on: bool) -> None:
        """Show the close-ups as edge maps (one shared scale per Before/After pair) or normally."""
        if on != self.edge_mode:
            self.edge_mode = on
            self._render()

    def _render(self) -> None:
        for i in range(self._n):
            b, a = self._raw_before[i], self._raw_after[i]
            if self.edge_mode:
                b, a = edge_strength_pair(b, a)
            self.before[i].set_crop(b, self._empty_text)
            self.after[i].set_crop(a, self._empty_text)


class ProfilePlot(Gtk.DrawingArea):
    """Cross-section of one steel point, before (grey) against after (green).

    Both curves are shifted so the flanks sit at 0 and the point rises upward, so the height is the
    tip contrast, the width the edge width, and a dip beside the peak is ringing. Both curves share
    one vertical scale so they can be compared directly.
    """

    BEFORE = (0.78, 0.78, 0.82)
    AFTER = (0.34, 0.89, 0.54)

    def __init__(self, title: str = ""):
        super().__init__()
        self.set_content_width(220)
        self.set_content_height(140)
        self.set_hexpand(True)
        self.title = title
        self.before: Optional[Tuple[np.ndarray, np.ndarray]] = None
        self.after: Optional[Tuple[np.ndarray, np.ndarray]] = None
        self.set_draw_func(self._draw)

    def set_data(self, title: str, before, after) -> None:
        self.title, self.before, self.after = title, before, after
        self.queue_draw()

    def _draw(self, _area, cr, w, h) -> None:
        pad_l, pad_r, pad_t, pad_b = 8, 8, 22, 18
        curves = [(c, col) for c, col in ((self.before, self.BEFORE), (self.after, self.AFTER)) if c is not None]
        cr.set_source_rgba(1, 1, 1, 0.58)
        cr.select_font_face("sans-serif")
        cr.set_font_size(11)
        cr.move_to(pad_l, 13)
        cr.show_text(self.title)
        if not curves:
            cr.move_to(pad_l, h / 2)
            cr.show_text("No picture")
            return
        xs = curves[0][0][0]
        lo = min(0.0, min(float(c[1].min()) for c, _ in curves))
        hi = max(10.0, max(float(c[1].max()) for c, _ in curves))
        x0, x1 = pad_l, w - pad_r
        y0, y1 = h - pad_b, pad_t

        def px(x): return x0 + (float(x) - float(xs[0])) / float(xs[-1] - xs[0]) * (x1 - x0)
        def py(v): return y0 + (float(v) - lo) / (hi - lo) * (y1 - y0)

        cr.set_line_width(1)
        cr.set_source_rgba(1, 1, 1, 0.14)          # baseline (flank level) and the centre line
        cr.move_to(x0, py(0)); cr.line_to(x1, py(0)); cr.stroke()
        cr.move_to(px(0), y1); cr.line_to(px(0), y0); cr.stroke()
        cr.set_source_rgba(1, 1, 1, 0.45)
        cr.move_to(x0, h - 5); cr.show_text(f"{int(xs[0])} px")
        cr.move_to(x1 - 28, h - 5); cr.show_text(f"+{int(xs[-1])} px")
        cr.set_line_width(2)
        for (cx, cy), col in curves:
            cr.set_source_rgb(*col)
            for i, (xv, yv) in enumerate(zip(cx, cy)):
                (cr.move_to if i == 0 else cr.line_to)(px(xv), py(yv))
            cr.stroke()
        # legend with the peak height (tip contrast in DN) of each curve
        cr.set_font_size(10.5)
        legend_x = x1 - 118
        for n, ((_cx, cy), col) in enumerate(curves):
            cr.set_source_rgb(*col)
            cr.move_to(legend_x + n * 60, 13)
            cr.show_text(f"{'Before' if col == self.BEFORE else 'After'} {float(cy.max()):.0f}")


# --------------------------------------------------------------------------- board drawing

class BoardPlacementDrawing(Gtk.DrawingArea):
    """Simplified dartboard (20 at the top) with the three numbered dart spots of D2.

    All three spots sit in the 20 segment, so a whole board at side-panel size would
    stack the dots on top of each other. focus=True (default) shows the upper part of
    the board, from just below the bull to the 20 label, in a rounded square.
    """

    ORDER = (20, 1, 18, 4, 13, 6, 10, 15, 2, 17, 3, 19, 7, 16, 8, 11, 14, 9, 12, 5)
    # Board radii in mm (outer edge of each ring); 170 = outer double wire
    R_BULL, R_OUTER_BULL = 6.35, 15.9
    R_TREBLE_IN, R_TREBLE_OUT = 99.0, 107.0
    R_DOUBLE_IN, R_DOUBLE_OUT = 162.0, 170.0
    R_BOARD = 190.0
    R_NUMBERS = 180.0

    def __init__(self, size: int = 96, focus: bool = True):
        super().__init__()
        self.focus = focus
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def _draw(self, _a, cr, w, h):
        side = min(w, h)
        x_off, y_off = (w - side) / 2, (h - side) / 2
        if self.focus:
            # Visible window: 210 mm square centred 92 mm above the bull
            k = side / 210.0
            cx, cy = x_off + side / 2, y_off + side / 2 + 92.0 * k
            _rounded_rect(cr, x_off, y_off, side, side, max(6.0, side * 0.09))
            cr.clip()
            cr.set_source_rgb(0.078, 0.078, 0.086)
            cr.paint()
        else:
            k = (side / 2 - 0.5) / self.R_BOARD
            cx, cy = x_off + side / 2, y_off + side / 2
            cr.arc(cx, cy, self.R_BOARD * k, 0, 2 * math.pi)
            cr.set_source_rgb(0.078, 0.078, 0.086)
            cr.fill()

        dark, light = (0.106, 0.106, 0.114), (0.910, 0.886, 0.816)
        red, green = (0.878, 0.106, 0.141), (0.180, 0.761, 0.494)
        seg = 2 * math.pi / 20
        for i in range(20):
            # angle 0 = up (20); segments run clockwise
            a0 = -math.pi / 2 + (i - 0.5) * seg
            a1 = a0 + seg
            even = i % 2 == 0
            for r_in, r_out, col in (
                (self.R_OUTER_BULL, self.R_DOUBLE_OUT, dark if even else light),
                (self.R_TREBLE_IN, self.R_TREBLE_OUT, red if even else green),
                (self.R_DOUBLE_IN, self.R_DOUBLE_OUT, red if even else green),
            ):
                cr.new_path()
                cr.arc(cx, cy, r_out * k, a0, a1)
                cr.arc_negative(cx, cy, r_in * k, a1, a0)
                cr.close_path()
                cr.set_source_rgb(*col)
                cr.fill()
        cr.arc(cx, cy, self.R_OUTER_BULL * k, 0, 2 * math.pi)
        cr.set_source_rgb(*green)
        cr.fill()
        cr.arc(cx, cy, self.R_BULL * k, 0, 2 * math.pi)
        cr.set_source_rgb(*red)
        cr.fill()

        cr.select_font_face("Sans", 0, 1)
        if self.focus:
            # "20" in the number ring so the segment is easy to name
            cr.set_font_size(max(8.0, 13.0 * k))
            ext = cr.text_extents("20")
            cr.set_source_rgb(0.92, 0.92, 0.92)
            ny = cy - self.R_NUMBERS * k
            cr.move_to(cx - ext.x_bearing - ext.width / 2, ny - ext.y_bearing - ext.height / 2)
            cr.show_text("20")

        # Dart spots, all on the vertical centre line of the 20
        dot_r = max(5.0, side * (0.058 if self.focus else 0.05))
        cr.set_font_size(dot_r * 1.25)
        for num, r_mm, _text in PLACEMENT_SPOTS:
            x, y = cx, cy - r_mm * k
            cr.new_path()      # show_text leaves a current point; do not join it to the dot
            cr.arc(x, y, dot_r, 0, 2 * math.pi)
            cr.set_source_rgb(*TIP_RGB)
            cr.fill_preserve()
            cr.set_source_rgb(*TIP_DARK)
            cr.set_line_width(max(1.2, side * 0.014))
            cr.stroke()
            ext = cr.text_extents(str(num))
            cr.move_to(x - ext.x_bearing - ext.width / 2, y - ext.y_bearing - ext.height / 2)
            cr.show_text(str(num))


class PlacementCard(Gtk.Box):
    """Step 1 panel: board drawing next to the numbered placement legend."""

    def __init__(self, heading: str = "", size: int = 100):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.add_css_class("card")
        self.add_css_class("placement-card")
        self.drawing = BoardPlacementDrawing(size)
        self.append(self.drawing)
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True,
                      valign=Gtk.Align.CENTER)
        if heading:
            txt.append(_wrap_label(heading, ("heading",), max_chars=20))
        for num, _r, text in PLACEMENT_SPOTS:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            n = Gtk.Label(label=str(num), valign=Gtk.Align.START)
            n.add_css_class("placement-num")
            line.append(n)
            line.append(_wrap_label(text, ("placement-text",), max_chars=18))
            txt.append(line)
        self.append(txt)


# --------------------------------------------------------------------------- layout B shell

class StepHeading(Gtk.Box):
    """Right-column heading: title + dim wrapping subtitle."""

    def __init__(self, title: str = "", subtitle: str = ""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("step-head")
        self.title_label = _wrap_label(title, ("title-3",), max_chars=22)
        self.subtitle_label = _wrap_label(subtitle, ("dim-label", "step-subtitle"), max_chars=28)
        self.append(self.title_label)
        self.append(self.subtitle_label)

    def set_text(self, title: str, subtitle: str = "") -> None:
        self.title_label.set_label(title)
        self.subtitle_label.set_label(subtitle)
        self.subtitle_label.set_visible(bool(subtitle))


class SplitLayout(Gtk.Widget):
    """Layout B body: large area on the left, narrow column on the right.

    `main_slot` holds the CameraView or a TipGrid (swap with set_main); `side` is a
    vertical box for the heading, the CameraList and the step's extra panel.

    Custom allocation instead of a Gtk.Box: the main area gets the largest 4:3 rect the
    height allows, the side column takes the rest (SIDE_MIN..SIDE_MAX) and any leftover
    width centres both. The side never reports its children's natural width, so long
    texts wrap instead of widening the window past 980 px.
    """

    SIDE_MIN = 316
    SIDE_MAX = 380
    SPACING = 16
    MAIN_MIN_W = 320
    RATIO = 4.0 / 3.0

    def __init__(self):
        super().__init__()
        self.add_css_class("optimizer-split")
        self.set_vexpand(True)
        self.set_hexpand(True)
        self.main_slot = Gtk.Box(hexpand=True, vexpand=True)
        self.side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.side.add_css_class("optimizer-side")
        self.main_slot.set_parent(self)
        self.side.set_parent(self)
        self._main: Optional[Gtk.Widget] = None

    def do_dispose(self):
        for child in (self.main_slot, self.side):
            if child.get_parent() is self:
                child.unparent()
        Gtk.Widget.do_dispose(self)

    def set_main(self, widget: Optional[Gtk.Widget]) -> None:
        if widget is self._main:
            return
        if self._main is not None:
            self.main_slot.remove(self._main)
        self._main = widget
        if widget is not None:
            self.main_slot.append(widget)

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            w = self.MAIN_MIN_W + self.SPACING + self.SIDE_MIN
            return w, w, -1, -1
        side_min, side_nat, _b1, _b2 = self.side.measure(Gtk.Orientation.VERTICAL, self.SIDE_MIN)
        main_min, _n, _b1, _b2 = self.main_slot.measure(Gtk.Orientation.VERTICAL, self.MAIN_MIN_W)
        h_min = max(side_min, main_min)
        return h_min, max(h_min, side_nat), -1, -1

    def do_size_allocate(self, width, height, baseline):
        main_w = int(min(height * self.RATIO, width - self.SPACING - self.SIDE_MIN))
        main_w = max(0, main_w)
        side_w = width - self.SPACING - main_w
        side_w = max(min(side_w, self.SIDE_MAX), min(self.SIDE_MIN, max(0, width - self.SPACING)))
        offset = max(0, (width - main_w - self.SPACING - side_w) // 2)
        main = Gdk.Rectangle()
        main.x, main.y, main.width, main.height = offset, 0, main_w, height
        self.main_slot.size_allocate(main, -1)
        side = Gdk.Rectangle()
        side.x, side.y, side.width, side.height = offset + main_w + self.SPACING, 0, side_w, height
        self.side.size_allocate(side, -1)

    def do_snapshot(self, snapshot):
        self.snapshot_child(self.main_slot, snapshot)
        self.snapshot_child(self.side, snapshot)
