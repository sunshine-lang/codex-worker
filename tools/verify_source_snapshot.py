"""Verify this checkout against its deployed-source manifest. Does not execute a Worker."""
from pathlib import Path
import hashlib
import json


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / 'deployment/current-runtime/source-manifest.json').read_text(encoding='utf-8'))
    entries = manifest['modules'] + manifest['deployed_scripts'] + [manifest['worker_entry']]
    for entry in entries:
        path = (root / entry['path']).resolve(strict=True)
        assert path.is_relative_to(root), 'Manifest path outside checkout'
        with path.open('rb') as stream:
            assert hashlib.file_digest(stream, 'sha256').hexdigest() == entry['sha256'], entry['path']
    print(json.dumps({'passed': True, 'service_version': manifest['service_version'],
        'source_files_verified': len(entries), 'deployed_executable_sha256': manifest['running_executable_sha256']}))


if __name__ == '__main__':
    main()
