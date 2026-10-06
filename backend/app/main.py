import mimetypes
import uuid
import re
import base64
import io
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Literal
from .generation import readiness, _validate_prompt
from .config import DATA, MAX_UPLOAD, ROOT
from .image_processor import inspect_image, target_size, preflight, read_pixels, SRGB_PROFILE
from .jobs import store, QueueFull, IdempotencyConflict, TERMINAL
from .requests import request_identity
from .security import ServiceSecurity, settings
from .model_manager import REGISTRY, select_device

mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/javascript', '.mjs')
mimetypes.add_type('text/css', '.css')


@asynccontextmanager
async def lifespan(app):
    settings.validate()
    store.startup()
    try:
        yield
    finally:
        await run_in_threadpool(store.shutdown)

app = FastAPI(title='AI Image Upscaler', version='1.1.0', lifespan=lifespan,
              docs_url=None if settings.production else '/docs',
              redoc_url=None if settings.production else '/redoc',
              openapi_url=None if settings.production else '/openapi.json')
app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins), allow_methods=['GET', 'POST'], allow_headers=['Idempotency-Key'])
app.add_middleware(ServiceSecurity, settings=settings, max_body_bytes=MAX_UPLOAD + 1024 * 1024)
if settings.production:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))


def submission_error(exc):
    return HTTPException(429 if isinstance(exc, QueueFull) else 409 if isinstance(exc, IdempotencyConflict) else 400, str(exc))


def bool_form(value):
    return str(value).lower() in {'1', 'true', 'yes', 'on'}


@app.get('/healthz')
def liveness():
    return {'ok': True}


@app.get('/readyz')
def ready():
    available = hasattr(store, 'process_lock') and store.process_lock.is_locked and not store.stop.is_set()
    return JSONResponse({'ready': available}, status_code=200 if available else 503)


@app.get('/api/operations')
def operations():
    with store.lock:
        counts = {state: sum(job.status == state for job in store.items.values())
                  for state in ('queued', 'generating', 'loading_model', 'processing', 'merging', 'encoding', 'completed', 'failed', 'cancelled')}
    return {'jobs': counts, 'worker_stopping': store.stop.is_set(), 'production': settings.production,
            'generation_enabled': settings.generation_enabled}


@app.get('/api/health')
def health():
    return {'ok': True, 'service': 'AI Image Upscaler', 'device': select_device(), 'models': list(REGISTRY),
            'max_upload_mb': 50, 'max_output_pixels': 33177600, 'max_edge': 7680}


class GenerateRequest(BaseModel):
    processing_mode: Literal['upscale', 'resize', 'ai_resize'] = 'upscale'
    shrink_scale: Literal[0.25, 0.5] = 0.5
    prompt: str = Field(min_length=1, max_length=16000)
    aspect: Literal['square', 'landscape', 'portrait'] = 'landscape'
    output_level: Literal['native', '2K', '4K'] = '4K'
    model: Literal['RealESRGAN_x4plus', 'RealESRGAN_x4plus_anime_6B'] = 'RealESRGAN_x4plus'
    output_format: Literal['png', 'jpg', 'webp'] = 'png'
    quality: int = Field(default=95, ge=1, le=100)


class ContinueRequest(BaseModel):
    processing_mode: Literal['upscale', 'resize', 'ai_resize'] = 'upscale'
    shrink_scale: Literal[0.25, 0.5] = 0.5
    output_level: Literal['2K', '4K'] = '4K'
    model: Literal['RealESRGAN_x4plus', 'RealESRGAN_x4plus_anime_6B'] = 'RealESRGAN_x4plus'
    output_format: Literal['png', 'jpg', 'webp'] = 'png'
    quality: int = Field(default=95, ge=1, le=100)


@app.get('/api/generation/config')
def generation_config():
    if not settings.generation_enabled:
        return {'available': False, 'reason': 'Generation is disabled; submit the platform-generated base image to /api/upscale.'}
    return {**readiness(), 'aspects': ['square', 'landscape', 'portrait'], 'levels': ['native', '2K', '4K']}


@app.post('/api/generate')
def generate(request: GenerateRequest, idempotency_key: str | None = Header(None)):
    if not settings.generation_enabled:
        raise HTTPException(403, 'Generation is disabled in this deployment.')
    try:
        prompt = _validate_prompt(request.prompt)
        identity = request_identity(idempotency_key, 'generate', request.model_dump())
        previous = store.lookup_request(identity)
        if previous:
            return {'job_id': previous.job_id, 'status': previous.status}
        store.check_capacity()
    except ValueError as exc:
        raise submission_error(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    config = readiness()
    if not config['available']:
        raise HTTPException(503, config['reason'])
    try:
        job = store.create_generated(prompt, request.aspect, request.model_dump(exclude={'prompt', 'aspect'}), identity=identity)
    except ValueError as exc:
        raise submission_error(exc) from exc
    return {'job_id': job.job_id, 'status': job.status}


@app.post('/api/upscale')
async def create_job(file: UploadFile = File(...), scale: float = Form(2), resolution: str = Form('scale'),
                     processing_mode: Literal['upscale', 'resize', 'ai_resize'] = Form('upscale'),
                     model: str = Form('RealESRGAN_x4plus'), face_enhance: bool = Form(False),
                     output_format: str = Form('png'), quality: int = Form(95), tile_size: int | None = Form(None),
                     target_width: int | None = Form(None), target_height: int | None = Form(None),
                     target_resolution: str | None = Form(None), idempotency_key: str | None = Header(None)):
    if output_format not in {'png', 'jpg', 'webp'}:
        raise HTTPException(400, 'Output format must be png, jpg or webp.')
    if not 1 <= quality <= 100:
        raise HTTPException(400, 'Quality must be between 1 and 100.')
    if model not in REGISTRY:
        raise HTTPException(400, 'Unknown model. Choose one of the available Real-ESRGAN models.')
    if tile_size is not None and tile_size not in (64, 128, 256, 384, 512):
        raise HTTPException(400, 'Tile size must be 64, 128, 256, 384 or 512.')
    try:
        if idempotency_key is None:
            store.check_capacity()
    except ValueError as exc:
        raise HTTPException(429, str(exc)) from exc
    if not await file.read(1):
        raise HTTPException(400, 'The uploaded file is empty.')
    await file.seek(0)
    temp = DATA / 'uploads' / f'{uuid.uuid4().hex}.upload'
    size = 0
    try:
        with open(temp, 'wb') as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, 'Upload exceeds the 50 MB limit.')
                target.write(chunk)
        info = await run_in_threadpool(inspect_image, temp)
        resolution = target_resolution or resolution
        if processing_mode != 'upscale' and face_enhance:
            raise ValueError('Face enhancement is only available in upscale mode.')
        dimensions = target_size(info.width, info.height, scale, resolution, target_width, target_height, processing_mode)
        preflight(info.width, info.height, *dimensions, face_enhance)
        options = {'scale': scale, 'resolution': resolution, 'model': model,
                   'face_enhance': face_enhance, 'output_format': output_format,
                   'quality': quality, 'tile_size': tile_size, 'target_width': target_width,
                   'target_height': target_height, 'processing_mode': processing_mode}
        name = re.sub(r'[^\w. -]', '_', (file.filename or 'upload').replace('\\', '/').split('/')[-1])[:120]
        identity = await run_in_threadpool(request_identity, idempotency_key, 'upscale', options, temp)
        job = store.create(temp, name or 'upload', options, identity=identity)
        return {'job_id': job.job_id, 'status': job.status}
    except HTTPException:
        temp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temp.unlink(missing_ok=True)
        raise submission_error(exc) from exc
    finally:
        await file.close()


@app.get('/api/upscale/{job_id}/status')
def status(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found.')
    with store.lock:
        return {**{k: v for k, v in asdict(job).items() if k not in {'cancel_requested', 'output_file', 'base_file', 'request_key', 'request_fingerprint'}},
                'base_available': bool(job.base_file and Path(job.base_file).is_file()),
                'queue_position': store.queue_position(job)}


def download_name(job, suffix):
    """Readable, filesystem-safe name such as photo_3840x2160_upscaled.png."""
    stem = Path((job.original_name or '').replace('\\', '/').split('/')[-1]).stem
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', stem).strip(' .')[:80] or job.job_id[:8]
    tag = {'resize': 'resized', 'ai_resize': 'ai-resized'}.get(job.processing_mode, 'upscaled')
    if job.workflow in {'generate', 'base-upscale'}:
        stem = f'generated-{job.job_id[:8]}'
        if job.output_level == 'native' and job.processing_mode == 'upscale':
            tag = 'base'
    size = f'_{job.output_width}x{job.output_height}' if job.output_width and job.output_height else ''
    return f'{stem}{size}_{tag}.{suffix}'


@app.get('/api/upscale/{job_id}/base')
def base_image(job_id: str):
    job = store.get(job_id)
    if not job or not job.base_file:
        raise HTTPException(404, 'The base image is not available yet.')
    path = Path(job.base_file)
    if not path.is_file():
        raise HTTPException(410, 'The base image has expired or was removed.')
    return FileResponse(path, media_type='image/png')


@app.post('/api/upscale/{job_id}/continue')
def continue_upscale(job_id: str, request: ContinueRequest, idempotency_key: str | None = Header(None)):
    try:
        identity = request_identity(idempotency_key, 'continue:' + job_id, request.model_dump())
        job = store.create_from_base(job_id, request.model_dump(), identity=identity)
    except ValueError as exc:
        raise submission_error(exc) from exc
    return {'job_id': job.job_id, 'status': job.status, 'source_job_id': job_id}


@app.post('/api/upscale/{job_id}/cancel')
def cancel(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, 'Job not found.')
    store.cancel(job_id)
    return {'job_id': job_id, 'status': 'cancellation_requested'}


@app.get('/api/upscale/{job_id}/download')
def download(job_id: str):
    job = store.get(job_id)
    if not job or job.status != 'completed' or not job.output_file:
        raise HTTPException(404, 'The job has not completed yet.')
    path = Path(job.output_file)
    if not path.is_file():
        raise HTTPException(410, 'The output file has expired or was removed.')
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                        filename=download_name(job, path.suffix[1:]))


@app.get('/api/upscale/{job_id}/image')
def result_image(job_id: str):
    response = download(job_id)
    response.headers['content-disposition'] = 'inline'
    return response


@app.get('/api/sample')
def sample():
    path = DATA / 'fixtures' / 'photo.jpg'
    if not path.exists():
        raise HTTPException(404, 'Sample image is unavailable.')
    return FileResponse(path, media_type='image/jpeg')


@app.post('/api/inspect')
async def inspect_upload(file: UploadFile = File(...)):
    from PIL import Image
    path = DATA / 'uploads' / f'{uuid.uuid4().hex}.upload'
    alpha = None
    try:
        size = 0
        with path.open('wb') as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, 'Upload exceeds the 50 MB limit.')
                target.write(chunk)
        info = await run_in_threadpool(inspect_image, path)
        rgb, alpha = await run_in_threadpool(read_pixels, path)
        preview = Image.fromarray(rgb)
        preview.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        if alpha is not None:
            preview.putalpha(alpha.resize(preview.size, Image.Resampling.LANCZOS))
        buffer = io.BytesIO()
        preview.save(buffer, format='PNG', icc_profile=SRGB_PROFILE)
        preview.close()
        return {**asdict(info), 'preview': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        path.unlink(missing_ok=True)
        await file.close()
        if alpha is not None:
            alpha.close()


FRONTEND = ROOT / 'frontend' / 'dist'
if FRONTEND.exists() and not settings.production:
    app.mount('/', StaticFiles(directory=FRONTEND, html=True), name='frontend')
