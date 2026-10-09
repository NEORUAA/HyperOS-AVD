#!/usr/bin/env python3
"""Stage the complete audited application catalog, independent of device names."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import tempfile

from common import ROOT, adb, runtime, sha256
from apply_flutter_fix import root
from app_bridge_module import _hex, _inspect, _word
from app_compat_catalog import MODULE_ID, REVISION, canonical, catalog_files, package_catalog, previous_manifests, verify_prebuilt, verify_previous_prebuilt

PREFERENCES = '/data/adb/hyperos_app_compat/features'
MISMATCH_MARKER = '__HYPEROS_APP_CATALOG_MISMATCH__'


class CatalogMismatch(RuntimeError):
    """A completed guest check rejected catalog bytes or path ownership."""


def recipes():
    from app_compat_producers import recipes as producer_recipes
    return producer_recipes()


def prepare_prebuilt(workspace, sdk=None, cache_roots=None):
    """Produce one universal archive; normal consumers never compile helpers."""
    from app_compat_producers import collect_payloads
    workspace = Path(workspace)
    destination = workspace / 'tools/os4-app-compat'
    catalog = recipes()
    replacing = destination.exists() or destination.is_symlink()
    if replacing:
        try:
            verify_prebuilt(destination, catalog)
        except RuntimeError:
            verify_previous_prebuilt(destination)
        else:
            return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    payloads = collect_payloads(cache_roots or [workspace], sdk=sdk)
    with tempfile.TemporaryDirectory(prefix='.app-compat-', dir=destination.parent) as temporary:
        stage = Path(temporary)
        metadata = package_catalog(stage / 'app-compat.zip', catalog, payloads)
        (stage / 'receipt.json').write_bytes(canonical(metadata))
        verify_prebuilt(stage, catalog)
        if replacing:
            verify_previous_prebuilt(destination)
            backup = stage.parent / (stage.name + '.previous')
            destination.rename(backup)
            try:
                stage.rename(destination)
            except BaseException:
                backup.rename(destination)
                raise
            shutil.rmtree(backup)
        else:
            if destination.exists() or destination.is_symlink():
                raise RuntimeError('Application prebuilt appeared during publication.')
            stage.rename(destination)
    return destination


def _authentication_command(directory, expected):
    """Generate one bounded check without repeating directory ancestors.

    ADB shell service arguments have a finite size. Keep per-file checks in a
    fixed loop and send only the canonical hash/name table as input data.
    """
    manifest_hash = hashlib.sha256(canonical(expected)).hexdigest()
    hashes = {**expected['files_sha256'], 'manifest.json': manifest_hash}
    checksum_list = ''.join(checksum + '  ' + name + '\n' for name, checksum in sorted(hashes.items())
                            if name != 'customize.sh')
    hashes['SHA256SUMS'] = hashlib.sha256(checksum_list.encode()).hexdigest()
    ancestors, files = set(), []
    for name, checksum in hashes.items():
        if name == 'customize.sh':
            continue
        if name.startswith('/') or '..' in name.split('/') or any(not _word(part) for part in name.split('/')):
            raise RuntimeError('Invalid immutable application catalog asset.')
        if not _hex(checksum):
            raise RuntimeError('Invalid immutable application catalog checksum.')
        path = directory + '/' + name
        parent = Path(path).parent
        for ancestor in (parent, *parent.parents):
            if str(ancestor) == '/':
                continue
            ancestors.add(str(ancestor))
        files.append(checksum + ' ' + name)
    parents = ' '.join(shlex.quote(parent) for parent in sorted(ancestors, key=lambda path:(path.count('/'), path)))
    table = '\n'.join(files)
    return f'''catalog_mismatch() {{ printf '%s:%s\\n' {MISMATCH_MARKER} "$1"; exit 71; }}
for parent in {parents}; do
    [ -d "$parent" ] && [ ! -L "$parent" ] || catalog_mismatch "$parent"
    case "$parent" in /data) ;; *) [ "$(stat -c %u "$parent")" = 0 ] || catalog_mismatch "$parent" ;; esac
done
while read -r checksum name; do
    path={shlex.quote(directory)}/"$name"
    [ -f "$path" ] && [ ! -L "$path" ] || catalog_mismatch "$path"
    [ "$(stat -c %h:%u "$path")" = 1:0 ] || catalog_mismatch "$path"
    [ "$(sha256sum "$path" | cut -d ' ' -f 1)" = "$checksum" ] || catalog_mismatch "$path"
done <<'HYPEROS_CANONICAL_APP_ASSETS'
{table}
HYPEROS_CANONICAL_APP_ASSETS'''


def _saved(config, module, expected):
    """Authenticate every immutable asset; transport failure is not mismatch."""
    try:
        root(config, _authentication_command(module['directory'], expected))
    except RuntimeError as error:
        if MISMATCH_MARKER + ':' in str(error):
            raise CatalogMismatch('Application catalog content or ownership differs: ' + str(error)) from error
        raise


def set_features(config, choices, catalog=None):
    """Persist explicit feature choices without rewriting immutable catalogs."""
    catalog = recipes() if catalog is None else catalog
    known = {recipe['feature'] for recipe in catalog}
    if not isinstance(choices, dict) or any(feature not in known or not (type(value) is bool or value == 'auto')
                                           for feature, value in choices.items()):
        raise RuntimeError('Unknown application feature choice.')
    if not choices:
        return
    active_path, pending_path = '/data/adb/modules/' + MODULE_ID, '/data/adb/modules_update/' + MODULE_ID
    commands = ['set -e', f'for owner in {active_path} {pending_path}; do\n'
                ' [ ! -e "$owner/disable" ] && [ ! -L "$owner/disable" ] && '
                '[ ! -e "$owner/remove" ] && [ ! -L "$owner/remove" ] || exit 1\ndone',
                'for path in /data /data/adb /data/adb/hyperos_app_compat ' + PREFERENCES + '; do\n'
                ' [ ! -L "$path" ] || exit 1\ndone', f'mkdir -p {PREFERENCES}',
                f'chmod 700 /data/adb/hyperos_app_compat {PREFERENCES}']
    for feature, value in sorted(choices.items()):
        path = PREFERENCES + '/' + feature
        desired = 'auto' if value == 'auto' else 'on' if value is True else 'off'
        commands += [f'if [ -e {path} ]; then [ -f {path} ] && [ ! -L {path} ] && '
                     f'[ "$(stat -c %h:%u {path})" = 1:0 ] || exit 1; fi',
                     f'temporary=$(mktemp {PREFERENCES}/.choice.XXXXXX)',
                     f'printf \'%s\\n\' {desired} > "$temporary"', f'mv "$temporary" {path}']
    root(config, '\n'.join(commands))


def install_prebuilt(config, workspace=None, enabled_features=None, legacy=None, catalog=None, migration=None):
    catalog = recipes() if catalog is None else catalog
    values = root(config, 'getprop ro.boot.hardware; getprop ro.mi.os.version.incremental').splitlines()
    if len(values) != 2 or values[0] != 'ranchu' or not values[1].startswith(('OS4.', '4.')):
        raise RuntimeError('Universal application compatibility requires an ARM64 ranchu OS4 guest.')
    active_path, pending_path = '/data/adb/modules/' + MODULE_ID, '/data/adb/modules_update/' + MODULE_ID
    reviewed_previous = previous_manifests()
    revisions = {str(REVISION), *(str(item['revision']) for item in reviewed_previous)}
    active, pending = (_inspect(config, path, MODULE_ID, allowed_revisions=revisions)
                       for path in (active_path, pending_path))
    for module in (active, pending):
        if module and module['flags']:
            return {'installed': False, 'preserved': True, 'lifecycle': module['flags'], 'module': MODULE_ID}
    archive, expected = verify_prebuilt(Path(workspace or ROOT) / 'tools/os4-app-compat', catalog)
    migration_plan = None
    if migration is None:
        import app_compat_migration as migration
    if migration is not False:
        migration_plan = migration.plan(config, catalog, workspace=workspace or ROOT)
    for module in (pending, active):
        if module is None:
            continue
        try:
            _saved(config, module, expected)
        except CatalogMismatch:
            if module is pending:
                return {'installed': False, 'pending': True, 'deferred': True,
                        'reboot_required': True, 'module': MODULE_ID}
            known = False
            for previous in reviewed_previous:
                try:
                    _saved(config, module, previous)
                except CatalogMismatch:
                    continue
                known = True
                break
            if not known and (not legacy or not legacy(config, module, expected)):
                raise RuntimeError('Unknown application catalog revision or local changes are preserved.')
        else:
            if migration_plan:
                migration.import_choices(config, migration_plan)
            if enabled_features:
                set_features(config, enabled_features, catalog)
            retired = migration.retire(config, migration_plan, expected) if migration_plan and module is active else None
            return {'installed': True, 'reused': True, 'pending': module is pending,
                    'reboot_required': module is pending or bool(retired and retired.get('reboot_required')),
                    'migration': retired, 'module': MODULE_ID, 'features': expected['features']}
    guards = [f'[ ! -e {pending_path} ] && [ ! -L {pending_path} ] || exit 1']
    if active:
        guards += [f'test "$(cat {active_path}/module.prop)" = {shlex.quote(active["properties"])} || exit 1']
        guards += [f'[ ! -e {active_path}/{flag} ] && [ ! -L {active_path}/{flag} ] || exit 1'
                   for flag in ('disable', 'remove')]
    else:
        guards += [f'[ ! -e {active_path} ] && [ ! -L {active_path} ] || exit 1']
    checksum = sha256(archive)
    if migration_plan:
        migration.import_choices(config, migration_plan)
    remote = '/data/local/tmp/hyperos-app-catalog-' + checksum[:16] + '.zip'
    adb(config, 'push', str(archive), remote, check=True, capture_output=True, timeout=60)
    try:
        root(config, '\n'.join(guards) + f'\n[ "$(sha256sum {remote} | cut -d \' \' -f 1)" = {checksum} ] || exit 1\n'
             f'/data/adb/ksud module install {remote}')
    finally:
        root(config, 'rm -f ' + remote)
    if enabled_features:
        set_features(config, enabled_features, catalog)
    return {'installed': True, 'reused': False, 'reboot_required': True,
            'module': MODULE_ID, 'features': expected['features']}


def install_catalog(config, catalog, payloads, *, enabled_features=None, legacy=None):
    """Producer/test entry point; public consumers use the verified archive."""
    with tempfile.TemporaryDirectory(prefix='hyperos-app-catalog-') as temporary:
        workspace = Path(temporary)
        folder = workspace / 'tools/os4-app-compat'
        folder.mkdir(parents=True)
        metadata = package_catalog(folder / 'app-compat.zip', catalog, payloads)
        (folder / 'receipt.json').write_bytes(canonical(metadata))
        return install_prebuilt(config, workspace, enabled_features, legacy, catalog)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--feature')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--enable', action='store_true')
    group.add_argument('--disable', action='store_true')
    group.add_argument('--auto', action='store_true')
    args = parser.parse_args()
    choices = None
    if args.feature:
        if not (args.enable or args.disable or args.auto):
            parser.error('--feature requires --enable, --disable or --auto')
        choices = {args.feature: 'auto' if args.auto else args.enable}
    install_prebuilt(runtime(), enabled_features=choices)


if __name__ == '__main__':
    main()
