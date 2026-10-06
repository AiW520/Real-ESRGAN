"""Tuzi gpt-image-2 wrapper used by the server-side generation workflow."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

MODEL = "gpt-image-2"
MAX_PROMPT = 64 * 1024
ASPECT_SIZES = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536"}
DEFAULT_SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'tuzi_generate_image.py'


class GenerationError(RuntimeError):
    pass


def script_path() -> Path:
    configured = os.getenv("UPSCALER_TUZI_SCRIPT", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_SCRIPT


def readiness() -> dict:
    """Run the skill's non-billing readiness check without exposing credentials."""
    script = script_path()
    if not script.is_file():
        return {"available": False, "model": MODEL, "reason": "Tuzi 生成脚本不可用。"}
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--status", "--channel", "coding"],
            capture_output=True, text=True, timeout=20, check=False,
            encoding='utf-8', env={**os.environ, 'PYTHONUTF8': '1'},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(result.stdout or "{}") if result.returncode == 0 else {}
        if result.returncode == 0 and payload.get("configured") is True:
            return {"available": True, "model": MODEL}
        return {"available": False, "model": MODEL, "reason": "未配置 Tuzi coding 凭据。"}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"available": False, "model": MODEL, "reason": "无法检查 Tuzi 生成服务。"}


def _validate_prompt(prompt: str) -> str:
    prompt = str(prompt or "").strip()
    if not prompt:
        raise GenerationError("生成模式需要填写提示词。")
    if len(prompt.encode("utf-8")) > MAX_PROMPT:
        raise GenerationError("提示词不能超过 64 KB。")
    return prompt


def generate_base_image(prompt: str, aspect: str, destination: Path, cancel=lambda: None) -> tuple[int, int]:
    prompt = _validate_prompt(prompt)
    if aspect not in ASPECT_SIZES:
        raise GenerationError("不支持的基础图比例。")
    script = script_path()
    if not script.is_file():
        raise GenerationError("Tuzi 生成脚本不可用。")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, str(script), "--channel", "coding", "--prompt", prompt,
        "--size", ASPECT_SIZES[aspect], "--quality", "auto", "--output-format", "png",
        "--background", "opaque", "--output-dir", str(destination.parent), "--filename", destination.stem.replace('.', '-') + '.png',
    ]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', env={**os.environ, 'PYTHONUTF8': '1'},
            shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise GenerationError("无法启动 Tuzi 生成服务。") from exc
    try:
        started = time.monotonic()
        while True:
            cancel()
            if time.monotonic() - started > 600:
                raise GenerationError('生成等待超时；请求可能已消耗额度，请勿立即重复提交。')
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                continue
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    if process.returncode != 0:
        # Provider errors can contain prompts; persist only an error code.
        try:
            code = int(json.loads(stderr or '{}').get('code', 3))
        except (ValueError, TypeError):
            code = 3
        raise GenerationError(f'Tuzi 生成请求失败（{code}）；未自动重试，请检查服务配置和额度。超时或取消的请求可能已计费。')
    try:
        response = json.loads(stdout or "{}")
        saved = response.get("saved_files")
        generated = Path(saved[0]).resolve() if isinstance(saved, list) and saved else None
    except (ValueError, TypeError, OSError) as exc:
        raise GenerationError("Tuzi 生成服务返回了无效结果。") from exc
    if response.get('code') != 0 or not generated or generated.parent != destination.parent or not generated.is_file() or generated.stat().st_size == 0:
        raise GenerationError("Tuzi 生成服务没有返回有效基础图片。")
    if generated != destination:
        try:
            generated.replace(destination)
            generated = destination
        except OSError as exc:
            generated.unlink(missing_ok=True)
            raise GenerationError("无法保存 Tuzi 基础图片。") from exc
    cancel()
    temporary = None
    try:
        with Image.open(generated) as image:
            image.load()
            dimensions = image.size
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise GenerationError("Tuzi 返回的基础图片格式不受支持。")
            if image.format != 'PNG':
                temporary = destination.with_suffix('.normalized.png')
                try:
                    image.save(temporary, format='PNG')
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
        if temporary is not None:
            temporary.replace(destination)
    except GenerationError:
        generated.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        generated.unlink(missing_ok=True)
        raise GenerationError("Tuzi 返回的基础图片无法读取。") from exc
    return dimensions
