"""Build the alpha ZIP from an explicit allowlist; never include local secrets."""
import ast
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
VERSION = '0.2.9-alpha2'
FILES = [
    'app.py', 'management.py', 'runtime.py', 'wsgi.py', 'requirements.txt',
    'Dockerfile', '.dockerignore', '.gitignore', '.env.example', '.env.external-db.example',
    'compose.yaml', 'compose.external-db.yaml', 'portainer-stack.yaml', 'portainer-external-db.yaml',
    'templates/login.html', 'templates/page.html', 'templates/music.html', 'templates/monitor.html', 'protocol/ips_remote.py', 'protocol/player_v6.py',
    'tests/test_app.py', 'tests/test_runtime.py', 'README.md', 'VALIDATIE.md', 'NOTICE.md', 'build_release.py',
]

if __name__ == '__main__':
    output = ROOT.parent / ('IPS-multisite-'+VERSION+'.zip')
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            path = ROOT / name
            if path.suffix == '.py':
                ast.parse(path.read_text(encoding='utf-8'), feature_version=(3, 12))
            archive.write(path, name)
    print(output)
