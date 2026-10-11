"""Start isolated servers on free ports; test Chrome with synthetic assets only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image
from lutlab import lut, server
from lutlab.app import running_server


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='lut-lab-ui-') as directory:
        data = Path(directory)
        photos = data / 'photos'
        cubes = data / 'luts'
        photos.mkdir(); cubes.mkdir()
        # Smooth hue/saturation chart and grayscale row, generated without source photos.
        import colorsys
        pixels = np.array([[tuple(round(v * 255) for v in colorsys.hsv_to_rgb(x / 960, .3 + .7 * y / 540, .9))
                            for x in range(960)] for y in range(540)], dtype=np.uint8)
        pixels[460:] = np.linspace(0, 255, 960, dtype=np.uint8)[None, :, None]
        Image.fromarray(pixels).save(photos / 'chart.jpg', quality=95)
        records = []
        for i, name in enumerate(['Colour Study', 'Mono Study', *[f'Study {i:02}' for i in range(20)]]):
            cube = cubes / (name + '.cube')
            lut.write_cube(cube, lut.identity() ** (1 + i / 100))
            xmp = cubes / (name + '.xmp')
            mono = 'True' if name == 'Mono Study' else 'False'
            xmp.write_text(f'<xmp xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" crs:ConvertToGrayscale="{mono}"/>')
            records.append({'output': cube.name, 'source': xmp.name, 'status': 'ok',
                            'badges': ['B&W approximation'] if mono == 'True' else [], 'ignored': []})
        (cubes / 'index.json').write_text(json.dumps(records))
        fake = data / 'rr-lut'
        fake.write_text('#!' + sys.executable + '\n' + (root / 'tests/fixtures/fake_rr.py').read_text())
        fake.chmod(0o755)
        for no_engine in (True, False):
            app = server.create_app(photo_dirs=[photos], data_dir=data,
                                    rr_lut=data / 'missing' if no_engine else fake,
                                    lightcraft=data / 'missing', install=None if no_engine else data / 'install')
            with running_server(app) as (url, _):
                subprocess.run(['node', str(root / 'tests/test_lut_lab_ui.cjs'), url,
                                *(['--no-engine'] if no_engine else [])],
                               env={**os.environ, 'PYTHON': sys.executable, 'LIGHTCRAFT_CONFIG_DIR': str(data / 'config')},
                               check=True, timeout=240)


if __name__ == '__main__':
    main()
