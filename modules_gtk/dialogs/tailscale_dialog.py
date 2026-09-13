"""
Dedicated authentication dialog for Tailscale mesh VPN.
Displays a scannable QR code for phones, an open in browser button,
a copyable URL, and monitors connection status until successfully paired.
"""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
from core.system_service import SystemService
from modules_gtk.async_utils import run_async, open_browser_url
from modules_gtk.ui_helpers import create_button_with_icon, copy_to_clipboard, generate_qr_texture


class TailscaleAuthDialog(Adw.Window):
    """
    Dedicated authentication dialog for Tailscale mesh VPN.
    Displays a scannable QR code for phones, an open in browser button,
    a copyable URL, and monitors connection status until successfully paired.
    """
    def __init__(self, parent_window, initial_status, on_finished_callback):
        super().__init__()
        self.set_transient_for(parent_window)
        self.set_modal(True)
        self.set_title("Tailscale Authentication")
        self.set_default_size(480, 560)
        self.on_finished = on_finished_callback
        self.auth_url = initial_status.get("auth_url") if isinstance(initial_status, dict) else None
        self._is_closed = False
        self._is_polling = False
        self._timeout_id = None
        self._is_connected = False
        self.connect("close-request", self._on_close_request)

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content_box.set_margin_top(12)
        content_box.set_margin_bottom(16)
        content_box.set_margin_start(24)
        content_box.set_margin_end(24)
        toolbar_view.set_content(content_box)

        # Header Icon & Text
        icon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        icon_box.set_halign(Gtk.Align.CENTER)
        content_box.append(icon_box)

        self.img_header = Gtk.Image.new_from_icon_name("network-vpn-symbolic")
        self.img_header.set_pixel_size(44)
        icon_box.append(self.img_header)

        self.lbl_title = Gtk.Label(label="Connect to Tailscale")
        self.lbl_title.add_css_class("title-2")
        icon_box.append(self.lbl_title)

        self.lbl_desc = Gtk.Label(
            label="Scan the QR code with your phone or open the link below to authenticate.",
            wrap=True,
            justify=Gtk.Justification.CENTER
        )
        self.lbl_desc.add_css_class("dim-label")
        content_box.append(self.lbl_desc)

        # QR Code Container
        self.qr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.qr_box.add_css_class("qr-card")
        self.qr_box.set_halign(Gtk.Align.CENTER)
        self.qr_box.set_valign(Gtk.Align.CENTER)
        self.qr_picture = Gtk.Picture()
        self.qr_picture.set_size_request(160, 160)
        self.qr_picture.set_can_shrink(False)
        self.qr_box.append(self.qr_picture)
        content_box.append(self.qr_box)

        # Action Buttons: Open Browser & Copy Link
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_box.set_halign(Gtk.Align.CENTER)
        content_box.append(btn_box)

        self.btn_open_browser = create_button_with_icon(
            "web-browser-symbolic",
            "Open in Browser",
            "suggested-action",
            height=40
        )
        self.btn_open_browser.set_sensitive(False)
        self.btn_open_browser.connect("clicked", self._on_open_browser_clicked)
        btn_box.append(self.btn_open_browser)

        self.btn_copy = create_button_with_icon(
            "edit-copy-symbolic",
            "Copy Link",
            "secondary-btn",
            height=40
        )
        self.btn_copy.set_sensitive(bool(self.auth_url))
        self.btn_copy.connect("clicked", self._on_copy_clicked)
        btn_box.append(self.btn_copy)

        # URL Text Entry (read-only for reference)
        self.entry_url = Gtk.Entry()
        self.entry_url.set_editable(False)
        self.entry_url.set_hexpand(True)
        self.entry_url.set_text(self.auth_url or "Retrieving link...")
        content_box.append(self.entry_url)

        # Status / Polling Box
        self.status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.status_box.set_halign(Gtk.Align.CENTER)
        self.status_box.set_margin_top(6)
        content_box.append(self.status_box)

        self.spinner = Gtk.Spinner()
        self.spinner.start()
        self.status_box.append(self.spinner)

        self.lbl_status = Gtk.Label(label="Waiting for authentication...")
        self.lbl_status.add_css_class("caption")
        self.status_box.append(self.lbl_status)

        # Bottom Action Bar
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bottom_bar.set_halign(Gtk.Align.END)
        bottom_bar.set_margin_top(4)
        content_box.append(bottom_bar)

        self.btn_close = Gtk.Button(label="Cancel")
        self.btn_close.set_size_request(90, 38)
        self.btn_close.connect("clicked", lambda b: self.close())
        bottom_bar.append(self.btn_close)

        # Start URL retrieval or initial QR setup
        if self.auth_url:
            self._on_auth_url_ready(self.auth_url, auto_open=False)
        else:
            self.qr_box.set_visible(False)
            self._fetch_auth_url()

        # Start polling for completion
        self._timeout_id = GLib.timeout_add(2000, self._poll_tailscale_status)

    def _fetch_auth_url(self):
        self.lbl_status.set_label("Generating authentication link from Tailscale...")
        def worker():
            return SystemService.get_tailscale_auth_url()
        def on_done(url):
            if self._is_closed:
                return
            if url:
                self._on_auth_url_ready(url, auto_open=False)
            else:
                self.lbl_status.set_label("Could not retrieve authentication link. Check Tailscale service.")
                self.spinner.stop()
        run_async(worker, on_done=on_done)

    def _on_auth_url_ready(self, url: str, auto_open: bool = False):
        self.auth_url = url
        self.entry_url.set_text(url)
        self.btn_open_browser.set_sensitive(True)
        self.btn_copy.set_sensitive(True)
        self._update_qr_code(url)
        self.lbl_status.set_label("Waiting for authentication...")
        if auto_open:
            self._open_browser()

    def _update_qr_code(self, url: str):
        if not url:
            self.qr_box.set_visible(False)
            return
        texture = generate_qr_texture(url)
        if texture:
            self.qr_picture.set_paintable(texture)
            self.qr_box.set_visible(True)
        else:
            self.qr_box.set_visible(False)

    def _open_browser(self):
        if not self.auth_url:
            return
        open_browser_url(self, self.auth_url)

    def _on_open_browser_clicked(self, btn):
        self._open_browser()

    def _on_copy_clicked(self, btn):
        if self.auth_url:
            copy_to_clipboard(self.auth_url)
            self.lbl_status.set_label("Link copied to clipboard! Paste it into your browser.")

    def _poll_tailscale_status(self):
        if self._is_closed or self._is_connected or self._is_polling:
            return not (self._is_closed or self._is_connected)

        self._is_polling = True

        def worker():
            return SystemService.get_tailscale_status()

        def on_done(status):
            self._is_polling = False
            if self._is_closed or self._is_connected:
                return
            if status.get("running"):
                self._is_connected = True
                if self._timeout_id:
                    GLib.source_remove(self._timeout_id)
                    self._timeout_id = None
                self.spinner.stop()
                self.spinner.set_visible(False)
                self.qr_box.set_visible(False)
                self.btn_open_browser.set_visible(False)
                self.btn_copy.set_visible(False)
                self.entry_url.set_visible(False)
                self.img_header.set_from_icon_name("emblem-ok-symbolic")
                self.lbl_title.set_label("Tailscale Connected!")
                ips = status.get("ips", [])
                ip = ips[0] if ips else ""
                host = status.get("hostname", "")
                self.lbl_desc.set_label(f"Connected to Tailscale.\nIP: {ip} ({host})")
                self.lbl_status.set_label("Successfully authenticated")
                self.lbl_status.remove_css_class("caption")
                self.lbl_status.add_css_class("status-pill-running")
                self.btn_close.set_label("Done")
                self.btn_close.remove_css_class("secondary-btn")
                self.btn_close.add_css_class("suggested-action")
                if self.on_finished:
                    self.on_finished(True, f"Tailscale connected: {ip}")

        run_async(worker, on_done=on_done)
        return True

    def _on_close_request(self, win):
        self._is_closed = True
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        return False
