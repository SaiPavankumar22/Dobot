//! "Show Dobot the window you are in" — one chord, one picture, ready for the question.
//!
//! This is Perry's *Look* in Dobot's vocabulary: the user presses a chord while looking at
//! something ("what's this error?"), and a picture of exactly that window lands in the composer,
//! where they can check it, swap it, or drop it before anything is sent.
//!
//! Which window: the one in front — that is what they were looking at when they pressed. If the
//! front window is one of ours (they pressed it from inside Dobot), the topmost window that is not
//! ours stands in for it, and if there is nothing at all to point at, the screen behind our own
//! floating windows is captured instead. Nothing runs unless the chord or the button is pressed,
//! and nothing is written to disk: the PNG stays in memory, like every other capture.

use std::process;

use tauri::{AppHandle, Emitter, Manager};
use xcap::Window;

use crate::capture::{self, Region};
use crate::windows;
use crate::SelectionPayload;

/// A window this small is a border or a tooltip, not something to ask about.
const MIN_SIDE: i32 = 64;

/// Take the picture and hand it to every window — each keeps its own store, and the user may be in
/// any of them. The panel is opened behind it so the question has somewhere to go; in the full chat
/// window the composer is already on screen, and raising the panel over it would be noise.
pub fn ask(app: &AppHandle) {
    let payload = capture(app);
    let _ = app.emit("dobot://look", payload);
    let chat_is_front = app
        .get_webview_window(windows::CHAT)
        .and_then(|window| window.is_focused().ok())
        .unwrap_or(false);
    if !chat_is_front {
        windows::open_panel(app);
    }
}

/// The picture itself. An error is reported in the payload rather than as a failure: the user
/// pressed a key expecting feedback, and "I couldn't see that" is feedback.
pub fn capture(app: &AppHandle) -> SelectionPayload {
    picture(app).unwrap_or_else(|error| SelectionPayload {
        error,
        ..SelectionPayload::default()
    })
}

fn picture(app: &AppHandle) -> Result<SelectionPayload, String> {
    let listed = Window::all().map_err(|error| format!("could not list windows: {error}"))?;
    let ours = process::id();
    let is_ours = |window: &Window| window.pid().unwrap_or(0) == ours;
    let worth_asking = |window: &Window| {
        !window.title().unwrap_or_default().trim().is_empty()
            && !window.is_minimized().unwrap_or(true)
            && window.width().unwrap_or(0) as i32 >= MIN_SIDE
            && window.height().unwrap_or(0) as i32 >= MIN_SIDE
    };

    // The list comes back in z-order, so the first match is the frontmost that qualifies.
    let target = listed
        .iter()
        .find(|window| {
            window.is_focused().unwrap_or(false) && !is_ours(window) && worth_asking(window)
        })
        .or_else(|| listed.iter().find(|window| !is_ours(window) && worth_asking(window)));

    if let Some(window) = target {
        match window_picture(window) {
            Ok(payload) => return Ok(payload),
            // Some windows refuse to be printed (DRM video, the secure desktop). The screen will
            // still show what the user meant, so fall through rather than failing outright.
            Err(error) => eprintln!("dobot: window capture failed, falling back to the screen: {error}"),
        }
    }
    screen_picture(app)
}

/// Capture a window directly. The pixels come from the window itself (PrintWindow), not from a
/// rectangle of the screen, so a dot or a panel floating over it never ends up in the picture.
fn window_picture(window: &Window) -> Result<SelectionPayload, String> {
    let image = window
        .capture_image()
        .map_err(|error| format!("could not capture that window: {error}"))?;
    Ok(SelectionPayload {
        image: capture::to_base64(&capture::encode_png(&image)?),
        region: Region {
            x: window.x().unwrap_or(0),
            y: window.y().unwrap_or(0),
            width: window.width().unwrap_or(0),
            height: window.height().unwrap_or(0),
            monitor: 0,
        },
        application: window.app_name().unwrap_or_default(),
        window_title: window.title().unwrap_or_default(),
        error: String::new(),
    })
}

/// The whole desktop. Our own floating windows are lifted out of the shot for a moment and put
/// straight back — the dot and the panel are always on top, so they would otherwise be in it.
fn screen_picture(app: &AppHandle) -> Result<SelectionPayload, String> {
    let (x, y, width, height) = capture::virtual_bounds()?;
    let region = Region {
        x,
        y,
        width,
        height,
        monitor: 0,
    };
    let hidden = windows::hide_for_capture(app);
    let result = capture::capture_region(&region);
    windows::restore_after_capture(app, hidden);
    Ok(SelectionPayload {
        image: result?,
        region,
        application: String::new(),
        window_title: "Whole screen".into(),
        error: String::new(),
    })
}
