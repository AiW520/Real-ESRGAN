import gc
import numpy as np
import torch
import psutil
from PIL import Image
from .model_manager import manager


def choose_tile(device, requested=None):
    if requested:
        return int(requested)
    if device == 'cuda':
        free, _ = torch.cuda.mem_get_info()
        return 512 if free > 8 * 1024**3 else 384 if free > 4 * 1024**3 else 256
    return 256 if device == 'cpu' and psutil.virtual_memory().available > 3 * 1024**3 else 128


def _axis(length, tile, overlap):
    if length <= tile:
        return [0]
    values = list(range(0, length - tile + 1, tile - overlap))
    if values[-1] != length - tile:
        values.append(length - tile)
    return values


def _window(height, width, left, top, right, bottom):
    x, y = np.ones(width, dtype=np.float32), np.ones(height, dtype=np.float32)
    for vector, before, after in ((x, left, right), (y, top, bottom)):
        if before:
            size = min(len(vector), before)
            vector[:size] *= np.linspace(1 / (size + 1), 1, size, dtype=np.float32)
        if after:
            size = min(len(vector), after)
            vector[-size:] *= np.linspace(1, 1 / (size + 1), size, dtype=np.float32)
    return y[:, None, None] * x[None, :, None]


def infer_tile(model, crop):
    normalized = np.ascontiguousarray(crop.transpose(2, 0, 1), dtype=np.float32) / 255
    tensor = torch.from_numpy(normalized).unsqueeze(0).to(model.device)
    if model.device == 'cpu':
        tensor = tensor.contiguous(memory_format=torch.channels_last)
    if model.half:
        tensor = tensor.half()
    with torch.inference_mode():
        result = model.network(tensor).clamp_(0, 1)
        return result.squeeze(0).float().cpu().permute(1, 2, 0).mul_(255).round_().numpy().astype(np.uint8)


def close_map(array):
    if isinstance(array, np.memmap):
        array.flush()
        array._mmap.close()


def process_tiled(rgb, model, target_size, tile_size, workspace, tile_pad=24, overlap=16,
                  progress=lambda *_: None, cancel=lambda: None):
    """Halo inference and feathering at target resolution; no full native x4 canvas."""
    in_h, in_w = rgb.shape[:2]
    out_w, out_h = target_size
    sx, sy = out_w / in_w, out_h / in_h
    xs, ys = _axis(in_w, tile_size, overlap), _axis(in_h, tile_size, overlap)
    total = len(xs) * len(ys)
    workspace.mkdir(parents=True, exist_ok=True)
    accumulator = weights = None
    try:
        accumulator = np.memmap(workspace / 'rgb.f32', mode='w+', dtype='float32', shape=(out_h, out_w, 3))
        weights = np.memmap(workspace / 'weight.f32', mode='w+', dtype='float32', shape=(out_h, out_w, 1))
        for row, y in enumerate(ys):
            for column, x in enumerate(xs):
                cancel()
                x2, y2 = min(in_w, x + tile_size), min(in_h, y + tile_size)
                left, top = max(0, x - tile_pad), max(0, y - tile_pad)
                right, bottom = min(in_w, x2 + tile_pad), min(in_h, y2 + tile_pad)
                inferred = infer_tile(model, rgb[top:bottom, left:right])
                ox, oy, ox2, oy2 = round(x * sx), round(y * sy), round(x2 * sx), round(y2 * sy)
                # Correct only the AI-produced tile to the requested final scale.
                corrected = Image.fromarray(inferred).resize((ox2 - ox, oy2 - oy), Image.Resampling.LANCZOS,
                            box=((x - left) * 4, (y - top) * 4, (x2 - left) * 4, (y2 - top) * 4))
                tile = np.asarray(corrected)
                blend = _window(oy2 - oy, ox2 - ox,
                                round((xs[column - 1] + tile_size) * sx) - ox if column else 0,
                                round((ys[row - 1] + tile_size) * sy) - oy if row else 0,
                                ox2 - round(xs[column + 1] * sx) if column + 1 < len(xs) else 0,
                                oy2 - round(ys[row + 1] * sy) if row + 1 < len(ys) else 0)
                accumulator[oy:oy2, ox:ox2] += tile * blend
                weights[oy:oy2, ox:ox2] += blend
                corrected.close()
                del inferred, tile, blend
                progress(row * len(xs) + column + 1, total)
        progress(total, total, 'merging')
        output = np.memmap(workspace / 'output.u8', mode='w+', dtype='uint8', shape=(out_h, out_w, 3))
        try:
            for row in range(0, out_h, 64):
                cancel()
                w = weights[row:row + 64]
                if np.any(w <= 0):
                    raise RuntimeError('Tile coverage is incomplete.')
                output[row:row + 64] = np.rint(np.clip(accumulator[row:row + 64] / w, 0, 255)).astype('uint8')
            del w
            output.flush()
        except BaseException:
            close_map(output)
            raise
        return output
    finally:
        close_map(accumulator)
        close_map(weights)
        for name in ('rgb.f32', 'weight.f32'):
            (workspace / name).unlink(missing_ok=True)


def is_oom(exc):
    return isinstance(exc, (MemoryError, torch.OutOfMemoryError)) or any(
        text in str(exc).lower() for text in ('out of memory', 'not enough memory', "can't allocate memory"))


def process_with_fallback(rgb, model_name, model, target_size, requested_tile, workspace, progress, cancel):
    attempts = []
    tile = choose_tile(model.device, requested_tile)
    devices = [model.device] + (['cpu'] if model.device != 'cpu' else [])
    for device in devices:
        candidates = [tile] + [size for size in (512, 384, 256, 128, 64) if size < tile]
        for size in candidates:
            cancel()
            try:
                if model is None:
                    model = manager.load(model_name, device=device, cancel=cancel)
                attempts.append({'device': device, 'tile_size': size})
                result = process_tiled(rgb, model, target_size, size, workspace, progress=progress, cancel=cancel)
                return result, attempts
            except (RuntimeError, MemoryError) as exc:
                if not is_oom(exc):
                    if device != 'cpu' and any(text in str(exc).lower() for text in ('not implemented', 'not supported', 'unsupported')):
                        progress(0, 0, f'{device} cannot execute this operator; switching to CPU.')
                        break
                    raise
                error = str(exc)
            gc.collect()
            if device == 'cuda':
                torch.cuda.empty_cache()
            elif device == 'mps':
                torch.mps.empty_cache()
            progress(0, 0, f'Memory exhausted on {device} at {size}px; retrying with a smaller tile. {error[:80]}')
        if device != 'cpu':
            model = None
            manager.release(device)
            tile = 128
            progress(0, 0, 'Accelerator memory exhausted; switching to CPU.')
    raise RuntimeError('RAM is insufficient even with 64px tiles. Close other applications or use a smaller input.')
