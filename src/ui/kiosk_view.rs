//! Kiosk Mode: fullscreen Chromium on login, the In-Page Controls extension and the dartboard light button.
use super::camera_widgets::{opts, segmented};
use super::helpers::{group, icon_button, page_shell, run_async};
use super::{Page, Win};
use crate::core::i18n::{tr, trf};
use crate::core::light::{self, LightConfig};
use crate::core::{debloat, kiosk, util};
use adw::prelude::*;
use std::cell::{Cell, RefCell};
use std::rc::Rc;

pub const DEFAULT_URL: &str = "https://play.autodarts.com/";

pub struct KioskView {
    nav: adw::NavigationPage,
    win: Win,
    loading: Cell<bool>,
    light_type: RefCell<String>,
    row_url: adw::EntryRow,
    row_autostart: adw::SwitchRow,
    row_chromium: adw::ActionRow,
    row_ext: adw::ActionRow,
    btn_ext: gtk::Button,
    grp_light: adw::PreferencesGroup,
    row_light_enable: adw::SwitchRow,
    row_light_device: adw::ActionRow,
    light_seg: RefCell<Option<adw::ToggleGroup>>,
    row_light_ip: adw::EntryRow,
    row_light_test: adw::ActionRow,
    btn_on: gtk::Button,
    btn_off: gtk::Button,
}

impl Page for KioskView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }

    fn refresh(self: Rc<Self>) {
        self.loading.set(true);
        self.row_autostart.set_active(kiosk::is_autostart_enabled());
        self.loading.set(false);
        if kiosk::is_permanent_extension_installed() {
            self.row_ext.set_subtitle(&tr("Active in Chromium."));
            self.btn_ext.set_label(&tr("Remove"));
            self.btn_ext.remove_css_class("suggested-action");
            self.btn_ext.add_css_class("secondary-btn");
        } else {
            self.row_ext.set_subtitle(&tr("Adds power, restart and board config to the Autodarts header."));
            self.btn_ext.set_label(&tr("Install"));
            self.btn_ext.remove_css_class("secondary-btn");
            self.btn_ext.add_css_class("suggested-action");
        }
        let chrom = debloat::is_app_installed("chromium");
        self.row_chromium.set_visible(!chrom);
        self.row_ext.set_sensitive(chrom);
        self.grp_light.set_sensitive(chrom);
        let c = light::read_config();
        self.loading.set(true);
        self.row_light_enable.set_active(c.light_enabled);
        let t = if c.light_device_type == "smart_plug" { "tasmota".to_string() } else { c.light_device_type.clone() };
        if let Some(s) = self.light_seg.borrow().as_ref() {
            s.set_active_name(Some(&t));
        }
        *self.light_type.borrow_mut() = t;
        self.row_light_ip.set_text(&c.light_ip);
        self.update_light_sensitivity(c.light_enabled);
        self.loading.set(false);
    }
}

impl KioskView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let (scrolled, main) = page_shell();
        let nav = adw::NavigationPage::builder().title(tr("Kiosk Mode")).tag("kiosk").child(&scrolled).build();
        let gk = group(&tr("Kiosk"), "");
        let btn_launch = icon_button("media-playback-start-symbolic", "Launch", "suggested-action compact-btn", 36, false);
        btn_launch.set_tooltip_text(Some(&tr("Open the kiosk session now")));
        gk.set_header_suffix(Some(&btn_launch));
        main.append(&gk);
        let row_url = adw::EntryRow::new();
        row_url.set_title(&tr("Address"));
        row_url.set_text(DEFAULT_URL);
        let reset = gtk::Button::from_icon_name("edit-undo-symbolic");
        reset.set_valign(gtk::Align::Center);
        reset.add_css_class("flat");
        reset.set_tooltip_text(Some(&tr("Reset to play.autodarts.com")));
        let ru = row_url.clone();
        reset.connect_clicked(move |_| ru.set_text(DEFAULT_URL));
        row_url.add_suffix(&reset);
        gk.add(&row_url);
        let row_autostart = super::helpers::switch_row("Start on Login", "");
        gk.add(&row_autostart);
        let ge = group(&tr("In-Page Controls"), "");
        main.append(&ge);
        let row_chromium = super::helpers::action_row(&tr("Chromium Required"), &tr("The in-page controls run in Chromium."));
        let btn_chrom = gtk::Button::with_label(&tr("Install"));
        btn_chrom.set_valign(gtk::Align::Center);
        btn_chrom.add_css_class("suggested-action");
        btn_chrom.add_css_class("compact-btn");
        row_chromium.add_suffix(&btn_chrom);
        row_chromium.set_visible(false);
        ge.add(&row_chromium);
        let row_ext = super::helpers::action_row(&tr("Chromium Extension"), &tr("Checking..."));
        let btn_ext = gtk::Button::with_label(&tr("Install"));
        btn_ext.set_valign(gtk::Align::Center);
        btn_ext.add_css_class("compact-btn");
        row_ext.add_suffix(&btn_ext);
        ge.add(&row_ext);
        let gl = group(&tr("Dartboard Light"), "");
        main.append(&gl);
        let row_light_enable = super::helpers::switch_row("Light Button", "");
        gl.add(&row_light_enable);
        let row_light_device = super::helpers::action_row(&tr("Device"), "");
        gl.add(&row_light_device);
        let row_light_ip = adw::EntryRow::new();
        row_light_ip.set_title(&tr("IP Address"));
        gl.add(&row_light_ip);
        let row_light_test = super::helpers::action_row(&tr("Test"), "");
        let bt = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        bt.set_valign(gtk::Align::Center);
        let btn_on = gtk::Button::with_label(&tr("On"));
        let btn_off = gtk::Button::with_label(&tr("Off"));
        for b in [&btn_on, &btn_off] {
            b.add_css_class("secondary-btn");
            b.add_css_class("compact-btn");
            b.set_size_request(64, -1);
            bt.append(b);
        }
        row_light_test.add_suffix(&bt);
        gl.add(&row_light_test);
        let me = Rc::new(KioskView {
            nav,
            win,
            loading: Cell::new(false),
            light_type: RefCell::new("wled".into()),
            row_url,
            row_autostart,
            row_chromium,
            row_ext,
            btn_ext,
            grp_light: gl,
            row_light_enable,
            row_light_device,
            light_seg: RefCell::new(None),
            row_light_ip,
            row_light_test,
            btn_on,
            btn_off,
        });
        let m = Rc::downgrade(&me);
        let seg = segmented(
            &opts(&[("wled", "WLED"), ("tasmota", "Tasmota"), ("shelly", "Shelly")]),
            "wled",
            Rc::new(move |t: String| {
                if let Some(m) = m.upgrade() {
                    *m.light_type.borrow_mut() = t;
                    m.save_light();
                }
            }),
        );
        me.row_light_device.add_suffix(&seg);
        *me.light_seg.borrow_mut() = Some(seg);
        let m = me.clone();
        btn_launch.connect_clicked(move |_| m.launch());
        let m = me.clone();
        me.row_autostart.connect_active_notify(move |r| {
            if m.loading.get() {
                return;
            }
            let a = r.is_active();
            let url = m.url();
            kiosk::set_autostart(a, &url, &kiosk::resolve_browser(None), true);
            m.win.show_toast(if a { "Kiosk autostart enabled." } else { "Kiosk autostart disabled." });
        });
        let m = me.clone();
        btn_chrom.connect_clicked(move |b| {
            b.set_sensitive(false);
            b.set_label(&tr("Installing..."));
            m.win.show_toast("Installing Chromium in background...");
            let (m2, b2) = (m.clone(), b.clone());
            run_async(debloat::install_chromium, move |(_, msg)| {
                m2.win.show_toast(&msg);
                b2.set_label(&tr("Install"));
                b2.set_sensitive(true);
                m2.clone().refresh();
            });
        });
        let m = me.clone();
        me.btn_ext.connect_clicked(move |b| {
            b.set_sensitive(false);
            let inst = kiosk::is_permanent_extension_installed();
            let (m2, b2) = (m.clone(), b.clone());
            run_async(
                move || if inst { kiosk::uninstall_permanent_extension() } else { kiosk::install_permanent_extension() },
                move |(_, msg)| {
                    b2.set_sensitive(true);
                    m2.clone().refresh();
                    m2.win.show_toast(&msg);
                },
            );
        });
        let m = me.clone();
        me.row_light_enable.connect_active_notify(move |r| {
            let en = r.is_active();
            m.update_light_sensitivity(en);
            m.save_light();
            if !m.loading.get() {
                m.win.show_toast(if en { "Dartboard light button enabled." } else { "Dartboard light button disabled." });
            }
        });
        let m = me.clone();
        me.row_light_ip.connect_text_notify(move |_| m.save_light());
        for (b, on) in [(me.btn_on.clone(), true), (me.btn_off.clone(), false)] {
            let m = me.clone();
            b.connect_clicked(move |_| m.test_light(on));
        }
        let m = me.clone();
        me.nav.connect_map(move |_| m.clone().refresh());
        me
    }

    fn url(&self) -> String {
        let u = self.row_url.text().trim().to_string();
        if u.is_empty() { DEFAULT_URL.into() } else { u }
    }

    fn update_light_sensitivity(&self, en: bool) {
        self.row_light_device.set_sensitive(en);
        self.row_light_ip.set_sensitive(en);
        self.row_light_test.set_sensitive(en);
    }

    fn save_light(&self) {
        if self.loading.get() {
            return;
        }
        light::save_config(self.row_light_enable.is_active(), &self.light_type.borrow(), self.row_light_ip.text().trim(), 80);
        kiosk::sync_extension_files();
    }

    fn test_light(self: &Rc<Self>, on: bool) {
        let ip = self.row_light_ip.text().trim().to_string();
        if ip.is_empty() {
            self.win.show_toast("Please enter a device IP address first.");
            return;
        }
        self.btn_on.set_sensitive(false);
        self.btn_off.set_sensitive(false);
        self.row_light_test.set_subtitle(&tr("Sending..."));
        let cfg = LightConfig { light_enabled: true, light_device_type: self.light_type.borrow().clone(), light_ip: ip, light_port: 80 };
        let m = self.clone();
        run_async(move || light::set_power(on, Some(&cfg), 1.2), move |(ok, is_on, msg)| {
            m.btn_on.set_sensitive(true);
            m.btn_off.set_sensitive(true);
            if ok {
                m.row_light_test.set_subtitle(&tr(if is_on { "Connected. Light is on." } else { "Connected. Light is off." }));
            } else {
                let e = trf("Error: {msg}", &[("msg", &tr(&msg))]);
                m.row_light_test.set_subtitle(&super::helpers::esc(&e));
                m.win.show_toast(&e);
            }
        });
    }

    fn launch(&self) {
        let url = self.url();
        let browser = kiosk::resolve_browser(None);
        kiosk::ensure_native_messaging_host(None);
        kiosk::clean_stale_singleton_locks();
        let l = kiosk::ensure_launcher_script();
        let cmd = format!("{} {} {} {}", l.display(), browser, kiosk::get_kiosk_flags(true).join(" "), util::shquote(&url));
        log::info!("Launching Kiosk: {}", cmd);
        let _ = std::process::Command::new("bash").args(["-c", &cmd]).spawn();
        self.win.show_toast("Kiosk session launched.");
    }
}
