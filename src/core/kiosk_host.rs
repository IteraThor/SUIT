//! Chrome native messaging host for the kiosk extension (`suit kiosk-host`): power off, reboot, exit kiosk
//! and dartboard light control. Messages are 4-byte native-endian length + JSON on stdin/stdout.
use super::light;
use super::util::run;
use serde_json::{json, Value};
use std::io::{Read, Write};

const BROWSER_PATTERN: &str = "(chromium|chrome|firefox).*(--kiosk|play.autodarts.com)";

fn read_message() -> Option<Value> {
    let mut len = [0u8; 4];
    std::io::stdin().read_exact(&mut len).ok()?;
    let n = u32::from_ne_bytes(len) as usize;
    let mut buf = vec![0u8; n];
    std::io::stdin().read_exact(&mut buf).ok()?;
    serde_json::from_slice(&buf).ok()
}

fn send_message(v: &Value) -> std::io::Result<()> {
    let b = v.to_string().into_bytes();
    let mut out = std::io::stdout().lock();
    out.write_all(&(b.len() as u32).to_ne_bytes())?;
    out.write_all(&b)?;
    out.flush()
}

fn run_cmd(cmd: &[&str]) -> (i32, String) {
    let r = run(cmd, Some(5));
    log::debug!("Ran {:?} -> exit {}, out={:?}, err={:?}", cmd, r.code, r.stdout, r.stderr);
    (r.code, r.stderr)
}

fn power(action: &str) -> Value {
    let cmds: [Vec<&str>; 3] = [
        vec!["systemctl", action, "-i", "--no-ask-password", "--no-block"],
        vec!["sudo", "-n", "/usr/bin/systemctl", action, "-i", "--no-ask-password", "--no-block"],
        vec!["loginctl", action, "-i", "--no-ask-password"],
    ];
    let mut last = String::new();
    for c in &cmds {
        let (code, err) = run_cmd(c);
        if code == 0 {
            // Terminate kiosk browser after signaling shutdown
            run_cmd(&["pkill", "-TERM", "-f", BROWSER_PATTERN]);
            return json!({"status": "ok", "action": action});
        }
        last = if err.trim().is_empty() { format!("exit code {}", code) } else { err };
    }
    log::error!("All {} attempts failed. Last err: {}", action, last);
    let what = if action == "poweroff" { "Power off" } else { "Reboot" };
    json!({"status": "error", "error": format!("{} failed: {}", what, if last.is_empty() { "permission denied".into() } else { last })})
}

pub fn handle_action(action: &str) -> Value {
    log::info!("Handling action: {}", action);
    match action {
        "poweroff" | "reboot" => power(action),
        "exit_kiosk" => {
            run_cmd(&["pkill", "-TERM", "-f", BROWSER_PATTERN]);
            json!({"status": "ok", "action": "exit_kiosk"})
        }
        "ping" => json!({"status": "ok", "action": "ping", "message": "SUIT Kiosk Host ready"}),
        "get_light_status" => {
            let c = light::read_config();
            if !c.light_enabled {
                return json!({"status": "ok", "action": "get_light_status", "enabled": false, "is_on": false, "ip": c.light_ip});
            }
            let (reachable, on, msg) = light::query_status(Some(&c), 0.8);
            json!({"status": "ok", "action": "get_light_status", "enabled": true, "device_type": c.light_device_type,
                   "ip": c.light_ip, "reachable": reachable, "is_on": on, "message": msg})
        }
        "toggle_light" => {
            let (ok, on, msg) = light::toggle_light(None, 1.2);
            if ok {
                json!({"status": "ok", "action": "toggle_light", "is_on": on, "message": msg})
            } else {
                json!({"status": "error", "action": "toggle_light", "error": msg})
            }
        }
        a => json!({"status": "error", "error": format!("Unknown action: {}", a)}),
    }
}

pub fn main() -> i32 {
    log::info!("SUIT Kiosk Host started (PID {})", std::process::id());
    while let Some(msg) = read_message() {
        let action = msg["action"].as_str().unwrap_or("");
        if send_message(&handle_action(action)).is_err() {
            break;
        }
    }
    log::info!("SUIT Kiosk Host exiting (PID {})", std::process::id());
    0
}
