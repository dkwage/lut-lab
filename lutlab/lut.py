#!/usr/bin/env python3
"""Inspect, tweak, and preview .cube files; numpy + Pillow only.

sRGB input/output, red changes fastest. Tweak resamples to 33^3 with trilinear
interpolation, respecting the input domain. Post-ops run in this order:
* Decode sRGB, multiply by 2**EV and RGB gains 2**[t,-q,-t], encode sRGB.
  t=temp/200, q=tint/200: positive temperature warms, positive tint is magenta.
* Contrast: scale about 0.5 by 2**(contrast/100).
* Saturation: scale distance from Rec.709 weighted sRGB grey by 1+sat/100.
* Vibrance: same, weighted by 1-(max-min), favoring muted colors.
* Fade: lift black toward 0.25, preserving white, by fade/100.
Clip post-ops to [0,1], then apply identity + strength * (rgb - identity).
Strength is 0-2 (default 1); values above 1 exaggerate the LUT change.
Clip the final result to [0,1] after applying strength.
These are simple creative controls, not Lightroom or calibrated white balance.
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps


def identity(size=33):
    b, g, r = np.meshgrid(*([np.linspace(0, 1, size)] * 3), indexing="ij")
    return np.stack((r, g, b), axis=-1).reshape(-1, 3)


def read_cube(path):
    size, rows = None, []
    lo, hi = np.zeros(3), np.ones(3)
    seen = set()
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        parts = line.split("#", 1)[0].split()
        if not parts or parts[0] == "TITLE":
            continue
        key = parts[0]
        try:
            if key in ("LUT_3D_SIZE", "DOMAIN_MIN", "DOMAIN_MAX"):
                if key in seen:
                    raise ValueError(f"duplicate {key}")
                seen.add(key)
                if key == "LUT_3D_SIZE":
                    if len(parts) != 2:
                        raise ValueError("size needs one integer")
                    size = int(parts[1])
                    if not 2 <= size <= 256:
                        raise ValueError("size must be 2..256")
                else:
                    if len(parts) != 4:
                        raise ValueError("domain needs three values")
                    values = np.array([float(v) for v in parts[1:]])
                    if key == "DOMAIN_MIN":
                        lo = values
                    else:
                        hi = values
            else:
                if len(parts) != 3:
                    raise ValueError("expected RGB triplet (only 3D cubes supported)")
                rows.append([float(v) for v in parts])
        except ValueError as e:
            raise ValueError(f"{path}:{number}: {e}") from e
    table = np.array(rows, dtype=float)
    if size is None or len(rows) != size ** 3:
        raise ValueError(f"{path}: expected size^3 RGB rows, got {len(rows)} (size={size})")
    if not np.isfinite(table).all() or not np.isfinite([lo, hi]).all() or np.any(hi <= lo):
        raise ValueError(f"{path}: non-finite values or invalid domain")
    return table, lo, hi, size


def sample(cube, rgb):
    table, lo, hi, size = cube
    xyz = np.clip((rgb - lo) / (hi - lo), 0, 1) * (size - 1)
    lower = np.floor(xyz).astype(int)
    upper = np.minimum(lower + 1, size - 1)
    fraction = xyz - lower
    result = np.zeros_like(rgb, dtype=float)
    for b in (0, 1):
        for g in (0, 1):
            for r in (0, 1):
                choice = np.array([r, g, b])
                coords = np.where(choice, upper, lower)
                weight = np.prod(np.where(choice, fraction, 1 - fraction), axis=-1)
                index = coords[..., 0] + size * (coords[..., 1] + size * coords[..., 2])
                result += table[index] * weight[..., None]
    return result


def post_ops(rgb, *, strength=1, exposure=0, contrast=0, saturation=0,
             vibrance=0, temp=0, tint=0, fade=0):
    rgb = rgb.copy()
    if exposure or temp or tint:
        # Extended transfer function accepts LUT values below zero without NaNs.
        linear = np.where(rgb <= .04045, rgb / 12.92,
                          np.maximum((rgb + .055) / 1.055, 0) ** 2.4)
        linear *= np.exp2([exposure + temp / 200, exposure - tint / 200,
                          exposure - temp / 200])
        rgb = np.where(linear <= .0031308, 12.92 * linear,
                       1.055 * np.maximum(linear, 0) ** (1 / 2.4) - .055)
    if contrast:
        rgb = (rgb - .5) * 2 ** (contrast / 100) + .5
    if saturation:
        grey = rgb @ [.2126, .7152, .0722]
        rgb = grey[:, None] + (rgb - grey[:, None]) * (1 + saturation / 100)
    if vibrance:
        grey = rgb @ [.2126, .7152, .0722]
        muted = 1 - np.clip(np.ptp(rgb, axis=-1), 0, 1)
        rgb = grey[:, None] + (rgb - grey[:, None]) * (1 + vibrance / 100 * muted[:, None])
    if fade:
        rgb = rgb + (1 - rgb) * .25 * fade / 100
    if any((exposure, contrast, saturation, vibrance, temp, tint, fade)):
        rgb = np.clip(rgb, 0, 1)
    neutral = identity()
    return np.clip(neutral + strength * (rgb - neutral), 0, 1)


def write_cube(path, table):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as out:
        out.write("LUT_3D_SIZE 33\nDOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        np.savetxt(out, table, fmt="%.9f")


def tweak_table(cube, **options):
    """Shared CLI/Lab resampling and creative controls."""
    return post_ops(sample(cube, identity()), **options)


def cube_info(cube):
    table, lo, hi, size = cube
    error = np.abs(table - (lo + identity(size) * (hi - lo)))
    return {"size": size, "domain_min": lo.tolist(), "domain_max": hi.tolist(),
            "value_min": table.min(axis=0).tolist(), "value_max": table.max(axis=0).tolist(),
            "identity_max_abs": float(error.max()), "identity_mean_abs": float(error.mean())}


def bounded(low, high):
    def parse(text):
        value = float(text)
        if not math.isfinite(value) or not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be finite and in {low}..{high}")
        return value
    return parse


def preview(paths, images, output):
    cubes = [read_cube(p) for p in paths]
    width, height, label = 360, 270, 48
    grid = Image.new("RGB", ((len(paths) + 1) * width, len(images) * (height + label)), "#202020")
    draw = ImageDraw.Draw(grid)
    font = ImageFont.load_default(size=15)
    for row, path in enumerate(images):
        with Image.open(path) as source:
            original = ImageOps.exif_transpose(source).convert("RGB")
        original.thumbnail((width, height), Image.Resampling.LANCZOS)
        rgb = np.asarray(original, dtype=float) / 255
        for col, cube in enumerate([None, *cubes]):
            im = original if cube is None else Image.fromarray(np.rint(np.clip(sample(cube, rgb), 0, 1) * 255).astype(np.uint8))
            x, y = col * width, row * (height + label)
            grid.paste(im, (x + (width - im.width) // 2, y + (height - im.height) // 2))
            font = ImageFont.load_default(size=15)
            title = "Original" if col == 0 else Path(paths[col - 1]).stem
            # Wrap long names to the available width, retaining the full label.
            while draw.textlength(title, font=font) > width - 12 and len(title) > 1:
                font = ImageFont.load_default(size=max(6, font.size - 1))
                if font.size == 6:
                    break
            draw.text((x + 6, y + height + 4), title, font=font, fill="white")
            draw.text((x + 6, y + height + 25), f"Image {row + 1}", fill="#bbbbbb")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    grid.save(output, format="JPEG", quality=92)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    info = commands.add_parser("info")
    info.add_argument("input", type=Path)
    tweak = commands.add_parser("tweak")
    tweak.add_argument("input", type=Path)
    tweak.add_argument("-o", "--output", type=Path, required=True)
    tweak.add_argument("--strength", type=bounded(0, 2), default=1,
                       help="0-2 (1 = full LUT, >1 exaggerates)")
    # Finite +/-100 EV is already far beyond useful photographic adjustments.
    tweak.add_argument("--exposure", type=bounded(-100, 100), default=0)
    for name in ("contrast", "saturation", "vibrance", "temp", "tint"):
        tweak.add_argument(f"--{name}", type=bounded(-100, 100), default=0)
    tweak.add_argument("--fade", type=bounded(0, 100), default=0)
    prev = commands.add_parser("preview")
    prev.add_argument("luts", type=Path, nargs="+")
    prev.add_argument("--images", type=Path, nargs="+", required=True)
    prev.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "info":
            print(json.dumps(cube_info(read_cube(args.input)), indent=2))
        elif args.command == "tweak":
            options = {k: v for k, v in vars(args).items() if k not in ("command", "input", "output")}
            write_cube(args.output, tweak_table(read_cube(args.input), **options))
        else:
            preview(args.luts, args.images, args.output)
    except (OSError, ValueError) as e:
        parser.exit(1, f"{e}\n")


if __name__ == "__main__":
    main()
