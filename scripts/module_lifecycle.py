"""Shared conservative KernelSU lifecycle checks for host-delivered modules."""
import re
import shlex
import hashlib


class UnreviewedHook(RuntimeError):
    """An owned module contains a local or otherwise unknown startup hook."""

    def __init__(self, name):
        self.name = name
        super().__init__('Unreviewed module lifecycle hook preserved: ' + name)


def _paths(module):
    match = re.fullmatch(r'/data/adb/modules/([A-Za-z0-9_.-]+)', module)
    if not match:
        raise ValueError('Invalid owned KernelSU module path.')
    return match[1], '/data/adb/modules_update/' + match[1]


def state_script(module):
    """Read lifecycle markers without following or interpreting their content."""
    _, pending = _paths(module)
    return (f'if [ -e {shlex.quote(pending)} ] || [ -L {shlex.quote(pending)} ]; then echo pending; fi\n'
            f'for flag in disable remove; do if [ -e {shlex.quote(module)}/$flag ] || '
            f'[ -L {shlex.quote(module)}/$flag ]; then echo "$flag"; fi; done')


def preserved(root, config, module, *, enable=False):
    """Pending updates/removal always win; enabling may bypass only disable."""
    flags = root(config, state_script(module)).splitlines()
    if any(flag not in ('pending', 'disable', 'remove') for flag in flags):
        raise RuntimeError('Unexpected KernelSU lifecycle response.')
    blocked = [flag for flag in flags if flag != 'disable' or not enable]
    if blocked:
        print('KernelSU module lifecycle preserved: ' + ', '.join(blocked) + '.', flush=True)
        return {'preserved': True, 'lifecycle': blocked}
    return None


def mutation_guard(module, *, allow_disabled=False):
    """Recheck immediately before a host mutation; never overwrite KSU staging."""
    _, pending = _paths(module)
    flags = ('remove',) if allow_disabled else ('disable', 'remove')
    script = (f'if [ -e {shlex.quote(pending)} ] || [ -L {shlex.quote(pending)} ]; then exit 75; fi\n'
              f'[ ! -L {shlex.quote(module)} ] || exit 75\n')
    for flag in flags:
        path = shlex.quote(module + '/' + flag)
        script += f'if [ -e {path} ] || [ -L {path} ]; then exit 75; fi\n'
    return script


def enable_owned(root, config, module):
    """Call only after validating the existing manifest; authenticate ownership."""
    module_id, _ = _paths(module)
    root(config, 'set -e\n' + mutation_guard(module, allow_disabled=True) +
         f'test -d {module}\ntest ! -L {module}/module.prop\n'
         f'test "$(sed -n \'s/^id=//p\' {module}/module.prop)" = {module_id}\n'
         f'test "$(sed -n \'s/^author=//p\' {module}/module.prop)" = HyperOS-AVD\n'
         f'rm -f {module}/disable')


BLOCKED_FUNCTION = r'''blocked() {
    [ -e "$MODDIR/disable" ] || [ -L "$MODDIR/disable" ] ||
        [ -e "$MODDIR/remove" ] || [ -L "$MODDIR/remove" ] ||
        [ -e "/data/adb/modules_update/${MODDIR##*/}" ] ||
        [ -L "/data/adb/modules_update/${MODDIR##*/}" ]
}
blocked && exit 0
'''


def guarded_hook(script):
    """Standardize generated hooks, including checks after waits and before effects."""
    script = re.sub(r'^\[ -f "\$MODDIR/disable" \] && exit 0\n', '', script, flags=re.M)
    script = re.sub(r'^\[ ! -f "\$MODDIR/(?:disable|remove)" \] \|\| exit 0\n', '', script, flags=re.M)
    script = script.replace('if [ -e "$MODDIR/disable" ] || [ -L "$MODDIR/disable" ]; then exit 0; fi\n', '')
    anchor = 'MODDIR=${0%/*}\n'
    if script.count(anchor) != 1:
        raise RuntimeError('Ambiguous KernelSU hook module directory.')
    script = script.replace(anchor, anchor + BLOCKED_FUNCTION)
    # Namespace shells inherit only the authenticated module directory. Inline
    # marker checks also work inside quoted sh bodies; shell functions do not.
    script = script.replace(anchor, anchor + 'export MODDIR\n', 1)
    marker_check = ('if [ -e "$MODDIR/disable" ] || [ -L "$MODDIR/disable" ] || '
                    '[ -e "$MODDIR/remove" ] || [ -L "$MODDIR/remove" ] || '
                    '[ -e "/data/adb/modules_update/${MODDIR##*/}" ] || '
                    '[ -L "/data/adb/modules_update/${MODDIR##*/}" ]; then exit 0; fi\n')
    lines = script.splitlines(keepends=True)
    result = []
    for line in lines:
        stripped = line.strip()
        # A paired start restores a provider already stopped by this hook.
        # Cleanup/restoration must still run if a flag appears during stop.
        effects = (stripped.startswith(('for pid in ', 'while [ "$(getprop sys.boot_completed)',
                                        'am force-stop ', 'stop ', 'settings put ',
                                        '/data/adb/ksud resetprop ', 'sh "$MODDIR/',
                                        'mkdir ', 'cp ', 'chmod ', 'chcon ', 'ln '))
                   or ('mount -o ' in line and not stripped.startswith('#'))
                   or ('nsenter ' in line and any(word in line for word in ('cp ', 'mkdir ', 'chcon '))))
        if stripped.startswith('count=') and result and any('sys.boot_completed' in item for item in result[-3:]):
            effects = True
        if effects:
            result.append(line[:len(line) - len(line.lstrip())] + marker_check)
        result.append(line)
    return ''.join(result)


def _owned_guard(module, *, allow_disabled=False):
    module_id, _ = _paths(module)
    return (mutation_guard(module, allow_disabled=allow_disabled) +
            f'test -d {module}\ntest ! -L {module}/module.prop\n'
            f'test "$(sed -n \'s/^id=//p\' {module}/module.prop)" = {module_id}\n'
            f'test "$(sed -n \'s/^author=//p\' {module}/module.prop)" = HyperOS-AVD\n')


def _hook_plans(root, config, module, hooks, *, allow_disabled=False):
    """Authenticate actual hook bytes, never a hash supplied by the receipt."""
    plans = []
    for name, (legacy, current) in hooks.items():
        if name not in ('service.sh', 'post-fs-data.sh'):
            raise ValueError('Invalid module lifecycle hook.')
        versions = (legacy,) if isinstance(legacy, str) else tuple(legacy)
        if not versions or any(not isinstance(version, str) for version in versions):
            raise ValueError('Reviewed module hooks must contain exact script text.')
        reviewed = {hashlib.sha256(version.encode()).hexdigest() for version in versions}
        after = hashlib.sha256(current.encode()).hexdigest()
        command = (_owned_guard(module, allow_disabled=allow_disabled) +
                   f'if [ -f {module}/{name} ] && [ ! -L {module}/{name} ]; then\n'
                   f'sha256sum {module}/{name}\nelse echo unreviewed; fi')
        fields = root(config, 'set -e\n' + command).split()
        if not fields or fields[0] not in reviewed | {after}:
            raise UnreviewedHook(name)
        plans.append((name, fields[0], after, current))
    return plans


def verify_hooks(root, config, module, hooks, *, allow_disabled=False):
    """Read-only preflight before enabling or changing any existing payload."""
    return {name: before for name, before, _, _ in
            _hook_plans(root, config, module, hooks, allow_disabled=allow_disabled)}


def refresh_hooks(root, config, module, hooks):
    """Replace only exact reviewed hook versions, preserving local hook edits."""
    plans = _hook_plans(root, config, module, hooks)
    # Validate every hook before writing any. A local edit to the second hook
    # must not leave the first hook partially migrated.
    for name, before, after, current in plans:
        if before == after:
            continue
        marker = 'HYPEROS_LIFECYCLE_' + after
        temporary = module + '/.' + name + '.lifecycle.$$'
        replacement = ('set -e\n' + _owned_guard(module) +
                       f'test "$(sha256sum {module}/{name} | cut -d " " -f 1)" = {before}\n'
                       'umask 077\n' + f'temporary={temporary}\n'
                       'test ! -e "$temporary"\ntest ! -L "$temporary"\n'
                       'trap \'rm -f "$temporary"\' EXIT HUP INT TERM\n' +
                       f'cp -p {module}/{name} "$temporary"\n' +
                       f'cat > "$temporary" <<\'{marker}\'\n' + current + marker + '\n' +
                       f'test "$(sha256sum "$temporary" | cut -d " " -f 1)" = {after}\n' +
                       _owned_guard(module) +
                       f'test ! -L {module}/{name}\n'
                       f'test "$(sha256sum {module}/{name} | cut -d " " -f 1)" = {before}\n'
                       f'mv "$temporary" {module}/{name}\ntrap - EXIT HUP INT TERM')
        root(config, replacement)


def refresh_receipt(root, config, module, previous, current):
    """Update an inspected JSON receipt without touching native payloads."""
    path = module + '/manifest.json'
    if '\0' in previous or '\0' in current:
        raise ValueError('Invalid module receipt text.')
    after = hashlib.sha256(current.encode()).hexdigest()
    inspect = ('set -e\n' + _owned_guard(module) +
               f'test -f {path}\ntest ! -L {path}\n'
               f'test "$(cat {path})" = {shlex.quote(previous.rstrip(chr(10)))}\n'
               f'sha256sum {path}')
    fields = root(config, inspect).split()
    if not fields or not re.fullmatch(r'[0-9a-f]{64}', fields[0]):
        raise RuntimeError('Module receipt changed before lifecycle migration.')
    before = fields[0]
    if before == after:
        return
    marker = 'HYPEROS_RECEIPT_' + after
    temporary = module + '/.manifest.json.lifecycle.$$'
    replacement = ('set -e\n' + _owned_guard(module) +
                   f'test ! -L {path}\n'
                   f'test "$(sha256sum {path} | cut -d " " -f 1)" = {before}\n'
                   'umask 077\n' + f'temporary={temporary}\n'
                   'test ! -e "$temporary"\ntest ! -L "$temporary"\n'
                   'trap \'rm -f "$temporary"\' EXIT HUP INT TERM\n' +
                   f'cp -p {path} "$temporary"\n' +
                   f'cat > "$temporary" <<\'{marker}\'\n' + current + marker + '\n' +
                   f'test "$(sha256sum "$temporary" | cut -d " " -f 1)" = {after}\n' +
                   _owned_guard(module) + f'test ! -L {path}\n'
                   f'test "$(sha256sum {path} | cut -d " " -f 1)" = {before}\n'
                   f'mv "$temporary" {path}\ntrap - EXIT HUP INT TERM')
    root(config, replacement)
