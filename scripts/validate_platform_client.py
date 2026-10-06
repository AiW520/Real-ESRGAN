"""Exercise the platform adapter against the running real service, without generation."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.client import UpscalerClient
from backend.app.config import ROOT
import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity

out = ROOT / 'data/validation/quality-audit'
with UpscalerClient('http://127.0.0.1:8000') as client:
    job_id = client.submit(out / 'photo-input.png', mode='resize', scale=0.25)
    state = client.wait(job_id, timeout=60)
    path = client.download(job_id, out / 'sdk-25-percent.png')
    with Image.open(path) as image:
        assert image.size == (32, 32)

with Image.open(out / 'photo-reference.png') as ref:
    reference = ref.resize((64, 64), Image.Resampling.LANCZOS).convert('RGB')
with Image.open(out / 'photo-ai.png') as superresolved:
    full_then_small = superresolved.resize((64, 64), Image.Resampling.LANCZOS).convert('RGB')
report = {'sdk_job_id':job_id, 'sdk_dimensions':[state['output_width'], state['output_height']], 'modes':{}}
for mode in ['resize', 'ai_resize']:
    with Image.open(out / f'{mode}-api.png') as result:
        array = np.asarray(result.convert('RGB'))
        report['modes'][mode] = {'ssim_vs_small_reference':float(structural_similarity(np.asarray(reference), array, data_range=255, channel_axis=2))}
        if mode == 'ai_resize':
            error = float(np.abs(array.astype(float) - np.asarray(full_then_small, dtype=float)).mean())
            report['modes'][mode]['mae_vs_full_sr_then_downsample'] = error
            assert error < 1, error
(out / 'platform-client-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report), flush=True)
