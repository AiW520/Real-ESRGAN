#!/usr/bin/env python3
"""Generate one image through a fixed Tuzi channel without MCP dependencies."""

from __future__ import annotations

import argparse
import base64
import binascii
import http.client
import ipaddress
import json
import os
import pathlib
import socket
import ssl
import sys
import time
import uuid
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

MODEL = "gpt-image-2"
CHANNELS = {
    "coding": ("https://api.tu-zi.com/coding/images/generations", "TUZI_CODING_API_KEY"),
    "api": ("https://api.tu-zi.com/v1/images/generations", "TUZI_API_KEY"),
}
QUALITIES = {"auto", "low", "medium", "high"}
FORMATS = {"png", "jpeg", "webp"}
BACKGROUNDS = {"auto", "transparent", "opaque"}
SIZES = {"auto", "1024x1024", "1536x1024", "1024x1536"}
MAX_AUTH_BYTES = 64 * 1024
MAX_JSON_BYTES = 80 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_PROMPT_BYTES = 64 * 1024
TIMEOUT_SECONDS = 180
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_REDIRECTS = 3
DOWNLOAD_ATTEMPTS = 3


class SkillError(RuntimeError):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def codex_home() -> pathlib.Path:
    configured = os.getenv("CODEX_HOME", "").strip()
    return pathlib.Path(configured).expanduser().resolve() if configured else (pathlib.Path.home() / ".codex").resolve()


def normalize_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    key = value.strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if not key or any(character.isspace() for character in key) or len(key.encode("utf-8")) > 4096:
        return ""
    return key


def load_codex_key() -> tuple[str, pathlib.Path, str]:
    auth_path = codex_home() / "auth.json"
    try:
        if not auth_path.is_file() or auth_path.stat().st_size > MAX_AUTH_BYTES:
            return "", auth_path, "missing"
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "", auth_path, "invalid"
    if not isinstance(payload, dict):
        return "", auth_path, "invalid"
    auth_mode = str(payload.get("auth_mode") or "").strip().lower()
    if auth_mode and auth_mode != "apikey":
        return "", auth_path, auth_mode
    return normalize_key(payload.get("OPENAI_API_KEY")), auth_path, "apikey"


def load_credential(channel: str) -> tuple[str, str]:
    _, env_name = CHANNELS[channel]
    environment_key = normalize_key(os.getenv(env_name))
    if environment_key:
        return environment_key, env_name
    if channel == "coding":
        process_key = normalize_key(os.getenv("OPENAI_API_KEY"))
        if process_key:
            return process_key, "process-OPENAI_API_KEY"
        codex_key, _, auth_mode = load_codex_key()
        if codex_key:
            return codex_key, "codex-auth"
        if auth_mode not in {"missing", "invalid", "apikey"}:
            raise SkillError("Codex is using a session credential that cannot be exported as a Tuzi API Key. Set TUZI_CODING_API_KEY explicitly.", 2)
        raise SkillError(f"No coding credential found in {env_name}, OPENAI_API_KEY, or Codex auth.json.", 2)
    raise SkillError(f"No API-site credential found in {env_name}.", 2)


def validate_size(value: str) -> str:
    size = value.strip().lower() or "auto"
    if size not in SIZES:
        raise SkillError("size must be auto, 1024x1024, 1536x1024, or 1024x1536.", 2)
    return size


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    prompt = str(args.prompt or "").strip()
    if not prompt:
        raise SkillError("prompt cannot be empty.", 2)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise SkillError("prompt exceeds 65536 UTF-8 bytes.", 2)
    quality = args.quality.lower()
    output_format = args.output_format.lower()
    background = args.background.lower()
    if quality not in QUALITIES:
        raise SkillError("quality must be auto, low, medium, or high.", 2)
    if output_format not in FORMATS:
        raise SkillError("output-format must be png, jpeg, or webp.", 2)
    if background not in BACKGROUNDS:
        raise SkillError("background must be auto, transparent, or opaque.", 2)
    if background == "transparent" and output_format == "jpeg":
        raise SkillError("transparent background requires PNG or WebP.", 2)
    return {
        "model": MODEL,
        "prompt": prompt,
        "n": 1,
        "size": validate_size(args.size),
        "quality": quality,
        "output_format": output_format,
        "background": background,
    }


def read_limited(stream: BinaryIO, maximum: int, label: str) -> bytes:
    content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise SkillError(f"{label} exceeds {maximum} bytes.", 3)
    return content


def sanitize_provider_error(payload: bytes, status: int, key: str) -> str:
    message = f"HTTP {status}"
    try:
        body = json.loads(payload.decode("utf-8"))
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or message)
            else:
                message = str(body.get("message") or error or message)
    except Exception:
        pass
    return message.replace(key, "[REDACTED]").replace("\r", " ").replace("\n", " ")[:500]


def post_generation(url: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "tuzi-image-generation/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = read_limited(response, MAX_JSON_BYTES, "API response")
    except HTTPError as exc:
        body = read_limited(exc, 64 * 1024, "error response")
        raise SkillError(f"Tuzi API {exc.code}: {sanitize_provider_error(body, exc.code, key)}", int(exc.code)) from exc
    except (URLError, TimeoutError, socket.timeout, ssl.SSLError, ConnectionError) as exc:
        raise SkillError("Generation failed or timed out. It was not retried to avoid duplicate billing.", 3) from exc
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SkillError("Tuzi API returned invalid JSON.", 3) from exc
    if not isinstance(result, dict):
        raise SkillError("Tuzi API returned an invalid response object.", 3)
    return result


def public_addresses(hostname: str) -> list[tuple[int, str]]:
    if hostname.lower() == "localhost" or hostname.lower().endswith((".localhost", ".local")):
        raise SkillError("Image URL resolves to a local address.", 3)
    try:
        records = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SkillError("Image hostname could not be resolved.", 3) from exc
    addresses: list[tuple[int, str]] = []
    for family, _, _, _, socket_address in records:
        address = socket_address[0]
        try:
            if not ipaddress.ip_address(address).is_global:
                raise SkillError("Image URL resolves to a private or reserved address.", 3)
        except ValueError as exc:
            raise SkillError("Image URL resolved to an invalid address.", 3) from exc
        entry = (family, address)
        if entry not in addresses:
            addresses.append(entry)
    if not addresses:
        raise SkillError("Image hostname has no public address.", 3)
    return addresses


def pinned_https_get(url: str) -> tuple[http.client.HTTPResponse, socket.socket]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SkillError("Image URL must be an HTTPS URL without credentials.", 3)
    family, address = public_addresses(parsed.hostname)[0]
    raw_socket = socket.socket(family, socket.SOCK_STREAM)
    tls_socket: socket.socket | None = None
    raw_socket.settimeout(DOWNLOAD_TIMEOUT_SECONDS)
    try:
        raw_socket.connect((address, parsed.port or 443))
        tls_socket = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=parsed.hostname)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        host_header = parsed.hostname if (parsed.port in (None, 443)) else f"{parsed.hostname}:{parsed.port}"
        request = (
            f"GET {target} HTTP/1.1\r\nHost: {host_header}\r\n"
            "Accept: image/png,image/jpeg,image/webp\r\n"
            "User-Agent: tuzi-image-generation/1.0\r\nConnection: close\r\n\r\n"
        )
        tls_socket.sendall(request.encode("ascii"))
        response = http.client.HTTPResponse(tls_socket)
        response.begin()
        return response, tls_socket
    except Exception:
        if tls_socket is not None:
            tls_socket.close()
        else:
            raw_socket.close()
        raise


def image_format(prefix: bytes) -> tuple[str, str]:
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise SkillError("Downloaded content is not a valid PNG, JPEG, or WebP image.", 3)


def safe_stem(filename: str) -> str:
    raw = pathlib.Path(filename).name if filename else f"tuzi-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    stem = pathlib.Path(raw).stem
    cleaned = "".join(character if character.isalnum() or character in "._-" else "-" for character in stem).lstrip(".")
    return (cleaned[:100] or "tuzi-image")


def publish_temp(temporary: pathlib.Path, output_dir: pathlib.Path, stem: str, extension: str) -> pathlib.Path:
    destination = output_dir / f"{stem}.{extension}"
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise SkillError(f"Output file already exists: {destination.name}", 3) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return destination.resolve()


def stream_response(response: http.client.HTTPResponse, output_dir: pathlib.Path, stem: str) -> pathlib.Path:
    content_type = str(response.getheader("Content-Type") or "").split(";", 1)[0].lower()
    if not content_type.startswith("image/"):
        raise SkillError("Image download did not return image/* content.", 3)
    content_length = int(response.getheader("Content-Length") or 0)
    if content_length > MAX_IMAGE_BYTES:
        raise SkillError("Image exceeds the size limit.", 3)
    temporary = output_dir / f".{uuid.uuid4().hex}.part"
    received = 0
    prefix = b""
    try:
        with temporary.open("xb") as output:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_IMAGE_BYTES:
                    raise SkillError("Image exceeds the size limit.", 3)
                if len(prefix) < 16:
                    prefix += chunk[: 16 - len(prefix)]
                output.write(chunk)
        extension, actual_mime = image_format(prefix)
        if content_type not in (actual_mime, "image/jpg"):
            raise SkillError("Image MIME type does not match its file signature.", 3)
        return publish_temp(temporary, output_dir, stem, extension)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def download_image(url: str, output_dir: pathlib.Path, stem: str) -> pathlib.Path:
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        current = url
        try:
            for redirect in range(DOWNLOAD_REDIRECTS + 1):
                response, connection = pinned_https_get(current)
                try:
                    if 300 <= response.status < 400 and response.getheader("Location"):
                        if redirect == DOWNLOAD_REDIRECTS:
                            raise SkillError("Image download redirected too many times.", 3)
                        current = urljoin(current, str(response.getheader("Location")))
                        continue
                    if not 200 <= response.status < 300:
                        error = SkillError(f"Image download failed with HTTP {response.status}.", 3)
                        setattr(error, "retryable", response.status in {408, 425, 429, 500, 502, 503, 504})
                        raise error
                    return stream_response(response, output_dir, stem)
                finally:
                    response.close()
                    connection.close()
        except (OSError, ssl.SSLError, socket.timeout, SkillError) as exc:
            last_error = exc
            retryable = not isinstance(exc, SkillError) or bool(getattr(exc, "retryable", False))
            if not retryable or attempt == DOWNLOAD_ATTEMPTS:
                raise
            time.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
    raise last_error or SkillError("Image download failed.", 3)


def decode_base64_image(encoded: str, output_dir: pathlib.Path, stem: str) -> pathlib.Path:
    compact = encoded if not any(character.isspace() for character in encoded) else "".join(encoded.split())
    if len(compact) * 3 // 4 > MAX_IMAGE_BYTES:
        raise SkillError("Base64 image exceeds the size limit.", 3)
    temporary = output_dir / f".{uuid.uuid4().hex}.part"
    prefix = b""
    written = 0
    chunk_size = 4 * 16_384
    try:
        with temporary.open("xb") as output:
            for offset in range(0, len(compact), chunk_size):
                try:
                    chunk = base64.b64decode(compact[offset : offset + chunk_size], validate=True)
                except binascii.Error as exc:
                    raise SkillError("Tuzi API returned invalid Base64 image data.", 3) from exc
                written += len(chunk)
                if written > MAX_IMAGE_BYTES:
                    raise SkillError("Base64 image exceeds the size limit.", 3)
                if len(prefix) < 16:
                    prefix += chunk[: 16 - len(prefix)]
                output.write(chunk)
        extension, _ = image_format(prefix)
        return publish_temp(temporary, output_dir, stem, extension)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def save_result(result: dict[str, Any], args: argparse.Namespace) -> pathlib.Path:
    data = result.get("data")
    if not isinstance(data, list) or not data:
        raise SkillError("Tuzi API returned no image data.", 3)
    item = data[0]
    output_dir = pathlib.Path(args.output_dir).expanduser().resolve() if args.output_dir else (pathlib.Path.cwd() / "outputs" / "tuzi-image").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(args.filename)
    if isinstance(item, str):
        return download_image(item, output_dir, stem)
    if not isinstance(item, dict):
        raise SkillError("Tuzi API returned an unsupported image item.", 3)
    image_url = item.get("url") or item.get("oss_url")
    encoded = item.get("b64_json") or item.get("base64")
    if isinstance(image_url, str):
        return download_image(image_url, output_dir, stem)
    if isinstance(encoded, str):
        return decode_base64_image(encoded, output_dir, stem)
    raise SkillError("Tuzi API response has no supported image content.", 3)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Generate one image with Tuzi gpt-image-2 without MCP.")
    result.add_argument("--status", action="store_true")
    result.add_argument("--channel", choices=sorted(CHANNELS), default=os.getenv("TUZI_IMAGE_CHANNEL", "coding"))
    result.add_argument("--prompt", default="")
    result.add_argument("--size", default="auto")
    result.add_argument("--quality", default="auto")
    result.add_argument("--output-format", default="png")
    result.add_argument("--background", default="auto")
    result.add_argument("--output-dir", default="")
    result.add_argument("--filename", default="")
    return result


def main() -> int:
    key = ""
    try:
        args = parser().parse_args()
        endpoint, _ = CHANNELS[args.channel]
        key, source = load_credential(args.channel)
        if args.status:
            print(json.dumps({"configured": True, "channel": args.channel, "model": MODEL, "endpoint": endpoint, "credential_source": source}, ensure_ascii=False))
            return 0
        payload = build_payload(args)
        result = post_generation(endpoint, key, payload)
        saved = save_result(result, args)
        print(json.dumps({"code": 0, "channel": args.channel, "model": MODEL, "saved_files": [str(saved)]}, ensure_ascii=False))
        return 0
    except SkillError as exc:
        print(json.dumps({"code": exc.code, "message": str(exc).replace(key, "[REDACTED]"), "saved_files": []}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"code": 3, "message": str(exc).replace(key, "[REDACTED]")[:500], "saved_files": []}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
