import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(os.getenv('UPSCALER_CACHE_DIR', str(ROOT / '.cache'))).expanduser().resolve()
for name, path in {
    'TEMP': CACHE / 'tmp', 'TMP': CACHE / 'tmp',
    'TORCH_HOME': CACHE / 'torch', 'HF_HOME': CACHE / 'huggingface',
    'XDG_CACHE_HOME': CACHE, 'PIP_CACHE_DIR': CACHE / 'pip',
    'MPLCONFIGDIR': CACHE / 'matplotlib', 'NUMBA_CACHE_DIR': CACHE / 'numba',
}.items():
    path.mkdir(parents=True, exist_ok=True)
    os.environ[name] = str(path)
tempfile.tempdir = str(CACHE / 'tmp')

MODELS = Path(os.getenv('UPSCALER_MODELS_DIR', str(ROOT / 'models'))).expanduser().resolve()
DATA = Path(os.getenv('UPSCALER_DATA_DIR', str(ROOT / 'data'))).expanduser().resolve()
for path in (MODELS, DATA / 'uploads', DATA / 'outputs', DATA / 'jobs'):
    path.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD = 50 * 1024 * 1024
MAX_INPUT_PIXELS = 33_177_600
MAX_OUTPUT_PIXELS = 33_177_600
MAX_EDGE = 7680
MAX_JOBS = int(os.getenv('UPSCALER_MAX_JOBS', '4'))
JOB_TIMEOUT = int(os.getenv('UPSCALER_TIMEOUT', '21600'))
TTL_SECONDS = int(os.getenv('UPSCALER_TTL', '86400'))
if not 1 <= MAX_JOBS <= 64 or JOB_TIMEOUT < 1 or TTL_SECONDS < 60:
    raise ValueError('Queue must be 1-64, timeout positive, and result TTL at least 60 seconds.')
