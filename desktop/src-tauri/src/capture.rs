//! Screen capture. Capture happens only when the frontend asks for it — there is no polling loop, no
//! continuous monitoring, and nothing is written to disk: the caller receives PNG bytes in memory.

use base64::Engine;
use image::{ImageFormat, RgbaImage};
use serde::{Deserialize, Serialize};
use std::io::Cursor;
use xcap::Monitor;

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Region {
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    #[serde(default)]
    pub monitor: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct MonitorInfo {
    pub index: usize,
    pub name: String,
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    pub primary: bool,
}

pub fn monitors() -> Result<Vec<Monitor>, String> {
    Monitor::all().map_err(|error| format!("could not enumerate monitors: {error}"))
}

// xcap reports every geometry accessor as a Result; a monitor that cannot answer is not a reason to
// fail the whole capture, so these degrade to zero.
fn name_of(monitor: &Monitor) -> String {
    monitor.name().unwrap_or_else(|_| "display".to_string())
}

fn x_of(monitor: &Monitor) -> i32 {
    monitor.x().unwrap_or(0)
}

fn y_of(monitor: &Monitor) -> i32 {
    monitor.y().unwrap_or(0)
}

fn w_of(monitor: &Monitor) -> u32 {
    monitor.width().unwrap_or(0)
}

fn h_of(monitor: &Monitor) -> u32 {
    monitor.height().unwrap_or(0)
}

pub fn monitor_list() -> Result<Vec<MonitorInfo>, String> {
    let monitors = monitors()?;
    Ok(monitors
        .iter()
        .enumerate()
        .map(|(index, monitor)| MonitorInfo {
            index,
            name: name_of(monitor),
            x: x_of(monitor),
            y: y_of(monitor),
            width: w_of(monitor),
            height: h_of(monitor),
            primary: index == 0,
        })
        .collect())
}

/// Bounding box covering every monitor, used to size the selection overlay.
pub fn virtual_bounds() -> Result<(i32, i32, u32, u32), String> {
    let monitors = monitors()?;
    if monitors.is_empty() {
        return Err("no monitors were reported by the platform".into());
    }
    let min_x = monitors.iter().map(x_of).min().unwrap_or(0);
    let min_y = monitors.iter().map(y_of).min().unwrap_or(0);
    let max_x = monitors
        .iter()
        .map(|m| x_of(m) + w_of(m) as i32)
        .max()
        .unwrap_or(0);
    let max_y = monitors
        .iter()
        .map(|m| y_of(m) + h_of(m) as i32)
        .max()
        .unwrap_or(0);
    Ok((min_x, min_y, (max_x - min_x) as u32, (max_y - min_y) as u32))
}

fn monitor_for(region: &Region) -> Result<Monitor, String> {
    let monitors = monitors()?;
    if monitors.is_empty() {
        return Err("no monitors available".into());
    }
    // Prefer the monitor that actually contains the selection's origin.
    let centre_x = region.x + (region.width as i32 / 2);
    let centre_y = region.y + (region.height as i32 / 2);
    for monitor in &monitors {
        let (mx, my) = (x_of(monitor), y_of(monitor));
        let (mw, mh) = (w_of(monitor) as i32, h_of(monitor) as i32);
        if centre_x >= mx && centre_x < mx + mw && centre_y >= my && centre_y < my + mh {
            return Ok(monitor.clone());
        }
    }
    Ok(monitors
        .get(region.monitor)
        .cloned()
        .unwrap_or_else(|| monitors[0].clone()))
}

pub fn encode_png(image: &RgbaImage) -> Result<Vec<u8>, String> {
    let mut buffer = Cursor::new(Vec::new());
    image
        .write_to(&mut buffer, ImageFormat::Png)
        .map_err(|error| format!("could not encode PNG: {error}"))?;
    Ok(buffer.into_inner())
}

pub fn to_base64(png: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(png)
}

/// Capture a rectangle given in virtual-desktop coordinates.
pub fn capture_region(region: &Region) -> Result<String, String> {
    let monitor = monitor_for(region)?;
    let shot = monitor
        .capture_image()
        .map_err(|error| format!("screen capture failed: {error}"))?;

    if region.width < 1 || region.height < 1 {
        return Ok(to_base64(&encode_png(&shot)?));
    }

    // Translate virtual coordinates into monitor-local coordinates and clamp to the captured frame.
    let local_x = (region.x - x_of(&monitor)).max(0) as u32;
    let local_y = (region.y - y_of(&monitor)).max(0) as u32;
    let max_w = shot.width().saturating_sub(local_x);
    let max_h = shot.height().saturating_sub(local_y);
    let width = region.width.min(max_w).max(1);
    let height = region.height.min(max_h).max(1);

    let cropped = image::imageops::crop_imm(&shot, local_x, local_y, width, height).to_image();
    Ok(to_base64(&encode_png(&cropped)?))
}

/// The snip overlay measures its marquee in CSS pixels counted from the overlay's own top-left
/// corner, while everything below this line works in physical virtual-desktop pixels (what xcap
/// reports). Above 100% display scaling those are different numbers, so a region coming from the
/// frontend is converted here, once, with the overlay's real origin and scale factor.
pub fn to_physical(region: &Region, origin: (i32, i32), scale: f64) -> Region {
    let scale = if scale.is_finite() && scale > 0.0 { scale } else { 1.0 };
    let convert = |value: f64| (value * scale).round();
    Region {
        x: origin.0 + convert(region.x as f64) as i32,
        y: origin.1 + convert(region.y as f64) as i32,
        width: convert(region.width as f64).max(1.0) as u32,
        height: convert(region.height as f64).max(1.0) as u32,
        monitor: region.monitor,
    }
}

#[cfg(test)]
mod smoke {
    use super::*;

    /// Needs a real desktop to paint on — run with `cargo test -- --ignored` on a machine with a
    /// display; CI only ever runs `cargo check`.
    #[ignore = "captures a real screen"]
    #[test]
    fn capture_a_region_end_to_end() {
        let list = monitors().expect("enumerate monitors");
        for (index, monitor) in list.iter().enumerate() {
            println!(
                "monitor[{index}] name={:?} at {},{} {}x{} scale={:?}",
                name_of(monitor),
                x_of(monitor),
                y_of(monitor),
                w_of(monitor),
                h_of(monitor),
                monitor.scale_factor()
            );
        }
        let bounds = virtual_bounds().expect("virtual bounds");
        println!("virtual bounds: {bounds:?}");

        let region = Region {
            x: 0,
            y: 0,
            width: 200,
            height: 120,
            monitor: 0,
        };
        let encoded = capture_region(&region).expect("capture_region");
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(&encoded)
            .expect("base64");
        let decoded = image::load_from_memory(&bytes).expect("decode png").to_rgba8();
        println!("captured png: {}x{} ({} bytes)", decoded.width(), decoded.height(), bytes.len());
        assert_eq!((decoded.width(), decoded.height()), (200, 120));
        // A genuinely blank capture would be every pixel identical.
        let first = decoded.get_pixel(0, 0).0;
        let varied = decoded
            .pixels()
            .any(|pixel| pixel.0 != first);
        assert!(varied, "capture came back as a single flat colour");
    }

    #[ignore = "captures a real screen"]
    #[test]
    fn capture_a_window_end_to_end() {
        let windows = xcap::Window::all().expect("list windows");
        let mut attempted = 0usize;
        let mut captured = 0usize;
        for window in &windows {
            let title = window.title().unwrap_or_default();
            if title.trim().is_empty() || window.is_minimized().unwrap_or(true) {
                continue;
            }
            let width = window.width().unwrap_or(0);
            let height = window.height().unwrap_or(0);
            if width < 64 || height < 64 {
                continue;
            }
            attempted += 1;
            match window.capture_image() {
                Ok(shot) => {
                    captured += 1;
                    if captured == 1 {
                        let png = encode_png(&shot).expect("encode");
                        println!("window {title:?} {width}x{height} -> png {} bytes", png.len());
                    }
                }
                Err(error) => println!("window {title:?} {width}x{height} FAILED: {error}"),
            }
            if attempted >= 8 {
                break;
            }
        }
        println!("windows attempted={attempted} captured={captured}");
        assert!(attempted > 0, "no capturable window found");
        println!("windows captured={captured}/{attempted} (failures are printed above)");
    }
}

#[cfg(test)]
mod coordinate_tests {
    use super::*;

    fn region(x: i32, y: i32, width: u32, height: u32) -> Region {
        Region { x, y, width, height, monitor: 0 }
    }

    #[test]
    fn a_100_percent_display_needs_no_conversion() {
        let converted = to_physical(&region(120, 60, 400, 300), (0, 0), 1.0);
        assert_eq!((converted.x, converted.y), (120, 60));
        assert_eq!((converted.width, converted.height), (400, 300));
    }

    #[test]
    fn a_full_screen_selection_at_125_percent_fills_the_physical_screen() {
        // This machine: 1920x1080 physical at scale 1.25 → the overlay's CSS viewport is
        // 1536x864. Dragging across all of it must come back as the whole physical screen.
        let converted = to_physical(&region(0, 0, 1536, 864), (0, 0), 1.25);
        assert_eq!((converted.x, converted.y), (0, 0));
        assert_eq!((converted.width, converted.height), (1920, 1080));
    }

    #[test]
    fn a_selection_in_the_lower_right_lands_where_it_was_drawn() {
        let converted = to_physical(&region(1136, 664, 400, 200), (0, 0), 1.25);
        assert_eq!((converted.x, converted.y), (1420, 830));
        assert_eq!((converted.width, converted.height), (500, 250));
    }

    #[test]
    fn a_second_monitor_left_of_the_origin_keeps_its_offset() {
        let converted = to_physical(&region(10, 20, 100, 100), (-1920, 0), 1.5);
        assert_eq!((converted.x, converted.y), (-1905, 30));
        assert_eq!((converted.width, converted.height), (150, 150));
    }

    #[test]
    fn a_nonsense_scale_falls_back_to_identity() {
        for scale in [0.0, -1.0, f64::NAN] {
            let converted = to_physical(&region(7, 9, 11, 13), (0, 0), scale);
            assert_eq!((converted.x, converted.y), (7, 9));
            assert_eq!((converted.width, converted.height), (11, 13));
        }
    }
}
