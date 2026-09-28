//! Dobot desktop shell.
//!
//! The shell owns what only a native process can: transparent always-on-top windows, global hotkeys,
//! the system tray, autostart, native notifications and screen capture. It holds no credentials and no
//! reasoning — every decision still belongs to the backend.

mod backend;
mod capture;
mod hotkeys;
mod look;
mod state;
mod tray;
mod windows;

use capture::{MonitorInfo, Region};
use serde::{Deserialize, Serialize};
use state::DotPosition;
use tauri::{AppHandle, Emitter, Manager, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_notification::NotificationExt;

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct SelectionPayload {
    pub image: String,
    pub region: Region,
    #[serde(default)]
    pub application: String,
    #[serde(default)]
    pub window_title: String,
    /// Why there is no picture, when there is none (a Look that could not see). Empty when there is one.
    #[serde(default)]
    pub error: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct CaptureResult {
    pub image: String,
    pub width: u32,
    pub height: u32,
}

#[tauri::command]
fn list_monitors() -> Result<Vec<MonitorInfo>, String> {
    capture::monitor_list()
}

#[tauri::command]
fn capture_region(region: Region) -> Result<CaptureResult, String> {
    let image = capture::capture_region(&region)?;
    Ok(CaptureResult {
        image,
        width: region.width,
        height: region.height,
    })
}

#[tauri::command]
fn load_dot_position(app: AppHandle) -> Option<DotPosition> {
    state::load(&app).dot_position
}

#[tauri::command]
fn save_dot_position(app: AppHandle, position: DotPosition) {
    state::remember_dot_position(&app, position);
}

#[tauri::command]
fn open_chat(app: AppHandle) {
    windows::open_chat(&app);
}

#[tauri::command]
fn open_panel(app: AppHandle) {
    windows::open_panel(&app);
}

/// The dot click: show the compact chat dialog if hidden, hide it if shown.
#[tauri::command]
fn toggle_panel(app: AppHandle) {
    windows::toggle_panel(&app);
}

#[tauri::command]
fn hide_panel(app: AppHandle) {
    windows::hide(&app, windows::PANEL);
}

#[tauri::command]
fn open_dashboard(app: AppHandle, route: Option<String>) {
    windows::open_dashboard(&app, route);
}

/// Is the always-on floating dot currently enabled (and therefore on screen)?
#[tauri::command]
fn dot_enabled(app: AppHandle) -> bool {
    state::is_dot_enabled(&app)
}

/// Turn the always-on floating dot on or off. Returns the resulting state.
#[tauri::command]
fn set_dot_enabled(app: AppHandle, enabled: bool) -> bool {
    windows::set_dot_enabled(&app, enabled)
}

#[tauri::command]
fn quit_dobot(app: AppHandle) {
    app.exit(0);
}

#[tauri::command]
fn open_overlay(app: AppHandle) -> Result<(), String> {
    windows::open_overlay(&app)
}

#[tauri::command]
fn close_overlay(app: AppHandle) {
    windows::close_overlay(&app);
}

/// The overlay hands its selection back here; the app forwards it to whichever window is waiting.
#[tauri::command]
fn finish_selection(app: AppHandle, payload: SelectionPayload) {
    let _ = app.emit("dobot://selection", payload);
    windows::close_overlay(&app);
    windows::open_panel(&app);
}

#[tauri::command]
fn cancel_selection(app: AppHandle) {
    windows::close_overlay(&app);
}

/// Take a picture of the window the user is in — the Look chord's job, and the composer button's.
#[tauri::command]
async fn capture_active_window(app: AppHandle) -> Result<SelectionPayload, String> {
    // Capturing lists windows and paints a bitmap; neither belongs on the main thread.
    tauri::async_runtime::spawn_blocking(move || look::capture(&app))
        .await
        .map_err(|error| format!("could not capture the active window: {error}"))
}

/// Is the talk chord currently holding the panel in a listening session? The panel asks on mount,
/// because a press can land before its page has finished loading.
#[tauri::command]
fn voice_state() -> bool {
    hotkeys::talk_is_listening()
}

/// The panel has stopped listening on its own — transcribed, cancelled, or the mic never answered.
#[tauri::command]
fn voice_done() {
    hotkeys::talk_done();
}

/// Whether any Dobot window has the user's attention. A task that finishes while they are in
/// another application needs a notification; one that finishes while they are reading us does not.
#[tauri::command]
fn dobot_focused(app: AppHandle) -> bool {
    [windows::CHAT, windows::PANEL, windows::DASHBOARD]
        .into_iter()
        .filter_map(|label| app.get_webview_window(label))
        .any(|window| window.is_focused().unwrap_or(false))
}

#[tauri::command]
fn notify_user(app: AppHandle, title: String, body: String) -> Result<(), String> {
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|error| format!("could not show a notification: {error}"))
}

#[tauri::command]
fn autostart_enabled(app: AppHandle) -> bool {
    app.autolaunch().is_enabled().unwrap_or(false)
}

#[tauri::command]
fn set_autostart(app: AppHandle, enabled: bool) -> Result<bool, String> {
    let manager = app.autolaunch();
    let result = if enabled {
        manager.enable()
    } else {
        manager.disable()
    };
    result.map_err(|error| format!("could not change autostart: {error}"))?;
    Ok(manager.is_enabled().unwrap_or(false))
}

#[tauri::command]
fn app_status(app: AppHandle) -> serde_json::Value {
    let mut hotkeys = serde_json::json!([]);
    if let Ok(registered) = hotkeys_registered(&app) {
        hotkeys = serde_json::json!(registered);
    }
    serde_json::json!({
        "version": app.package_info().version.to_string(),
        "platform": std::env::consts::OS,
        "arch": std::env::consts::ARCH,
        "hotkeys": hotkeys,
        "autostart": app.autolaunch().is_enabled().unwrap_or(false),
    })
}

/// Hotkeys are registered during setup; this re-registers (idempotently) to report the current set.
fn hotkeys_registered(app: &AppHandle) -> Result<Vec<String>, String> {
    let _ = app;
    Ok(hotkeys::shortcuts().into_iter().map(|(name, _)| name.to_string()).collect())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        // Powers the bundled backend sidecar (start on launch, stop on exit) — Rust-side only, so no
        // capability entries are needed for it.
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| hotkeys::handle(app, shortcut, event))
                .build(),
        )
        .manage(backend::BackendProcess::default())
        .invoke_handler(tauri::generate_handler![
            list_monitors,
            capture_region,
            load_dot_position,
            save_dot_position,
            open_chat,
            open_panel,
            toggle_panel,
            hide_panel,
            open_dashboard,
            dot_enabled,
            set_dot_enabled,
            quit_dobot,
            open_overlay,
            close_overlay,
            finish_selection,
            cancel_selection,
            capture_active_window,
            voice_state,
            voice_done,
            dobot_focused,
            notify_user,
            autostart_enabled,
            set_autostart,
            app_status,
        ])
        .setup(|app| {
            let handle = app.handle().clone();

            // If this install bundles the backend (dobot-backend.exe next to Dobot.exe), start it and
            // wait for its port off the main thread so first paint is never blocked. The UI connects
            // lazily and reports "backend starting…" meanwhile; with no sidecar nothing is spawned
            // and the app expects an external backend exactly as before.
            if backend::spawn_if_bundled(&handle) {
                std::thread::spawn(move || {
                    if backend::wait_for_backend(std::time::Duration::from_secs(45)) {
                        println!("dobot: bundled backend is accepting connections");
                    } else {
                        eprintln!("dobot: bundled backend did not open its port in time");
                    }
                });
            }

            // Opens the chat window and reveals the dot only if the user turned always-on back on.
            windows::apply_startup_state(&handle);
            if let Err(error) = tray::build(app) {
                eprintln!("dobot: tray unavailable: {error}");
            }
            match hotkeys::register(&handle) {
                Ok(registered) => println!("dobot: hotkeys ready ({})", registered.join(", ")),
                Err(error) => eprintln!("dobot: {error}"),
            }

            // Remember where the user drags the dot, and hide the panel instead of closing it.
            if let Some(dot) = app.get_webview_window(windows::DOT) {
                let dot_handle = handle.clone();
                dot.on_window_event(move |event| {
                    if let WindowEvent::Moved(position) = event {
                        state::remember_dot_position(
                            &dot_handle,
                            DotPosition {
                                x: position.x,
                                y: position.y,
                                monitor: 0,
                                scale: 0.0,
                            },
                        );
                    }
                });
            }
            for label in [windows::PANEL, windows::DASHBOARD] {
                if let Some(window) = app.get_webview_window(label) {
                    let label_owned = label.to_string();
                    let window_handle = handle.clone();
                    window.on_window_event(move |event| {
                        if let WindowEvent::CloseRequested { api, .. } = event {
                            api.prevent_close();
                            windows::hide(&window_handle, &label_owned);
                        }
                    });
                }
            }

            // Closing the chat window behaves like any other app: quit — unless the dot is on,
            // in which case the assistant should keep running in the background.
            if let Some(chat) = app.get_webview_window(windows::CHAT) {
                let chat_handle = handle.clone();
                chat.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        if state::is_dot_enabled(&chat_handle) {
                            api.prevent_close();
                            windows::hide(&chat_handle, windows::CHAT);
                        } else {
                            chat_handle.exit(0);
                        }
                    }
                });
            }

            println!("dobot: desktop shell ready");
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running the Dobot desktop shell")
        .run(|app, event| {
            // Stop the bundled backend on every exit path (window close, tray quit, app.exit).
            backend::handle_exit_event(app, &event);
        });
}
