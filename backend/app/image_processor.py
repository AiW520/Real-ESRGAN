import io
import math
import shutil
import struct
import warnings
from dataclasses import dataclass

import numpy as np
import psutil
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from .config import DATA, MAX_EDGE, MAX_INPUT_PIXELS, MAX_OUTPUT_PIXELS

Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS
FORMATS = {'JPEG', 'PNG', 'WEBP', 'TIFF'}
SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()


@dataclass
class ImageInfo:
    width: int
    height: int
    format: str
    alpha: bool


def inspect_image(path):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in FORMATS:
                    raise ValueError('Only JPG, PNG, WEBP and TIFF images are supported.')
                if getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('Animated or multi-page images are not supported.')
                if image.width * image.height > MAX_INPUT_PIXELS:
                    raise ValueError('Input exceeds the 33.2 MP pixel limit.')
                if image.mode in ('I', 'F', 'I;16', 'I;16B', 'I;16L'):
                    raise ValueError('HDR / 16-bit input is not supported by the 8-bit RGB model. Convert to SDR first.')
                if image.format == 'PNG':
                    with open(path, 'rb') as header:
                        header.seek(16)
                        _, _, depth, *_ = struct.unpack('>IIBBBBB', header.read(13))
                    if depth > 8:
                        raise ValueError('16-bit PNG is not supported. Convert to 8-bit SDR first.')
                if image.format == 'TIFF' and max(image.tag_v2.get(258, (8,))) > 8:
                    raise ValueError('16-bit TIFF is not supported. Convert to 8-bit SDR first.')
                fmt = image.format
                image.verify()
            with Image.open(path) as image:
                oriented = ImageOps.exif_transpose(image)
                alpha = 'A' in oriented.getbands() or 'transparency' in oriented.info
                return ImageInfo(*oriented.size, fmt, alpha)
    except (UnidentifiedImageError, OSError, SyntaxError, struct.error, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('The upload is damaged, is not a valid image, or exceeds the pixel limit.') from exc


def target_size(width, height, scale=2, resolution=None, target_width=None, target_height=None, processing_mode='upscale'):
    if processing_mode not in ('upscale', 'resize', 'ai_resize'):
        raise ValueError('Unknown processing mode.')
    shrinking = processing_mode != 'upscale'
    if shrinking and scale not in (0.25, 0.5, 1):
        raise ValueError('Shrink scale must be 0.25, 0.5 or 1.')
    if not shrinking and scale not in (1, 2, 4):
        raise ValueError('Scale must be 1, 2 or 4.')
    if resolution not in (None, '', 'scale', 'original', '2K', '4K', '8K'):
        raise ValueError('Target resolution must be original, 2K, 4K or 8K.')
    if resolution in ('2K', '4K', '8K') and (target_width or target_height):
        raise ValueError('Choose a resolution preset or explicit dimensions, not both.')
    if target_width or target_height:
        if target_width is not None and target_width < 1 or target_height is not None and target_height < 1:
            raise ValueError('Target dimensions must be positive.')
        if target_width and target_height:
            expected = width / height
            if abs(target_width / target_height - expected) > max(1 / target_height, expected / target_width):
                raise ValueError('Target dimensions would change the image aspect ratio.')
        factor = target_width / width if target_width else target_height / height
    elif resolution in ('2K', '4K', '8K'):
        longest, pixels = {'2K': (2560, 2560 * 1440), '4K': (3840, 3840 * 2160),
                           '8K': (7680, MAX_OUTPUT_PIXELS)}[resolution]
        factor = min(longest / max(width, height), math.sqrt(pixels / (width * height)))
    elif resolution == 'original':
        factor = 1
    else:
        factor = scale
    out_width, out_height = max(1, round(width * factor)), max(1, round(height * factor))
    if shrinking:
        out_width, out_height = max(1, math.floor(width * factor + 0.5)), max(1, math.floor(height * factor + 0.5))
    if resolution in ('2K', '4K', '8K') and out_width * out_height > pixels:
        out_width, out_height = max(1, math.floor(width * factor)), max(1, math.floor(height * factor))
    if target_width and target_height:
        out_width, out_height = target_width, target_height
    if out_width * out_height > MAX_OUTPUT_PIXELS or max(out_width, out_height) > MAX_EDGE:
        raise ValueError('The requested output exceeds 33.2 MP or the 7680 px longest-edge limit. Choose a smaller scale or the 8K preset.')
    if shrinking and (out_width > width or out_height > height):
        raise ValueError('Shrink modes cannot enlarge the input.')
    if processing_mode == 'ai_resize' and factor < 0.25:
        raise ValueError('AI shrinking supports at least 25% of input dimensions. Use plain resize for smaller outputs.')
    if not shrinking and min(out_width / width, out_height / height) < 1 - 1 / min(width, height):
        raise ValueError('The target is smaller than the input. Choose original enhancement or a larger target.')
    if factor > 4 + 1 / min(width, height):
        raise ValueError('The requested scale exceeds 4x. Choose a smaller target.')
    return out_width, out_height


def preflight(width, height, out_width, out_height, face=False):
    input_pixels, output_pixels = width * height, out_width * out_height
    ram_required = input_pixels * 9 + output_pixels * (32 if face else 7) + 512 * 1024**2
    disk_required = output_pixels * 24 + input_pixels * 8 + 512 * 1024**2
    if psutil.virtual_memory().available < ram_required:
        raise ValueError(f'Insufficient RAM: approximately {ram_required / 1024**3:.1f} GB of available memory is needed.')
    if shutil.disk_usage(DATA).free < disk_required:
        raise ValueError(f'Insufficient D-drive space: at least {disk_required / 1024**3:.1f} GB free is needed.')
    return ram_required


def read_pixels(path):
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        alpha = image.convert('RGBA').getchannel('A') if ('A' in image.getbands() or 'transparency' in image.info) else None
        icc = image.info.get('icc_profile')
        if icc:
            try:
                original = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                color_input = image.getchannel('L') if image.mode == 'LA' else image
                converted = ImageCms.profileToProfile(color_input.convert('RGB') if color_input.mode not in ('RGB', 'CMYK', 'LAB', 'L') else color_input,
                                                     original, ImageCms.createProfile('sRGB'), outputMode='RGB')
                image = converted
            except Exception as exc:
                raise ValueError('The embedded ICC profile cannot be converted safely to sRGB.') from exc
        pixels = np.asarray(image.convert('RGB'))
        gamma = source.info.get('gamma')
        if not icc and gamma is not None and 'srgb' not in source.info and abs(gamma - 0.45455) > 0.0001:
            if not math.isfinite(gamma) or gamma <= 0:
                raise ValueError('Invalid PNG gamma metadata.')
            linear = (np.arange(256, dtype='float64') / 255) ** (1 / gamma)
            encoded = np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)
            pixels = np.rint(np.clip(encoded * 255, 0, 255)).astype('uint8')[pixels]
        return pixels, alpha


def encode(rgb, alpha, path, output_format, quality):
    image = Image.fromarray(rgb)
    try:
        if alpha is not None:
            enlarged = alpha.resize(image.size, Image.Resampling.LANCZOS)
            image.putalpha(enlarged)
            enlarged.close()
        options = {'icc_profile': SRGB_PROFILE}
        if output_format == 'jpg':
            if image.mode == 'RGBA':
                background = Image.new('RGB', image.size, 'white')
                background.paste(image, mask=image.getchannel('A'))
                image.close()
                image = background
            options.update(quality=quality, subsampling=0)
        elif output_format == 'webp':
            options.update(quality=quality, method=4)
        else:
            options.update(compress_level=4)
        image.save(path, format={'jpg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}[output_format], **options)
        with Image.open(path) as verification:
            verification.load()
            if verification.size != image.size:
                raise RuntimeError('Encoded output dimensions do not match.')
        if path.stat().st_size == 0:
            raise RuntimeError('The encoded output is empty.')
    finally:
        image.close()
