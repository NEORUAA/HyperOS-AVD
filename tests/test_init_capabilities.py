"""Check early init gates and capability publication without guest operations."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_init_capabilities as gates


def fixture(path):
    """Own small inputs exercise transformations without vendoring OEM text."""
    row = dict(gates.PROFILES[path])
    options = b'    class main\n    user system\n'
    if row['disabled']:
        options += b'    disabled\n'
    if row['capability'] == 'qti_display':
        options += b'    oneshot\n    seclabel u:r:vendor_sys_qti_display:s0\n'
        commands = b'    start vendor_sys_qti_display'
    elif row['capability'] == 'millet':
        options += b'    onrestart setprop sys.millet.monitor 2\n'
        commands = b'    stop millet_monitor\n    start millet_monitor\n'
    else:
        commands = b'    start iorapd\n'
    original = b'service ' + row['service'].encode() + b' /system_ext/bin/test\n'
    original += options + b'\n' + row['trigger'] + commands
    original += b'\n\non boot\n    setprop unrelated.value 1\n'
    row['before'] = gates.digest(original)
    row['after'] = gates.digest(gates._transform(original, row))
    return original, row


class InitGateTests(unittest.TestCase):
    def test_property_gates_keep_commands_user_conditions_and_restart_options(self):
        for path in tuple(gates.PROFILES)[:2]:
            original, row = fixture(path)
            with patch.dict(gates.PROFILES, {path: row}):
                result = gates.patch(path, original)
                self.assertEqual(gates.patch('/' + path, result), result)
                self.assertIn(row['trigger'][:-1] + (' && property:' + gates.CAPABILITY +
                    row['capability'] + '=1\n').encode(), result)
                self.assertIn(b'on boot\n    setprop unrelated.value 1\n', result)
                self.assertEqual(result.count(b'    disabled\n'), 1)
                self.assertIn(b'    start ' + row['service'].encode(), result)
                if row['service'] == 'millet_monitor':
                    self.assertIn(b'    onrestart setprop sys.millet.monitor 2\n', result)
                    self.assertIn(b'    stop millet_monitor\n    start millet_monitor\n', result)

    def test_qti_event_is_latched_without_changing_exec_domain_or_service(self):
        path = 'product/etc/init/init.qti.display.rc'
        original, row = fixture(path)
        with patch.dict(gates.PROFILES, {path: row}):
            result = gates.patch(path, original)
            self.assertIn(('on post-fs-data\n    setprop ' + gates.REQUEST + ' 1').encode(), result)
            self.assertIn(('on property:' + gates.REQUEST + '=1 && property:' +
                          gates.CAPABILITY + 'qti_display=1\n').encode(), result)
            self.assertEqual(result.count(b'    start vendor_sys_qti_display'), 1)
            self.assertIn(b'    seclabel u:r:vendor_sys_qti_display:s0\n', result)
            self.assertIn(b'    oneshot\n', result)

    def test_unknown_paths_bytes_and_changed_transform_fail_closed(self):
        path = next(iter(gates.PROFILES))
        original, row = fixture(path)
        with patch.dict(gates.PROFILES, {path: row}):
            for value in (original + b'# changed\n', b'unknown'):
                with self.assertRaisesRegex(RuntimeError, 'Unsupported init capability content'):
                    gates.patch(path, value)
            with patch.object(gates, '_transform', return_value=original):
                with self.assertRaisesRegex(RuntimeError, 'output differs'):
                    gates.patch(path, original)
        with self.assertRaisesRegex(RuntimeError, 'Unsupported init capability path'):
            gates.patch('vendor/etc/init/unrelated.rc', original)

    def test_qti_bundle_authenticates_script_before_returning_edits(self):
        path = 'product/etc/init/init.qti.display.rc'
        original, row = fixture(path)
        with patch.dict(gates.PROFILES, {path: row}):
            with self.assertRaisesRegex(RuntimeError, 'requires its audited script'):
                gates.image_replacements({path: original})
            with self.assertRaisesRegex(RuntimeError, 'Unsupported QTI display script'):
                gates.image_replacements({path: original, gates.QTI_SCRIPT: b'unknown'})
            script = b'own fixture script\n'
            with patch.object(gates, 'QTI_SCRIPT_SHA256', gates.digest(script)):
                edits, receipt = gates.image_replacements({path: original, gates.QTI_SCRIPT: script})
            self.assertEqual(set(edits), {path, gates.SCRIPT_PATH})
            self.assertEqual(receipt['targets'][path]['after'], row['after'])
            self.assertEqual(edits[gates.SCRIPT_PATH][1], 0o755)

    def test_init_graph_refuses_other_direct_or_control_start_routes(self):
        path = next(iter(gates.PROFILES))
        original, row = fixture(path)
        with patch.dict(gates.PROFILES, {path: row}):
            gates.audit_start_commands({path: original,
                'system/etc/init/other.rc': b'on boot\n    start unrelated\n'})
            gates.audit_start_commands({path: gates.patch(path, original)})
            for command in ('start iorapd', 'start "iorapd"', 'restart iorapd',
                            'exec_start iorapd', 'enable iorapd',
                            'onrestart start iorapd', 'setprop ctl.start iorapd',
                            'setprop ctl.restart iorapd'):
                with self.subTest(command=command):
                    with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service start'):
                        gates.audit_start_commands({path: original,
                            'vendor/etc/init/other.rc': ('on boot\n    ' + command + '\n').encode()})

    def test_folded_direct_routes_cannot_hide_guarded_service_starts(self):
        path = next(iter(gates.PROFILES))
        original, row = fixture(path)
        commands = ('start \\\n        iorapd', 'restart \\\n        iorapd',
                    'exec_start \\\n        iorapd', 'enable \\\n        iorapd',
                    'ctl.start \\\n        iorapd', 'ctl.restart \\\n        iorapd',
                    'setprop ctl.start \\\n        iorapd',
                    'setprop \\\n        ctl.restart \\\n        iorapd',
                    'onrestart \\\n        start \\\n        iorapd',
                    'st\\\nart iorapd', 'start ior\\\napd',
                    '"start" "iorapd"', 'setprop "ctl.start" iorapd')
        with patch.dict(gates.PROFILES, {path: row}):
            for command in commands:
                with self.subTest(command=command):
                    with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service start path'):
                        gates.audit_start_commands({path: original,
                            'vendor/etc/init/other.rc': ('on boot\n    ' + command + '\n').encode()})

    def test_guarded_declarations_authenticate_the_entire_profile_even_without_start(self):
        path = next(iter(gates.PROFILES))
        original, row = fixture(path)
        foreign = (b'service iorapd /vendor/bin/foreign\n    override\n    class main\n',
                   b'"service" "iorapd" /vendor/bin/foreign\n    override\n',
                   b'service \\\n    iorapd /vendor/bin/foreign\n    override\n',
                   b'ser\\\nvice iorapd /vendor/bin/foreign\n    override\n')
        with patch.dict(gates.PROFILES, {path: row}):
            for body in foreign:
                with self.subTest(body=body):
                    with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service definition'):
                        gates.audit_start_commands({path: original, 'vendor/etc/init/override.rc': body})
            changed = original.replace(b'    start iorapd\n', b'')
            with self.assertRaisesRegex(RuntimeError, 'Unsupported init capability content'):
                gates.audit_start_commands({path: changed})

    def test_inline_comment_ending_in_backslash_cannot_mask_the_next_start(self):
        cases = (b'on boot\n    write /sys/x 1 # comment \\\n    start iorapd\n',
                 b'on boot\n    write /sys/x 1 # comment \\\r\n    start iorapd\r\n',
                 b'on boot\n    write /sys/x \\\n    1 # comment \\\n    start iorapd\n',
                 b'on boot\n    write /sys/x "literal#hash" \\\n    value\n')
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaisesRegex(RuntimeError, 'Ambiguous commented init continuation'):
                    gates.audit_start_commands({'vendor/etc/init/other.rc': body})
        with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service start path'):
            gates.audit_start_commands({'vendor/etc/init/other.rc':
                b'on boot\n    # whole-line comment \\\n    start iorapd\n'})

    def test_crlf_continuations_cannot_hide_starts_or_overriding_declarations(self):
        for body, message in (
                (b'on boot\r\n    start \\\r\n    iorapd\r\n', 'start path'),
                (b'on boot\r\n    st\\\r\nart iorapd\r\n', 'start path'),
                (b'service \\\r\n    iorapd /vendor/bin/foreign\r\n    override\r\n', 'definition')):
            with self.subTest(body=body):
                with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service ' + message):
                    gates.audit_start_commands({'vendor/etc/init/other.rc': body})

    def test_escaped_terminal_backslash_keeps_the_next_command_independent(self):
        for ending in (b'\n', b'\r\n'):
            body = b'on boot' + ending + b'    write /sys/x two-backslashes \\\\' + ending
            body += b'    start iorapd' + ending
            with self.subTest(ending=ending):
                with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service start path'):
                    gates.audit_start_commands({'vendor/etc/init/other.rc': body})
        for body in (b'on boot\n    st\\\rart iorapd\n',
                     b'on boot\n    start ior\\\rapd\n'):
            with self.subTest(body=body):
                with self.assertRaisesRegex(RuntimeError, 'Unsupported escaped carriage return in init'):
                    gates.audit_start_commands({'vendor/etc/init/other.rc': body})

    def test_dynamic_direct_service_targets_are_explicitly_unsupported(self):
        commands = ('start ${ro.boot.service}', 'restart ${ro.boot.service:-iorapd}',
                    'exec_start "${ro.boot.service}"', 'enable ${ro.boot.service}',
                    'ctl.start ${ro.boot.service}', 'ctl.restart ${ro.boot.service}',
                    'setprop ctl.start ${ro.boot.service}',
                    'setprop "ctl.restart" ${ro.boot.service}',
                    'onrestart start ${ro.boot.service}',
                    'start \\\r\n    ${ro.boot.service}',
                    'start io${ro.boot.service}', 'start ${ro.boot.service}apd',
                    'start i${ro.boot.first}${ro.boot.second}d')
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaisesRegex(RuntimeError, 'Unsupported init service target expansion'):
                    gates.audit_start_commands({'vendor/etc/init/other.rc':
                        ('on boot\n    ' + command + '\n').encode()})

    def test_dynamic_control_property_names_are_explicitly_unsupported(self):
        for command in ('setprop ${ro.boot.control} iorapd',
                        'setprop ctl.${ro.boot.control} iorapd',
                        'setprop ctl.st${ro.boot.control} iorapd',
                        'setprop ${ro.boot.control}start iorapd',
                        'onrestart setprop "${ro.boot.control}" iorapd',
                        'setprop \\\n    ${ro.boot.control} ${ro.boot.service}'):
            with self.subTest(command=command):
                with self.assertRaisesRegex(RuntimeError, 'Unsupported init control property expansion'):
                    gates.audit_start_commands({'vendor/etc/init/other.rc':
                        ('on boot\n    ' + command + '\n').encode()})
        gates.audit_start_commands({'vendor/etc/init/other.rc':
            b'on boot\n    setprop unrelated.value ${ro.boot.value}\n'})

    def test_dynamic_routes_with_provably_unrelated_literal_prefixes_or_suffixes_are_allowed(self):
        body = b'''on boot
    start llkd-${ro.debuggable:-0}
    restart llkd-${ro.debuggable}
    start io.${ro.boot.service}
    start ${ro.boot.service}route
    setprop ctl.start llkd-${ro.debuggable:-0}
    setprop sys.${ro.boot.profile} value
    setprop ${ro.boot.profile}.sys value
'''
        gates.audit_start_commands({'system/etc/init/any-name.rc': body})
        # Literal suffixes are tested against the catalog, never hard-coded.
        with patch.dict(gates.PROFILES, {'vendor/etc/init/custom.rc': {'service': 'customroute'}}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'Unsupported init service target expansion'):
                gates.audit_start_commands({'system/etc/init/any-name.rc':
                    b'on boot\n    start ${ro.boot.service}route\n'})

    def test_malformed_or_unsupported_expansions_refuse_even_with_unrelated_prefix(self):
        values = ('${}', '${ro.boot.service', '${ro.boot.service:bad}',
                  '${ro.boot.service:-${ro.boot.other}}', '$ro.boot.service', '$$')
        for value in values:
            for command, message in (('start llkd-' + value, 'service target'),
                                     ('setprop sys.' + value + ' value', 'control property')):
                with self.subTest(command=command):
                    with self.assertRaisesRegex(RuntimeError, 'Unsupported init ' + message + ' expansion syntax'):
                        gates.audit_start_commands({'system/etc/init/any-name.rc':
                            ('on boot\n    ' + command + '\n').encode()})

    def test_init_graph_ignores_unrelated_literal_quotes_but_rejects_guarded_bad_syntax(self):
        path = next(iter(gates.PROFILES))
        original, row = fixture(path)
        unrelated = b'''on boot
    write /sys/example/value literal"quote
    export EXAMPLE literal'quote
    setprop example.value literal"quote
    onrestart write /sys/example/value literal'quote
    start unrelated"quote
'''
        with patch.dict(gates.PROFILES, {path: row}):
            gates.audit_start_commands({path: original, 'vendor/etc/init/other.rc': unrelated})
            self.assertIn(b'literal"quote', unrelated)
            for command in ('start "iorapd', 'onrestart start "iorapd',
                            'setprop ctl.start "iorapd'):
                with self.subTest(command=command):
                    with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service start syntax'):
                        gates.audit_start_commands({'vendor/etc/init/other.rc': command.encode()})

    def test_real_retained_images_match_content_profiles_without_unpacking(self):
        from lp_image import read_lp, SECTOR
        tool = Path('/opt/homebrew/bin/dump.erofs')
        images = [REPO / 'work/os4-pad/images/system.img',
                  REPO / 'work/os4-official/images/system.img']
        images = [path for path in images if path.is_file()]
        if not images or not tool.is_file():
            self.skipTest('Local retained images and EROFS reader are optional fixtures.')
        seen = set()
        for image in images:
            base, parts = read_lp(image)
            part = next(row for row in parts if row['name'] == 'system')
            self.assertEqual(len(part['extents']), 1)
            length, kind, start, device = part['extents'][0]
            self.assertEqual((kind, device), (0, 0))
            for path, row in gates.PROFILES.items():
                result = subprocess.run([str(tool), '--offset=' + str(base + start * SECTOR),
                    '--cat', '--path=/' + path, str(image)], capture_output=True)
                if result.returncode or not result.stdout:
                    continue
                self.assertIn(gates.digest(result.stdout), (row['before'], row['after']))
                self.assertEqual(gates.digest(gates.patch(path, result.stdout)), row['after'])
                gates.audit_start_commands({path: result.stdout})
                seen.add(path)
        self.assertTrue(seen)


class CapabilityScriptTests(unittest.TestCase):
    def run_probe(self, *, millet=2, platform='art', soc=None, iorap=False, executable=True,
                  protocol=30, publish_fail=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dev, sysfs = root / 'dev', root / 'sys'
            dev.mkdir(); sysfs.mkdir()
            if iorap:
                (dev / 'iorap_dev').touch()
            if soc is not None:
                target = sysfs / 'devices/soc0/soc_id'
                target.parent.mkdir(parents=True)
                target.write_text(str(soc))
            events = root / 'events'
            programs = {
                'getprop': 'case "$1" in ro.millet.netlink) echo "$PROTOCOL";; ro.board.platform) echo "$PLATFORM";; *) exit 9;; esac',
                'setprop': 'echo "$1=$2" >> "$EVENTS"\nexit "$PUBLISH_RESULT"',
                'probe': '[ "$1" = "$PROTOCOL" ] || exit 3\nexit "$MILLET_RESULT"',
            }
            for name, body in programs.items():
                file = root / name
                file.write_text('#!/bin/sh\n' + body + '\n')
                file.chmod(0o755 if name != 'probe' or executable else 0o644)
            script = gates.SCRIPT_SOURCE.read_text().replace('/dev/iorap_dev', str(dev / 'iorap_dev'))
            script = script.replace('[ -r /dev ] && [ -x /dev ]', f'[ -r {dev} ] && [ -x {dev} ]')
            script = script.replace('/sys/devices/', str(sysfs / 'devices') + '/')
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'], EVENTS=str(events),
                PLATFORM=platform, PROTOCOL=str(protocol), MILLET_RESULT=str(millet),
                PUBLISH_RESULT='1' if publish_fail else '0')
            result = subprocess.run(['sh', '-s', '--', str(root / 'probe')], input=script,
                text=True, env=env, capture_output=True)
            values = dict(line.split('=', 1) for line in events.read_text().splitlines())
            return result, values

    def test_unsupported_kernel_is_successful_and_preserves_all_user_settings(self):
        result, values = self.run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, {'sys.hyperos_avd.cap.iorap': '0',
            'sys.hyperos_avd.cap.millet': '0', 'sys.hyperos_avd.cap.qti_display': '0'})
        self.assertNotIn('persist.', gates.SCRIPT_SOURCE.read_text())
        self.assertNotIn('ctl.stop', gates.SCRIPT_SOURCE.read_text())

    def test_supported_kernel_uses_declared_protocol_without_device_version_binding(self):
        for protocol in (30, 31):
            result, values = self.run_probe(millet=0, iorap=True, protocol=protocol)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(values['sys.hyperos_avd.cap.millet'], '1')
            self.assertEqual(values['sys.hyperos_avd.cap.iorap'], '1')

    def test_denied_or_missing_probe_is_unknown_and_does_not_enable_monitor(self):
        for millet, executable in ((3, True), (0, False)):
            result, values = self.run_probe(millet=millet, executable=executable)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(values['sys.hyperos_avd.cap.millet'], 'unknown')
            self.assertEqual(values['sys.hyperos_avd.cap.qti_display'], '0')

    def test_only_actual_oem_platform_and_soc_branches_allow_qti_script(self):
        for platform, soc in (('lahaina', 415), ('lahaina', 439), ('lahaina', 456), ('lito', 400)):
            result, values = self.run_probe(platform=platform, soc=soc)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(values['sys.hyperos_avd.cap.qti_display'], '1')
        for platform, soc in (('art', 415), ('lito', 415), ('lahaina', 400), ('lahaina', 999)):
            result, values = self.run_probe(platform=platform, soc=soc)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(values['sys.hyperos_avd.cap.qti_display'], '0')
        result, values = self.run_probe(platform='lahaina')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(values['sys.hyperos_avd.cap.qti_display'], 'unknown')

    def test_failed_property_publication_is_reported(self):
        result, _ = self.run_probe(publish_fail=True)
        self.assertEqual(result.returncode, 1)


if __name__ == '__main__':
    unittest.main()
