//! SUIT for Fedora: Setup Utilities by IteraThor for Autodarts (Rust port).
//!
//! `suit` starts the GTK app; subcommands run the background parts that the app installs:
//!   suit camera-keeper [--interval 2] [--once]   keep saved camera settings applied (user service)
//!   suit kiosk-host                              Chrome native messaging host for the kiosk extension
//!   suit display --apply-boot                    re-apply rotation and touch calibration at login
//!   suit display                                 list monitors and touch screens
//!   suit install-extensions                      pre-install the SUIT GNOME Shell extensions
//!   suit create-launcher                         write the desktop launchers
//!   suit exposure-lock                           remove a leftover suit-exposure-lock.service
mod core;
mod launcher;
mod ui;
#[cfg(test)]
mod tests;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let sub = args.first().map(|s| s.as_str()).unwrap_or("");
    let rest = if args.is_empty() { &[][..] } else { &args[1..] };
    match sub {
        "camera-keeper" => {
            core::logger::setup_console_logging();
            std::process::exit(core::hbvcal::daemon::main(rest));
        }
        "kiosk-host" => {
            core::logger::setup_logging(Some(&std::env::temp_dir().join("suit_kiosk_host.log")));
            std::process::exit(core::kiosk_host::main());
        }
        "display" => {
            core::logger::setup_logging(None);
            if rest.iter().any(|a| a == "--apply-boot") {
                core::display::apply_boot_config();
            } else {
                for m in core::display::get_monitors() {
                    println!("Monitor: {} {} rotation={} primary={}", m.name, m.res, m.rotation, m.is_primary);
                }
                for t in core::display::get_touchscreens() {
                    println!("Touchscreen: {}", t);
                }
            }
        }
        "install-extensions" => {
            core::logger::setup_logging(None);
            let _ = core::desktop::install_suit_extensions();
        }
        "ensure-autostart" => {
            core::logger::setup_logging(None);
            core::display::ensure_autostart();
        }
        "create-launcher" => {
            core::logger::setup_logging(None);
            std::process::exit(launcher::main());
        }
        "exposure-lock" => {
            core::logger::setup_console_logging();
            core::hbvcal::service::exposure_lock_cleanup();
        }
        "--version" | "-V" => println!("suit {}", env!("CARGO_PKG_VERSION")),
        "--help" | "-h" => println!("{}", include_str!("main.rs").lines().skip(2).take(8).map(|l| l.trim_start_matches("//!")).collect::<Vec<_>>().join("\n")),
        _ => {
            core::logger::setup_logging(None);
            std::process::exit(ui::run_app());
        }
    }
}
