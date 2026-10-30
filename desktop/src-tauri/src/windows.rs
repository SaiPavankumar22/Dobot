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
use tauri::{
    AppHandle, Emitter, Manager, PhysicalPosition, PhysicalSize, WebviewUrl, WebviewWindowBuilder,
};

pub const CHAT: &str = "chat";
pub const DOT: &str = "dot";
pub const MENU: &str = "menu";
pub const PANEL: &str = "panel";
pub const DASHBOARD: &str = "dashboard";
pub const OVERLAY: &str = "overlay";

/// The dot window is 72px with the 44px circle centred in it: the circle's centre sits at +36.
pub const DOT_HALF: i32 = 36;
/// The radial menu window is a square whose centre must land on the dot's circle.
pub const MENU_SIZE: i32 = 310;
pub const MENU_HALF: i32 = MENU_SIZE / 2;

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
    if label == PANEL {
        app.state::<crate::Pair>().remember(false, (x, y));
    }
    let _ = window.set_position(tauri::PhysicalPosition::new(x, y));
}

/// Show the compact panel with the dot pinned to its **top-right corner**: the circle starts 14px
/// inside the dot's 72px window, so the panel's right edge lands on dot_x + 14 (the icon tangent
/// to the corner, never over the header's buttons) and +14 down lines the 44px circle up with the
/// 44px header. From here the pair is kept together by `Pair` (lib.rs): whichever of the two the
/// user drags, the other keeps this exact relationship, so they can never drift apart.
/// Is this dot position entirely outside the virtual desktop? The dot is 72px square, so anything
/// past these edges is invisible and ungrabbable — the state that reads as "the icon disappeared".
fn off_desktop(position: tauri::PhysicalPosition<i32>) -> bool {
    let bounds = capture::virtual_bounds().unwrap_or((0, 0, 1920, 1080));
    position.x + 72 <= bounds.0
        || position.y + 72 <= bounds.1
        || position.x >= bounds.0 + bounds.2 as i32
        || position.y >= bounds.1 + bounds.3 as i32
}

pub fn open_panel(app: &AppHandle) {
    // The menu and the dialog are never up at the same time — the menu is the hub, the dialog
    // what you pick from it (or the double click's shortcut to it).
    hide(app, MENU);
    if let Some(panel) = app.get_webview_window(PANEL) {
        let dot_enabled = state::is_dot_enabled(app);
        if let Some(dot) = app.get_webview_window(DOT) {
            // Self-heal: if the icon is somewhere it cannot be seen — a parked-window move from an
            // earlier build, a monitor that has since been unplugged, a drag past the edge — raise
            // it back onto the desktop instead of anchoring a new window to an invisible dot.
            if dot.is_visible().unwrap_or(false) && dot.outer_position().map(|p| off_desktop(p)).unwrap_or(false) {
                restore_dot_position(app);
            }
        }
        if let Some(dot) = app.get_webview_window(DOT) {
            if let (Ok(dot_pos), Ok(size)) = (dot.outer_position(), panel.outer_size()) {
                let bounds = capture::virtual_bounds().unwrap_or((0, 0, 1920, 1080));
                let mut x = dot_pos.x + 14 - size.width as i32;
                let mut y = dot_pos.y + 14;
                let max_x = bounds.0 + bounds.2 as i32 - size.width as i32 - 8;
                let max_y = bounds.1 + bounds.3 as i32 - size.height as i32 - 8;
                x = x.clamp(bounds.0 + 8, max_x.max(bounds.0 + 8));
                y = y.clamp(bounds.1 + 8, max_y.max(bounds.1 + 8));
                app.state::<crate::Pair>()
                    .remember(false, (x, y));
                let _ = panel.set_position(tauri::PhysicalPosition::new(x, y));
            }
        }
        // With no dot on screen, a panel floating at the old dot position would be disorienting.
        if !dot_enabled {
            anchor_bottom_right(app, PANEL);
        }
        show(app, PANEL);
        // Fix the pair's relationship at the positions they actually landed on (the clamp above
        // may have moved the panel) — later drags preserve *this*, never accumulated deltas.
        crate::anchor_pair(app);
    }
}

/// The dot's radial menu: a transparent window centred on the icon, so the dot itself is the hub
/// the three options spring out of. Opening it retires whatever quick surface is showing — the menu
/// replaces it, and picking Chat (or the dialog's own double click) brings that surface back.
pub fn open_menu(app: &AppHandle) {
    let Some(menu) = app.get_webview_window(MENU) else { return };
    if menu.is_visible().unwrap_or(false) {
        return;
    }
    if let Some(dot) = app.get_webview_window(DOT) {
        if let Ok(pos) = dot.outer_position() {
            let _ = menu.set_position(tauri::PhysicalPosition::new(
                pos.x + DOT_HALF - MENU_HALF,
                pos.y + DOT_HALF - MENU_HALF,
            ));
        }
    }
    hide(app, PANEL);
    let _ = menu.show();
    let _ = menu.set_focus();
}

pub fn close_menu(app: &AppHandle) {
    hide(app, MENU);
}

/// The dot is a toggle: double-click it and the compact chat dialog appears at its top-right
/// corner; double-click again and it disappears. Kept separate from `open_panel`, which always
/// shows (the selection flow relies on it).
pub fn toggle_panel(app: &AppHandle) {
    // Picking Chat from the menu lands here too, and the menu must not outlive that choice.
    hide(app, MENU);
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
    // No dot on screen means no hub for the menu either.
    hide(app, MENU);
    state::set_dot_enabled(app, enabled);
    if let Some(dot) = app.get_webview_window(DOT) {
        if enabled {
            restore_dot_position(app);
            let _ = dot.show();
            // While the dot was off, the panel could still be dragged on its own — which leaves the
            // stored offset describing where the pair *used* to be. Re-read it from where the two
            // windows actually are now, so the next drag of either carries them together instead of
            // throwing one to a stale corner.
            crate::anchor_pair(app);
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
    app.state::<crate::Pair>().remember(true, (x, y));
    let _ = dot.set_position(tauri::PhysicalPosition::new(x, y));
}

/// Lift our own floating windows out of a screen shot for a moment. The dot and the panel sit on
/// top of everything, and the selection overlay paints its own hint and marquee over the region it
/// is about to grab — a picture of the desktop would otherwise have Dobot in it. Only windows that
/// are actually visible are moved, and the caller puts back exactly those — nothing that was hidden
/// gets shown by a screenshot.
pub fn hide_for_capture(app: &AppHandle) -> Vec<&'static str> {
    let mut hidden = Vec::new();
    for label in [DOT, MENU, PANEL, OVERLAY] {
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
    // Snipper, the Look chord's button, the hotkey: whoever wants a region, the menu steps aside.
    hide(app, MENU);
    if let Some(existing) = app.get_webview_window(OVERLAY) {
        let _ = existing.show();
        let _ = existing.set_focus();
        return Ok(());
    }
    let (x, y, width, height) = capture::virtual_bounds()?;
    let window = WebviewWindowBuilder::new(app, OVERLAY, WebviewUrl::App("index.html#/overlay".into()))
        .title("Select a region")
        .transparent(true)
        .decorations(false)
        .always_on_top(true)
        .skip_taskbar(true)
        .shadow(false)
        .focused(true)
        .build()
        .map_err(|error| format!("could not open the selection overlay: {error}"))?;
    // `virtual_bounds` is in physical pixels (it comes from xcap), but the builder's position and
    // size take *logical* pixels. On a display scaled above 100% handing them over directly makes
    // the overlay larger than the screen — its Cancel button and hint end up off the display, and
    // every drag coordinate is measured against the wrong box. Place and size it physically
    // instead, so the overlay covers the virtual desktop exactly once, CSS pixel for physical
    // pixel after it.
    let _ = window.set_position(PhysicalPosition::new(x, y));
    let _ = window.set_size(PhysicalSize::new(width, height));
    Ok(())
}

pub fn close_overlay(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(OVERLAY) {
        let _ = window.close();
    }
}
