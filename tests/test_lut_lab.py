"""Run: uv run python tests/test_lut_lab.py"""
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import numpy as np
from PIL import Image
from fastapi.testclient import TestClient

from lutlab import lut
from lutlab import server as lab
from lutlab import imaging


def equal(a, b):
    assert a.mode == b.mode and a.size == b.size and a.tobytes() == b.tobytes()


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        photos, cubes, custom, uploads, install = [root / name for name in ('photos', 'luts', 'custom', 'uploads', 'install')]
        photos.mkdir()
        cubes.mkdir()
        photo, cube = photos / 'test.jpg', cubes / 'look.cube'
        # EXIF orientation and asymmetric color values exercise the actual develop path.
        pixels = np.random.default_rng(13).integers(0, 256, (57, 83, 3), dtype=np.uint8)
        exif = Image.Exif()
        exif[274] = 6
        Image.fromarray(pixels).save(photo, exif=exif)
        lut.write_cube(cube, lut.identity() ** 1.25)
        (cubes / 'index.json').write_text(json.dumps([{'output': 'look.cube', 'ignored': ['GrainAmount'],
                                                    'ignored_reasons': {'GrainAmount': 'Spatial feature'}}]))
        app = lab.create_app([cubes], [photos], data_dir=root, custom=custom, uploads=uploads, install=install)
        client = TestClient(app, base_url='http://127.0.0.1')
        payload = {'photo': str(photo), 'size': 0}
        developed = imaging.develop(photo)
        equal(lab.render_image(photo), developed)
        response = client.post('/api/render', json=payload)
        assert response.status_code == 200 and response.headers['content-type'] == 'image/jpeg'
        equal(Image.open(BytesIO(response.content)), Image.open(BytesIO(lab.jpeg(developed))))
        print('PASS: no-LUT pixels equal develop(photo), including EXIF orientation; JPEG endpoint matches')

        expected = developed.filter(imaging.load_cube(cube))
        equal(lab.render_image(photo, cube), expected)
        response = client.post('/api/render', json={**payload, 'lut': str(cube)})
        equal(Image.open(BytesIO(response.content)), Image.open(BytesIO(lab.jpeg(expected))))
        assert float(response.headers['x-render-ms']) >= 0
        equal(lab.render_image(photo), developed)  # No mutation of cached decoded pixels.
        print('PASS: cube pixels equal develop(photo).filter(load_cube(cube)) pixel-for-pixel')

        options = {'version': 1, 'adjustments': {'exposure': .4, 'contrast': 12,
                   'hsl': {'reds': {'hue': 15}}, 'colorGrading': {'shadows': {'hue': 200, 'saturation': 8}},
                   'curves': {'luma': [{'x': 0, 'y': 5}, {'x': 130, 'y': 140}, {'x': 255, 'y': 255}]}}, 'amount': .73}
        recipe = lab.Recipe(**options)
        output = root / 'cli.cube'
        save = {'lut': str(cube), 'mode': 'compose', 'name': 'My test', 'recipe': options}
        response = client.post('/api/save', json=save)
        assert response.status_code == 200, response.text
        saved = Path(response.json()['path'])
        preview_bytes = lab.bake(recipe, cube, 'compose')[0]
        assert saved.read_bytes() == preview_bytes
        restored = client.get('/api/recipe', params={'lut': str(saved)}).json()
        assert {key: restored[key] for key in options} == options
        assert restored['mode'] == 'compose' and restored['base_lut'] == str(cube)
        command = [sys.executable, '-m', 'lutlab.server', 'bake', str(saved.with_suffix('.recipe.json')), '-o', str(output)]
        subprocess.run(command, check=True)
        assert output.read_bytes() == preview_bytes
        assert subprocess.run(command, capture_output=True).returncode != 0
        expected = developed.filter(imaging.load_cube(output))
        equal(lab.render_image(photo, cube, recipe), expected)
        response = client.post('/api/render', json={**payload, **{k: save[k] for k in ('lut', 'mode', 'recipe')}})
        equal(Image.open(BytesIO(response.content)), Image.open(BytesIO(lab.jpeg(expected))))
        assert client.post('/api/save', json=save).status_code == 409
        assert client.post('/api/save', json={**save, 'overwrite': True}).status_code == 200
        assert client.post('/api/install', json=save).status_code == 200
        assert (install / 'My test.cube').read_bytes() == output.read_bytes()
        assert not (install / 'My test.recipe.json').exists()
        assert next(r for r in client.get('/api/luts').json() if r['path'] == str(saved))['has_recipe']
        print('PASS: nested recipe render pixels, preview/save/install/CLI bytes, sidecar round-trip, overwrite protection')

        fast_recipe = lab.Recipe(adjustments={'exposure': .27, 'colorCalibration': {'blueHue': 12}})
        fast_bytes, fast_filter = lab.bake(fast_recipe)
        fast_cube = root / 'fast.cube'
        fast_cube.write_bytes(fast_bytes)
        assert fast_filter.table == imaging.load_cube.__wrapped__(fast_cube).table
        assert lab.bake(fast_recipe, cube, 'preset')[0] == fast_bytes
        print('PASS: fixed-format parser/filter exactly matches imaging.load_cube; preset mode does not compose twice')
        neutral = lab.bake(lab.Recipe())[1]
        assert np.array_equal(np.array(neutral.table).reshape(-1, 3), lut.identity())
        assert lab.bake(lab.Recipe(), cube, 'compose')[0] == cube.read_bytes()
        assert np.array_equal(np.array(lab.bake(lab.Recipe(amount=0), cube, 'compose')[1].table).reshape(-1, 3), lut.identity())
        assert client.post('/api/render', json={**payload, 'recipe': {'amount': 2}}).status_code == 200
        print('PASS: neutral identity, compose neutral preserves exact base bytes, amount zero identity and amount two accepted')

        outside = root / 'secret.jpg'
        developed.save(outside)
        (photos / 'escape.jpg').symlink_to(outside)
        for path in [outside, photos / '../secret.jpg', photos / 'escape.jpg']:
            assert client.post('/api/render', json={**payload, 'photo': str(path)}).status_code == 403
            assert client.get('/api/thumbnail', params={'photo': str(path)}).status_code == 403
        for path in [str(root / 'cli.cube'), str(cubes / '../cli.cube')]:
            assert client.get('/api/info', params={'lut': path}).status_code == 403
            assert client.post('/api/render', json={**payload, 'lut': path}).status_code == 403
            assert client.post('/api/save', json={**save, 'lut': path}).status_code == 403
        for name in ['../escape', '/tmp/escape', 'a/b', 'a\\b', '..', '']:
            assert client.post('/api/save', json={**save, 'name': name}).status_code == 400
            assert client.post('/api/install', json={**save, 'name': name}).status_code == 400
        (custom / 'link.cube').symlink_to(output)
        assert client.post('/api/save', json={**save, 'name': 'link', 'overwrite': True}).status_code == 403
        assert client.post('/api/save', json=save, headers={'Origin': 'https://example.com'}).status_code == 403
        assert client.get('/api/photos', headers={'Host': 'evil.example'}).status_code == 400
        print('PASS: traversal, outside roots, symlink escapes, invalid output names, foreign origins and hosts rejected')

        for name in ('original', 'mono', 'warm'):
            target = install / (name + '.cube')
            target.write_bytes(cube.read_bytes())
            request = {**save, 'name': name}
            assert client.post('/api/install', json=request).status_code == 409
            assert target.read_bytes() == cube.read_bytes()
            assert client.post('/api/install', json={**request, 'overwrite': True}).status_code == 200
            assert target.read_bytes() == output.read_bytes()
        print('PASS: original, mono and warm cannot be replaced without overwrite=true')

        listing = client.get('/api/photos').json()
        assert len(listing) == 1 and listing[0]['path'] == str(photo)
        thumb = client.get(listing[0]['thumbnail'])
        assert max(Image.open(BytesIO(thumb.content)).size) <= 180
        listing = client.get('/api/luts').json()
        entry = next(item for item in listing if item['path'] == str(cube))
        assert entry['ignored'] == ['GrainAmount'] and entry['category'] == 'luts' and entry['source'] == str(cubes)
        assert client.get('/api/info', params={'lut': str(cube)}).json() == lut.cube_info(lut.read_cube(cube))
        response = client.post('/api/upload', params={'name': '../../test.jpg'}, content=photo.read_bytes())
        assert response.status_code == 200 and Path(response.json()['path']).parent == uploads
        assert client.post('/api/render', json={**payload, 'photo': response.json()['path']}).status_code == 200
        assert client.post('/api/upload', params={'name': 'bad.jpg'}, content=b'not an image').status_code == 400
        assert client.post('/api/upload', params={'name': 'bad.arw'}, content=b'not a RAW image').status_code == 400
        assert client.post('/api/upload', params={'name': 'bad.html'}, content=b'test').status_code == 400
        command = client.post('/api/command', json=save).json()
        assert 'bake' in command['command'] and 'My test.cube' in command['command']
        command_recipe = lab.SavedRecipe.model_validate_json(Path(command['recipe']).read_text())
        assert command_recipe.adjustments == recipe.adjustments
        for invalid in [{'amount': 2.01}, {'version': 2}, {'version': True}, {'adjustments': {'fade': 1}},
                        {'adjustments': {'temperature': 101}}, {'adjustments': {'exposure': 5.1}},
                        {'adjustments': {'hsl': {'red': {'hue': 0}}}},
                        {'adjustments': {'hsl': {'reds': {'hue': -101}}}},
                        {'adjustments': {'colorGrading': {'global': {'hue': 361}}}},
                        {'adjustments': {'colorCalibration': {'redHue': 101}}},
                        {'adjustments': {'curveMode': 'parametric'}},
                        {'adjustments': {'curves': {'luma': [{'x': 0, 'y': 0}]}}},
                        {'adjustments': {'curves': {'luma': [{'x': 1, 'y': 0}, {'x': 1, 'y': 255}]}}},
                        {'adjustments': {'curves': {'luma': [{'x': 0.5, 'y': 0}, {'x': 255, 'y': 255}]}}},
                        {'adjustments': {'curves': {'luma': [{'x': i, 'y': i} for i in range(17)]}}}]:
            assert client.post('/api/render', json={**payload, 'recipe': invalid}).status_code == 422, invalid
        # Exact fifteen exclusions asserted by headless/prepare.py, plus other spatial inputs.
        for key in ('showClipping', 'vignetteAmount', 'grainAmount', 'sharpness', 'clarity', 'dehaze',
                    'structure', 'centré', 'glowAmount', 'halationAmount', 'flareAmount', 'lumaNoiseReduction',
                    'colorNoiseReduction', 'chromaticAberrationRedCyan', 'chromaticAberrationBlueYellow',
                    'texture', 'crop', 'masks', 'lutPath'):
            assert client.post('/api/render', json={**payload, 'recipe': {'adjustments': {key: 1}}}).status_code == 422
        missing = TestClient(lab.create_app([cubes], [photos], data_dir=root, rr_lut=root / 'missing'), base_url='http://127.0.0.1')
        assert missing.post('/api/render', json=payload).status_code == 200
        assert missing.post('/api/render', json={**payload, 'recipe': options}).status_code == 503
        assert missing.get('/api/config').json()['available'] is False
        with patch.object(lab.subprocess, 'run', side_effect=subprocess.TimeoutExpired('rr-lut', 20)):
            try:
                lab.rr_run(lab.rr_binary(), 'adjustments')
            except lab.HTTPException as error:
                assert error.status_code == 503
            else:
                assert False, 'timeout must be 503'
        assert client.post('/api/render', json={**payload, 'size': -1}).status_code == 422
        print('PASS: discovery, metadata, info, thumbnails, uploads, CLI copy and input validation')

        # File edits must invalidate both pixel and cube caches.
        previous = lab.bake(recipe, cube, 'compose')[0]
        lut.write_cube(cube, lut.identity() * .5)
        assert lab.bake(recipe, cube, 'compose')[0] != previous
        equal(lab.render_image(photo, cube), imaging.develop(photo).filter(imaging.load_cube.__wrapped__(cube)))
        Image.new('RGB', (21, 18), '#1e395f').save(photo)
        equal(lab.render_image(photo), imaging.develop(photo))
        assert client.get('/').status_code == 200
        assert client.get('/fonts/Pretendard-Regular.woff2').status_code == 200
        print('PASS: photo/cube cache invalidation and offline page/font serving')
        (custom / 'bad.recipe.json').symlink_to(root / 'secret.json')
        (root / 'secret.json').write_text('{}')
        assert client.post('/api/save', json={**save, 'name': 'bad', 'overwrite': True}).status_code == 403
        assert not (custom / 'bad.cube').exists()
        (custom / 'bad.cube').write_bytes(cube.read_bytes())
        assert client.get('/api/recipe', params={'lut': str(custom / 'bad.cube')}).status_code == 403
        self_save = {**save, 'lut': str(saved), 'overwrite': True}
        assert client.post('/api/save', json=self_save).status_code == 409
        old_sidecar, old_cube = saved.with_suffix('.recipe.json').read_bytes(), saved.read_bytes()
        write_new = lab.write_new
        def failing_cube(target, content, overwrite):
            if target == saved:
                raise OSError('simulated cube publish failure')
            return write_new(target, content, overwrite)
        with patch.object(lab, 'write_new', side_effect=failing_cube):
            assert client.post('/api/save', json={**save, 'overwrite': True, 'recipe': {'adjustments': {'contrast': 3}}}).status_code == 400
        assert saved.with_suffix('.recipe.json').read_bytes() == old_sidecar
        assert saved.read_bytes() == old_cube
        failed_binary = root / 'failed-rr'
        failed_binary.write_text('#!/bin/sh\nexit 2\n')
        failed_binary.chmod(0o755)
        failed = TestClient(lab.create_app([cubes], [photos], data_dir=root, rr_lut=failed_binary), base_url='http://127.0.0.1')
        assert failed.post('/api/render', json={**payload, 'recipe': options}).status_code == 503
        assert lab.rr_binary(failed_binary) == failed_binary
        with patch.dict(lab.os.environ, {'RR_LUT': str(failed_binary)}):
            assert lab.rr_binary() == failed_binary
            assert lab.rr_binary(root / "explicit") == root / "explicit"
        print('PASS: sidecar symlink escape, self-overwrite protection, failed-save rollback, failed binary and binary precedence')
    print('All LUT Lab self-checks passed.')


def raw_checks():
    import xml.etree.ElementTree as ET
    from concurrent.futures import ThreadPoolExecutor

    with tempfile.TemporaryDirectory(prefix='lab-test-') as directory:
        root = Path(directory).resolve()
        fake = root / 'lightcraft-cli'
        fixture = Path(__file__).parent / 'fixtures/fake_lightcraft.py'
        fake.write_text('#!' + sys.executable + '\n' + fixture.read_text().split('\n', 1)[1])
        fake.chmod(0o755)
        photos = root / 'photos'; photos.mkdir()
        photo = photos / 'test.arw'; photo.write_bytes(b'fake raw')
        cube = root / 'look.cube'; lut.write_cube(cube, lut.identity())
        preset = root / 'look.xmp'; preset.write_text('<xmp/>')
        app = lab.create_app([root], [photos], data_dir=root, lightcraft=fake, custom=root / 'custom',
                             install=root / 'install', uploads=root / 'uploads', rr_lut=root / 'missing-rr')
        with TestClient(app, base_url='http://127.0.0.1') as client:
            config = client.get('/api/config').json()
            assert config['raw']['available'] and not config['available']
            opened = client.post('/api/raw/open', json={'photo': str(photo)})
            assert opened.status_code == 200, opened.text
            session = opened.json()['session']; engine = app.state.raw
            assert engine.link.is_symlink() and engine.link.parent != photos
            temp = engine.temp
            assert not photo.with_suffix('.xmp').exists()
            body = {'photo': str(photo), 'session': session, 'seq': 2, 'values': {'effects.clarity': 40, 'light.exposure': .5},
                    'curves': {'luma': [{'x': 0, 'y': 4}, {'x': 255, 'y': 255}]}}
            response = client.post('/api/raw/render', json=body)
            assert response.status_code == 200 and response.headers['x-render-seq'] == '2', response.text
            assert client.post('/api/raw/render', json={**body, 'seq': 1}).status_code == 409
            assert client.post('/api/raw/render', json={**body, 'values': {'light.exposure': 99}}).status_code == 422
            assert client.post('/api/raw/render', json={**body, 'curves': {'bad': []}}).status_code == 422
            # An older call arriving after a newer call cannot mutate engine state.
            with ThreadPoolExecutor(2) as pool:
                newer = pool.submit(client.post, '/api/raw/render', json={**body, 'seq': 5})
                assert newer.result().status_code == 200
                older = pool.submit(client.post, '/api/raw/render', json={**body, 'seq': 4, 'values': {'light.exposure': -2}})
                assert older.result().status_code == 409
            assert engine.settings['light']['exposure'] == .5
            saved = client.post('/api/raw/export', json={'name': 'filter', 'session': session})
            assert saved.status_code == 200, saved.text
            target = Path(saved.json()['path']); packet = ET.fromstring(target.read_bytes())
            settings = json.loads(packet.find('.//{' + lab.LC_NS + '}settings').text)
            assert settings['wb']['temp'] == 6500 and settings['light']['exposure'] == .5
            assert not set(settings) & {'crop', 'geometry', 'masks', 'spots', 'red_eye', 'orientation', 'optics', 'enhance', 'lens_blur'}
            assert b'private' not in target.read_bytes() and b'GPSLatitude' not in target.read_bytes() and b'Rating' not in target.read_bytes()
            assert client.post('/api/raw/export', json={'name': 'filter'}).status_code == 409
            assert client.post('/api/raw/export', json={'name': 'filter.xmp', 'overwrite': True}).status_code == 200
            installed = client.post('/api/raw/install', json={'name': 'installed'}).json()
            assert Path(installed['path']) == root / 'install/installed.xmp'
            assert Path(installed['path']).read_bytes() == target.read_bytes()
            assert not photo.with_suffix('.xmp').exists()
            assert next(p for p in client.get('/api/photos').json() if p['path'] == str(photo))['kind'] == 'raw'
            assert next(p for p in client.get('/api/luts').json() if p['path'] == str(target))['kind'] == 'xmp'
            assert client.post('/api/raw/preset', json={'lut': str(preset)}).json()['unmapped'] == ['CameraProfile']
            assert client.post('/api/raw/preset', json={'lut': str(target)}).json()['settings']['light']['exposure'] == .5
            assert client.post('/api/raw/preset', json={'lut': str(cube)}).status_code == 200
            bundle = client.post('/api/raw/export', json={'name': 'cube-profile'}).json()['path']
            assert b'labProfileCube' in Path(bundle).read_bytes()
            assert client.post('/api/raw/preset', json={'lut': bundle}).status_code == 200
            malformed = root / 'malformed.xmp'; malformed.write_text('<broken')
            assert client.post('/api/raw/preset', json={'lut': str(malformed)}).status_code == 400
            reset = client.post('/api/raw/reset', json={})
            assert reset.status_code == 200
            body['session'] = reset.json()['session']
            first_pid = engine.process.pid
            engine.process.kill(); engine.process.wait()
            assert client.post('/api/raw/render', json={**body, 'seq': 6}).status_code == 200
            assert engine.process.pid != first_pid
            for tool in ['test_crash', 'test_timeout']:
                engine.call_timeout = .1
                try:
                    engine.tool(tool)
                    assert False, 'expected 503'
                except lab.HTTPException as error:
                    assert error.status_code == 503
                assert engine.process is None
                engine.call_timeout = 10
                assert client.post('/api/raw/render', json={**body, 'seq': 7}).status_code == 200
                assert engine.settings['light']['exposure'] == .5
            pid = engine.process.pid
            try:
                engine.tool('test_error')
                assert False
            except lab.HTTPException as error:
                assert error.status_code == 400 and engine.process.pid == pid
            outside = root / 'outside.arw'; outside.write_bytes(b'raw')
            (photos / 'escape.arw').symlink_to(outside)
            for bad in [outside, photos / 'escape.arw', photos / '../outside.arw']:
                assert client.post('/api/raw/open', json={'photo': str(bad)}).status_code == 403
                assert client.post('/api/raw/render', json={**body, 'photo': str(bad)}).status_code == 403
            (root / 'custom/escape.xmp').symlink_to(outside)
            assert client.post('/api/raw/export', json={'name': 'escape', 'overwrite': True}).status_code == 403
            for name in ['../escape', 'a/b', '..', '', 'bad\\name']:
                assert client.post('/api/raw/export', json={'name': name}).status_code == 400
            previous = engine.process
            newer = client.post('/api/raw/open', json={'photo': str(photo)}).json()
            assert previous.poll() is not None and not temp.exists()
            assert client.post('/api/raw/render', json=body).status_code == 409
            current_process = engine.process
            current_temp = engine.temp
        assert current_process.poll() is not None and not current_temp.exists()
        missing = lab.create_app([], [photos], data_dir=root, lightcraft=root / 'absent', rr_lut=root / 'absent')
        with TestClient(missing, base_url='http://127.0.0.1') as client:
            assert not client.get('/api/config').json()['raw']['available']
            assert client.post('/api/raw/open', json={'photo': str(photo)}).status_code == 503
            assert client.post('/api/raw/reset', json={}).status_code == 503
            assert client.post('/api/raw/render', json={'photo': str(photo)}).status_code == 503
        with patch.object(Path, 'symlink_to', side_effect=OSError('no symlinks')):
            engine = lab.LightcraftSession(fake)
            try:
                engine.open(photo)
                assert not engine.link.is_symlink() and engine.link.read_bytes() == photo.read_bytes()
            finally:
                engine.close()
    print('PASS: fake RAW protocol, lifecycle/crash/timeout/replay, stale renders, controls/curves, XMP stripping/round-trip, cube bundles, install, containment and copy fallback')


def raw_integration():
    import os
    import shutil
    binary = lab.resolve_binary(None, 'LIGHTCRAFT_CLI', 'lightcraft-cli')
    sample = Path(os.environ['LUT_LAB_RAW_SAMPLE']).expanduser() if os.environ.get('LUT_LAB_RAW_SAMPLE') else None
    if binary is None or sample is None or not binary.is_file() or not sample.is_file():
        print('SKIP: real RAW integration (LightCraft binary or LUT_LAB_RAW_SAMPLE missing)')
        return
    with tempfile.TemporaryDirectory(prefix='lab-real-') as directory:
        root = Path(directory).resolve(); photo = root / sample.name
        shutil.copyfile(sample, photo)
        app = lab.create_app([root], [root], data_dir=root, lightcraft=binary, custom=root / 'custom', install=root / 'install')
        with TestClient(app, base_url='http://127.0.0.1') as client:
            start = time.perf_counter(); response = client.post('/api/raw/open', json={'photo': str(photo)})
            assert response.status_code == 200, response.text
            times = {'open_ms': round((time.perf_counter() - start) * 1000, 1)}
            session = response.json()['session']
            body = {'photo': str(photo), 'session': session, 'values': {'effects.clarity': 40, 'light.exposure': .5}, 'size': 1600, 'seq': 1}
            response = client.post('/api/raw/render', json=body); assert response.status_code == 200, response.text
            times['first_render_ms'] = float(response.headers['x-render-ms'])
            times['drag_ms'] = []
            for seq in range(2, 7):
                response = client.post('/api/raw/render', json={**body, 'size': 1024, 'seq': seq, 'values': {**body['values'], 'light.exposure': seq / 10}})
                assert response.status_code == 200, response.text
                times['drag_ms'].append(float(response.headers['x-render-ms']))
            assert client.post('/api/raw/render', json={**body, 'seq': 7}).status_code == 200
            saved = client.post('/api/raw/export', json={'name': 'real'}); assert saved.status_code == 200, saved.text
            target = Path(saved.json()['path'])
            side = root / 'side'; direct = root / 'direct'; side.mkdir(); direct.mkdir()
            for folder in (side, direct):
                (folder / photo.name).symlink_to(photo)
            shutil.copyfile(target, (side / photo.name).with_suffix('.xmp'))
            env = {**os.environ, 'LIGHTCRAFT_CONFIG_DIR': str(root / 'config')}
            for folder, options in [(side, []), (direct, ['--set', 'effects.clarity=40', '--set', 'light.exposure=0.5'])]:
                subprocess.run([str(binary), 'render', str(folder / photo.name), '-o', str(folder / 'out.jpg'), '--size', '800', *options],
                               env=env, capture_output=True, check=True, timeout=60)
            assert (side / 'out.jpg').read_bytes() == (direct / 'out.jpg').read_bytes()
            assert not photo.with_suffix('.xmp').exists()
            # A crash must restore settings and custom profile assets as well as the photo.
            engine = app.state.raw
            before = engine.settings
            engine.process.kill(); engine.process.wait()
            response = client.post('/api/raw/render', json={'photo': str(photo), 'seq': 8, 'values': {}, 'size': 800})
            assert response.status_code == 200, response.text
            assert engine.settings == before
            cube = root / 'custom/profile.cube'; lut.write_cube(cube, lut.identity() ** 1.2)
            response = client.post('/api/raw/preset', json={'lut': str(cube)})
            assert response.status_code == 200, response.text
            response = client.post('/api/raw/render', json={'photo': str(photo), 'seq': 9, 'size': 800})
            expected = response.content
            bundle = client.post('/api/raw/export', json={'name': 'bundled'}).json()['path']
            assert client.post('/api/raw/open', json={'photo': str(photo)}).status_code == 200
            response = client.post('/api/raw/preset', json={'lut': bundle})
            assert response.status_code == 200, response.text
            response = client.post('/api/raw/render', json={'photo': str(photo), 'seq': 1, 'size': 800})
            assert response.status_code == 200 and response.content == expected
            print('RAW LATENCY ' + json.dumps(times))
    print('PASS: real LightCraft RAW open/edit/export, exact 800px JPEG sidecar round-trip')


if __name__ == '__main__':
    raw_checks()
    raw_integration()
    if '--raw-only' not in sys.argv:
        # The deterministic engine tests integration contracts, not GPU color science.
        with tempfile.TemporaryDirectory(prefix='lab-rr-test-') as directory:
            fake = Path(directory) / 'rr-lut'
            source = Path(__file__).parent / 'fixtures/fake_rr.py'
            fake.write_text('#!' + sys.executable + '\n' + source.read_text())
            fake.chmod(0o755)
            with patch.dict(os.environ, {'RR_LUT': str(fake)}):
                main()
