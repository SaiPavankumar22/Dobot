//! Global hotkeys. These are the escape hatches: reach the assistant from anywhere, talk to it with
//! one chord, ask about the window you are in, and stop Dobot with a single chord.
//!
//! **Push-to-talk.** The talk chord is the quick-ask panel chord doing what it always wanted to do:
//! press it and the panel comes up already listening, let go of a key you have held for `HOLD_MS`
//! and what you said is sent, tap it twice to send instead. Holding is what makes it feel like an
//! intercom; tapping is there for when a held key is awkward. The chord's own key auto-repeats
//! while it is held — at whatever interval the keyboard likes — so a press only counts as a new one
//! once the key has come up, and two presses within `REPEAT_MS` of that are still one.
//!
//! The chord's work is split: Rust decides *when* to listen (it owns the key events, and it must
//! answer even when no window is looking), the panel owns *how* — the microphone, the waveform, the
//! transcription and the send. The two are stitched by the `dobot://voice` event.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Emitter};
use tauri_plugin_global_shortcut::{
    Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutEvent, ShortcutState,
};

/// Ctrl+Shift+Space — talk to Dobot (and open the quick-ask panel)
/// Ctrl+Shift+S     — select a screen region
/// Ctrl+Alt+L       — show Dobot the window you are in
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
            "look",
            Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyL),
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

/// Held this long before letting go sends; a shorter press is a tap, and the next tap sends.
const HOLD_MS: Duration = Duration::from_millis(400);
/// A release is reported on a 50 ms poll, so it can arrive just behind the next press; presses
/// closer together than this after the key comes up are one gesture, not two requests to send.
const REPEAT_MS: Duration = Duration::from_millis(200);

/// Where the talk chord is: whether the panel should be listening, when the press that started it
/// came in, when the most recent press came in, and whether the key has come up since — which is
/// what tells a held key's auto-repeat from a second tap.
struct Talk {
    listening: bool,
    started: Option<Instant>,
    last_press: Option<Instant>,
    released: bool,
}

/// Process-wide, because the chord is process-wide. Never held across an `emit`, so a listener that
/// re-enters this module (the `voice_done` command) cannot deadlock.
static TALK: Mutex<Talk> = Mutex::new(Talk {
    listening: false,
    started: None,
    last_press: None,
    released: false,
});

fn talk<F, T>(f: F) -> T
where
    F: FnOnce(&mut Talk) -> T,
{
    // A panic inside a lock only happens if a caller already panicked; the state is a few booleans,
    // so carrying on with it is better than taking the whole shell down with a poisoned mutex.
    let mut guard = TALK.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    f(&mut guard)
}

/// The chord was pressed. Returns what the panel should do: `"start"` listening, or `"stop"` and
/// send — `""` when this press is a repeat of one already acted on.
fn talk_press_at(state: &mut Talk, now: Instant) -> &'static str {
    if !state.listening {
        state.listening = true;
        state.started = Some(now);
        state.last_press = Some(now);
        state.released = false;
        return "start";
    }
    // A key that has not come up since the last press is still being held: this is its auto-repeat,
    // however long the keyboard waits before repeating. Only a key that went up can be pressed again.
    if !state.released {
        return "";
    }
    let gap = now - state.last_press.unwrap_or(now);
    state.last_press = Some(now);
    state.released = false;
    // The release can be reported a beat behind the next press; that race is not a second request.
    if gap < REPEAT_MS {
        return "";
    }
    state.listening = false;
    state.started = None;
    "stop"
}

/// The chord was released. Letting go of a key held long enough sends; a tap just keeps listening
/// for the next one.
fn talk_release_at(state: &mut Talk, now: Instant) -> &'static str {
    state.released = true;
    if !state.listening {
        return "";
    }
    let held = state.started.map(|at| now - at >= HOLD_MS).unwrap_or(true);
    if !held {
        return "";
    }
    state.listening = false;
    state.started = None;
    "stop"
}

fn talk_press() -> &'static str {
    talk(|state| talk_press_at(state, Instant::now()))
}

fn talk_release() -> &'static str {
    talk(|state| talk_release_at(state, Instant::now()))
}

/// Whether the chord currently holds the panel in listening state.
pub fn talk_is_listening() -> bool {
    talk(|state| state.listening)
}

/// The panel has stopped on its own — transcribed, cancelled, or the microphone never answered —
/// so the next press starts fresh instead of trying to stop something that is already over.
pub fn talk_done() {
    talk(|state| {
        state.listening = false;
        state.started = None;
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn idle() -> Talk {
        Talk {
            listening: false,
            started: None,
            last_press: None,
            released: false,
        }
    }

    fn at(offset_ms: u64) -> Instant {
        Instant::now() + Duration::from_millis(offset_ms)
    }

    #[test]
    fn first_press_starts_listening() {
        let mut session = idle();
        assert_eq!(talk_press_at(&mut session, at(0)), "start");
        assert!(session.listening);
    }

    #[test]
    fn a_hold_is_one_press_not_many() {
        // The chord's key auto-repeats while it is held — even after a long keyboard delay — and
        // none of those repeats may send, because the key has not come up.
        let mut session = idle();
        talk_press_at(&mut session, at(0));
        for offset in [500, 533, 566, 700, 1_400, 2_100, 9_000] {
            assert_eq!(
                talk_press_at(&mut session, at(offset)),
                "",
                "repeat at {offset}ms must not send"
            );
            assert!(session.listening);
        }
    }

    #[test]
    fn letting_go_of_a_hold_sends() {
        let mut session = idle();
        talk_press_at(&mut session, at(0));
        assert_eq!(talk_release_at(&mut session, at(900)), "stop");
        assert!(!session.listening);
        // The platform reports a release once per repeat of the chord; only the first counts.
        assert_eq!(talk_release_at(&mut session, at(950)), "");
    }

    #[test]
    fn a_tap_keeps_listening_and_the_next_press_sends() {
        let mut session = idle();
        assert_eq!(talk_press_at(&mut session, at(0)), "start");
        assert_eq!(talk_release_at(&mut session, at(150)), ""); // too short to be a hold
        assert!(session.listening);
        assert_eq!(talk_press_at(&mut session, at(1_000)), "stop"); // tap again to send
    }

    #[test]
    fn a_brisk_second_tap_still_sends() {
        let mut session = idle();
        talk_press_at(&mut session, at(0));
        talk_release_at(&mut session, at(150));
        assert_eq!(talk_press_at(&mut session, at(400)), "stop");
    }

    #[test]
    fn a_release_before_anything_is_listening_is_only_a_note() {
        let mut session = idle();
        assert_eq!(talk_release_at(&mut session, at(0)), "");
        assert!(!session.listening);
        assert!(session.released);
    }

    #[test]
    fn the_panel_stopping_resets_the_chord() {
        let mut session = idle();
        talk_press_at(&mut session, at(0));
        session.listening = false;
        session.started = None;
        session.released = true;
        // After the panel has finished (transcribed, cancelled, mic refused), a press starts again.
        assert_eq!(talk_press_at(&mut session, at(2_000)), "start");
        assert!(session.listening);
    }
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
    let action = shortcuts()
        .into_iter()
        .find(|(_, candidate)| candidate == shortcut)
        .map(|(name, _)| name);
    let Some(action) = action else { return };

    if action == "panel" {
        // Both edges of the chord matter here: pressed starts (or reaps) a listening session,
        // released may end one. Every other action fires once, on press.
        let signal = match event.state {
            ShortcutState::Pressed => talk_press(),
            ShortcutState::Released => talk_release(),
        };
        if signal.is_empty() {
            return;
        }
        let _ = app.emit("dobot://hotkey", action);
        // The panel is what records, so it must be on screen before it is told to listen.
        crate::windows::open_panel(app);
        let _ = app.emit("dobot://voice", signal);
        return;
    }

    if event.state != ShortcutState::Pressed {
        return;
    }
    let _ = app.emit("dobot://hotkey", action);

    // A few actions are handled natively so they work even if no webview is focused.
    match action {
        "dashboard" => crate::windows::open_dashboard(app, None),
        // Capturing takes a moment and must happen while the other application is still in front,
        // so it leaves the handler thread rather than holding up the key event.
        "look" => {
            let handle = app.clone();
            std::thread::spawn(move || crate::look::ask(&handle));
        }
        _ => {}
    }
}
