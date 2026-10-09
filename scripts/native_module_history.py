"""Reviewed immutable Core releases eligible for unattended control migration.

Add the full manifest/checksum identities of each shipped module here before
changing its controls. Guest-provided receipts never authorize unknown code.
"""
import hashlib
import json

REVIEWED = (
    {'revision': 2, 'source_commit': '0ec75d3',
     'manifest_sha256': '88fd81c92905b0f75ff733aa4fe10994c8c6f4a779f995c26d294951fe4e98ca',
     'checksums_sha256': '41129ea926983baa8972f1a3579512c9a0f489701e67318355bb84ed3959cfd9'},
    {'revision': 3,
     'package_sha256': '311608ab7ecfcb490337e102a637c72e9f492e892fcaaa17d09e789a77066d61',
     'manifest_sha256': 'dfb9e53dcf9b3ce065f48c6dcfe6c191666fff253d274e9e0624e33c1c19382a',
     'checksums_sha256': 'fa204e270753618e9ff114c1131d118265eb359da682801679c546105833f355'},
)


def checksum_for(manifest):
    if not isinstance(manifest, dict):
        return None
    body = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    expected = hashlib.sha256(body).hexdigest()
    return next((record['checksums_sha256'] for record in REVIEWED
                 if record['revision'] == manifest.get('revision')
                 and record['manifest_sha256'] == expected), None)
