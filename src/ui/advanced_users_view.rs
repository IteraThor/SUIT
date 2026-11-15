//! Advanced utilities: Desktop Sharing, Tailscale VPN, and the Android Darts Scorer via Waydroid.
use super::helpers::{copy_to_clipboard, group, icon_button, page_shell, run_async, switch_row, BadgeOrButton};
use super::{Page, Win};
use crate::core::i18n::{tr, trf};
use crate::core::{remote_desktop, tailscale, util, waydroid};
use adw::prelude::*;
use std::cell::{Cell, RefCell};
use std::rc::Rc;

pub struct AdvancedUsersView {
    nav: adw::NavigationPage,
    win: Win,
    updating_rdp: Cell<bool>,
    rdp_info: RefCell<String>,
    row_sharing: adw::SwitchRow,
    row_control: adw::SwitchRow,
    row_rdp_info: adw::ActionRow,
    row_tailscale: adw::ActionRow,
    tail: BadgeOrButton,
    grp_waydroid: adw::PreferencesGroup,
    row_scorer: adw::ActionRow,
    scorer: BadgeOrButton,
    scorer_installed: Cell<bool>,
    expander: adw::ExpanderRow,
}

impl Page for AdvancedUsersView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }

    fn refresh(self: Rc<Self>) {
        let m = self.clone();
        run_async(remote_desktop::get_remote_desktop_status, move |r| {
            m.updating_rdp.set(true);
            m.row_sharing.set_active(r.enabled);
            m.row_control.set_active(r.remote_control);
            m.row_control.set_sensitive(r.enabled);
            if r.enabled {
                let ip = if r.ip.is_empty() { r.hostname.clone() } else { r.ip.clone() };
                let user = if r.username.is_empty() { "autodarts".into() } else { r.username.clone() };
                let pw = if r.password.is_empty() { "autodarts".into() } else { r.password.clone() };
                m.row_rdp_info.set_visible(true);
                m.row_rdp_info.set_subtitle(&super::helpers::esc(&trf(
                    "Connect to {ip}:{port} (User: {user} / Password: {pw})",
                    &[("ip", &ip), ("port", &r.port), ("user", &user), ("pw", &pw)],
                )));
                *m.rdp_info.borrow_mut() = format!("Host: {}:{}\nUsername: {}\nPassword: {}", ip, r.port, user, pw);
            } else {
                m.row_rdp_info.set_visible(false);
                m.rdp_info.borrow_mut().clear();
            }
            m.updating_rdp.set(false);
        });
        let m = self.clone();
        run_async(tailscale::get_tailscale_status, move |t| {
            if !t.installed {
                m.row_tailscale.set_subtitle(&tr("Remote network access for board management away from home."));
                m.tail.show_button("software-update-available-symbolic", &tr("Install Tailscale"));
            } else if t.running && !t.ips.is_empty() {
                m.tail.show_badge(&trf("Connected: {ip}", &[("ip", &t.ips[0])]), "status-pill-running");
                m.row_tailscale.set_subtitle(&super::helpers::esc(&trf("Connected as {host}.", &[("host", &t.hostname)])));
            } else if t.state == "Stopped" {
                m.row_tailscale.set_subtitle(&tr("Installed but disconnected."));
                m.tail.show_button("network-vpn-symbolic", &tr("Connect"));
            } else {
                m.row_tailscale.set_subtitle(&tr("Authentication required to connect."));
                m.tail.show_button("network-vpn-symbolic", &tr("Sign In"));
            }
        });
        let m = self.clone();
        run_async(waydroid::get_waydroid_status, move |w| {
            let has_installer = waydroid::has_private_darts_scorer();
            m.scorer_installed.set(w.darts_scorer);
            if !has_installer && !w.darts_scorer {
                m.row_scorer.set_visible(false);
                m.grp_waydroid.set_title(&tr("Android Container (Waydroid)"));
            } else {
                m.row_scorer.set_visible(true);
                m.grp_waydroid.set_title(&tr("Darts Scorer"));
            }
            if w.darts_scorer {
                m.row_scorer.set_subtitle(&tr("Android scoreboard app running via Waydroid."));
                m.scorer.show_button("media-playback-start-symbolic", &tr("Launch Scorer"));
                m.scorer.button.remove_css_class("suggested-action");
                m.scorer.button.add_css_class("secondary-btn");
            } else if has_installer {
                m.row_scorer.set_subtitle(&tr("Android scoreboard app for local score tracking."));
                m.scorer.show_button("software-update-available-symbolic", &tr("1-Click Install"));
            }
            m.expander.set_visible(w.installed);
        });
    }
}

impl AdvancedUsersView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let (scrolled, main) = page_shell();
        let nav = adw::NavigationPage::builder().title(tr("Advanced Users")).tag("advanced_users").child(&scrolled).build();
        let gr = group(&tr("Desktop Sharing"), "");
        main.append(&gr);
        let row_sharing = switch_row("Desktop Sharing", "Allow connecting to this display from another device.");
        gr.add(&row_sharing);
        let row_control = switch_row("Remote Control", "Allow remote keyboard, mouse, and touch control.");
        gr.add(&row_control);
        let row_rdp_info = adw::ActionRow::new();
        row_rdp_info.set_title(&tr("Connection Details"));
        row_rdp_info.set_subtitle(&tr("Checking connection details..."));
        let ra = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        ra.set_valign(gtk::Align::Center);
        let b_copy = icon_button("edit-copy-symbolic", "Copy Info", "secondary-btn compact-btn", 34, false);
        let b_settings = icon_button("preferences-system-symbolic", "Settings", "secondary-btn compact-btn", 34, false);
        b_settings.connect_clicked(|_| {
            remote_desktop::open_gnome_sharing_settings();
        });
        ra.append(&b_copy);
        ra.append(&b_settings);
        row_rdp_info.add_suffix(&ra);
        gr.add(&row_rdp_info);
        let gt = group(&tr("Tailscale VPN"), "");
        main.append(&gt);
        let row_tailscale = super::helpers::action_row(&tr("Tailscale VPN"), &tr("Remote network access for board management."));
        let tail = BadgeOrButton::new("network-vpn-symbolic", "Install Tailscale", "suggested-action compact-btn", 34);
        row_tailscale.add_suffix(&tail.widget);
        gt.add(&row_tailscale);
        let gw = group(&tr("Darts Scorer"), "");
        main.append(&gw);
        let row_scorer = super::helpers::action_row(&tr("Darts Scorer App"), &tr("Android scoreboard app for local score tracking."));
        let scorer = BadgeOrButton::new("media-playback-start-symbolic", "Launch Scorer", "secondary-btn compact-btn", 38);
        row_scorer.add_suffix(&scorer.widget);
        gw.add(&row_scorer);
        let expander = adw::ExpanderRow::new();
        expander.set_title(&tr("Advanced Waydroid Options"));
        expander.set_subtitle(&tr("Diagnostics, debloat, and Android container settings."));
        gw.add(&expander);
        let me = Rc::new(AdvancedUsersView {
            nav,
            win: win.clone(),
            updating_rdp: Cell::new(false),
            rdp_info: RefCell::new(String::new()),
            row_sharing,
            row_control,
            row_rdp_info,
            row_tailscale,
            tail,
            grp_waydroid: gw,
            row_scorer,
            scorer,
            scorer_installed: Cell::new(false),
            expander,
        });
        type Act = fn(&Rc<AdvancedUsersView>);
        let subs: [(&str, &str, &str, &str, Act); 3] = [
            ("Waydroid Debloat", "Clean unused LineageOS apps to save memory and CPU.", "view-refresh-symbolic", "Run Debloat", |m| {
                m.win.show_toast("Running Waydroid debloat and seamless multi-window setup...");
                let m2 = m.clone();
                run_async(waydroid::waydroid_debloat_and_seamless, move |(_, msg)| {
                    m2.win.show_toast(&msg);
                    m2.clone().refresh();
                });
            }),
            ("Android Settings", "Open native Android container settings.", "emblem-system-symbolic", "Open Settings", |m| {
                util::spawn(&["waydroid", "app", "launch", "com.android.settings"]);
                m.win.show_toast("Opening Android Settings...");
            }),
            ("Aurora Store", "Download and update Android apps without a Google account.", "software-update-available-symbolic", "Open Store", |m| {
                if waydroid::is_aurora_store_installed() {
                    util::spawn(&["waydroid", "app", "launch", "com.aurora.store"]);
                    m.win.show_toast("Opening Aurora Store...");
                    return;
                }
                m.win.show_toast("Downloading and installing Aurora Store APK...");
                let m2 = m.clone();
                run_async(waydroid::install_aurora_store, move |(_, msg)| {
                    m2.win.show_toast(&msg);
                    m2.clone().refresh();
                });
            }),
        ];
        for (t, s, icon, label, act) in subs {
            let r = super::helpers::action_row(&tr(t), &tr(s));
            let b = icon_button(icon, label, "secondary-btn compact-btn", 38, false);
            b.set_valign(gtk::Align::Center);
            let m = me.clone();
            b.connect_clicked(move |_| act(&m));
            r.add_suffix(&b);
            me.expander.add_row(&r);
        }
        let m = me.clone();
        me.row_sharing.connect_active_notify(move |r| {
            if m.updating_rdp.get() {
                return;
            }
            let a = r.is_active();
            remote_desktop::set_desktop_sharing(a);
            if a && m.row_control.is_active() {
                remote_desktop::set_remote_control(true);
            }
            m.win.show_toast(if a { "Desktop sharing enabled." } else { "Desktop sharing disabled." });
            m.clone().refresh();
        });
        let m = me.clone();
        me.row_control.connect_active_notify(move |r| {
            if m.updating_rdp.get() {
                return;
            }
            let a = r.is_active();
            remote_desktop::set_remote_control(a);
            m.win.show_toast(if a { "Remote control enabled." } else { "Remote control disabled (view-only)." });
            m.clone().refresh();
        });
        let m = me.clone();
        b_copy.connect_clicked(move |_| {
            let i = m.rdp_info.borrow().clone();
            if !i.is_empty() {
                copy_to_clipboard(&i);
                m.win.show_toast("Connection details copied to clipboard.");
            }
        });
        let m = me.clone();
        me.tail.button.connect_clicked(move |b| m.on_tailscale(b));
        let m = me.clone();
        me.scorer.button.connect_clicked(move |_| {
            if m.scorer_installed.get() || waydroid::is_darts_scorer_installed() {
                waydroid::launch_darts_scorer();
                m.win.show_toast("Opening Darts Scorer...");
            } else {
                m.win.show_toast("Darts Scorer installer is not available.");
            }
        });
        me
    }

    fn on_tailscale(self: &Rc<Self>, btn: &gtk::Button) {
        let st = tailscale::get_tailscale_status();
        if !st.installed {
            self.win.show_toast("Installing Tailscale in background...");
            btn.set_sensitive(false);
            let (m, b) = (self.clone(), btn.clone());
            run_async(tailscale::install_tailscale, move |(_, msg)| {
                b.set_sensitive(true);
                m.win.show_toast(&msg);
                m.clone().refresh();
            });
            return;
        }
        if st.running {
            btn.set_sensitive(false);
            let (m, b) = (self.clone(), btn.clone());
            run_async(tailscale::disconnect_tailscale, move |(_, msg)| {
                b.set_sensitive(true);
                m.win.show_toast(&msg);
                m.clone().refresh();
            });
            return;
        }
        let m = self.clone();
        super::dialogs::tailscale::present(
            &self.win,
            st.auth_url.clone(),
            Rc::new(move |_, msg| {
                m.win.show_toast(&msg);
                m.clone().refresh();
            }),
        );
    }
}
