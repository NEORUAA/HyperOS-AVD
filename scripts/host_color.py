"""Build and load the owned OS4 emulator's macOS sRGB window tag."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from common import OS4_NAME, REPO_ROOT, sha256

LIBRARY = 'emulator_srgb.dylib'
RECEIPT = 'emulator_srgb.build.json'


def build(root):
    """Reuse verified release binaries; only source builds need Command Line Tools."""
    source = REPO_ROOT / 'native/emulator_srgb.m'
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    directory = Path(root) / 'tools'
    library, receipt = directory / LIBRARY, directory / RECEIPT
    if library.is_file() and receipt.is_file():
        metadata = json.loads(receipt.read_text())
        if (metadata.get('source_sha256') == source_digest
                and metadata.get('sha256') == sha256(library)):
            return library, receipt
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / ('emulator_srgb-' + str(os.getpid()) + '.dylib')
    try:
        subprocess.run(['xcrun', 'clang', '-dynamiclib', '-fobjc-arc', '-Wall', '-Wextra',
                        '-Werror', '-arch', 'arm64', '-mmacosx-version-min=12.0',
                        '-framework', 'AppKit', '-framework', 'QuartzCore', str(source),
                        '-o', str(temporary)], check=True)
        temporary.replace(library)
    finally:
        temporary.unlink(missing_ok=True)
    receipt.write_text(json.dumps({'source_sha256': source_digest,
                                   'sha256': sha256(library)}, indent=2) + '\n')
    return library, receipt


def environment(root, name, enabled=True):
    """Keep the SDK and other emulator processes unchanged."""
    result = os.environ.copy()
    if not enabled or not (Path(root) / 'local/build.json').is_file() or json.loads(
            (Path(root) / 'local/build.json').read_text()).get('source') != 'official-hongkong-ota':
        return result
    library, _ = build(root)
    injected = result.get('DYLD_INSERT_LIBRARIES', '')
    result['DYLD_INSERT_LIBRARIES'] = str(library.resolve()) + (':' + injected if injected else '')
    result['HYPEROS_AVD_SRGB'] = '1'
    return result
