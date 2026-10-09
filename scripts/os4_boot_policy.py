"""Share pre-init capability gates and ART topology policy across OS4 images."""
from pathlib import Path
import re
import shlex
import stat
import posixpath

from build_image import erofs
from erofs_image import Reader
import dex2oat_cpu_policy as cpu
import patch_init_capabilities as capabilities
from patch_boot_services import kernel_probe

INIT_PATH = 'system_ext/etc/init/init.hyperos_avd.rc'
POLICY_PATH = 'system_ext/etc/selinux/system_ext_sepolicy.cil'
PROBE_PATH = 'system/bin/hyperos_kernel_probe'
HELPER_POLICY = b'''
; Enforcing boot-local capability and ART helpers.
(allow init su (process (transition)))
(allow su shell_exec (file (entrypoint)))
(allow su default_prop (file (read open getattr map)))
(allow su bootloader_prop (file (read open getattr map)))
(allow su device (dir (read open getattr search)))
(allow su dalvik_config_prop (file (read open getattr map)))
(allow su dalvik_dynamic_config_prop (file (read open getattr map)))
(allow su system_file (file (read open getattr map execute execute_no_trans)))
(allow su system_lib_file (file (read open getattr map execute)))
(allow su toolbox_exec (file (read open getattr map execute execute_no_trans)))
(allow su property_socket (sock_file (write)))
(allow su su (unix_stream_socket (create connect write read getattr getopt setopt shutdown)))
(allow su init (unix_stream_socket (connectto)))
(allow su su (netlink_socket (create)))
(allow su system_prop (property_service (set)))
(allow su sysfs_devices_system_cpu (dir (read open getattr search)))
(allow su sysfs_devices_system_cpu (file (read open getattr)))
'''


def init_files(partitions, replacements=None, removals=()):
    """Read the effective init graph from raw partitions without extracting them."""
    nodes = {}
    for selection, image in partitions:
        prefix, _, subtree = selection.partition(':')
        reader = Reader(image)
        try:
            tree = dict(reader.walk())
            entries = reader.walk(tree[subtree]['nid']) if subtree else tree.items()
            for path, inode in entries:
                full = '/'.join(filter(None, (prefix.strip('/'), path)))
                source = '/'.join(filter(None, (subtree, path)))
                link = reader.flat(inode).decode() if stat.S_ISLNK(inode['mode']) else None
                nodes[full] = {'mode': inode['mode'], 'source': (image, '/' + source), 'link': link}
        finally:
            reader.close()
    for path in removals:
        for existing in tuple(nodes):
            if existing == path or existing.startswith(path + '/'):
                del nodes[existing]
    for path, (data, _, _) in (replacements or {}).items():
        nodes[path] = {'mode': stat.S_IFREG, 'data': data, 'link': None}
    def resolve(path, *, optional=False, directory=False):
        current = path
        for _ in range(32):
            parts = current.split('/')
            for index in range(1, len(parts) + 1):
                prefix = '/'.join(parts[:index])
                node = nodes.get(prefix, {})
                link = node.get('link')
                if link is None:
                    continue
                if not link or '\0' in link:
                    raise RuntimeError('Invalid init source alias: ' + path)
                target = link.lstrip('/') if link.startswith('/') else posixpath.join(posixpath.dirname(prefix), link)
                current = posixpath.normpath(posixpath.join(target, *parts[index:]))
                if current == '..' or current.startswith('../'):
                    raise RuntimeError('Init source alias escapes the image: ' + path)
                break
            else:
                final = nodes.get(current)
                if final is None and optional:
                    return None
                if not final or not (stat.S_ISREG(final['mode'])
                                     or directory and stat.S_ISDIR(final['mode'])):
                    raise RuntimeError('Unresolved init source alias: ' + path)
                return current, final
        raise RuntimeError('Cyclic init source alias: ' + path)
    files, contents = {}, {}
    pending = []

    def collect(path, *, optional=False):
        resolved = resolve(path, optional=optional, directory=optional)
        if resolved is None or stat.S_ISDIR(resolved[1]['mode']):
            return
        target, final = resolved
        if target not in contents:
            contents[target] = final['data'] if 'data' in final else erofs(*final['source'])
        if path not in files:
            files[path] = contents[target]
            if path != capabilities.QTI_SCRIPT:
                pending.append(path)

    # Init imports can point outside /etc/init or through directory aliases.
    # Audit every possible image rc instead of guessing boot property values.
    for path in nodes:
        if ((path.endswith('.rc') and (stat.S_ISREG(nodes[path]['mode'])
                                      or stat.S_ISLNK(nodes[path]['mode'])))
                or path == capabilities.QTI_SCRIPT):
            collect(path)
    while pending:
        path = pending.pop()
        # A literal import need not use the .rc suffix. Property imports are
        # overapproximated against the final image, including optional defaults.
        for line, continued in capabilities.logical_lines(files[path]):
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError as error:
                if re.match(r'^\s*import(?:\s|$)', line):
                    raise RuntimeError('Invalid init import syntax: ' + path) from error
                continue
            if not tokens or tokens[0] != 'import':
                continue
            # Refuse folded imports rather than guessing escaped path tokens.
            # In particular, a continued import may reference a non-rc leaf
            # that is absent from the conservative initial .rc scan.
            if continued:
                raise RuntimeError('Unsupported continued init import: ' + path)
            if len(tokens) != 2 or '\0' in tokens[1]:
                raise RuntimeError('Invalid init import syntax: ' + path)
            imported = tokens[1].lstrip('/')
            normalized = posixpath.normpath(imported)
            if normalized == '..' or normalized.startswith('../'):
                raise RuntimeError('Init import escapes the image: ' + path)
            properties = list(re.finditer(r'\$\{[a-zA-Z0-9_.-]+(?::-[^}]*)?\}', imported))
            if not properties:
                if '$' in imported:
                    raise RuntimeError('Unsupported init import expansion: ' + path)
                collect(normalized, optional=True)
                continue
            imported_directory = posixpath.dirname(imported)
            if '$' not in imported_directory:
                resolved_directory = resolve(imported_directory, optional=True, directory=True)
                if resolved_directory is not None:
                    imported = posixpath.join(resolved_directory[0], posixpath.basename(imported))
                    properties = list(re.finditer(r'\$\{[a-zA-Z0-9_.-]+(?::-[^}]*)?\}', imported))
            elif not imported.endswith('.rc'):
                # Every image .rc is already audited, including alias targets.
                # Dynamic directories importing other grammars need review.
                raise RuntimeError('Unsupported init import directory expansion: ' + path)
            pattern, cursor = '', 0
            for match in properties:
                literal = imported[cursor:match.start()]
                if '$' in literal:
                    raise RuntimeError('Unsupported init import expansion: ' + path)
                pattern += re.escape(literal) + '.*'
                cursor = match.end()
            if '$' in imported[cursor:]:
                raise RuntimeError('Unsupported init import expansion: ' + path)
            expression = re.compile(pattern + re.escape(imported[cursor:]) + '$')
            for candidate in nodes:
                if expression.fullmatch(candidate):
                    collect(candidate, optional=True)
    return files


def append_policy(original):
    """Keep existing grants, adding only the scoped helper rules once."""
    result = original.rstrip(b'\n') + b'\n'
    for line in (HELPER_POLICY + cpu.SEPOLICY).splitlines():
        if line.startswith(b'(allow ') and line not in result.splitlines():
            result += line + b'\n'
    return result


def service_definitions(data, service):
    """Recognize protected helper declarations using init's folded lines."""
    definitions = []
    for line, _ in capabilities.logical_lines(data):
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError as error:
            if service.decode() in line and re.match(r'^\s*"?service(?:"?\s|$)', line):
                raise RuntimeError('Unexpected existing boot policy service: ' + service.decode()) from error
            continue
        if len(tokens) >= 2 and tokens[:2] == ['service', service.decode()]:
            definitions.append(line)
    return definitions


def append_init(original):
    result = original
    for block, service in ((capabilities.BOOT_INIT, b'hyperos-init-capabilities'),
                           (cpu.BOOT_INIT, b'hyperos-dex2oat-cpu')):
        definitions = service_definitions(result, service)
        if block not in result or len(definitions) != 1:
            if definitions:
                raise RuntimeError('Unexpected existing boot policy service: ' + service.decode())
            result += block
    return result


def image_replacements(graph, qti_script, init, policy, work):
    """Reject unknown start routes before producing the shared image edits."""
    for block, service in ((capabilities.BOOT_INIT, b'hyperos-init-capabilities'),
                           (cpu.BOOT_INIT, b'hyperos-dex2oat-cpu')):
        definitions = [path for path, data in graph.items() if path != capabilities.QTI_SCRIPT
                       for _ in service_definitions(data, service)]
        if definitions and (definitions != [INIT_PATH] or block not in graph[INIT_PATH]):
            raise RuntimeError('Unexpected existing boot policy service: ' + service.decode())
    if INIT_PATH in graph and graph[INIT_PATH] != init:
        raise RuntimeError('Boot policy init differs from its effective graph.')
    capabilities.audit_start_commands({path: data for path, data in graph.items()
                                        if path != capabilities.QTI_SCRIPT})
    inputs = {path: graph[path] for path in capabilities.PROFILES if path in graph}
    for path in tuple(capabilities.PROFILES)[:2]:
        if path not in inputs:
            raise RuntimeError('Missing OS4 init capability service: ' + path)
    if 'product/etc/init/init.qti.display.rc' in inputs:
        inputs[capabilities.QTI_SCRIPT] = qti_script
    edits, receipt = capabilities.image_replacements(inputs)
    edits.update(cpu.image_replacements())
    edits[INIT_PATH] = (append_init(init), 0o644, 'u:object_r:system_file:s0')
    edits[POLICY_PATH] = (append_policy(policy), 0o644, 'u:object_r:system_file:s0')
    edits[PROBE_PATH] = (kernel_probe(Path(work) / 'kernel-probe').read_bytes(),
                         0o755, 'u:object_r:system_file:s0')
    receipt['dex2oat'] = cpu.receipt()
    return edits, receipt
