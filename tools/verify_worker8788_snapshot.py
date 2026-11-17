"""Verify the published 8788 source fingerprints without starting a service."""
from pathlib import Path
import hashlib
import json


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root/'deployments/worker8788/release-manifest.json').read_text(encoding='utf-8'))
    for entry in manifest['files']:
        path = (root/entry['path']).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError('Manifest path outside checkout')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != entry['sha256']:
            raise ValueError('Source fingerprint mismatch: '+entry['path'])
    print(json.dumps({'passed':True,'controller_version':manifest['controller_version'],
        'files_verified':len(manifest['files']),
        'deployed_source_modules':len(manifest['source_modules'])}))


if __name__ == '__main__':
    main()
