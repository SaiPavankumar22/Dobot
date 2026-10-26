"""Prepare Dobot's supplied logo assets for the README, UI, and Windows bundle."""

from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "assets"
ICON_SOURCE = ASSETS / "Glossy Blue Playmark App Icon.png"
WORDMARK_SOURCE = ASSETS / "Dobot Blue Gradient Logo.png"
ICON_OUT = ROOT / "desktop" / "src-tauri" / "icons"
UI_OUT = ROOT / "desktop" / "public" / "branding"
DOCS_OUT = ROOT / "docs" / "branding"


def transparent_icon() -> Image.Image:
    image = Image.open(ICON_SOURCE).convert("RGBA")
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            r, g, b, a = pixels[x, y]
            if r > 245 and g > 245 and b > 245:
                pixels[x, y] = (r, g, b, 0)
    return image


def dark_theme_wordmark() -> Image.Image:
    image = Image.open(WORDMARK_SOURCE).convert("RGBA")
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            r, g, b, a = pixels[x, y]
            if r > 245 and g > 245 and b > 245:
                pixels[x, y] = (r, g, b, 0)
            elif r < 75 and g < 85 and b < 125:
                pixels[x, y] = (235, 243, 255, a)
    return image


def main() -> None:
    ICON_OUT.mkdir(parents=True, exist_ok=True)
    UI_OUT.mkdir(parents=True, exist_ok=True)
    DOCS_OUT.mkdir(parents=True, exist_ok=True)

    icon = transparent_icon()
    for name, size in {
        "32x32.png": 32,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        "icon.png": 512,
    }.items():
        icon.resize((size, size), Image.Resampling.LANCZOS).save(ICON_OUT / name)
    icon.resize((256, 256), Image.Resampling.LANCZOS).save(
        ICON_OUT / "icon.ico", format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )

    dark_theme_wordmark().save(UI_OUT / "dobot-logo.png")
    shutil.copyfile(WORDMARK_SOURCE, DOCS_OUT / "dobot-logo.png")
    print("prepared Dobot branding assets")


if __name__ == "__main__":
    main()
