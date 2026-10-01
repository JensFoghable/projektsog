"""Render the Projektsøg app icon: a magnifying glass over a film frame.

Dev-time tool (needs Pillow; the app itself never imports it):

    python tools/make_icon.py [--preview PATH]

Writes ``projektsog/assets/icon.ico`` (16, 20, 24, 32, 40, 48, 64, 256 px; 32-bit BMP entries
up to 64 px and a PNG entry for 256 px) and ``projektsog/assets/icon.png`` (256 px). Every
size is drawn separately with its own level of detail, and shapes are snapped to the pixel
grid at small sizes, so the tray icon stays crisp instead of being a blurry downscale.
"""

from __future__ import annotations

import argparse
import io
import math
import os
import struct

from PIL import Image, ImageDraw

SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "projektsog", "assets")

BG_TOP = (52, 60, 78)
BG_BOTTOM = (24, 28, 37)
FILM = (237, 240, 245)
HOLE = (30, 35, 46)
PICTURE_TOP = (86, 116, 158)
PICTURE_BOTTOM = (46, 62, 88)
AMBER = (255, 178, 46)
AMBER_DARK = (233, 136, 12)
HALO = (22, 26, 34)


def _vertical_gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
    width, height = size
    column = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        column.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return column.resize((width, height))


def render(size: int) -> Image.Image:
    """Draw the icon at ``size`` px (supersampled, then box-filtered down)."""
    ss = max(4, 1024 // size)
    canvas = size * ss
    unit = canvas / 256.0
    snap = size <= 48

    def px(v: float) -> int:
        """Design units (0–256) → canvas px; snapped to whole target pixels when small."""
        return round(v * size / 256) * ss if snap else round(v * unit)

    def box(x0: float, y0: float, x1: float, y1: float) -> list[int]:
        return [px(x0), px(y0), px(x1) - 1, px(y1) - 1]

    # Background: dark rounded square with a subtle vertical gradient.
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    mask = Image.new("L", (canvas, canvas), 0)
    inset = px(8) if size > 24 else 0
    ImageDraw.Draw(mask).rounded_rectangle([inset, inset, canvas - 1 - inset, canvas - 1 - inset],
                                           radius=max(px(52), 3 * ss), fill=255)
    img.paste(_vertical_gradient((canvas, canvas), BG_TOP, BG_BOTTOM), (0, 0), mask)

    # Film frame: perforated strip with a picture window (less detail when small).
    draw = ImageDraw.Draw(img)
    frame = (26, 58, 190, 178) if size <= 20 else (30, 60, 190, 176)
    window = (26, 98, 190, 138) if size <= 20 else (42, 98, 178, 138)
    draw.rounded_rectangle(box(*frame), radius=max(px(10), ss), fill=FILM)
    window_mask = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(window_mask).rounded_rectangle(box(*window), radius=px(4) if size > 32 else 0,
                                                  fill=255)
    img.paste(_vertical_gradient((canvas, canvas), PICTURE_TOP, PICTURE_BOTTOM), (0, 0),
              window_mask)
    holes = 0 if size <= 20 else 3 if size <= 32 else 4 if size <= 48 else 5
    if holes:
        left, right = frame[0] + 14, frame[2] - 14
        hole_w = 16 if holes >= 5 else 22 if holes == 4 else 28
        gap = (right - left - holes * hole_w) / (holes - 1)
        for row_top, row_bottom in ((72, 86), (150, 164)):
            for i in range(holes):
                x0 = left + i * (hole_w + gap)
                draw.rounded_rectangle(box(x0, row_top, x0 + hole_w, row_bottom),
                                       radius=px(3) if size > 48 else 0, fill=HOLE)
    scene = img.copy()          # what the lens magnifies

    # Magnifying glass geometry (design units scaled to the canvas).
    cx = cy = 164 * unit
    ring_outer = 54 * unit
    ring_width = (17 if size > 32 else 22 if size > 20 else 26) * unit
    inner = ring_outer - ring_width
    halo = (7 if size > 24 else 10) * unit
    handle_width = (24 if size > 32 else 30) * unit
    reach = ring_outer - ring_width / 2
    hx0 = hy0 = cx + reach * math.cos(math.radians(45))
    hx1 = hy1 = 218 * unit

    def disc(x: float, y: float, r: float, fill: tuple) -> None:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)

    def stroke(width: float, fill: tuple) -> None:
        draw.line([hx0, hy0, hx1, hy1], fill=fill, width=round(width))
        disc(hx0, hy0, width / 2, fill)
        disc(hx1, hy1, width / 2, fill)

    stroke(handle_width + 2 * halo, HALO)      # dark halo separates the glass from the film
    disc(cx, cy, ring_outer + halo, HALO)
    stroke(handle_width, AMBER_DARK)

    # Lens: the film behind it magnified 1.6× (a dark blue glass when too small to read),
    # under a faint glass tint and a highlight.
    lens_box = [round(cx - inner), round(cy - inner), round(cx + inner), round(cy + inner)]
    if size >= 32:
        diameter = lens_box[2] - lens_box[0]
        source = inner / 1.6
        magnified = scene.crop((round(cx - source), round(cy - source),
                                round(cx + source), round(cy + source))).resize(
            (diameter, diameter), Image.LANCZOS)
        lens_mask = Image.new("L", (diameter, diameter), 0)
        ImageDraw.Draw(lens_mask).ellipse([0, 0, diameter - 1, diameter - 1], fill=255)
        img.paste(magnified, (lens_box[0], lens_box[1]), lens_mask)
    else:
        draw.ellipse(lens_box, fill=PICTURE_BOTTOM)
    glass = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    glass_draw = ImageDraw.Draw(glass)
    glass_draw.ellipse(lens_box, fill=(255, 255, 255, 34))
    if size >= 32:
        arc_r = inner * 0.66
        glass_draw.arc([cx - arc_r, cy - arc_r, cx + arc_r, cy + arc_r], start=200, end=250,
                       fill=(255, 255, 255, 215), width=max(ss, round(6 * unit)))
    img.alpha_composite(glass)
    draw.ellipse([cx - ring_outer, cy - ring_outer, cx + ring_outer, cy + ring_outer],
                 outline=AMBER, width=round(ring_width))
    return img.reduce(ss)


def _bmp_entry(image: Image.Image) -> bytes:
    """32-bit BGRA DIB (bottom-up) + 1-bit AND mask, as stored inside .ico files."""
    width, height = image.size
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for x in range(width):
            r, g, b, a = image.getpixel((x, y))
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    mask_stride = ((width + 31) // 32) * 4
    mask_rows = []
    for y in range(height - 1, -1, -1):
        bits = bytearray(mask_stride)
        for x in range(width):
            if image.getpixel((x, y))[3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask_rows.append(bytes(bits))
    return header + b"".join(rows) + b"".join(mask_rows)


def _png_bytes(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def write_ico(path: str, images: dict[int, Image.Image]) -> None:
    entries = [(size, _png_bytes(img) if size >= 256 else _bmp_entry(img))
               for size, img in sorted(images.items())]
    offset = 6 + 16 * len(entries)
    directory = struct.pack("<HHH", 0, 1, len(entries))
    blobs = b""
    for size, data in entries:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    with open(path, "wb") as fh:
        fh.write(directory + blobs)


def write_preview(path: str, images: dict[int, Image.Image]) -> None:
    """Contact sheet: every size at 1× on dark and light backgrounds, plus 8× blow-ups."""
    scale = 8
    small = [s for s in SIZES if s <= 64]
    width = 40 + sum(s * scale + 24 for s in small) + 280
    height = 64 * scale + 140
    sheet = Image.new("RGBA", (width, height), (40, 40, 40, 255))
    x = 20
    for size in small:
        sheet.alpha_composite(images[size].resize((size * scale, size * scale), Image.NEAREST), (x, 20))
        sheet.alpha_composite(images[size], (x, 64 * scale + 40))
        light = Image.new("RGBA", (size + 8, size + 8), (238, 238, 238, 255))
        light.alpha_composite(images[size], (4, 4))
        sheet.alpha_composite(light, (x + size + 12, 64 * scale + 36))
        x += size * scale + 24
    sheet.alpha_composite(images[256], (x, 20))
    sheet.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--preview", help="also write a contact sheet PNG here")
    args = parser.parse_args()
    images = {size: render(size) for size in SIZES}
    os.makedirs(ASSETS, exist_ok=True)
    write_ico(os.path.join(ASSETS, "icon.ico"), images)
    images[256].save(os.path.join(ASSETS, "icon.png"), optimize=True)
    if args.preview:
        write_preview(args.preview, images)
    print(f"wrote {ASSETS}\\icon.ico ({', '.join(map(str, SIZES))}) and icon.png")


if __name__ == "__main__":
    main()
