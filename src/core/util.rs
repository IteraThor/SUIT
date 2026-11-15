//! Small process and filesystem helpers shared by the services (the Rust stand-in for `subprocess`).
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Result of a finished command, like `subprocess.CompletedProcess` with `text=True`.
#[derive(Debug, Clone, Default)]
pub struct Output {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
    pub timed_out: bool,
}

impl Output {
    pub fn ok(&self) -> bool {
        self.code == 0 && !self.timed_out
    }
    pub fn combined(&self) -> String {
        format!("{}\n{}", self.stdout, self.stderr)
    }
}

fn spawn_failed(e: std::io::Error) -> Output {
    Output { code: -1, stdout: String::new(), stderr: e.to_string(), timed_out: false }
}

/// Run a command and capture its output. `timeout` kills the child when exceeded.
pub fn run_cmd(mut cmd: Command, timeout: Option<Duration>) -> Output {
    cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return spawn_failed(e),
    };
    let mut out_pipe = child.stdout.take();
    let mut err_pipe = child.stderr.take();
    let out_t = std::thread::spawn(move || {
        let mut s = Vec::new();
        if let Some(p) = out_pipe.as_mut() {
            let _ = p.read_to_end(&mut s);
        }
        String::from_utf8_lossy(&s).into_owned()
    });
    let err_t = std::thread::spawn(move || {
        let mut s = Vec::new();
        if let Some(p) = err_pipe.as_mut() {
            let _ = p.read_to_end(&mut s);
        }
        String::from_utf8_lossy(&s).into_owned()
    });
    let start = Instant::now();
    let mut timed_out = false;
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break Some(st),
            Ok(None) => {
                if let Some(t) = timeout {
                    if start.elapsed() >= t {
                        let _ = child.kill();
                        timed_out = true;
                        break child.wait().ok();
                    }
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(_) => break None,
        }
    };
    let stdout = out_t.join().unwrap_or_default();
    let stderr = err_t.join().unwrap_or_default();
    let code = status.and_then(|s| s.code()).unwrap_or(-1);
    Output { code, stdout, stderr, timed_out }
}

/// `run(&["systemctl", "--user", "status"], Some(10))`
pub fn run(args: &[&str], timeout_s: Option<u64>) -> Output {
    if args.is_empty() {
        return Output { code: -1, ..Default::default() };
    }
    let mut c = Command::new(args[0]);
    c.args(&args[1..]);
    run_cmd(c, timeout_s.map(Duration::from_secs))
}

/// Same as [`run`] with owned arguments.
pub fn run_owned(args: &[String], timeout_s: Option<u64>) -> Output {
    let v: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
    run(&v, timeout_s)
}

/// Run in a working directory.
pub fn run_in(dir: &Path, args: &[&str], timeout_s: Option<u64>) -> Output {
    let mut c = Command::new(args[0]);
    c.args(&args[1..]).current_dir(dir);
    run_cmd(c, timeout_s.map(Duration::from_secs))
}

/// `bash -c <script>` (the stand-in for `shell=True`).
pub fn run_shell(script: &str, timeout_s: Option<u64>) -> Output {
    run(&["bash", "-c", script], timeout_s)
}

/// Run with stdin fed from `input`.
pub fn run_with_input(args: &[&str], input: &str, timeout_s: Option<u64>) -> Output {
    use std::io::Write;
    let mut c = Command::new(args[0]);
    c.args(&args[1..]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = match c.spawn() {
        Ok(c) => c,
        Err(e) => return spawn_failed(e),
    };
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(input.as_bytes());
    }
    let deadline = timeout_s.map(|t| Instant::now() + Duration::from_secs(t));
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) => {
                if deadline.map(|d| Instant::now() >= d).unwrap_or(false) {
                    let _ = child.kill();
                    let o = child.wait_with_output();
                    return o
                        .map(|o| Output {
                            code: -1,
                            stdout: String::from_utf8_lossy(&o.stdout).into(),
                            stderr: String::from_utf8_lossy(&o.stderr).into(),
                            timed_out: true,
                        })
                        .unwrap_or_else(spawn_failed);
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(_) => break,
        }
    }
    match child.wait_with_output() {
        Ok(o) => Output {
            code: o.status.code().unwrap_or(-1),
            stdout: String::from_utf8_lossy(&o.stdout).into(),
            stderr: String::from_utf8_lossy(&o.stderr).into(),
            timed_out: false,
        },
        Err(e) => spawn_failed(e),
    }
}

/// Fire and forget (`subprocess.Popen`).
pub fn spawn(args: &[&str]) -> bool {
    if args.is_empty() {
        return false;
    }
    Command::new(args[0])
        .args(&args[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .is_ok()
}

pub fn which(cmd: &str) -> Option<PathBuf> {
    which::which(cmd).ok()
}

pub fn has_cmd(cmd: &str) -> bool {
    which(cmd).is_some()
}

pub fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/root"))
}

pub fn username() -> String {
    std::env::var("USER")
        .ok()
        .filter(|u| !u.is_empty())
        .or_else(|| {
            let o = run(&["id", "-un"], Some(2));
            Some(o.stdout.trim().to_string()).filter(|s| !s.is_empty())
        })
        .unwrap_or_else(|| "user".into())
}

pub fn hostname() -> String {
    std::fs::read_to_string("/proc/sys/kernel/hostname")
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|_| "localhost".into())
}

pub fn read_trim(p: impl AsRef<Path>) -> Option<String> {
    std::fs::read_to_string(p).ok().map(|s| s.trim().to_string())
}

/// Write a file creating its parents.
pub fn write_file(p: impl AsRef<Path>, content: &str) -> std::io::Result<()> {
    let p = p.as_ref();
    if let Some(parent) = p.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(p, content)
}

/// Write atomically (temp file + rename).
pub fn write_atomic(p: impl AsRef<Path>, content: &[u8]) -> std::io::Result<()> {
    let p = p.as_ref();
    if let Some(parent) = p.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp = p.with_extension(format!(
        "{}.tmp",
        p.extension().and_then(|e| e.to_str()).unwrap_or("")
    ));
    std::fs::write(&tmp, content)?;
    std::fs::rename(&tmp, p)
}

/// Write a root-owned file through `sudo -n tee`.
pub fn sudo_write(path: &str, content: &str) -> bool {
    run_with_input(&["sudo", "-n", "tee", path], content, Some(20)).ok()
}

pub fn sleep_s(s: f64) {
    std::thread::sleep(Duration::from_secs_f64(s.max(0.0)));
}

pub fn now_s() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Shell-quote one word for bash.
pub fn shquote(s: &str) -> String {
    if !s.is_empty() && s.chars().all(|c| c.is_ascii_alphanumeric() || "-_./=:@%+,".contains(c)) {
        return s.to_string();
    }
    format!("'{}'", s.replace('\'', "'\"'\"'"))
}

/// Read a JSON file to a value (None when missing or invalid).
pub fn read_json(p: impl AsRef<Path>) -> Option<serde_json::Value> {
    let s = std::fs::read_to_string(p).ok()?;
    serde_json::from_str(&s).ok()
}

pub fn write_json(p: impl AsRef<Path>, v: &serde_json::Value) -> std::io::Result<()> {
    let s = serde_json::to_string_pretty(v).unwrap_or_else(|_| "{}".into());
    write_atomic(p, s.as_bytes())
}

/// Python-style `str.format` with named fields: `fmt("{a} of {b}", &[("a", "1"), ("b", "2")])`.
/// A Python format spec in the template (`{seconds:.1f}`) is ignored: callers pass the value pre-formatted.
pub fn fmt(template: &str, args: &[(&str, &dyn std::fmt::Display)]) -> String {
    static RE: once_cell::sync::Lazy<regex::Regex> =
        once_cell::sync::Lazy::new(|| regex::Regex::new(r"\{(\w+)(?::[^{}]*)?\}").unwrap());
    RE.replace_all(template, |c: &regex::Captures| match args.iter().find(|(k, _)| *k == &c[1]) {
        Some((_, v)) => v.to_string(),
        None => c[0].to_string(),
    })
    .to_string()
}

/// Directory the app runs from: the project checkout (holds assets/, style_gtk.css ...).
pub fn project_dir() -> PathBuf {
    if let Ok(d) = std::env::var("SUIT_DIR") {
        return PathBuf::from(d);
    }
    // target/release/suit -> project root
    if let Ok(exe) = std::env::current_exe() {
        let mut d = exe.parent().map(|p| p.to_path_buf());
        while let Some(dir) = d {
            if dir.join("assets").is_dir() && dir.join("Cargo.toml").is_file() {
                return dir;
            }
            d = dir.parent().map(|p| p.to_path_buf());
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

/// The binary currently running (for systemd units and autostarts).
pub fn self_exe() -> PathBuf {
    let release = project_dir().join("target").join("release").join("suit");
    if release.is_file() {
        return release;
    }
    std::env::current_exe().unwrap_or(release)
}

/// Parse a gsettings string array ("['a.desktop', 'b.desktop']" or "@as []"); None if it is not one.
pub fn parse_strv(s: &str) -> Option<Vec<String>> {
    let t = s.trim().trim_start_matches("@as").trim();
    if !t.starts_with('[') || !t.ends_with(']') {
        return None;
    }
    let re = regex::Regex::new(r#"'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)""#).unwrap();
    Some(
        re.captures_iter(&t[1..t.len() - 1])
            .map(|c| c.get(1).or_else(|| c.get(2)).map(|m| m.as_str().replace("\\'", "'")).unwrap_or_default())
            .collect(),
    )
}

/// Format a string array the way gsettings accepts it.
pub fn fmt_strv(v: &[String]) -> String {
    format!("[{}]", v.iter().map(|s| format!("'{}'", s.replace('\'', "\\'"))).collect::<Vec<_>>().join(", "))
}

pub fn gsettings_get(schema: &str, key: &str) -> String {
    run(&["gsettings", "get", schema, key], Some(5)).stdout.trim().to_string()
}

pub fn gsettings_set(schema: &str, key: &str, value: &str) -> bool {
    run(&["gsettings", "set", schema, key, value], Some(10)).ok()
}
