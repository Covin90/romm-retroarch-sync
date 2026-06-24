#!/usr/bin/env python3
"""Generate RomM-branded Steam library artwork for the RomM launcher tile.

Composites the RomM isotipo (brand mark) onto a RomM-purple gradient background,
producing every artwork asset type Steam's SetCustomArtworkForApp accepts:

    0 Grid    portrait capsule   600x900   -> romm-grid.png
    1 Hero    background banner   1920x620  -> romm-hero.png
    2 Logo    transparent mark    640x640   -> romm-logo.png
    3 Header  landscape capsule   920x430   -> romm-header.png
    4 Icon    square icon         256x256   -> romm-icon.png

Requires cairosvg (dev-only) to rasterize the SVG; output PNGs are bundled in
decky_plugin/assets/ so the plugin needs no SVG renderer at runtime.

Run from repo root:  python3 scripts/gen_romm_artwork.py
"""
import io
from pathlib import Path

import cairosvg
from PIL import Image, ImageFilter

ASSETS = Path(__file__).resolve().parent.parent / "decky_plugin" / "assets"
ISOTIPO = ASSETS / "romm-isotipo.svg"
# RomM wordmark ("RomM"); same asset the plugin home nav shows next to the mark.
LOGOTIPO = ASSETS / "romm-logotipo.svg"
# RomM's own brand backdrop (the login/auth page background): purple base with
# a dark-purple and a peach blob. Fetched from a RomM server's /assets and
# bundled so the artwork matches RomM exactly.
AUTH_BG = ASSETS / "auth_background.svg"

BG = (7, 7, 15)          # --r-color-bg #07070f (overlay vignette color)
GLOW = (139, 116, 232)   # #8b74e8 brand primary (subtle logo halo)


def render_isotipo(size: int) -> Image.Image:
    """Rasterize the isotipo SVG to a square RGBA image of `size` px."""
    png = cairosvg.svg2png(url=str(ISOTIPO), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def render_wordmark(width: int, color=(255, 255, 255)) -> Image.Image:
    """Rasterize the RomM wordmark, recolored (default white) on transparency."""
    png = cairosvg.svg2png(url=str(LOGOTIPO), output_width=width)
    word = Image.open(io.BytesIO(png)).convert("RGBA")
    solid = Image.new("RGBA", word.size, color + (255,))
    solid.putalpha(word.split()[3])
    return solid.crop(solid.getbbox())


def _grad(length: int, stops, vertical: bool) -> Image.Image:
    """1-px-wide (or tall) BG-colored gradient with alpha `stops` = [(pos0..1, opacity0..1)]."""
    line = Image.new("RGBA", (1, length) if vertical else (length, 1))
    px = line.load()
    for i in range(length):
        t = i / max(1, length - 1)
        # piecewise-linear interpolation between the surrounding stops
        a = stops[0][1]
        for (p0, a0), (p1, a1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                a = a0 + (a1 - a0) * ((t - p0) / max(1e-6, p1 - p0))
                break
            if t > p1:
                a = a1
        px[(0, i) if vertical else (i, 0)] = BG + (int(a * 255),)
    return line.resize((1, length) if vertical else (length, 1))


def romm_bg(w: int, h: int) -> Image.Image:
    """RomM's auth_background composed exactly as the site does it:
    scale(1.08) + center-20% crop, blur(28px-equivalent), then the two-gradient
    BG-color vignette overlay (global.css .r-v2-bg__layer / __overlay)."""
    bw, bh = 2160, 840
    png = cairosvg.svg2png(url=str(AUTH_BG), output_width=bw, output_height=bh)
    src = Image.open(io.BytesIO(png)).convert("RGBA")
    # cover-fit with the site's 1.08 zoom so the blur never bleeds an edge
    scale = max(w / bw, h / bh) * 1.08
    rw, rh = max(w, int(bw * scale)), max(h, int(bh * scale))
    src = src.resize((rw, rh), Image.BICUBIC)
    left = (rw - w) // 2
    top = int((rh - h) * 0.20)            # background-position: center 20%
    cropped = src.crop((left, top, left + w, top + h))
    # blur(28px) is authored against a ~1920px-wide viewport; scale to canvas.
    blur = max(10, round(28 * (w / 1920)))
    bg = cropped.filter(ImageFilter.GaussianBlur(blur)).convert("RGBA")
    # Overlay: horizontal (72%/30%/55%) over vertical (10%/0/70%/92%), BG color.
    horiz = _grad(w, [(0.0, 0.72), (0.55, 0.30), (1.0, 0.55)], vertical=False).resize((w, h))
    vert = _grad(h, [(0.0, 0.10), (0.35, 0.0), (0.72, 0.70), (1.0, 0.92)], vertical=True).resize((w, h))
    bg.alpha_composite(vert)
    bg.alpha_composite(horiz)
    return bg


def compose(w: int, h: int, mark_frac: float = 0.0) -> Image.Image:
    """Composed RomM background, optionally with a centered isotipo (no glow,
    as RomM has none). mark_frac == 0 -> background only (horizontal covers)."""
    canvas = romm_bg(w, h)
    if mark_frac:
        ms = int(min(w, h) * mark_frac)
        mark = render_isotipo(ms)
        x, y = (w - ms) // 2, (h - ms) // 2
        # Soft brand-purple glow: the mark's real alpha (not a hard threshold,
        # which made a ringy halo) tinted purple and blurred wide, so it reads
        # as ambient glow that lifts the mark off the blurred backdrop.
        glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        g = Image.new("RGBA", mark.size, GLOW + (0,))
        g.putalpha(mark.split()[3].point(lambda a: int(a * 0.55)))
        glow.alpha_composite(g, (x, y))
        glow = glow.filter(ImageFilter.GaussianBlur(max(8, ms // 12)))
        canvas.alpha_composite(glow)
        canvas.alpha_composite(mark, (x, y))
    return canvas


def subtle_glow(img: Image.Image, blur: int, opacity: float) -> Image.Image:
    """A tight brand-purple halo from `img`'s solid alpha. Thresholded so faint
    SVG edge pixels don't smear into a square; small blur keeps it close."""
    alpha = img.split()[3].point(lambda a: 255 if a > 160 else 0)
    glow = Image.new("RGBA", img.size, GLOW + (0,))
    glow.putalpha(alpha.point(lambda a: int(a * opacity)))
    return glow.filter(ImageFilter.GaussianBlur(blur))


def brand_logo() -> Image.Image:
    """Transparent 'logo' overlay = isotipo mark + white 'RomM' wordmark, the
    same lockup the plugin home nav shows. Steam paints this over the hero in
    place of the text name, so it carries the name onto the tile."""
    mark_h = 360
    mark = render_isotipo(mark_h)
    word = render_wordmark(900)               # white
    scale = (mark_h * 0.62) / word.height     # wordmark ~62% of mark height
    word = word.resize((int(word.width * scale), int(word.height * scale)), Image.LANCZOS)
    gap = int(mark_h * 0.10)
    pad = int(mark_h * 0.16)                  # room so the soft glow isn't clipped
    w = pad + mark.width + gap + word.width + pad
    h = pad + mark_h + pad
    content = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    content.alpha_composite(mark, (pad, (h - mark.height) // 2))
    content.alpha_composite(word, (pad + mark.width + gap, (h - word.height) // 2))
    # Subtle, tight brand halo behind the lockup.
    canvas = subtle_glow(content, blur=max(4, mark_h // 28), opacity=0.40)
    canvas.alpha_composite(content)
    return canvas


def compose_grid(w: int, h: int) -> Image.Image:
    """Portrait capsule (Steam grid): the RomM mark + 'RomM' wordmark lockup on
    the blurred brand background, sitting in the lower third for a branded
    key-art look that also shows the name on the vertical tile."""
    canvas = romm_bg(w, h)
    lock = brand_logo()                       # mark + white 'RomM' wordmark
    tw = int(w * 0.78)
    lock = lock.resize((tw, round(lock.height * tw / lock.width)), Image.LANCZOS)
    x = (w - lock.width) // 2
    y = int(h * 0.82) - lock.height // 2
    canvas.alpha_composite(lock, (x, y))
    return canvas


def compose_banner(w: int, h: int) -> Image.Image:
    """Landscape capsule (Steam type 4, the {appid}.png that Big Picture's
    featured banner uses): the RomM mark + 'RomM' wordmark lockup on the blurred
    brand background, placed lower-left like a game key-art logo."""
    canvas = romm_bg(w, h)
    lock = brand_logo()                       # mark + white 'RomM' wordmark
    th = int(h * 0.34)
    lock = lock.resize((round(lock.width * th / lock.height), th), Image.LANCZOS)
    x = int(w * 0.06)
    y = h - lock.height - int(h * 0.10)       # lower-left, with a bottom margin
    canvas.alpha_composite(lock, (x, y))
    return canvas


def save(img: Image.Image, name: str):
    out = ASSETS / name
    img.convert("RGBA").save(out, "PNG", optimize=True)
    print(f"  {name}  {img.size[0]}x{img.size[1]}")


def main():
    print("Generating RomM Steam artwork ->", ASSETS)
    save(compose_grid(600, 900), "romm-grid.png")      # 0 Grid (portrait)
    save(compose(1920, 620), "romm-hero.png")          # 1 Hero (background only)
    save(brand_logo(), "romm-logo.png")                 # 2 Logo (mark + wordmark)
    save(compose_banner(920, 430), "romm-header.png")  # landscape banner (mark + name, left)
    save(compose(256, 256, 0.82), "romm-icon.png")     # 4 Icon
    print("Done.")


if __name__ == "__main__":
    main()
