//! Autodarts page: the official Autodarts v2 console ('ad') embedded in a VTE terminal that scales its font to
//! fit, plus restart / reinstall / uninstall tools.
use super::helpers::{icon_button, run_async};
use super::{Page, Win};
use crate::core::autodarts::{get_autodarts_cli_binary, install_autodarts, is_autodarts_installed, uninstall_autodarts};
use crate::core::i18n::{tr, trf};
use crate::core::util::home;
use adw::prelude::*;
use gtk::{gdk, glib, pango};
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use vte::prelude::*;

/// Grid the Autodarts TUI needs to show every section without its own scrollbar (measured on v2.1).
const TUI_COLS: i64 = 90;
const TUI_ROWS: i64 = 27;
const FONT_SCALE_MIN: f64 = 0.5;
const FONT_SCALE_MAX: f64 = 1.6;

pub struct AutodartsView {
    nav: adw::NavigationPage,
    win: Win,
    overlay: gtk::Overlay,
    terminal: RefCell<vte::Terminal>,
    banner: gtk::Box,
    lbl_msg: gtk::Label,
    btn_start: gtk::Button,
    btn_restart: gtk::Button,
    spinner: adw::Spinner,
    busy: Cell<bool>,
    child: Cell<Option<glib::Pid>>,
    console_active: Cell<bool>,
    spawn_pending: Cell<bool>,
    page_active: Cell<bool>,
    spawn_wait: RefCell<Option<glib::SourceId>>,
    fit_timer: RefCell<Option<glib::SourceId>>,
    last_size: Cell<(i32, i32)>,
    fitting: Cell<bool>,
    generation: Cell<u64>,
}

impl Page for AutodartsView {
    fn nav(&self) -> &adw::NavigationPage {
        &self.nav
    }

    fn refresh(self: Rc<Self>) {
        let m = self.clone();
        run_async(is_autodarts_installed, move |inst| {
            if !inst {
                m.banner.set_visible(true);
                m.lbl_msg.set_text(&tr("Autodarts is not installed."));
                m.btn_start.set_visible(false);
            }
            m.btn_restart.set_sensitive(inst);
        });
    }

    /// Navigating away: terminate the embedded console so V4L2 handles are released.
    fn on_closed(self: Rc<Self>) {
        if !self.page_active.replace(false) {
            return;
        }
        log::info!("AutodartsView closed: terminating child console process if running");
        if let Some(id) = self.spawn_wait.borrow_mut().take() {
            id.remove();
        }
        if let Some(id) = self.fit_timer.borrow_mut().take() {
            id.remove();
        }
        if let Some(p) = self.child.take() {
            unsafe { libc::kill(p.0, libc::SIGTERM) };
            self.console_active.set(false);
        }
    }
}

impl AutodartsView {
    pub fn new(win: Win) -> Rc<dyn Page> {
        let main = gtk::Box::new(gtk::Orientation::Vertical, 12);
        main.set_margin_top(12);
        main.set_margin_bottom(12);
        main.set_margin_start(16);
        main.set_margin_end(16);
        main.set_vexpand(true);
        let nav = adw::NavigationPage::builder().title(tr("Autodarts")).tag("autodarts").child(&main).build();
        let card = gtk::Box::new(gtk::Orientation::Vertical, 0);
        card.add_css_class("terminal-card");
        card.set_vexpand(true);
        let overlay = gtk::Overlay::new();
        overlay.set_vexpand(true);
        overlay.set_hexpand(true);
        card.append(&overlay);
        let banner = gtk::Box::new(gtk::Orientation::Vertical, 8);
        banner.add_css_class("terminal-overlay-banner");
        banner.set_halign(gtk::Align::Center);
        banner.set_valign(gtk::Align::Center);
        let lbl_msg = gtk::Label::new(Some(&tr("Console is not running.")));
        lbl_msg.add_css_class("title-3");
        banner.append(&lbl_msg);
        let btn_start = gtk::Button::with_label(&tr("Start Console"));
        btn_start.add_css_class("suggested-action");
        banner.append(&btn_start);
        overlay.add_overlay(&banner);
        banner.set_visible(false);
        main.append(&card);
        let exp = adw::ExpanderRow::new();
        exp.set_title(&tr("Tools and Maintenance"));
        let rc = super::helpers::action_row(&tr("Terminal Console"), &tr("Restart the embedded Autodarts session"));
        let btn_restart = icon_button("utilities-terminal-symbolic", "Restart", "secondary-btn", 36, true);
        btn_restart.set_tooltip_text(Some(&tr("Restart the embedded Autodarts terminal console")));
        btn_restart.set_valign(gtk::Align::Center);
        rc.add_suffix(&btn_restart);
        exp.add_row(&rc);
        let ri = super::helpers::action_row(&tr("Software Management"), "");
        let bi = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        bi.set_valign(gtk::Align::Center);
        let spinner = adw::Spinner::new();
        spinner.set_visible(false);
        bi.append(&spinner);
        let btn_reinstall = icon_button("software-update-available-symbolic", "Reinstall", "secondary-btn", 36, true);
        let btn_uninstall = icon_button("user-trash-symbolic", "Uninstall", "secondary-btn-destructive", 36, true);
        bi.append(&btn_reinstall);
        bi.append(&btn_uninstall);
        ri.add_suffix(&bi);
        exp.add_row(&ri);
        let lb = gtk::ListBox::new();
        lb.add_css_class("boxed-list");
        lb.append(&exp);
        main.append(&lb);
        let footer = gtk::Label::new(Some(&tr("Autodarts Console Wrapper")));
        footer.set_xalign(0.0);
        footer.add_css_class("dim-label");
        footer.set_margin_start(4);
        main.append(&footer);
        let me = Rc::new(AutodartsView {
            nav,
            win,
            overlay,
            terminal: RefCell::new(vte::Terminal::new()),
            banner,
            lbl_msg,
            btn_start,
            btn_restart,
            spinner,
            busy: Cell::new(false),
            child: Cell::new(None),
            console_active: Cell::new(false),
            spawn_pending: Cell::new(false),
            page_active: Cell::new(false),
            spawn_wait: RefCell::new(None),
            fit_timer: RefCell::new(None),
            last_size: Cell::new((0, 0)),
            fitting: Cell::new(false),
            generation: Cell::new(0),
        });
        me.new_terminal();
        let m = me.clone();
        me.btn_start.connect_clicked(move |_| m.spawn_console(true));
        let m = me.clone();
        me.btn_restart.connect_clicked(move |_| m.spawn_console(true));
        let m = me.clone();
        btn_reinstall.connect_clicked(move |_| m.install(true));
        let m = me.clone();
        btn_uninstall.connect_clicked(move |_| m.install(false));
        let m = me.clone();
        me.nav.connect_map(move |_| {
            m.page_active.set(true);
            m.start_fit_timer();
            m.clone().refresh();
            if !m.console_active.get() && !m.spawn_pending.get() && m.spawn_wait.borrow().is_none() {
                m.spawn_console(false);
            }
        });
        let m = me.clone();
        me.nav.connect_unmap(move |_| m.clone().on_closed());
        me
    }

    /// Every console session gets a fresh VTE widget: a second child spawned into a reused terminal
    /// intermittently stays blank or aborts.
    fn new_terminal(self: &Rc<Self>) {
        let t = vte::Terminal::new();
        t.set_font(Some(&pango::FontDescription::from_string("Monospace 8.5")));
        t.set_cursor_blink_mode(vte::CursorBlinkMode::Off);
        t.set_scrollback_lines(3000);
        t.set_mouse_autohide(true);
        t.set_vexpand(true);
        t.set_hexpand(true);
        t.set_size_request(360, 200);
        t.set_colors(Some(&gdk::RGBA::parse("#cdd6f4").unwrap()), Some(&gdk::RGBA::parse("#181825").unwrap()), &[]);
        let gen = self.generation.get() + 1;
        self.generation.set(gen);
        let m = Rc::downgrade(self);
        t.connect_child_exited(move |_, status| {
            let Some(m) = m.upgrade() else { return };
            log::info!("Autodarts console exited with status {}", status);
            if m.generation.get() != gen {
                return;
            }
            m.child.set(None);
            m.console_active.set(false);
            m.banner.set_visible(true);
            m.lbl_msg.set_text(&tr("Console exited."));
            m.btn_start.set_visible(true);
        });
        self.overlay.set_child(Some(&t));
        *self.terminal.borrow_mut() = t;
        self.last_size.set((0, 0));
    }

    /// Scale the font so the TUI_COLS x TUI_ROWS grid fits (checked on a timer: GTK4 has no size-allocate signal).
    fn start_fit_timer(self: &Rc<Self>) {
        if self.fit_timer.borrow().is_some() {
            return;
        }
        let m = Rc::downgrade(self);
        let id = super::helpers::every(150, move || {
            let Some(m) = m.upgrade() else { return false };
            m.fit_step();
            true
        });
        *self.fit_timer.borrow_mut() = Some(id);
    }

    fn fit_step(&self) {
        let t = self.terminal.borrow().clone();
        let (w, h) = (t.width(), t.height());
        if w <= 1 || h <= 1 {
            return;
        }
        let scale = t.font_scale();
        if self.fitting.get() {
            // Cells round to whole pixels, so the estimate can land one row/column short
            if (t.column_count() >= TUI_COLS && t.row_count() >= TUI_ROWS) || scale <= FONT_SCALE_MIN {
                self.fitting.set(false);
            } else {
                t.set_font_scale((scale - 0.02).max(FONT_SCALE_MIN));
            }
            return;
        }
        if self.last_size.get() == (w, h) {
            return;
        }
        self.last_size.set((w, h));
        let (cw, ch) = (t.char_width() as f64, t.char_height() as f64);
        if cw <= 0.0 || ch <= 0.0 || scale <= 0.0 {
            return;
        }
        let (cw0, ch0) = (cw / scale, ch / scale);
        let target = ((w as f64 / (TUI_COLS as f64 * cw0)).min(h as f64 / (TUI_ROWS as f64 * ch0)) * 0.97).clamp(FONT_SCALE_MIN, FONT_SCALE_MAX);
        if (target - scale).abs() > 0.02 {
            t.set_font_scale(target);
            self.fitting.set(true);
        }
    }

    fn spawn_console(self: &Rc<Self>, force: bool) {
        let Some(bin) = get_autodarts_cli_binary() else {
            log::warn!("No autodarts binary found to spawn");
            self.banner.set_visible(true);
            self.lbl_msg.set_text(&tr("Autodarts binary not found."));
            self.btn_start.set_visible(false);
            return;
        };
        if force {
            if let Some(p) = self.child.take() {
                unsafe { libc::kill(p.0, libc::SIGKILL) };
                self.console_active.set(false);
            }
        }
        let busy = self.child.get().is_some() || self.spawn_pending.get() || self.spawn_wait.borrow().is_some();
        if busy && !force {
            return;
        }
        if let Some(id) = self.spawn_wait.borrow_mut().take() {
            id.remove();
        }
        self.banner.set_visible(false);
        self.new_terminal();
        self.start_fit_timer();
        // Start the TUI only once the terminal has its real size and font scale
        let tries = Rc::new(Cell::new(0));
        let m = Rc::downgrade(self);
        let id = super::helpers::every(50, move || {
            let Some(m) = m.upgrade() else { return false };
            let t = m.terminal.borrow().clone();
            let sized = t.width() > 1 && !m.fitting.get() && m.last_size.get() != (0, 0);
            tries.set(tries.get() + 1);
            if !sized && tries.get() < 40 {
                return true;
            }
            *m.spawn_wait.borrow_mut() = None;
            if m.page_active.get() && !m.console_active.get() {
                m.start_child(&bin);
            }
            false
        });
        *self.spawn_wait.borrow_mut() = Some(id);
    }

    fn start_child(self: &Rc<Self>, bin: &str) {
        self.spawn_pending.set(true);
        let t = self.terminal.borrow().clone();
        let gen = self.generation.get();
        let m = self.clone();
        let h = home().to_string_lossy().to_string();
        t.spawn_async(
            vte::PtyFlags::DEFAULT,
            Some(&h),
            &[bin],
            &[],
            glib::SpawnFlags::DEFAULT,
            || {},
            -1,
            gtk::gio::Cancellable::NONE,
            move |r| {
                if m.generation.get() != gen {
                    // Superseded by a forced respawn while this one was in flight
                    if let Ok(p) = r {
                        unsafe { libc::kill(p.0, libc::SIGTERM) };
                    }
                    return;
                }
                m.spawn_pending.set(false);
                match r {
                    Ok(p) if !m.page_active.get() => {
                        log::info!("Autodarts console spawned after page closed; terminating PID {}", p.0);
                        unsafe { libc::kill(p.0, libc::SIGTERM) };
                    }
                    Ok(p) => {
                        log::info!("VTE spawned Autodarts console (PID {})", p.0);
                        m.child.set(Some(p));
                        m.console_active.set(true);
                        m.banner.set_visible(false);
                        m.terminal.borrow().grab_focus();
                    }
                    Err(e) => {
                        log::error!("VTE spawn error: {}", e);
                        m.console_active.set(false);
                        m.child.set(None);
                        m.banner.set_visible(true);
                        m.lbl_msg.set_text(&trf("Spawn failed: {e}", &[("e", &e)]));
                        m.btn_start.set_visible(true);
                    }
                }
            },
        );
    }

    fn install(self: &Rc<Self>, install: bool) {
        if self.busy.replace(true) {
            return;
        }
        self.spinner.set_visible(true);
        self.win.show_toast(if install { "Installing Autodarts..." } else { "Uninstalling Autodarts..." });
        let m = self.clone();
        run_async(move || if install { install_autodarts() } else { uninstall_autodarts() }, move |(ok, msg)| {
            m.busy.set(false);
            m.spinner.set_visible(false);
            m.win.show_toast(&msg);
            if !install {
                if let Some(p) = m.child.take() {
                    unsafe { libc::kill(p.0, libc::SIGKILL) };
                    m.console_active.set(false);
                }
            }
            m.clone().refresh();
            if install && ok {
                let m2 = m.clone();
                super::helpers::once(1500, move || m2.spawn_console(true));
            }
        });
    }
}
