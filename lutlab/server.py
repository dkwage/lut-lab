#!/usr/bin/env python3
"""Local LUT Lab. Run: lut-lab"""
import argparse
import base64
from contextlib import asynccontextmanager
import select
import shutil
from functools import lru_cache
from io import BytesIO
import json
import math
import os
from pathlib import Path
import shlex
import sys
import subprocess
from threading import Lock
from typing import Literal
import xml.etree.ElementTree as ET
import tempfile
import time
from uuid import uuid4

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from lutlab import imaging, lut

FROZEN = getattr(sys, 'frozen', False)
RESOURCES = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
STATIC = Path(__file__).parent / 'static'

PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', *imaging.RAW_EXTS}
MAX_UPLOAD = 40 * 1024 * 1024


# Data contract from RapidRAW v1.6.4 UI; no rendering code is imported.
BANDS = ('reds', 'oranges', 'yellows', 'greens', 'aquas', 'blues', 'purples', 'magentas')
CHANNELS = ('luma', 'red', 'green', 'blue')
ZONES = ('shadows', 'midtones', 'highlights', 'global')
BASIC = ('temperature', 'tint', 'exposure', 'contrast', 'highlights', 'shadows',
         'whites', 'blacks', 'vibrance', 'saturation')
RANGES = {key: (-5, 5) if key == 'exposure' else (-100, 100) for key in BASIC}
RANGES.update(hsl={band: {key: (-100, 100) for key in ('hue', 'saturation', 'luminance')} for band in BANDS},
              colorGrading={**{zone: {'hue': (0, 360), 'saturation': (0, 100), 'luminance': (-100, 100)} for zone in ZONES},
                            'blending': (0, 100), 'balance': (-100, 100)},
              colorCalibration={key: (-100, 100) for key in ('shadowsTint', 'redHue', 'redSaturation',
                                                           'greenHue', 'greenSaturation', 'blueHue', 'blueSaturation')})
# shortcut: serialize local saves; use per-target locks if concurrent writers matter.
SAVE_LOCK = Lock()


def validate_adjustments(value, schema=RANGES, path='adjustments'):
    if not isinstance(value, dict):
        raise ValueError(f'{path}: 객체가 필요합니다.')
    for key, item in value.items():
        name = f'{path}.{key}'
        if path == 'adjustments' and key == 'curveMode':
            if item != 'point':
                raise ValueError('LUT에서는 포인트 곡선만 지원합니다.')
        elif path == 'adjustments' and key == 'curves':
            if not isinstance(item, dict) or set(item) - set(CHANNELS):
                raise ValueError('올바른 곡선 채널이 필요합니다.')
            for points in item.values():
                if not isinstance(points, list) or not 2 <= len(points) <= 16:
                    raise ValueError('곡선은 2–16개의 점이 필요합니다.')
                previous = -1
                for point in points:
                    if (not isinstance(point, dict) or set(point) != {'x', 'y'} or
                            any(type(v) is not int or not 0 <= v <= 255 for v in point.values()) or point['x'] <= previous):
                        raise ValueError('곡선 좌표는 0–255 정수, x는 오름차순이어야 합니다.')
                    previous = point['x']
        elif key not in schema:
            raise ValueError(f'{name}: 지원하지 않는 보정입니다.')
        elif isinstance(schema[key], dict):
            validate_adjustments(item, schema[key], name)
        elif type(item) not in (int, float) or not math.isfinite(item) or not schema[key][0] <= item <= schema[key][1]:
            raise ValueError(f'{name}: 범위를 벗어난 값입니다.')
    return value


class Recipe(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    version: Literal[1] = 1
    adjustments: dict = Field(default_factory=dict)
    amount: float = Field(1, ge=0, le=2, strict=True)
    _validate = field_validator('adjustments')(validate_adjustments)

    @field_validator('version', mode='before')
    @classmethod
    def version_number(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError('레시피 버전은 정수 1이어야 합니다.')
        return value


class SavedRecipe(Recipe):
    mode: Literal['none', 'compose', 'preset'] = 'none'
    base_lut: str | None = None


class CubeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    lut: str | None = None
    mode: Literal['none', 'compose', 'preset'] = 'compose'
    recipe: Recipe = Field(default_factory=Recipe)


class RenderRequest(CubeRequest):
    photo: str
    size: int = Field(1600, ge=0, le=4096)  # 0 preserves all pixels.


class SaveRequest(CubeRequest):
    name: str
    overwrite: bool = False


def allowed_path(value, roots, extensions=None):
    """Resolve symlinks before checking containment, including absent output files."""
    path = Path(value).expanduser()
    if '..' in path.parts:
        raise HTTPException(403, '상위 폴더 접근은 허용되지 않습니다.')
    path = path.resolve()
    if not any(path.is_relative_to(root) for root in roots):
        raise HTTPException(403, '허용된 폴더 밖의 파일입니다.')
    if extensions and path.suffix.lower() not in extensions:
        raise HTTPException(400, '지원하지 않는 파일 형식입니다.')
    return path


def cube_name(value):
    if not value.strip() or value != value.strip() or any(c in value for c in '/\\\x00') or any(ord(c) < 32 for c in value):
        raise HTTPException(400, '폴더 경로 없이 파일 이름만 입력하세요.')
    name = value[:-5] if value.lower().endswith('.cube') else value
    if name in ('', '.', '..') or len(name.encode()) > 200:
        raise HTTPException(400, '올바른 파일 이름을 입력하세요.')
    return name + '.cube'


def stamp(path):
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


@lru_cache(maxsize=4)
def decoded(path, version):
    return imaging.develop(path)


@lru_cache(maxsize=24)
def parsed(path, version):
    # Validate and reload whenever the file stamp changes.
    return lut.read_cube(path), imaging.load_cube.__wrapped__(path)


def resolve_binary(value, variable, name):
    explicit = value or os.environ.get(variable)
    if explicit:
        return Path(shutil.which(str(explicit)) or explicit).expanduser().resolve()
    bundled = RESOURCES / 'bin' / name
    if FROZEN and bundled.is_file():
        return bundled
    found = shutil.which(name)
    return Path(found).resolve() if found else None


def rr_binary(value=None):
    return resolve_binary(value, 'RR_LUT', 'rr-lut')


def app_config_dir():
    return Path.home() / ('Library/Application Support/LUT Lab' if sys.platform == 'darwin' else '.lut-lab')


def data_config(data_dir=None):
    directory = Path(data_dir or app_config_dir()).expanduser().resolve()
    path = directory / 'config.json'
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(config, dict) or set(config) - {'install_dir'}:
            raise ValueError('설정에는 install_dir만 지정할 수 있습니다.')
        value = config.get('install_dir')
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError('install_dir에는 폴더 경로가 필요합니다.')
        return config, []
    except (OSError, ValueError) as error:
        return {}, [f'설정 파일을 읽을 수 없습니다: {path} · {error}']


def rr_run(binary, *args):
    try:
        return subprocess.run([str(binary), *map(str, args)], capture_output=True, text=True,
                              check=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, 'stderr', None) or str(error)
        raise HTTPException(503, f'rr-lut 실행 실패. 실행 파일과 Metal GPU를 확인하세요: {detail[:600]}') from error


def rr_stamp(binary):
    if binary is None:
        raise HTTPException(503, '보정 엔진(rr-lut) 없음')
    try:
        return stamp(binary)
    except OSError as error:
        raise HTTPException(503, f'rr-lut 실행 파일을 찾을 수 없습니다: {binary}') from error


@lru_cache(maxsize=24)
def adjusted(path, version, options, mode, binary, binary_version):
    recipe = Recipe.model_validate_json(options)
    with tempfile.TemporaryDirectory(prefix='lut-lab-') as directory:
        source, output = Path(directory) / 'adjustments.json', Path(directory) / 'preview.cube'
        # rr-lut supplies neutral defaults for every omitted key, including nested keys.
        source.write_text(json.dumps(recipe.adjustments, sort_keys=True))
        rr_run(binary, 'adjustments', source, '-o', output)
        try:
            content = output.read_bytes()
            if content.splitlines()[:3] != [b'LUT_3D_SIZE 33', b'DOMAIN_MIN 0.0 0.0 0.0', b'DOMAIN_MAX 1.0 1.0 1.0']:
                raise ValueError('rr-lut header')
            table = np.loadtxt(BytesIO(content), skiprows=3)
            if table.shape != (33 ** 3, 3) or not np.isfinite(table).all():
                raise ValueError('rr-lut table')
            rr_cube = table, np.zeros(3), np.ones(3), 33
        except (OSError, ValueError) as error:
            raise HTTPException(503, 'rr-lut이 올바른 큐브를 만들지 못했습니다.') from error
        identity = lut.identity()
        neutral = np.array_equal(rr_cube[0], identity)
        if path and mode == 'compose' and neutral and recipe.amount == 1:
            return Path(path).read_bytes(), parsed(path, version)[1]
        table = rr_cube[0]
        if path and mode == 'compose':
            base = lut.sample(parsed(path, version)[0], identity)
            table = base if neutral else lut.sample(rr_cube, base)
        if recipe.amount != 1 or (path and mode == 'compose'):
            lut.write_cube(output, np.clip(identity + recipe.amount * (table - identity), 0, 1))
            content = output.read_bytes()
            return content, imaging.load_cube.__wrapped__(output)
        return content, imaging.ImageFilter.Color3DLUT(33, rr_cube[0].ravel().tolist())


def bake(recipe, path=None, mode='none', binary=None):
    binary = rr_binary(binary)
    if mode == 'none' and path:
        raise HTTPException(422, '필터 없음 모드에는 기본 LUT를 지정할 수 없습니다.')
    if mode == 'preset' and not path:
        raise HTTPException(422, '프리셋의 원본 LUT 경로가 필요합니다.')
    if not recipe.adjustments and mode != 'preset':
        if path and recipe.amount == 1:
            return Path(path).read_bytes(), parsed(path, stamp(path))[1]
        identity = lut.identity()
        table = lut.sample(parsed(path, stamp(path))[0], identity) if path else identity
        table = np.clip(identity + recipe.amount * (table - identity), 0, 1)
        with tempfile.TemporaryDirectory(prefix='lut-lab-cube-') as directory:
            output = Path(directory) / 'look.cube'
            lut.write_cube(output, table)
            return output.read_bytes(), imaging.load_cube.__wrapped__(output)
    return adjusted(path, stamp(path) if path else None, json.dumps(recipe.model_dump(), sort_keys=True),
                    mode, binary, rr_stamp(binary))


def render_image(photo, cube=None, recipe=None, size=0, mode='compose', binary=None):
    """Full-resolution developed pixels first; resize only the completed preview."""
    original = decoded(photo, stamp(photo))
    if recipe is None and cube is None:
        image = original.copy()
    else:
        image = original.filter(bake(recipe or Recipe(), cube, mode, binary)[1])
    if size:
        image.thumbnail((size, size))
    return image


def indexed_source(path, roots):
    """Find source presets in indexes beside a cube or its ancestor directories."""
    for root in roots:
        if not path.is_relative_to(root):
            continue
        for parent in path.parents:
            if not parent.is_relative_to(root):
                break
            index = parent / 'index.json'
            if not index.is_file():
                continue
            index = allowed_path(str(index), roots, {'.json'})
            records = json.loads(index.read_text())
            if not isinstance(records, list):
                raise ValueError('index.json must contain a list')
            for row in records:
                if (not isinstance(row, dict) or row.get('status', 'ok') != 'ok' or
                        not all(isinstance(row.get(k), str) for k in ('output', 'source'))):
                    continue
                for base in (parent, parent.parent):
                    if (base / row['output']).resolve() == path:
                        return allowed_path(str(base / row['source']), roots, {'.xmp', '.lrtemplate'})
    return None


def initial_recipe(path, binary=None, roots=()):
    source = indexed_source(path, roots) if path and rr_binary(binary) else None
    if source:
        tree = ET.fromstring(source.read_text()) if source.suffix.lower() == '.xmp' else None
        ns = '{http://ns.adobe.com/camera-raw-settings/1.0/}'
        if tree is not None and any(e.get(ns + 'ConvertToGrayscale') == 'True' for e in tree.iter()):
            return SavedRecipe(mode='compose', base_lut=str(path)), '흑백 근사 프리셋 · 기존 큐브 위에 보정합니다.'
        with tempfile.TemporaryDirectory(prefix='lut-lab-preset-') as directory:
            normalized = source
            if tree is not None:
                for node in tree.iter():
                    for zone in ('Shadow', 'Highlight'):
                        for old, new in [('Hue', 'Hue'), ('Sat', 'Saturation')]:
                            value = node.get(ns + f'ColorGrade{zone}{old}')
                            if value is not None:
                                node.attrib.setdefault(ns + f'SplitToning{zone}{new}', value)
                    if (value := node.get(ns + 'ColorGradeBalance')) is not None:
                        node.attrib.setdefault(ns + 'SplitToningBalance', value)
                    value = node.get(ns + 'Temperature', '')
                    if value.startswith(('+', '-')):
                        node.set(ns + 'Temperature', f'{5500 + float(value):g}')
                normalized = Path(directory) / 'preset.xmp'
                ET.register_namespace('crs', ns[1:-1])
                normalized.write_bytes(ET.tostring(tree))
            converted = json.loads(rr_run(rr_binary(binary), 'preset', normalized))
        adjustments = {k: v for k, v in converted.items() if k in {*RANGES, 'curves', 'curveMode'}}
        return SavedRecipe(mode='preset', base_lut=str(path), adjustments=adjustments), '프리셋의 원본 보정값을 불러왔습니다.'
    return SavedRecipe(mode='compose' if path else 'none', base_lut=str(path) if path else None), '기존 큐브 위에 보정합니다.' if path else '필터 없음'


def jpeg(image):
    output = BytesIO()
    image.save(output, 'JPEG', quality=95, subsampling=0)
    return output.getvalue()


def write_new(target, content, overwrite):
    """Publish a complete file atomically; a concurrent save never clobbers it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise HTTPException(403, '심볼릭 링크에는 저장할 수 없습니다.')
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        if overwrite:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise HTTPException(409, '같은 이름의 파일이 있습니다. 다른 이름을 쓰거나 덮어쓰기를 선택하세요.')
    finally:
        temporary.unlink(missing_ok=True)


LC_NS = 'http://ns.lightcraft.app/lc/1.0/'
FILTER_KEYS = {'version', 'process', 'profile', 'wb', 'light', 'curve', 'color', 'mixer',
               'grading', 'effects', 'vignette', 'grain', 'detail', 'calibration',
               'treatment', 'bw_mix', 'point_colors', 'disabled_sections'}


def lightcraft_run(binary, *args):
    with tempfile.TemporaryDirectory(prefix='lut-lab-probe-') as directory:
        try:
            return subprocess.run([str(binary), *args], env={**os.environ, 'LIGHTCRAFT_CONFIG_DIR': directory},
                                  capture_output=True, text=True, check=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise HTTPException(503, f'LightCraft 실행 실패. RAW 실행 파일을 확인하세요: {str(error)[:400]}') from error


@lru_cache(maxsize=4)
def lightcraft_specs(binary, version):
    try:
        specs = json.loads(lightcraft_run(binary, 'controls', '--json'))
        if not isinstance(specs, list) or not specs or any(not {'id', 'label', 'min', 'max', 'step', 'default'} <= item.keys() for item in specs):
            raise ValueError('잘못된 controls 응답')
        return specs
    except (ValueError, TypeError, AttributeError) as error:
        raise HTTPException(503, f'LightCraft 제어 규격을 읽을 수 없습니다: {error}') from error


def filter_xmp(content):
    tree = ET.fromstring(content)
    settings = tree.find('.//{' + LC_NS + '}settings')
    if settings is None or not settings.text:
        raise HTTPException(400, 'LightCraft 현상 설정이 없는 XMP입니다.')
    values = json.loads(settings.text)
    if not isinstance(values, dict):
        raise HTTPException(400, 'XMP 현상 설정은 객체여야 합니다.')
    values = {key: value for key, value in values.items() if key in FILTER_KEYS}
    # A fresh packet excludes all photo metadata, including unknown namespaces.
    for prefix, uri in [('x', 'adobe:ns:meta/'), ('rdf', 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'), ('lc', LC_NS)]:
        ET.register_namespace(prefix, uri)
    root = ET.Element('{adobe:ns:meta/}xmpmeta')
    rdf = ET.SubElement(root, '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF')
    description = ET.SubElement(rdf, '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description')
    ET.SubElement(description, '{' + LC_NS + '}settings').text = json.dumps(values, ensure_ascii=False)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


class RawPhoto(BaseModel):
    model_config = ConfigDict(extra='forbid')
    photo: str


class RawRender(RawPhoto):
    values: dict = Field(default_factory=dict)
    curves: dict | None = None
    size: int = Field(1600, ge=1, le=4096)
    seq: int = Field(0, ge=0)
    session: str | None = None
    original: bool = False

    @field_validator('curves')
    @classmethod
    def check_curves(cls, value):
        if value is not None:
            validate_adjustments({'curves': value})
        return value


class RawAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session: str | None = None


class RawPreset(RawAction):
    lut: str


class RawSave(RawAction):
    name: str
    overwrite: bool = False


class LightcraftSession:
    """One serialized engine; every path it may write is inside its own temp dir."""
    call_timeout = 10
    render_timeout = 30

    def __init__(self, binary):
        self.binary = binary
        self.lock = Lock()
        self.process = None
        self.directory = None
        self.photo = None
        self.settings = None
        self.session = None
        self.latest_seq = -1
        self.original = None
        self.imports = {}
        self.profile_source = None
        self.request_id = 0

    def stop(self):
        if self.process:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None

    def close(self):
        self.stop()
        if self.directory:
            self.directory.cleanup()
            self.directory = None
        self.photo = None

    def rpc(self, method, params, timeout=None):
        self.request_id += 1
        request_id = self.request_id
        try:
            self.process.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': request_id,
                                                 'method': method, 'params': params}) + '\n').encode())
            self.process.stdin.flush()
            deadline = time.monotonic() + (timeout or self.call_timeout)
            while True:
                while b'\n' not in self.buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                        raise TimeoutError('응답 시간 초과')
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        raise OSError('엔진이 종료되었습니다.')
                    self.buffer += chunk
                line, self.buffer = self.buffer.split(b'\n', 1)
                reply = json.loads(line)
                if reply.get('id') != request_id:
                    continue
                if 'error' in reply:
                    raise ValueError(str(reply['error']))
                result = reply['result']
                if result.get('isError'):
                    raise HTTPException(400, 'LightCraft: ' + str(result.get('content'))[:600])
                return result
        except (OSError, ValueError, TimeoutError, KeyError, TypeError, AttributeError) as error:
            self.stop()
            raise HTTPException(503, f'LightCraft 응답 실패. 다시 시도하면 재시작합니다: {error}') from error

    def tool(self, name, arguments=None, timeout=None):
        return self.rpc('tools/call', {'name': name, 'arguments': arguments or {}}, timeout)

    def data(self, name, arguments=None):
        result = self.tool(name, arguments)
        if 'structuredContent' in result:
            return result['structuredContent']
        text = next(item['text'] for item in result['content'] if item['type'] == 'text')
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if text == 'ok':
                return None
            raise

    def command(self, name, params=None):
        return self.data('command_run', {'id': name, 'params': params or {}})

    def start(self):
        self.stop()
        self.buffer = b''
        try:
            self.process = subprocess.Popen([str(self.binary), 'mcp', '--library', str(self.temp / 'library'), str(self.link)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={**os.environ, 'LIGHTCRAFT_CONFIG_DIR': str(self.temp / 'config'), 'TMPDIR': str(self.temp)})
        except OSError as error:
            raise HTTPException(503, f'LightCraft 실행 파일을 확인하세요: {error}') from error
        self.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                               'clientInfo': {'name': 'lut-lab', 'version': '1'}})
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()
        if self.settings is not None:
            self.tool('set_develop', {'settings': self.settings})

    def ensure(self, session=None, photo=None):
        if self.binary is None or not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise HTTPException(503, 'LightCraft 실행 파일을 찾을 수 없습니다.')
        if self.photo is None:
            raise HTTPException(409, 'RAW 사진을 먼저 여세요.')
        if (session is not None and session != self.session) or (photo is not None and photo != self.photo):
            raise HTTPException(409, '사진 세션이 바뀌었습니다. 다시 여세요.')
        if self.process is None or self.process.poll() is not None:
            self.start()

    def snapshot(self):
        self.settings = self.command('develop.get')
        # Control IDs and serialized setting keys differ (camelCase vs snake_case).
        controls = self.data('list_controls')
        return {'settings': self.settings, 'controls': controls, 'session': self.session}

    def open(self, photo):
        self.close()
        if self.binary is None:
            raise HTTPException(503, 'LightCraft 실행 파일을 찾을 수 없습니다.')
        try:
            self.specs = lightcraft_specs(self.binary, stamp(self.binary))
        except OSError as error:
            raise HTTPException(503, f'LightCraft 실행 파일을 찾을 수 없습니다: {self.binary}') from error
        self.directory = tempfile.TemporaryDirectory(prefix='lut-lab-raw-')
        self.temp = Path(self.directory.name)
        self.photo = photo
        self.link = self.temp / photo.name
        try:
            self.link.symlink_to(photo)
        except OSError:
            shutil.copyfile(photo, self.link)
        self.settings = None
        self.original = None
        self.imports = {}
        self.profile_source = None
        self.session = uuid4().hex
        self.latest_seq = -1
        self.start()
        imported = self.command('develop.get')
        self.command('develop.reset')
        self.original = self.preview(1600)
        self.tool('set_develop', {'settings': imported})
        result = self.snapshot()
        return result

    def preview(self, size):
        result = self.tool('render_preview', {'max_side': size, 'format': 'jpeg'}, self.render_timeout)
        return base64.b64decode(next(item['data'] for item in result['content'] if item['type'] == 'image'), validate=True)

    def render(self, body):
        specs = {item['id']: item for item in self.specs}
        for key, value in body.values.items():
            spec = specs.get(key)
            if not spec or type(value) not in (int, float) or not math.isfinite(value) or not spec['min'] <= value <= spec['max']:
                raise HTTPException(422, f'잘못된 RAW 조정값: {key}')
        if self.original is None:
            # First render precedes any edits; keep it across previews and resets.
            self.original = self.preview(1600)
        if body.original:
            return self.original
        current = {item['id']: item['value'] for item in self.data('list_controls')}
        changed = {key: value for key, value in body.values.items() if current.get(key) != value}
        if changed:
            self.tool('set_develop', {'values': changed})
        for channel, points in (body.curves or {}).items():
            name = 'master' if channel == 'luma' else channel
            normalized = [[p['x'] / 255, p['y'] / 255] for p in points]
            existing = self.settings.get('curve', {}).get(name, [])
            existing = [[p['x'], p['y']] if isinstance(p, dict) else p for p in existing]
            if normalized != existing:
                self.command('develop.curve', {'channel': name, 'points': normalized})
        self.settings = self.command('develop.get')
        return self.preview(body.size)

    def preset(self, path):
        if self.original is None:
            self.original = self.preview(1600)
        if path.suffix.lower() == '.xmp':
            tree = ET.fromstring(path.read_bytes())
            native = tree.find('.//{' + LC_NS + '}settings')
            if native is not None:
                values = json.loads(native.text or '{}')
                if not isinstance(values, dict):
                    raise HTTPException(400, 'XMP 현상 설정은 객체여야 합니다.')
                values = {k: v for k, v in values.items() if k in FILTER_KEYS}
                embedded = tree.find('.//{' + LC_NS + '}labProfileCube')
                if embedded is not None:
                    cube = self.temp / 'embedded.cube'
                    cube.write_text(embedded.text)
                    result = self.command('profile.import', {'paths': [str(cube)]})
                    if not result.get('imported'):
                        raise HTTPException(400, 'XMP에 포함된 큐브 프로필을 읽을 수 없습니다.')
                    values.setdefault('profile', {})['id'] = result['imported'][0]['id']
                    self.profile_source = cube
                elif values.get('profile', {}).get('id', '').startswith('lut:'):
                    raise HTTPException(400, '큐브 프로필 데이터가 없는 XMP입니다.')
                self.tool('set_develop', {'settings': values})
                self.session = uuid4().hex
                return {**self.snapshot(), 'unmapped': [], 'kind': 'preset'}
        cache_key = (path, stamp(path))
        if cache_key not in self.imports:
            result = self.command('profile.import' if path.suffix.lower() == '.cube' else 'preset.import', {'paths': [str(path)]})
            if not result.get('imported'):
                raise HTTPException(400, '프리셋을 가져오지 못했습니다: ' + str(result.get('failed', [])))
            self.imports[cache_key] = result['imported'][0]
        entry = self.imports[cache_key]
        if path.suffix.lower() == '.cube':
            self.command('develop.profile', {'id': entry['id'], 'amount': self.settings['profile']['amount']})
            self.profile_source = path
        else:
            self.command('preset.apply', {'id': entry['id']})
        self.session = uuid4().hex
        return {**self.snapshot(), 'unmapped': entry.get('unmapped', []),
                'kind': 'profile' if path.suffix.lower() == '.cube' else 'preset'}

    def export(self):
        self.command('photo.saveMetadataToFile')
        content = filter_xmp(self.link.with_suffix('.xmp').read_bytes())
        if self.settings.get('profile', {}).get('id', '').startswith('lut:'):
            if self.profile_source is None:
                raise HTTPException(409, '큐브 프로필 원본이 없습니다. 라이브러리에서 다시 선택하세요.')
            tree = ET.fromstring(content)
            ET.SubElement(tree, '{' + LC_NS + '}labProfileCube').text = self.profile_source.read_text()
            content = ET.tostring(tree, encoding='utf-8', xml_declaration=True)
        return content


def create_app(lut_dirs=None, photo_dirs=None, *, data_dir=None, uploads=None, custom=None,
               install=None, rr_lut=None, lightcraft=None):
    directory = Path(data_dir or app_config_dir()).expanduser().resolve()
    settings, config_errors = data_config(directory)
    binary = rr_binary(rr_lut)
    raw_binary = resolve_binary(lightcraft, 'LIGHTCRAFT_CLI', 'lightcraft-cli')
    raw = LightcraftSession(raw_binary)
    uploads = Path(uploads or directory / 'uploads').expanduser().resolve()
    custom = Path(custom or directory / 'custom').expanduser().resolve()
    install = install or settings.get('install_dir')
    install = Path(install).expanduser().resolve() if install else None
    lut_roots = list(dict.fromkeys(Path(p).expanduser().resolve() for p in [directory / 'luts', *(lut_dirs or [])]))
    photo_roots = list(dict.fromkeys(Path(p).expanduser().resolve() for p in (photo_dirs or [])))
    readable_luts = [*lut_roots, custom, *([install] if install else [])]
    readable_photos = [*photo_roots, uploads]
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            with raw.lock:
                raw.close()

    app = FastAPI(title='LUT Lab', docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.raw = raw
    app.state.data_dir = directory
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost'])

    @app.middleware('http')
    async def local_requests(request, call_next):
        if request.method not in ('GET', 'HEAD'):
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': '다른 사이트에서의 요청은 허용되지 않습니다.'}, status_code=403)
        return await call_next(request)

    @app.exception_handler(OSError)
    @app.exception_handler(ValueError)
    @app.exception_handler(ET.ParseError)
    @app.exception_handler(imaging.rawpy.LibRawError)
    @app.exception_handler(imaging.Image.DecompressionBombError)
    async def invalid_file(request, error):
        return JSONResponse({'detail': f'파일 처리 실패: {error}'}, status_code=400)

    app.mount('/fonts', StaticFiles(directory=STATIC / 'fonts'), name='fonts')

    @app.get('/')
    def page():
        return FileResponse(STATIC / 'index.html')

    @app.get('/api/config')
    def config():
        try:
            adjusted(None, None, Recipe().model_dump_json(), 'none', binary, rr_stamp(binary))
            error = None
        except HTTPException as failure:
            error = failure.detail
        try:
            version = lightcraft_run(raw_binary, '--version').strip()
            lightcraft_specs(raw_binary, stamp(raw_binary))
            raw_config = {'available': True, 'version': version, 'error': None}
        except (HTTPException, OSError, ValueError) as failure:
            raw_config = {'available': False, 'version': None, 'error': str(getattr(failure, 'detail', failure))}
        data_errors = config_errors + [f'폴더를 찾을 수 없습니다: {p}'
                                      for p in [*lut_roots[1:], *photo_roots] if not p.is_dir()]
        return {'data_errors': data_errors, 'raw': raw_config, 'custom': str(custom),
                'install': str(install) if install else None, 'uploads': str(uploads),
                'rr_lut': str(binary) if binary else None, 'available': error is None, 'error': error}

    def discover(roots, extensions):
        found = {}
        for root in roots:
            if not root.is_dir():
                continue
            for candidate in sorted(root.rglob('*')):
                if candidate.suffix.lower() not in extensions or not candidate.is_file():
                    continue
                path = candidate.resolve()
                if path.is_relative_to(root):
                    found.setdefault(path, root)
        return found

    @app.get('/api/photos')
    def photos():
        from urllib.parse import urlencode
        return [{'path': str(path), 'name': path.name, 'source': str(root),
                 'kind': 'raw' if path.suffix.lower() in imaging.RAW_EXTS else 'image',
                 'thumbnail': '/api/thumbnail?' + urlencode({'photo': str(path)})}
                for path, root in discover(readable_photos, PHOTO_EXTS).items()]

    @app.get('/api/thumbnail')
    def thumbnail(photo: str):
        path = allowed_path(photo, readable_photos, PHOTO_EXTS)
        return Response(jpeg(render_image(path, size=180)), media_type='image/jpeg')

    @app.get('/api/luts')
    def cubes():
        files = discover(readable_luts, {'.cube', '.xmp', '.lrtemplate'})
        metadata = {}
        for path, root in files.items():
            for parent in [path.parent, *path.parent.parents]:
                if not parent.is_relative_to(root):
                    break
                index = parent / 'index.json'
                if index in metadata or not index.is_file() or not index.resolve().is_relative_to(root):
                    continue
                try:
                    records = json.loads(index.read_text())
                    metadata[index] = records if isinstance(records, list) else []
                except (OSError, ValueError):
                    metadata[index] = []
        indexed = {}
        for index, records in metadata.items():
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get('output'), str):
                    continue
                # Accept paths relative to the index or its parent directory.
                for parent in (index.parent, index.parent.parent):
                    candidate = (parent / record['output']).resolve()
                    if candidate in files:
                        indexed[candidate] = record
        return [{'path': str(path), 'name': path.stem, 'category': path.parent.name,
                 'kind': 'cube' if path.suffix.lower() == '.cube' else 'xmp',
                 'source': str(root), 'has_recipe': path.with_suffix('.recipe.json').is_file(), 'ignored': indexed.get(path, {}).get('ignored', []),
                 'ignored_reasons': indexed.get(path, {}).get('ignored_reasons', {}),
                 'badges': indexed.get(path, {}).get('badges', [])}
                for path, root in files.items()]

    def source(body):
        return allowed_path(body.lut, readable_luts, {'.cube'}) if body.lut else None

    @app.get('/api/recipe')
    def recipe(path: str = Query(alias='lut')):
        path = allowed_path(path, readable_luts, {'.cube'})
        if binary is None or not binary.is_file():
            saved, note = initial_recipe(path)
            return {**saved.model_dump(), 'note': note}
        sidecar = path.with_suffix('.recipe.json')
        if sidecar.exists() or sidecar.is_symlink():
            sidecar = allowed_path(str(sidecar), readable_luts, {'.json'})
            saved = SavedRecipe.model_validate_json(sidecar.read_text())
            if saved.base_lut:
                allowed_path(saved.base_lut, readable_luts, {'.cube'})
            return {**saved.model_dump(), 'note': '저장한 레시피를 복원했습니다.'}
        saved, note = initial_recipe(path, binary, readable_luts)
        return {**saved.model_dump(), 'note': note}

    @app.get('/api/info')
    def info(path: str = Query(alias='lut')):
        path = allowed_path(path, readable_luts, {'.cube'})
        return lut.cube_info(parsed(path, stamp(path))[0])

    @app.post('/api/render')
    def render(body: RenderRequest):
        start = time.perf_counter()
        photo = allowed_path(body.photo, readable_photos, PHOTO_EXTS)
        image = render_image(photo, source(body), body.recipe, body.size, body.mode, binary)
        content = jpeg(image)
        return Response(content, media_type='image/jpeg',
                        headers={'X-Render-Ms': f'{(time.perf_counter() - start) * 1000:.1f}'})

    @app.post('/api/raw/open')
    def raw_open(body: RawPhoto):
        photo = allowed_path(body.photo, readable_photos, imaging.RAW_EXTS)
        if not photo.is_file():
            raise HTTPException(404, 'RAW 사진을 찾을 수 없습니다.')
        with raw.lock:
            return raw.open(photo)

    @app.post('/api/raw/render')
    def raw_render(body: RawRender):
        photo = allowed_path(body.photo, readable_photos, imaging.RAW_EXTS)
        start = time.perf_counter()
        with raw.lock:
            raw.ensure(body.session, photo)
            if not body.original and body.seq < raw.latest_seq:
                raise HTTPException(409, '더 최신 미리보기 요청이 있습니다.')
            if not body.original:
                raw.latest_seq = body.seq
            content = raw.render(body)
        return Response(content, media_type='image/jpeg', headers={
            'X-Render-Ms': f'{(time.perf_counter() - start) * 1000:.1f}', 'X-Render-Seq': str(body.seq)})

    @app.post('/api/raw/reset')
    def raw_reset(body: RawAction = RawAction()):
        with raw.lock:
            raw.ensure(body.session)
            raw.command('develop.reset')
            raw.profile_source = None
            raw.session = uuid4().hex
            return raw.snapshot()

    @app.post('/api/raw/preset')
    def raw_preset(body: RawPreset):
        path = allowed_path(body.lut, readable_luts, {'.cube', '.xmp', '.lrtemplate'})
        if path.suffix.lower() == '.cube':
            path = indexed_source(path, readable_luts) or path
        with raw.lock:
            raw.ensure(body.session)
            return raw.preset(path)

    def save_raw(body, folder):
        name = body.name[:-4] if body.name.lower().endswith('.xmp') else body.name
        name = cube_name(name)[:-5] + '.xmp'
        target = folder / name
        with SAVE_LOCK:
            if target.is_symlink():
                raise HTTPException(403, '심볼릭 링크에는 저장할 수 없습니다.')
            target = allowed_path(str(target), [folder], {'.xmp'})
            if target.exists() and not body.overwrite:
                raise HTTPException(409, '같은 이름의 파일이 있습니다. 덮어쓰기를 선택하세요.')
            with raw.lock:
                raw.ensure(body.session)
                content = raw.export()
            write_new(target, content, body.overwrite)
        return {'path': str(target)}

    @app.post('/api/raw/export')
    def raw_export(body: RawSave):
        return save_raw(body, custom)

    @app.post('/api/raw/install')
    def raw_install(body: RawSave):
        if install is None:
            raise HTTPException(403, '설치 폴더가 설정되지 않았습니다.')
        return save_raw(body, install)

    @app.post('/api/upload')
    async def upload(request: Request, name: str):
        suffix = Path(name).suffix.lower()
        if suffix not in PHOTO_EXTS:
            raise HTTPException(400, '지원하지 않는 사진 형식입니다.')
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, '사진은 40 MB 이하로 올려주세요.')
        def store():
            uploads.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(suffix=suffix, dir=uploads, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            try:
                image = imaging.develop(temporary)
                image.close()
                target = allowed_path(str(uploads / (uuid4().hex + suffix)), [uploads], PHOTO_EXTS)
                os.replace(temporary, target)
                return {'path': str(target), 'name': target.name}
            finally:
                temporary.unlink(missing_ok=True)
        return await run_in_threadpool(store)

    def save(body, folder, installing=False):
        name = cube_name(body.name)
        target = folder / name
        if target.is_symlink():
            raise HTTPException(403, '심볼릭 링크에는 저장할 수 없습니다.')
        target = allowed_path(str(target), [folder], {'.cube'})
        path = source(body)
        if not installing and path == target:
            raise HTTPException(409, '레시피의 기본 LUT를 덮어쓸 수 없습니다. 다른 이름으로 저장하세요.')
        content = bake(body.recipe, path, body.mode, binary)[0]
        sidecar = target.with_suffix('.recipe.json')
        saved = SavedRecipe(**body.recipe.model_dump(), mode=body.mode if path else 'none',
                            base_lut=str(path) if path else None)
        with SAVE_LOCK:
            targets = [target] if installing else [target, sidecar]
            for destination in targets:
                if destination.is_symlink():
                    raise HTTPException(403, '심볼릭 링크에는 저장할 수 없습니다.')
                allowed_path(str(destination), [folder])
                if destination.exists() and not body.overwrite:
                    raise HTTPException(409, '같은 이름의 파일이 있습니다. 덮어쓰기를 선택하세요.')
            if installing:
                write_new(target, content, body.overwrite)
            else:
                previous = sidecar.read_bytes() if sidecar.exists() else None
                write_new(sidecar, (saved.model_dump_json(indent=2) + '\n').encode(), body.overwrite)
                try:
                    write_new(target, content, body.overwrite)
                except Exception:
                    if previous is None:
                        sidecar.unlink()
                    else:
                        write_new(sidecar, previous, True)
                    raise
        return {'path': str(target)}

    @app.post('/api/save')
    def save_cube(body: SaveRequest):
        return save(body, custom)

    @app.post('/api/install')
    def install_cube(body: SaveRequest):
        if install is None:
            raise HTTPException(403, '설치 폴더가 설정되지 않았습니다.')
        return save(body, install, installing=True)

    @app.post('/api/command')
    def command(body: SaveRequest):
        path = source(body)
        # Persist the exact recipe behind a copied command; never refer to a stale save.
        target = custom / (Path(cube_name(body.name)).stem + '-' + uuid4().hex[:8] + '.recipe.json')
        saved = SavedRecipe(**body.recipe.model_dump(), mode=body.mode if path else 'none',
                            base_lut=str(path) if path else None)
        write_new(allowed_path(str(target), [custom]), (saved.model_dump_json(indent=2) + '\n').encode(), False)
        executable = [sys.executable] if FROZEN else ['lut-lab']
        args = [*executable, 'bake', str(target), '-o', str(custom / cube_name(body.name))]
        if binary:
            args.extend(['--rr-lut', str(binary)])
        return {'command': shlex.join(args), 'recipe': str(target)}

    return app


def argument_parser(*, desktop=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rr-lut', type=Path)
    parser.add_argument('--lightcraft', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--install-dir', type=Path)
    parser.add_argument('--port', type=int, default=0 if desktop else 8765)
    parser.add_argument('--lut-dir', type=Path, action='append')
    parser.add_argument('--photo-dir', type=Path, action='append')
    if desktop:
        parser.add_argument('--check', action='store_true', help='서버 설정을 출력하고 창 없이 종료')
    return parser


def app_from_args(args):
    return create_app(args.lut_dir, args.photo_dir, rr_lut=args.rr_lut, lightcraft=args.lightcraft,
                      data_dir=args.data_dir, install=args.install_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    if len(sys.argv) > 1 and sys.argv[1] == 'bake':
        parser.add_argument('command')
        parser.add_argument('recipe', type=Path)
        parser.add_argument('-o', '--output', type=Path, required=True)
        parser.add_argument('--rr-lut', type=Path)
        parser.add_argument('--overwrite', action='store_true')
        args = parser.parse_args()
        try:
            saved = SavedRecipe.model_validate_json(args.recipe.read_text())
            path = Path(saved.base_lut).expanduser().resolve() if saved.base_lut else None
            recipe = Recipe(**saved.model_dump(include={'version', 'adjustments', 'amount'}))
            content = bake(recipe, path, saved.mode, args.rr_lut)[0]
            write_new(args.output, content, args.overwrite)
        except (OSError, ValueError, HTTPException) as error:
            parser.exit(1, str(getattr(error, 'detail', error)) + '\n')
        return
    args = argument_parser().parse_args()
    import uvicorn
    uvicorn.run(app_from_args(args), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
