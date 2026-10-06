"""Launch an isolated authenticated production process on loopback and verify real inference."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import ROOT
from backend.client import UpscalerClient
from PIL import Image
import numpy as np
import httpx

out = ROOT / 'data/validation/production' / uuid.uuid4().hex[:12]
out.mkdir(parents=True)
# Synthetic numeric fixture makes the release test independent of user images.
fixture = out / 'input.png'
pixels = np.zeros((64, 96, 4), dtype=np.uint8)
pixels[:, :, :3] = (18, 100, 210)
pixels[:, :, 3] = np.linspace(0, 255, 96, dtype=np.uint8)[None, :]
Image.fromarray(pixels).save(fixture)
token = secrets.token_urlsafe(48)
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
base_url = f'http://127.0.0.1:{port}'
env = {**os.environ, 'PYTHONUTF8': '1', 'UPSCALER_PRODUCTION': '1',
       'UPSCALER_API_TOKEN': token, 'UPSCALER_DATA_DIR': str(out / 'runtime'),
       'UPSCALER_MODELS_DIR': str(ROOT / 'models'), 'UPSCALER_ALLOWED_HOSTS': '127.0.0.1',
       'UPSCALER_ENABLE_GENERATION': '0', 'UPSCALER_SUBMISSIONS_PER_MINUTE': '30'}
log = (out / 'server.log').open('w', encoding='utf-8')
process = None


def start():
    process = subprocess.Popen([sys.executable, '-m', 'backend.serve', '--port', str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError('Isolated production process failed to start; inspect the sanitized server log.')
        try:
            if httpx.get(base_url + '/readyz', timeout=2, trust_env=False).status_code == 200:
                return process
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    process.terminate()
    process.wait(timeout=15)
    raise TimeoutError('Production worker did not become ready.')


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


try:
    preflight = subprocess.run([sys.executable, 'scripts/preflight.py', '--output', str(out / 'preflight.json')],
                              cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8')
    assert preflight.returncode == 0, 'Preflight failed; inspect the report.'
    process = start()
    with httpx.Client(base_url=base_url, timeout=30, trust_env=False) as unauth:
        assert unauth.get('/healthz').status_code == 200
        assert unauth.get('/api/health').status_code == 401
        assert unauth.get('/api/operations').status_code == 401
        assert unauth.get('/api/upscale/unknown/download').status_code == 401
        assert unauth.get('/').status_code == 404
        assert unauth.get('/docs').status_code == 404
        invalid_host = unauth.get('/healthz', headers={'host':'wrong.example'})
        assert invalid_host.status_code == 400, f'Host validation returned {invalid_host.status_code}: {invalid_host.text[:200]}'
    report = {'authentication': True, 'host_validation': True, 'web_ui_disabled': True,
              'preflight': True, 'paid_generation_calls': 0, 'cases': []}
    with UpscalerClient(base_url, api_token=token) as client:
        assert client.http.get('/api/generation/config').json()['available'] is False
        assert client.http.post('/api/generate', json={'prompt':'not sent'}).status_code == 403
        for mode, scale, expected in [('upscale', 2, (192, 128)), ('resize', 0.5, (48, 32)), ('ai_resize', 0.5, (48, 32))]:
            request_id = 'release-' + mode + '-' + uuid.uuid4().hex
            options = {'mode': mode, 'scale':scale, 'resolution':'scale', 'request_id':request_id}
            job_id = client.submit(fixture, **options)
            assert client.submit(fixture, **options) == job_id
            state = client.wait(job_id, timeout=180)
            path = client.download(job_id, out / f'{mode}.png')
            with Image.open(path) as image:
                image.load()
                assert image.size == expected and image.mode == 'RGBA'
            assert (state['tiles_total'] > 0) == (mode != 'resize')
            report['cases'].append({'mode': mode, 'dimensions': expected, 'milliseconds':state['processing_ms'], 'device':state['device']})
            print(mode + ': real inference/resize and authenticated download passed', flush=True)
        # Persistence under process termination/restart; production restart must not resubmit completed work.
        stop(process)
        process = None
        process = start()
        assert client.submit(fixture, **options) == job_id
        assert client.status(job_id)['status'] == 'completed'
        assert client.http.get('/api/operations').json()['jobs']['completed'] == 3
        report['idempotency_across_restart'] = True
    (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Production smoke passed:', out, flush=True)
finally:
    if process is not None:
        stop(process)
    log.close()
