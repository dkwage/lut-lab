#!/usr/bin/env python3
"""Native LUT Lab launcher; --check runs the same server without a window."""
from contextlib import contextmanager
import json
import socket
import sys
from threading import Thread
import time
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
import uvicorn

from lutlab import server as lab


def desktop_storage(app):
    """Keep Lab preferences across launches even though the localhost port changes."""
    path = app.state.data_dir / 'preferences.json'

    @app.middleware('http')
    async def restore_preferences(request, call_next):
        response = await call_next(request)
        if request.url.path != '/' or request.method != 'GET' or response.status_code != 200:
            return response
        warning = None
        try:
            saved = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(saved, dict):
                raise ValueError('preferences must be an object')
            saved = {k: v for k, v in saved.items() if k.startswith('lut-lab-') and isinstance(v, str)}
        except (OSError, ValueError) as error:
            saved = {}
            warning = f'앱 설정을 읽을 수 없습니다: {path} · {error}'
        snapshot = json.dumps(saved, ensure_ascii=False).replace('<', '\\u003c')
        script = """<script>
try {
  const saved=SNAPSHOT;
  for(const key of Object.keys(localStorage))if(key.startsWith('lut-lab-'))localStorage.removeItem(key);
  for(const [key,value] of Object.entries(saved))localStorage.setItem(key,value);
  const setItem=Storage.prototype.setItem;
  let writes=Promise.resolve();
  Storage.prototype.setItem=function(key,value){
    setItem.call(this,key,value);
    if(this!==localStorage||!key.startsWith('lut-lab-'))return;
    const state=Object.fromEntries(Object.keys(localStorage).filter(k=>k.startsWith('lut-lab-')).map(k=>[k,localStorage.getItem(k)]));
    writes=writes.then(async()=>{
      const response=await fetch('/api/app-storage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(state),keepalive:true});
      if(!response.ok)throw Error('앱 설정을 저장하지 못했습니다.');
    }).catch(error=>{document.getElementById('status').textContent=error.message;});
  };
} catch(error) { console.error('앱 설정 복원 실패',error); }
</script>""".replace('SNAPSHOT', snapshot)
        html = (lab.STATIC / 'index.html').read_text()
        if warning:
            import html as html_module
            html = html.replace('<main>', f'<p role="alert">{html_module.escape(warning)}</p><main>', 1)
        return HTMLResponse(html.replace('<script>', script + '<script>', 1))

    @app.post('/api/app-storage')
    async def save_preferences(request: Request):
        # Bound this local write endpoint before decoding user-controlled JSON.
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 65536:
                raise HTTPException(413, '앱 설정이 너무 큽니다.')
        try:
            state = json.loads(data)
        except ValueError as error:
            raise HTTPException(422, '앱 설정은 JSON 객체여야 합니다.') from error
        if not isinstance(state, dict) or any(not k.startswith('lut-lab-') or not isinstance(v, str) for k, v in state.items()):
            raise HTTPException(422, '올바른 Lab 설정이 필요합니다.')
        lab.write_new(path, (json.dumps(state, ensure_ascii=False) + '\n').encode(), True)
        return {'saved': True}


@contextmanager
def running_server(app, port=0):
    # Keep the bound socket open so another process cannot steal the selected port.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                log_config=None, access_log=False, timeout_graceful_shutdown=30))
        thread = Thread(target=server.run, kwargs={'sockets': [sock]}, name='lut-lab-server')
        thread.start()
        try:
            url = f'http://127.0.0.1:{sock.getsockname()[1]}'
            opener = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if not thread.is_alive():
                    raise RuntimeError('LUT Lab 서버가 시작되지 않았습니다.')
                if server.started:
                    try:
                        with opener.open(url + '/api/config', timeout=45) as response:
                            config = json.load(response)
                        break
                    except URLError:
                        pass
                time.sleep(.05)
            else:
                raise RuntimeError('LUT Lab 서버 응답 시간이 초과되었습니다.')
            yield url, config
        finally:
            server.should_exit = True
            thread.join()  # Wait for the FastAPI lifespan to close the LightCraft session.


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'bake':
        return lab.main()
    parser = lab.argument_parser(desktop=True)
    args = parser.parse_args()
    app = lab.app_from_args(args)
    if not args.check:
        desktop_storage(app)
    try:
        with running_server(app, args.port) as (url, config):
            if args.check:
                print(json.dumps(config, ensure_ascii=False, indent=2))
                return
            import webview
            webview.create_window('LUT Lab', url, width=1600, height=1000, min_size=(1280, 800))
            webview.start(private_mode=False)
    except (OSError, RuntimeError) as error:
        parser.exit(1, f'LUT Lab 실행 실패: {error}\n')


if __name__ == '__main__':
    main()
