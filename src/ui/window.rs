//! Main application window: header bar with back button, navigation view, toasts, page cache, first-run
//! privileges prompt, background heal and the post-upgrade repair offer.
use super::helpers::{alert, ask, message, once, run_async, run_bg};
use super::Page;
use crate::core::i18n::{set_language, tr, trf};
use crate::core::{privileges, update, upgrade, util};
use adw::prelude::*;
use gtk::{gdk, glib};
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::rc::Rc;
use std::time::{Duration, Instant};

/// Large pages with a live view: they open the window maximized
const CAMERA_PAGES: [&str; 2] = ["camera_controls", "camera_optimizer"];
/// Longest the window stays open after a close request while a Camera Optimizer job runs
const JOB_WAIT_S: u64 = 360;

pub struct WinInner {
    pub window: adw::ApplicationWindow,
    pub toast_overlay: adw::ToastOverlay,
    pub nav: adw::NavigationView,
    pub btn_back: gtk::Button,
    pub title: adw::WindowTitle,
    pages: RefCell<HashMap<String, Rc<dyn Page>>>,
    menu: RefCell<Option<Rc<dyn Page>>>,
    last_visible: RefCell<Option<adw::NavigationPage>>,
    close_ready: Cell<bool>,
}

/// Cheap handle to the window, shared by all pages.
#[derive(Clone)]
pub struct Win(pub Rc<WinInner>);

impl std::ops::Deref for Win {
    type Target = WinInner;
    fn deref(&self) -> &WinInner {
        &self.0
    }
}

fn screen_size() -> (i32, i32) {
    gdk::Display::default()
        .and_then(|d| d.monitors().item(0))
        .and_then(|m| m.downcast::<gdk::Monitor>().ok())
        .map(|m| (m.geometry().width(), m.geometry().height()))
        .unwrap_or((1280, 800))
}

impl Win {
    pub fn new(app: &adw::Application) -> Win {
        let (sw, sh) = screen_size();
        let window = adw::ApplicationWindow::builder()
            .application(app)
            .title(tr("SUIT for Fedora"))
            .default_width(840.min((sw - 60).max(560)))
            .default_height(720.min((sh - 90).max(540)))
            .build();
        window.set_size_request(380, 420);
        window.set_icon_name(Some("de.iterathor.suit.gtk"));
        let pd = util::project_dir();
        if let Some(display) = gdk::Display::default() {
            let icons = pd.join("assets/icons");
            if icons.exists() {
                gtk::IconTheme::for_display(&display).add_search_path(&icons);
            }
            let css = pd.join("style_gtk.css");
            if css.exists() {
                let p = gtk::CssProvider::new();
                p.load_from_path(&css);
                gtk::style_context_add_provider_for_display(&display, &p, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
            }
        }
        let toast_overlay = adw::ToastOverlay::new();
        window.set_content(Some(&toast_overlay));
        let bx = gtk::Box::new(gtk::Orientation::Vertical, 0);
        toast_overlay.set_child(Some(&bx));
        let header = adw::HeaderBar::new();
        bx.append(&header);
        let btn_back = gtk::Button::from_icon_name("go-previous-symbolic");
        btn_back.add_css_class("flat");
        btn_back.set_tooltip_text(Some(&tr("Back to Menu")));
        btn_back.set_size_request(30, 28);
        btn_back.set_visible(false);
        header.pack_start(&btn_back);
        let title = adw::WindowTitle::new(&tr("SUIT for Fedora"), "");
        header.set_title_widget(Some(&title));
        let nav = adw::NavigationView::new();
        nav.set_vexpand(true);
        bx.append(&nav);
        let w = Win(Rc::new(WinInner {
            window,
            toast_overlay,
            nav,
            btn_back,
            title,
            pages: RefCell::new(HashMap::new()),
            menu: RefCell::new(None),
            last_visible: RefCell::new(None),
            close_ready: Cell::new(false),
        }));
        let w2 = w.clone();
        w.btn_back.connect_clicked(move |_| w2.on_back_clicked());
        let w2 = w.clone();
        w.nav.connect_visible_page_notify(move |_| w2.on_nav_changed());
        w.push_menu();
        let w2 = w.clone();
        glib::idle_add_local_once(move || w2.check_first_run_privileges());
        w.start_background_heal();
        let w2 = w.clone();
        w.window.connect_close_request(move |_| w2.on_close_request());
        if std::env::var_os("SUIT_SMOKE_PAGES").is_some() {
            w.smoke_pages();
        }
        w
    }

    /// SUIT_SMOKE_PAGES=1: open every page that does not touch the cameras once, then quit (a start-up test).
    fn smoke_pages(&self) {
        type Open = fn(&Win);
        let pages: Vec<(&str, Open)> = vec![
            ("system", |w| w.open_system_page()),
            ("fast_boot", |w| w.open_fast_boot_page()),
            ("hw_overlay", |w| w.open_hw_overlay_page()),
            ("advanced_users", |w| w.open_advanced_users_page()),
            ("autodarts", |w| w.open_autodarts_page()),
            ("camera_tools", |w| w.open_camera_tools_page()),
            ("camera_focus", |w| w.open_focus_page()),
            ("rotation", |w| w.open_rotation_page()),
            ("kiosk", |w| w.open_kiosk_page()),
            ("autoglow", |w| w.open_autoglow_page()),
        ];
        let w = self.clone();
        let i = std::rc::Rc::new(std::cell::Cell::new(0usize));
        super::helpers::every(2500, move || {
            let n = i.get();
            i.set(n + 1);
            if n > 0 {
                w.nav.pop_to_tag("main_menu");
            }
            match pages.get(n) {
                Some((tag, open)) => {
                    log::info!("smoke: opening {}", tag);
                    open(&w);
                    true
                }
                None => {
                    log::info!("smoke: done");
                    w.close_ready.set(true);
                    w.window.close();
                    false
                }
            }
        });
    }

    pub fn present(&self) {
        self.window.present();
    }

    fn push_menu(&self) {
        let m: Rc<dyn Page> = super::menu::MenuPage::new(self.clone());
        *self.last_visible.borrow_mut() = Some(m.nav().clone());
        self.nav.replace(&[m.nav().clone()]);
        *self.menu.borrow_mut() = Some(m);
    }

    fn page_for(&self, np: &adw::NavigationPage) -> Option<Rc<dyn Page>> {
        if let Some(m) = self.menu.borrow().as_ref() {
            if m.nav() == np {
                return Some(m.clone());
            }
        }
        self.pages.borrow().values().find(|p| p.nav() == np).cloned()
    }

    fn on_back_clicked(&self) {
        if let Some(np) = self.nav.visible_page() {
            if let Some(p) = self.page_for(&np) {
                let nav = self.nav.clone();
                if p.clone().confirm_leave(Box::new(move || {
                    nav.pop();
                })) {
                    return;
                }
            }
        }
        self.nav.pop();
    }

    fn on_close_request(&self) -> glib::Propagation {
        if self.close_ready.get() {
            return glib::Propagation::Proceed;
        }
        log::info!("Window close requested: running active page cleanup");
        if let Some(p) = self.nav.visible_page().and_then(|np| self.page_for(&np)) {
            p.clone().on_closed();
        }
        // A Camera Optimizer job runs in this process with Autodarts stopped: keep the window (insensitive)
        // until the job is done, or Autodarts would stay down.
        let job = crate::core::hbvcal::oneclick::JOB.clone();
        if !job.running() {
            return glib::Propagation::Proceed;
        }
        self.window.set_sensitive(false);
        self.title.set_subtitle(&tr("Finishing the camera optimization…"));
        let deadline = Instant::now() + Duration::from_secs(JOB_WAIT_S);
        let w = self.clone();
        super::helpers::every(100, move || {
            if job.running() && Instant::now() < deadline {
                return true;
            }
            if job.running() {
                log::error!("Camera Optimizer job still running after {} s; closing anyway", JOB_WAIT_S);
            }
            w.close_ready.set(true);
            w.window.close();
            false
        });
        glib::Propagation::Stop
    }

    /// Heal boot services (e.g. after a CLI git pull), then check for a Fedora upgrade.
    fn start_background_heal(&self) {
        // SUIT_NO_HEAL=1: a test run next to an installed SUIT must not point its services at this binary
        if std::env::var_os("SUIT_NO_HEAL").is_some() {
            log::info!("SUIT_NO_HEAL set: skipping the background service heal");
            return;
        }
        let w = self.clone();
        run_async(
            || {
                std::thread::sleep(Duration::from_millis(400)); // let the main menu render first
                update::ensure_camera_keeper();
                update::ensure_display_autostart();
                update::ensure_shell_extensions_installed();
                crate::core::kiosk::sync_extension_files();
                upgrade::startup_check()
            },
            move |r| w.on_upgrade_check(Rc::new(r)),
        );
    }

    fn check_first_run_privileges(&self) {
        let w = self.clone();
        run_async(privileges::check_sudo_privileges, move |ok| {
            if ok {
                return;
            }
            let d = alert(
                &tr("Administrator Privileges Required"),
                &tr("SUIT requires administrator rights to configure systemd background services, kiosk mode, and system settings without interrupting you.\n\nAuthorize once to enable seamless passwordless access for future runs?"),
                &[("cancel", &tr("Not Now")), ("auth", &tr("Authorize"))],
                Some("auth"),
                None,
            );
            let w2 = w.clone();
            ask(&w.window, d, move |r| {
                if r == "auth" {
                    let w3 = w2.clone();
                    run_async(
                        || privileges::setup_sudoers_pkexec(None),
                        move |(ok, _)| {
                            w3.show_toast(if ok { "Administrator privileges successfully authorized." } else { "Authorization was not completed." })
                        },
                    );
                }
            });
        });
    }

    /// Startup: after a Fedora upgrade, offer to repair what it broke.
    fn on_upgrade_check(&self, r: Rc<upgrade::StartupCheck>) {
        if r.problems.is_empty() {
            return;
        }
        if r.problems.iter().all(|p| p.waiting_for_login) {
            self.show_toast("Restart the computer to finish the repair after the system update.");
            return;
        }
        if self.window.visible_dialog().is_some() {
            // e.g. the privileges dialog: ask after it
            let w = self.clone();
            once(1000, move || w.on_upgrade_check(r));
            return;
        }
        let lines = r.problems.iter().map(|p| format!("• {}", p.title)).collect::<Vec<_>>().join("\n");
        let (heading, body) = if !r.changes.is_empty() {
            (
                tr("System Update Detected"),
                trf("{changes}\n\nThese SUIT features need repairs to work again:\n{list}", &[("changes", &r.changes.join("\n")), ("list", &lines)]),
            )
        } else {
            (tr("Repairs Needed"), trf("A system update left these SUIT features broken:\n{list}", &[("list", &lines)]))
        };
        let d = alert(&heading, &body, &[("later", &tr("Later")), ("fix", &tr("Fix All"))], Some("fix"), None);
        let w = self.clone();
        ask(&self.window, d, move |resp| {
            if resp == "fix" {
                w.open_upgrade_repair_page(r.problems.clone(), r.changes.clone(), true);
            }
        });
    }

    pub fn open_upgrade_repair_page(&self, problems: Vec<upgrade::Problem>, changes: Vec<String>, start: bool) {
        let p = super::upgrade_repair::UpgradeRepairPage::new(self.clone(), problems, changes);
        self.pages.borrow_mut().insert("upgrade_repair".into(), p.clone());
        self.show_page(p.nav());
        if start {
            p.fix_all();
        }
    }

    fn on_nav_changed(&self) {
        let page = self.nav.visible_page();
        let last = self.last_visible.borrow().clone();
        if let Some(l) = &last {
            if page.as_ref() != Some(l) {
                if let Some(p) = self.page_for(l) {
                    p.clone().on_closed();
                }
            }
        }
        *self.last_visible.borrow_mut() = page.clone();
        let is_menu = self.menu.borrow().as_ref().map(|m| Some(m.nav()) == page.as_ref()).unwrap_or(false);
        match &page {
            Some(p) if !is_menu => {
                self.btn_back.set_visible(true);
                let t = p.title();
                self.title.set_title(if t.is_empty() { tr("SUIT for Fedora") } else { t.to_string() }.as_str());
                self.title.set_subtitle(&tr("SUIT for Fedora"));
            }
            _ => {
                self.btn_back.set_visible(false);
                self.title.set_title(&tr("SUIT for Fedora"));
                self.title.set_subtitle("");
            }
        }
        if let Some(p) = page {
            self.adapt_window_size(&p);
        }
    }

    fn adapt_window_size(&self, page: &adw::NavigationPage) {
        let (sw, sh) = screen_size();
        let (mw, mh) = ((sw - 60).max(420), (sh - 90).max(420));
        let tag = page.tag().map(|t| t.to_string()).unwrap_or_default();
        if CAMERA_PAGES.contains(&tag.as_str()) {
            self.window.maximize();
            return;
        }
        if self.window.is_fullscreen() {
            self.window.unfullscreen();
        }
        if self.window.is_maximized() {
            self.window.unmaximize();
        }
        let (w, h) = match tag.as_str() {
            "main_menu" => (760, 640),
            "camera_tools" => (740, 480),
            "camera_focus" => (940, 640),
            "autodarts" => (840, 680),
            "rotation" => (780, 600),
            "kiosk" => (780, 680),
            "system" => (800, 700),
            "advanced_users" => (800, 720),
            "hw_overlay" => (800, 880),
            _ => (780, 680),
        };
        self.window.set_default_size(w.min(mw), h.min(mh));
    }

    /// Switch the UI language: rebuild the main menu and drop cached pages so they rebuild on next open.
    pub fn set_language(&self, lang: &str) {
        let _ = set_language(lang);
        self.window.set_title(Some(&tr("SUIT for Fedora")));
        self.btn_back.set_tooltip_text(Some(&tr("Back to Menu")));
        // A running Camera Optimizer job needs its page; it keeps the old language until restart
        let keep_opt = crate::core::hbvcal::oneclick::JOB.running();
        self.pages.borrow_mut().retain(|k, _| keep_opt && k == "camera_optimizer");
        self.push_menu();
    }

    pub fn show_toast(&self, text: &str) {
        let t = adw::Toast::new(&tr(text));
        t.set_timeout(3);
        self.toast_overlay.add_toast(t);
    }

    /// Push a page, or pop back to it when it is already in the stack (the camera pages link to each other).
    pub fn show_page(&self, np: &adw::NavigationPage) {
        let stack = self.nav.navigation_stack();
        let in_stack = (0..stack.n_items()).any(|i| stack.item(i).and_then(|o| o.downcast::<adw::NavigationPage>().ok()).as_ref() == Some(np));
        if in_stack {
            self.nav.pop_to_page(np);
        } else {
            self.nav.push(np);
        }
    }

    /// Open (and cache) a page by tag.
    pub fn open<F: FnOnce(Win) -> Rc<dyn Page>>(&self, tag: &str, make: F) -> Rc<dyn Page> {
        let existing = self.pages.borrow().get(tag).cloned();
        let p = match existing {
            Some(p) => p,
            None => {
                let p = make(self.clone());
                self.pages.borrow_mut().insert(tag.into(), p.clone());
                p
            }
        };
        self.show_page(p.nav());
        p.clone().on_opened();
        p.clone().refresh();
        p
    }

    pub fn cached(&self, tag: &str) -> Option<Rc<dyn Page>> {
        self.pages.borrow().get(tag).cloned()
    }

    pub fn open_system_page(&self) {
        self.open("system", super::system_view::SystemView::new);
    }
    pub fn open_autodarts_page(&self) {
        self.open("autodarts", super::autodarts_view::AutodartsView::new);
    }
    pub fn open_camera_tools_page(&self) {
        self.open("camera_tools", super::camera_tools_view::CameraToolsView::new);
    }
    pub fn open_camera_controls_page(&self) {
        self.open("camera_controls", super::camera_controls_view::CameraControlsPage::new);
    }
    pub fn open_camera_optimizer_page(&self) {
        self.open("camera_optimizer", super::camera_optimizer_view::CameraOptimizerPage::new);
    }
    pub fn open_focus_page(&self) {
        self.open("camera_focus", super::camera_focus_view::CameraFocusView::new);
    }
    pub fn open_rotation_page(&self) {
        self.open("rotation", super::rotation_view::RotationView::new);
    }
    pub fn open_kiosk_page(&self) {
        self.open("kiosk", super::kiosk_view::KioskView::new);
    }
    pub fn open_autoglow_page(&self) {
        self.open("autoglow", super::autoglow_view::AutoGlowView::new);
    }
    pub fn open_advanced_users_page(&self) {
        self.open("advanced_users", super::advanced_users_view::AdvancedUsersView::new);
    }
    pub fn open_fast_boot_page(&self) {
        self.open("fast_boot", super::fast_boot_view::FastBootView::new);
    }
    pub fn open_hw_overlay_page(&self) {
        self.open("hw_overlay", super::hw_overlay_view::HwOverlayView::new);
    }

    /// Main menu hook: AutoGlow update badge.
    pub fn set_autoglow_update(&self, available: bool) {
        if let Some(m) = self.menu.borrow().as_ref() {
            m.set_flag("autoglow_update", available);
        }
    }

    pub fn error_dialog(&self, heading: &str, body: &str) {
        message(&self.window, heading, body);
    }
}
