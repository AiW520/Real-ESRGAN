import json
import shutil
import threading
import time
import uuid
import re
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from filelock import FileLock
import numpy as np
from PIL import Image

from .config import DATA, MAX_JOBS, TTL_SECONDS, JOB_TIMEOUT
from .image_processor import encode, inspect_image, preflight, read_pixels, target_size
from .model_manager import manager
from .tile_processor import process_with_fallback, close_map
from .generation import generate_base_image

TERMINAL = {'completed', 'failed', 'cancelled'}
logger = logging.getLogger(__name__)


class QueueFull(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class JobCancelled(Exception):
    pass


@dataclass
class Job:
    job_id: str
    status: str = 'queued'
    progress: int = 0
    current_stage: str = 'Queued'
    error: str | None = None
    output_width: int | None = None
    output_height: int | None = None
    output_file: str | None = None
    input_width: int | None = None
    input_height: int | None = None
    input_size: int = 0
    processing_ms: int | None = None
    model: str = 'RealESRGAN_x4plus'
    tile_size: int | None = None
    original_name: str = ''
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    device: str = ''
    tiles_done: int = 0
    tiles_total: int = 0
    output_size: int = 0
    attempts: list = field(default_factory=list)
    estimated_memory: int = 0
    workflow: str = 'upload'
    prompt_label: str = ''
    aspect: str = ''
    base_file: str | None = None
    base_width: int | None = None
    base_height: int | None = None
    output_level: str = ''
    processing_mode: str = 'upscale'
    shrink_scale: float = 0.5
    request_key: str | None = None
    request_fingerprint: str | None = None


class JobStore:
    def __init__(self, data_dir=DATA):
        self.data = data_dir
        for name in ('jobs', 'uploads', 'outputs'):
            (self.data / name).mkdir(parents=True, exist_ok=True)
        self.items = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='upscale')
        self.stop = threading.Event()

    def startup(self):
        # Lifespan, sync health routes and shutdown run on different threads.
        self.process_lock = FileLock(self.data / 'worker.lock', thread_local=False)
        self.process_lock.acquire(timeout=0)
        for path in (self.data / 'jobs').glob('*.json'):
            try:
                fields = json.loads(path.read_text('utf-8'))
                job = Job(**fields)
                if not re.fullmatch('[a-f0-9]{32}', job.job_id) or path.stem != job.job_id:
                    continue
                if job.status not in TERMINAL:
                    job.status, job.error = 'failed', 'The server restarted before this job finished. Continue from the saved base image if available.'
                    job.finished_at = time.time()
                    self._save(job)
                self.items[job.job_id] = job
            except (OSError, ValueError, TypeError):
                continue
        for directory in (self.data / 'jobs').iterdir():
            if directory.is_dir():
                shutil.rmtree(directory)
        for path in (self.data / 'uploads').glob('*.upload'):
            path.unlink(missing_ok=True)
        self.cleanup()
        self.cleaner = threading.Thread(target=self._maintenance, daemon=True)
        self.cleaner.start()

    def _maintenance(self):
        while not self.stop.wait(60):
            try:
                self.cleanup()
            except OSError:
                logger.warning('Result cleanup could not finish; will retry on the next maintenance cycle.')

    def cleanup(self):
        with self.lock:
            for job_id, job in list(self.items.items()):
                if job.status in TERMINAL and time.time() - (job.finished_at or job.created_at) > TTL_SECONDS:
                    for path in (self.data / 'outputs').glob(f'{job_id}.*'):
                        path.unlink(missing_ok=True)
                    (self.data / 'jobs' / f'{job_id}.json').unlink(missing_ok=True)
                    self.items.pop(job_id)

    def shutdown(self):
        self.stop.set()
        with self.lock:
            for job in self.items.values():
                if job.status not in TERMINAL:
                    job.cancel_requested = True
        self.executor.shutdown(wait=True)
        if hasattr(self, 'cleaner'):
            self.cleaner.join(timeout=5)
        if hasattr(self, 'process_lock'):
            self.process_lock.release()

    def check_capacity(self):
        with self.lock:
            if self.stop.is_set():
                raise QueueFull('The worker is stopping. Wait for the service to become ready.')
            if sum(job.status not in TERMINAL for job in self.items.values()) >= MAX_JOBS:
                raise QueueFull(f'The queue is full ({MAX_JOBS} jobs). Wait for an existing task to finish.')

    def lookup_request(self, identity):
        if not identity:
            return None
        with self.lock:
            for job in self.items.values():
                if job.request_key == identity['request_key']:
                    if job.request_fingerprint != identity['request_fingerprint']:
                        raise IdempotencyConflict('Idempotency-Key was already used with different input or options.')
                    return job
        return None

    def _submit(self, job, runner, *args):
        """Caller holds the store lock; do not leave a phantom queued job on failure."""
        try:
            self._save(job)
            self.items[job.job_id] = job
            self.executor.submit(runner, job.job_id, *args)
        except Exception:
            self.items.pop(job.job_id, None)
            (self.data / 'jobs' / f'{job.job_id}.json').unlink(missing_ok=True)
            raise

    def create(self, source, original_name, options, identity=None):
        job = Job(uuid.uuid4().hex, model=options['model'], tile_size=options.get('tile_size'),
                  original_name=original_name, input_size=source.stat().st_size,
                  processing_mode=options.get('processing_mode', 'upscale'),
                  shrink_scale=options.get('scale', 0.5) if options.get('processing_mode', 'upscale') != 'upscale' else 0.5)
        with self.lock:
            previous = self.lookup_request(identity)
            if previous:
                source.unlink(missing_ok=True)
                return previous
            self.check_capacity()
            if identity:
                job.request_key, job.request_fingerprint = identity['request_key'], identity['request_fingerprint']
            self._submit(job, self._run, source, options)
        return job

    def create_generated(self, prompt, aspect, options, identity=None):
        job = Job(uuid.uuid4().hex, model=options['model'], original_name='gpt-image-2 基础图',
                  workflow='generate', aspect=aspect, output_level=options.get('output_level', '4K'),
                  prompt_label='已提交生成提示词', processing_mode=options.get('processing_mode', 'upscale'),
                  shrink_scale=options.get('shrink_scale', 0.5))
        with self.lock:
            previous = self.lookup_request(identity)
            if previous:
                return previous
            self.check_capacity()
            if identity:
                job.request_key, job.request_fingerprint = identity['request_key'], identity['request_fingerprint']
            self._submit(job, self._run_generated, prompt, aspect, options)
        return job

    def create_from_base(self, source_job_id, options, identity=None):
        with self.lock:
            previous = self.lookup_request(identity)
            if previous:
                return previous
            self.check_capacity()
            source_job = self.items.get(source_job_id)
            if not source_job or source_job.workflow not in {'generate', 'base-upscale'} or not source_job.base_file:
                raise ValueError('This task has no reusable generated base image.')
            base_source = Path(source_job.base_file).resolve()
            outputs_root = (self.data / 'outputs').resolve()
            if outputs_root not in base_source.parents or not base_source.is_file():
                raise ValueError('The generated base image has expired or was removed.')
            info = inspect_image(base_source)
            sr_options = self._base_options(options)
            out_w, out_h = target_size(info.width, info.height, scale=sr_options['scale'],
                                      resolution=sr_options['resolution'], processing_mode=sr_options['processing_mode'])
            estimated = preflight(info.width, info.height, out_w, out_h, False)
            job = Job(uuid.uuid4().hex, model=options['model'], original_name='复用生成基础图',
                      workflow='base-upscale', aspect=source_job.aspect,
                      output_level=options.get('output_level', '4K'),
                      base_width=info.width, base_height=info.height,
                      input_width=info.width, input_height=info.height,
                      output_width=out_w, output_height=out_h, estimated_memory=estimated)
            job.processing_mode = options.get('processing_mode', 'upscale')
            job.shrink_scale = options.get('shrink_scale', 0.5)
            if identity:
                job.request_key, job.request_fingerprint = identity['request_key'], identity['request_fingerprint']
            base_copy = self.data / 'outputs' / f'{job.job_id}.base.png'
            try:
                shutil.copyfile(base_source, base_copy)
                job.base_file = str(base_copy)
                self._submit(job, self._run_base_upscale, options)
            except Exception:
                base_copy.unlink(missing_ok=True)
                raise
        return job

    def get(self, job_id):
        with self.lock:
            return self.items.get(job_id)

    def queue_position(self, job):
        """1-based position among unfinished jobs (the running job counts as 1)."""
        with self.lock:
            if job.status != 'queued':
                return 0
            ahead = sum(other.status not in TERMINAL and other.created_at < job.created_at
                        for other in self.items.values() if other is not job)
            return ahead + 1

    def cancel(self, job_id):
        with self.lock:
            job = self.items.get(job_id)
            if job and job.status not in TERMINAL:
                self._set(job, cancel_requested=True)

    def _save(self, job):
        path = self.data / 'jobs' / f'{job.job_id}.json'
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(asdict(job), indent=2), encoding='utf-8')
        temporary.replace(path)

    def _set(self, job, **updates):
        with self.lock:
            for key, value in updates.items():
                setattr(job, key, value)
            self._save(job)

    def _cancel_check(self, job, started=None):
        if job.cancel_requested:
            raise JobCancelled('Task cancelled by the user.')
        if started and time.perf_counter() - started > JOB_TIMEOUT:
            raise TimeoutError('The task exceeded the processing timeout. Use a smaller input or CUDA.')

    def _run(self, job_id, source, options, started=None):
        job = self.get(job_id)
        output = self.data / 'outputs' / f'{job_id}.{options["output_format"]}'
        workspace = self.data / 'jobs' / job_id
        started = started or time.perf_counter()
        result = alpha = None
        check = lambda: self._cancel_check(job, started)
        try:
            check()
            self._set(job, status='loading_model', current_stage='Validating image',
                      started_at=job.started_at or time.time())
            info = inspect_image(source)
            mode = options.get('processing_mode', 'upscale')
            out_w, out_h = target_size(info.width, info.height, processing_mode=mode, **{k: options[k] for k in ('scale', 'resolution', 'target_width', 'target_height')})
            estimated = preflight(info.width, info.height, out_w, out_h, options['face_enhance'])
            self._set(job, input_width=info.width, input_height=info.height, output_width=out_w, output_height=out_h, estimated_memory=estimated)
            self._cancel_check(job)
            if mode == 'resize':
                self._set(job, status='processing', current_stage='High-quality downsampling', device='cpu')
                rgb, alpha = read_pixels(source)
                image = Image.fromarray(rgb)
                # Premultiply alpha through Pillow RGBA resize to avoid colored fringes.
                if alpha is not None:
                    image.putalpha(alpha)
                resized = image.resize((out_w, out_h), Image.Resampling.LANCZOS)
                image.close()
                if alpha is not None:
                    alpha.close()
                    alpha = resized.getchannel('A')
                result = np.array(resized.convert('RGB'))
                resized.close()
                check()
                self._set(job, status='encoding', current_stage='Encoding output')
                encode(result, alpha, output, options['output_format'], options['quality'])
                check()
                self._set(job, status='completed', current_stage='Completed', progress=100,
                          output_file=str(output), output_size=output.stat().st_size,
                          finished_at=time.time(), processing_ms=round((time.perf_counter() - started) * 1000))
                return
            self._set(job, status='loading_model', current_stage='Loading Real-ESRGAN model')
            try:
                model = manager.load(options['model'], cancel=check)
            except RuntimeError as exc:
                if 'out of memory' not in str(exc).lower():
                    raise
                manager.release('cuda')
                manager.release('mps')
                model = manager.load(options['model'], device='cpu', cancel=check)
            self._set(job, device=model.device)
            self._set(job, status='processing', current_stage='AI Upscaling', progress=0)
            rgb, alpha = read_pixels(source)

            def progress(done, total, message=None):
                if message:
                    if message == 'merging':
                        self._set(job, status='merging', progress=99, current_stage='Merging overlaps')
                    else:
                        self._set(job, progress=0, current_stage=message, tiles_done=0)
                elif total:
                    self._set(job, status='processing', current_stage=f'AI Upscaling: tile {done} / {total}',
                              progress=round(done / total * 98), tiles_done=done, tiles_total=total)
                check()

            result, attempts = process_with_fallback(rgb, options['model'], model, (out_w, out_h), options.get('tile_size'), workspace, progress, check)
            self._set(job, status='merging', current_stage='Merging tile overlaps', progress=99,
                      tile_size=attempts[-1]['tile_size'], device=attempts[-1]['device'], attempts=attempts)
            self._cancel_check(job)
            if options['face_enhance']:
                from .face_enhancer import face_enhancer
                self._set(job, status='processing', current_stage='Optional face enhancement')
                result_array, _ = face_enhancer.enhance(np.asarray(result), check,
                    lambda done, total: self._set(job, current_stage=f'Face enhancement: {done} / {total}'))
                if result_array is result or np.shares_memory(result_array, result):
                    result_array = np.array(result_array, copy=True)
                close_map(result)
                result = result_array
            self._set(job, status='encoding', current_stage='Encoding output')
            encode(np.asarray(result), alpha, output, options['output_format'], options['quality'])
            check()
            self._set(job, status='completed', current_stage='Completed', progress=100, output_file=str(output),
                      output_size=output.stat().st_size, finished_at=time.time(), processing_ms=round((time.perf_counter() - started) * 1000))
        except Exception as exc:
            output.unlink(missing_ok=True)
            state = 'cancelled' if isinstance(exc, JobCancelled) else 'failed'
            self._set(job, status=state, current_stage=state.title(), error=str(exc), finished_at=time.time(),
                      processing_ms=round((time.perf_counter() - started) * 1000))
        finally:
            close_map(result)
            if alpha is not None:
                alpha.close()
            if workspace.exists():
                shutil.rmtree(workspace, ignore_errors=True)
            source.unlink(missing_ok=True)

    def _run_generated(self, job_id, prompt, aspect, options):
        job = self.get(job_id)
        started = time.perf_counter()
        workspace = self.data / 'jobs' / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        base = self.data / 'outputs' / f'{job_id}.base.png'
        output = self.data / 'outputs' / f'{job_id}.{options["output_format"]}'
        result = alpha = None
        check = lambda: self._cancel_check(job, started)
        try:
            check()
            self._set(job, status='generating', current_stage='Generating base image with gpt-image-2', progress=0,
                      started_at=time.time())
            generated = workspace / 'base.png'
            generate_base_image(prompt, aspect, generated, check)
            check()
            info = inspect_image(generated)
            base_w, base_h = info.width, info.height
            generated.replace(base)
            self._set(job, base_file=str(base), base_width=base_w, base_height=base_h,
                      input_width=base_w, input_height=base_h)
            check()
            level = options.get('output_level', '4K')
            mode = options.get('processing_mode', 'upscale')
            if mode != 'upscale':
                out_w, out_h = target_size(base_w, base_h, scale=options.get('shrink_scale', 0.5), processing_mode=mode)
            elif level == 'native':
                out_w, out_h = base_w, base_h
            else:
                out_w, out_h = target_size(base_w, base_h, scale=2 if level == '2K' else 4, resolution=level)
            self._set(job, output_width=out_w, output_height=out_h)
            if level == 'native' and mode == 'upscale':
                self._set(job, status='encoding', current_stage='Encoding generated base image', progress=90)
                rgb, alpha = read_pixels(base)
                encode(np.asarray(rgb), alpha, output, options['output_format'], options['quality'])
            else:
                source = self.data / 'uploads' / f'{job_id}.upload'
                shutil.copyfile(base, source)
                sr_options = self._base_options(options)
                self._run(job_id, source, sr_options, started)
                return
            check()
            self._set(job, status='completed', current_stage='Completed', progress=100, output_file=str(output),
                      output_size=output.stat().st_size, finished_at=time.time(), processing_ms=round((time.perf_counter() - started) * 1000))
        except Exception as exc:
            output.unlink(missing_ok=True)
            if job.base_file is None:
                base.unlink(missing_ok=True)
            state = 'cancelled' if isinstance(exc, JobCancelled) else 'failed'
            self._set(job, status=state, current_stage=state.title(), error=str(exc), finished_at=time.time(),
                      processing_ms=round((time.perf_counter() - started) * 1000))
        finally:
            close_map(result)
            if alpha is not None:
                alpha.close()
            if workspace.exists():
                shutil.rmtree(workspace, ignore_errors=True)

    @staticmethod
    def _base_options(options):
        mode = options.get('processing_mode', 'upscale')
        return {**options, 'scale': options.get('shrink_scale', 0.5) if mode != 'upscale' else 2,
                'resolution': 'scale' if mode != 'upscale' else options.get('output_level', '4K'),
                'processing_mode': mode, 'face_enhance': False, 'target_width': None, 'target_height': None}

    def _run_base_upscale(self, job_id, options):
        job = self.get(job_id)
        if not job or not job.base_file:
            return
        started = time.perf_counter()
        source = self.data / 'uploads' / f'{job_id}.upload'
        try:
            shutil.copyfile(job.base_file, source)
            level = options.get('output_level', '4K')
            if level == 'native':
                raise ValueError('Choose 2K or 4K when continuing an upscale task.')
            self._run(job_id, source, self._base_options(options), started)
        except Exception as exc:
            source.unlink(missing_ok=True)
            self._set(job, status='failed', current_stage='Failed', error=str(exc),
                      finished_at=time.time(), processing_ms=round((time.perf_counter() - started) * 1000))


store = JobStore()
