"""Create a deterministic submission archive from an explicit file allowlist."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP = ['README.md', 'AI_WORKLOG.md', 'pyproject.toml', 'requirements.lock.txt', '.env.example', '.gitignore']
FOLDERS = {'agent': {'.py', '.html', '.css', '.js', '.md'}, 'tests': {'.py'}, 'docs': {'.md'}, 'scripts': {'.py'}}

def selected_files():
    files = [ROOT / p for p in TOP]
    for directory, suffixes in FOLDERS.items():
        files += [p for p in (ROOT / directory).rglob('*') if p.is_file() and p.suffix in suffixes and '__pycache__' not in p.parts]
    files += [ROOT / 'outputs' / n for n in ('test-results.xml', 'live-test-results.xml', 'live-terra-test-results.xml')]
    return sorted(files)

def build():
    out = ROOT / 'dist'
    out.mkdir(exist_ok=True)
    archive = out / 'small-harness-submission.zip'
    manifest = []
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in selected_files():
            relative = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            # Defense against accidentally packaging secret values from local configs.
            for config in (ROOT / '.env', ROOT / '.env.responses'):
                if config.exists():
                    for line in config.read_text().splitlines():
                        if line.startswith('LLM_API_KEY='):
                            secret = line.split('=', 1)[1].strip()
                            if secret and secret.encode() in data:
                                raise RuntimeError(f'Credential detected in {relative}')
            info = zipfile.ZipInfo('small-harness/' + relative, date_time=(2026, 9, 16, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
            manifest.append({'path': relative, 'sha256': hashlib.sha256(data).hexdigest()})
        z.writestr('small-harness/MANIFEST.json', json.dumps(manifest, indent=2, ensure_ascii=False))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / 'SHA256SUMS').write_text(f'{checksum}  {archive.name}\n')
    print(f'{archive}: {len(manifest)} files; sha256 {checksum}')

if __name__ == '__main__':
    build()
