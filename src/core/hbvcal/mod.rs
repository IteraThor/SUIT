//! Camera Optimizer for HBV OV2710 USB cameras (Realtek RTS5842 bridge, USB ID 0bdc:2710).
//!
//! Uses the cameras' hidden runtime controls: the MJPEG quality byte, sensor analog gain and the standard UVC
//! controls, calibrated per camera against the board. Nothing is flashed: every change lives in camera RAM and
//! is gone after a power cycle; the keeper service (daemon.rs, installed by service.rs) re-applies it.
pub mod autocal;
pub mod autodarts;
pub mod board;
pub mod capture;
pub mod daemon;
pub mod live;
pub mod multistream;
pub mod oneclick;
pub mod profiles;
pub mod service;
pub mod settings;
pub mod v4l2;
pub mod xu;
