#!/usr/bin/env python3
"""Deterministic stdio engine for Lab tests; no RAW decoder or GPU needed."""
import copy
import json
import os
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET

SPECS = []
for key, low, high, default in [('wb.temp', 2000, 50000, 6500), ('wb.tint', -150, 150, 0),
        ('light.exposure', -5, 5, 0), ('light.contrast', -100, 100, 0),
        ('effects.clarity', -100, 100, 0), ('effects.texture', -100, 100, 0), ('effects.dehaze', -100, 100, 0),
        ('detail.sharpenAmount', 0, 150, 0), ('detail.sharpenRadius', .5, 3, 1),
        ('detail.sharpenDetail', 0, 100, 25), ('detail.sharpenMasking', 0, 100, 0),
        ('detail.nrLuminance', 0, 100, 0), ('detail.nrDetail', 0, 100, 50),
        ('detail.nrContrast', 0, 100, 0), ('detail.nrColor', 0, 100, 25),
        ('detail.nrColorDetail', 0, 100, 50), ('detail.nrColorSmoothness', 0, 100, 50),
        ('profile.amount', 0, 200, 100), ('curve.shadows', -100, 100, 0),
        ('grain.amount', 0, 100, 0), ('grain.size', 0, 100, 25), ('grain.roughness', 0, 100, 50),
        ('vignette.amount', -100, 100, 0), ('vignette.midpoint', 0, 100, 50),
        ('vignette.roundness', -100, 100, 0), ('vignette.feather', 0, 100, 50), ('vignette.highlights', 0, 100, 0)]:
    SPECS.append(dict(id=key, label=key, min=low, max=high, default=default, value=default,
                      step=.01 if key == 'light.exposure' else 1, decimals=2, section=key.split('.')[0]))
for group, names in [('mixer', ['red', 'orange', 'yellow', 'green', 'aqua', 'blue', 'purple', 'magenta']),
                     ('grading', ['shadows', 'midtones', 'highlights', 'global'])]:
    for name in names:
        for metric in ['hue', 'sat', 'lum']:
            key = f'{group}.{name}.{metric}'
            SPECS.append(dict(id=key, label=key, min=0 if group == 'grading' and metric != 'lum' else -100,
                              max=360 if group == 'grading' and metric == 'hue' else 100,
                              default=0, value=0, step=1, decimals=0, section=group))
DEFAULT = {'profile': {'id': 'lc.color', 'amount': 100}, 'curve': {}, 'wb': {'mode': 'asShot'},
           'crop': {'private': True}, 'geometry': {'private': True}, 'masks': [{'private': True}],
           'spots': [1], 'red_eye': [1], 'orientation': 'Rotate90', 'lens_blur': {'enabled': True},
           'optics': {'distortion': 20}, 'enhance': {'raw_details': True}, 'version': 1, 'process': 1}

def put(settings, key, value):
    node = settings
    for part in key.split('.')[:-1]:
        node = node.setdefault(part, {})
    node[key.split('.')[-1]] = value

for spec in SPECS:
    put(DEFAULT, spec['id'], spec['default'])
# Generate a synthetic image at runtime, with no embedded image or metadata.
from io import BytesIO
import base64
from PIL import Image
image = Image.new('RGB', (16, 12), (56, 112, 168))
stream = BytesIO()
image.save(stream, 'JPEG')
JPEG = base64.b64encode(stream.getvalue()).decode('ascii')


def main():
    assert os.environ.get('LIGHTCRAFT_CONFIG_DIR'), 'configuration must be isolated'
    if '--fixture' in sys.argv:
        print(json.dumps({'controls': SPECS, 'settings': DEFAULT, 'session': 'fixture', 'jpeg': JPEG}))
        return
    assert os.environ.get('LIGHTCRAFT_CONFIG_DIR'), 'configuration must be isolated'
    if '--version' in sys.argv:
        print('fake-lightcraft 1')
        return
    if 'controls' in sys.argv:
        print(json.dumps(SPECS))
        return
    photo = Path(sys.argv[-1])
    settings = copy.deepcopy(DEFAULT)
    for line in sys.stdin:
        req = json.loads(line)
        if 'id' not in req:
            continue
        result = {}
        if req['method'] == 'initialize':
            result = {'protocolVersion': '2025-06-18'}
        else:
            params = req['params']; name = params['name']; args = params.get('arguments', {})
            if name == 'test_crash':
                os._exit(3)
            if name == 'test_timeout':
                time.sleep(60)
            if name == 'test_error':
                result = {'isError': True, 'content': [{'type': 'text', 'text': 'bad control'}]}
            elif name == 'render_preview':
                result = {'content': [{'type': 'image', 'mimeType': 'image/jpeg', 'data': JPEG}]}
            else:
                data = {}
                if name == 'set_develop':
                    settings.update(args.get('settings', {}))
                    for key, value in args.get('values', {}).items():
                        put(settings, key, value)
                elif name == 'list_controls':
                    data = copy.deepcopy(SPECS)
                    for spec in data:
                        node = settings
                        for key in spec['id'].split('.'):
                            node = node[key]
                        spec['value'] = node
                elif name == 'command_run':
                    cmd = args['id']; p = args.get('params', {})
                    if cmd == 'develop.get':
                        data = settings
                    elif cmd == 'develop.reset':
                        settings = copy.deepcopy(DEFAULT)
                    elif cmd == 'develop.curve':
                        settings['curve'][p['channel']] = p['points']
                    elif cmd in ('preset.import', 'profile.import'):
                        data = {'imported': [{'id': 'lut:test' if cmd == 'profile.import' else 'test-preset', 'unmapped': ['CameraProfile']}]}
                    elif cmd == 'preset.apply':
                        settings['light']['exposure'] = .8
                    elif cmd == 'develop.profile':
                        settings['profile'] = p
                    elif cmd == 'photo.saveMetadataToFile':
                        root = ET.Element('xmp', {'xmlns:lc': 'http://ns.lightcraft.app/lc/1.0/',
                            'xmlns:exif': 'http://ns.adobe.com/exif/1.0/', 'xmlns:xmp': 'http://ns.adobe.com/xap/1.0/',
                            'xmlns:dc': 'http://purl.org/dc/elements/1.1/', 'xmp:Rating': '5', 'exif:GPSLatitude': 'secret'})
                        ET.SubElement(root, 'dc:title').text = 'private title'
                        ET.SubElement(root, 'lc:settings').text = json.dumps(settings)
                        photo.with_suffix('.xmp').write_bytes(ET.tostring(root))
                result = {'content': [{'type': 'text', 'text': json.dumps(data)}]}
        print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}), flush=True)


if __name__ == '__main__':
    main()
