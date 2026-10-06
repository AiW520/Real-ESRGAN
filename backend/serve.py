"""Supported single-worker entry point for local or internal production service."""
import argparse
import ipaddress
import os
import uvicorn


def main():
    parser = argparse.ArgumentParser(description='Start the image-processing API with one worker.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int)
    parser.add_argument('--env-file', help='Optional server-only dotenv file; existing process variables take precedence.')
    args = parser.parse_args()
    if args.env_file:
        from pathlib import Path
        from dotenv import load_dotenv
        path = Path(args.env_file)
        if not path.is_file():
            parser.error('The server environment file does not exist.')
        load_dotenv(path, override=False)
    from .app.security import settings
    if args.port is None:
        try:
            args.port = int(os.getenv('UPSCALER_PORT', '8000'))
        except ValueError:
            parser.error('UPSCALER_PORT must be an integer.')
    settings.validate()
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == 'localhost'
    if not settings.production and not loopback:
        parser.error('Non-loopback binding requires UPSCALER_PRODUCTION=1 and a service token.')
    if not 1 <= args.port <= 65535:
        parser.error('Port must be 1-65535.')
    uvicorn.run('backend.app.main:app', host=args.host, port=args.port, workers=1,
                proxy_headers=False, server_header=False, timeout_keep_alive=10,
                timeout_graceful_shutdown=120)


if __name__ == '__main__':
    main()
