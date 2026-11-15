//! Community and 3D print models dialog: QR codes for phones plus open / copy buttons.
use crate::core::i18n::{tr, trf};
use crate::ui::helpers::{copy_to_clipboard, generate_qr_texture, icon_button, open_browser_url};
use crate::ui::Win;
use adw::prelude::*;

pub const DISCORD_URL: &str = "https://discord.gg/3ryuF4CXra";
pub const MAKERWORLD_URL: &str = "https://makerworld.com/en/@HipsThor";

pub fn present(win: &Win) {
    let dlg = adw::Window::new();
    dlg.set_transient_for(Some(&win.window));
    dlg.set_modal(true);
    dlg.set_title(Some(&tr("IteraThor Community & 3D Models")));
    dlg.set_default_size(680, 370);
    let tv = adw::ToolbarView::new();
    dlg.set_content(Some(&tv));
    let hb = adw::HeaderBar::new();
    hb.set_title_widget(Some(&gtk::Box::new(gtk::Orientation::Horizontal, 0)));
    tv.add_top_bar(&hb);
    let sc = gtk::ScrolledWindow::builder().hscrollbar_policy(gtk::PolicyType::Never).vscrollbar_policy(gtk::PolicyType::Never).build();
    tv.set_content(Some(&sc));
    let clamp = adw::Clamp::builder().maximum_size(700).tightening_threshold(580).margin_top(12).margin_bottom(16).margin_start(18).margin_end(18).build();
    sc.set_child(Some(&clamp));
    let cards = gtk::Box::new(gtk::Orientation::Horizontal, 16);
    cards.set_homogeneous(true);
    clamp.set_child(Some(&cards));
    cards.append(&card(win, &tr("Discord Community"), DISCORD_URL, "discord.gg/3ryuF4CXra"));
    cards.append(&card(win, &tr("3D Print Models"), MAKERWORLD_URL, "makerworld.com/@HipsThor"));
    dlg.present();
}

fn card(win: &Win, title: &str, url: &'static str, display: &str) -> gtk::Box {
    let c = gtk::Box::new(gtk::Orientation::Vertical, 10);
    c.add_css_class("tile-card");
    let t = gtk::Label::new(Some(title));
    t.add_css_class("title-3");
    c.append(&t);
    let qb = gtk::Box::new(gtk::Orientation::Vertical, 0);
    qb.add_css_class("qr-card");
    qb.set_halign(gtk::Align::Center);
    let pic = gtk::Picture::new();
    pic.set_size_request(136, 136);
    pic.set_can_shrink(false);
    if let Some(tex) = generate_qr_texture(url) {
        pic.set_paintable(Some(&tex));
    }
    qb.append(&pic);
    c.append(&qb);
    let bb = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    bb.set_halign(gtk::Align::Center);
    bb.set_margin_top(4);
    c.append(&bb);
    let open = icon_button("web-browser-symbolic", display, "suggested-action compact-btn", 38, true);
    open.set_tooltip_text(Some(&trf("Open {url} in browser", &[("url", &url)])));
    open.connect_clicked(move |_| {
        open_browser_url(url);
    });
    bb.append(&open);
    let copy = icon_button("edit-copy-symbolic", "Copy", "secondary-btn compact-btn", 38, true);
    copy.set_tooltip_text(Some(&trf("Copy {url} to clipboard", &[("url", &url)])));
    let w = win.clone();
    copy.connect_clicked(move |_| {
        if copy_to_clipboard(url) {
            w.show_toast("Link copied to clipboard!");
        }
    });
    bb.append(&copy);
    c
}
