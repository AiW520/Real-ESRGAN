"""Build a source release from an explicit allowlist; never bundle runtime data or keys."""
import hashlib
from importlib import metadata
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
release_dir = ROOT / 'releases'
release_dir.mkdir(exist_ok=True)
destination = release_dir / 'ai-upscaler-1.1.0.zip'
files = set()
for name in ['README.md', 'DEPLOYMENT.md', 'RELEASE_NOTES.md', 'requirements.txt',
             'requirements-runtime.txt', 'pytest.ini', '.env.example', '.gitignore',
             'setup.ps1', 'start.ps1', 'start-production.ps1']:
    files.add(ROOT / name)
files.add(ROOT / 'THIRD_PARTY_NOTICES.md')
files.update(p for p in (ROOT / 'licenses').glob('*.txt') if p.is_file())
for directory, pattern in [('backend', '*.py'), ('tests', '*.py'), ('frontend/src', '*')]:
    files.update(p for p in (ROOT / directory).rglob(pattern) if p.is_file() and '__pycache__' not in p.parts)
for name in ['preflight.py', 'validate_production.py', 'tuzi_generate_image.py', 'build_release.py']:
    files.add(ROOT / 'scripts' / name)
for name in ['package.json', 'package-lock.json', 'vite.config.js', 'index.html', 'dist/index.html']:
    files.add(ROOT / 'frontend' / name)
files.update(p for p in (ROOT / 'frontend/dist/assets').glob('*') if p.is_file())
if not (ROOT / 'frontend/dist/index.html').is_file():
    raise RuntimeError('Build the frontend first.')

manifest = {}
with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(files):
        name = path.relative_to(ROOT).as_posix()
        if any(part in {'data', 'models', '.cache', '.venv', 'node_modules', 'secrets'} for part in Path(name).parts):
            raise RuntimeError('Runtime path is not allowed in release.')
        if path.name.startswith('.env') and path.name != '.env.example':
            raise RuntimeError('Secret configuration cannot be packaged.')
        content = path.read_bytes()
        manifest[name] = hashlib.sha256(content).hexdigest()
        archive.writestr(name, content)
    environment = json.dumps({
        'purpose': 'Local verification inventory; not a cross-platform dependency lock.',
        'packages': sorted([{'name': d.metadata['Name'], 'version': d.version}
                            for d in metadata.distributions()], key=lambda item: item['name'].lower())
    }, indent=2).encode()
    archive.writestr('environment-local.json', environment)
    manifest['environment-local.json'] = hashlib.sha256(environment).hexdigest()
    archive.writestr('SHA256SUMS.json', json.dumps(manifest, indent=2))

with zipfile.ZipFile(destination) as archive:
    assert archive.testzip() is None
    for name, digest in manifest.items():
        assert hashlib.sha256(archive.read(name)).hexdigest() == digest
digest = hashlib.sha256(destination.read_bytes()).hexdigest()
destination.with_suffix('.zip.sha256').write_text(digest + '  ' + destination.name + '\n', encoding='ascii')
print(json.dumps({'archive': str(destination), 'files': len(manifest), 'bytes': destination.stat().st_size,
                  'sha256': digest, 'integrity_verified': True}, ensure_ascii=False, indent=2))
