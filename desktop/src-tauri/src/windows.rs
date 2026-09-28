//! Window management.
//!
//! Dobot has two personalities and both are windows:
//!
//! * **chat** — an ordinary, decorated application window. This is what opens when you launch Dobot,
//!   and it behaves like any other chatbot: sidebar, message thread, composer.
//! * **dot** — the always-on floating dot. It is hidden on a fresh install and only appears once the
//!   user turns *always on* on. It stays above every window and is the ambient surface.
//!
//! On top of those there is the compact **panel** (a quick-ask surface anchored to the dot), the
//! **dashboard** (tasks, memory, automation, security) and a temporary transparent **overlay** used
//! to select a screen region.

use crate::capture;
use crate::state;
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};

pub const CHAT: &str = "chat";
pub const DOT: &str = "dot";
pub const PANEL: &str = "panel";
pub const DASHBOARD: &str = "dashboard";
pub const OVERLAY: &str = "overlay";

pub fn show(app: &AppHandle, label: &str) {
    if let Some(window) = app.get_webview_window(label) {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

pub fn hide(app: &AppHandle, label: &str) {
    if let Some(window) = app.get_webview_window(label) {
        let _ = window.hide();
    }
}

/// Raise the main chat window — the default surface on launch.
pub fn open_chat(app: &AppHandle) {
    show(app, CHAT);
}

/// Anchor a small window to the bottom-right of the virtual desktop, used when the dot is off.
fn anchor_bottom_right(app: &AppHandle, label: &str) {
    let Some(window) = app.get_webview_window(label) else { return };
    let Ok(size) = window.outer_size() else { return };
    let bounds = capture::virtual_bounds().unwrap_or((0, 0, 1920, 1080));
    let x = bounds.0 + bounds.2 as i32 - size.width as i32 - 24;
    let y = bounds.1 + bounds.3 as i32 - size.height as i32 - 64;
    let _ = window.set_position(tauri::PhysicalPosition::new(x, y));
}

/// Show the compact panel next to the dot, keeping it on screen.
pub fn open_panel(app: &AppHandle) {
    if let Some(panel) = app.get_webview_window(PANEL) {
        let dot_enabled = state::is_dot_enabled(app);
        if let Some(dot) = app.get_webview_window(DOT) {
            if let (Ok(dot_pos), Ok(size)) = (dot.outer_position(), panel.outer_size()) {
                let bounds = capture::virtual_bounds().unwrap_or((0, 0, 1920, 1080));
                let mut x = dot_pos.x + 56;
                let mut y = dot_pos.y - 40;
                let max_x = bounds.0 + bounds.2 as i32 - size.width as i32 - 8;
                let max_y = bounds.1 + bounds.3 as i32 - size.height as i32 - 8;
                x = x.clamp(bounds.0 + 8, max_x.max(bounds.0 + 8));
                y = y.clamp(bounds.1 + 8, max_y.max(bounds.1 + 8));
                let _ = panel.set_position(tauri::PhysicalPosition::new(x, y));
            }
        }
        // With no dot on screen, a panel floating at the old dot position would be disorienting.
        if !dot_enabled {
            anchor_bottom_right(app, PANEL);
        }
        show(app, PANEL);
    }
}

/// The dot is a toggle: click it and the compact chat dialog appears next to it; click again and it
/// disappears. Kept separate from `open_panel`, which always shows (the selection flow relies on it).
pub fn toggle_panel(app: &AppHandle) {
    if let Some(panel) = app.get_webview_window(PANEL) {
        if panel.is_visible().unwrap_or(false) {
            let _ = panel.hide();
            return;
        }
    }
    open_panel(app);
}

pub fn open_dashboard(app: &AppHandle, route: Option<String>) {
    if let Some(window) = app.get_webview_window(DASHBOARD) {
        if let Some(route) = route {
            let _ = window.emit("dobot://route", route);
        }
        show(app, DASHBOARD);
    }
}

/// Turn the always-on floating dot on or off. Returns the resulting state.
pub fn set_dot_enabled(app: &AppHandle, enabled: bool) -> bool {
    state::set_dot_enabled(app, enabled);
    if let Some(dot) = app.get_webview_window(DOT) {
        if enabled {
            restore_dot_position(app);
            let _ = dot.show();
        } else {
            let _ = dot.hide();
        }
    }
    // Tell every window so a toggle in the chat UI and the tray menu stay in sync.
    let _ = app.emit("dobot://dot-enabled", enabled);
    enabled
}

pub fn toggle_dot(app: &AppHandle) {
    let next = !state::is_dot_enabled(app);
    set_dot_enabled(app, next);
}

/// Start every window in the state the user left behind: chat open, dot only if enabled.
pub fn apply_startup_state(app: &AppHandle) {
    let enabled = state::is_dot_enabled(app);
    if let Some(dot) = app.get_webview_window(DOT) {
        if enabled {
            restore_dot_position(app);
            let _ = dot.show();
        } else {
            let _ = dot.hide();
        }
    }
    open_chat(app);
}

/// Place the dot where the user last left it, clamped to a monitor that still exists.
pub fn restore_dot_position(app: &AppHandle) {
    let saved = state::load(app).dot_position;
    let bounds = capture::virtual_bounds().unwrap_or((0, 0, 1920, 1080));
    let Some(dot) = app.get_webview_window(DOT) else { return };
    let (x, y) = match saved {
        Some(position) => (
            position.x.clamp(bounds.0, bounds.0 + bounds.2 as i32 - 72),
            position.y.clamp(bounds.1, bounds.1 + bounds.3 as i32 - 72),
        ),
        None => (
            bounds.0 + bounds.2 as i32 - 132,
            bounds.1 + bounds.3 as i32 - 168,
        ),
    };
    let _ = dot.set_position(tauri::PhysicalPosition::new(x, y));
}

/// Lift our own floating windows out of a screen shot for a moment. The dot and the panel sit on
/// top of everything, so a picture of the desktop would otherwise have Dobot in it. Only windows
/// that are actually visible are moved, and the caller puts back exactly those — nothing that was
/// hidden gets shown by a screenshot.
pub fn hide_for_capture(app: &AppHandle) -> Vec<&'static str> {
    let mut hidden = Vec::new();
    for label in [DOT, PANEL] {
        if let Some(window) = app.get_webview_window(label) {
            if window.is_visible().unwrap_or(false) {
                let _ = window.hide();
                hidden.push(label);
            }
        }
    }
    if !hidden.is_empty() {
        // The compositor needs a beat to rebuild the desktop without them in it.
        std::thread::sleep(std::time::Duration::from_millis(120));
    }
    hidden
}

/// Put back whatever `hide_for_capture` took away, in the same order, right after the picture.
pub fn restore_after_capture(app: &AppHandle, hidden: Vec<&'static str>) {
    for label in hidden {
        if let Some(window) = app.get_webview_window(label) {
            let _ = window.show();
        }
    }
}

/// Create (or raise) the transparent selection overlay covering the whole virtual desktop.
pub fn open_overlay(app: &AppHandle) -> Result<(), String> {
    if let Some(existing) = app.get_webview_window(OVERLAY) {
        let _ = existing.show();
        let _ = existing.set_focus();
        return Ok(());
    }
    let (x, y, width, height) = capture::virtual_bounds()?;
    WebviewWindowBuilder::new(app, OVERLAY, WebviewUrl::App("index.html#/overlay".into()))
        .title("Select a region")
        .position(x as f64, y as f64)
        .inner_size(width as f64, height as f64)
        .transparent(true)
        .decorations(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .shadow(false)
        .focused(true)
        .build()
        .map_err(|error| format!("could not open the selection overlay: {error}"))?;
    Ok(())
}

pub fn close_overlay(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(OVERLAY) {
        let _ = window.close();
    }
}
