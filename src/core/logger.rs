//! File + console logging with rotation (5 MB x 3), like core/logger.py.
use log::{Level, LevelFilter, Metadata, Record};
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

const MAX_BYTES: u64 = 5 * 1024 * 1024;
const BACKUP_COUNT: usize = 3;

struct SuitLogger {
    path: PathBuf,
    file: Mutex<Option<File>>,
}

impl SuitLogger {
    fn rotate_if_needed(&self, f: &mut Option<File>) {
        let size = std::fs::metadata(&self.path).map(|m| m.len()).unwrap_or(0);
        if size < MAX_BYTES {
            return;
        }
        *f = None;
        for i in (1..BACKUP_COUNT).rev() {
            let src = self.path.with_extension(format!("log.{}", i));
            let dst = self.path.with_extension(format!("log.{}", i + 1));
            let _ = std::fs::rename(src, dst);
        }
        let _ = std::fs::rename(&self.path, self.path.with_extension("log.1"));
        *f = OpenOptions::new().create(true).append(true).open(&self.path).ok();
    }
}

impl log::Log for SuitLogger {
    fn enabled(&self, _m: &Metadata) -> bool {
        true
    }

    fn log(&self, record: &Record) {
        let ts = chrono::Local::now().format("%Y-%m-%d %H:%M:%S");
        let target = record.target().trim_start_matches("suit::").replace("::", ".");
        let line = format!(
            "{} [{}] [suit.{}:{}]: {}\n",
            ts,
            record.level().as_str().replace("WARN", "WARNING"),
            target,
            record.line().unwrap_or(0),
            record.args()
        );
        if record.level() <= Level::Info {
            // stderr: stdout carries the native messaging protocol in `suit kiosk-host`
            let _ = std::io::stderr().write_all(line.as_bytes());
        }
        if let Ok(mut f) = self.file.lock() {
            self.rotate_if_needed(&mut f);
            if let Some(file) = f.as_mut() {
                let _ = file.write_all(line.as_bytes());
            }
        }
    }

    fn flush(&self) {
        if let Ok(mut f) = self.file.lock() {
            if let Some(file) = f.as_mut() {
                let _ = file.flush();
            }
        }
    }
}

pub fn default_log_file() -> PathBuf {
    super::util::project_dir().join("suit.log")
}

/// Install the logger (call once) and a panic hook that logs crashes.
pub fn setup_logging(log_file: Option<&Path>) {
    let path = log_file.map(|p| p.to_path_buf()).unwrap_or_else(default_log_file);
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let file = OpenOptions::new().create(true).append(true).open(&path).ok();
    let logger = SuitLogger { path, file: Mutex::new(file) };
    if log::set_boxed_logger(Box::new(logger)).is_ok() {
        log::set_max_level(LevelFilter::Debug);
    }
    install_global_exception_handler();
}

pub fn install_global_exception_handler() {
    let default = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        log::error!(target: "crash", "Unhandled panic caught: {}\n{}", info, std::backtrace::Backtrace::force_capture());
        default(info);
    }));
}

/// Console-only logging at INFO for the background subcommands (their output goes to the journal).
pub fn setup_console_logging() {
    struct Console;
    impl log::Log for Console {
        fn enabled(&self, m: &Metadata) -> bool {
            m.level() <= Level::Info
        }
        fn log(&self, r: &Record) {
            if self.enabled(r.metadata()) {
                println!("{} {}", chrono::Local::now().format("%Y-%m-%d %H:%M:%S"), r.args());
            }
        }
        fn flush(&self) {}
    }
    if log::set_boxed_logger(Box::new(Console)).is_ok() {
        log::set_max_level(LevelFilter::Info);
    }
}
