//! Granular review dialog for bloatware removal with checkboxes and live progress.
use crate::core::debloat::{self, BloatApp};
use crate::core::i18n::{tr, trf};
use crate::ui::helpers::{esc, every, run_async};
use crate::ui::Win;
use adw::prelude::*;
use std::cell::Cell;
use std::rc::Rc;
use std::sync::{Arc, Mutex};

pub fn present(win: &Win, apps: Vec<BloatApp>, on_finished: Rc<dyn Fn(bool, String)>) {
    let dlg = adw::Window::new();
    dlg.set_transient_for(Some(&win.window));
    dlg.set_modal(true);
    dlg.set_title(Some(&tr("Debloat Fedora System")));
    dlg.set_default_size(560, 580);
    let removing = Rc::new(Cell::new(false));
    let r2 = removing.clone();
    dlg.connect_close_request(move |_| if r2.get() { gtk::glib::Propagation::Stop } else { gtk::glib::Propagation::Proceed });
    let installed: Vec<BloatApp> = apps.iter().filter(|a| a.is_installed).cloned().collect();
    let clean: Vec<BloatApp> = apps.iter().filter(|a| !a.is_installed).cloned().collect();
    let tv = adw::ToolbarView::new();
    dlg.set_content(Some(&tv));
    tv.add_top_bar(&adw::HeaderBar::new());
    let content = gtk::Box::new(gtk::Orientation::Vertical, 12);
    content.set_margin_top(12);
    content.set_margin_bottom(16);
    content.set_margin_start(16);
    content.set_margin_end(16);
    tv.set_content(Some(&content));
    let sc = gtk::ScrolledWindow::builder().vexpand(true).min_content_height(300).hscrollbar_policy(gtk::PolicyType::Never).build();
    let clamp = adw::Clamp::builder().maximum_size(540).build();
    sc.set_child(Some(&clamp));
    let inner = gtk::Box::new(gtk::Orientation::Vertical, 16);
    clamp.set_child(Some(&inner));
    let mut cl = Vec::new();
    let lbl_count = gtk::Label::new(None);
    let btn_sel = gtk::Button::with_label(&tr("Select All"));
    let btn_desel = gtk::Button::with_label(&tr("Deselect All"));
    if !installed.is_empty() {
        let sub = gtk::Label::new(Some(&tr("Select applications to remove. Deselect any app you want to keep on your system.")));
        sub.set_wrap(true);
        sub.set_xalign(0.0);
        sub.add_css_class("dim-label");
        content.append(&sub);
        let top = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        btn_sel.add_css_class("flat");
        btn_desel.add_css_class("flat");
        top.append(&btn_sel);
        top.append(&btn_desel);
        lbl_count.set_xalign(1.0);
        lbl_count.set_hexpand(true);
        lbl_count.add_css_class("dim-label");
        top.append(&lbl_count);
        content.append(&top);
        let g = adw::PreferencesGroup::new();
        g.set_title(&esc(&trf("Installed Default Applications ({count})", &[("count", &installed.len())])));
        g.set_description(Some(&tr("Checked applications will be uninstalled. Uncheck any app to keep it.")));
        inner.append(&g);
        for it in &installed {
            let row = adw::ActionRow::new();
            row.set_title(&esc(&tr(&it.title)));
            row.set_subtitle(&esc(&tr(&it.description)));
            let img = gtk::Image::from_icon_name(&it.icon);
            img.set_pixel_size(24);
            row.add_prefix(&img);
            let chk = gtk::CheckButton::new();
            chk.set_active(true);
            chk.set_valign(gtk::Align::Center);
            row.add_suffix(&chk);
            row.set_activatable_widget(Some(&chk));
            g.add(&row);
            cl.push((chk, it.package.clone()));
        }
    } else {
        let sp = adw::StatusPage::new();
        sp.set_icon_name(Some("emblem-ok-symbolic"));
        sp.set_title(&tr("System is Clean"));
        sp.set_description(Some(&tr("0 bloatware packages found. All default bloatware apps have been removed from this system.")));
        inner.append(&sp);
    }
    let checks = Rc::new(cl);
    let _ = &checks;
    if !clean.is_empty() {
        let cg = adw::PreferencesGroup::new();
        let ex = adw::ExpanderRow::new();
        ex.set_title(&esc(&trf("Already Removed / Clean ({count})", &[("count", &clean.len())])));
        ex.set_subtitle(&tr("Applications not installed on this system"));
        ex.set_expanded(installed.is_empty());
        for it in &clean {
            let r = adw::ActionRow::new();
            r.set_title(&esc(&tr(&it.title)));
            r.set_subtitle(&esc(&tr(&it.description)));
            let i = gtk::Image::from_icon_name(&it.icon);
            i.set_pixel_size(20);
            r.add_prefix(&i);
            let p = gtk::Label::new(Some(&tr("Clean")));
            p.add_css_class("status-pill-running");
            p.set_valign(gtk::Align::Center);
            r.add_suffix(&p);
            ex.add_row(&r);
        }
        cg.add(&ex);
        inner.append(&cg);
    }
    content.append(&sc);
    let pbox = gtk::Box::new(gtk::Orientation::Vertical, 6);
    pbox.set_visible(false);
    let lbl_prog = gtk::Label::new(Some(&tr("Starting package removal...")));
    lbl_prog.set_xalign(0.0);
    lbl_prog.add_css_class("caption");
    pbox.append(&lbl_prog);
    let pbar = gtk::ProgressBar::new();
    pbar.set_show_text(true);
    pbox.append(&pbar);
    content.append(&pbox);
    let bottom = gtk::Box::new(gtk::Orientation::Horizontal, 10);
    bottom.set_halign(gtk::Align::End);
    content.append(&bottom);
    if !installed.is_empty() {
        let cancel = gtk::Button::with_label(&tr("Cancel"));
        cancel.set_size_request(80, 38);
        let d = dlg.clone();
        cancel.connect_clicked(move |_| d.close());
        bottom.append(&cancel);
        let confirm = gtk::Button::with_label(&tr("Remove Selected"));
        confirm.add_css_class("destructive-action");
        confirm.set_size_request(160, 38);
        bottom.append(&confirm);
        let update = {
            let (checks, lbl, confirm) = (checks.clone(), lbl_count.clone(), confirm.clone());
            Rc::new(move || {
                let sel = checks.iter().filter(|(c, _)| c.is_active()).count();
                lbl.set_label(&trf("{selected} of {total} selected", &[("selected", &sel), ("total", &checks.len())]));
                confirm.set_sensitive(sel > 0);
                confirm.set_label(&trf("Remove Selected ({selected})", &[("selected", &sel)]));
            })
        };
        update();
        for (c, _) in checks.iter() {
            let u = update.clone();
            c.connect_toggled(move |_| u());
        }
        for (b, state) in [(&btn_sel, true), (&btn_desel, false)] {
            let (checks, u) = (checks.clone(), update.clone());
            b.connect_clicked(move |_| {
                for (c, _) in checks.iter() {
                    c.set_active(state);
                }
                u();
            });
        }
        let (d, checks2) = (dlg.clone(), checks.clone());
        confirm.connect_clicked(move |b| {
            let pk: Vec<String> = checks2.iter().filter(|(c, _)| c.is_active()).map(|(_, p)| p.clone()).collect();
            if pk.is_empty() {
                return;
            }
            removing.set(true);
            b.set_sensitive(false);
            cancel.set_sensitive(false);
            btn_sel.set_sensitive(false);
            btn_desel.set_sensitive(false);
            for (c, _) in checks2.iter() {
                c.set_sensitive(false);
            }
            pbox.set_visible(true);
            pbar.set_fraction(0.0);
            let prog: Arc<Mutex<(f64, String)>> = Arc::new(Mutex::new((0.0, tr("Starting package removal..."))));
            let (p2, bar, lp) = (prog.clone(), pbar.clone(), lbl_prog.clone());
            let done_flag = Rc::new(Cell::new(false));
            let df = done_flag.clone();
            every(100, move || {
                let (f, m) = p2.lock().unwrap().clone();
                bar.set_fraction(f.clamp(0.0, 1.0));
                lp.set_label(&m);
                !df.get()
            });
            let (d2, of, rm) = (d.clone(), on_finished.clone(), removing.clone());
            run_async(
                move || {
                    debloat::debloat_packages(&pk, &|c, t, m| {
                        *prog.lock().unwrap() = (if t > 0 { c as f64 / t as f64 } else { 0.0 }, m);
                    })
                },
                move |(ok, msg)| {
                    done_flag.set(true);
                    rm.set(false);
                    d2.close();
                    of(ok, msg);
                },
            );
        });
    } else {
        let bd = gtk::Button::with_label(&tr("Clean Dash Favorites"));
        bd.add_css_class("secondary-btn");
        bd.set_size_request(160, 38);
        let (d, of) = (dlg.clone(), on_finished.clone());
        bd.connect_clicked(move |b| {
            b.set_sensitive(false);
            let (d2, of2) = (d.clone(), of.clone());
            run_async(
                || {
                    debloat::clean_dash_favorites();
                    debloat::move_seahorse_to_utilities();
                },
                move |_| {
                    d2.close();
                    of2(true, "Dash favorites and app grid cleaned.".into());
                },
            );
        });
        bottom.append(&bd);
        let close = gtk::Button::with_label(&tr("Close"));
        close.set_size_request(80, 38);
        let d = dlg.clone();
        close.connect_clicked(move |_| d.close());
        bottom.append(&close);
    }
    dlg.present();
}
