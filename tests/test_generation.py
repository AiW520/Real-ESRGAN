import io
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

import backend.app.jobs as jobs
import backend.app.main as main
import backend.app.generation as generation
from backend.app.jobs import Job, JobCancelled, JobStore
from tests.test_api import wait


def base_fixture(prompt, aspect, destination, cancel):
    cancel()
    dimensions = {'square': (1024, 1024), 'landscape': (1536, 1024), 'portrait': (1024, 1536)}[aspect]
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', dimensions, (90, 160, 40)).save(destination)
    return dimensions


@pytest.mark.parametrize('level,aspect', [('native', 'square'), ('2K', 'landscape'), ('4K', 'portrait')])
def test_generation_pipeline(client, monkeypatch, level, aspect):
    api, store = client
    monkeypatch.setattr(main, 'readiness', lambda: {'available': True, 'model': 'gpt-image-2'})
    monkeypatch.setattr(jobs, 'generate_base_image', base_fixture)
    calls = []
    monkeypatch.setattr(jobs.manager, 'load', lambda *args, **kwargs: SimpleNamespace(device='cpu'))

    def inference(rgb, name, model, size, tile, workspace, progress, cancel):
        calls.append((rgb.shape, name, size))
        cancel()
        progress(1, 1)
        return np.full((size[1], size[0], 3), 120, dtype=np.uint8), [{'tile_size': 128, 'device': 'cpu'}]

    monkeypatch.setattr(jobs, 'process_with_fallback', inference)
    response = api.post('/api/generate', json={'prompt': 'private prompt', 'aspect': aspect, 'output_level': level})
    assert response.status_code == 200
    status = wait(api, response.json()['job_id'])
    assert status['status'] == 'completed', status
    assert 'base_file' not in status and 'output_file' not in status
    assert status['workflow'] == 'generate'
    assert len(calls) == (0 if level == 'native' else 1)
    base = api.get(f'/api/upscale/{status["job_id"]}/base')
    result = api.get(f'/api/upscale/{status["job_id"]}/download')
    assert base.status_code == result.status_code == 200
    with Image.open(io.BytesIO(base.content)) as image:
        assert image.size == (status['base_width'], status['base_height'])
    with Image.open(io.BytesIO(result.content)) as image:
        assert image.size == (status['output_width'], status['output_height'])
    assert 'private prompt' not in (store.data / 'jobs' / f'{status["job_id"]}.json').read_text('utf-8')
    assert not list((store.data / 'uploads').iterdir())
    assert not list(path for path in (store.data / 'jobs').iterdir() if path.is_dir())


@pytest.mark.parametrize('body', [{'prompt': ''}, {'prompt': 'x', 'aspect': 'wide'}, {'prompt': 'x', 'output_level': '8K'}, {'prompt': 'x', 'model': 'fake'}, {'prompt': 'x', 'quality': 101}])
def test_generation_invalid_options(client, body):
    api, _ = client
    assert api.post('/api/generate', json=body).status_code == 422


def test_generation_unavailable(client, monkeypatch):
    api, store = client
    monkeypatch.setattr(main, 'readiness', lambda: {'available': False, 'reason': 'missing configuration'})
    assert api.post('/api/generate', json={'prompt': 'x'}).status_code == 503
    assert not store.items


@pytest.mark.parametrize('cancelled', [False, True])
def test_generation_failure_no_retry(client, monkeypatch, cancelled):
    api, store = client
    calls = []
    monkeypatch.setattr(main, 'readiness', lambda: {'available': True})

    def fail(*args):
        calls.append(1)
        raise JobCancelled('cancelled') if cancelled else generation.GenerationError('provider failed')

    monkeypatch.setattr(jobs, 'generate_base_image', fail)
    response = api.post('/api/generate', json={'prompt': 'x'})
    status = wait(api, response.json()['job_id'])
    assert status['status'] == ('cancelled' if cancelled else 'failed')
    assert calls == [1]
    assert not list((store.data / 'outputs').iterdir())


@pytest.mark.parametrize('returned_format', ['PNG', 'JPEG'])
def test_bundled_script_wrapper(tmp_path, monkeypatch, returned_format):
    script = tmp_path / 'fixture_provider.py'
    script.write_text(
        "import argparse,json,pathlib\nfrom PIL import Image\n"
        "p=argparse.ArgumentParser()\np.add_argument('--output-dir')\np.add_argument('--filename')\n"
        "a,_=p.parse_known_args()\nf=pathlib.Path(a.output_dir)/a.filename\n"
        f"Image.new('RGB',(32,24)).save(f,format='{returned_format}')\n"
        "print(json.dumps({'code':0,'saved_files':[str(f)]}))\n", encoding='utf-8')
    monkeypatch.setenv('UPSCALER_TUZI_SCRIPT', str(script))
    destination = tmp_path / 'some.base.png'
    assert generation.generate_base_image('fixture prompt', 'square', destination) == (32, 24)
    assert destination.is_file()
    with Image.open(destination) as image:
        assert image.format == 'PNG'
    assert not (tmp_path / 'some-base.png').exists()


def test_wrapper_rejects_external_path(tmp_path, monkeypatch):
    script = tmp_path / 'fixture.py'
    script.write_text('pass', encoding='ascii')
    foreign = tmp_path / 'foreign.png'
    Image.new('RGB', (32, 24)).save(foreign)
    monkeypatch.setenv('UPSCALER_TUZI_SCRIPT', str(script))
    process = SimpleNamespace(returncode=0, communicate=lambda **kwargs: (json.dumps({'code': 0, 'saved_files': [str(foreign)]}), ''))
    monkeypatch.setattr(generation.subprocess, 'Popen', lambda *args, **kwargs: process)
    with pytest.raises(generation.GenerationError):
        generation.generate_base_image('x', 'square', tmp_path / 'expected' / 'base.png')
    assert foreign.is_file()


def saved_base(store, state='failed', width=1024):
    job = Job('a' * 32, workflow='generate', status=state, aspect='square',
              base_width=width, base_height=width)
    path = store.data / 'outputs' / f'{job.job_id}.base.png'
    Image.new('RGB', (width, width), (90, 160, 40)).save(path)
    job.base_file = str(path)
    store.items[job.job_id] = job
    store._save(job)
    return job


def mock_sr(monkeypatch, fail=False):
    monkeypatch.setattr(jobs.manager, 'load', lambda *args, **kwargs: SimpleNamespace(device='cpu'))
    def process(rgb, name, model, size, tile, workspace, progress, cancel):
        if fail:
            raise RuntimeError('inference unavailable')
        cancel()
        progress(1, 1)
        return np.full((size[1], size[0], 3), 130, dtype=np.uint8), [{'tile_size': 128, 'device': 'cpu'}]
    monkeypatch.setattr(jobs, 'process_with_fallback', process)


def test_continue_without_generation_or_credentials(client, monkeypatch):
    api, store = client
    original = saved_base(store)
    def forbidden(*args, **kwargs):
        pytest.fail('Continuing a base image must not call the paid provider or readiness check.')
    monkeypatch.setattr(main, 'readiness', forbidden)
    monkeypatch.setattr(jobs, 'generate_base_image', forbidden)
    mock_sr(monkeypatch)
    response = api.post(f'/api/upscale/{original.job_id}/continue', json={'output_level': '2K'})
    assert response.status_code == 200, response.text
    status = wait(api, response.json()['job_id'])
    assert status['status'] == 'completed', status
    assert status['base_available'] and status['workflow'] == 'base-upscale'
    assert (status['output_width'], status['output_height']) == (1920, 1920)
    child = store.get(status['job_id'])
    assert child.base_file != original.base_file
    Path(original.base_file).unlink()
    assert api.get(f'/api/upscale/{child.job_id}/base').status_code == 200
    second = api.post(f'/api/upscale/{child.job_id}/continue', json={'output_level': '4K'})
    assert second.status_code == 200
    assert wait(api, second.json()['job_id'])['status'] == 'completed'


def test_continue_validation_and_capacity(client):
    api, store = client
    job = saved_base(store, width=64)
    assert api.post(f'/api/upscale/{job.job_id}/continue', json={'output_level': '4K'}).status_code == 400
    assert api.post(f'/api/upscale/{job.job_id}/continue', json={'output_level': 'native'}).status_code == 422
    assert api.post('/api/upscale/missing/continue', json={}).status_code == 400
    job.base_file = str(store.data.parent / 'outside.png')
    assert api.post(f'/api/upscale/{job.job_id}/continue', json={}).status_code == 400
    for i in range(4):
        active = Job(f'{i:032x}')
        store.items[active.job_id] = active
    assert api.post(f'/api/upscale/{job.job_id}/continue', json={}).status_code == 429


def test_sr_failure_retains_base_and_allows_continue(client, monkeypatch):
    api, store = client
    monkeypatch.setattr(main, 'readiness', lambda: {'available': True})
    monkeypatch.setattr(jobs, 'generate_base_image', base_fixture)
    mock_sr(monkeypatch, fail=True)
    response = api.post('/api/generate', json={'prompt': 'fixture', 'aspect': 'square'})
    failed = wait(api, response.json()['job_id'])
    assert failed['status'] == 'failed' and failed['base_available']
    mock_sr(monkeypatch)
    continuation = api.post(f'/api/upscale/{failed["job_id"]}/continue', json={'output_level': '2K'})
    assert wait(api, continuation.json()['job_id'])['status'] == 'completed'


def test_restart_retains_completed_base(tmp_path):
    store = JobStore(tmp_path / 'persisted')
    job = saved_base(store, state='processing')
    store.executor.shutdown()
    restored = JobStore(store.data)
    restored.startup()
    try:
        previous = restored.get(job.job_id)
        assert previous.status == 'failed'
        assert Path(previous.base_file).is_file()
        assert previous.base_width == 1024
    finally:
        restored.shutdown()
