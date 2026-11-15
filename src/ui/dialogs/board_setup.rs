//! Console dialog embedding the official Autodarts terminal UI ('ad') in a VTE terminal.
use crate::core::autodarts::get_autodarts_cli_binary;
use crate::core::i18n::tr;
use crate::core::util::home;
use adw::prelude::*;
use gtk::{gdk, glib, pango};
use std::cell::Cell;
use std::rc::Rc;
use vte::prelude::*;

pub fn present(parent: &gtk::Window, on_closed: Option<Rc<dyn Fn()>>) {
    let dlg = adw::Window::new();
    dlg.set_modal(true);
    dlg.set_title(Some(&tr("Autodarts Console")));
    dlg.set_transient_for(Some(parent));
    dlg.set_default_size(740, 520);
    dlg.set_size_request(600, 420);
    let tv = adw::ToolbarView::new();
    dlg.set_content(Some(&tv));
    let hb = adw::HeaderBar::new();
    hb.set_show_end_title_buttons(true);
    let restart = gtk::Button::from_icon_name("view-refresh-symbolic");
    restart.set_tooltip_text(Some(&tr("Restart Console")));
    hb.pack_end(&restart);
    tv.add_top_bar(&hb);
    let bx = gtk::Box::new(gtk::Orientation::Vertical, 0);
    for m in [8] {
        bx.set_margin_top(m);
        bx.set_margin_bottom(m);
        bx.set_margin_start(m);
        bx.set_margin_end(m);
    }
    bx.add_css_class("terminal-card");
    bx.set_vexpand(true);
    let term = vte::Terminal::new();
    term.set_font(Some(&pango::FontDescription::from_string("Monospace 10.5")));
    term.set_cursor_blink_mode(vte::CursorBlinkMode::Off);
    term.set_scrollback_lines(2000);
    term.set_mouse_autohide(true);
    term.set_vexpand(true);
    term.set_hexpand(true);
    let fg = gdk::RGBA::parse("#cdd6f4").unwrap();
    let bg = gdk::RGBA::parse("#181825").unwrap();
    term.set_colors(Some(&fg), Some(&bg), &[]);
    let sc = gtk::ScrolledWindow::new();
    sc.set_vexpand(true);
    sc.set_hexpand(true);
    sc.set_child(Some(&term));
    bx.append(&sc);
    tv.set_content(Some(&bx));
    let pid: Rc<Cell<Option<glib::Pid>>> = Rc::new(Cell::new(None));
    let spawn = {
        let (term, pid) = (term.clone(), pid.clone());
        Rc::new(move |force: bool| {
            let Some(bin) = get_autodarts_cli_binary() else {
                log::warn!("Autodarts CLI binary not found");
                return;
            };
            if let Some(p) = pid.get() {
                if !force {
                    return;
                }
                unsafe { libc::kill(p.0, libc::SIGKILL) };
                pid.set(None);
            }
            term.reset(true, true);
            let (t2, p2) = (term.clone(), pid.clone());
            let h = home().to_string_lossy().to_string();
            term.spawn_async(
                vte::PtyFlags::DEFAULT,
                Some(&h),
                &[&bin],
                &[],
                glib::SpawnFlags::DEFAULT,
                || {},
                -1,
                gtk::gio::Cancellable::NONE,
                move |r| match r {
                    Ok(p) => {
                        p2.set(Some(p));
                        t2.grab_focus();
                    }
                    Err(e) => log::error!("Failed to spawn console dialog: {}", e),
                },
            );
        })
    };
    let s = spawn.clone();
    restart.connect_clicked(move |_| s(true));
    let p = pid.clone();
    dlg.connect_close_request(move |_| {
        if let Some(pp) = p.take() {
            unsafe { libc::kill(pp.0, libc::SIGKILL) };
        }
        if let Some(cb) = &on_closed {
            cb();
        }
        glib::Propagation::Proceed
    });
    let s = spawn.clone();
    glib::idle_add_local_once(move || s(false));
    dlg.present();
}
