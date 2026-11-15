//! AutoGlow 2 Manager: install / replace older copies, service control, autostart, updates, repair, uninstall.
use super::dialogs::community::DISCORD_URL;
use super::helpers::{alert, ask, esc, group, icon_button, open_browser_url, page_shell, run_async, set_pill};
use super::{Page, Win};
use crate::core::autoglow::{self, AutoGlowStatus, OldInstall, WEB_URL};
use crate::core::i18n::{tr, trf};
use crate::core::update::UpdateInfo;
use crate::core::util::home;
use adw::prelude::*;
use std::cell::{Cell, RefCell};
use std::path::Path;
use std::rc::Rc;

fn compact(label: &str, css: &str) -> gtk::Button {
    let b = gtk::Button::with_label(&tr(label));
    b.set_valign(gtk::Align::Center);
    b.add_css_class(css);
    b.add_css_class("compact-btn");
    b
}

fn short_path(p: &Path) -> String {
    let h = home().to_string_lossy().to_string();
    let s = p.to_string_lossy().to_string();
    if s.starts_with(&format!("{}/", h)) { format!("~{}", &s[h.len()..]) } else { s }
}

fn describe_old(old: &[OldInstall]) -> String {
    old.iter()
        .map(|o| if o.version.is_empty() { short_path(&o.path) } else { format!("{} ({})", short_path(&o.path), o.version) })
        .collect::<Vec<_>>()
        .join(", ")
}

pub struct AutoGlowView {
    nav: adw::NavigationPage,
    win: Win,
    status: RefCell<AutoGlowStatus>,
    busy: Cell<bool>,
    loading: Cell<bool>,
    update_info: RefCell<Option<UpdateInfo>>,
    row_status: adw::ActionRow,
    spinner: adw::Spinner,
    pill: gtk::Label,
    btn_open: gtk::Button,
    row_install: adw::ActionRow,
    btn_install: gtk::Button,
    row_old: adw::ActionRow,
    row_service: adw::ActionRow,
    btn_start_stop: gtk::Button,
    btn_restart: gtk::Button,
    row_autostart: adw::SwitchRow,
    grp_software: adw::PreferencesGroup,
    row_update: adw::ActionRow,
    btn_update: gtk::Button,
}

impl Page for AutoGlowView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }
    fn refresh(self: Rc<Self>) {
        self.do_refresh(false);
    }
}

impl AutoGlowView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let (scrolled, main) = page_shell();
        let nav = adw::NavigationPage::builder().title(tr("AutoGlow 2 Manager")).tag("autoglow").child(&scrolled).build();
        let (beta, _) = super::helpers::warning_card(&tr("AutoGlow 2 is in beta and experimental. Stay updated on Discord."), true);
        beta.add_css_class("warning-card");
        let bd = compact("Discord", "secondary-btn");
        bd.set_tooltip_text(Some(&tr("Open the IteraThor Discord")));
        bd.connect_clicked(|_| {
            open_browser_url(DISCORD_URL);
        });
        beta.append(&bd);
        main.append(&beta);
        let gm = group(&tr("AutoGlow 2"), "");
        main.append(&gm);
        let row_status = super::helpers::action_row(&tr("Status"), &tr("Checking..."));
        let bs = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        bs.set_valign(gtk::Align::Center);
        let spinner = adw::Spinner::new();
        spinner.set_visible(false);
        bs.append(&spinner);
        let pill = super::helpers::status_pill(&tr("Checking..."), "status-pill-checking");
        bs.append(&pill);
        let btn_open = icon_button("web-browser-symbolic", "Open", "suggested-action compact-btn", 36, false);
        btn_open.set_valign(gtk::Align::Center);
        btn_open.connect_clicked(|_| {
            open_browser_url(WEB_URL);
        });
        bs.append(&btn_open);
        row_status.add_suffix(&bs);
        gm.add(&row_status);
        let row_install = super::helpers::action_row(&tr("Install AutoGlow 2"), &tr("WLED lighting driven by your live games, with the AutoGlow Bridge browser extension."));
        let btn_install = compact("Install", "suggested-action");
        row_install.add_suffix(&btn_install);
        gm.add(&row_install);
        let row_old = super::helpers::action_row(&tr("Older AutoGlow 2 found"), "");
        let btn_replace = compact("Replace", "suggested-action");
        btn_replace.set_tooltip_text(Some(&tr("Remove the older copy and keep its profiles")));
        row_old.add_suffix(&btn_replace);
        gm.add(&row_old);
        let row_service = super::helpers::action_row(&tr("Service"), "");
        let bsv = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        bsv.set_valign(gtk::Align::Center);
        let btn_start_stop = compact("Stop", "secondary-btn");
        btn_start_stop.set_size_request(80, -1);
        bsv.append(&btn_start_stop);
        let btn_restart = compact("Restart", "secondary-btn");
        bsv.append(&btn_restart);
        row_service.add_suffix(&bsv);
        gm.add(&row_service);
        let row_autostart = super::helpers::switch_row("Start on Boot", "");
        gm.add(&row_autostart);
        let gsw = group(&tr("Software"), "");
        main.append(&gsw);
        let row_update = super::helpers::action_row(&tr("Updates"), &tr("Not checked yet."));
        let btn_update = compact("Check", "secondary-btn");
        row_update.add_suffix(&btn_update);
        gsw.add(&row_update);
        let row_maint = super::helpers::action_row(&tr("Maintenance"), "");
        let bm = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        bm.set_valign(gtk::Align::Center);
        let btn_repair = compact("Repair", "secondary-btn");
        btn_repair.set_tooltip_text(Some(&tr("Reinstall packages, the browser extension and the service. Settings are kept.")));
        bm.append(&btn_repair);
        let btn_uninstall = compact("Uninstall", "secondary-btn-destructive");
        btn_uninstall.set_tooltip_text(Some(&tr("Remove the service and the browser extension")));
        bm.append(&btn_uninstall);
        row_maint.add_suffix(&bm);
        gsw.add(&row_maint);
        let me = Rc::new(AutoGlowView {
            nav,
            win,
            status: RefCell::new(AutoGlowStatus::default()),
            busy: Cell::new(false),
            loading: Cell::new(false),
            update_info: RefCell::new(None),
            row_status,
            spinner,
            pill,
            btn_open,
            row_install,
            btn_install,
            row_old,
            row_service,
            btn_start_stop,
            btn_restart,
            row_autostart,
            grp_software: gsw,
            row_update,
            btn_update,
        });
        let m = me.clone();
        me.btn_install.connect_clicked(move |_| m.confirm_install("Installing...", true));
        let m = me.clone();
        btn_replace.connect_clicked(move |_| m.confirm_install("Repairing...", false));
        let m = me.clone();
        btn_repair.connect_clicked(move |_| {
            if !m.old_installs().is_empty() {
                m.confirm_install("Repairing...", false);
            } else {
                m.run_task(Box::new(autoglow::install), "Repairing...", None);
            }
        });
        let m = me.clone();
        me.btn_start_stop.connect_clicked(move |_| {
            let running = m.status.borrow().running;
            m.run_task(
                Box::new(move || {
                    let ok = if running { autoglow::stop() } else { autoglow::start() };
                    if !ok {
                        return (false, tr("Failed changing the AutoGlow service state."));
                    }
                    (true, tr(if running { "AutoGlow stopped." } else { "AutoGlow started." }))
                }),
                if running { "Stopping..." } else { "Starting..." },
                None,
            );
        });
        let m = me.clone();
        me.btn_restart.connect_clicked(move |_| {
            m.run_task(
                Box::new(|| {
                    let ok = autoglow::restart();
                    (ok, tr(if ok { "AutoGlow restarted." } else { "Failed restarting AutoGlow." }))
                }),
                "Restarting...",
                None,
            );
        });
        let m = me.clone();
        me.row_autostart.connect_active_notify(move |r| {
            if m.loading.get() {
                return;
            }
            let en = r.is_active();
            let m2 = m.clone();
            run_async(move || autoglow::set_enabled(en), move |ok| {
                m2.win.show_toast(if !ok {
                    "Failed changing AutoGlow autostart."
                } else if en {
                    "AutoGlow starts on boot."
                } else {
                    "AutoGlow no longer starts on boot."
                });
                m2.clone().refresh();
            });
        });
        let m = me.clone();
        me.btn_update.connect_clicked(move |_| m.on_update());
        let m = me.clone();
        btn_uninstall.connect_clicked(move |_| m.on_uninstall());
        let m = me.clone();
        me.nav.connect_map(move |_| m.do_refresh(true));
        me
    }

    fn do_refresh(self: &Rc<Self>, check_updates: bool) {
        let m = self.clone();
        run_async(autoglow::get_status, move |s| {
            let is_git = s.is_git;
            *m.status.borrow_mut() = s;
            m.apply_status();
            if !is_git {
                m.win.set_autoglow_update(false);
            }
            if check_updates && is_git && !m.busy.get() {
                m.check_updates(true);
            }
        });
    }

    /// Older copies the install replaces. A kept working copy that is not running is left alone.
    fn old_installs(&self) -> Vec<OldInstall> {
        self.status.borrow().old_installs.iter().filter(|o| !o.keep || o.active).cloned().collect()
    }

    fn apply_status(&self) {
        let s = self.status.borrow().clone();
        let old = self.old_installs();
        if !s.installed && !old.is_empty() {
            set_pill(&self.pill, &tr("Older Version"), "status-pill-pending");
            self.row_status.set_subtitle(&esc(&trf("Found in {paths}.", &[("paths", &describe_old(&old))])));
            self.row_install.set_subtitle(&tr("Removes the older AutoGlow 2 first. Your profiles, flows and settings are carried over."));
        } else if !s.installed {
            set_pill(&self.pill, &tr("Not Installed"), "status-pill-checking");
            self.row_status.set_subtitle(&esc(&if s.legacy {
                tr("An AutoGlow 1 service was found. Installing replaces it.")
            } else {
                trf("Installs into {path}.", &[("path", &short_path(&autoglow::default_dir()))])
            }));
            self.row_install.set_subtitle(&tr("WLED lighting driven by your live games, with the AutoGlow Bridge browser extension."));
        } else if !s.service_present {
            set_pill(&self.pill, &tr("No Service"), "status-pill-pending");
        } else if s.running {
            set_pill(&self.pill, &tr("Running"), "status-pill-running");
        } else if s.active_state == "activating" || s.active_state == "reloading" {
            set_pill(&self.pill, &tr("Starting"), "status-pill-pending");
        } else {
            set_pill(&self.pill, &tr(if s.active_state == "failed" { "Failed" } else { "Stopped" }), "status-pill-stopped");
        }
        if s.installed {
            let mut parts = vec![];
            if !s.version.is_empty() {
                parts.push(trf("Version {version}", &[("version", &s.version)]));
            }
            if let Some(d) = &s.install_dir {
                parts.push(short_path(d));
            }
            self.row_status.set_subtitle(&esc(&parts.join(" · ")));
        }
        self.row_install.set_visible(!s.installed);
        self.row_old.set_visible(s.installed && !old.is_empty());
        if s.installed && !old.is_empty() {
            self.row_old.set_subtitle(&esc(&trf("{paths}. Replacing removes it and keeps its profiles.", &[("paths", &describe_old(&old))])));
        }
        self.btn_open.set_visible(s.installed);
        self.btn_open.set_sensitive(s.running && !self.busy.get());
        self.btn_open.set_tooltip_text(Some(&if s.running { trf("Open the web interface ({url})", &[("url", &WEB_URL)]) } else { tr("Start the AutoGlow service to open it") }));
        let has_service = s.installed && s.service_present;
        self.row_service.set_visible(has_service);
        self.row_autostart.set_visible(has_service);
        self.grp_software.set_visible(s.installed);
        self.row_update.set_visible(s.is_git);
        self.btn_start_stop.set_label(&tr(if s.running { "Stop" } else { "Start" }));
        self.btn_restart.set_sensitive(s.running);
        self.loading.set(true);
        self.row_autostart.set_active(s.enabled);
        self.loading.set(false);
    }

    fn set_busy(&self, busy: bool, text: &str) {
        self.busy.set(busy);
        self.btn_install.set_sensitive(!busy);
        self.row_old.set_sensitive(!busy);
        self.row_service.set_sensitive(!busy);
        self.row_autostart.set_sensitive(!busy);
        self.grp_software.set_sensitive(!busy);
        self.btn_open.set_sensitive(!busy);
        self.spinner.set_visible(busy);
        if busy {
            set_pill(&self.pill, &tr(text), "status-pill-pending");
        }
    }

    /// Run a long action with the page locked, then toast its message and refresh.
    fn run_task(self: &Rc<Self>, work: Box<dyn FnOnce() -> (bool, String) + Send>, busy_text: &str, on_success: Option<Box<dyn Fn(&Rc<AutoGlowView>)>>) {
        self.set_busy(true, busy_text);
        let m = self.clone();
        run_async(work, move |(ok, msg)| {
            m.set_busy(false, "");
            m.win.show_toast(&msg);
            if ok {
                if let Some(f) = on_success {
                    f(&m);
                }
            }
            m.clone().refresh();
        });
    }

    /// Install or repair; with an older AutoGlow 2 around, first ask before it is removed.
    fn confirm_install(self: &Rc<Self>, busy_text: &'static str, hint: bool) {
        let run = {
            let m = self.clone();
            move || {
                m.win.show_toast("Installing AutoGlow 2. This takes a few minutes...");
                let succ: Option<Box<dyn Fn(&Rc<AutoGlowView>)>> = if hint { Some(Box::new(|m: &Rc<AutoGlowView>| m.show_browser_hint())) } else { None };
                m.run_task(Box::new(autoglow::install), busy_text, succ);
            }
        };
        let old = self.old_installs();
        if old.is_empty() {
            run();
            return;
        }
        let lines: Vec<String> = old
            .iter()
            .map(|o| {
                let mut l = format!("• {}", describe_old(std::slice::from_ref(o)));
                if o.keep {
                    l += &format!(" – {}", tr("folder kept, it has changes that are not on GitHub"));
                }
                l
            })
            .collect();
        let body = format!(
            "{}\n\n{}\n\n{}",
            trf("SUIT installs AutoGlow 2 into {path} and removes this older installation completely:", &[("path", &short_path(&autoglow::default_dir()))]),
            lines.join("\n"),
            trf(
                "Your profiles, flows and settings are carried over. A backup is kept in {path}.",
                &[("path", &short_path(&home().join(".local/share/suit/autoglow-backups")))]
            )
        );
        let d = alert(&tr("Replace Older AutoGlow 2?"), &body, &[("cancel", &tr("Cancel")), ("replace", &tr("Replace"))], Some("replace"), None);
        ask(&self.win.window, d, move |r| {
            if r == "replace" {
                run();
            }
        });
    }

    fn show_browser_hint(&self) {
        let d = alert(
            &tr("AutoGlow 2 Installed"),
            &tr("AutoGlow gets your live games from the AutoGlow Bridge extension, which installs itself in Chrome and Chromium.\n\nRestart the browser, play on play.autodarts.com and set up your WLED lights in the AutoGlow web interface."),
            &[("close", &tr("Later")), ("open", &tr("Open AutoGlow"))],
            Some("open"),
            None,
        );
        ask(&self.win.window, d, |r| {
            if r == "open" {
                open_browser_url(WEB_URL);
            }
        });
    }

    fn check_updates(self: &Rc<Self>, quiet: bool) {
        self.row_update.set_subtitle(&tr("Checking for updates..."));
        self.btn_update.set_sensitive(false);
        let m = self.clone();
        run_async(autoglow::check_update, move |info| {
            m.btn_update.set_sensitive(true);
            if let Some(e) = &info.error {
                m.row_update.set_subtitle(&esc(&tr(e)));
                m.set_update_button(false);
                if !quiet {
                    m.win.show_toast(e);
                }
                *m.update_info.borrow_mut() = Some(info);
                return;
            }
            m.win.set_autoglow_update(info.has_update);
            if info.has_update {
                let mut t = if info.commits_behind == 1 { tr("1 update available.") } else { trf("{count} updates available.", &[("count", &info.commits_behind)]) };
                if !info.latest_message.is_empty() {
                    t += &format!(" {}", trf("Latest: {msg}", &[("msg", &info.latest_message)]));
                }
                m.row_update.set_subtitle(&esc(&t));
            } else {
                m.row_update.set_subtitle(&tr("AutoGlow 2 is up to date."));
                if !quiet {
                    m.win.show_toast("AutoGlow 2 is up to date.");
                }
            }
            m.set_update_button(info.has_update);
            *m.update_info.borrow_mut() = Some(info);
        });
    }

    fn set_update_button(&self, has: bool) {
        self.btn_update.set_label(&tr(if has { "Update" } else { "Check" }));
        if has {
            self.btn_update.remove_css_class("secondary-btn");
            self.btn_update.add_css_class("suggested-action");
        } else {
            self.btn_update.remove_css_class("suggested-action");
            self.btn_update.add_css_class("secondary-btn");
        }
    }

    fn on_update(self: &Rc<Self>) {
        if !self.update_info.borrow().as_ref().map(|i| i.has_update).unwrap_or(false) {
            self.check_updates(false);
            return;
        }
        self.win.show_toast("Updating AutoGlow 2. Settings, flows and profiles are kept...");
        self.run_task(
            Box::new(autoglow::update),
            "Updating...",
            Some(Box::new(|m: &Rc<AutoGlowView>| {
                *m.update_info.borrow_mut() = None;
                m.win.set_autoglow_update(false);
                m.set_update_button(false);
                m.row_update.set_subtitle(&tr("AutoGlow 2 is up to date."));
            })),
        );
    }

    fn on_uninstall(self: &Rc<Self>) {
        let own = self.status.borrow().install_dir.as_deref() == Some(autoglow::default_dir().as_path());
        let mut body = tr("This stops AutoGlow and removes its service and the AutoGlow Bridge browser extension.");
        if own {
            body += &format!(
                "\n\n{}",
                trf("Delete the AutoGlow folder ({path}) as well? This also deletes your AutoGlow settings, flows and profiles.", &[("path", &short_path(&autoglow::default_dir()))])
            );
        }
        let d = adw::AlertDialog::new(Some(&tr("Uninstall AutoGlow 2?")), Some(&body));
        d.add_response("cancel", &tr("Cancel"));
        if own {
            d.add_response("keep", &tr("Keep Settings"));
            d.add_response("delete", &tr("Delete Everything"));
            d.set_response_appearance("delete", adw::ResponseAppearance::Destructive);
        } else {
            d.add_response("keep", &tr("Uninstall"));
            d.set_response_appearance("keep", adw::ResponseAppearance::Destructive);
        }
        d.set_default_response(Some("cancel"));
        d.set_close_response("cancel");
        let m = self.clone();
        ask(&self.win.window, d, move |r| {
            if r == "keep" || r == "delete" {
                let rm = r == "delete";
                m.run_task(Box::new(move || autoglow::uninstall(rm)), "Removing...", None);
            }
        });
    }
}
