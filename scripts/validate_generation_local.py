"""Validate genuine SR with a local base fixture; never call a paid provider."""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PIL import Image, ImageOps
import backend.app.jobs as jobs
from backend.app.config import ROOT

output = ROOT / 'data' / 'validation' / 'generation-local'
output.mkdir(parents=True, exist_ok=True)


def fixture(prompt, aspect, destination, cancel):
    cancel()
    with Image.open(ROOT / 'data' / 'fixtures' / '1080p.jpg') as image:
        square = ImageOps.fit(image.convert('RGB'), (1024, 1024), method=Image.Resampling.LANCZOS)
        square.save(destination)
    return 1024, 1024


jobs.generate_base_image = fixture
store = jobs.JobStore()
store.startup()
try:
    job = store.create_generated('LOCAL FIXTURE ONLY - NO PROVIDER REQUEST', 'square', {
        'model': 'RealESRGAN_x4plus_anime_6B', 'output_level': '4K', 'output_format': 'png', 'quality': 95,
    })
    (output / 'job.json').write_text(json.dumps({'job_id': job.job_id, 'source': 'local fixture; no image generation API'}), encoding='utf-8')
    while job.status not in jobs.TERMINAL:
        print(job.status, job.progress, job.tiles_done, '/', job.tiles_total, flush=True)
        time.sleep(10)
    assert job.status == 'completed', job.error
    shutil.copyfile(job.base_file, output / 'base.png')
    shutil.copyfile(job.output_file, output / 'result-4k.png')
    with Image.open(job.base_file) as base, Image.open(job.output_file) as result:
        assert base.size == (1024, 1024)
        assert result.size == (2880, 2880)
        a = np.asarray(result.convert('RGB'), dtype=np.int16)
        b = np.asarray(base.convert('RGB').resize(result.size, Image.Resampling.LANCZOS), dtype=np.int16)
        difference = float(np.abs(a - b).mean())
        assert difference > 0.1
        crop = result.crop((700, 700, 1340, 1340))
        crop.save(output / 'detail.png')
    report = {'job_id': job.job_id, 'generation': 'local fixture only', 'base': [1024, 1024],
              'output': [2880, 2880], 'model': job.model, 'device': job.device,
              'processing_ms': job.processing_ms, 'tiles': job.tiles_total, 'difference_from_lanczos_mae': difference,
              'note': 'Pixel difference confirms distinct output, not a perceptual quality score.'}
    (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)
finally:
    store.shutdown()
