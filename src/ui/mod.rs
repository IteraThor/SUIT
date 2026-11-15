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
pub mod system_view;
pub mod upgrade_repair;
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

/// Build the application: `suit` with no subcommand.
pub fn run_app() -> i32 {
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
        if w2.borrow().is_none() {
            *w2.borrow_mut() = Some(Win::new(app));
        }
        w2.borrow().as_ref().unwrap().present();
    });
    app.connect_shutdown(|_| {
        log::info!("Shutting down SUIT for Fedora GTK4 Application");
        crate::core::systemd::manager::ensure_autodarts_restored_on_exit();
    });
    let code = app.run_with_args::<&str>(&[]);
    crate::core::systemd::manager::ensure_autodarts_restored_on_exit();
    code.into()
}
