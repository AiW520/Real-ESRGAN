import gc
import hashlib
import json
import os
import threading
import urllib.request
from filelock import FileLock
from dataclasses import dataclass

from .config import MODELS
import torch

from .rrdb import RRDBNet

REGISTRY = {
    'RealESRGAN_x4plus': {
        'blocks': 23, 'version': 'v0.1.0',
        'url': 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth',
    },
    'RealESRGAN_x4plus_anime_6B': {
        'blocks': 6, 'version': 'v0.2.2.4',
        'url': 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth',
    },
}


def select_device():
    if torch.cuda.is_available():
        return 'cuda'
    if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        return 'mps'
    return 'cpu'


def checksum(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def download_weight(name, url, cancel=lambda: None):
    with FileLock(MODELS / f'{name}.lock', timeout=300):
        cancel()
        return _download_weight(name, url, cancel)


def _download_weight(name, url, cancel):
    path = MODELS / f'{name}.pth'
    manifest_path = MODELS / f'{name}.json'
    if path.exists():
        if not manifest_path.exists():
            raise RuntimeError(f'Cached model {name} has no provenance manifest. Remove it and download the official release again.')
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text('utf-8'))
            if checksum(path) != manifest['sha256']:
                raise RuntimeError(f'Model checksum mismatch: {name}. Remove the corrupted cached weight and retry.')
        return path
    part = path.with_suffix('.part')
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'AI-Upscaler/1.0'})
        with urllib.request.urlopen(request, timeout=60) as response, open(part, 'wb') as stream:
            while chunk := response.read(1024 * 1024):
                cancel()
                stream.write(chunk)
        if part.stat().st_size < 1024 * 1024:
            raise RuntimeError('Model download was truncated.')
        # Restricted deserialization rejects pickled executable Python objects.
        torch.load(part, map_location='cpu', weights_only=True)
        os.replace(part, path)
        manifest_path.write_text(json.dumps({
            'url': url, 'sha256': checksum(path),
            'model': name, 'version': REGISTRY.get(name, {}).get('version', 'official release in source URL'),
            'checksum_source': 'locally computed after HTTPS download; upstream does not publish a checksum',
        }, indent=2), encoding='utf-8')
        return path
    except Exception as exc:
        part.unlink(missing_ok=True)
        if type(exc).__name__ == 'JobCancelled' or isinstance(exc, TimeoutError):
            raise
        raise RuntimeError(f'Unable to download {name} from the official release: {exc}') from exc


@dataclass
class LoadedModel:
    network: torch.nn.Module
    device: str
    half: bool


class ModelManager:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()
        # Default to physical cores: the old fixed value of 4 left most CPUs idle on 8+ core machines,
        # while hyper-threads add little for conv-heavy RRDB inference.
        import psutil
        cores = psutil.cpu_count(logical=False) or os.cpu_count() or 1
        configured = os.getenv('UPSCALER_THREADS')
        torch.set_num_threads(max(1, min(int(configured) if configured else cores, os.cpu_count() or 1)))

    def load(self, name, device=None, cancel=lambda: None):
        if name not in REGISTRY:
            raise ValueError('Unknown super-resolution model.')
        device = device or select_device()
        key = (name, device)
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            spec = REGISTRY[name]
            path = download_weight(name, spec['url'], cancel)
            cancel()
            checkpoint = torch.load(path, map_location='cpu', weights_only=True)
            state = checkpoint.get('params_ema', checkpoint.get('params', checkpoint))
            network = RRDBNet(spec['blocks'])
            network.load_state_dict(state, strict=True)
            del checkpoint, state
            network.eval().requires_grad_(False)
            network.to(device)
            if device == 'cpu':
                network.to(memory_format=torch.channels_last)
            half = device == 'cuda'
            if half:
                network.half()
            model = LoadedModel(network, device, half)
            self.cache[key] = model
            return model

    def release(self, device):
        for key in list(self.cache):
            if key[1] == device:
                del self.cache[key]
        gc.collect()
        if device == 'cuda' and torch.cuda.is_available():
            torch.cuda.empty_cache()
        elif device == 'mps' and hasattr(torch, 'mps') and torch.backends.mps.is_available():
            torch.mps.empty_cache()


manager = ModelManager()
