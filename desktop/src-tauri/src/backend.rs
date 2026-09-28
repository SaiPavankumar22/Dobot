//! The *opt-in* bundled backend sidecar.
//!
//! Installers bundle no backend by default: the app talks to a deployed backend (a Hugging Face
//! Space, set in Settings → Backend) or to one the user runs themselves. With
//! `DOBOT_BUNDLE_SIDECAR=1` the installer ships the FastAPI backend as `dobot-backend.exe` next to
//! `Dobot.exe` (Tauri `externalBin`), and this module starts it on launch, waits for the API port to
//! accept connections and stops it when the shell exits. Every step is honest and optional: if the
//! sidecar is not registered, nothing is spawned and the app behaves exactly as before — connect to
//! whatever backend the user runs or points Settings at.

use std::net::TcpStream;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

/// The sidecar's Tauri name; the bundler resolves the platform triple and `.exe` suffix.
const SIDECAR_NAME: &str = "dobot-backend";

/// Connection settings for the managed backend. Keep in step with the frontend default
/// (`DEFAULT_BASE_URL` in `desktop/src/services/api.ts`).
const HOST: &str = "127.0.0.1";
const PORT: u16 = 8756;

#[derive(Default)]
pub struct BackendProcess(Mutex<Option<CommandChild>>);

/// Start `dobot-backend.exe` if this install bundles it.
///
/// Returns immediately with `false` when the sidecar is absent — that is the normal case for a
/// source checkout, where the backend runs from `uv run` or is pointed at a deployed host.
fn take_child(app: &AppHandle) -> Option<CommandChild> {
    match app.state::<BackendProcess>().0.lock() {
        Ok(mut guard) => guard.take(),
        Err(poisoned) => poisoned.into_inner().take(),
    }
}

/// True when something already serves the backend port (the user ran one themselves, or a crashed
/// session left one behind). We adopt it instead of spawning a duplicate — and because it is not
/// ours, we also leave it running on exit.
fn port_already_serving() -> bool {
    TcpStream::connect(format!("{HOST}:{PORT}")).is_ok()
}

pub fn spawn_if_bundled(app: &AppHandle) -> bool {
    // Dev runs (`npm run tauri dev`) never spawn the sidecar: the backend there is the one you start
    // with `uv run uvicorn`, and the staged binaries/ entry is a placeholder, not a real exe.
    if cfg!(debug_assertions) {
        return false;
    }
    if port_already_serving() {
        println!("dobot: a backend is already serving on http://{HOST}:{PORT} — using it");
        return false;
    }
    // Tauri resolves externalBin sidecars itself (installed next to Dobot.exe in production); when
    // no sidecar is registered — the default installer, a source checkout — resolving the name fails
    // and we carry on with whatever external backend the user runs or points Settings at.
    let state = app.state::<BackendProcess>();
    let managed = match state.0.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    if managed.is_some() {
        return true;
    }
    drop(managed);
    drop(state);
    let command = match app.shell().sidecar(SIDECAR_NAME) {
        Ok(command) => command,
        Err(error) => {
            eprintln!("dobot: no bundled backend sidecar ({error}) — expecting an external one");
            return false;
        }
    };
    match command
        .args(["--host", HOST, "--port", &PORT.to_string()])
        .spawn()
    {
        Ok((_, child)) => {
            let state = app.state::<BackendProcess>();
            let mut guarded = match state.0.lock() {
                Ok(guard) => guard,
                Err(poisoned) => poisoned.into_inner(),
            };
            *guarded = Some(child);
            drop(guarded);
            drop(state);
            println!("dobot: bundled backend starting on http://{HOST}:{PORT}");
            true
        }
        Err(error) => {
            eprintln!("dobot: no bundled backend sidecar ({error}) — expecting an external one");
            false
        }
    }
}

/// Wait (up to `timeout`) for the backend to accept TCP connections on its port.
pub fn wait_for_backend(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    let address = format!("{HOST}:{PORT}");
    while Instant::now() < deadline {
        if TcpStream::connect(&address).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

/// Stop the managed backend, if we started one. Never touches a backend the user runs themselves.
pub fn shutdown(app: &AppHandle) {
    if let Some(child) = take_child(app) {
        if let Err(error) = child.kill() {
            eprintln!("dobot: could not stop the bundled backend: {error}");
        } else {
            println!("dobot: bundled backend stopped");
        }
    }
}

/// Exit hook: runs for every exit path (window close, tray quit, `app.exit(0)`).
pub fn handle_exit_event(app: &AppHandle, event: &RunEvent) {
    if let RunEvent::Exit = event {
        shutdown(app);
    }
}
