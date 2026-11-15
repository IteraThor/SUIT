//! Tailscale authentication dialog: QR code for phones, open/copy link, and polling until paired.
use crate::core::i18n::{tr, trf};
use crate::core::tailscale;
use crate::ui::helpers::{copy_to_clipboard, every, generate_qr_texture, icon_button, open_browser_url, run_async};
use crate::ui::Win;
use adw::prelude::*;
use std::cell::{Cell, RefCell};
use std::rc::Rc;

pub fn present(win: &Win, initial_url: Option<String>, on_finished: Rc<dyn Fn(bool, String)>) {
    let dlg = adw::Window::new();
    dlg.set_transient_for(Some(&win.window));
    dlg.set_modal(true);
    dlg.set_title(Some(&tr("Tailscale Authentication")));
    dlg.set_default_size(480, 560);
    let closed = Rc::new(Cell::new(false));
    let connected = Rc::new(Cell::new(false));
    let polling = Rc::new(Cell::new(false));
    let url: Rc<RefCell<Option<String>>> = Rc::new(RefCell::new(initial_url.clone()));
    let c2 = closed.clone();
    dlg.connect_close_request(move |_| {
        c2.set(true);
        gtk::glib::Propagation::Proceed
    });
    let tv = adw::ToolbarView::new();
    dlg.set_content(Some(&tv));
    tv.add_top_bar(&adw::HeaderBar::new());
    let content = gtk::Box::new(gtk::Orientation::Vertical, 14);
    content.set_margin_top(12);
    content.set_margin_bottom(16);
    content.set_margin_start(24);
    content.set_margin_end(24);
    tv.set_content(Some(&content));
    let ib = gtk::Box::new(gtk::Orientation::Vertical, 6);
    ib.set_halign(gtk::Align::Center);
    content.append(&ib);
    let img = gtk::Image::from_icon_name("network-vpn-symbolic");
    img.set_pixel_size(44);
    ib.append(&img);
    let title = gtk::Label::new(Some(&tr("Connect to Tailscale")));
    title.add_css_class("title-2");
    ib.append(&title);
    let desc = gtk::Label::new(Some(&tr("Scan the QR code with your phone or open the link below to authenticate.")));
    desc.set_wrap(true);
    desc.set_justify(gtk::Justification::Center);
    desc.add_css_class("dim-label");
    content.append(&desc);
    let qb = gtk::Box::new(gtk::Orientation::Vertical, 0);
    qb.add_css_class("qr-card");
    qb.set_halign(gtk::Align::Center);
    let qp = gtk::Picture::new();
    qp.set_size_request(160, 160);
    qp.set_can_shrink(false);
    qb.append(&qp);
    content.append(&qb);
    let bb = gtk::Box::new(gtk::Orientation::Horizontal, 10);
    bb.set_halign(gtk::Align::Center);
    content.append(&bb);
    let b_open = icon_button("web-browser-symbolic", "Open in Browser", "suggested-action", 40, false);
    b_open.set_sensitive(false);
    bb.append(&b_open);
    let b_copy = icon_button("edit-copy-symbolic", "Copy Link", "secondary-btn", 40, false);
    b_copy.set_sensitive(initial_url.is_some());
    bb.append(&b_copy);
    let entry = gtk::Entry::new();
    entry.set_editable(false);
    entry.set_hexpand(true);
    entry.set_text(&initial_url.clone().unwrap_or_else(|| tr("Retrieving link...")));
    content.append(&entry);
    let sb = gtk::Box::new(gtk::Orientation::Horizontal, 10);
    sb.set_halign(gtk::Align::Center);
    sb.set_margin_top(6);
    content.append(&sb);
    let spin = adw::Spinner::new();
    sb.append(&spin);
    let status = gtk::Label::new(Some(&tr("Waiting for authentication...")));
    status.add_css_class("caption");
    sb.append(&status);
    let bottom = gtk::Box::new(gtk::Orientation::Horizontal, 10);
    bottom.set_halign(gtk::Align::End);
    bottom.set_margin_top(4);
    content.append(&bottom);
    let b_close = gtk::Button::with_label(&tr("Cancel"));
    b_close.set_size_request(90, 38);
    let d = dlg.clone();
    b_close.connect_clicked(move |_| d.close());
    bottom.append(&b_close);

    let ready = {
        let (url, entry, b_open, b_copy, qp, qb, status) = (url.clone(), entry.clone(), b_open.clone(), b_copy.clone(), qp.clone(), qb.clone(), status.clone());
        Rc::new(move |u: String| {
            entry.set_text(&u);
            b_open.set_sensitive(true);
            b_copy.set_sensitive(true);
            match generate_qr_texture(&u) {
                Some(t) => {
                    qp.set_paintable(Some(&t));
                    qb.set_visible(true);
                }
                None => qb.set_visible(false),
            }
            status.set_label(&tr("Waiting for authentication..."));
            *url.borrow_mut() = Some(u);
        })
    };
    match initial_url {
        Some(u) => ready(u),
        None => {
            qb.set_visible(false);
            status.set_label(&tr("Generating authentication link from Tailscale..."));
            let (r, cl, st, sp) = (ready.clone(), closed.clone(), status.clone(), spin.clone());
            run_async(tailscale::get_tailscale_auth_url, move |u| {
                if cl.get() {
                    return;
                }
                match u {
                    Some(u) => r(u),
                    None => {
                        st.set_label(&tr("Could not retrieve authentication link. Check Tailscale service."));
                        sp.set_visible(false);
                    }
                }
            });
        }
    }
    let u2 = url.clone();
    b_open.connect_clicked(move |_| {
        if let Some(u) = u2.borrow().as_ref() {
            open_browser_url(u);
        }
    });
    let (u3, st) = (url.clone(), status.clone());
    b_copy.connect_clicked(move |_| {
        if let Some(u) = u3.borrow().as_ref() {
            copy_to_clipboard(u);
            st.set_label(&tr("Link copied to clipboard! Paste it into your browser."));
        }
    });
    every(2000, move || {
        if closed.get() || connected.get() {
            return false;
        }
        if polling.replace(true) {
            return true;
        }
        let (pl, cn, cl) = (polling.clone(), connected.clone(), closed.clone());
        let (spin, qb, b_open, b_copy, entry, img, title, desc, status, b_close, of) =
            (spin.clone(), qb.clone(), b_open.clone(), b_copy.clone(), entry.clone(), img.clone(), title.clone(), desc.clone(), status.clone(), b_close.clone(), on_finished.clone());
        run_async(tailscale::get_tailscale_status, move |s| {
            pl.set(false);
            if cl.get() || cn.get() || !s.running {
                return;
            }
            cn.set(true);
            spin.set_visible(false);
            qb.set_visible(false);
            b_open.set_visible(false);
            b_copy.set_visible(false);
            entry.set_visible(false);
            img.set_icon_name(Some("emblem-ok-symbolic"));
            title.set_label(&tr("Tailscale Connected!"));
            let ip = s.ips.first().cloned().unwrap_or_default();
            desc.set_label(&trf("Connected to Tailscale.\nIP: {ip} ({host})", &[("ip", &ip), ("host", &s.hostname)]));
            status.set_label(&tr("Successfully authenticated"));
            status.remove_css_class("caption");
            status.add_css_class("status-pill-running");
            b_close.set_label(&tr("Done"));
            b_close.add_css_class("suggested-action");
            of(true, trf("Tailscale connected: {ip}", &[("ip", &ip)]));
        });
        true
    });
    dlg.present();
}
