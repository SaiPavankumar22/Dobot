//! System tray: the way back to Dobot when the dot is hidden or the panel is closed.

use crate::windows;
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    App, AppHandle, Emitter, Wry,
};

// Desktop only: the shell always runs on the Wry runtime, so no runtime generic is needed (and the
// `Manager` bound for window-icon access only holds for the concrete runtime).
pub fn build(app: &App<Wry>) -> tauri::Result<()> {
    let open_chat = MenuItem::with_id(app, "open_chat", "Open Dobot", true, None::<&str>)?;
    let quick_panel = MenuItem::with_id(app, "open_panel", "Quick ask panel", true, None::<&str>)?;
    let dashboard = MenuItem::with_id(app, "dashboard", "Dashboard", true, None::<&str>)?;
    let select = MenuItem::with_id(app, "select_screen", "Select screen region", true, None::<&str>)?;
    let toggle_dot = MenuItem::with_id(app, "toggle_dot", "Always-on dot", true, None::<&str>)?;
    let stop = MenuItem::with_id(app, "stop", "STOP Dobot", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Dobot", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;

    let menu = Menu::with_items(
        app,
        &[
            &open_chat,
            &quick_panel,
            &dashboard,
            &select,
            &toggle_dot,
            &separator,
            &stop,
            &separator,
            &quit,
        ],
    )?;

    let mut builder = TrayIconBuilder::with_id("dobot-tray")
        .tooltip("Dobot — your AI operating layer")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app: &AppHandle, event| match event.id().as_ref() {
            "open_chat" => windows::open_chat(app),
            "open_panel" => windows::open_panel(app),
            "dashboard" => windows::open_dashboard(app, None),
            "select_screen" => {
                windows::open_panel(app);
                if let Err(error) = windows::open_overlay(app) {
                    eprintln!("dobot: {error}");
                }
            }
            "toggle_dot" => windows::toggle_dot(app),
            "stop" => {
                let _ = app.emit("dobot://hotkey", "kill");
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                windows::open_chat(tray.app_handle());
            }
        });

    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }

    builder.build(app)?;
    Ok(())
}
