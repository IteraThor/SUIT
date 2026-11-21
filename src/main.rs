//! SUIT for Fedora: Setup Utilities by IteraThor for Autodarts (Rust port).
//!
//! `suit` starts the GTK app; subcommands run the background parts that the app installs:
//!   suit camera-keeper [--interval 2] [--once]   keep saved camera settings applied (user service)
//!   suit kiosk-host                              Chrome native messaging host for the kiosk extension
//!   suit display --apply-boot                    re-apply rotation and touch calibration at login
//!   suit display                                 list monitors and touch screens
//!   suit wireless-screen --watch                 mirror or extend a plugged-in monitor onto the tablet screen
//!   suit wireless-screen --remove                undo the Wireless Screen setup (also --status, --help)
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
        "wireless-screen" => {
            core::logger::setup_logging(None);
            std::process::exit(wireless_cli(rest));
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
        "hbv-probe" => {
            core::logger::setup_console_logging();
            std::process::exit(core::hbvcal::probe::main(rest));
        }
        "exposure-lock" => {
            core::logger::setup_console_logging();
            core::hbvcal::service::exposure_lock_cleanup();
        }
        "--version" | "-V" => println!("suit {}", env!("CARGO_PKG_VERSION")),
        "--help" | "-h" => println!("{}", include_str!("main.rs").lines().skip(2).take(10).map(|l| l.trim_start_matches("//!")).collect::<Vec<_>>().join("\n")),
        _ => {
            core::logger::setup_logging(None);
            std::process::exit(ui::run_app(None));
        }
    }
}

const WIRELESS_HELP: &str = "suit wireless-screen OPTION (same functions as the Wireless Screen page; never restarts the PC)
  --status                  status, preflight and checklist as JSON
  --setup W H [HZ]          Start step: virtual screen on a free port (size fitted to it), watcher service
  --install                 install and configure Sunshine
  --apply W H [HZ]          new tablet size on the port set up
  --pair PIN [NAME]         send the PIN Moonlight shows
  --check [--fix]           checklist; --fix repairs every red item it can
  --layout mirror|virtual|extend   arrange the screens now (temporary)
  --client                  size the last tablet asked for and whether one streams now
  --hide-cursor on|off      hide the cursor while streaming
  --remove                  undo everything (safe to run twice)
  --watch                   monitor watcher (the service runs it)
  --resume                  open SUIT on the Wireless Screen page
  --screenshots DIR         render the wizard screens to PNG files (fake data, changes nothing)";

/// `suit wireless-screen ...`: the Wireless Screen page without a GUI. Exit 0 ok, 1 failed, 2 usage.
fn wireless_cli(rest: &[String]) -> i32 {
    use core::wireless_screen as ws;
    let arg = |i: usize| rest.get(i).map(|s| s.as_str()).unwrap_or("");
    let num = |i: usize| arg(i).parse::<u32>().ok();
    let say = |t: &str| println!("{}", t);
    let done = |(ok, msg): (bool, String)| {
        println!("{}", msg);
        if ok { 0 } else { 1 }
    };
    let size = || match (num(1), num(2)) {
        (Some(w), Some(h)) if ws::parse_resolution(&format!("{}x{}", w, h)).is_some() => Some((w, h, num(3).unwrap_or(60))),
        _ => None,
    };
    match arg(0) {
        "--watch" => {
            ws::watch();
            0
        }
        "--resume" => {
            // one-shot after the setup restart: open SUIT on the Wireless Screen page
            ws::remove_resume_autostart();
            ui::run_app(Some(|w| {
                w.open_wireless_screen_page();
                // the kiosk browser autostarts later and would cover the wizard
                w.keep_in_front(90);
            }))
        }
        "--screenshots" if !arg(1).is_empty() => ui::screenshots(std::path::PathBuf::from(arg(1))),
        "--status" => {
            println!("{}", serde_json::to_string_pretty(&ws::report()).unwrap_or_default());
            0
        }
        "--setup" => match size() {
            Some((w, h, hz)) => done(ws::setup(w, h, hz, &say)),
            None => {
                eprintln!("{}", WIRELESS_HELP);
                2
            }
        },
        "--apply" => match size() {
            Some((w, h, hz)) => done(ws::apply_size(w, h, hz, &say)),
            None => {
                eprintln!("{}", WIRELESS_HELP);
                2
            }
        },
        "--install" => done(ws::install_sunshine(&say)),
        "--pair" if !arg(1).is_empty() => done(ws::pair(arg(1), arg(2))),
        "--check" => {
            let fix = arg(1) == "--fix";
            let mut checks = ws::checklist(&ws::status());
            if fix {
                for c in checks.iter().filter(|c| !c.ok) {
                    let (ok, msg) = ws::fix(c.id, &say);
                    println!("fix {}: {} {}", c.id, if ok { "ok" } else { "FAILED" }, msg);
                }
                checks = ws::checklist(&ws::status());
            }
            for c in &checks {
                println!("{:4} {:8} {}", if c.ok { "ok" } else { "FAIL" }, c.id, c.detail);
            }
            if checks.iter().all(|c| c.ok) { 0 } else { 1 }
        }
        "--layout" => {
            let cfg = ws::load_config();
            let ok = match arg(1) {
                "mirror" => ws::apply_layout_as(&cfg, ws::Layout::Mirror),
                "extend" => ws::apply_layout_as(&cfg, ws::Layout::Extend),
                "virtual" => ws::apply_layout_as(&cfg, ws::Layout::TabletOnly),
                _ => {
                    eprintln!("{}", WIRELESS_HELP);
                    return 2;
                }
            };
            println!("{}", if ok { "layout applied" } else { "layout not applied (virtual screen off?)" });
            if ok { 0 } else { 1 }
        }
        "--client" => {
            let s = ws::status();
            match s.client_mode {
                Some((w, h, hz)) => println!("requested: {}x{}@{}", w, h, hz),
                None => println!("requested: unknown"),
            }
            println!("session: {}", if s.streaming { "active" } else { "none" });
            match s.tablet_offer() {
                Some((w, h)) => println!("offer: {}x{}", w, h),
                None => println!("offer: none"),
            }
            0
        }
        "--hide-cursor" if matches!(arg(1), "on" | "off") => done(ws::set_hide_cursor(arg(1) == "on")),
        "--remove" => done(ws::remove_everything()),
        "--help" | "-h" => {
            println!("{}", WIRELESS_HELP);
            0
        }
        _ => {
            eprintln!("{}", WIRELESS_HELP);
            2
        }
    }
}
