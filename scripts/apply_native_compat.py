#!/usr/bin/env python3
"""Install the portable native compatibility module through KernelSU itself."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import tempfile
import zipfile

from common import ROOT, adb, runtime
from apply_flutter_fix import root
from package_native_module import MODULE_ID, REVISION, package

MODULE = '/data/adb/modules/' + MODULE_ID
PENDING = '/data/adb/modules_update/' + MODULE_ID


def guest_supported(config):
    values = root(config, 'getprop ro.boot.hardware; getprop ro.mi.os.version.incremental').splitlines()
    if len(values) != 2 or values[0] != 'ranchu' or not values[1].startswith(('OS4.', '4.')):
        raise RuntimeError('Native compatibility module requires a ranchu OS4 guest.')


def _module(config, directory):
    """Inspect both active and staged ownership without following module links."""
    value = root(config, f'''if [ -e {directory} ] || [ -L {directory} ]; then
    [ -d {directory} ] && [ ! -L {directory} ] || exit 1
    [ -f {directory}/module.prop ] && [ ! -L {directory}/module.prop ] || exit 1
    printf 'present\\n'
    cat {directory}/module.prop
else
    printf 'absent\\n'
fi''')
    if value == 'absent':
        return None
    if not value.startswith('present\n'):
        raise RuntimeError('Refused an invalid native module ownership record.')
    properties = value.split('\n', 1)[1]
    values = {}
    for line in properties.splitlines():
        if '=' not in line:
            continue
        key, item = line.split('=', 1)
        if key in values:
            raise RuntimeError('Refused duplicate native module properties.')
        values[key] = item
    if values.get('id') != MODULE_ID or values.get('author') != 'HyperOS-AVD':
        raise RuntimeError('Refused unrelated native module ownership.')
    revision = values.get('versionCode', '')
    if not revision.isdigit() or not 1 <= int(revision) <= REVISION:
        raise RuntimeError('Refused an unknown or newer native module revision.')
    flags = root(config, f'''for flag in disable remove; do
    if [ -e {directory}/"$flag" ] || [ -L {directory}/"$flag" ]; then printf '%s\\n' "$flag"; fi
done''').splitlines()
    if any(flag not in ('disable', 'remove') for flag in flags):
        raise RuntimeError('Refused invalid native module lifecycle flags.')
    return {'directory': directory, 'properties': properties, 'revision': int(revision), 'flags': flags}


def _manifest(config, module):
    directory = module['directory']
    value = root(config, f'''if [ -e {directory}/manifest.json ] || [ -L {directory}/manifest.json ]; then
    [ -f {directory}/manifest.json ] && [ ! -L {directory}/manifest.json ] || exit 1
    cat {directory}/manifest.json
fi''')
    if not value:
        return None
    try:
        saved = json.loads(value)
    except (ValueError, TypeError) as error:
        raise RuntimeError('Refused invalid native module manifest.') from error
    if (not isinstance(saved, dict) or saved.get('schema') != 1 or saved.get('id') != MODULE_ID
            or saved.get('revision') != module['revision']):
        raise RuntimeError('Refused unknown native module manifest ownership or schema.')
    return saved


def _verify_assets(config, directory, checksum):
    """Anchor the guest checksum list to the newly built, verified package."""
    root(config, f'''[ -d {directory} ] && [ ! -L {directory} ] || exit 1
[ -f {directory}/SHA256SUMS ] && [ ! -L {directory}/SHA256SUMS ] || exit 1
[ -z "$(find {directory} -type l -print)" ] || exit 1
test "$(/data/adb/ksu/bin/busybox sha256sum {directory}/SHA256SUMS | cut -d ' ' -f 1)" = {checksum}
cd {directory}
/data/adb/ksu/bin/busybox sha256sum -c SHA256SUMS >/dev/null''')


def _stage_guard(active):
    """Do not replace a module or user flags changed during host packaging."""
    commands = [f'[ ! -e {PENDING} ] && [ ! -L {PENDING} ]']
    if active is None:
        commands.append(f'[ ! -e {MODULE} ] && [ ! -L {MODULE} ]')
    else:
        commands.extend((f'[ -d {MODULE} ] && [ ! -L {MODULE} ]',
                         f'[ -f {MODULE}/module.prop ] && [ ! -L {MODULE}/module.prop ]',
                         f'test "$(cat {MODULE}/module.prop)" = {shlex.quote(active["properties"])}'))
        for flag in ('disable', 'remove'):
            commands.append(f'[ ! -e {MODULE}/{flag} ] && [ ! -L {MODULE}/{flag} ]')
    guard = '\n'.join(command + ' || exit 1' for command in commands) + '\n'
    if active is not None and active.get('checksums_sha256'):
        guard += f'''[ -z "$(find {MODULE} -type l -print)" ] || exit 1
test "$(/data/adb/ksu/bin/busybox sha256sum {MODULE}/SHA256SUMS | cut -d ' ' -f 1)" = {active['checksums_sha256']} || exit 1
(cd {MODULE} && /data/adb/ksu/bin/busybox sha256sum -c SHA256SUMS >/dev/null) || exit 1
'''
    return guard


def install(config, platform_context=None):
    """Leave user choices and KernelSU stages intact; activation happens at boot."""
    if platform_context is not None:
        from core_platform import context_files
        context_files(platform_context)
    guest_supported(config)
    active, pending = _module(config, MODULE), _module(config, PENDING)
    for module in (active, pending):
        if module and module['flags']:
            print('Native compatibility lifecycle choice is preserved: ' +
                  ', '.join(module['flags']) + '.', flush=True)
            return {'installed': False, 'disabled': True, 'flags': module['flags'],
                    'pending': pending is not None}
    # Standard KernelSU handles staging, permissions and lifecycle. Never start
    # late shared-library mounts or reboot the user's running Android session.
    with tempfile.TemporaryDirectory(prefix='hyperos-native-module-') as temporary:
        archive = Path(temporary) / 'native-compat.zip'
        receipt = package(archive)
        with zipfile.ZipFile(archive) as contents:
            current = json.loads(contents.read('manifest.json'))
            checksum = hashlib.sha256(contents.read('SHA256SUMS')).hexdigest()
        if pending is not None:
            identical = _manifest(config, pending) == current
            if identical:
                _verify_assets(config, PENDING, checksum)
            # KernelSU may erase its old staging directory before extraction.
            # Keep a non-identical stage, including its feature choices, until
            # a normal boot activates it. The next launch can then update it.
            print('Native compatibility update is staged; reboot normally to activate it.', flush=True)
            result = {'installed': identical, 'reused': identical, 'pending': True,
                    'deferred': not identical, 'revision': pending['revision'], 'reboot_required': True}
            if identical and platform_context is not None:
                from core_platform import configure
                result['platform'] = configure(root, config, platform_context, PENDING, checksum)
            return result
        if active is not None:
            saved = _manifest(config, active)
            if saved == current:
                _verify_assets(config, MODULE, checksum)
                print('Portable native module is already installed.', flush=True)
                result = {'installed': True, 'reused': True, 'revision': REVISION}
                if platform_context is not None:
                    from core_platform import configure
                    result['platform'] = configure(root, config, platform_context, MODULE, checksum)
                return result
            from native_module_history import checksum_for
            prior_checksum = checksum_for(saved)
            if prior_checksum is None:
                print('Unknown Core controls are preserved; automatic replacement was skipped.', flush=True)
                return {'installed': False, 'preserved': True, 'reason': 'unreviewed-core-controls',
                        'revision': active['revision']}
            try:
                _verify_assets(config, MODULE, prior_checksum)
            except RuntimeError:
                print('Changed Core controls are preserved; automatic replacement was skipped.', flush=True)
                return {'installed': False, 'preserved': True, 'reason': 'changed-core-controls',
                        'revision': active['revision']}
            active['checksums_sha256'] = prior_checksum
        remote = '/data/local/tmp/hyperos-native-compat-' + receipt['sha256'][:16] + '.zip'
        adb(config, 'push', str(archive), remote, capture_output=True, check=True, timeout=30)
        try:
            root(config, _stage_guard(active) +
                 f'test "$(sha256sum {remote} | cut -d " " -f 1)" = {receipt["sha256"]}\n'
                 f'/data/adb/ksud module install {shlex.quote(remote)}')
        finally:
            root(config, 'rm -f ' + shlex.quote(remote))
        platform = None
        if platform_context is not None:
            # Standard KernelSU versions may activate a first install directly
            # or publish a staged update. Authenticate whichever it published.
            staged, installed = _module(config, PENDING), _module(config, MODULE)
            selected = staged or installed
            if selected is None or selected['flags'] or _manifest(config, selected) != current:
                raise RuntimeError('Core platform migration deferred: installed owner is not verified.')
            _verify_assets(config, selected['directory'], checksum)
            from core_platform import configure
            platform = configure(root, config, platform_context, selected['directory'], checksum)
    print('Portable native module installed. Reboot once to activate early patches.', flush=True)
    result = {'installed': True, 'reused': False, 'revision': REVISION, 'reboot_required': True}
    if platform is not None:
        result['platform'] = platform
    return result


def status(config):
    return root(config, f'if [ -f {MODULE}/state/status.tsv ]; then cat {MODULE}/state/status.tsv; '
                'else echo "not-activated"; fi')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    config = runtime()
    if args.status:
        print(status(config))
    else:
        result = install(config)
        (ROOT / 'local').mkdir(exist_ok=True)
        (ROOT / 'local/native-compat.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
