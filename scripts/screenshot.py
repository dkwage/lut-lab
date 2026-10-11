"""Capture the actual UI using a synthetic chart and a local, offline API fixture."""
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import sys


def fixture_html():
    root = Path(__file__).resolve().parents[1]
    y, x = np.mgrid[0:600, 0:900]
    pixels = np.stack((x / 899, y / 599, (np.sin(x / 70) + 1) / 2), axis=-1)
    images = []
    for rgb in (pixels, pixels ** 1.25):
        stream = BytesIO()
        Image.fromarray(np.rint(rgb * 255).astype('uint8')).save(stream, 'JPEG', quality=95)
        images.append('data:image/jpeg;base64,' + base64.b64encode(stream.getvalue()).decode())
    payload = {'config': {'available': False, 'raw': {'available': False}, 'custom': 'data/custom', 'install': None},
               'photos': [{'path': 'chart.jpg', 'name': 'Synthetic colour chart', 'kind': 'image', 'thumbnail': images[0]}],
               'luts': [{'path': 'colour.cube', 'name': 'Colour Study', 'kind': 'cube', 'category': 'Synthetic',
                         'source': 'data/luts', 'ignored': [], 'badges': []}],
               'recipe': {'version': 1, 'adjustments': {}, 'amount': 1, 'mode': 'compose', 'base_lut': 'colour.cube', 'note': '기존 큐브 위에 보정합니다.'},
               'info': {'size': 33, 'identity_mean_abs': .06}}
    bootstrap = '''<script>
const fixture=PAYLOAD, images=IMAGES, originalFetch=window.fetch;
window.fetch=async(input,options={})=>{
  const route=String(input).replace('/api/','').split('?')[0];
  if(route==='render')return originalFetch(images[JSON.parse(options.body).lut?1:0]);
  return Response.json(fixture[route]||{});
};
window.addEventListener('load',()=>setTimeout(()=>selectLut(luts[0]),200));
</script>'''.replace('PAYLOAD', json.dumps(payload, ensure_ascii=False)).replace('IMAGES', json.dumps(images))
    html = (root / 'lutlab/static/index.html').read_text().replace('<script>', bootstrap + '<script>', 1)
    for font in (root / 'lutlab/static/fonts').glob('*.woff2'):
        html = html.replace('/fonts/' + font.name, 'data:font/woff2;base64,' + base64.b64encode(font.read_bytes()).decode())
    return html


def main():
    root = Path(__file__).resolve().parents[1]
    if '--chart-only' in sys.argv:
        # This explicitly labelled comparison is a fallback artifact, not a browser capture.
        from lutlab import lut
        y, x = np.mgrid[0:540, 0:720]
        rgb = np.stack((x / 719, y / 539, (np.sin(x / 56) + 1) / 2), axis=-1)
        cube = (lut.identity() ** 1.25, np.zeros(3), np.ones(3), 33)
        original = Image.fromarray(np.rint(rgb * 255).astype('uint8'))
        graded = Image.fromarray(np.rint(lut.sample(cube, rgb) * 255).astype('uint8'))
        canvas = Image.new('RGB', (1600, 800), '#171719')
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default(size=30)
        draw.text((50, 32), 'LUT Lab | Synthetic colour chart', fill='#f2f2ef', font=font)
        draw.text((50, 100), 'Original', fill='#f2f2ef', font=font)
        draw.text((830, 100), 'Cube applied | 100%', fill='#f2f2ef', font=font)
        canvas.paste(original, (50, 160)); canvas.paste(graded, (830, 160))
        draw.text((50, 745), 'Generated comparison - browser capture unavailable', fill='#a0a0a4', font=ImageFont.load_default(size=22))
        canvas.save(root / 'docs/screenshot.png')
        assert canvas.size == (1600, 800) and original.tobytes() != graded.tobytes()
        return
    with tempfile.TemporaryDirectory(prefix='lut-lab-screenshot-') as directory:
        temp = Path(directory)
        page = temp / 'index.html'
        page.write_text(fixture_html())
        chrome = os.environ.get('CHROME', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        subprocess.run([chrome, '--headless=new', '--no-first-run', '--no-default-browser-check',
                        '--user-data-dir=' + str(temp / 'profile'), '--window-size=1600,1000',
                        '--hide-scrollbars', '--virtual-time-budget=3000',
                        '--screenshot=' + str(root / 'docs/screenshot.png'), page.as_uri()], check=True, timeout=30)
        assert (root / 'docs/screenshot.png').is_file()


if __name__ == '__main__':
    main()
