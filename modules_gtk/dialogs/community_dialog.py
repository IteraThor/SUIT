"""
Community and 3D Print Models dialog for SUIT GTK4.
Displays scannable QR codes for phone access and direct clickable links for:
- IteraThor Discord Community (https://discord.gg/3ryuF4CXra)
- IteraThor 3D Print Models (https://makerworld.com/en/@HipsThor)
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
from modules_gtk.async_utils import open_browser_url
from modules_gtk.ui_helpers import create_button_with_icon, copy_to_clipboard, generate_qr_texture
from core.logger import get_logger

logger = get_logger("community_dialog")

DISCORD_URL = "https://discord.gg/3ryuF4CXra"
MAKERWORLD_URL = "https://makerworld.com/en/@HipsThor"


class CommunityDialog(Adw.Window):
    """
    Dedicated modal dialog displaying QR codes and clickable links
    for the IteraThor Discord Community and 3D Print Models on MakerWorld.
    Fits comfortably on screen without vertical scrolling.
    """
    def __init__(self, parent_window):
        super().__init__()
        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.set_title("IteraThor Community & 3D Models")
        self.set_default_size(680, 370)
        self.parent_window = parent_window

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Box())
        toolbar_view.add_top_bar(header)

        # Scrolled container with overlay scrolling, natural fit without scrollbars
        scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.NEVER
        )
        toolbar_view.set_content(scrolled)

        clamp = Adw.Clamp(maximum_size=700, tightening_threshold=580)
        clamp.set_margin_top(12)
        clamp.set_margin_bottom(16)
        clamp.set_margin_start(18)
        clamp.set_margin_end(18)
        scrolled.set_child(clamp)

        # Two-Column Cards Container directly in clamp
        cards_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        cards_box.set_homogeneous(True)
        clamp.set_child(cards_box)

        # Card 1: Discord Community
        card_discord = self._build_item_card(
            title="Discord Community",
            url=DISCORD_URL,
            display_url="discord.gg/3ryuF4CXra",
        )
        cards_box.append(card_discord)

        # Card 2: 3D Print Models
        card_models = self._build_item_card(
            title="3D Print Models",
            url=MAKERWORLD_URL,
            display_url="makerworld.com/@HipsThor",
        )
        cards_box.append(card_models)

    def _build_item_card(
        self,
        title: str,
        url: str,
        display_url: str,
    ) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("tile-card")
        card.set_valign(Gtk.Align.FILL)

        # Card Title
        lbl_item_title = Gtk.Label(label=title)
        lbl_item_title.add_css_class("title-3")
        lbl_item_title.set_halign(Gtk.Align.CENTER)
        card.append(lbl_item_title)

        # QR Code Container
        qr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        qr_box.add_css_class("qr-card")
        qr_box.set_halign(Gtk.Align.CENTER)
        qr_box.set_valign(Gtk.Align.CENTER)
        qr_box.set_margin_top(2)
        qr_box.set_margin_bottom(2)

        qr_picture = Gtk.Picture()
        qr_picture.set_size_request(136, 136)
        qr_picture.set_can_shrink(False)

        texture = generate_qr_texture(url)
        if texture:
            qr_picture.set_paintable(texture)
        qr_box.append(qr_picture)
        card.append(qr_box)

        # Action Buttons (Open Link & Copy)
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.set_margin_top(4)
        card.append(btn_box)

        btn_open = create_button_with_icon(
            "web-browser-symbolic",
            display_url,
            "suggested-action compact-btn",
            height=38,
            touch_btn=True,
        )
        btn_open.set_tooltip_text(f"Open {url} in browser")
        btn_open.connect("clicked", lambda b, target_url=url: self._open_url(target_url))
        btn_box.append(btn_open)

        btn_copy = create_button_with_icon(
            "edit-copy-symbolic",
            "Copy",
            "secondary-btn compact-btn",
            height=38,
            touch_btn=True,
        )
        btn_copy.set_tooltip_text(f"Copy {url} to clipboard")
        btn_copy.connect("clicked", lambda b, target_url=url: self._copy_url(target_url))
        btn_box.append(btn_copy)

        return card

    def _open_url(self, url: str):
        try:
            open_browser_url(self, url)
        except Exception:
            logger.exception("Failed opening URL in browser: %s", url)

    def _copy_url(self, url: str):
        if copy_to_clipboard(url):
            if hasattr(self.parent_window, "show_toast"):
                self.parent_window.show_toast("Link copied to clipboard!")
