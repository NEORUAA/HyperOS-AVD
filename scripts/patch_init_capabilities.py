#!/usr/bin/env python3
"""Gate audited OEM init services on their actual boot-local capabilities.

Content profiles are shared across firmware/device names. Unknown init text or
QTI script content is rejected before producing edits. Init rc must be baked
before it is parsed; mounting a replacement after boot is not equivalent.
"""
import hashlib
from pathlib import Path
import re
import shlex


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = 'system/etc/hyperos-init-capabilities.sh'
SCRIPT_SOURCE = ROOT / 'config/init_capabilities.sh'
QTI_SCRIPT = 'product/bin/init.qti.display.sh'
QTI_SCRIPT_SHA256 = '9d78779177d4f96b0b94c50172d3fcd30d1d55d0be5e6eff2a72a6c6bfb9511d'
CAPABILITY = 'sys.hyperos_avd.cap.'
REQUEST = 'sys.hyperos_avd.init.qti_display_requested'

# Full rc hashes admit only the reviewed service options and start paths.
# Profiles deliberately describe file contents, not an OTA version or AVD name.
PROFILES = {
    'system_ext/etc/init/init.launch_boost.rc': {
        'before': 'fc8952d3a009c795dced757f865e4629969b9c8ad1696cd9fa7b7c776d211002',
        'after': '6602b229ffc7eb525ed24c3ed41eabe9734daa93ae509da1aba10870030af8fc',
        'service': 'iorapd', 'capability': 'iorap',
        'trigger': b'on property:persist.sys.stability.PrereadEnable=true\n',
        'disabled': False,
    },
    'system_ext/etc/init/init.milletmonitor.rc': {
        'before': 'c4f01393bea227d42b14342a2b9736acae5111c968bc60dcea37094d646f943d',
        'after': 'c316e231f1c9580c655d7af8f85f7d78851ccd58df8dae80c17ecd2f8b63cffe',
        'service': 'millet_monitor', 'capability': 'millet',
        'trigger': b'on property:sys.millet.monitor=1\n',
        'disabled': True,
    },
    'product/etc/init/init.qti.display.rc': {
        'before': '42d8d3fd727f02a2c6618555f5b3bacb6f2935376a984cef9f6b5cf83e5be747',
        'after': 'dbf00386b1d2ffc6b1735df5e224d58da37be3564d575422746e579dc59f89e4',
        'service': 'vendor_sys_qti_display', 'capability': 'qti_display',
        'trigger': b'on post-fs-data\n', 'disabled': True,
    },
}

BOOT_INIT = b'''
# Resolve capabilities before guarded OEM services are allowed to start.
service hyperos-init-capabilities /system/bin/sh /system/etc/hyperos-init-capabilities.sh
    user root
    group root system
    seclabel u:r:su:s0
    disabled
    oneshot

on post-fs-data
    start hyperos-init-capabilities
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _transform(data, row):
    """Preserve all unrelated OEM commands and original property conditions."""
    service = row['service'].encode()
    definition = re.compile(rb'(?m)^service ' + re.escape(service) + rb' [^\n]+\n')
    matches = list(definition.finditer(data))
    if len(matches) != 1 or data.count(row['trigger']) != 1:
        raise RuntimeError('Audited init service layout differs.')
    if not row['disabled']:
        match = matches[0]
        data = data[:match.end()] + b'    disabled\n' + data[match.end():]
    if row['capability'] != 'qti_display':
        trigger = row['trigger'][:-1]
        gate = (' && property:' + CAPABILITY + row['capability'] + '=1\n').encode()
        return data.replace(row['trigger'], trigger + gate, 1)
    # Event+property actions cannot replay a past post-fs-data event. Latch its
    # request so either ordering of the request and capability publication works.
    original = b'    start ' + service
    if data.count(original) != 1:
        raise RuntimeError('Audited QTI start differs.')
    data = data.replace(original, ('    setprop ' + REQUEST + ' 1').encode(), 1)
    data = data.rstrip(b'\n') + b'\n\n'
    data += ('on property:' + REQUEST + '=1 && property:' + CAPABILITY +
             'qti_display=1\n    start ' + row['service'] + '\n').encode()
    return data


def patch(path, data):
    """Return a verified replacement; reject unknown bytes and paths."""
    path = str(path).lstrip('/')
    row = PROFILES.get(path)
    if row is None:
        raise RuntimeError('Unsupported init capability path: ' + path)
    before = digest(data)
    if before == row['before']:
        fixed = _transform(data, row)
        if digest(fixed) != row['after']:
            raise RuntimeError('Init capability output differs from its audited profile.')
        return fixed
    # The accepted patched digest is explicit, never inferred from arbitrary
    # input text carrying a marker. Catalog output hashes are filled below.
    if before == row['after']:
        return data
    raise RuntimeError('Unsupported init capability content: ' + path)


def logical_lines(data):
    """Expose conservative init folding before auditing commands or imports.

    Android accepts both LF and CRLF continuations. A comment at a token
    boundary instead ends the statement, even if its text ends in backslash.
    Refuse an inline hash on a candidate folding line rather than interpreting
    ambiguous comment/quoted-token grammar as a complete Android parser.
    """
    pending, folded = '', False
    for physical in data.decode('utf-8').split('\n'):
        if pending or folded:
            physical = physical.lstrip(' \t\r')
        # A whole-line comment is discarded by init, not a continuation prefix.
        if not pending and not folded and physical.lstrip().startswith('#'):
            yield physical, False
            continue
        ending = physical[:-1] if physical.endswith('\r') else physical
        trailing = len(ending) - len(ending.rstrip('\\'))
        continuation = (2 if physical.endswith('\r') else 1) if trailing % 2 else 0
        if '\\\r' in physical[:-2] or '\\\r' in physical and not physical.endswith('\\\r'):
            # Init's nonterminal escaped CR handling differs from shell lexing
            # and can splice a keyword or target without a line continuation.
            raise RuntimeError('Unsupported escaped carriage return in init')
        if continuation and '#' in physical:
            raise RuntimeError('Ambiguous commented init continuation')
        pending += physical[:-continuation] if continuation else physical
        if continuation:
            folded = True
            continue
        yield pending, folded
        pending, folded = '', False
    if pending or folded:
        yield pending, folded


def expansion_can_match(value, candidates):
    """Overapproximate supported init properties without guessing their values."""
    expression, cursor = '', 0
    for match in re.finditer(r'\$\{[a-zA-Z0-9_.-]+(?::-[^${}]*)?\}', value):
        literal = value[cursor:match.start()]
        if '$' in literal:
            raise ValueError('Malformed or unsupported init property expansion')
        expression += re.escape(literal) + '.*'
        cursor = match.end()
    suffix = value[cursor:]
    if '$' in suffix:
        raise ValueError('Malformed or unsupported init property expansion')
    expression += re.escape(suffix)
    pattern = re.compile(expression)
    return any(pattern.fullmatch(candidate) for candidate in candidates)


def audit_start_commands(init_files):
    """Authenticate guarded declarations and direct routes in the init graph."""
    owners = {row['service']: path for path, row in PROFILES.items()}
    for path, data in init_files.items():
        name = str(path).lstrip('/')
        for line, _ in logical_lines(data):
            # Init accepts literal quoting in unrelated write/export arguments.
            # Tokenize folded lines first so quoted keywords and arguments do
            # not hide a guarded declaration or a direct service start.
            command = re.sub(r'^\s*onrestart\s+', '', line).lstrip()
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError as error:
                if any(re.search(r'(?<![\w.-])' + re.escape(service) + r'(?![\w.-])', command)
                       for service in owners):
                    raise RuntimeError('Unreviewed init service start syntax: ' + name) from error
                continue
            if len(tokens) >= 2 and tokens[0] == 'service':
                owner = owners.get(tokens[1])
                if owner is not None:
                    # An override in any other parsed file can remove disabled
                    # or change its class even without an explicit start route.
                    if name != owner:
                        raise RuntimeError('Unreviewed init service definition: ' + name)
                    patch(name, data)
                continue
            if tokens and tokens[0] == 'onrestart':
                tokens = tokens[1:]
            if len(tokens) >= 2 and tokens[0] == 'setprop' and '$' in tokens[1]:
                try:
                    control = expansion_can_match(tokens[1], ('ctl.start', 'ctl.restart'))
                except ValueError as error:
                    raise RuntimeError('Unsupported init control property expansion syntax: ' + name) from error
                if control:
                    raise RuntimeError('Unsupported init control property expansion: ' + name)
            if (len(tokens) >= 3 and tokens[0] == 'setprop'
                    and tokens[1] in ('ctl.start', 'ctl.restart')):
                tokens = [tokens[1], *tokens[2:]]
            if len(tokens) >= 2 and tokens[0] in ('start', 'restart', 'exec_start',
                                                'enable', 'ctl.start', 'ctl.restart'):
                if '$' in tokens[1]:
                    try:
                        guarded = expansion_can_match(tokens[1], owners)
                    except ValueError as error:
                        raise RuntimeError('Unsupported init service target expansion syntax: ' + name) from error
                    if guarded:
                        raise RuntimeError('Unsupported init service target expansion: ' + name)
                owner = owners.get(tokens[1])
            else:
                owner = None
            if owner is not None:
                if name != owner or tokens[0] != 'start':
                    raise RuntimeError('Unreviewed init service start path: ' + name)
                # A profile hash validates all allowed direct commands together.
                patch(name, data)


def image_replacements(inputs):
    """Produce small image edits/receipt without reading or writing any image."""
    normalized = {str(path).lstrip('/'): data for path, data in inputs.items()}
    if len(normalized) != len(inputs) or not normalized:
        raise RuntimeError('Invalid init capability inputs.')
    supported = set(PROFILES) | {QTI_SCRIPT}
    if set(normalized) - supported:
        raise RuntimeError('Unsupported init capability input path.')
    qti = 'product/etc/init/init.qti.display.rc' in normalized
    if qti != (QTI_SCRIPT in normalized):
        raise RuntimeError('QTI rc requires its audited script identity.')
    if qti and digest(normalized[QTI_SCRIPT]) != QTI_SCRIPT_SHA256:
        raise RuntimeError('Unsupported QTI display script content.')
    edits, targets = {}, {}
    # Validate all inputs before exposing any replacement to the caller.
    for path, data in normalized.items():
        if path == QTI_SCRIPT:
            continue
        fixed = patch(path, data)
        edits[path] = (fixed, 0o644, 'u:object_r:system_file:s0')
        targets[path] = {'before': PROFILES[path]['before'], 'after': digest(fixed),
                         'capability': PROFILES[path]['capability']}
    if not targets:
        raise RuntimeError('No init capability service inputs.')
    script = SCRIPT_SOURCE.read_bytes()
    edits[SCRIPT_PATH] = (script, 0o755, 'u:object_r:system_file:s0')
    receipt = {'schema': 1, 'targets': targets,
               'script': {'path': SCRIPT_PATH, 'sha256': digest(script)},
               'qti_script_sha256': QTI_SCRIPT_SHA256 if qti else None}
    return edits, receipt
