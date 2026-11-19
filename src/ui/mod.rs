//! GTK4 / Libadwaita user interface.
pub mod advanced_users_view;
pub mod autodarts_view;
pub mod autoglow_view;
pub mod camera_controls_view;
pub mod camera_focus_view;
pub mod camera_optimizer_view;
pub mod camera_tools_view;
pub mod camera_widgets;
pub mod dialogs;
pub mod fast_boot_view;
pub mod helpers;
pub mod hw_overlay_view;
pub mod kiosk_view;
pub mod menu;
pub mod rotation_view;
pub mod screen_touch_view;
pub mod system_view;
pub mod upgrade_repair;
pub mod wireless_screen_view;
pub mod window;

pub use window::Win;
use std::rc::Rc;

/// A page in the navigation view. Pages are cached by the window and told when they show and hide.
pub trait Page {
    fn nav(&self) -> &adw::NavigationPage;
    fn on_opened(self: Rc<Self>) {}
    fn on_closed(self: Rc<Self>) {}
    fn refresh(self: Rc<Self>) {}
    /// Return true when the page asks first and calls `go` itself once leaving is fine.
    fn confirm_leave(self: Rc<Self>, _go: Box<dyn Fn()>) -> bool {
        false
    }
    /// Page specific switches set from elsewhere (e.g. the menu's AutoGlow update badge).
    fn set_flag(&self, _name: &str, _on: bool) {}
}

use adw::prelude::*;

/// Build the application: `suit` with no subcommand; `start` opens a page right away.
pub fn run_app(start: Option<fn(&Win)>) -> i32 {
    gtk::glib::set_prgname(Some("de.iterathor.suit.gtk"));
    gtk::glib::set_application_name("SUIT for Fedora");
    let app = adw::Application::builder().application_id("de.iterathor.suit.gtk").build();
    let win: std::rc::Rc<std::cell::RefCell<Option<Win>>> = Default::default();
    let w2 = win.clone();
    app.connect_activate(move |app| {
        log::info!("Activating SUIT for Fedora GTK4 Application");
        // Force/prefer dark mode (matching GNOME native dark palette)
        adw::StyleManager::default().set_color_scheme(adw::ColorScheme::PreferDark);
        gtk::Window::set_default_icon_name("de.iterathor.suit.gtk");
        let first = w2.borrow().is_none();
        if first {
            *w2.borrow_mut() = Some(Win::new(app));
        }
        let w = w2.borrow().as_ref().unwrap().clone();
        w.present();
        if let Some(open) = start.filter(|_| first) {
            open(&w);
        }
    });
    app.connect_shutdown(|_| {
        log::info!("Shutting down SUIT for Fedora GTK4 Application");
        crate::core::systemd::manager::ensure_autodarts_restored_on_exit();
    });
    let code = app.run_with_args::<&str>(&[]);
    crate::core::systemd::manager::ensure_autodarts_restored_on_exit();
    code.into()
}

/// `suit wireless-screen --screenshots DIR`: render the main menu, the Screen & Touch hub and every
/// Wireless Screen state (fake data, nothing on the PC is read or changed) at 760x640 and 520x700 to
/// DIR/NN-name.png, then quit. GTK renders the window itself, so no compositor screenshot API is needed.
pub fn screenshots(dir: std::path::PathBuf) -> i32 {
    use std::cell::Cell;
    use std::rc::Rc;
    use std::sync::atomic::Ordering;
    if let Err(e) = std::fs::create_dir_all(&dir) {
        eprintln!("{}: {}", dir.display(), e);
        return 1;
    }
    wireless_screen_view::DEMO.store(true, Ordering::Relaxed);
    std::env::set_var("SUIT_NO_HEAL", "1");
    // a separate app id, so a running SUIT is not just activated
    let app = adw::Application::builder().application_id("de.iterathor.suit.screenshots").flags(gtk::gio::ApplicationFlags::NON_UNIQUE).build();
    let failed = Rc::new(Cell::new(false));
    let f2 = failed.clone();
    app.connect_activate(move |app| {
        adw::StyleManager::default().set_color_scheme(adw::ColorScheme::PreferDark);
        let w = Win::new(app);
        w.present();
        // (file name, size, action)
        type Step = (String, (i32, i32), Box<dyn Fn(&Win)>);
        let mut steps: Vec<Step> = vec![
            ("01-menu".into(), (760, 640), Box::new(|_: &Win| {})),
            ("02-hub".into(), (760, 640), Box::new(|w: &Win| w.open_screen_touch_page())),
        ];
        for (i, name) in wireless_screen_view::DEMO_STATES.iter().enumerate() {
            for (suffix, size) in [("", (760, 640)), ("-narrow", (520, 700))] {
                let n = name.to_string();
                steps.push((
                    format!("{:02}-{}{}", i + 3, name, suffix),
                    size,
                    Box::new(move |w: &Win| {
                        w.open_wireless_screen_page();
                        let v = wireless_screen_view::DEMO_VIEW.with(|v| v.borrow().clone());
                        if let Some(v) = v {
                            v.demo_state(&n);
                        }
                    }),
                ));
            }
        }
        let steps = Rc::new(steps);
        let (i, app, failed, dir) = (Rc::new(Cell::new(0usize)), app.clone(), f2.clone(), dir.clone());
        // every step: act and size the window, then render it on the next tick
        let acted = Rc::new(Cell::new(false));
        helpers::every(700, move || {
            let n = i.get();
            let Some((name, (sw, sh), act)) = steps.get(n) else {
                app.quit();
                return false;
            };
            if !acted.get() {
                act(&w);
                w.window.set_default_size(*sw, *sh);
                acted.set(true);
                return true;
            }
            acted.set(false);
            i.set(n + 1);
            let path = dir.join(format!("{}.png", name));
            if !save_png(&w.window, &path) {
                failed.set(true);
            }
            println!("{}", path.display());
            true
        });
    });
    app.run_with_args::<&str>(&[]);
    if failed.get() { 1 } else { 0 }
}

/// Render `window` with GTK's own renderer to a PNG.
fn save_png(window: &adw::ApplicationWindow, path: &std::path::Path) -> bool {
    let (w, h) = (window.width(), window.height());
    let paintable = gtk::WidgetPaintable::new(Some(window));
    let snap = gtk::Snapshot::new();
    paintable.snapshot(&snap, w as f64, h as f64);
    let (Some(node), Some(renderer)) = (snap.to_node(), window.native().and_then(|n| n.renderer())) else {
        eprintln!("{}: nothing to render", path.display());
        return false;
    };
    let tex = renderer.render_texture(node, Some(&gtk::graphene::Rect::new(0.0, 0.0, w as f32, h as f32)));
    match tex.save_to_png(path) {
        Ok(_) => true,
        Err(e) => {
            eprintln!("{}: {}", path.display(), e);
            false
        }
    }
}
