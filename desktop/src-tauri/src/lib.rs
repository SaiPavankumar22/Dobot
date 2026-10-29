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

/// The dot and the compact panel are one surface — the icon rides at the panel's top-right corner —
/// so the shell keeps them glued together: whichever of the two the user drags carries the other.
///
/// The relationship is stored as an *offset* (panel − dot in physical pixels), never as a stream of
/// move deltas: a dropped or duplicated Moved event therefore cannot make the pair drift apart —
/// every correction is an absolute recomputation that converges on the right place. And every
/// position the shell sets for itself is remembered here, so its own repositioning is never
/// mistaken for the user dragging something.
#[derive(Default)]
pub struct Pair {
    inner: std::sync::Mutex<PairInner>,
}

#[derive(Default)]
struct PairInner {
    /// Panel position minus dot position; `None` while the two are not paired (no dot on screen).
    offset: Option<(i32, i32)>,
    /// The most recent positions the shell set for each window itself — a Moved that lands exactly
    /// on one of them is the echo of that work, not a drag.
    set_dot: Vec<(i32, i32)>,
    set_panel: Vec<(i32, i32)>,
}

/// How many self-set positions are remembered per window. One dragged frame already produces more
/// than this many, so an echo is matched with room to spare.
const ECHOES: usize = 32;

impl Pair {
    fn inner(&self) -> std::sync::MutexGuard<'_, PairInner> {
        self.inner.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// The dialog has been (re)anchored to the dot — fix their relationship at these positions.
    pub fn anchor(&self, dot: (i32, i32), panel: (i32, i32)) {
        let mut inner = self.inner();
        inner.offset = Some((panel.0 - dot.0, panel.1 - dot.1));
    }

    pub fn offset(&self) -> Option<(i32, i32)> {
        self.inner().offset
    }

    /// Note a position the shell is about to set for one of the pair, so its Moved is recognisable.
    pub fn remember(&self, dot: bool, pos: (i32, i32)) {
        let mut inner = self.inner();
        let seen = if dot { &mut inner.set_dot } else { &mut inner.set_panel };
        seen.push(pos);
        if seen.len() > ECHOES {
            seen.remove(0);
        }
    }

    /// Is this Moved the echo of a position we set ourselves rather than a user drag?
    pub fn is_echo(&self, dot: bool, pos: (i32, i32)) -> bool {
        let inner = self.inner();
        let seen = if dot { &inner.set_dot } else { &inner.set_panel };
        seen.contains(&pos)
    }
}

/// Record the panel's position relative to the dot, so every later move of either preserves it.
pub fn anchor_pair(app: &AppHandle) {
    let (Some(dot), Some(panel)) = (
        app.get_webview_window(windows::DOT),
        app.get_webview_window(windows::PANEL),
    ) else {
        return;
    };
    if let (Ok(dot_pos), Ok(panel_pos)) = (dot.outer_position(), panel.outer_position()) {
        app.state::<Pair>()
            .anchor((dot_pos.x, dot_pos.y), (panel_pos.x, panel_pos.y));
    }
}

/// Windows parks a window it has just minimised at (-32000, -32000) and reports that as a move.
/// It is never a real position — no monitor can be that far out — so both listeners below treat it
/// as "no drag happened at all". Without this, minimising the dialog (the chat surface and the voice
/// call are the same window) carried the always-on icon off the desktop with it: the panel's Moved
/// handler subtracted the pair offset from -32000 and put the dot there, and the icon vanished.
fn is_parked(position: &tauri::PhysicalPosition<i32>) -> bool {
    position.x <= -10_000 || position.y <= -10_000
}

/// Keep a computed position inside the virtual desktop (the dot is 72px square).
fn clamp_to_desktop(position: (i32, i32)) -> (i32, i32) {
    let bounds = capture::virtual_bounds().unwrap_or((0, 0, 1920, 1080));
    let max_x = bounds.0 + bounds.2 as i32 - 72;
    let max_y = bounds.1 + bounds.3 as i32 - 72;
    (
        position.0.clamp(bounds.0, max_x.max(bounds.0)),
        position.1.clamp(bounds.1, max_y.max(bounds.1)),
    )
}

/// Lift our own floating windows out of a screen shot for the duration of one capture — and put
/// them back **whatever happens**. `Drop` is the point: the restore used to be a plain call after
/// the capture, so a panic inside the capture thread skipped it and the dot stayed hidden for the
/// rest of the session, which reads as "the icon disappeared".
pub(crate) struct RestoreWindows<'a> {
    app: &'a AppHandle,
    hidden: Vec<&'static str>,
}

impl Drop for RestoreWindows<'_> {
    fn drop(&mut self) {
        if !self.hidden.is_empty() {
            windows::restore_after_capture(self.app, std::mem::take(&mut self.hidden));
        }
    }
}

#[tauri::command]
fn list_monitors() -> Result<Vec<MonitorInfo>, String> {
    capture::monitor_list()
}

#[tauri::command]
async fn capture_region(app: AppHandle, region: Region) -> Result<CaptureResult, String> {
    // The marquee arrives as CSS pixels measured from the overlay's own corner. Read the overlay's
    // real physical origin and scale factor *before* it is lifted out of the shot, then hand the
    // capture layer physical virtual-desktop coordinates — at 125% scaling a selection drawn on
    // the right-hand side of the screen is 25% away from where it lands otherwise.
    let region = {
        let (origin, scale) = app
            .get_webview_window(windows::OVERLAY)
            .and_then(|window| {
                let position = window.outer_position().ok()?;
                Some(((position.x, position.y), window.scale_factor().unwrap_or(1.0)))
            })
            .unwrap_or(((0, 0), 1.0));
        capture::to_physical(&region, origin, scale)
    };
    let width = region.width;
    let height = region.height;
    // Our own windows are on top of everything — the dot floats over the desktop, and the snip
    // overlay paints its hint and marquee over the very rectangle being grabbed — so they are
    // lifted out of the shot and put straight back, the same way the Look chord's screen fallback
    // treats them. Capturing, hiding and restoring stay on one blocking thread so the restore can
    // never be skipped, on success or on failure.
    let image = tauri::async_runtime::spawn_blocking(move || {
        let guard = RestoreWindows {
            app: &app,
            hidden: windows::hide_for_capture(&app),
        };
        let result = capture::capture_region(&region);
        drop(guard);
        result
    })
    .await
    .map_err(|error| format!("could not capture the selected region: {error}"))??;
    Ok(CaptureResult {
        image,
        width,
        height,
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

/// The dot's double-click: show the compact chat dialog if hidden, hide it if shown.
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

/// The dot's single click: raise the radial menu over the icon, or take it down if it is already up.
#[tauri::command]
fn toggle_menu(app: AppHandle) {
    let open = app
        .get_webview_window(windows::MENU)
        .map(|menu| menu.is_visible().unwrap_or(false))
        .unwrap_or(false);
    if open {
        windows::close_menu(&app);
    } else {
        windows::open_menu(&app);
    }
}

#[tauri::command]
fn close_menu(app: AppHandle) {
    windows::close_menu(&app);
}

/// The radial menu's Voice option: raise the compact dialog as the call surface. No window is
/// created or navigated — the panel hears this event and *becomes* the phone, so the call still
/// opens on the dot's top-right corner like every other quick surface.
#[tauri::command]
fn start_voice_call(app: AppHandle) {
    windows::open_panel(&app);
    let _ = app.emit("dobot://start-call", ());
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
fn backend_status(app: AppHandle) -> serde_json::Value {
    backend::status(&app)
}

#[tauri::command]
fn restart_backend(app: AppHandle) -> bool {
    backend::restart_if_owned(&app)
}

#[tauri::command]
fn prepare_update(app: AppHandle) -> serde_json::Value {
    backend::prepare_update(&app)
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
        // In-app updates (frontend: services/updater.ts + components/UpdateBanner.tsx):
        // check GitHub Releases, download+install, relaunch. process:allow-restart is in
        // the default capability so the webview may ask Rust to restart after installing.
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| hotkeys::handle(app, shortcut, event))
                .build(),
        )
        .manage(backend::BackendProcess::default())
        .manage(Pair::default())
        .invoke_handler(tauri::generate_handler![
            list_monitors,
            capture_region,
            load_dot_position,
            save_dot_position,
            open_chat,
            open_panel,
            toggle_panel,
            toggle_menu,
            close_menu,
            start_voice_call,
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
            backend_status,
            restart_backend,
            prepare_update,
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

            // Remember where the user drags the dot, keep the open dialog pinned to its corner —
            // and tell the dot window when it has *really* been dragged: on Windows a press enters
            // the OS drag loop (so the dot can be moved from anywhere) and swallows its own
            // mouse-up, which makes this event the only trustworthy drag signal it has. It uses it
            // to tell a click from a drag, and to retire a menu the icon is being carried away from.
            if let Some(dot) = app.get_webview_window(windows::DOT) {
                let dot_handle = handle.clone();
                dot.on_window_event(move |event| {
                    if let WindowEvent::Moved(position) = event {
                        // A minimised window's "move" to the parked coordinates is not a drag: the
                        // icon must neither follow it nor persist it as where the user left it.
                        if is_parked(position) {
                            return;
                        }
                        let pos = (position.x, position.y);
                        state::remember_dot_position(
                            &dot_handle,
                            DotPosition {
                                x: pos.0,
                                y: pos.1,
                                monitor: 0,
                                scale: 0.0,
                            },
                        );
                        let pair = dot_handle.state::<Pair>();
                        // A move the shell made itself (the panel carrying the dot to its new
                        // corner) is not a drag: no event, no follow, nothing to dismiss.
                        if pair.is_echo(true, pos) {
                            return;
                        }
                        let _ = dot_handle.emit("dobot://dot-moved", pos);
                        if let (Some(offset), Some(panel)) =
                            (pair.offset(), dot_handle.get_webview_window(windows::PANEL))
                        {
                            if panel.is_visible().unwrap_or(false) {
                                let target = (pos.0 + offset.0, pos.1 + offset.1);
                                pair.remember(false, target);
                                let _ = panel
                                    .set_position(tauri::PhysicalPosition::new(target.0, target.1));
                            }
                        }
                    }
                });
            }
            // The panel: closing hides it, dragging it carries the dot to the dialog's new
            // top-right corner, and a resize moves that corner — so the pair re-reads its offset.
            if let Some(panel) = app.get_webview_window(windows::PANEL) {
                let panel_handle = handle.clone();
                panel.on_window_event(move |event| match event {
                    WindowEvent::CloseRequested { api, .. } => {
                        api.prevent_close();
                        windows::hide(&panel_handle, windows::PANEL);
                    }
                    WindowEvent::Moved(position) => {
                        // Minimising the dialog reports a move to (-32000, -32000). Following that
                        // with the pair is what threw the always-on icon off the desktop.
                        if is_parked(position) {
                            return;
                        }
                        let pos = (position.x, position.y);
                        let pair = panel_handle.state::<Pair>();
                        if pair.is_echo(false, pos) {
                            return;
                        }
                        // The user dragged the dialog: put the dot back on its top-right corner.
                        if let (Some(offset), Some(dot)) =
                            (pair.offset(), panel_handle.get_webview_window(windows::DOT))
                        {
                            if dot.is_visible().unwrap_or(false) {
                                // Clamped, so dragging the dialog past an edge can never strand the
                                // icon somewhere it cannot be seen or grabbed again.
                                let target = clamp_to_desktop((pos.0 - offset.0, pos.1 - offset.1));
                                pair.remember(true, target);
                                let _ =
                                    dot.set_position(tauri::PhysicalPosition::new(target.0, target.1));
                            }
                        }
                    }
                    WindowEvent::Resized(_) => anchor_pair(&panel_handle),
                    _ => {}
                });
            }
            if let Some(window) = app.get_webview_window(windows::DASHBOARD) {
                let window_handle = handle.clone();
                window.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        windows::hide(&window_handle, windows::DASHBOARD);
                    }
                });
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

#[cfg(test)]
mod dot_geometry_tests {
    use super::*;

    #[test]
    fn a_minimized_windows_move_is_not_a_position() {
        // What Windows reports when the dialog or the voice-call surface is minimised.
        assert!(is_parked(&tauri::PhysicalPosition::new(-32000, -32000)));
        assert!(is_parked(&tauri::PhysicalPosition::new(-32000, 400)));
        // A monitor to the left of the primary is genuinely negative — never mistaken for parked.
        assert!(!is_parked(&tauri::PhysicalPosition::new(-1920, 0)));
        assert!(!is_parked(&tauri::PhysicalPosition::new(100, 200)));
    }

    #[test]
    fn clamping_puts_a_lost_icon_back_on_the_desktop() {
        let bounds = capture::virtual_bounds().expect("a display is attached");
        for lost in [(-32000, -32000), (-100_000, 500), (100_000, 100_000)] {
            let (x, y) = clamp_to_desktop(lost);
            assert!(x >= bounds.0, "x {x} left of desktop");
            assert!(y >= bounds.1, "y {y} above desktop");
            assert!(x <= bounds.0 + bounds.2 as i32 - 72, "x {x} past the right edge");
            assert!(y <= bounds.1 + bounds.3 as i32 - 72, "y {y} past the bottom");
        }
    }
}
