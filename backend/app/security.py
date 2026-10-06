"""Controls for a single trusted platform backend, not end-user account auth."""
from dataclasses import dataclass, field
import hmac
import os
import time
import threading
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


@dataclass(frozen=True)
class ServiceSettings:
    production: bool = False
    api_token: str = field(default='', repr=False)
    allowed_hosts: tuple = ('127.0.0.1', 'localhost', 'testserver')
    allowed_origins: tuple = ('http://127.0.0.1:5173', 'http://localhost:5173')
    generation_enabled: bool = True
    request_limit: int = 30

    @classmethod
    def from_environment(cls):
        production = os.getenv('UPSCALER_PRODUCTION', '0') == '1'
        hosts = tuple(v.strip() for v in os.getenv('UPSCALER_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',') if v.strip())
        origins = tuple(v.strip() for v in os.getenv('UPSCALER_ALLOWED_ORIGINS', '' if production else 'http://127.0.0.1:5173,http://localhost:5173').split(',') if v.strip())
        return cls(production=production, api_token=os.getenv('UPSCALER_API_TOKEN', ''),
                   allowed_hosts=hosts, allowed_origins=origins,
                   generation_enabled=os.getenv('UPSCALER_ENABLE_GENERATION', '0' if production else '1') == '1',
                   request_limit=int(os.getenv('UPSCALER_SUBMISSIONS_PER_MINUTE', '30')))

    def validate(self):
        if self.production and (len(self.api_token) < 32 or not self.api_token.isascii() or any(c.isspace() for c in self.api_token)):
            raise RuntimeError('Production requires an ASCII UPSCALER_API_TOKEN of at least 32 characters without whitespace.')
        if not self.allowed_hosts or '*' in self.allowed_hosts:
            raise RuntimeError('Set explicit UPSCALER_ALLOWED_HOSTS; wildcard hosts are not supported.')
        if not 1 <= self.request_limit <= 10000:
            raise RuntimeError('UPSCALER_SUBMISSIONS_PER_MINUTE must be between 1 and 10000.')


class ServiceSecurity:
    def __init__(self, app, settings, max_body_bytes):
        settings.validate()
        self.app, self.settings, self.max_body_bytes = app, settings, max_body_bytes
        self.submissions = []
        self.lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        path, method = scope['path'], scope['method']
        async def reject(code, detail, extra=None):
            await JSONResponse({'detail': detail}, status_code=code, headers=extra)(scope, receive, send)
        if self.settings.production and path not in {'/healthz', '/readyz'}:
            if not path.startswith('/api/'):
                return await reject(404, 'This deployment serves the platform API only.')
            provided = headers.get('authorization', '')
            if not hmac.compare_digest(provided.encode('utf-8'), ('Bearer ' + self.settings.api_token).encode('utf-8')):
                return await reject(401, 'A valid platform service token is required.', {'WWW-Authenticate': 'Bearer'})
        if method == 'POST':
            own_origin = f"{scope.get('scheme', 'http')}://{headers.get('host', '')}"
            origin = headers.get('origin')
            if origin and origin not in (own_origin, *self.settings.allowed_origins):
                return await reject(403, 'Request origin is not allowed.')
            length = headers.get('content-length')
            if length and (not length.isascii() or not length.isdigit() or int(length) > self.max_body_bytes):
                return await reject(413, 'Request exceeds the upload size limit.')
            submission = path in {'/api/upscale', '/api/generate', '/api/inspect'} or path.endswith('/continue')
            if self.settings.production and submission:
                now = time.monotonic()
                with self.lock:
                    self.submissions = [stamp for stamp in self.submissions if now - stamp < 60]
                    limited = len(self.submissions) >= self.settings.request_limit
                    if not limited:
                        self.submissions.append(now)
                if limited:
                    return await reject(429, 'Submission rate limit reached. Retry later using the same Idempotency-Key.', {'Retry-After': '60'})
        received, exceeded, response_started = 0, False, False

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            received += len(message.get('body', b''))
            if received > self.max_body_bytes:
                exceeded = True
                raise HTTPException(413, 'Request exceeds the upload size limit.')
            return message

        async def safe_send(message):
            nonlocal response_started
            if message['type'] == 'http.response.start':
                response_started = True
                # Multipart parsers may translate receive errors into 400; preserve the real limit status.
                if exceeded:
                    message = {**message, 'status': 413}
                message = {**message, 'headers': list(message.get('headers', [])) + [
                    (b'x-content-type-options', b'nosniff'), (b'referrer-policy', b'no-referrer'),
                    (b'cache-control', b'no-store') if path.startswith('/api/') else (b'x-frame-options', b'DENY')]}
            await send(message)

        try:
            await self.app(scope, limited_receive if method == 'POST' else receive, safe_send)
        except HTTPException as exc:
            if exceeded and not response_started:
                return await reject(413, 'Request exceeds the upload size limit.')
            raise exc


settings = ServiceSettings.from_environment()
