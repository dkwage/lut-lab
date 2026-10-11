"""Minimal image development, cube loading and previews."""
from functools import lru_cache, wraps
from pathlib import Path

import numpy as np
import rawpy
from PIL import Image, ImageFilter, ImageOps
from lutlab import lut


RAW_EXTS = {".arw", ".cr2", ".cr3", ".nef", ".raf", ".dng", ".rw2", ".orf", ".pef"}



def _read_cube(path: Path) -> ImageFilter.Color3DLUT:
    """Validate a cube, respecting its domain and Pillow's size limit."""
    table, lo, hi, size = lut.read_cube(path)
    if size > 65 or not (np.array_equal(lo, np.zeros(3)) and np.array_equal(hi, np.ones(3))):
        table = lut.sample((table, lo, hi, size), lut.identity())
        size = 33
    return ImageFilter.Color3DLUT(size, table.ravel().tolist())


@lru_cache(maxsize=32)
def _cached_cube(path: Path, mtime_ns: int, size: int) -> ImageFilter.Color3DLUT:
    return _read_cube(path)


@wraps(_read_cube)
def load_cube(path: Path) -> ImageFilter.Color3DLUT:
    path = Path(path)
    stamp = path.stat()
    return _cached_cube(path, stamp.st_mtime_ns, stamp.st_size)


def develop(path: Path) -> Image.Image:
    if path.suffix.lower() in RAW_EXTS:
        with rawpy.imread(str(path)) as raw:
            return Image.fromarray(raw.postprocess(use_camera_wb=True, output_bps=8))
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


@lru_cache(maxsize=64)
def preview(path: Path, width: int = 600) -> Image.Image:
    """Small cached copy for instant filter switching. Callers must not mutate it."""
    img = develop(path)
    img.thumbnail((width, width * 3))
    return img

