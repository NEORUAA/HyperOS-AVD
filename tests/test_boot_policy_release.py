"""Verify claimed shared boot policies against actual bytes and the init graph."""
from contextlib import ExitStack
import copy
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import apply_boot_service_fix as legacy
import dex2oat_cpu_policy as cpu
import os4_boot_policy as boot
import package_release as release
import patch_boot_services as services
import patch_init_capabilities as gates


def digest(data):
    return hashlib.sha256(data).hexdigest()


class BootPolicyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        rows, self.fixed = {}, {}
        for path, row in gates.PROFILES.items():
            original = ('service ' + row['service'] + ' /system/bin/example\n' +
                        ('    disabled\n' if row['disabled'] else '') +
                        '    class main\n').encode() + row['trigger'] + (
                        '    start ' + row['service'] + '\n').encode()
            fixed = gates._transform(original, row)
            rows[path] = {**row, 'before': digest(original), 'after': digest(fixed)}
            self.fixed[path] = fixed
        self.stack.enter_context(patch.dict(gates.PROFILES, rows, clear=True))
        self.probe = b'small verified probe fixture\n'
        self.qti = b'audited QTI script fixture\n'
        self.stack.enter_context(patch.object(services, 'PROBE_SHA256', digest(self.probe)))
        self.stack.enter_context(patch.object(gates, 'QTI_SCRIPT_SHA256', digest(self.qti)))

    def fixture(self, qti=False):
        targets = list(gates.PROFILES) if qti else list(gates.PROFILES)[:2]
        marker = {'schema': 1,
                  'targets': {path: {'before': gates.PROFILES[path]['before'],
                                     'after': gates.PROFILES[path]['after'],
                                     'capability': gates.PROFILES[path]['capability']}
                              for path in targets},
                  'script': {'path': gates.SCRIPT_PATH,
                             'sha256': digest(gates.SCRIPT_SOURCE.read_bytes())},
                  'qti_script_sha256': digest(self.qti) if qti else None,
                  'dex2oat': cpu.receipt()}
        init = b'on boot\n    setprop unrelated keep\n' + gates.BOOT_INIT + cpu.BOOT_INIT
        graph = {path: self.fixed[path] for path in targets}
        graph[boot.INIT_PATH] = init
        files = {'/' + path: data for path, data in graph.items()}
        files['/' + gates.SCRIPT_PATH] = gates.SCRIPT_SOURCE.read_bytes()
        files[cpu.SYSTEM_TARGET] = cpu.policy_script()
        files['/' + boot.PROBE_PATH] = self.probe
        files['/' + boot.POLICY_PATH] = boot.HELPER_POLICY + cpu.SEPOLICY
        if qti:
            files['/' + gates.QTI_SCRIPT] = self.qti
        return marker, files, graph

    def test_absent_marker_does_not_reinterpret_old_releases(self):
        read = Mock()
        self.assertEqual(release.verify_boot_policy({}, read), 0)
        read.assert_not_called()

    def test_phone_and_pad_verify_actual_sources_and_keep_the_receipt(self):
        for qti, count in ((False, 7), (True, 9)):
            with self.subTest(qti=qti):
                marker, files, graph = self.fixture(qti)
                self.assertEqual(release.verify_boot_policy({'boot_policy': marker}, files.__getitem__, graph), count)
                self.assertEqual(release.validate_boot_policy_receipt(marker), marker)
        self.assertIn('boot_policy', release.BUILD_KEYS)

    def test_unknown_or_incomplete_receipts_are_rejected_before_reading(self):
        marker, _, _ = self.fixture()
        values = [None, [], {}, {**marker, 'schema': 999}, {**marker, 'probe_sha256': 'unknown'}]
        for key in marker:
            value = copy.deepcopy(marker)
            del value[key]
            values.append(value)
        value = copy.deepcopy(marker)
        value['targets'].pop(next(iter(value['targets'])))
        values.append(value)
        for value in values:
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'boot policy'):
                release.validate_boot_policy_receipt(value)

    def test_every_claimed_file_must_match_its_actual_checksum(self):
        marker, files, graph = self.fixture(True)
        for path in ('/' + gates.SCRIPT_PATH, cpu.SYSTEM_TARGET, '/' + boot.PROBE_PATH,
                     '/' + gates.QTI_SCRIPT, *('/' + item for item in marker['targets'])):
            with self.subTest(path=path):
                changed = {**files, path: files[path] + b'changed'}
                with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                    release.verify_boot_policy({'boot_policy': marker}, changed.__getitem__, graph)

    def test_missing_extra_or_alternate_helper_definitions_cannot_pass(self):
        marker, files, graph = self.fixture()
        path = '/' + boot.INIT_PATH
        for change in (files[path].replace(gates.BOOT_INIT, b''),
                       files[path] + cpu.BOOT_INIT,
                       files[path] + b'\nservice hyperos-init-capabilities /system/bin/another\n'):
            with self.subTest(change=change):
                changed_graph = {**graph, boot.INIT_PATH: change}
                with self.assertRaisesRegex(RuntimeError, 'Missing or duplicate'):
                    release.verify_boot_policy({'boot_policy': marker},
                                               {**files, path: change}.__getitem__, changed_graph)
        with self.assertRaisesRegex(RuntimeError, 'Missing or duplicate'):
            release.verify_boot_policy({'boot_policy': marker}, files.__getitem__,
                {**graph, 'vendor/etc/init/alias.rc': cpu.BOOT_INIT})
        for declaration in (b'service \\\n    hyperos-dex2oat-cpu /system/bin/foreign\n',
                            b'"service" "hyperos-init-capabilities" /system/bin/foreign\n'):
            with self.subTest(declaration=declaration):
                with self.assertRaisesRegex(RuntimeError, 'Missing or duplicate'):
                    release.verify_boot_policy({'boot_policy': marker}, files.__getitem__,
                        {**graph, 'vendor/routes/hidden-helper': declaration})

    def test_complete_graph_cannot_omit_qti_or_add_unguarded_start_routes(self):
        marker, files, graph = self.fixture(True)
        missing = copy.deepcopy(marker)
        del missing['targets']['product/etc/init/init.qti.display.rc']
        missing['qti_script_sha256'] = None
        with self.assertRaisesRegex(RuntimeError, 'service targets'):
            release.verify_boot_policy({'boot_policy': missing}, files.__getitem__, graph)
        with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service start'):
            release.verify_boot_policy({'boot_policy': marker}, files.__getitem__,
                {**graph, 'vendor/etc/init/extra.rc': b'on boot\n    restart iorapd\n'})
        with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service start'):
            release.verify_boot_policy({'boot_policy': marker}, files.__getitem__,
                {**graph, 'vendor/etc/imported-starts': b'on boot\n    start iorapd\n'})
        with self.assertRaisesRegex(RuntimeError, 'Missing or duplicate'):
            release.verify_boot_policy({'boot_policy': marker}, files.__getitem__,
                {**graph, 'vendor/etc/imported-helper': cpu.BOOT_INIT})

    def test_enforcing_rules_and_effective_init_identity_are_required(self):
        marker, files, graph = self.fixture()
        policy = '/' + boot.POLICY_PATH
        with self.assertRaisesRegex(RuntimeError, 'enforcing rule'):
            release.verify_boot_policy({'boot_policy': marker},
                {**files, policy: files[policy].replace(cpu.SEPOLICY.splitlines()[0] + b'\n', b'')}.__getitem__, graph)
        with self.assertRaisesRegex(RuntimeError, 'effective graph'):
            release.verify_boot_policy({'boot_policy': marker}, files.__getitem__,
                                       {**graph, boot.INIT_PATH: b'different init'})

    def test_retained_phone_helper_requires_the_new_policy_without_old_opt_out(self):
        marker, files, graph = self.fixture()
        init = files['/' + boot.INIT_PATH] + services.BOOT_INIT
        files['/' + boot.INIT_PATH] = init
        graph[boot.INIT_PATH] = init
        files['/system/etc/hyperos-kernel-services.sh'] = legacy.PERF_SCRIPT.read_bytes()
        release.verify_boot_policy({'boot_policy': marker}, files.__getitem__, graph)
        files['/system/etc/hyperos-kernel-services.sh'] = b'old property mutator'
        with self.assertRaisesRegex(RuntimeError, 'conflicts with shared boot policy'):
            release.verify_boot_policy({'boot_policy': marker}, files.__getitem__, graph)

    def test_foreign_folded_legacy_helper_is_rejected_even_without_direct_starts(self):
        marker, files, graph = self.fixture()
        for declaration in (b'service \\\n    hyperos-kernel-services /system/bin/foreign\n',
                            b'"service" "hyperos-kernel-services" /system/bin/foreign\n'):
            with self.subTest(declaration=declaration):
                with self.assertRaisesRegex(RuntimeError, 'Unexpected legacy helper'):
                    release.verify_boot_policy({'boot_policy': marker}, files.__getitem__,
                        {**graph, 'vendor/routes/hidden-helper': declaration})


class LegacyBootServiceReleaseTests(unittest.TestCase):
    def test_published_receipt_accepts_only_known_helpers_and_new_policy_requires_current(self):
        files = {path: ('fixture-' + name).encode() for name, (path, _, _) in services.TARGETS.items()}
        after = {name: digest(files[path]) for name, (path, _, _) in services.TARGETS.items()}
        probe = b'pinned probe fixture'
        with patch.object(services, 'AFTER', after), patch.object(services, 'PROBE_SHA256', digest(probe)):
            marker = {'schema': 1, 'firmware': 'OS4.0.18.0.XFRCNXM',
                      'targets': {name: {'path': path, 'before': before, 'after': after[name]}
                                  for name, (path, before, _) in services.TARGETS.items()},
                      'probe_sha256': digest(probe)}
            files['/system/bin/hyperos_kernel_probe'] = probe
            files['/system_ext/etc/init/init.hyperos_avd.rc'] = services.BOOT_INIT
            files['/system_ext/etc/selinux/system_ext_sepolicy.cil'] = services.BOOT_SEPOLICY
            current = legacy.PERF_SCRIPT.read_bytes()
            block = b'''if [ ! -e /dev/iorap_dev ]; then
    # Use the service's own documented opt-out; retain data and other preloaders.
    setprop persist.sys.stability.PrereadEnable false
    setprop ctl.stop iorapd
fi
'''
            previous = current.replace(b'PROBE=${1:-', block + b'PROBE=${1:-', 1)
            self.assertEqual(digest(previous), legacy.LEGACY_KERNEL_SCRIPT_SHA256)
            build = {'hyperos': '4.0.18.0.XFRCNXM', 'boot_service_fix': marker}
            for helper in (previous, current):
                files['/system/etc/hyperos-kernel-services.sh'] = helper
                release.verify_boot_services(build, files.__getitem__)
            files['/system/etc/hyperos-kernel-services.sh'] = previous
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                release.verify_boot_services({**build, 'boot_policy': {'current': True}}, files.__getitem__)
            files['/system/etc/hyperos-kernel-services.sh'] = current
            release.verify_boot_services({**build, 'boot_policy': {'current': True}}, files.__getitem__)
            files['/system/etc/hyperos-kernel-services.sh'] = b'unknown mutation'
            with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                release.verify_boot_services(build, files.__getitem__)


if __name__ == '__main__':
    unittest.main()
