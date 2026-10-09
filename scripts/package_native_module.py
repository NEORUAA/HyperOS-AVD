#!/usr/bin/env python3
"""Build a self-contained KernelSU module from the audited native patch catalog."""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import zipfile

REPO = Path(__file__).resolve().parent.parent
MODULE_ID = 'hyperos_avd_native_compat'
REVISION = 4


def module_files(value=None, template=None):
    """Serialize data, never shell code, from the same profiles as image patchers."""
    from native_patch_catalog import catalog, validate_catalog
    value = catalog() if value is None else value
    validate_catalog(value)
    template = REPO / 'modules/native-compat' if template is None else Path(template)
    files = {}
    for name in ('module.prop', 'customize.sh', 'runtime.sh', 'platform.sh', 'post-fs-data.sh', 'service.sh'):
        files[name] = (template / name).read_bytes()
    from dex2oat_cpu_policy import policy_script
    files['dex2oat-cpu-policy.sh'] = policy_script()
    from module_webui import files as webui_files
    files.update(webui_files('core'))
    properties = dict(line.split('=', 1) for line in files['module.prop'].decode().splitlines() if '=' in line)
    if properties.get('id') != MODULE_ID or properties.get('author') != 'HyperOS-AVD':
        raise RuntimeError('Unexpected native module template ownership.')
    if properties.get('versionCode') != str(REVISION):
        raise RuntimeError('Native module revision and template disagree.')
    files['skip_mount'] = b''
    files['catalog.json'] = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    profiles = []
    for profile in sorted(value['profiles'], key=lambda item: item['id']):
        profiles.append('|'.join((profile['before'], profile['after'],
                                  ','.join(profile.get('legacy', ())), profile['feature'], profile['id'])))
        sites = []
        for index, (offset, before, after) in enumerate(profile['sites']):
            original, replacement = bytes.fromhex(before), bytes.fromhex(after)
            if not original or len(original) != len(replacement):
                raise RuntimeError('Native runtime supports equal-length sites only: ' + profile['id'])
            prefix = f"sites/{profile['id']}/{index:03d}"
            files[prefix + '.before'] = original
            files[prefix + '.after'] = replacement
            sites.append('|'.join((str(offset), str(len(original)), prefix + '.before', prefix + '.after')))
        files['sites/' + profile['id'] + '.tsv'] = ('\n'.join(sites) + '\n').encode()
    files['profiles.tsv'] = ('\n'.join(profiles) + '\n').encode()
    rows = ['|'.join((item['phase'], item['kind'], item['feature'], item['path'],
                      item['package'], item['library'])) for item in value['targets']]
    files['targets.tsv'] = ('\n'.join(rows) + '\n').encode()
    features = sorted({profile['feature'] for profile in value['profiles']} |
                      {'identity', 'serial', 'thermal', 'refresh', 'boot-services', 'rear-wake'})
    files['features.tsv'] = ('\n'.join(features) + '\n').encode()
    manifest = {'schema': 1, 'id': MODULE_ID, 'revision': REVISION,
                'catalog_sha256': hashlib.sha256(files['catalog.json']).hexdigest(),
                'files_sha256': {name: hashlib.sha256(body).hexdigest()
                                 for name, body in sorted(files.items())},
                'profile_count': len(value['profiles']),
                'features': features,
                'delivery': 'guest-core-exact-content-and-capabilities', 'automatic_reboot': False,
                'install_only_files': ['customize.sh']}
    files['manifest.json'] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    # KernelSU removes the install-time customizer after a successful install.
    # Its source hash remains in the manifest, but runtime integrity checks
    # cover only the assets retained in the installed module directory.
    files['SHA256SUMS'] = ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n'
                                 for name, data in sorted(files.items())
                                 if name != 'customize.sh').encode()
    return files


def package(output, value=None, template=None):
    """Write deterministically and activate only a complete verified archive."""
    output = Path(output)
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise RuntimeError('Refused non-regular module output.')
    files = module_files(value, template)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.next')
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError('A native module packaging operation is already staged.')
    created = False
    try:
        stream = temporary.open('xb')
        created = True
        with stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED,
                                                           compresslevel=9) as archive:
            for name, body in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                mode = 0o755 if name.endswith('.sh') else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, body)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None or set(archive.namelist()) != set(files):
                raise RuntimeError('Native module archive verification failed.')
            for name, body in files.items():
                if archive.read(name) != body:
                    raise RuntimeError('Native module content verification failed: ' + name)
        temporary.replace(output)
    finally:
        if created:
            temporary.unlink(missing_ok=True)
    return {'path': str(output.resolve()), 'bytes': output.stat().st_size,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'profiles': len(json.loads(files['catalog.json'])['profiles'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.output), indent=2))


if __name__ == '__main__':
    main()
