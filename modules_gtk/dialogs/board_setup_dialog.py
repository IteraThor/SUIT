import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk

from core.autodarts_service import read_stored_auth, save_stored_auth
from core.systemd_service import SystemdService

SERVICE_NAME = "autodarts.service"


class BoardSetupDialog(Adw.Window):
    def __init__(self, parent_window=None, on_saved_cb=None):
        super().__init__(modal=True, title="Link Cloud Board")
        if isinstance(parent_window, Gtk.Window):
            self.set_transient_for(parent_window)
        self.set_default_size(720, 680)
        self.set_size_request(480, 520)
        self.on_saved_cb = on_saved_cb

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(box)

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(True)
        box.append(header)

        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scrolled.set_vexpand(True)
        box.append(scrolled)

        clamp = Adw.Clamp(maximum_size=640, tightening_threshold=520)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(20)
        clamp.set_margin_end(20)
        scrolled.set_child(clamp)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(content_box)

        guide_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        guide_card.add_css_class("tile-card")

        lbl_h = Gtk.Label(label="How to Link Your Board", xalign=0)
        lbl_h.add_css_class("title-3")
        guide_card.append(lbl_h)

        # Step 1: Clickable link
        step1_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        step1_box.set_valign(Gtk.Align.CENTER)

        lbl_step1 = Gtk.Label(label="1. Open Autodarts boards:", xalign=0)
        lbl_step1.add_css_class("heading")
        step1_box.append(lbl_step1)

        link_btn = Gtk.LinkButton(uri="https://play.autodarts.com/boards", label="play.autodarts.com/boards")
        link_btn.set_tooltip_text("Open https://play.autodarts.com/boards in your browser")
        step1_box.append(link_btn)
        guide_card.append(step1_box)

        # Step 2: Create board instruction
        lbl_step2 = Gtk.Label(
            label="2. On the website, click '+ New' (or generate a new API key for an existing board).",
            xalign=0,
            wrap=True
        )
        lbl_step2.add_css_class("dim-label")
        guide_card.append(lbl_step2)

        # Step 3: Copy-paste instruction
        lbl_step3 = Gtk.Label(
            label="3. Copy your Board ID and API Key, then paste them into the fields below using the paste buttons.",
            xalign=0,
            wrap=True
        )
        lbl_step3.add_css_class("dim-label")
        guide_card.append(lbl_step3)

        content_box.append(guide_card)

        grp_form = Adw.PreferencesGroup(title="Board Authorization")
        content_box.append(grp_form)

        # 1. Board ID Row
        self.row_bid = Adw.EntryRow(title="Board ID (UUID)")
        btn_paste_bid = Gtk.Button(icon_name="edit-paste-symbolic")
        btn_paste_bid.add_css_class("flat")
        btn_paste_bid.add_css_class("touch-btn")
        btn_paste_bid.set_size_request(44, 44)
        btn_paste_bid.set_valign(Gtk.Align.CENTER)
        btn_paste_bid.set_tooltip_text("Paste from clipboard")
        btn_paste_bid.connect("clicked", lambda b: self._paste_into(self.row_bid))
        self.row_bid.add_suffix(btn_paste_bid)
        grp_form.add(self.row_bid)

        # 2. API Key Row
        self.row_key = Adw.PasswordEntryRow(title="API Key")
        btn_paste_key = Gtk.Button(icon_name="edit-paste-symbolic")
        btn_paste_key.add_css_class("flat")
        btn_paste_key.add_css_class("touch-btn")
        btn_paste_key.set_size_request(44, 44)
        btn_paste_key.set_valign(Gtk.Align.CENTER)
        btn_paste_key.set_tooltip_text("Paste from clipboard")
        btn_paste_key.connect("clicked", lambda b: self._paste_into(self.row_key))
        self.row_key.add_suffix(btn_paste_key)
        grp_form.add(self.row_key)

        # Pre-fill stored values
        b_id, a_key = read_stored_auth()
        if b_id:
            self.row_bid.set_text(b_id)
        if a_key:
            self.row_key.set_text(a_key)

        # Action Buttons: Cancel and Save & Link Board
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        actions_box.set_homogeneous(True)
        actions_box.set_margin_top(6)

        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.add_css_class("secondary-btn")
        btn_cancel.add_css_class("touch-btn")
        btn_cancel.set_size_request(-1, 52)
        btn_cancel.connect("clicked", lambda b: self.close())
        actions_box.append(btn_cancel)

        btn_save = Gtk.Button(label="Save & Link Board")
        btn_save.add_css_class("suggested-action")
        btn_save.add_css_class("touch-btn")
        btn_save.set_size_request(-1, 52)
        btn_save.connect("clicked", self._on_save_clicked)
        actions_box.append(btn_save)

        content_box.append(actions_box)

    def _paste_into(self, row):
        clipboard = Gdk.Display.get_default().get_clipboard()
        def on_read(clip, res):
            try:
                text = clip.read_text_finish(res)
                if text:
                    row.set_text(text.strip())
            except Exception:
                pass
        clipboard.read_text_async(None, on_read)

    def _on_save_clicked(self, btn):
        b_id = self.row_bid.get_text().strip()
        a_key = self.row_key.get_text().strip()
        save_stored_auth(b_id, a_key)
        SystemdService.restart_unit(SERVICE_NAME)
        if self.on_saved_cb:
            self.on_saved_cb()
        self.close()
