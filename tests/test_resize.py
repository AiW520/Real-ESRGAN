import io
import numpy as np
import pytest
from PIL import Image

import backend.app.jobs as jobs
import backend.app.main as main
from backend.app.image_processor import target_size
from tests.test_api import wait
from tests.test_generation import base_fixture, saved_base


@pytest.mark.parametrize('scale,size', [(0.5, (51, 34)), (0.25, (25, 17))])
def test_shrink_dimensions(scale, size):
    assert target_size(101, 67, scale=scale, processing_mode='resize') == size
    assert target_size(101, 67, scale=scale, processing_mode='ai_resize') == size
    with pytest.raises(ValueError):
        target_size(101, 67, scale=scale)
    with pytest.raises(ValueError):
        target_size(101, 67, scale=scale, target_width=202, processing_mode='resize')
    with pytest.raises(ValueError):
        target_size(1024, 1024, scale=0.5, target_width=1, processing_mode='ai_resize')


def source_bytes():
    image = Image.new('RGBA', (48, 32), (255, 0, 0, 0))
    image.paste((30, 120, 200, 255), (12, 8, 36, 24))
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return image, stream.getvalue()


def test_resize_never_loads_model_and_preserves_alpha(client, monkeypatch):
    api, _ = client
    def forbidden(*args, **kwargs):
        pytest.fail('Plain resize must not load any AI model.')
    monkeypatch.setattr(jobs.manager, 'load', forbidden)
    source, data = source_bytes()
    response = api.post('/api/upscale', files={'file': ('test.png', data, 'image/png')},
                        data={'processing_mode': 'resize', 'scale': 0.5})
    assert response.status_code == 200, response.text
    job = wait(api, response.json()['job_id'])
    assert job['status'] == 'completed', job
    assert job['tiles_total'] == 0 and job['processing_mode'] == 'resize'
    output = Image.open(io.BytesIO(api.get(f'/api/upscale/{job["job_id"]}/download').content))
    assert output.size == (24, 16)
    assert np.array_equal(np.asarray(output), np.asarray(source.resize((24, 16), Image.Resampling.LANCZOS)))


@pytest.mark.real
def test_ai_resize_uses_real_model(client):
    api, _ = client
    source, data = source_bytes()
    response = api.post('/api/upscale', files={'file': ('test.png', data, 'image/png')},
                        data={'processing_mode': 'ai_resize', 'scale': 0.5})
    assert response.status_code == 200, response.text
    job = wait(api, response.json()['job_id'])
    assert job['status'] == 'completed', job
    assert job['tiles_total'] > 0 and job['processing_mode'] == 'ai_resize'
    output = Image.open(io.BytesIO(api.get(f'/api/upscale/{job["job_id"]}/download').content))
    assert output.size == (24, 16)
    assert np.array_equal(np.asarray(output.getchannel('A')), np.asarray(source.getchannel('A').resize((24, 16), Image.Resampling.LANCZOS)))
    assert not np.array_equal(np.asarray(output), np.asarray(source.resize((24, 16), Image.Resampling.LANCZOS)))


@pytest.mark.parametrize('data', [{'processing_mode':'fake'}, {'processing_mode':'resize','scale':2},
    {'processing_mode':'ai_resize','scale':0.5,'face_enhance':'true'}, {'scale':0.5}])
def test_invalid_resize_options(client, data):
    api, _ = client
    _, image = source_bytes()
    response = api.post('/api/upscale', files={'file': ('x.png', image, 'image/png')}, data=data)
    assert response.status_code in (400, 422)


def test_generated_and_reused_base_support_resize(client, monkeypatch):
    api, store = client
    monkeypatch.setattr(main, 'readiness', lambda: {'available': True})
    monkeypatch.setattr(jobs, 'generate_base_image', base_fixture)
    response = api.post('/api/generate', json={'prompt':'fixture', 'aspect':'square',
        'processing_mode':'resize', 'shrink_scale':0.25})
    job = wait(api, response.json()['job_id'])
    assert job['status'] == 'completed', job
    assert (job['output_width'], job['output_height']) == (256, 256)
    def forbidden(*args, **kwargs):
        pytest.fail('Reuse must not call generation.')
    monkeypatch.setattr(jobs, 'generate_base_image', forbidden)
    response = api.post(f'/api/upscale/{job["job_id"]}/continue', json={'processing_mode':'resize', 'shrink_scale':0.5})
    new = wait(api, response.json()['job_id'])
    assert new['status'] == 'completed', new
    assert (new['output_width'], new['output_height']) == (512, 512)
    assert new['base_width'] == 1024  # Always resize the base, never cascade from the previous smaller output.
