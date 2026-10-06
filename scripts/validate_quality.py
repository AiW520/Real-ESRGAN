"""Real HTTP/model validation. No generation requests, no mocked inference."""
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import ROOT
import httpx
import numpy as np
from PIL import Image, ImageDraw
from skimage.metrics import peak_signal_noise_ratio, structural_similarity

OUT = ROOT / 'data' / 'validation' / 'quality-audit'
OUT.mkdir(parents=True, exist_ok=True)
REPORT = {'scope': 'Local HTTP API + official RealESRGAN_x4plus; no generation API', 'cases': []}


def metrics(reference, candidate):
    a, b = np.asarray(reference.convert('RGB')), np.asarray(candidate.convert('RGB'))
    return {'psnr_db': float(peak_signal_noise_ratio(a, b, data_range=255)),
            'ssim': float(structural_similarity(a, b, data_range=255, channel_axis=2)),
            'mean_rgb_shift': [round(float(v), 3) for v in (b.astype(float) - a).mean(axis=(0, 1))]}


def panel(images, name):
    width, height = 360, 400
    canvas = Image.new('RGB', (width * len(images), height), '#17191e')
    draw = ImageDraw.Draw(canvas)
    for i, (label, image) in enumerate(images):
        draw.text((i * width + 12, 12), label, fill='white')
        preview = image.convert('RGB').copy()
        preview.thumbnail((width - 16, height - 40))
        canvas.paste(preview, (i * width + (width - preview.width) // 2, 35))
    canvas.save(OUT / name)


def run_case(client, name, source, scale, reference=None, tile=None):
    source.save(OUT / f'{name}-input.png')
    stream = io.BytesIO()
    source.save(stream, format='PNG')
    options = {'model': 'RealESRGAN_x4plus', 'scale': scale, 'output_format': 'png'}
    if tile:
        options['tile_size'] = tile
    response = client.post('/api/upscale', files={'file': (f'{name}.png', stream.getvalue(), 'image/png')}, data=options)
    response.raise_for_status()
    job_id = response.json()['job_id']
    print(f'{name}: submitted {job_id}', flush=True)
    deadline = time.monotonic() + 3600
    previous = None
    while time.monotonic() < deadline:
        response = client.get(f'/api/upscale/{job_id}/status')
        response.raise_for_status()
        status = response.json()
        current = (status['status'], status['tiles_done'], status['tiles_total'])
        if current != previous:
            print(name, current, flush=True)
            previous = current
        if status['status'] in {'completed', 'failed', 'cancelled'}:
            break
        time.sleep(5)
    assert status['status'] == 'completed', status
    response = client.get(f'/api/upscale/{job_id}/download')
    response.raise_for_status()
    path = OUT / f'{name}-ai.png'
    path.write_bytes(response.content)
    with Image.open(path) as decoded:
        decoded.load()
        result = decoded.copy()
        assert decoded.info.get('icc_profile')
    assert result.size == (source.width * scale, source.height * scale)
    assert status['tiles_done'] == status['tiles_total'] > 0
    baseline = source.resize(result.size, Image.Resampling.LANCZOS)
    baseline.save(OUT / f'{name}-lanczos.png')
    record = {'name': name, 'job_id': job_id, 'input': source.size, 'output': result.size,
              'milliseconds': status['processing_ms'], 'tiles': status['tiles_total'],
              'model': status['model'], 'device': status['device'],
              'difference_vs_lanczos_mae': float(np.abs(np.asarray(result.convert('RGB'), dtype=float) - np.asarray(baseline.convert('RGB'), dtype=float)).mean())}
    if reference:
        reference.save(OUT / f'{name}-reference.png')
        record['ai_metrics'] = metrics(reference, result)
        record['lanczos_metrics'] = metrics(reference, baseline)
        panel([('Reference', reference), ('Lanczos', baseline), ('Real-ESRGAN', result)], f'{name}-comparison.png')
    else:
        panel([('Input', source), ('Lanczos', baseline), ('Real-ESRGAN', result)], f'{name}-comparison.png')
    if source.mode == 'RGBA':
        record['alpha_matches_lanczos'] = bool(np.array_equal(np.asarray(result.getchannel('A')), np.asarray(baseline.getchannel('A'))))
        assert record['alpha_matches_lanczos']
    REPORT['cases'].append(record)
    (OUT / 'report.json').write_text(json.dumps(REPORT, indent=2), encoding='utf-8')
    return result


with httpx.Client(base_url='http://127.0.0.1:8000', timeout=120) as client:
    with Image.open(ROOT / 'data/fixtures/photo.jpg') as image:
        reference = image.convert('RGB')
    low = reference.resize((128, 128), Image.Resampling.LANCZOS)
    compressed = io.BytesIO()
    low.save(compressed, format='JPEG', quality=60)
    compressed.seek(0)
    low = Image.open(compressed).convert('RGB')
    photo = run_case(client, 'photo', low, 4, reference)
    # Compare tile inference to a single tile using the same model and source.
    tiled = run_case(client, 'photo-tiled', low, 4, reference, tile=64)
    difference = np.abs(np.asarray(tiled, dtype=float) - np.asarray(photo, dtype=float))
    REPORT['tiling'] = {'mae': float(difference.mean()), 'p99_error': float(np.percentile(difference, 99)), 'max_error': float(difference.max())}
    panel([('Single tile', photo), ('Tiled 64 px', tiled), ('Absolute error x8', Image.fromarray(np.clip(difference * 8, 0, 255).astype('uint8')))], 'seams.png')
    with Image.open(ROOT / 'data/fixtures/text.png') as image:
        text_reference = image.convert('RGB')
    run_case(client, 'text', text_reference.resize((112, 64), Image.Resampling.LANCZOS), 2, text_reference)
    with Image.open(ROOT / 'data/fixtures/rgba.png') as image:
        rgba = image.convert('RGBA')
    run_case(client, 'alpha', rgba, 2)
    # A diagnostic of SR followed by downsampling, not an exposed resize feature.
    enhanced_small = photo.resize((64, 64), Image.Resampling.LANCZOS)
    ordinary_small = low.resize((64, 64), Image.Resampling.LANCZOS)
    small_reference = reference.resize((64, 64), Image.Resampling.LANCZOS)
    enhanced_small.save(OUT / 'sr-then-downsample.png')
    ordinary_small.save(OUT / 'ordinary-downsample.png')
    REPORT['downsample_experiment'] = {'input': low.size, 'output': enhanced_small.size,
        'ai_then_downsample': metrics(small_reference, enhanced_small),
        'ordinary_downsample': metrics(small_reference, ordinary_small),
        'note': 'Offline comparison only; no resize API added yet.'}
    panel([('Reference 64px', small_reference.resize((320, 320))), ('Normal shrink', ordinary_small.resize((320, 320))), ('SR then shrink', enhanced_small.resize((320, 320)))], 'downsample-comparison.png')
    (OUT / 'report.json').write_text(json.dumps(REPORT, indent=2), encoding='utf-8')
    with Image.open(ROOT / 'data/fixtures/4k.jpg') as image:
        large = image.convert('RGB').resize((960, 540), Image.Resampling.LANCZOS)
    run_case(client, 'photo-4k', large, 4)
    REPORT['limitations'] = ['Controlled local fixtures, not a live generation-provider test.', 'Full 4K case verifies pipeline and pixels; fixture provenance is not a verified native 4K reference.', 'PSNR/SSIM measure fidelity, not universally preferred perceived quality.', 'No claim of perfect text, faces or invented detail recovery.']
    (OUT / 'report.json').write_text(json.dumps(REPORT, indent=2), encoding='utf-8')
print('Quality audit completed:', OUT, flush=True)
