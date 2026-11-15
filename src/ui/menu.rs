//! Main menu: banner with language switch and Community button, module rows, and the update check.
use super::helpers::{alert, ask, esc, page_shell, run_async};
use super::{Page, Win};
use crate::core::i18n::{get_language, tr, trf};
use crate::core::{autoglow, update, util};
use adw::prelude::*;
use gtk::{gdk, gdk_pixbuf, glib};
use std::cell::Cell;
use std::rc::Rc;

/// 5:3, so the German stripes land on whole pixels
const FLAG_SIZE: (i32, i32) = (25, 15);
const FLAG_OVERSAMPLE: i32 = 4;

pub struct MenuPage {
    nav: adw::NavigationPage,
    win: Win,
    btn_update: gtk::Button,
    badge_new: gtk::Label,
    badge_update: gtk::Label,
    update_available: Cell<bool>,
}

impl Page for MenuPage {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }

    fn set_flag(&self, name: &str, on: bool) {
        if name == "autoglow_update" {
            // Show UPDATE instead of NEW on the AutoGlow 2 row
            self.badge_update.set_visible(on);
            self.badge_new.set_visible(!on);
        }
    }
}

impl MenuPage {
    pub fn new(win: Win) -> Rc<MenuPage> {
        let (scrolled, main) = page_shell();
        let nav = adw::NavigationPage::builder().title(tr("SUIT for Fedora")).tag("main_menu").child(&scrolled).build();

        // Header banner card with logo
        let banner = gtk::Box::new(gtk::Orientation::Vertical, 2);
        banner.add_css_class("card");
        banner.set_margin_bottom(2);
        let inner = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        inner.set_margin_top(6);
        inner.set_margin_bottom(6);
        inner.set_margin_start(12);
        inner.set_margin_end(12);
        let logo = util::project_dir().join("assets/icons/suit-icon.png");
        if logo.exists() {
            let img = gtk::Image::from_file(&logo);
            img.set_pixel_size(52);
            img.set_valign(gtk::Align::Center);
            inner.append(&img);
        }
        let text = gtk::Box::new(gtk::Orientation::Vertical, 0);
        text.set_valign(gtk::Align::Center);
        text.set_hexpand(true);
        let t = gtk::Label::new(Some(&tr("SUIT for Fedora")));
        t.set_xalign(0.0);
        t.add_css_class("title-3");
        text.append(&t);
        let st = gtk::Label::new(Some(&tr("Setup Utilities by IteraThor for Autodarts")));
        st.set_xalign(0.0);
        st.add_css_class("dim-label");
        text.append(&st);
        inner.append(&text);
        inner.append(&language_switch(&win));

        let btn_comm = gtk::Button::new();
        btn_comm.set_valign(gtk::Align::Center);
        btn_comm.add_css_class("compact-btn");
        btn_comm.add_css_class("secondary-btn");
        btn_comm.set_tooltip_text(Some(&tr("IteraThor Community, Discord & 3D Print Models")));
        let cb = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        cb.set_halign(gtk::Align::Center);
        let qr = gtk::Image::from_icon_name("qr-code-symbolic");
        qr.set_pixel_size(16);
        cb.append(&qr);
        let lc = gtk::Label::new(Some(&tr("Community")));
        lc.add_css_class("heading");
        cb.append(&lc);
        btn_comm.set_child(Some(&cb));
        let w = win.clone();
        btn_comm.connect_clicked(move |_| super::dialogs::community::present(&w));
        inner.append(&btn_comm);
        banner.append(&inner);
        main.append(&banner);

        let group = adw::PreferencesGroup::new();
        main.append(&group);
        let badge_new = gtk::Label::new(Some(&tr("NEW")));
        badge_new.add_css_class("status-pill-new");
        badge_new.set_valign(gtk::Align::Center);
        let badge_update = gtk::Label::new(Some(&tr("UPDATE")));
        badge_update.add_css_class("status-pill-update");
        badge_update.set_valign(gtk::Align::Center);
        badge_update.set_tooltip_text(Some(&tr("A new AutoGlow 2 version is available")));
        badge_update.set_visible(false);

        type Open = fn(&Win);
        let items: [(&str, &str, &str, &str, Open); 6] = [
            ("system", "preferences-system-symbolic", "System Utilities & Tweaks", "Performance, power, and touch tweaks.", |w| w.open_system_page()),
            ("autodarts", "darts-symbolic", "Autodarts Setup", "Board pairing, service, and camera telemetry.", |w| w.open_autodarts_page()),
            ("camera_tools", "camera-photo-symbolic", "Camera Tools", "Focus calibration, camera controls, and the camera optimizer.", |w| w.open_camera_tools_page()),
            ("touch", "screen-touch-symbolic", "Screen & Touch Manager", "Display rotation and touch calibration.", |w| w.open_rotation_page()),
            ("kiosk", "view-fullscreen-symbolic", "Kiosk Mode", "Fullscreen browser autostart and controls.", |w| w.open_kiosk_page()),
            ("autoglow", "display-brightness-symbolic", "AutoGlow 2 Manager", "WLED dartboard automation and lighting effects.", |w| w.open_autoglow_page()),
        ];
        for (key, icon, title, sub, open) in items {
            let row = adw::ActionRow::new();
            row.set_title(&esc(&tr(title)));
            row.set_subtitle(&esc(&tr(sub)));
            let img = gtk::Image::from_icon_name(icon);
            img.set_pixel_size(24);
            img.set_margin_end(10);
            img.set_valign(gtk::Align::Center);
            row.add_prefix(&img);
            row.set_activatable(true);
            let w = win.clone();
            row.connect_activated(move |_| {
                log::debug!("Navigating to {} view", key);
                open(&w)
            });
            if key == "autoglow" {
                row.add_suffix(&badge_new);
                row.add_suffix(&badge_update);
            }
            let chev = gtk::Image::from_icon_name("go-next-symbolic");
            chev.set_opacity(0.5);
            chev.set_valign(gtk::Align::Center);
            row.add_suffix(&chev);
            group.add(&row);
        }

        let ug = gtk::Box::new(gtk::Orientation::Vertical, 0);
        ug.set_margin_top(4);
        main.append(&ug);
        let btn_update = gtk::Button::new();
        btn_update.add_css_class("suggested-action");
        btn_update.add_css_class("touch-btn");
        ug.append(&btn_update);

        let me = Rc::new(MenuPage { nav, win, btn_update, badge_new, badge_update, update_available: Cell::new(false) });
        me.set_loading(false, "");
        let m = me.clone();
        me.btn_update.connect_clicked(move |_| m.on_check_updates());
        let m = me.clone();
        glib::idle_add_local_once(move || m.check_updates_on_start());
        me
    }

    fn set_loading(&self, loading: bool, text: &str) {
        self.btn_update.set_sensitive(!loading);
        let bx = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        bx.set_halign(gtk::Align::Center);
        bx.set_valign(gtk::Align::Center);
        if loading {
            bx.append(&adw::Spinner::new());
            let l = gtk::Label::new(Some(if text.is_empty() { tr("Checking for Updates...") } else { text.to_string() }.as_str()));
            l.add_css_class("heading");
            bx.append(&l);
        } else {
            let i = gtk::Image::from_icon_name("software-update-available-symbolic");
            i.set_pixel_size(22);
            bx.append(&i);
            let l = gtk::Label::new(Some(&tr(if self.update_available.get() { "Update Available" } else { "Check for Updates" })));
            l.add_css_class("heading");
            bx.append(&l);
        }
        self.btn_update.set_child(Some(&bx));
    }

    fn set_update_available(&self, a: bool) {
        self.update_available.set(a);
        if a {
            self.btn_update.remove_css_class("suggested-action");
            self.btn_update.add_css_class("update-available-btn");
        } else {
            self.btn_update.remove_css_class("update-available-btn");
            self.btn_update.add_css_class("suggested-action");
        }
        self.set_loading(false, "");
    }

    /// Quiet background check at launch; only recolors the button when an update exists.
    fn check_updates_on_start(self: &Rc<Self>) {
        let m = self.clone();
        run_async(|| update::check_update(None), move |i| {
            if i.has_update {
                m.set_update_available(true);
            }
        });
        let w = self.win.clone();
        run_async(
            // Only SUIT's own git install can update; nothing to ask GitHub otherwise
            || autoglow::get_install_dir().map(|_| autoglow::check_update().has_update).unwrap_or(false),
            move |a| w.set_autoglow_update(a),
        );
    }

    fn on_check_updates(self: &Rc<Self>) {
        log::info!("Check for updates and requirements triggered");
        self.set_loading(true, &tr("Checking for Updates..."));
        let m = self.clone();
        run_async(
            || (update::check_requirements(), update::check_update(None)),
            move |(missing, info)| {
                m.set_loading(false, "");
                if !missing.is_empty() {
                    m.show_missing_requirements(missing, Some(info));
                    return;
                }
                if info.error.is_none() {
                    m.set_update_available(info.has_update);
                }
                m.process_update_info(&info);
            },
        );
    }

    fn show_missing_requirements(self: &Rc<Self>, missing: Vec<String>, next: Option<update::UpdateInfo>) {
        let pk = missing.iter().map(|p| format!("• {}", p)).collect::<Vec<_>>().join("\n");
        let d = alert(
            &tr("Requirements Not Installed"),
            &trf("The following required packages are not installed:\n\n{pkgs}\n\nInstall missing requirements now?", &[("pkgs", &pk)]),
            &[("cancel", &tr("Later")), ("install", &tr("Install"))],
            Some("install"),
            None,
        );
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            if r != "install" {
                return;
            }
            m.win.show_toast("Installing requirements...");
            m.set_loading(true, &tr("Installing..."));
            let (m2, miss, nx) = (m.clone(), missing.clone(), next.clone());
            run_async(move || update::install_requirements(Some(miss)), move |(ok, msg)| {
                m2.set_loading(false, "");
                m2.win.show_toast(&msg);
                if let (true, Some(n)) = (ok, nx) {
                    m2.process_update_info(&n);
                }
            });
        });
    }

    fn process_update_info(self: &Rc<Self>, info: &update::UpdateInfo) {
        if let Some(e) = &info.error {
            self.win.show_toast(e);
            return;
        }
        if !info.has_update {
            self.win.show_toast("SUIT for Fedora is up to date.");
            return;
        }
        let mut body = if info.commits_behind == 1 {
            tr("A new version of SUIT is available (1 update).")
        } else {
            trf("A new version of SUIT is available ({count} updates).", &[("count", &info.commits_behind)])
        };
        if !info.latest_message.is_empty() {
            body += &format!("\n\n{}", trf("Latest: {msg}", &[("msg", &info.latest_message)]));
        }
        body += &format!("\n\n{}", tr("Install the latest updates now?"));
        let d = alert(&tr("Update Available"), &body, &[("cancel", &tr("Later")), ("update", &tr("Update Now"))], Some("update"), None);
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            if r == "update" {
                m.perform_update();
            }
        });
    }

    fn perform_update(self: &Rc<Self>) {
        self.win.show_toast("Downloading and applying update...");
        self.set_loading(true, &tr("Updating SUIT..."));
        let m = self.clone();
        run_async(|| update::apply_update(None), move |(ok, msg)| {
            m.set_loading(false, "");
            if !ok {
                m.win.show_toast(&msg);
                return;
            }
            m.set_update_available(false);
            let d = alert(
                &tr("Update Complete"),
                &tr("SUIT has been successfully updated.\n\nRestart now to apply the changes?"),
                &[("later", &tr("Restart Later")), ("restart", &tr("Restart SUIT"))],
                Some("restart"),
                None,
            );
            ask(&m.win.window, d, |r| {
                if r == "restart" {
                    update::restart_application();
                }
            });
        });
    }
}

/// English / German flag toggles.
fn language_switch(win: &Win) -> gtk::Box {
    let bx = gtk::Box::new(gtk::Orientation::Vertical, 0);
    bx.set_valign(gtk::Align::Center);
    bx.set_margin_end(-8); // sits right against the Community button
    let flags = util::project_dir().join("assets/flags");
    let mut first: Option<gtk::ToggleButton> = None;
    for (lang, tip) in [("en", "English"), ("de", "Deutsch")] {
        let b = gtk::ToggleButton::new();
        b.add_css_class("flat");
        b.add_css_class("lang-flag-btn");
        b.set_valign(gtk::Align::Center);
        b.set_tooltip_text(Some(tip));
        // rendered at FLAG_OVERSAMPLE x its size and scaled down, so it stays crisp
        if let Ok(pb) = gdk_pixbuf::Pixbuf::from_file_at_scale(
            flags.join(format!("{}.svg", lang)),
            FLAG_SIZE.0 * FLAG_OVERSAMPLE,
            FLAG_SIZE.1 * FLAG_OVERSAMPLE,
            false,
        ) {
            #[allow(deprecated)]
            let tex = gdk::Texture::for_pixbuf(&pb);
            let pic = gtk::Picture::for_paintable(&tex);
            pic.set_can_shrink(true);
            pic.set_size_request(FLAG_SIZE.0, FLAG_SIZE.1);
            pic.set_halign(gtk::Align::Center);
            pic.set_valign(gtk::Align::Center);
            b.set_child(Some(&pic));
        } else {
            b.set_label(&lang.to_uppercase());
        }
        match &first {
            Some(f) => b.set_group(Some(f)),
            None => first = Some(b.clone()),
        }
        b.set_active(lang == get_language());
        let w = win.clone();
        b.connect_toggled(move |btn| {
            if btn.is_active() && lang != get_language() {
                log::info!("Switching UI language to {}", lang);
                // Rebuild after the toggle signal returns: the rebuild replaces this page
                let w2 = w.clone();
                glib::idle_add_local_once(move || w2.set_language(lang));
            }
        });
        bx.append(&b);
    }
    bx
}
