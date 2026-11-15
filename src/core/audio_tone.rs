//! Audio guidance tone for camera focus adjustment (radar ping mode).
//!
//! Frequency scales gently in the mellow tenor range (300–360 Hz) with an exponential decay envelope,
//! accelerating in tempo as focus sharpens, and plays a harmonic chord (330 Hz + 495 Hz) at peak focus
//! (>= 95%). Raw S16LE mono PCM is piped into the first available player (pw-cat, pacat, aplay).
use std::io::Write;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const SAMPLE_RATE: u32 = 44100;

fn synth(duration: f64, f: impl Fn(f64) -> f64) -> Vec<u8> {
    let n = (duration * SAMPLE_RATE as f64) as usize;
    let mut out = Vec::with_capacity(n * 2);
    for i in 0..n {
        let t = i as f64 / SAMPLE_RATE as f64;
        let v = (f(t) * 32767.0).clamp(-32767.0, 32767.0) as i16;
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

pub fn synth_ping(freq: f64) -> Vec<u8> {
    synth(0.12, |t| (2.0 * std::f64::consts::PI * freq * t).sin() * 0.22 * (-t / 0.03).exp())
}

pub fn synth_chord() -> Vec<u8> {
    use std::f64::consts::PI;
    synth(1.2, |t| ((2.0 * PI * 330.0 * t).sin() + 0.6 * (2.0 * PI * 495.0 * t).sin()) * 0.20 * (-t / 0.35).exp())
}

/// Convert a 0–100 sharpness score into pulse interval (0.80 s down to 0.12 s).
pub fn score_to_interval(score: f64) -> f64 {
    let n = (score.clamp(40.0, 95.0) - 40.0) / 55.0;
    ((0.80 - n * 0.68) * 1000.0).round() / 1000.0
}

/// Convert a 0–100 sharpness score into radar ping frequency (300–360 Hz).
pub fn score_to_freq(score: f64) -> f64 {
    let n = (score.clamp(40.0, 95.0) - 40.0) / 55.0;
    ((300.0 + n * 60.0) * 10.0).round() / 10.0
}

struct Shared {
    muted: bool,
    score: f64,
    player: Option<Child>,
}

pub struct AudioToneService {
    shared: Arc<Mutex<Shared>>,
    running: Arc<AtomicBool>,
    pings: Arc<Vec<Vec<u8>>>,
    chord: Arc<Vec<u8>>,
}

fn spawn_player() -> Option<Child> {
    let rate = SAMPLE_RATE.to_string();
    let cands: Vec<Vec<&str>> = vec![
        vec!["pw-cat", "--playback", "--format", "s16", "--rate", &rate, "--channels", "1", "-"],
        vec!["pacat", "--raw", "--format=s16le", "--channels=1", "--latency-msec=40"],
        vec!["aplay", "-q", "-f", "S16_LE", "-r", &rate, "-c", "1"],
    ];
    for c in cands {
        let mut cmd = Command::new(c[0]);
        cmd.args(&c[1..]).stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null());
        if c[0] == "pacat" {
            cmd.arg(format!("--rate={}", SAMPLE_RATE));
        }
        if let Ok(ch) = cmd.spawn() {
            log::info!("Audio radar ping guidance started using {}", c[0]);
            return Some(ch);
        }
    }
    None
}

impl AudioToneService {
    pub fn new(muted: bool) -> Self {
        AudioToneService {
            shared: Arc::new(Mutex::new(Shared { muted, score: 0.0, player: None })),
            running: Arc::new(AtomicBool::new(false)),
            pings: Arc::new((0..11).map(|i| synth_ping(300.0 + i as f64 * 6.0)).collect()),
            chord: Arc::new(synth_chord()),
        }
    }

    /// Start the audio pipeline and radar pulse loop.
    pub fn start(&self) -> bool {
        if self.running.load(Ordering::SeqCst) {
            return true;
        }
        let Some(p) = spawn_player() else {
            log::warn!("Could not initialize any audio sink for radar tone guidance");
            return false;
        };
        self.shared.lock().unwrap().player = Some(p);
        self.running.store(true, Ordering::SeqCst);
        let (sh, run, pings, chord) = (self.shared.clone(), self.running.clone(), self.pings.clone(), self.chord.clone());
        std::thread::Builder::new()
            .name("audio_radar_pulse".into())
            .spawn(move || pulse_loop(sh, run, pings, chord))
            .ok();
        true
    }

    pub fn set_score(&self, score: f64) {
        self.shared.lock().unwrap().score = score;
    }
    pub fn set_muted(&self, muted: bool) {
        self.shared.lock().unwrap().muted = muted;
    }
    pub fn is_muted(&self) -> bool {
        self.shared.lock().unwrap().muted
    }
    pub fn is_running(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }
    /// Kept for API compatibility; the sweet spot plays the harmonic chord.
    pub fn play_lock_chime(&self) {}

    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
        if let Some(mut p) = self.shared.lock().unwrap().player.take() {
            let _ = p.kill();
            let _ = p.wait();
        }
    }
}

impl Drop for AudioToneService {
    fn drop(&mut self) {
        self.stop();
    }
}

fn push(sh: &Arc<Mutex<Shared>>, data: &[u8]) {
    let mut s = sh.lock().unwrap();
    if s.muted {
        return;
    }
    if let Some(p) = s.player.as_mut() {
        if let Some(stdin) = p.stdin.as_mut() {
            let _ = stdin.write_all(data);
            let _ = stdin.flush();
        }
    }
}

fn pulse_loop(sh: Arc<Mutex<Shared>>, run: Arc<AtomicBool>, pings: Arc<Vec<Vec<u8>>>, chord: Arc<Vec<u8>>) {
    let snap = |sh: &Arc<Mutex<Shared>>| {
        let s = sh.lock().unwrap();
        (s.muted, s.score)
    };
    while run.load(Ordering::SeqCst) {
        let (muted, score) = snap(&sh);
        if muted || score < 40.0 {
            std::thread::sleep(Duration::from_millis(50));
            continue;
        }
        if score >= 95.0 {
            // Sweet spot: harmonious lock chord
            push(&sh, &chord);
            let end = Instant::now() + Duration::from_millis(1200);
            while Instant::now() < end && run.load(Ordering::SeqCst) {
                let (m, s) = snap(&sh);
                if m || s < 95.0 {
                    break;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            continue;
        }
        let interval = score_to_interval(score);
        let norm = ((score - 40.0) / 55.0).clamp(0.0, 1.0);
        let bucket = ((norm * 10.0).round() as usize).min(10);
        push(&sh, &pings[bucket]);
        let end = Instant::now() + Duration::from_secs_f64(interval);
        while Instant::now() < end && run.load(Ordering::SeqCst) {
            let (m, s) = snap(&sh);
            if m || s >= 95.0 || s < 40.0 || (s - score).abs() > 6.0 {
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}
