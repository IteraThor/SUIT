//! System Repair page: lists what a Fedora upgrade left broken and fixes it step by step ("Fix All").
use super::helpers::{group, icon_button, page_shell, run_async, set_button_label, set_pill};
use super::{Page, Win};
use crate::core::i18n::{tr, trf};
use crate::core::upgrade::{self, Problem};
use adw::prelude::*;
use std::cell::{Cell, RefCell};
use std::collections::HashSet;
use std::rc::Rc;

struct StepRow {
    row: adw::ActionRow,
    spinner: adw::Spinner,
    pill: gtk::Label,
    problem: Problem,
}

impl StepRow {
    fn new(p: Problem) -> StepRow {
        let row = adw::ActionRow::new();
        row.set_title(&super::helpers::esc(&p.title));
        row.set_subtitle(&super::helpers::esc(&p.detail));
        row.set_subtitle_lines(0);
        let bx = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        bx.set_valign(gtk::Align::Center);
        let spinner = adw::Spinner::new();
        spinner.set_visible(false);
        let pill = gtk::Label::new(None);
        bx.append(&spinner);
        bx.append(&pill);
        row.add_suffix(&bx);
        let s = StepRow { row, spinner, pill, problem: p };
        s.set_state("waiting", None);
        s
    }

    fn set_state(&self, state: &str, msg: Option<&str>) {
        let (t, css) = match state {
            "running" => ("Fixing...", "status-pill-checking"),
            "done" => ("Fixed", "status-pill-running"),
            "failed" => ("Failed", "status-pill-stopped"),
            _ => ("To Fix", "status-pill-pending"),
        };
        set_pill(&self.pill, &tr(t), css);
        self.spinner.set_visible(state == "running");
        if let Some(m) = msg.filter(|m| !m.is_empty()) {
            self.row.set_subtitle(&super::helpers::esc(m));
        }
    }
}

pub struct UpgradeRepairPage {
    nav: adw::NavigationPage,
    win: Win,
    rows: Vec<StepRow>,
    running: Cell<bool>,
    fixed: RefCell<HashSet<String>>,
    results: RefCell<Vec<(String, bool)>>,
    btn_fix: gtk::Button,
    grp_done: adw::PreferencesGroup,
    row_done: adw::ActionRow,
    btn_restart: gtk::Button,
}

impl Page for UpgradeRepairPage {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }
}

impl UpgradeRepairPage {
    pub fn new(win: Win, problems: Vec<Problem>, changes: Vec<String>) -> Rc<UpgradeRepairPage> {
        let (scrolled, content) = page_shell();
        let nav = adw::NavigationPage::builder().title(tr("System Repair")).tag("upgrade_repair").child(&scrolled).build();
        let desc = if !changes.is_empty() {
            trf("Updated: {changes}. Some SUIT features need repairs to work again.", &[("changes", &changes.join(" · "))])
        } else {
            tr("A system update left some SUIT features broken.")
        };
        let summary = group(&tr("After the System Update"), &desc);
        let btn_fix = icon_button("emblem-system-symbolic", "Fix All", "suggested-action compact-btn", 36, false);
        btn_fix.set_valign(gtk::Align::Center);
        summary.set_header_suffix(Some(&btn_fix));
        content.append(&summary);
        let steps = group(&tr("Steps"), "");
        let rows: Vec<StepRow> = problems.into_iter().map(StepRow::new).collect();
        for r in &rows {
            steps.add(&r.row);
        }
        content.append(&steps);
        let grp_done = adw::PreferencesGroup::new();
        grp_done.set_visible(false);
        let row_done = adw::ActionRow::new();
        row_done.set_subtitle_lines(0);
        let btn_restart = icon_button("system-reboot-symbolic", "Restart Now", "suggested-action compact-btn", 36, false);
        btn_restart.set_valign(gtk::Align::Center);
        btn_restart.set_visible(false);
        row_done.add_suffix(&btn_restart);
        grp_done.add(&row_done);
        content.append(&grp_done);
        let me = Rc::new(UpgradeRepairPage {
            nav,
            win,
            rows,
            running: Cell::new(false),
            fixed: RefCell::new(HashSet::new()),
            results: RefCell::new(Vec::new()),
            btn_fix,
            grp_done,
            row_done,
            btn_restart,
        });
        let m = me.clone();
        me.btn_fix.connect_clicked(move |_| m.fix_all());
        let m = me.clone();
        me.btn_restart.connect_clicked(move |b| {
            b.set_sensitive(false);
            let (m2, b2) = (m.clone(), b.clone());
            run_async(upgrade::reboot, move |(ok, msg)| {
                if !ok {
                    b2.set_sensitive(true);
                    m2.win.show_toast(&trf("Restart failed: {error}", &[("error", &msg)]));
                }
            });
        });
        me
    }

    pub fn fix_all(self: &Rc<Self>) {
        if self.running.replace(true) {
            return;
        }
        self.btn_fix.set_sensitive(false);
        self.grp_done.set_visible(false);
        self.results.borrow_mut().clear();
        self.next(0);
    }

    fn next(self: &Rc<Self>, mut idx: usize) {
        // Skip what an earlier "Fix All" run already fixed
        while idx < self.rows.len() && self.fixed.borrow().contains(&self.rows[idx].problem.id) {
            idx += 1;
        }
        if idx >= self.rows.len() {
            self.finish();
            return;
        }
        let row = &self.rows[idx];
        let prompt = tr("A password prompt opens.");
        row.set_state("running", if row.problem.interactive { Some(prompt.as_str()) } else { None });
        log::info!("Repair step {} started", row.problem.id);
        let fix = row.problem.fix.clone();
        let m = self.clone();
        run_async(move || fix(), move |(ok, msg)| {
            let r = &m.rows[idx];
            log::info!("Repair step {}: {} ({})", r.problem.id, if ok { "ok" } else { "failed" }, msg);
            r.set_state(if ok { "done" } else { "failed" }, Some(&msg));
            m.results.borrow_mut().push((r.problem.id.clone(), ok));
            m.next(idx + 1);
        });
    }

    fn finish(&self) {
        self.running.set(false);
        for (id, ok) in self.results.borrow().iter() {
            if *ok {
                self.fixed.borrow_mut().insert(id.clone());
            }
        }
        let fixed = self.fixed.borrow();
        let failed = self.rows.iter().filter(|r| !fixed.contains(&r.problem.id)).count();
        let relogin = self.rows.iter().any(|r| r.problem.relogin && fixed.contains(&r.problem.id));
        if failed > 0 {
            self.row_done.set_title(&trf("{count} step(s) could not be fixed", &[("count", &failed)]));
            self.row_done.set_subtitle(&tr("Check the messages above, then try again. Details are in suit.log."));
            self.btn_fix.set_sensitive(true);
            set_button_label(&self.btn_fix, &tr("Try Again"));
        } else {
            upgrade::mark_fixed();
            self.row_done.set_title(&tr("Everything is repaired"));
            self.row_done.set_subtitle(&if relogin { tr("Restart the computer so GNOME loads the updated extensions.") } else { String::new() });
            self.btn_fix.set_visible(false);
        }
        self.btn_restart.set_visible(relogin && failed == 0);
        self.grp_done.set_visible(true);
        self.win.show_toast(if failed == 0 { "Repair finished." } else { "Repair finished with errors." });
    }
}
