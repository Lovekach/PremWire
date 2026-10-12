"""No binary assets, real env, runtime or private configuration in the Git index."""
import json
from pathlib import Path
import subprocess

allowed_roots = {'src', 'tests', 'scripts', 'docs', 'config', '.github'}
allowed_root_files = {'.env.example', '.gitignore', '.python-version', 'README.md',
                      'LICENSE', 'SECURITY.md', 'CONTRIBUTING.md', 'THIRD_PARTY_NOTICES.md',
                      'requirements.lock', 'pyproject.toml'}
allowed_suffixes = {'.py', '.json', '.md', '.toml', '.txt', '.yml', '.yaml'}
paths = subprocess.run(['git', 'ls-files', '-z'], capture_output=True, check=True).stdout.decode().split('\0')
for rel in filter(None, paths):
    path = Path(rel)
    data = path.read_bytes()
    if path.is_symlink() or len(data) > 2_000_000 or b'\0' in data:
        raise SystemExit('PUBLIC_TREE_BLOCKED')
    if rel not in allowed_root_files:
        if path.parts[0] not in allowed_roots or path.suffix not in allowed_suffixes:
            raise SystemExit('PUBLIC_TREE_BLOCKED')
    if any(p in {'runtime', '.runtime', 'data', 'reports', 'secrets', 'credentials',
                 'assets', 'browser-profile', 'models', 'weights'} for p in path.parts):
        raise SystemExit('PUBLIC_TREE_BLOCKED')
    if path.name.startswith('.env') and rel != '.env.example':
        raise SystemExit('PUBLIC_ENV_BLOCKED')
    data.decode('utf-8-sig')
print(json.dumps({'status': 'PASS', 'tracked_files': len(list(filter(None, paths)))}))
