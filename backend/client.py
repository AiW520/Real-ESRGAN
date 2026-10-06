"""Server-to-server adapter. Keep returned job IDs to resume after a disconnect."""
from pathlib import Path
import os
import time
import httpx


class UpscalerClient:
    def __init__(self, base_url, *, timeout=120, headers=None, api_token=None, trust_env=False):
        request_headers = dict(headers or {})
        if api_token:
            request_headers['Authorization'] = 'Bearer ' + api_token
        self.http = httpx.Client(base_url=base_url.rstrip('/'), timeout=timeout, headers=request_headers,
                                 trust_env=trust_env)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()

    def submit(self, image_path, *, mode='upscale', resolution='4K', scale=None,
               model='RealESRGAN_x4plus', output_format='png', request_id=None):
        """One submission only. The caller owns persistence and any retry decision."""
        if mode not in {'upscale', 'resize', 'ai_resize'}:
            raise ValueError('Unknown processing mode.')
        options = {'processing_mode': mode, 'resolution': resolution if mode == 'upscale' else 'scale',
                   'scale': scale if scale is not None else (2 if mode == 'upscale' else 0.5),
                   'model': model, 'output_format': output_format}
        path = Path(image_path)
        with path.open('rb') as source:
            response = self.http.post('/api/upscale', files={'file': (path.name, source)}, data=options,
                                      headers={'Idempotency-Key': request_id} if request_id else None)
        response.raise_for_status()
        return response.json()['job_id']

    def status(self, job_id):
        response = self.http.get(f'/api/upscale/{job_id}/status')
        response.raise_for_status()
        return response.json()

    def wait(self, job_id, *, timeout=1800, poll_seconds=2):
        if timeout <= 0 or poll_seconds <= 0:
            raise ValueError('Timeout and polling interval must be positive.')
        deadline = time.monotonic() + timeout
        while True:
            status = self.status(job_id)
            if status['status'] == 'completed':
                return status
            if status['status'] in {'failed', 'cancelled'}:
                raise RuntimeError(f"Task {job_id}: {status.get('error') or status['status']}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'Task {job_id} is still running. Query this ID again; do not regenerate.')
            time.sleep(min(poll_seconds, remaining))

    def download(self, job_id, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + '.part')
        created = False
        try:
            with self.http.stream('GET', f'/api/upscale/{job_id}/download') as response:
                response.raise_for_status()
                with temporary.open('xb') as output:
                    created = True
                    for chunk in response.iter_bytes():
                        output.write(chunk)
            if temporary.stat().st_size == 0:
                raise RuntimeError('Empty image download.')
            os.replace(temporary, destination)
        finally:
            if created:
                temporary.unlink(missing_ok=True)
        return destination
