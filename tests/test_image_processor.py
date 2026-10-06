from pathlib import Path

import pytest
from PIL import Image

from backend.app.image_processor import inspect_image, target_size


def test_target_sizes_keep_ratio():
    assert target_size(1920, 1080, scale=2, resolution='scale') == (3840, 2160)
    assert target_size(3840, 2160, scale=4, resolution='8K') == (7680, 4320)
    assert target_size(1000, 700, scale=2, resolution='scale') == (2000, 1400)


def test_input_verification(tmp_path: Path):
    path = tmp_path / 'rgba.png'
    Image.new('RGBA', (32, 24), (255, 10, 5, 128)).save(path)
    info = inspect_image(path)
    assert (info.width, info.height, info.alpha) == (32, 24, True)


def test_rejects_bad_target_ratio():
    with pytest.raises(ValueError, match='aspect ratio'):
        target_size(100, 100, scale=2, resolution='original', target_width=400, target_height=100)
