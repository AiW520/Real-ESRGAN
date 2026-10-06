from urllib.parse import unquote

from backend.app.jobs import Job
from backend.app.main import download_name
from tests.test_api import image_bytes, wait


def test_download_uses_readable_name_and_status_reports_timing(client):
    api, _ = client
    response = api.post('/api/upscale', files={'file': ('我的照片.png', image_bytes(), 'image/png')},
                        data={'processing_mode': 'resize', 'scale': 0.5})
    assert response.status_code == 200, response.text
    status = wait(api, response.json()['job_id'])
    assert status['status'] == 'completed', status
    assert status['started_at'] and status['started_at'] >= status['created_at']
    assert status['queue_position'] == 0
    disposition = api.get(f'/api/upscale/{status["job_id"]}/download').headers['content-disposition']
    assert unquote(disposition).endswith('我的照片_12x8_resized.png')


def test_download_name_is_filesystem_safe():
    job = Job('a' * 32, original_name='..\\con:bad*name?.jpg', output_width=640, output_height=480)
    assert download_name(job, 'png') == 'con_bad_name__640x480_upscaled.png'
    generated = Job('b' * 32, workflow='generate', output_level='native', output_width=1024, output_height=1024)
    assert download_name(generated, 'png') == 'generated-bbbbbbbb_1024x1024_base.png'
    assert download_name(Job('c' * 32, original_name='...'), 'jpg') == 'cccccccc_upscaled.jpg'


def test_queue_position_counts_unfinished_jobs_ahead(client):
    _, store = client
    running = Job('1' * 32, status='processing', created_at=1)
    first = Job('2' * 32, created_at=2)
    second = Job('3' * 32, created_at=3)
    done = Job('4' * 32, status='completed', created_at=0)
    for job in (running, first, second, done):
        store.items[job.job_id] = job
    assert store.queue_position(running) == 0
    assert store.queue_position(first) == 2
    assert store.queue_position(second) == 3
