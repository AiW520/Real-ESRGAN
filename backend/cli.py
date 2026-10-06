import argparse
import json
import shutil
import time
import uuid
from pathlib import Path

from .app.config import DATA
from .app.jobs import JobStore


def main():
    parser = argparse.ArgumentParser(description='Real-ESRGAN tiled super-resolution CLI')
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scale', type=int, choices=(1, 2, 4), default=2)
    parser.add_argument('--resolution', choices=('scale', 'original', '2K', '4K', '8K'), default='scale')
    parser.add_argument('--model', choices=('RealESRGAN_x4plus', 'RealESRGAN_x4plus_anime_6B'), default='RealESRGAN_x4plus')
    parser.add_argument('--face-enhance', action='store_true')
    parser.add_argument('--tile-size', type=int, choices=(64, 128, 256, 384, 512))
    parser.add_argument('--quality', type=int, default=95)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.drive.upper() != 'D:':
        parser.error('Output must be on drive D.')
    fmt = output.suffix.lower().lstrip('.')
    if fmt == 'jpeg':
        fmt = 'jpg'
    if fmt not in ('png', 'jpg', 'webp'):
        parser.error('Output must have .png, .jpg or .webp extension.')
    if not 1 <= args.quality <= 100:
        parser.error('Quality must be 1-100.')
    store = JobStore(DATA / 'cli')
    store.startup()
    source = store.data / 'uploads' / f'{uuid.uuid4().hex}.upload'
    shutil.copyfile(args.input, source)
    options = dict(scale=args.scale, resolution=args.resolution, model=args.model, face_enhance=args.face_enhance,
                   tile_size=args.tile_size, quality=args.quality, output_format=fmt, target_width=None, target_height=None)
    try:
        job = store.create(source, args.input.name, options)
        previous = None
        while True:
            with store.lock:
                current = (job.status, job.progress, job.tiles_done)
                info = {'job_id': job.job_id, 'status': job.status, 'progress': job.progress,
                        'stage': job.current_stage, 'tiles_done': job.tiles_done, 'tiles_total': job.tiles_total}
            if current != previous:
                print(json.dumps(info), flush=True)
                previous = current
            if job.status in ('completed', 'failed', 'cancelled'):
                break
            time.sleep(1)
        if job.status != 'completed':
            raise RuntimeError(job.error)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(job.output_file, output)
        print(json.dumps({'output': str(output), 'width': job.output_width, 'height': job.output_height,
                          'size': job.output_size, 'milliseconds': job.processing_ms, 'device': job.device}), flush=True)
    finally:
        store.shutdown()


if __name__ == '__main__':
    main()
