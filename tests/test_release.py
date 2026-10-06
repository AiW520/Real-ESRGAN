import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from backend.app.security import ServiceSecurity, ServiceSettings
from backend.app.jobs import JobStore, Job, QueueFull
from backend.app.requests import request_identity
import backend.app.jobs as jobs
import backend.app.main as main
from tests.test_api import image_bytes, wait
from tests.test_generation import base_fixture


def test_upload_idempotency_and_conflict(client):
    api, store = client
    headers = {'Idempotency-Key': 'platform-request-001'}
    options = {'processing_mode': 'resize', 'scale': 0.5}
    def submit(data=options):
        return api.post('/api/upscale', headers=headers, files={'file': ('x.png', image_bytes(), 'image/png')}, data=data)
    first = submit()
    assert first.status_code == 200, first.text
    job_id = first.json()['job_id']
    assert wait(api, job_id)['status'] == 'completed'
    for i in range(4):
        placeholder = Job(f'{i:032x}')
        store.items[placeholder.job_id] = placeholder
    duplicate = submit()
    assert duplicate.status_code == 200 and duplicate.json()['job_id'] == job_id
    assert submit({**options, 'scale': 0.25}).status_code == 409
    public = api.get(f'/api/upscale/{job_id}/status').text
    assert 'request_fingerprint' not in public and 'request_key' not in public
    assert not list((store.data / 'uploads').iterdir())


def test_concurrent_generation_exactly_one_submission(client, monkeypatch):
    api, store = client
    calls = []
    monkeypatch.setattr(main, 'readiness', lambda: {'available': True})
    def generate(*args):
        calls.append(1)
        return base_fixture(*args)
    monkeypatch.setattr(jobs, 'generate_base_image', generate)
    def submit():
        return api.post('/api/generate', headers={'Idempotency-Key': 'generation-request-001'},
                        json={'prompt': 'private prompt not stored', 'output_level': 'native'})
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: submit(), range(4)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    ids = {r.json()['job_id'] for r in responses}
    assert len(ids) == 1
    job_id = ids.pop()
    assert wait(api, job_id)['status'] == 'completed'
    assert calls == [1]
    persisted = (store.data / 'jobs' / f'{job_id}.json').read_text('utf-8')
    assert 'private prompt not stored' not in persisted and 'generation-request-001' not in persisted


def test_idempotency_survives_restart(tmp_path):
    first = JobStore(tmp_path / 'tasks')
    identity = request_identity('generation-request-001', 'generate', {'prompt': 'sensitive'})
    job = Job('e' * 32, status='failed', **identity)
    first._save(job)
    first.executor.shutdown()
    second = JobStore(first.data)
    second.startup()
    try:
        assert second.lookup_request(identity).job_id == job.job_id
        assert second.lookup_request(identity).status == 'failed'  # Never silently resubmit a possibly billed request.
    finally:
        second.shutdown()


def test_failed_enqueue_does_not_leave_phantom_job(tmp_path, monkeypatch):
    store = JobStore(tmp_path / 'store')
    def fail(*args, **kwargs):
        raise RuntimeError('executor unavailable')
    monkeypatch.setattr(store.executor, 'submit', fail)
    try:
        with pytest.raises(RuntimeError):
            store.create_generated('fixture', 'square', {'model':'RealESRGAN_x4plus'})
        assert not store.items
        assert not list((store.data / 'jobs').glob('*.json'))
    finally:
        store.shutdown()


async def echo(request: Request):
    return JSONResponse({'size': len(await request.body())})


def secure_app(limit=30, max_body=128):
    settings = ServiceSettings(production=True, api_token='test-only-service-token-' + 'a' * 32,
                               request_limit=limit, generation_enabled=False)
    app = Starlette(routes=[Route('/api/upscale', echo, methods=['POST']), Route('/api/status', echo),
                            Route('/healthz', echo)])
    return ServiceSecurity(app, settings, max_body), {'Authorization': 'Bearer ' + settings.api_token}


def test_production_security_and_limits():
    app, auth = secure_app(limit=1)
    with TestClient(app) as api:
        assert api.get('/api/status').status_code == 401
        assert api.get('/api/status', headers=auth).status_code == 200
        assert api.get('/api/status', headers={**auth, 'Authorization':'Bearer wrong'}).status_code == 401
        assert api.get('/').status_code == 404
        assert api.get('/healthz').status_code == 200
        assert api.post('/api/upscale', headers={**auth, 'origin':'https://other.example'}, content=b'123').status_code == 403
        assert api.post('/api/upscale', headers=auth, content=b'x' * 129).status_code == 413
        assert api.post('/api/upscale', headers=auth, content=b'ok').status_code == 200
        response = api.post('/api/upscale', headers=auth, content=b'ok')
        assert response.status_code == 429 and response.headers['retry-after'] == '60'


def test_chunked_body_limit_without_content_length():
    app, auth = secure_app()
    messages = [{'type':'http.request', 'body': b'a' * 80, 'more_body': True},
                {'type':'http.request', 'body': b'b' * 80, 'more_body': False}]
    sent = []
    async def receive():
        return messages.pop(0)
    async def send(message):
        sent.append(message)
    scope = {'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','scheme':'http',
             'path':'/api/upscale','raw_path':b'/api/upscale','query_string':b'',
             'headers':[(b'host',b'localhost'),(b'authorization',auth['Authorization'].encode())],
             'client':('127.0.0.1',1234),'server':('localhost',8000)}
    asyncio.run(app(scope, receive, send))
    assert next(m for m in sent if m['type'] == 'http.response.start')['status'] == 413


def test_production_configuration_fails_closed():
    with pytest.raises(RuntimeError):
        ServiceSettings(production=True).validate()
    with pytest.raises(RuntimeError):
        ServiceSettings(production=True, api_token='x'*32, allowed_hosts=('*',)).validate()


def test_generation_can_be_disabled(client, monkeypatch):
    api, _ = client
    monkeypatch.setattr(main, 'settings', replace(main.settings, generation_enabled=False))
    assert not api.get('/api/generation/config').json()['available']
    assert api.post('/api/generate', json={'prompt':'must not be sent'}).status_code == 403


def test_readiness_and_cross_thread_shutdown_release_lock(client):
    api, store = client
    assert api.get('/readyz').status_code == 200
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(store.shutdown).result(timeout=10)
    assert api.get('/readyz').status_code == 503
    replacement = JobStore(store.data)
    replacement.startup()
    replacement.shutdown()
