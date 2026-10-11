"""Standalone defaults, optional engines, index mappings and vendored cache checks."""
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image
from lutlab import imaging, lut, server as lab


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        data, extra, photos, install = [root / name for name in ('data', 'extra', 'photos', 'install')]
        for folder in (data / 'luts', extra, photos):
            folder.mkdir(parents=True)
        image = Image.new('RGB', (20, 12), (100, 150, 200))
        stream = BytesIO(); image.save(stream, 'PNG')
        photo = photos / 'private.png'; photo.write_bytes(stream.getvalue())
        cube = data / 'luts/look.cube'; lut.write_cube(cube, lut.identity() ** 1.25)
        other = extra / 'extra.cube'; lut.write_cube(other, lut.identity())
        app = lab.create_app([extra], data_dir=data, rr_lut=root / 'missing', lightcraft=root / 'missing')
        with TestClient(app, base_url='http://127.0.0.1') as client:
            config = client.get('/api/config').json()
            assert not config['available'] and not config['raw']['available'] and config['install'] is None
            assert config['custom'] == str(data / 'custom') and config['uploads'] == str(data / 'uploads')
            assert client.get('/api/photos').json() == []
            assert {r['path'] for r in client.get('/api/luts').json()} == {str(cube), str(other)}
            assert client.post('/api/render', json={'photo': str(photo)}).status_code == 403
            uploaded = client.post('/api/upload', params={'name': 'chart.png'}, content=stream.getvalue()).json()['path']
            assert Path(uploaded).parent == data / 'uploads'
            for amount in (0, .5, 1, 2):
                body = {'photo': uploaded, 'lut': str(cube), 'recipe': {'amount': amount}}
                rendered = client.post('/api/render', json=body)
                assert rendered.status_code == 200
                saved = client.post('/api/save', json={k: v for k, v in {**body, 'name': f'amount-{amount}'}.items() if k != 'photo'})
                assert saved.status_code == 200, saved.text
                expected = np.clip(lut.identity() + amount * (lut.read_cube(cube)[0] - lut.identity()), 0, 1)
                assert np.max(np.abs(lut.read_cube(saved.json()['path'])[0] - expected)) < 1e-8
            assert client.post('/api/install', json={'name': 'look'}).status_code == 403
            assert client.post('/api/raw/install', json={'name': 'look'}).status_code == 403
            assert client.post('/api/render', json={**body, 'recipe': {'adjustments': {'exposure': 1}}}).status_code == 503
            # Missing adjustment engines use the already baked cube even with a recipe sidecar.
            cube.with_suffix('.recipe.json').write_text(lab.SavedRecipe(mode='preset', base_lut=str(other), adjustments={'exposure': 1}).model_dump_json())
            recipe = client.get('/api/recipe', params={'lut': str(cube)}).json()
            assert recipe['base_lut'] == str(cube) and recipe['adjustments'] == {} and recipe['mode'] == 'compose'
            with patch.object(lab, 'MAX_UPLOAD', 3):
                assert client.post('/api/upload', params={'name': 'bad.jpg'}, content=b'1234').status_code == 413
        (data / 'config.json').write_text(json.dumps({'install_dir': str(install)}))
        app = lab.create_app(data_dir=data, rr_lut=root / 'missing', lightcraft=root / 'missing')
        with TestClient(app, base_url='http://127.0.0.1') as client:
            assert client.get('/api/config').json()['install'] == str(install)
            assert client.post('/api/install', json={'name': 'neutral'}).status_code == 200
            assert (install / 'neutral.cube').is_file()
        override = lab.create_app(data_dir=data, install=root / 'override')
        with TestClient(override, base_url='http://127.0.0.1') as client:
            assert client.get('/api/config').json()['install'] == str(root / 'override')
        # Generic indexes work in nested directories, with either supported relative base.
        nested = extra / 'category'; nested.mkdir()
        mapped = nested / 'mapped.cube'; lut.write_cube(mapped, lut.identity())
        source = extra / 'source.xmp'
        source.write_text('<xmp xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" crs:Temperature="+400" crs:ColorGradeShadowHue="200" crs:ColorGradeShadowSat="8"/>')
        records = [{'output': 'category/mapped.cube', 'source': 'source.xmp'}]
        (nested / 'index.json').write_text(json.dumps(records))
        assert lab.indexed_source(mapped, [extra]) == source
        ns = '{http://ns.adobe.com/camera-raw-settings/1.0/}'
        def preset(binary, command, normalized):
            assert command == 'preset'
            tree = ET.fromstring(normalized.read_bytes())
            assert tree.get(ns + 'Temperature') == '5900'
            assert tree.get(ns + 'SplitToningShadowHue') == '200'
            assert tree.get(ns + 'SplitToningShadowSaturation') == '8'
            return '{"exposure": 0.2, "grainAmount": 0}'
        with patch.object(lab, 'rr_run', side_effect=preset):
            saved, _ = lab.initial_recipe(mapped, root / 'fake', [extra])
            assert saved.mode == 'preset' and saved.adjustments == {'exposure': .2}
        source.write_text('<xmp xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" crs:ConvertToGrayscale="True"/>')
        assert lab.initial_recipe(mapped, root / 'fake', [extra])[0].mode == 'compose'
        source.unlink(); source.symlink_to(photo)
        try:
            lab.indexed_source(mapped, [extra])
            assert False, 'source symlink escape accepted'
        except lab.HTTPException as error:
            assert error.status_code == 403
        # Cache replacement and input domain handling in the minimal image module.
        first = imaging.load_cube(other)
        lut.write_cube(other, lut.identity() * .5)
        os.utime(other, ns=(other.stat().st_atime_ns, other.stat().st_mtime_ns + 1_000_000))
        assert imaging.load_cube(other).table != first.table
        domain = extra / 'domain.cube'
        domain.write_text('LUT_3D_SIZE 2\nDOMAIN_MIN -0.5 -0.5 -0.5\nDOMAIN_MAX 1.5 1.5 1.5\n' + '\n'.join(' '.join(map(str, row)) for row in lut.identity(2) * 2 - .5))
        assert image.filter(imaging.load_cube(domain)).tobytes() == image.tobytes()
        assert '.dng' in imaging.RAW_EXTS
        with patch.object(Path, 'home', return_value=root), patch.object(lab.sys, 'platform', 'linux'):
            assert lab.app_config_dir() == root / '.lut-lab'
        args = lab.argument_parser().parse_args(['--data-dir', str(data), '--install-dir', str(install), '--photo-dir', str(photos)])
        with TestClient(lab.app_from_args(args), base_url='http://127.0.0.1') as client:
            assert len(client.get('/api/photos').json()) == 2
    print('PASS: standalone paths/config, default privacy, import, optional engines, cube strength/save, install gating, index/XMP normalization/containment, domain and cache invalidation')


if __name__ == '__main__':
    main()
