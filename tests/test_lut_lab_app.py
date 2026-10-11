"""Offline app regression checks; no sockets, GUI, Metal or external writes."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from lutlab import server as lab
from lutlab import app as desktop


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        with patch.object(Path, 'home', return_value=root), patch.object(lab, 'FROZEN', True):
            settings, errors = lab.data_config()
            assert settings == {} and not errors
            assert not lab.app_config_dir().exists()
            config_path = lab.app_config_dir() / 'config.json'
            config_path.parent.mkdir(parents=True)
            settings = {'install_dir': str(root / 'install')}
            config_path.write_text(json.dumps(settings))
            app = lab.create_app(rr_lut=root / 'missing', lightcraft=root / 'missing')
            with TestClient(app, base_url='http://127.0.0.1') as client:
                config = client.get('/api/config').json()
                assert not config['data_errors']
                assert config['custom'] == str(lab.app_config_dir() / 'custom')
                assert config['install'] == str(root / 'install')
                assert client.get('/api/photos').json() == []
                assert client.get('/').status_code == 200
                assert client.get('/fonts/Pretendard-Regular.woff2').status_code == 200
            config_path.write_text('{broken')
            assert lab.data_config()[1]
            config_path.write_text('{"install_dir": 3}')
            assert lab.data_config()[1]
            config_path.write_text(json.dumps(settings))

            bundle = root / 'Resources'
            (bundle / 'bin').mkdir(parents=True)
            for name, variable in [('rr-lut', 'RR_LUT'), ('lightcraft-cli', 'LIGHTCRAFT_CLI')]:
                bundled = bundle / 'bin' / name
                bundled.touch()
                with patch.object(lab, 'RESOURCES', bundle), patch.dict(lab.os.environ, {variable: ''}), \
                        patch.object(lab.shutil, 'which', return_value=None):
                    assert lab.resolve_binary(None, variable, name) == bundled
                    with patch.dict(lab.os.environ, {variable: str(root / 'env')}):
                        assert lab.resolve_binary(None, variable, name) == root / 'env'
                        assert lab.resolve_binary(root / 'cli', variable, name) == root / 'cli'
                    bundled.unlink()
                    assert lab.resolve_binary(None, variable, name) is None
                    with patch.object(lab.shutil, 'which', return_value=str(root / 'path-engine')):
                        assert lab.resolve_binary(None, variable, name) == root / 'path-engine'

            app = lab.create_app([], [], lightcraft=root / 'missing')
            desktop.desktop_storage(app)
            with patch.object(app.state.raw, 'close') as close:
                with TestClient(app, base_url='http://127.0.0.1') as client:
                    state = {'lut-lab-favorites': '["example.cube"]', 'lut-lab-drop-zone-collapsed': 'true'}
                    assert client.post('/api/app-storage', json=state).status_code == 200
                    page = client.get('/').text
                    assert 'example.cube' in page and 'Storage.prototype.setItem' in page
                    assert client.get('/', headers={'Host': 'untrusted.test'}).status_code == 400
                    assert client.post('/api/app-storage', json={'outside': 'no'}).status_code == 422
                    assert client.post('/api/app-storage', content='x'*65537).status_code == 413
                    assert client.post('/api/app-storage', json=state, headers={'Origin': 'https://untrusted.test'}).status_code == 403
                    assert json.loads((lab.app_config_dir() / 'preferences.json').read_text()) == state
                    html = root / 'app.html'
                    html.write_text(page)
                    subprocess.run(['node', str(Path(__file__).with_name('test_lut_lab_app_ui.cjs')), str(html)], check=True)
                close.assert_called_once()
            # A second app/port restores the same preferences.
            second = lab.create_app([], [])
            desktop.desktop_storage(second)
            with TestClient(second, base_url='http://127.0.0.1:9999') as client:
                assert 'example.cube' in client.get('/').text
                (lab.app_config_dir() / 'preferences.json').write_text('{broken')
                assert '앱 설정을 읽을 수 없습니다' in client.get('/').text

    # Exercise the actual launcher's shutdown path, including a window/start failure.
    stopped = Event()
    server = Mock(started=True, should_exit=False)
    def serve(**kwargs):
        while not server.should_exit:
            stopped.wait(.01)
        stopped.set()
    server.run = serve
    sock = Mock()
    sock.getsockname.return_value = ('127.0.0.1', 12345)
    socket_context = Mock(__enter__=Mock(return_value=sock), __exit__=Mock(return_value=False))
    response = Mock(__enter__=Mock(return_value=io.StringIO('{"available": false}')), __exit__=Mock(return_value=False))
    opener = Mock()
    opener.open.return_value = response
    with patch.object(desktop.socket, 'socket', return_value=socket_context), \
            patch.object(desktop.uvicorn, 'Server', return_value=server), \
            patch.object(desktop, 'build_opener', return_value=opener):
        try:
            with desktop.running_server(lab.create_app([], [], data_dir='/tmp/lut-lab-launch-test')) as (url, config):
                assert url == 'http://127.0.0.1:12345' and config == {'available': False}
                sock.bind.assert_called_once_with(('127.0.0.1', 0))
                raise RuntimeError('window failed')
        except RuntimeError as error:
            assert str(error) == 'window failed'
        assert stopped.is_set() and server.should_exit

    @contextmanager
    def fake_server(app, port):
        yield 'http://127.0.0.1:12345', {'available': False}
    with patch.object(sys, 'argv', ['lut-lab-app', '--check', '--data-dir', '/tmp/lut-lab-launch-test']), \
            patch.object(desktop, 'running_server', fake_server), patch.object(sys, 'stdout', new_callable=io.StringIO) as output:
        desktop.main()
        assert json.loads(output.getvalue()) == {'available': False}
        assert 'webview' not in sys.modules
    assert lab.argument_parser().parse_args([]).port == 8765
    assert lab.argument_parser(desktop=True).parse_args([]).port == 0
    print('PASS: frozen config/resources/binary precedence, missing paths, persistence across ports, input bounds/origin, lifespan cleanup, launcher shutdown and windowless check')


if __name__ == '__main__':
    main()
