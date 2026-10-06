"""Readiness audit: no paid calls, no downloads unless --warm-models is explicit."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--warm-models', action='store_true', help='Load/verify official SR models; download missing weights.')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        if not args.env_file.is_file():
            parser.error('The server environment file does not exist.')
        load_dotenv(args.env_file, override=False)
    from backend.app.config import DATA, MODELS, CACHE, MAX_JOBS, JOB_TIMEOUT, TTL_SECONDS
    from backend.app.security import settings
    from backend.app.model_manager import REGISTRY, checksum, manager, select_device
    checks = []
    def check(name, passed, detail):
        checks.append({'name': name, 'passed': bool(passed), 'detail': detail})
    try:
        settings.validate()
        check('service_configuration', True, 'Configuration is valid; secrets are omitted.')
    except (RuntimeError, ValueError) as exc:
        check('service_configuration', False, str(exc))
    check('production_mode', settings.production, 'Production mode must be enabled for deployment.')
    for name, directory in [('data', DATA), ('models', MODELS), ('cache', CACHE)]:
        probe = directory / f'.preflight-{uuid.uuid4().hex}'
        try:
            probe.write_bytes(b'probe')
            probe.unlink()
            check(name + '_writable', True, str(directory))
        except OSError:
            check(name + '_writable', False, 'Directory is not writable.')
    check('disk_space', shutil.disk_usage(DATA).free >= 2 * 1024**3, 'Require at least 2 GiB free for a baseline worker.')
    check('memory', psutil.virtual_memory().available >= 1024**3, 'Require at least 1 GiB available; large inputs need more.')
    if args.warm_models:
        for name in REGISTRY:
            try:
                manager.load(name)
                check(name + '_load', True, 'Official model loaded on ' + select_device())
            except Exception:
                check(name + '_load', False, 'Model could not be loaded. Check runtime, weight provenance and memory.')
    for name in REGISTRY:
        weight, manifest = MODELS / f'{name}.pth', MODELS / f'{name}.json'
        valid = False
        try:
            entry = json.loads(manifest.read_text('utf-8'))
            valid = weight.is_file() and checksum(weight) == entry.get('sha256')
        except (OSError, ValueError):
            pass
        check(name + '_cached', valid, 'Cached weight checksum verified.' if valid else 'Use --warm-models to prepare official model weights.')
    if settings.generation_enabled:
        from backend.app.generation import readiness
        configured = readiness().get('available', False)
        check('generation_configuration', configured, 'Checks configuration only, not upstream availability or billing.')
    report = {'ready_for_local_production_smoke': all(item['passed'] for item in checks),
              'platform': platform.platform(), 'python': platform.python_version(), 'device': select_device(),
              'queue_limit': MAX_JOBS, 'job_timeout_seconds': JOB_TIMEOUT, 'result_ttl_seconds': TTL_SECONDS,
              'generation_enabled': settings.generation_enabled, 'checks': checks,
              'limits': ['Not a target-server acceptance test.', 'No paid generation request was made.',
                         'One worker per data directory; platform owns end-user permissions and quotas.']}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ready_for_local_production_smoke'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
