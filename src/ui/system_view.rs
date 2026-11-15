//! System Utilities and Tweaks: Essentials (what Apply All covers) and Extras tabs, with a progress overlay
//! for Apply All.
use super::fast_boot_view::summarize_fast_boot;
use super::helpers::{alert, ask, esc, group, icon_button, page_shell, run_async, switch_row, view_switcher, BadgeOrButton};
use super::hw_overlay_view::summarize_hw_overlay;
use super::{Page, Win};
use crate::core::i18n::{tr, trf};
use crate::core::{boot, debloat, desktop, keyring, system, util};
use adw::prelude::*;
use std::cell::Cell;
use std::rc::Rc;
use std::sync::{Arc, Mutex};

/// One-line state for the Minimal Desktop switch.
pub fn summarize_minimal_desktop(enabled: bool, relogin: bool) -> String {
    tr(if !enabled {
        "Off. Desktop dock and a trimmed top bar and quick settings."
    } else if relogin {
        "On. Log out and back in once to apply."
    } else {
        "On. Dock on the desktop, top bar and quick settings trimmed."
    })
}

struct Data {
    status: system::RecommendedStatus,
    keyring_blank: bool,
    bloat: Vec<String>,
    chromium: bool,
    discord: bool,
    visuals_ok: bool,
    touch: bool,
    minimal: (bool, bool),
    fast_boot: ((usize, usize), Option<f64>),
    hw_overlay: (bool, serde_json::Value),
}

pub struct SystemView {
    nav: adw::NavigationPage,
    win: Win,
    user: String,
    updating: Cell<bool>,
    refreshing: Cell<bool>,
    pending: Cell<bool>,
    keyring_oo7: bool,
    keyring_blank: Cell<bool>,
    btn_apply_all: gtk::Button,
    overlay_box: gtk::Box,
    lbl_step: gtk::Label,
    lbl_detail: gtk::Label,
    pbar: gtk::ProgressBar,
    lbl_profile: gtk::Label,
    row_autologin: adw::SwitchRow,
    row_blanking: adw::SwitchRow,
    row_keyring: adw::ActionRow,
    keyring: BadgeOrButton,
    row_visuals: adw::ActionRow,
    visuals: BadgeOrButton,
    row_touch: adw::SwitchRow,
    row_minimal: adw::SwitchRow,
    row_fast_boot: adw::ActionRow,
    row_hw_overlay: adw::ActionRow,
    row_debloat: adw::ActionRow,
    debloat: BadgeOrButton,
    row_chromium: adw::ActionRow,
    chromium: BadgeOrButton,
    discord: BadgeOrButton,
}

impl Page for SystemView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }

    fn refresh(self: Rc<Self>) {
        if self.refreshing.replace(true) {
            self.pending.set(true);
            return;
        }
        self.updating.set(true);
        let user = self.user.clone();
        let m = self.clone();
        run_async(
            move || Data {
                status: system::get_recommended_status(&user),
                keyring_blank: keyring::is_keyring_blank_or_unlocked(None).0,
                bloat: debloat::get_installed_bloatware(None),
                chromium: debloat::is_app_installed("chromium"),
                discord: debloat::is_discord_installed(),
                visuals_ok: desktop::get_visuals_status().all_ok,
                touch: desktop::is_touch_scaling_enabled(),
                minimal: (desktop::is_minimal_desktop_enabled(), desktop::minimal_desktop_needs_relogin()),
                fast_boot: (boot::get_boot_tweak_counts(), boot::get_last_boot_time()),
                hw_overlay: (desktop::is_hw_overlay_enabled(), desktop::get_hw_overlay_config()),
            },
            move |d| {
                m.show(&d);
                m.updating.set(false);
                m.refreshing.set(false);
                if m.pending.replace(false) {
                    m.clone().refresh();
                }
            },
        );
    }
}

impl SystemView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let root = gtk::Overlay::new();
        let (sc_e, main) = page_shell();
        let (sc_x, extra) = page_shell();
        let stack = adw::ViewStack::new();
        stack.add_titled(&sc_e, Some("essentials"), &tr("Essentials"));
        stack.add_titled(&sc_x, Some("extras"), &tr("Extras"));
        stack.set_vexpand(true);
        let pb = gtk::Box::new(gtk::Orientation::Vertical, 0);
        pb.append(&view_switcher(&stack, 8));
        pb.append(&stack);
        root.set_child(Some(&pb));
        let nav = adw::NavigationPage::builder().title(tr("System Utilities and Tweaks")).tag("system").child(&root).build();

        // Apply All progress overlay
        let overlay_box = gtk::Box::new(gtk::Orientation::Vertical, 0);
        overlay_box.add_css_class("apply-all-overlay");
        overlay_box.set_hexpand(true);
        overlay_box.set_vexpand(true);
        overlay_box.set_visible(false);
        let blocker = gtk::GestureClick::new();
        blocker.set_propagation_phase(gtk::PropagationPhase::Capture);
        overlay_box.add_controller(blocker);
        let card = gtk::Box::new(gtk::Orientation::Vertical, 14);
        card.add_css_class("apply-all-card");
        card.set_valign(gtk::Align::Center);
        card.set_halign(gtk::Align::Center);
        card.set_size_request(420, -1);
        let sp = adw::Spinner::new();
        sp.set_size_request(40, 40);
        card.append(&sp);
        let t = gtk::Label::new(Some(&tr("Applying System Configurations")));
        t.add_css_class("title");
        card.append(&t);
        let lbl_step = gtk::Label::new(Some(&tr("Preparing...")));
        lbl_step.add_css_class("step-title");
        card.append(&lbl_step);
        let pbar = gtk::ProgressBar::new();
        pbar.set_size_request(340, -1);
        pbar.set_halign(gtk::Align::Center);
        card.append(&pbar);
        let lbl_detail = gtk::Label::new(Some(&tr("Please wait...")));
        lbl_detail.add_css_class("detail-text");
        lbl_detail.set_wrap(true);
        lbl_detail.set_max_width_chars(45);
        card.append(&lbl_detail);
        overlay_box.append(&card);
        root.add_overlay(&overlay_box);

        let gs = group(&tr("Essentials"), "");
        let btn_apply_all = icon_button("system-run-symbolic", "Apply All", "suggested-action compact-btn", 36, false);
        gs.set_header_suffix(Some(&btn_apply_all));
        main.append(&gs);
        let gx = group(&tr("Extras"), "");
        extra.append(&gx);

        let row_profile = super::helpers::action_row(&tr("Power Profile"), &tr("Locks CPU to maximum performance."));
        let lbl_profile = gtk::Label::new(Some(&tr("Checking...")));
        lbl_profile.add_css_class("dim-label");
        row_profile.add_suffix(&lbl_profile);
        gs.add(&row_profile);
        let row_autologin = switch_row("Automatic Login", "Bypass password prompt when turning on.");
        gs.add(&row_autologin);
        let row_blanking = switch_row("Display Always On", "Keep screen lit continuously.");
        gs.add(&row_blanking);
        let row_keyring = super::helpers::action_row(&tr("Keyring Password"), &tr("Unlock apps without password popups."));
        let kr = BadgeOrButton::new("dialog-password-symbolic", "Remove Password", "suggested-action compact-btn", 36);
        row_keyring.add_suffix(&kr.widget);
        gs.add(&row_keyring);
        let row_visuals = super::helpers::action_row(&tr("Appearance and Theme"), &tr("Dark theme and clean wallpaper."));
        let vis = BadgeOrButton::new("preferences-desktop-wallpaper-symbolic", "Apply Theme", "suggested-action compact-btn", 36);
        row_visuals.add_suffix(&vis.widget);
        gs.add(&row_visuals);
        let row_touch = switch_row("Touchscreen Tweaks", "Enlarge top bar, controls, and keyboard.");
        gx.add(&row_touch);
        let row_minimal = switch_row("Minimal Desktop", "Checking...");
        let exp = gtk::Label::new(Some(&tr("EXPERIMENTAL")));
        exp.add_css_class("status-pill-experimental");
        exp.set_valign(gtk::Align::Center);
        row_minimal.add_suffix(&exp);
        gx.add(&row_minimal);
        let nav_row = |t: &str, s: &str| {
            let r = super::helpers::action_row(&tr(t), &tr(s));
            r.set_activatable(true);
            r.add_suffix(&gtk::Image::from_icon_name("go-next-symbolic"));
            r
        };
        let row_fast_boot = nav_row("Fast Boot", "Pick boot-time tweaks one by one.");
        gx.add(&row_fast_boot);
        let row_hw_overlay = nav_row("Hardware Overlay", "Show CPU, temperature, RAM and camera FPS on the wallpaper.");
        gx.add(&row_hw_overlay);
        let row_debloat = super::helpers::action_row(&tr("Remove Unused Apps"), &tr("Remove default desktop apps like LibreOffice, Maps, and Weather."));
        let db = BadgeOrButton::new("user-trash-symbolic", "Review & Clean", "suggested-action compact-btn", 36);
        row_debloat.add_suffix(&db.widget);
        gs.add(&row_debloat);
        let row_chromium = super::helpers::action_row(&tr("Chromium Web Browser"), &tr("Required browser for Autodarts board display and kiosk."));
        let ch = BadgeOrButton::new("software-update-available-symbolic", "Install Chromium", "suggested-action compact-btn", 36);
        row_chromium.add_suffix(&ch.widget);
        gs.add(&row_chromium);
        let row_discord = super::helpers::action_row(&tr("Discord"), &tr("Join the official Autodarts and IteraThor community servers for quick support and updates."));
        let dc = BadgeOrButton::new("software-update-available-symbolic", "Install Discord", "suggested-action compact-btn", 36);
        row_discord.add_suffix(&dc.widget);
        gx.add(&row_discord);
        let row_adv = nav_row("Advanced Users", "Tailscale remote VPN and Android Darts Scorer app.");
        gx.add(&row_adv);

        let me = Rc::new(SystemView {
            nav,
            win: win.clone(),
            user: util::username(),
            updating: Cell::new(false),
            refreshing: Cell::new(false),
            pending: Cell::new(false),
            keyring_oo7: keyring::uses_oo7(),
            keyring_blank: Cell::new(false),
            btn_apply_all,
            overlay_box,
            lbl_step,
            lbl_detail,
            pbar,
            lbl_profile,
            row_autologin,
            row_blanking,
            row_keyring,
            keyring: kr,
            row_visuals,
            visuals: vis,
            row_touch,
            row_minimal,
            row_fast_boot,
            row_hw_overlay,
            row_debloat,
            debloat: db,
            row_chromium,
            chromium: ch,
            discord: dc,
        });
        let w = win.clone();
        me.row_fast_boot.connect_activated(move |_| w.open_fast_boot_page());
        let w = win.clone();
        me.row_hw_overlay.connect_activated(move |_| w.open_hw_overlay_page());
        let w = win.clone();
        row_adv.connect_activated(move |_| w.open_advanced_users_page());
        let m = me.clone();
        me.btn_apply_all.connect_clicked(move |_| m.on_apply_all());
        let m = me.clone();
        me.row_autologin.connect_active_notify(move |r| {
            if m.updating.get() {
                return;
            }
            power_toggle(&m, r.is_active(), true);
        });
        let m = me.clone();
        me.row_blanking.connect_active_notify(move |r| {
            if m.updating.get() {
                return;
            }
            power_toggle(&m, r.is_active(), false);
        });
        let m = me.clone();
        me.keyring.button.connect_clicked(move |_| m.on_keyring());
        let m = me.clone();
        me.visuals.button.connect_clicked(move |b| {
            m.win.show_toast("Applying desktop visuals and organizing App Grid...");
            m.run_and_refresh(b, desktop::apply_desktop_visuals);
        });
        let m = me.clone();
        me.row_touch.connect_active_notify(move |r| {
            if m.updating.get() {
                return;
            }
            let (_, msg) = desktop::set_touch_scaling(r.is_active(), 1.2);
            m.win.show_toast(&msg);
            m.clone().refresh();
        });
        let m = me.clone();
        me.row_minimal.connect_active_notify(move |r| {
            if m.updating.get() {
                return;
            }
            let (_, msg) = desktop::set_minimal_desktop(r.is_active());
            m.win.show_toast(&msg);
            m.clone().refresh();
        });
        let m = me.clone();
        me.debloat.button.connect_clicked(move |_| {
            let details = debloat::get_installed_bloatware_details(true);
            let m2 = m.clone();
            super::dialogs::debloat::present(
                &m.win,
                details,
                Rc::new(move |_, msg| {
                    m2.win.show_toast(&msg);
                    m2.clone().refresh();
                }),
            );
        });
        let m = me.clone();
        me.chromium.button.connect_clicked(move |b| {
            m.win.show_toast("Installing Chromium in background...");
            m.run_and_refresh(b, debloat::install_chromium);
        });
        let m = me.clone();
        me.discord.button.connect_clicked(move |b| {
            m.win.show_toast("Installing Discord in background...");
            m.run_and_refresh(b, debloat::install_discord);
        });
        let m = me.clone();
        me.nav.connect_map(move |_| m.clone().refresh());
        me
    }

    fn run_and_refresh(self: &Rc<Self>, b: &gtk::Button, f: fn() -> (bool, String)) {
        b.set_sensitive(false);
        let (m, b) = (self.clone(), b.clone());
        run_async(f, move |(_, msg)| {
            b.set_sensitive(true);
            m.win.show_toast(&msg);
            m.clone().refresh();
        });
    }

    fn show(&self, d: &Data) {
        let s = &d.status;
        let all = s.profile_ok && s.autologin_ok && s.power_ok && d.keyring_blank && d.visuals_ok && d.bloat.is_empty() && d.chromium;
        self.btn_apply_all.set_visible(!all);
        self.lbl_profile.set_label(&tr(if s.profile_ok { "Active: Performance" } else { "Active: Default/Powersave" }));
        self.row_autologin.set_active(s.autologin_ok);
        self.row_blanking.set_active(s.power_ok);
        self.keyring_blank.set(d.keyring_blank);
        if d.keyring_blank {
            self.keyring.show_badge("Unlocked", "status-pill-running");
            self.row_keyring.set_subtitle(&tr("Unlocked on boot."));
        } else {
            self.keyring.show_button("dialog-password-symbolic", &tr(if self.keyring_oo7 { "Set Up" } else { "Remove Password" }));
            self.row_keyring.set_subtitle(&tr("Password required on boot."));
        }
        if d.visuals_ok {
            self.visuals.show_badge("Applied", "status-pill-running");
            self.row_visuals.set_subtitle(&tr("Dark theme and clean desktop active."));
        } else {
            self.visuals.show_button("preferences-desktop-wallpaper-symbolic", &tr("Apply Theme"));
            self.row_visuals.set_subtitle(&tr("Dark theme and wallpaper need setup."));
        }
        self.row_touch.set_active(d.touch);
        self.row_minimal.set_active(d.minimal.0);
        self.row_minimal.set_subtitle(&summarize_minimal_desktop(d.minimal.0, d.minimal.1));
        let ((a, t), bt) = d.fast_boot;
        self.row_fast_boot.set_subtitle(&esc(&summarize_fast_boot(a, t, bt)));
        self.row_hw_overlay.set_subtitle(&esc(&summarize_hw_overlay(d.hw_overlay.0, &d.hw_overlay.1)));
        if d.bloat.is_empty() {
            self.debloat.show_badge("Clean", "status-pill-running");
            self.row_debloat.set_subtitle(&tr("No unused apps detected."));
        } else {
            let n = d.bloat.len();
            self.debloat.show_button("user-trash-symbolic", &trf("Review ({count})", &[("count", &n)]));
            let sample = d.bloat.iter().take(3).map(|p| p.split('-').next().unwrap_or(p).to_string()).collect::<Vec<_>>().join(", ");
            self.row_debloat.set_subtitle(&esc(&trf("{count} default packages installed ({sample}).", &[("count", &n), ("sample", &sample)])));
        }
        if d.chromium {
            self.chromium.show_badge("Installed", "status-pill-running");
            self.row_chromium.set_subtitle(&tr("Installed and ready for kiosk."));
        } else {
            self.chromium.show_button("software-update-available-symbolic", &tr("Install Chromium"));
            self.row_chromium.set_subtitle(&tr("Required for board display and kiosk."));
        }
        if d.discord {
            self.discord.show_badge("Installed", "status-pill-running");
        } else {
            self.discord.show_button("software-update-available-symbolic", &tr("Install Discord"));
        }
    }

    fn on_apply_all(self: &Rc<Self>) {
        let d = alert(
            &tr("Use Touchscreen Tweaks?"),
            &tr("Apply All will configure maximum performance, automatic login, display always-on, keyring unlock, dark theme, remove unused default apps, and install Chromium.\n\nWill you be using a touchscreen display on this system?"),
            &[("cancel", &tr("Cancel")), ("no", &tr("No, Standard Display")), ("yes", &tr("Yes, Touchscreen"))],
            Some("yes"),
            None,
        );
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            if r == "yes" || r == "no" {
                m.start_apply_all(r == "yes");
            }
        });
    }

    fn start_apply_all(self: &Rc<Self>, touch: bool) {
        self.btn_apply_all.set_sensitive(false);
        self.win.btn_back.set_sensitive(false);
        self.lbl_step.set_label(&tr("Initializing..."));
        self.lbl_detail.set_label(&tr("Starting system optimization queue..."));
        self.pbar.set_fraction(0.0);
        self.overlay_box.set_visible(true);
        let prog: Arc<Mutex<Option<(usize, usize, String, String)>>> = Arc::new(Mutex::new(None));
        let done = Rc::new(Cell::new(false));
        let (p2, d2, m2) = (prog.clone(), done.clone(), self.clone());
        super::helpers::every(100, move || {
            if let Some((c, t, title, detail)) = p2.lock().unwrap().take() {
                m2.lbl_step.set_label(&trf("Step {cur} of {tot}: {title}", &[("cur", &c), ("tot", &t), ("title", &tr(&title))]));
                m2.lbl_detail.set_label(&tr(&detail));
                m2.pbar.set_fraction(if t > 0 { (c as f64 / t as f64).clamp(0.0, 1.0) } else { 0.0 });
            }
            !d2.get()
        });
        let user = self.user.clone();
        let m = self.clone();
        run_async(
            move || system::apply_all_system_and_apps(&user, touch, &|c, t, title, detail| *prog.lock().unwrap() = Some((c, t, title.into(), detail.into()))),
            move |(_, summary, _)| {
                done.set(true);
                m.overlay_box.set_visible(false);
                m.btn_apply_all.set_sensitive(true);
                m.win.btn_back.set_sensitive(true);
                m.win.show_toast(&summary);
                m.clone().refresh();
            },
        );
    }

    fn on_keyring(self: &Rc<Self>) {
        let blank = self.keyring_blank.get();
        let d = if self.keyring_oo7 {
            if blank {
                self.win.show_toast("The keyring already unlocks automatically on boot.");
                return;
            }
            // oo7 (Fedora 45+) has no blank keyring: it unlocks with a password stored for it in the TPM
            alert(
                &tr("Unlock the Keyring on Boot?"),
                &tr("SUIT stores a keyring password protected by this computer's TPM, so the keyring unlocks by itself after the automatic login.\n\nSaved entries are kept. If the keyring is locked now, it asks for its current password once."),
                &[("cancel", &tr("Cancel")), ("reset", &tr("Set Up"))],
                Some("reset"),
                None,
            )
        } else if blank {
            alert(
                &tr("Reset Keyring to Blank Password?"),
                &tr("Your keyring is already unencrypted and unlocks automatically.\n\nIf you choose to reset it, a fresh blank keyring will be created. Existing entries will be backed up to login.keyring.bak."),
                &[("cancel", &tr("Cancel")), ("reset", &tr("Reset Keyring"))],
                None,
                Some("reset"),
            )
        } else {
            alert(
                &tr("Remove Keyring Password?"),
                &tr("This automatically sets the login keyring to a blank password so it unlocks without prompting on boot.\n\nExisting keyring entries will be backed up to login.keyring.bak.\n\nIf you have personal saved passwords to preserve, choose 'Manual (Seahorse)' instead."),
                &[("cancel", &tr("Cancel")), ("seahorse", &tr("Manual (Seahorse)")), ("reset", &tr("Remove Password"))],
                Some("reset"),
                None,
            )
        };
        let m = self.clone();
        ask(&self.win.window, d, move |r| match r {
            "reset" => {
                m.win.show_toast("Removing keyring password in background...");
                let b = m.keyring.button.clone();
                m.run_and_refresh(&b, || keyring::reset_keyring_to_blank(None));
            }
            "seahorse" => m.open_seahorse_guide(),
            _ => {}
        });
    }

    fn open_seahorse_guide(self: &Rc<Self>) {
        run_async(
            || {
                if !keyring::is_seahorse_installed() {
                    keyring::install_seahorse();
                }
                keyring::launch_seahorse();
            },
            |_| {},
        );
        let d = alert(
            &tr("Disable Keyring Password in Seahorse"),
            &tr("Due to security restrictions, GNOME requires entering your password to unlock the keyring.\n\nTo make the keyring unlock automatically on boot without prompts:\n\n1. In the opened Passwords and Keys (Seahorse) window, locate the 'Login' keyring under Passwords.\n2. Right-click on 'Login' and select 'Change Password'.\n3. Enter your current user password when prompted.\n4. Leave the new password and confirmation fields completely blank.\n5. Click Continue and confirm storing unencrypted passwords."),
            &[("ok", &tr("Got It"))],
            Some("ok"),
            None,
        );
        let m = self.clone();
        ask(&self.win.window, d, move |_| m.clone().refresh());
    }
}

fn power_toggle(m: &Rc<SystemView>, active: bool, autologin: bool) {
    if autologin {
        crate::core::power::set_auto_login(active, &m.user, None);
        m.win.show_toast(if active { "Automatic login enabled." } else { "Automatic login disabled." });
    } else {
        crate::core::power::set_screen_blanking(active);
        m.win.show_toast(if active { "Display always-on enabled." } else { "Display always-on disabled." });
    }
    m.clone().refresh();
}
