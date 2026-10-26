//! The supervised backend sidecar.
//!
//! Release builds may ship the FastAPI backend as `dobot-backend.exe` next to `Dobot.exe`
//! (Tauri `externalBin`). This module owns its lifecycle, adopts compatible external processes,
//! reports startup failures and never kills a process it did not start.

use std::io::{Read, Write};
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
    health_ready()
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
    while Instant::now() < deadline {
        if health_ready() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

/// A listening port is not readiness: Uvicorn may have bound before the service graph is built.
/// Keep this tiny probe dependency-free so the supervisor remains usable before the webview starts.
fn health_ready() -> bool {
    let mut stream = match TcpStream::connect(format!("{HOST}:{PORT}")) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(750)));
    let _ = stream.write_all(
        format!("GET /health HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nConnection: close\r\n\r\n").as_bytes(),
    );
    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }
    response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200")
}

fn port_released() -> bool {
    TcpStream::connect(format!("{HOST}:{PORT}")).is_err()
}

/// Whether this shell owns the bundled backend process.
pub fn managed(app: &AppHandle) -> bool {
    match app.state::<BackendProcess>().0.lock() {
        Ok(guard) => guard.is_some(),
        Err(poisoned) => poisoned.into_inner().is_some(),
    }
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

/// Prepare an update without ever requiring the user to find a process manually.
/// An adopted/external backend is deliberately left alone.
pub fn prepare_update(app: &AppHandle) -> serde_json::Value {
    let was_managed = managed(app);
    if was_managed {
        shutdown(app);
    }
    let released = if !was_managed {
        !health_ready()
    } else {
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if port_released() {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        port_released()
    };
    serde_json::json!({
        "managed": was_managed,
        "stopped": was_managed,
        "released": released,
        "external_backend": !was_managed && health_ready(),
    })
}

/// Runtime status exposed to the desktop settings/Doctor surfaces.
pub fn status(app: &AppHandle) -> serde_json::Value {
    let managed = match app.state::<BackendProcess>().0.lock() {
        Ok(guard) => guard.is_some(),
        Err(poisoned) => poisoned.into_inner().is_some(),
    };
    serde_json::json!({
        "managed": managed,
        "adopted_or_external": !managed && health_ready(),
        "ready": health_ready(),
        "host": HOST,
        "port": PORT,
    })
}

/// Restart only a sidecar Dobot owns. An adopted/external backend is never killed.
pub fn restart_if_owned(app: &AppHandle) -> bool {
    let owned = match app.state::<BackendProcess>().0.lock() {
        Ok(guard) => guard.is_some(),
        Err(poisoned) => poisoned.into_inner().is_some(),
    };
    if !owned {
        return false;
    }
    shutdown(app);
    spawn_if_bundled(app)
}

/// Exit hook: runs for every exit path (window close, tray quit, `app.exit(0)`).
pub fn handle_exit_event(app: &AppHandle, event: &RunEvent) {
    if let RunEvent::Exit = event {
        shutdown(app);
    }
}
