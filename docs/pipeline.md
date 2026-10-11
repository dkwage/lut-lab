# LUT Lab pipeline

## JPG / cube

1. Pillow reads the image, applies EXIF orientation, and converts it to RGB.
2. A validated `.cube` filter applies to the full-resolution image; resizing happens after filtering.
3. With rr-lut, supported adjustments are rendered to a 33³ cube. Compose mode applies adjustments after the base cube. Preset mode uses the source preset's adjustments without applying the base cube twice.
4. Strength uses `identity + amount × (result − identity)` and clips to `[0, 1]`. Supported amounts are 0–2. Unchanged compose mode at 100% preserves the original cube bytes.
5. Save writes the cube and a `.recipe.json` sidecar. Export to the optional install folder writes only the cube. File publication is atomic and failed cube saves restore the previous sidecar.

Without rr-lut, plain cubes, full-image previews, strength, save and export remain available. Source preset adjustments are not reconstructed; a saved cube is used directly. Nonempty adjustments require the engine. JPG spatial effects such as grain, sharpening and local masks cannot be represented accurately by a color-only cube; use RAW mode for those effects.

The cube reader validates size, row count, finite values and domain. Cubes whose domain differs from `[0, 1]`, or whose size exceeds Pillow's 65³ limit, are resampled to 33³. Cube caches include file modification stamps. Photo caches include file stamps so replacing a file invalidates the preview.

## Generic source preset index

Any LUT directory, including nested categories, may contain `index.json`:

```json
[
  {
    "output": "looks/Colour Study.cube",
    "source": "presets/Colour Study.xmp",
    "status": "ok",
    "ignored": ["GrainAmount"],
    "ignored_reasons": {"GrainAmount": "Spatial effect"},
    "badges": ["Grain"]
  }
]
```

Paths may be relative to the index's directory or its parent. Output must match the selected cube; source and index files must resolve inside allowed LUT roots, including after resolving symlinks. `status` may be omitted; failed records are not used to restore presets. `ignored`, `ignored_reasons`, and `badges` are optional UI metadata.

In JPG mode, a matched source preset is converted through `rr-lut preset` when the engine is available. Shadow/highlight grading aliases and signed temperature offsets are normalized in a temporary XMP; source files are unchanged. Monochrome presets retain compose mode. RAW mode sends the source XMP or template to LightCraft. Without an index, RAW mode imports cubes as creative profiles.

Saved recipe files record `version: 1`, `adjustments`, `amount`, `mode` and `base_lut`. Base references are local absolute paths; recipes are user data rather than portable preset bundles. A copied CLI command persists the current recipe in the custom folder:

```sh
lut-lab bake /path/to/look.recipe.json -o /path/to/look.cube
python -m lutlab.lut info /path/to/look.cube
python -m lutlab.lut tweak /path/to/look.cube --strength 0.7 -o /path/to/soft.cube
```

## RAW / XMP

RAW uploads and thumbnails use rawpy/LibRaw. Full editing uses the optional LightCraft MCP process. Each opened image has a serialized engine session with a temporary configuration directory, library and image link (or copy). Original previews are cached; sequence numbers reject stale renders. Failed or timed-out processes are stopped and restarted with the previous settings on the next request.

RAW controls come from the engine's control specification. Basic tone, curves, mixer, grading, effects, detail and calibration are validated before dispatch. Previews use 1024 pixels during dragging and 1600 after release.

XMP export keeps only supported development settings and removes photo metadata, rating, GPS and unrelated namespaces. Exporting a cube profile embeds the cube content so the XMP can restore the profile after restart. Saves go to `<data>/custom`; explicit installs go to the same `install_dir` as cubes. No preset or sidecar is written beside the source photo.

## Storage and boundaries

Default photo roots are empty. Import copies to `<data>/uploads`; `--photo-dir` grants read access to additional photos. LUT roots are `<data>/luts`, extra `--lut-dir` paths, custom outputs and an explicitly configured install folder. Path traversal, symlink escapes, invalid output names and unapproved cross-origin writes are rejected. The server binds only to localhost.

Engine resolution is CLI → environment → frozen app bundle → PATH. LightCraft always receives a temporary `LIGHTCRAFT_CONFIG_DIR`. rr-lut stays external to app bundles. Tests synthesize images at runtime and use temporary destinations; real RAW tests require an explicit `LUT_LAB_RAW_SAMPLE`.
