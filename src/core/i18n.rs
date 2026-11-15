//! UI language. Source strings are English; `tr()` looks them up in the German table.
//!
//! The choice is kept in ~/.config/suit/settings.json. English is the default.
use once_cell::sync::Lazy;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::RwLock;

pub const LANGUAGES: [&str; 2] = ["en", "de"];
pub const DEFAULT_LANGUAGE: &str = "en";

static DE: Lazy<HashMap<String, String>> =
    Lazy::new(|| serde_json::from_str(include_str!("../../data/i18n_de.json")).unwrap_or_default());

static LANGUAGE: RwLock<Option<String>> = RwLock::new(None);

pub fn settings_path() -> PathBuf {
    super::util::home().join(".config").join("suit").join("settings.json")
}

fn read_settings() -> serde_json::Map<String, serde_json::Value> {
    match std::fs::read_to_string(settings_path()) {
        Ok(s) => match serde_json::from_str::<serde_json::Value>(&s) {
            Ok(serde_json::Value::Object(m)) => m,
            _ => Default::default(),
        },
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Default::default(),
        Err(e) => {
            log::error!("Failed reading {}: {}", settings_path().display(), e);
            Default::default()
        }
    }
}

pub fn get_language() -> String {
    if let Some(l) = LANGUAGE.read().unwrap().clone() {
        return l;
    }
    let lang = read_settings()
        .get("language")
        .and_then(|v| v.as_str())
        .filter(|l| LANGUAGES.contains(l))
        .unwrap_or(DEFAULT_LANGUAGE)
        .to_string();
    *LANGUAGE.write().unwrap() = Some(lang.clone());
    lang
}

pub fn set_language(lang: &str) -> anyhow::Result<()> {
    if !LANGUAGES.contains(&lang) {
        anyhow::bail!("Unsupported language: {}", lang);
    }
    *LANGUAGE.write().unwrap() = Some(lang.to_string());
    let mut settings = read_settings();
    settings.insert("language".into(), lang.into());
    let p = settings_path();
    if let Err(e) = super::util::write_file(&p, &serde_json::to_string_pretty(&settings).unwrap()) {
        log::error!("Failed saving language to {}: {}", p.display(), e);
    }
    Ok(())
}

/// Translate an English UI string into the active language (unknown strings stay English).
pub fn tr(text: &str) -> String {
    if text.is_empty() || get_language() == "en" {
        return text.to_string();
    }
    DE.get(text).cloned().unwrap_or_else(|| text.to_string())
}

/// `tr()` followed by named `{field}` substitution.
pub fn trf(text: &str, args: &[(&str, &dyn std::fmt::Display)]) -> String {
    super::util::fmt(&tr(text), args)
}
