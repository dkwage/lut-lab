#!/usr/bin/env python3
"""Draw the LUT Lab app icon: the header mark as a before/after split.

uv run python scripts/make_icon.py
"""
from pathlib import Path
import subprocess
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[1] / 'assets'
S = 4096  # draw 4x, downsample to 1024
BG_TOP, BG_BOTTOM = (36, 36, 39), (14, 14, 15)  # --raise -> --bg
TEXT = (242, 242, 239)
GRADE = [(255, 90, 54), (255, 61, 127), (122, 92, 255)]  # --accent -> magenta -> violet


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def vertical(size, stops):
    w, h = size
    strip = Image.new('RGB', (1, h))
    for y in range(h):
        t = y / (h - 1) * (len(stops) - 1)
        i = min(int(t), len(stops) - 2)
        strip.putpixel((0, y), lerp(stops[i], stops[i + 1], t - i))
    return strip.resize((w, h))


def draw():
    canvas = Image.new('RGBA', (S, S))
    # macOS Big Sur grid: 824/1024 body, centred, soft drop shadow.
    pad, radius = S * 100 // 1024, S * 185 // 1024
    body = [pad, pad, S - pad, S - pad]
    shadow = Image.new('L', (S, S))
    ImageDraw.Draw(shadow).rounded_rectangle([body[0], body[1] + S // 80, body[2], body[3] + S // 80], radius, fill=110)
    canvas.paste((0, 0, 0, 255), mask=shadow.filter(ImageFilter.GaussianBlur(S // 60)))
    squircle = Image.new('L', (S, S))
    ImageDraw.Draw(squircle).rounded_rectangle(body, radius, fill=255)
    canvas.paste(vertical((S, S), [BG_TOP, BG_BOTTOM]), mask=squircle)

    # The mark: left half = original (paper white), right half = graded colour.
    r = S * 270 // 1024
    c = S // 2
    disc = Image.new('L', (S, S))
    ImageDraw.Draw(disc).ellipse([c - r, c - r, c + r, c + r], fill=255)
    left, right = Image.new('L', (S, S)), Image.new('L', (S, S))
    ImageDraw.Draw(left).rectangle([0, 0, c, S], fill=255)
    ImageDraw.Draw(right).rectangle([c, 0, S, S], fill=255)
    canvas.paste(TEXT, mask=ImageChops.multiply(disc, left))
    grade = Image.new('RGB', (S, S), GRADE[-1])
    grade.paste(vertical((S, 2 * r), GRADE), (0, c - r))  # stops span the disc, not the canvas
    canvas.paste(grade, mask=ImageChops.multiply(disc, right))
    # Split line like the Lab's comparison divider.
    w = S // 256
    ImageDraw.Draw(canvas).rectangle([c - w // 2, c - r - S // 22, c + w // 2, c + r + S // 22], fill=TEXT)
    return canvas.resize((1024, 1024), Image.LANCZOS)


def main():
    OUT.mkdir(exist_ok=True)
    icon = draw()
    icon.save(OUT / 'icon.png')
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / 'lut_lab.iconset'
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            icon.resize((size, size), Image.LANCZOS).save(iconset / f'icon_{size}x{size}.png')
            icon.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f'icon_{size}x{size}@2x.png')
        subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(OUT / 'icon.icns')], check=True)
    print(OUT / 'icon.icns')


if __name__ == '__main__':
    main()
