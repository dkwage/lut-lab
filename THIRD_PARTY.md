# Third-party notices

## Pretendard — OFL-1.1

The bundled WOFF2 fonts are Pretendard by Kil Hyung-jin, copyright 2021, with Reserved Font Name “Pretendard”. They are distributed under the SIL Open Font License 1.1. The original notice and full license are preserved in [lutlab/static/fonts/LICENSE](lutlab/static/fonts/LICENSE).

## LightCraft — MIT OR Apache-2.0

LightCraft is an optional external engine. Its source declares `MIT OR Apache-2.0`; the MIT notice credits the ArtCraft Team and LightCraft contributors, copyright 2026. The repository does not contain its binary. When `scripts/build_app.sh` finds and bundles lightcraft-cli, it also includes the unmodified `LICENSE-MIT` and `LICENSE-APACHE` files under the application's `licenses/lightcraft` directory. The script reads available license files from the binary's ancestors or the local source checkout. Place both files beside an independently supplied binary if needed. Missing notices cause the bundle build to stop.

## RapidRAW / rr-lut — AGPL

rr-lut is a separately installed headless engine derived from RapidRAW. RapidRAW is AGPL-3.0 licensed. LUT Lab communicates with it as an optional executable and imports no RapidRAW rendering code. The application build script never bundles rr-lut. Distribution of a separate engine must retain that engine's notices and satisfy its license, including applicable source obligations. The MIT license on LUT Lab does not relicense that engine.

## Python runtime dependencies

FastAPI, Uvicorn, NumPy, Pillow, rawpy and Pydantic retain their respective licenses, as do pywebview and PyInstaller in the optional app build group and the native libraries they use. Their notices travel with the installed distributions; LUT Lab's license applies to its own code. Review the complete bundled dependency notices when distributing a built app.
