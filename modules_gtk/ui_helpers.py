"""
Reusable UI helper functions for GTK4 / Libadwaita views in SUIT.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk
from core.logger import get_logger
from core.i18n import tr

logger = get_logger("ui_helpers")


def copy_to_clipboard(text: str) -> bool:
    """Copy given text to the default system clipboard."""
    try:
        display = Gdk.Display.get_default()
        if display:
            clipboard = display.get_clipboard()
            clipboard.set(text)
            return True
    except Exception:
        logger.exception("Failed copying text to clipboard")
    return False


def generate_qr_texture(url: str) -> Gdk.Texture | None:
    """Generate a Gdk.Texture containing a QR code for the given URL."""
    try:
        import qrcode
        import io
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=5,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        glib_bytes = GLib.Bytes.new(buf.getvalue())
        return Gdk.Texture.new_from_bytes(glib_bytes)
    except Exception as e:
        logger.debug("Failed generating QR code: %s", e)
        return None


# One content width, margin and rhythm for every page, so pages line up when navigating
PAGE_MAX_WIDTH = 760
PAGE_TIGHTEN_AT = 600
PAGE_SPACING = 18


def build_page_shell(max_width: int = PAGE_MAX_WIDTH) -> tuple[Gtk.ScrolledWindow, Gtk.Box]:
    """Scrolling, width-clamped page body shared by all pages.

    Returns (root, content): set `root` as the page child (or inside an overlay) and append
    groups to `content`.
    """
    scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
    scrolled.set_kinetic_scrolling(True)
    scrolled.set_overlay_scrolling(True)
    scrolled.set_vexpand(True)

    clamp = Adw.Clamp(maximum_size=max_width, tightening_threshold=PAGE_TIGHTEN_AT)
    clamp.set_margin_top(12)
    clamp.set_margin_bottom(16)
    clamp.set_margin_start(16)
    clamp.set_margin_end(16)
    scrolled.set_child(clamp)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=PAGE_SPACING)
    clamp.set_child(content)
    return scrolled, content


def create_button_with_icon(
    icon_name: str,
    label_text: str,
    css_class: str | None = None,
    height: int = 38,
    touch_btn: bool = False,
) -> Gtk.Button:
    """Create a styled Gtk.Button containing an icon and text label."""
    btn = Gtk.Button()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8 if touch_btn else 6)
    box.set_halign(Gtk.Align.CENTER)
    box.set_valign(Gtk.Align.CENTER)
    img = Gtk.Image.new_from_icon_name(icon_name)
    img.set_pixel_size(18 if touch_btn else 16)
    box.append(img)
    lbl = Gtk.Label(label=tr(label_text))
    box.append(lbl)
    btn.set_child(box)
    if touch_btn:
        btn.add_css_class("touch-btn")
    if css_class:
        for c in css_class.split():
            if c:
                btn.add_css_class(c)
    btn.set_size_request(-1, height)
    return btn
