"""Canonical, device-independent OS4 application compatibility bundles."""
import hashlib
import json
from pathlib import Path
import stat
import zipfile

from common import REPO_ROOT, sha256
from app_bridge_module import EMPTY, _hex, _path, _word

MODULE_ID = 'hyperos_avd_app_compat'
REVISION = 2
TEMPLATE = REPO_ROOT / 'modules/app-bridge'
SCRIPTS = ('customize.sh', 'runtime.sh', 'catalog.sh', 'post-fs-data.sh', 'service.sh', 'uninstall.sh')
# Reviewed immutable snapshots, keyed by their canonical JSON SHA-256. Add a
# snapshot here when changing a released universal catalog or runtime. Guest
# manifests can never extend this authorization list.
PREVIOUS_MANIFESTS = {}
HISTORY = REPO_ROOT / 'scripts/app_compat_history.json'
HISTORY_SHA256 = '0ec0eafeca23bb6db757966dc4015a470a0fd3ce3b6823c59bb8f69fde1819e0'


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def validate(recipes, payloads=None):
    if not isinstance(recipes, list) or not recipes:
        raise RuntimeError('Empty universal application compatibility catalog.')
    ids, features, outputs, selections, packages = set(), {}, set(), set(), {}
    for recipe in recipes:
        if not isinstance(recipe, dict):
            raise RuntimeError('Invalid universal application recipe.')
        for field in ('id', 'feature', 'package'):
            if not _word(recipe.get(field)):
                raise RuntimeError('Invalid universal application recipe identity.')
        if recipe['id'] in ids or not _hex(recipe.get('apk_sha256')):
            raise RuntimeError('Duplicate or invalid application compatibility recipe.')
        ids.add(recipe['id'])
        if packages.setdefault(recipe['feature'], recipe['package']) != recipe['package']:
            raise RuntimeError('An application feature must identify one package.')
        if set(recipe) - {'id', 'feature', 'package', 'default_enabled', 'apk_sha256',
                          'factory_apk', 'native', 'factory_overlay', 'factory_passthrough', 'apk_libraries',
                          'libraries', 'system_targets', 'system_libraries', 'native_libraries', 'policy'}:
            raise RuntimeError('Unknown application recipe field.')
        selection = recipe['feature'], recipe['package'], recipe['apk_sha256']
        if selection in selections:
            raise RuntimeError('Ambiguous application compatibility workload.')
        selections.add(selection)
        default = recipe.get('default_enabled')
        if type(default) is not bool:
            raise RuntimeError('Invalid application feature default.')
        if recipe['feature'] not in features:
            features[recipe['feature']] = default
        elif features[recipe['feature']] != default:
            features[recipe['feature']] = 'recipe'
        factory, native = recipe.get('factory_apk', '-'), recipe.get('native', '-')
        if (factory == '-') != (native == '-') or factory != '-' and not (_path(factory) and _path(native)):
            raise RuntimeError('Invalid factory application layout.')
        mirror = recipe.get('factory_overlay', False)
        if type(mirror) is not bool or mirror and factory == '-':
            raise RuntimeError('Invalid factory application overlay policy.')
        passthrough = recipe.get('factory_passthrough', {})
        if (not isinstance(passthrough, dict) or passthrough and not mirror or any(
                not isinstance(path, str) or len(path.split('/')) != 3
                or path.split('/')[:2] != ['oat', 'arm64']
                or not _word(path.rsplit('/', 1)[1])
                or not path.endswith(('.odex', '.vdex')) or not _hex(checksum)
                for path, checksum in passthrough.items())):
            raise RuntimeError('Invalid pinned factory passthrough assets.')
        apk_libraries = recipe.get('apk_libraries', {})
        if not isinstance(apk_libraries, dict) or any(
                not isinstance(entry, str) or not entry.startswith('lib/arm64-v8a/')
                or not _word(entry.rsplit('/', 1)[1]) or not entry.endswith('.so')
                or len(entry.split('/')) != 3 or not _hex(checksum)
                for entry, checksum in apk_libraries.items()):
            raise RuntimeError('Invalid embedded application ABI anchors.')
        names = set()
        if not isinstance(recipe.get('libraries', []), list):
            raise RuntimeError('Invalid private library list.')
        for library in recipe.get('libraries', []):
            if not isinstance(library, dict):
                raise RuntimeError('Invalid private application library recipe.')
            name = library.get('name')
            if set(library) - {'name', 'before', 'after', 'placeholder', 'legacy_direct'}:
                raise RuntimeError('Unknown private library recipe field.')
            if (not _word(name) or not name.endswith('.so') or name in names
                    or not _hex(library.get('before')) or not _hex(library.get('after'))
                    or type(library.get('placeholder', False)) is not bool):
                raise RuntimeError('Invalid private application library recipe.')
            names.add(name)
            if library.get('placeholder') and library['before'] != EMPTY and not (
                    mirror and apk_libraries.get('lib/arm64-v8a/' + name) == library['before']):
                raise RuntimeError('Missing original library is allowed only for an audited factory extraction.')
            legacy = library.get('legacy_direct', [])
            if not isinstance(legacy, list) or any(not _hex(value) for value in legacy) or legacy and not library.get('placeholder'):
                raise RuntimeError('Invalid obsolete private-helper migration.')
            outputs.add(library['after'])
        targets = set()
        if not isinstance(recipe.get('system_targets', []), list):
            raise RuntimeError('Invalid early overlay list.')
        for target in recipe.get('system_targets', []):
            if (not isinstance(target, dict) or not _path(target.get('target'))
                    or target['target'] in targets or not _hex(target.get('before'))
                    or not _hex(target.get('after'))):
                raise RuntimeError('Invalid early system application overlay.')
            targets.add(target['target'])
            if set(target) - {'target', 'before', 'after', 'mount_flags'}:
                raise RuntimeError('Unknown early overlay recipe field.')
            flags = target.get('mount_flags', 'ro,nosuid,nodev')
            if flags not in ('ro,nosuid,nodev', 'ro,suid,exec') or (
                    flags == 'ro,suid,exec' and not target['target'].startswith('/vendor/bin/hw/')):
                raise RuntimeError('Invalid early provider mount policy.')
            outputs.add(target['after'])
        if not isinstance(recipe.get('system_libraries', {}), dict) or not isinstance(recipe.get('native_libraries', {}), dict):
            raise RuntimeError('Invalid application ABI dependency map.')
        for path, checksum in recipe.get('system_libraries', {}).items():
            if not _path(path) or not _hex(checksum):
                raise RuntimeError('Invalid application system ABI anchor.')
        for name, checksum in recipe.get('native_libraries', {}).items():
            if not _word(name) or not name.endswith('.so') or not _hex(checksum):
                raise RuntimeError('Invalid application caller ABI anchor.')
        policy = recipe.get('policy', {})
        if not isinstance(policy, dict) or set(policy) - {'angle'}:
            raise RuntimeError('Unknown application compatibility policy.')
        if 'angle' in policy:
            angle = policy['angle']
            if (not isinstance(angle, dict) or set(angle) != {'driver', 'features'}
                    or angle['driver'] != 'angle' or not isinstance(angle['features'], list)
                    or any(not _word(item) for item in angle['features'])):
                raise RuntimeError('Invalid private application ANGLE policy.')
        if not recipe.get('libraries') and not recipe.get('system_targets') and not policy:
            raise RuntimeError('Empty application compatibility recipe.')
    if payloads is not None:
        if not isinstance(payloads, dict) or set(payloads) != outputs:
            raise RuntimeError('Universal payload set differs from the complete recipe union.')
        for checksum, source in payloads.items():
            source = Path(source)
            if not source.is_file() or source.is_symlink() or source.stat().st_nlink != 1 or sha256(source) != checksum:
                raise RuntimeError('Universal application payload changed: ' + checksum)
    return features, outputs


def catalog_files(recipes, payloads=None):
    features, outputs = validate(recipes, payloads)
    recipes = sorted(recipes, key=lambda value: value['id'])
    files = {}
    for name in SCRIPTS:
        path = TEMPLATE / name
        if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
            raise RuntimeError('Universal application runtime source is unavailable: ' + name)
        files[name] = path.read_bytes()
    from module_webui import files as webui_files
    files.update(webui_files('apps'))
    files['module.prop'] = (f'id={MODULE_ID}\nname=HyperOS AVD application compatibility\n'
                            f'version={REVISION}\nversionCode={REVISION}\nauthor=HyperOS-AVD\n'
                            'description=Adaptive verified application, private rendering and camera compatibility\n').encode()
    files['skip_mount'] = b''
    files['catalog.tsv'] = ''.join('|'.join((recipe['id'], recipe['feature'], recipe['package'],
                                           '1' if recipe['default_enabled'] else '0')) + '\n'
                                   for recipe in recipes).encode()
    for recipe in recipes:
        identity, prefix = recipe['id'], 'contexts/' + recipe['id'] + '/'
        files[prefix + 'package.txt'] = (recipe['package'] + '\n').encode()
        files[prefix + 'profiles.tsv'] = ('|'.join((identity, recipe['apk_sha256'], recipe.get('factory_apk', '-'),
                                                 recipe.get('native', '-'))) + '\n').encode()
        files[prefix + 'libraries.tsv'] = ''.join('|'.join((identity, library['name'], library['before'], library['after'],
                '1' if library.get('placeholder') else '0', ','.join(library.get('legacy_direct', [])))) + '\n'
                for library in recipe.get('libraries', [])).encode()
        files[prefix + 'systems.tsv'] = ''.join('|'.join((identity, path, checksum)) + '\n'
                for path, checksum in sorted(recipe.get('system_libraries', {}).items())).encode()
        files[prefix + 'callers.tsv'] = ''.join('|'.join((identity, name, checksum)) + '\n'
                for name, checksum in sorted(recipe.get('native_libraries', {}).items())).encode()
        files[prefix + 'embedded.tsv'] = ''.join('|'.join((entry, checksum)) + '\n'
                for entry, checksum in sorted(recipe.get('apk_libraries', {}).items())).encode()
        files[prefix + 'passthrough.tsv'] = ''.join('|'.join((path, checksum)) + '\n'
                for path, checksum in sorted(recipe.get('factory_passthrough', {}).items())).encode()
        files[prefix + 'targets.tsv'] = ''.join('|'.join((target['target'], target['before'], target['after'],
                target.get('mount_flags', 'ro,nosuid,nodev'))) + '\n'
                for target in recipe.get('system_targets', [])).encode()
        files[prefix + 'mirror.txt'] = ('1\n' if recipe.get('factory_overlay') else '0\n').encode()
        angle = recipe.get('policy', {}).get('angle')
        files[prefix + 'angle.tsv'] = (('angle|' + ','.join(angle['features']) + '\n').encode() if angle else b'')
    for checksum, source in sorted((payloads or {}).items()):
        files['objects/' + checksum + '.so'] = Path(source).read_bytes()
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}
    hashes.update({'objects/' + checksum + '.so': checksum for checksum in outputs})
    manifest = {'schema': 2, 'revision': REVISION, 'id': MODULE_ID,
                'delivery': 'guest-app-universal-catalog', 'recipes': recipes,
                'features': dict(sorted(features.items())), 'files_sha256': dict(sorted(hashes.items())),
                'automatic_reboot': False, 'install_only_files': ['customize.sh']}
    files['manifest.json'] = canonical(manifest)
    hashes['manifest.json'] = hashlib.sha256(files['manifest.json']).hexdigest()
    files['SHA256SUMS'] = ''.join(checksum + '  ' + name + '\n' for name, checksum in sorted(hashes.items())
                                 if name != 'customize.sh').encode()
    return files


def package_catalog(destination, recipes, payloads):
    validate(recipes, payloads)
    files = catalog_files(recipes, payloads)
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, body in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if name.endswith('.sh') else 0o644)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)
    return receipt(destination, recipes)


def receipt(archive, recipes):
    files = catalog_files(recipes)
    return {'schema': 1, 'module': MODULE_ID, 'archive_sha256': sha256(archive),
            'manifest_sha256': hashlib.sha256(files['manifest.json']).hexdigest(),
            'templates': {name: hashlib.sha256(files[name]).hexdigest() for name in SCRIPTS}}


def verify_prebuilt(folder, recipes):
    folder = Path(folder)
    archive, metadata = folder / 'app-compat.zip', folder / 'receipt.json'
    for path in (archive, metadata):
        if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
            raise RuntimeError('Universal application archive or receipt is not a regular owned asset.')
    if not folder.is_dir() or folder.is_symlink():
        raise RuntimeError('Universal application prebuilt directory is aliased.')
    try:
        saved = json.loads(metadata.read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError('Invalid universal application archive receipt.') from error
    if saved != receipt(archive, recipes):
        raise RuntimeError('Universal application archive differs from the canonical catalog/runtime.')
    expected = catalog_files(recipes)
    manifest = json.loads(expected['manifest.json'])
    names = set(expected) | {name for name in manifest['files_sha256'] if name.startswith('objects/')}
    with zipfile.ZipFile(archive) as packed:
        entries = packed.infolist()
        if len(entries) != len(names) or {entry.filename for entry in entries} != names:
            raise RuntimeError('Universal application archive has unknown or duplicate entries.')
        for entry in entries:
            if stat.S_IFMT(entry.external_attr >> 16) != stat.S_IFREG:
                raise RuntimeError('Universal application archive contains nonregular assets.')
            if entry.filename in expected:
                if packed.read(entry) != expected[entry.filename]:
                    raise RuntimeError('Universal application archive control bytes changed.')
            else:
                checksum = hashlib.sha256()
                with packed.open(entry) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        checksum.update(block)
                if checksum.hexdigest() != manifest['files_sha256'][entry.filename]:
                    raise RuntimeError('Universal application archive payload changed.')
    return archive, manifest


def previous_manifests():
    values = []
    if not HISTORY.is_file() or HISTORY.is_symlink() or HISTORY.stat().st_nlink != 1 or sha256(HISTORY) != HISTORY_SHA256:
        raise RuntimeError('Reviewed universal application history changed.')
    history = json.loads(HISTORY.read_text())
    if (not isinstance(history, dict) or set(history) != {'schema', 'module', 'manifests'}
            or history['schema'] != 1 or history['module'] != MODULE_ID
            or not isinstance(history['manifests'], dict)):
        raise RuntimeError('Invalid reviewed universal application history schema.')
    reviewed = {**history['manifests'], **PREVIOUS_MANIFESTS}
    for checksum, manifest in reviewed.items():
        if (hashlib.sha256(canonical(manifest)).hexdigest() != checksum
                or manifest.get('id') != MODULE_ID or manifest.get('schema') != 2
                or manifest.get('delivery') != 'guest-app-universal-catalog'):
            raise RuntimeError('Invalid reviewed universal application history.')
        validate(manifest.get('recipes'))
        if not isinstance(manifest.get('files_sha256'), dict) or any(
                not isinstance(name, str) or name.startswith('/') or '..' in name.split('/')
                or not _hex(value) for name, value in manifest['files_sha256'].items()):
            raise RuntimeError('Invalid reviewed universal asset history.')
        values.append(manifest)
    return values


def verify_previous_prebuilt(folder):
    """Authorize replacement only against reviewed immutable history."""
    folder = Path(folder)
    if not folder.is_dir() or folder.is_symlink():
        raise RuntimeError('Aliased application prebuilt history.')
    archive, metadata = folder / 'app-compat.zip', folder / 'receipt.json'
    for path in (archive, metadata):
        if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
            raise RuntimeError('Invalid previous application archive asset.')
    saved = json.loads(metadata.read_text())
    for manifest in previous_manifests():
        controls = {**manifest['files_sha256'], 'manifest.json': hashlib.sha256(canonical(manifest)).hexdigest()}
        checksum_list = ''.join(value + '  ' + name + '\n' for name, value in sorted(controls.items())
                                if name != 'customize.sh').encode()
        controls['SHA256SUMS'] = hashlib.sha256(checksum_list).hexdigest()
        expected = {'schema': 1, 'module': MODULE_ID, 'archive_sha256': sha256(archive),
                    'manifest_sha256': controls['manifest.json'],
                    'templates': {name: value for name, value in controls.items()
                                  if '/' not in name and name.endswith('.sh') and name != 'webui-status.sh'}}
        if saved != expected:
            continue
        with zipfile.ZipFile(archive) as packed:
            entries = packed.infolist()
            if len(entries) != len(controls) or {entry.filename for entry in entries} != set(controls):
                continue
            if any(stat.S_IFMT(entry.external_attr >> 16) != stat.S_IFREG or
                   hashlib.sha256(packed.read(entry)).hexdigest() != controls[entry.filename] for entry in entries):
                continue
        return manifest
    raise RuntimeError('Unknown previous application prebuilt is preserved.')


def freeze_previous_prebuilt(folder, recipes, destination):
    """Emit a reviewed-history candidate only from a fully verified archive.

    Register the returned canonical manifest and digest in PREVIOUS_MANIFESTS
    before replacing released templates; never copy a guest saved manifest.
    """
    _, manifest = verify_prebuilt(folder, recipes)
    destination = Path(destination)
    with destination.open('xb') as output:
        output.write(canonical(manifest))
    return hashlib.sha256(canonical(manifest)).hexdigest()
