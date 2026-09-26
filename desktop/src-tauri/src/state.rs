//! Shell state that must survive restarts.
//!
//! Two things persist: where the dot sits on screen, and whether the user has the always-on floating
//! dot enabled at all. With the dot disabled Dobot behaves like an ordinary chat application; with it
//! enabled the dot stays above every window.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;
use tauri::{AppHandle, Manager};

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct DotPosition {
    pub x: i32,
    pub y: i32,
    #[serde(default)]
    pub monitor: usize,
    /// Which monitor's coordinate space the position belongs to (best effort).
    #[serde(default)]
    pub scale: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DobotState {
    #[serde(default)]
    pub dot_position: Option<DotPosition>,
    /// Always-on floating dot. Off by default: a fresh install opens as a normal chat application.
    #[serde(default)]
    pub dot_enabled: bool,
}

impl Default for DobotState {
    fn default() -> Self {
        Self {
            dot_position: None,
            dot_enabled: false,
        }
    }
}

fn state_path(app: &AppHandle) -> PathBuf {
    app.path()
        .app_config_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
        .join("dobot-state.json")
}

pub fn load(app: &AppHandle) -> DobotState {
    let path = state_path(app);
    match fs::read_to_string(&path) {
        Ok(raw) => serde_json::from_str(&raw).unwrap_or_default(),
        Err(_) => DobotState::default(),
    }
}

pub fn save(app: &AppHandle, state: &DobotState) -> Result<(), String> {
    let path = state_path(app);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| format!("could not create config dir: {error}"))?;
    }
    let payload = serde_json::to_string_pretty(state)
        .map_err(|error| format!("could not serialise state: {error}"))?;
    fs::write(&path, payload).map_err(|error| format!("could not write state: {error}"))
}

pub fn remember_dot_position(app: &AppHandle, position: DotPosition) {
    let mut state = load(app);
    state.dot_position = Some(position);
    let _ = save(app, &state);
}

pub fn is_dot_enabled(app: &AppHandle) -> bool {
    load(app).dot_enabled
}

pub fn set_dot_enabled(app: &AppHandle, enabled: bool) {
    let mut state = load(app);
    state.dot_enabled = enabled;
    let _ = save(app, &state);
}
