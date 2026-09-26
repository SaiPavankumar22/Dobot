//! Screen capture. Capture happens only when the frontend asks for it — there is no polling loop, no
//! continuous monitoring, and nothing is written to disk: the caller receives PNG bytes in memory.

use base64::Engine;
use image::{ImageFormat, RgbaImage};
use serde::{Deserialize, Serialize};
use std::io::Cursor;
use xcap::Monitor;

#[derive(Debug, Clone, Serialize, Deserialize)]
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
