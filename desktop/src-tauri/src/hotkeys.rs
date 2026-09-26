//! Global hotkeys. These are the escape hatches: reach the assistant from anywhere, select a screen
//! region without touching the dot, and stop Dobot with a single chord.

use tauri::{AppHandle, Emitter};
use tauri_plugin_global_shortcut::{
    Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutEvent, ShortcutState,
};

/// Ctrl+Shift+Space — open the panel
/// Ctrl+Shift+S     — select a screen region
/// Ctrl+Alt+D       — open the dashboard
/// Ctrl+Shift+Esc   — kill switch (the specification's emergency shortcut)
pub fn shortcuts() -> Vec<(&'static str, Shortcut)> {
    vec![
        (
            "panel",
            Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::Space),
        ),
        (
            "capture",
            Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyS),
        ),
        (
            "dashboard",
            Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyD),
        ),
        (
            "kill",
            Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::Escape),
        ),
    ]
}

pub fn register(app: &AppHandle) -> Result<Vec<String>, String> {
    let mut registered = Vec::new();
    for (action, shortcut) in shortcuts() {
        match app.global_shortcut().register(shortcut) {
            Ok(()) => registered.push(action.to_string()),
            Err(error) => {
                // A chord can already be owned by another application; report it rather than failing
                // startup, because losing one hotkey should not take Dobot down.
                eprintln!("dobot: could not register the {action} hotkey: {error}");
            }
        }
    }
    if registered.is_empty() {
        return Err("no global hotkeys could be registered".into());
    }
    Ok(registered)
}

/// Called by the plugin handler for every registered chord.
pub fn handle(app: &AppHandle, shortcut: &Shortcut, event: ShortcutEvent) {
    if event.state != ShortcutState::Pressed {
        return;
    }
    let action = shortcuts()
        .into_iter()
        .find(|(_, candidate)| candidate == shortcut)
        .map(|(name, _)| name);

    if let Some(action) = action {
        let _ = app.emit("dobot://hotkey", action);
        // A few actions are handled natively so they work even if no webview is focused.
        match action {
            "panel" => crate::windows::open_panel(app),
            "dashboard" => crate::windows::open_dashboard(app, None),
            _ => {}
        }
    }
}
