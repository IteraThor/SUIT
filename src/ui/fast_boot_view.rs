//! Fast Boot submenu: one switch per boot tweak with its estimated saving, and a BIOS guide.
use super::helpers::{alert, ask, esc, group, icon_button, page_shell, run_async, top_clamp, view_switcher};
use super::{Page, Win};
use crate::core::boot::{self, FirmwareAnalysis, TweakRow};
use crate::core::i18n::{tr, trf};
use adw::prelude::*;
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;

const PHASE_LABELS: [(&str, &str); 5] = [("firmware", "BIOS"), ("loader", "Boot Loader"), ("kernel", "Kernel"), ("initrd", "Early Boot"), ("userspace", "System")];

fn rating_text(r: &str) -> &'static str {
    match r {
        "good" => "Fast",
        "ok" => "Could be faster",
        "slow" => "Slow",
        _ => "Not measured",
    }
}

/// One-line state for the System page row, e.g. '4 of 11 tweaks on · Last boot 33.3 s'.
pub fn summarize_fast_boot(applied: usize, total: usize, boot_time: Option<f64>) -> String {
    let mut t = if applied > 0 {
        trf("{applied} of {total} tweaks on", &[("applied", &applied), ("total", &total)])
    } else {
        tr("Off. Pick boot-time tweaks one by one.")
    };
    if let Some(b) = boot_time.filter(|b| *b > 0.0) {
        t += &format!(" · {}", trf("Last boot {seconds:.1f} s", &[("seconds", &format!("{:.1}", b))]));
    }
    t
}

pub fn format_boot_phases(r: Option<&HashMap<String, f64>>) -> String {
    let Some(r) = r else { return tr("Boot time not available.") };
    PHASE_LABELS.iter().filter_map(|(k, l)| r.get(*k).map(|v| format!("{} {:.1} s", tr(l), v))).collect::<Vec<_>>().join(" · ")
}

pub struct FastBootView {
    nav: adw::NavigationPage,
    win: Win,
    updating: Cell<bool>,
    busy: Cell<bool>,
    tweaks: RefCell<Vec<TweakRow>>,
    rows: RefCell<HashMap<String, (adw::SwitchRow, gtk::Label)>>,
    btn_revert_all: gtk::Button,
    btn_recommended: gtk::Button,
    row_total: adw::ActionRow,
    grp_login: adw::PreferencesGroup,
    grp_background: adw::PreferencesGroup,
    bios_box: gtk::Box,
    btn_setup: gtk::Button,
    grp_bios: RefCell<Option<adw::PreferencesGroup>>,
}

impl Page for FastBootView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }
    fn refresh(self: Rc<Self>) {
        let me = self.clone();
        run_async(
            || {
                let r = boot::get_boot_report();
                let fw = boot::analyze_firmware(r.as_ref());
                (boot::get_tweaks(), r, fw)
            },
            move |(t, r, fw)| {
                *me.tweaks.borrow_mut() = t;
                me.load_summary(r.as_ref());
                me.load_tweaks();
                me.load_firmware(&fw);
            },
        );
    }
}

impl FastBootView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let summary = group(&tr("Last Boot"), "");
        let actions = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        let btn_revert_all = icon_button("edit-undo-symbolic", "Revert All", "secondary-btn compact-btn", 36, false);
        let btn_recommended = icon_button("system-run-symbolic", "Apply Recommended", "suggested-action compact-btn", 36, false);
        actions.append(&btn_revert_all);
        actions.append(&btn_recommended);
        summary.set_header_suffix(Some(&actions));
        let row_total = adw::ActionRow::new();
        row_total.set_title(&tr("Checking..."));
        summary.add(&row_total);
        let (sc_t, tweaks_box) = page_shell();
        let (sc_b, bios_box) = page_shell();
        let stack = adw::ViewStack::new();
        stack.set_vexpand(true);
        stack.add_titled(&sc_t, Some("tweaks"), &tr("Tweaks"));
        stack.add_titled(&sc_b, Some("bios"), "BIOS");
        let pb = gtk::Box::new(gtk::Orientation::Vertical, 0);
        pb.append(&top_clamp(&summary));
        pb.append(&view_switcher(&stack, 4));
        pb.append(&stack);
        let nav = adw::NavigationPage::builder().title(tr("Fast Boot")).tag("fast_boot").child(&pb).build();
        let grp_login = group(&tr("Faster to the Desktop"), &tr("Login waits for these. Estimates come from your last boot."));
        let grp_background = group(&tr("Less Background Load"), &tr("These start alongside login and compete for CPU and disk."));
        tweaks_box.append(&grp_login);
        tweaks_box.append(&grp_background);
        let btn_setup = icon_button("system-reboot-symbolic", "Open BIOS Setup", "secondary-btn compact-btn", 36, false);
        btn_setup.set_valign(gtk::Align::Center);
        let me = Rc::new(FastBootView {
            nav,
            win,
            updating: Cell::new(false),
            busy: Cell::new(false),
            tweaks: RefCell::new(vec![]),
            rows: RefCell::new(HashMap::new()),
            btn_revert_all,
            btn_recommended,
            row_total,
            grp_login,
            grp_background,
            bios_box,
            btn_setup,
            grp_bios: RefCell::new(None),
        });
        let m = me.clone();
        me.btn_revert_all.connect_clicked(move |_| m.run_bulk(false));
        let m = me.clone();
        me.btn_recommended.connect_clicked(move |_| m.run_bulk(true));
        let m = me.clone();
        me.btn_setup.connect_clicked(move |_| m.on_open_setup());
        me
    }

    fn load_summary(&self, r: Option<&HashMap<String, f64>>) {
        match r.and_then(|r| r.get("total")) {
            Some(t) => self.row_total.set_title(&trf("{seconds:.1f} s from power-on to desktop", &[("seconds", &format!("{:.1}", t))])),
            None => self.row_total.set_title(&tr("Boot time not available")),
        }
        let tw = self.tweaks.borrow();
        let pending: f64 = tw.iter().filter(|t| !t.applied && t.unavailable.is_empty()).map(|t| t.estimate_s).sum();
        let mut ph = format_boot_phases(r);
        if pending >= 0.1 {
            ph += &format!("\n{}", trf("Up to ≈ {seconds:.1f} s more available below.", &[("seconds", &format!("{:.1}", pending))]));
        }
        self.row_total.set_subtitle(&esc(&ph));
        self.btn_recommended.set_visible(tw.iter().any(|t| t.recommended && !t.applied && t.unavailable.is_empty()));
        self.btn_revert_all.set_visible(tw.iter().any(|t| t.applied));
    }

    fn load_tweaks(self: &Rc<Self>) {
        self.updating.set(true);
        for t in self.tweaks.borrow().iter() {
            let exists = self.rows.borrow().contains_key(&t.id);
            if !exists {
                let row = adw::SwitchRow::new();
                row.set_title(&esc(&tr(&t.title)));
                // Fixed width so every title lines up whatever the estimate says
                let l = gtk::Label::new(None);
                l.set_valign(gtk::Align::Center);
                l.set_width_chars(10);
                l.add_css_class("status-pill-checking");
                row.add_prefix(&l);
                let (m, id) = (self.clone(), t.id.clone());
                row.connect_active_notify(move |r| m.on_tweak_toggled(r, &id));
                if t.blocks_login { &self.grp_login } else { &self.grp_background }.add(&row);
                self.rows.borrow_mut().insert(t.id.clone(), (row, l));
            }
            let rows = self.rows.borrow();
            let (row, l) = &rows[&t.id];
            let mut sub = tr(&t.summary);
            if !t.unavailable.is_empty() {
                sub += &format!("\n{}", tr(&t.unavailable));
            } else if !t.warning.is_empty() {
                sub += &format!("\n⚠ {}", tr(&t.warning));
            }
            row.set_subtitle(&esc(&sub));
            row.set_active(t.applied);
            row.set_sensitive(!self.busy.get() && (t.applied || t.unavailable.is_empty()));
            if t.applied {
                l.set_label(&trf("saves {seconds:.1f} s", &[("seconds", &format!("{:.1}", t.estimate_s))]));
            } else {
                l.set_label(&format!("≈ {:.1} s", t.estimate_s));
            }
            l.remove_css_class("status-pill-checking");
            l.remove_css_class("status-pill-running");
            l.add_css_class(if t.applied { "status-pill-running" } else { "status-pill-checking" });
        }
        self.updating.set(false);
    }

    fn load_firmware(&self, fw: &FirmwareAnalysis) {
        if let Some(g) = self.grp_bios.borrow_mut().take() {
            g.set_header_suffix(None::<&gtk::Widget>);
            self.bios_box.remove(&g);
        }
        let took = fw.firmware_s.map(|s| format!("{:.1} s · ", s)).unwrap_or_default();
        let machine = if fw.model.starts_with(&fw.vendor) { fw.model.clone() } else { format!("{} {}", fw.vendor, fw.model) };
        let machine = if machine.trim().is_empty() { tr("This machine") } else { machine.trim().to_string() };
        let g = group(
            &format!("BIOS · {}{}", took, tr(rating_text(&fw.rating))),
            &trf("{machine}: press {key} at power-on. Change one thing at a time and check it still boots.", &[("machine", &machine), ("key", &tr(&fw.setup_key))]),
        );
        g.set_header_suffix(Some(&self.btn_setup));
        for f in &fw.findings {
            let r = adw::ActionRow::new();
            r.set_title(&esc(&tr(f)));
            r.set_title_lines(1);
            r.add_css_class("property");
            r.add_prefix(&gtk::Image::from_icon_name("dialog-warning-symbolic"));
            g.add(&r);
        }
        for (i, s) in fw.steps.iter().enumerate() {
            let r = adw::ActionRow::new();
            r.set_title(&esc(&format!("{}. {}", i + 1, tr(&s.title))));
            r.set_subtitle(&esc(&tr(if s.r#where.is_empty() { &s.detail } else { &s.r#where })));
            r.set_title_lines(1);
            r.set_subtitle_lines(1);
            r.set_tooltip_text(Some(&tr(&s.detail)));
            g.add(&r);
        }
        self.bios_box.append(&g);
        *self.grp_bios.borrow_mut() = Some(g);
    }

    fn set_busy(&self, b: bool) {
        self.busy.set(b);
        self.btn_recommended.set_sensitive(!b);
        self.btn_revert_all.set_sensitive(!b);
        if b {
            for (r, _) in self.rows.borrow().values() {
                r.set_sensitive(false);
            }
        }
        self.win.btn_back.set_sensitive(!b);
    }

    fn finish(self: &Rc<Self>, msg: &str) {
        self.set_busy(false);
        self.win.show_toast(msg);
        self.clone().refresh();
        if let Some(s) = self.win.cached("system") {
            s.clone().refresh();
        }
    }

    fn on_tweak_toggled(self: &Rc<Self>, row: &adw::SwitchRow, id: &str) {
        if self.updating.get() || self.busy.get() {
            return;
        }
        let active = row.is_active();
        let t = self.tweaks.borrow().iter().find(|t| t.id == id).cloned();
        let Some(t) = t else { return };
        if active && !t.warning.is_empty() {
            let d = alert(
                &trf("Turn on {title}?", &[("title", &tr(&t.title))]),
                &format!("{}\n\n{}", tr(&t.summary), tr(&t.warning)),
                &[("cancel", &tr("Cancel")), ("apply", &tr("Turn On"))],
                Some("apply"),
                None,
            );
            let (m, r, id) = (self.clone(), row.clone(), id.to_string());
            ask(&self.win.window, d, move |resp| {
                if resp == "apply" {
                    m.run_tweak(&id, true);
                } else {
                    m.updating.set(true);
                    r.set_active(false);
                    m.updating.set(false);
                }
            });
            return;
        }
        self.run_tweak(id, active);
    }

    fn run_tweak(self: &Rc<Self>, id: &str, active: bool) {
        self.set_busy(true);
        if id == "splash" {
            self.win.show_toast("Rebuilding the boot image, this takes about a minute...");
        }
        let (m, id) = (self.clone(), id.to_string());
        run_async(move || boot::set_tweak(&id, active), move |(_, msg)| m.finish(&msg));
    }

    fn run_bulk(self: &Rc<Self>, enable: bool) {
        self.set_busy(true);
        self.win.show_toast(if enable { "Applying recommended tweaks..." } else { "Reverting all tweaks..." });
        let m = self.clone();
        run_async(move || boot::set_fast_boot(enable), move |(_, msg)| m.finish(&msg));
    }

    fn on_open_setup(&self) {
        let d = alert(
            &tr("Restart into BIOS Setup?"),
            &tr("The computer restarts straight into the BIOS. Save your work first. Autodarts stops until you leave the BIOS again."),
            &[("cancel", &tr("Cancel")), ("restart", &tr("Restart Now"))],
            None,
            Some("restart"),
        );
        let w = self.win.clone();
        ask(&self.win.window, d, move |r| {
            if r == "restart" {
                let w2 = w.clone();
                run_async(boot::reboot_to_firmware, move |(_, m)| w2.show_toast(&m));
            }
        });
    }
}
