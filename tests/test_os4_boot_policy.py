"""Exercise the shared pre-init edit bundle without images, guests or compilers."""
import hashlib
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import dex2oat_cpu_policy as cpu
import os4_boot_policy as boot
import patch_init_capabilities as gates


class BootPolicyBundleTests(unittest.TestCase):
    def fixture(self, *, qti=True):
        graph, profiles = {}, {}
        for path, value in gates.PROFILES.items():
            if value['capability'] == 'qti_display' and not qti:
                continue
            row = dict(value)
            options = b'    class main\n    user system\n'
            if row['disabled']:
                options += b'    disabled\n'
            original = b'service ' + row['service'].encode() + b' /system_ext/bin/fixture\n'
            original += options + b'\n' + row['trigger']
            original += b'    start ' + row['service'].encode() + b'\n'
            original += b'\non boot\n    setprop unrelated.fixture 1\n'
            row['before'] = gates.digest(original)
            row['after'] = gates.digest(gates._transform(original, row))
            profiles[path], graph[path] = row, original
        graph['vendor/etc/init/vendor.fixture.rc'] = b'on boot\n    start unrelated-service\n'
        return graph, profiles

    def bundle(self, graph, profiles, folder, *, qti_script=b'verified fixture qti script\n',
               init=b'on boot\n    setprop unrelated.value 1\n', policy=b'; existing grants\n'):
        probe = folder / 'probe-fixture'
        probe.write_bytes(b'bounded native capability probe fixture')
        with patch.dict(gates.PROFILES, profiles, clear=True), \
                patch.object(gates, 'QTI_SCRIPT_SHA256', gates.digest(qti_script)), \
                patch.object(boot, 'kernel_probe', return_value=probe) as builder:
            edits, receipt = boot.image_replacements(graph, qti_script, init, policy, folder)
        builder.assert_called_once_with(folder / 'kernel-probe')
        return edits, receipt

    def test_complete_bundle_preserves_inputs_emits_narrow_helpers_and_verified_receipt(self):
        for qti in (False, True):
            with self.subTest(qti=qti), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                graph, profiles = self.fixture(qti=qti)
                preserved = dict(graph)
                edits, receipt = self.bundle(graph, profiles, folder)
                self.assertEqual(graph, preserved)
                self.assertEqual(set(receipt['targets']), set(profiles))
                for path, row in profiles.items():
                    self.assertEqual(edits[path][1:], (0o644, 'u:object_r:system_file:s0'))
                    self.assertEqual(hashlib.sha256(edits[path][0]).hexdigest(), row['after'])
                    self.assertEqual(receipt['targets'][path]['before'], row['before'])
                for path in (gates.SCRIPT_PATH, cpu.SYSTEM_TARGET.lstrip('/'), boot.PROBE_PATH):
                    self.assertEqual(edits[path][1:], (0o755, 'u:object_r:system_file:s0'))
                self.assertEqual(receipt['dex2oat'], cpu.receipt())
                self.assertEqual(receipt['qti_script_sha256'] is not None, qti)
                self.assertIn(b'    exec_start hyperos-dex2oat-cpu\n', edits[boot.INIT_PATH][0])
                self.assertIn(b'    start hyperos-init-capabilities\n', edits[boot.INIT_PATH][0])

    def test_unknown_start_route_or_known_file_content_refuses_before_probe_build(self):
        graph, profiles = self.fixture()
        cases = [dict(graph, **{'vendor/etc/init/extra.rc': b'on boot\n    exec_start millet_monitor\n'}),
                 dict(graph, **{'vendor/etc/init/extra.rc': b'on boot\n    start ${ro.boot.service}\n'}),
                 dict(graph, **{'vendor/etc/init/extra.rc': b'on boot\n    setprop ${ro.boot.control} millet_monitor\n'}),
                 dict(graph, **{next(iter(profiles)): graph[next(iter(profiles))] + b'# changed\n'})]
        for candidate in cases:
            with self.subTest(candidate=candidate), patch.dict(gates.PROFILES, profiles, clear=True), \
                    patch.object(boot, 'kernel_probe') as builder:
                with self.assertRaises(RuntimeError):
                    boot.image_replacements(candidate, b'unknown', b'', b'', Path('/unused'))
                builder.assert_not_called()

    def test_missing_required_service_and_unknown_qti_script_refuse_before_probe_build(self):
        graph, profiles = self.fixture()
        cases = [({path: data for path, data in graph.items() if path != next(iter(profiles))}, b'unknown'),
                 (graph, b'unknown')]
        for candidate, script in cases:
            with patch.dict(gates.PROFILES, profiles, clear=True), patch.object(boot, 'kernel_probe') as builder:
                with self.assertRaises(RuntimeError):
                    boot.image_replacements(candidate, script, b'', b'', Path('/unused'))
                builder.assert_not_called()

    def test_second_bundle_is_idempotent_for_already_patched_rc_init_and_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            graph, profiles = self.fixture()
            edits, receipt = self.bundle(graph, profiles, folder)
            updated = {path: edits[path][0] if path in edits else data for path, data in graph.items()}
            again, second = self.bundle(updated, profiles, folder, init=edits[boot.INIT_PATH][0],
                                        policy=edits[boot.POLICY_PATH][0])
            self.assertEqual(edits, again)
            self.assertEqual(receipt, second)

    def test_conflicting_helper_service_definition_is_never_silently_appended(self):
        for name in (b'hyperos-init-capabilities', b'hyperos-dex2oat-cpu'):
            with self.subTest(name=name):
                for whitespace in (b' ', b'\t', b'   '):
                    unknown = b'service' + whitespace + name + whitespace + b'/system/bin/unknown\n    disabled\n'
                    with self.assertRaisesRegex(RuntimeError, 'Unexpected existing boot policy service'):
                        boot.append_init(unknown)
                    with self.assertRaisesRegex(RuntimeError, 'Unexpected existing boot policy service'):
                        boot.append_init(boot.append_init(b'') + unknown)

    def test_complete_graph_rejects_foreign_duplicate_helpers_before_native_builder(self):
        graph, profiles = self.fixture(qti=False)
        canonical = boot.append_init(b'')
        for body in (b'service\thyperos-init-capabilities\t/vendor/bin/foreign\n',
                     b'service hyperos-dex2oat-cpu /vendor/bin/foreign\n',
                     b'service \\\n    hyperos-init-capabilities /vendor/bin/foreign\n',
                     b'"service" "hyperos-dex2oat-cpu" /vendor/bin/foreign\n', canonical):
            for also_canonical in (False, True):
                candidate = dict(graph, **{'vendor/etc/init/foreign.rc': body})
                if also_canonical:
                    candidate[boot.INIT_PATH] = canonical
                with self.subTest(body=body, also_canonical=also_canonical), \
                        patch.dict(gates.PROFILES, profiles, clear=True), patch.object(boot, 'kernel_probe') as builder:
                    with self.assertRaisesRegex(RuntimeError, 'Unexpected existing boot policy service'):
                        boot.image_replacements(candidate, None, canonical if also_canonical else b'', b'', Path('/unused'))
                    builder.assert_not_called()

    def test_effective_init_mismatch_refuses_before_native_builder(self):
        graph, profiles = self.fixture(qti=False)
        graph[boot.INIT_PATH] = b'on boot\n    setprop known 1\n'
        with patch.dict(gates.PROFILES, profiles, clear=True), patch.object(boot, 'kernel_probe') as builder:
            with self.assertRaisesRegex(RuntimeError, 'differs from its effective graph'):
                boot.image_replacements(graph, None, b'on boot\n    setprop unknown 1\n', b'', Path('/unused'))
            builder.assert_not_called()

    def test_policy_preserves_existing_grants_without_importing_runtime_stop_or_binder_permissions(self):
        original = b'; preserved\n(allow original own_type (file (read)))\n'
        policy = boot.append_policy(original)
        self.assertTrue(policy.startswith(original))
        self.assertEqual(boot.append_policy(policy), policy)
        added = policy[len(original):]
        for permission in (b'permissive', b'dontaudit', b'ctl_stop_prop', b'binder', b'service_manager',
                           b'process (kill)', b'sysfs (file (write'):
            self.assertNotIn(permission, added)
        for line in cpu.SEPOLICY.splitlines():
            self.assertEqual(policy.splitlines().count(line), 1)


class InitGraphReaderTests(unittest.TestCase):
    def test_subtree_overlays_replacements_removals_and_qti_use_final_image_precedence(self):
        first, overlay = Mock(), Mock()
        regular = {'mode': stat.S_IFREG | 0o644, 'nid': 3}
        first.walk.return_value = [('etc/init/known.rc', regular), ('etc/init/removed.rc', regular)]
        overlay.walk.side_effect = [[('system_ext', {'mode': stat.S_IFDIR | 0o755, 'nid': 100}),
                                     ('system_ext/etc/init/known.rc', regular),
                                     ('system_ext/etc/init/added.rc', regular)],
                                    [('etc/init/known.rc', regular), ('etc/init/added.rc', regular)]]
        replacements = {'system_ext/etc/init/known.rc': (b'explicit edit', 0o644, 'label'),
                        gates.QTI_SCRIPT: (b'final QTI script', 0o755, 'label')}
        with patch.object(boot, 'Reader', side_effect=[first, overlay]), \
                patch.object(boot, 'erofs', return_value=b'added') as read:
            graph = boot.init_files([('system_ext', Path('/first')), ('system_ext:system_ext', Path('/mi_ext'))],
                                    replacements, ('system_ext/etc/init/removed.rc',))
        self.assertEqual(graph, {'system_ext/etc/init/known.rc': b'explicit edit',
                                'system_ext/etc/init/added.rc': b'added', gates.QTI_SCRIPT: b'final QTI script'})
        self.assertEqual(read.call_args.args, (Path('/mi_ext'), '/system_ext/etc/init/added.rc'))
        overlay.walk.assert_any_call(100)
        first.close.assert_called_once()
        overlay.close.assert_called_once()

    def test_rc_graph_audits_all_image_rc_and_final_partition_precedence(self):
        first, second = Mock(), Mock()
        first.walk.return_value = [('etc/init/known.rc', {'mode': stat.S_IFREG | 0o644}),
                                   ('etc/init/service.sh', {'mode': stat.S_IFREG | 0o755}),
                                   ('etc/not-init/no.rc', {'mode': stat.S_IFREG | 0o644})]
        second.walk.return_value = [('etc/init/known.rc', {'mode': stat.S_IFREG | 0o644})]
        with patch.object(boot, 'Reader', side_effect=[first, second]), \
                patch.object(boot, 'erofs', return_value=b'final') as read:
            graph = boot.init_files([('system_ext', Path('/first')), ('system_ext', Path('/second'))])
        self.assertEqual(graph, {'system_ext/etc/init/known.rc': b'final',
                                'system_ext/etc/not-init/no.rc': b'final'})
        self.assertEqual(read.call_count, 2)
        read.assert_any_call(Path('/second'), '/etc/init/known.rc')
        read.assert_any_call(Path('/first'), '/etc/not-init/no.rc')
        first.close.assert_called_once()
        second.close.assert_called_once()

    def alias_reader(self, paths, links=None):
        reader = Mock()
        entries = [(path, {'mode': mode, 'nid': index}) for index, (path, mode) in enumerate(paths)]
        reader.walk.return_value = entries
        reader.flat.side_effect = lambda inode: (links or {})[entries[inode['nid']][0]].encode()
        return reader

    def test_valid_file_alias_reads_final_overlaid_target_without_host_path_resolution(self):
        regular, alias = stat.S_IFREG | 0o644, stat.S_IFLNK | 0o777
        first = self.alias_reader([('system/etc/init/alias.rc', alias), ('system/etc/shared/target.rc', regular)],
                                  {'system/etc/init/alias.rc': '../shared/target.rc'})
        second = self.alias_reader([('etc/shared/target.rc', regular)])
        with patch.object(boot, 'Reader', side_effect=[first, second]), \
                patch.object(boot, 'erofs', return_value=b'final target') as read:
            graph = boot.init_files([('', Path('/base')), ('system', Path('/overlay'))])
        self.assertEqual(graph, {'system/etc/init/alias.rc': b'final target',
                                'system/etc/shared/target.rc': b'final target'})
        read.assert_called_once_with(Path('/overlay'), '/etc/shared/target.rc')
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_file_alias_can_follow_directory_alias_and_explicit_final_replacement(self):
        alias, directory, regular = stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o755, stat.S_IFREG | 0o644
        reader = self.alias_reader([('system/etc/init/alias.rc', alias), ('system/shared', alias),
                                    ('product/shared', directory), ('product/shared/target.rc', regular)],
                                   {'system/etc/init/alias.rc': '/system/shared/target.rc',
                                    'system/shared': '/product/shared'})
        replacement = {'product/shared/target.rc': (b'explicit final target', 0o644, 'preserved-label')}
        with patch.object(boot, 'Reader', return_value=reader), patch.object(boot, 'erofs') as read:
            graph = boot.init_files([('', Path('/image'))], replacement)
        self.assertEqual(graph, {'system/etc/init/alias.rc': b'explicit final target',
                                'product/shared/target.rc': b'explicit final target'})
        read.assert_not_called()
        reader.close.assert_called_once()

    def test_unsafe_alias_cycle_escape_missing_or_nonregular_target_is_refused(self):
        alias, directory = stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o755
        alias_path = 'system/etc/init/alias.rc'
        cases = [([(alias_path, alias)], {alias_path: 'alias.rc'}),
                 ([(alias_path, alias), ('system/etc/init/other.rc', alias)],
                  {alias_path: 'other.rc', 'system/etc/init/other.rc': 'alias.rc'}),
                 ([(alias_path, alias)], {alias_path: '../../../../outside.rc'}),
                 ([(alias_path, alias)], {alias_path: '/../../outside.rc'}),
                 ([(alias_path, alias)], {alias_path: '../missing.rc'}),
                 ([(alias_path, alias), ('system/target', directory)], {alias_path: '/system/target'}),
                 ([(alias_path, alias)], {alias_path: ''}),
                 ([(alias_path, alias)], {alias_path: '/system/invalid\0target.rc'})]
        for paths, links in cases:
            with self.subTest(links=links):
                reader = self.alias_reader(paths, links)
                with patch.object(boot, 'Reader', return_value=reader), patch.object(boot, 'erofs') as read:
                    with self.assertRaises(RuntimeError):
                        boot.init_files([('', Path('/image'))])
                    read.assert_not_called()
                reader.close.assert_called_once()

    def test_shadowed_or_removed_bad_alias_is_not_resolved_from_a_stale_layer(self):
        alias, regular = stat.S_IFLNK | 0o777, stat.S_IFREG | 0o644
        first = self.alias_reader([('system/etc/init/known.rc', alias), ('system/etc/init/removed.rc', alias)],
                                  {'system/etc/init/known.rc': '/missing/target.rc',
                                   'system/etc/init/removed.rc': '/missing/target.rc'})
        second = self.alias_reader([('etc/init/known.rc', regular)])
        with patch.object(boot, 'Reader', side_effect=[first, second]), patch.object(boot, 'erofs', return_value=b'final') as read:
            graph = boot.init_files([('', Path('/base')), ('system', Path('/overlay'))],
                                    removals=('system/etc/init/removed.rc',))
        self.assertEqual(graph, {'system/etc/init/known.rc': b'final'})
        read.assert_called_once_with(Path('/overlay'), '/etc/init/known.rc')

    def test_reader_is_closed_after_rc_read_failure(self):
        reader = Mock()
        reader.walk.return_value = [('system/etc/init/init.rc', {'mode': stat.S_IFREG | 0o644})]
        with patch.object(boot, 'Reader', return_value=reader), \
                patch.object(boot, 'erofs', side_effect=RuntimeError('unreadable rc')):
            with self.assertRaisesRegex(RuntimeError, 'unreadable rc'):
                boot.init_files([('', Path('/image'))])
        reader.close.assert_called_once()

    def test_directory_alias_and_recursive_imports_cannot_hide_non_rc_start_paths(self):
        regular, alias, directory = stat.S_IFREG | 0o644, stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o755
        reader = self.alias_reader([('system/etc/init/main.rc', regular), ('system/etc/init/hw', alias),
                                    ('product/routes', directory), ('product/routes/hidden.conf', regular),
                                    ('product/routes/deeper.settings', regular)],
                                   {'system/etc/init/hw': '/product/routes'})
        bodies = {'/system/etc/init/main.rc': b'import /system/etc/init/hw/hidden.conf\n',
                  '/product/routes/hidden.conf': b'import /product/routes/deeper.settings\n',
                  '/product/routes/deeper.settings': b'on boot\n    start iorapd\n'}
        with patch.object(boot, 'Reader', return_value=reader), \
                patch.object(boot, 'erofs', side_effect=lambda image, path: bodies[path]) as read:
            graph = boot.init_files([('', Path('/image'))])
        self.assertEqual(graph['system/etc/init/hw/hidden.conf'], bodies['/product/routes/hidden.conf'])
        self.assertEqual(graph['product/routes/deeper.settings'], bodies['/product/routes/deeper.settings'])
        read.assert_any_call(Path('/image'), '/product/routes/hidden.conf')
        with patch.object(boot, 'kernel_probe') as builder:
            with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service start path'):
                boot.image_replacements(graph, None, b'', b'', Path('/unused'))
            builder.assert_not_called()

    def test_continued_imports_refuse_before_a_non_rc_leaf_can_be_hidden(self):
        regular = stat.S_IFREG | 0o644
        main = 'system/etc/init/main.rc'
        hidden = 'product/routes/hidden.conf'
        cases = (b'import \\\n    /product/routes/hidden.conf\n',
                 b'import /product/routes/hid\\\nden.conf\n',
                 b'im\\\nport /product/routes/hidden.conf\n',
                 b'"import" \\\n    /product/routes/hidden.conf\n',
                 b'import /product/routes/${ro.hardware}.\\\nconf\n',
                 b'import \\\r\n    /product/routes/hidden.conf\r\n',
                 b'im\\\r\nport /product/routes/hidden.conf\r\n')
        for body in cases:
            with self.subTest(body=body):
                reader = self.alias_reader([(main, regular), (hidden, regular)])
                with patch.object(boot, 'Reader', return_value=reader), \
                        patch.object(boot, 'erofs', return_value=body) as read:
                    with self.assertRaisesRegex(RuntimeError, 'Unsupported continued init import'):
                        boot.init_files([('', Path('/image'))])
                read.assert_called_once_with(Path('/image'), '/' + main)
                reader.close.assert_called_once()

    def test_comment_ending_in_backslash_cannot_mask_a_non_rc_import(self):
        regular = stat.S_IFREG | 0o644
        main, hidden = 'system/etc/init/main.rc', 'product/routes/hidden.conf'
        for ending in (b'\n', b'\r\n'):
            body = b'on boot' + ending + b'    write /sys/x 1 # comment \\' + ending
            body += ('import /' + hidden).encode() + ending
            reader = self.alias_reader([(main, regular), (hidden, regular)])
            with self.subTest(ending=ending), patch.object(boot, 'Reader', return_value=reader), \
                    patch.object(boot, 'erofs', return_value=body) as read:
                with self.assertRaisesRegex(RuntimeError, 'Ambiguous commented init continuation'):
                    boot.init_files([('', Path('/image'))])
            read.assert_called_once_with(Path('/image'), '/' + main)
            reader.close.assert_called_once()

    def test_escaped_terminal_backslash_keeps_a_non_rc_import_independent(self):
        regular = stat.S_IFREG | 0o644
        main, hidden = 'system/etc/init/main.rc', 'product/routes/hidden.conf'
        for ending in (b'\n', b'\r\n'):
            bodies = {'/' + main: b'on boot' + ending + b'    write /sys/x literal \\\\' + ending
                        + ('import /' + hidden).encode() + ending,
                      '/' + hidden: b'on boot\n    start iorapd\n'}
            reader = self.alias_reader([(main, regular), (hidden, regular)])
            with self.subTest(ending=ending), patch.object(boot, 'Reader', return_value=reader), \
                    patch.object(boot, 'erofs', side_effect=lambda image, path: bodies[path]) as read:
                graph = boot.init_files([('', Path('/image'))])
            self.assertEqual(graph[hidden], bodies['/' + hidden])
            self.assertEqual(read.call_count, 2)
            with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service start path'):
                gates.audit_start_commands(graph)

    def test_recursive_continued_import_is_refused_after_literal_non_rc_import(self):
        regular = stat.S_IFREG | 0o644
        main, nested = 'system/etc/init/main.rc', 'product/routes/nested.conf'
        reader = self.alias_reader([(main, regular), (nested, regular)])
        bodies = {'/' + main: ('import /' + nested + '\n').encode(),
                  '/' + nested: b'import \\\n    /product/routes/hidden.conf\n'}
        with patch.object(boot, 'Reader', return_value=reader), \
                patch.object(boot, 'erofs', side_effect=lambda image, path: bodies[path]) as read:
            with self.assertRaisesRegex(RuntimeError, 'Unsupported continued init import'):
                boot.init_files([('', Path('/image'))])
        self.assertEqual(read.call_count, 2)
        reader.close.assert_called_once()

    def test_dynamic_imports_audit_all_possible_final_non_rc_targets_without_guessed_properties(self):
        regular = stat.S_IFREG | 0o644
        paths = ['system/etc/init/main.rc', 'product/routes/init.ranchu.conf',
                 'product/routes/init.hardware.conf', 'product/routes/unrelated.bin']
        reader = self.alias_reader([(path, regular) for path in paths])
        bodies = {'/' + paths[0]: b'import /product/routes/init.${ro.boot.hardware:-ranchu}.conf\n',
                  '/' + paths[1]: b'on boot\n    start unrelated-service\n',
                  '/' + paths[2]: b'on boot\n    start millet_monitor\n'}
        with patch.object(boot, 'Reader', return_value=reader), \
                patch.object(boot, 'erofs', side_effect=lambda image, path: bodies[path]) as read:
            graph = boot.init_files([('', Path('/image'))])
        self.assertEqual(set(graph), set(paths[:3]))
        self.assertEqual(read.call_count, 3)
        with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service start path'):
            gates.audit_start_commands(graph)

    def test_optional_missing_imports_and_missing_leaf_after_directory_alias_are_skipped(self):
        regular, alias = stat.S_IFREG | 0o644, stat.S_IFLNK | 0o777
        reader = self.alias_reader([('system/etc/init/main.rc', regular), ('odm', alias)],
                                   {'odm': '/vendor/odm'})
        main = (b'import /init.ramdisk-missing.rc\nimport /odm/etc/ueventd.rc\n'
                b'import /vendor/etc/init/hw/init.${ro.hardware}.rc\n')
        with patch.object(boot, 'Reader', return_value=reader), \
                patch.object(boot, 'erofs', return_value=main) as read:
            graph = boot.init_files([('', Path('/image'))])
        self.assertEqual(graph, {'system/etc/init/main.rc': main})
        read.assert_called_once_with(Path('/image'), '/system/etc/init/main.rc')

    def test_dynamic_non_rc_import_resolves_its_static_directory_alias_before_matching(self):
        regular, alias, directory = stat.S_IFREG | 0o644, stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o755
        reader = self.alias_reader([('system/etc/init/main.rc', regular), ('system/etc/init/hw', alias),
                                    ('product/routes', directory), ('product/routes/init.ranchu.conf', regular)],
                                   {'system/etc/init/hw': '/product/routes'})
        bodies = {'/system/etc/init/main.rc': b'import /system/etc/init/hw/init.${ro.hardware}.conf\n',
                  '/product/routes/init.ranchu.conf': b'on boot\n    start iorapd\n'}
        with patch.object(boot, 'Reader', return_value=reader), \
                patch.object(boot, 'erofs', side_effect=lambda image, path: bodies[path]):
            graph = boot.init_files([('', Path('/image'))])
        self.assertEqual(set(graph), {'system/etc/init/main.rc', 'product/routes/init.ranchu.conf'})
        with self.assertRaisesRegex(RuntimeError, 'Unreviewed init service start path'):
            gates.audit_start_commands(graph)

    def test_invalid_import_syntax_escape_or_alias_cycle_is_refused(self):
        regular, alias = stat.S_IFREG | 0o644, stat.S_IFLNK | 0o777
        for command in (b'import\n', b'import /system/first.rc /system/second.rc\n',
                        b'import "/system/missing.rc\n', b'import /../../outside.conf\n',
                        b'import /system/${unknown\n', b'import /system/invalid\0name.conf\n',
                        b'import /system/${ro.hardware}/hidden.conf\n',
                        b'import /system/loop/hidden.conf\n'):
            with self.subTest(command=command):
                reader = self.alias_reader([('system/etc/init/main.rc', regular), ('system/loop', alias)],
                                           {'system/loop': '/system/loop'})
                with patch.object(boot, 'Reader', return_value=reader), \
                        patch.object(boot, 'erofs', return_value=command):
                    with self.assertRaises(RuntimeError):
                        boot.init_files([('', Path('/image'))])
                reader.close.assert_called_once()

    def test_helper_definition_file_alias_is_rejected_as_duplicate_effective_owner(self):
        regular, alias = stat.S_IFREG | 0o644, stat.S_IFLNK | 0o777
        alias_path = 'system/etc/init/helper-alias.rc'
        reader = self.alias_reader([(boot.INIT_PATH, regular), (alias_path, alias)],
                                   {alias_path: '/' + boot.INIT_PATH})
        canonical = boot.append_init(b'')
        with patch.object(boot, 'Reader', return_value=reader), \
                patch.object(boot, 'erofs', return_value=canonical) as read:
            graph = boot.init_files([('', Path('/image'))])
        self.assertEqual(graph, {boot.INIT_PATH: canonical, alias_path: canonical})
        read.assert_called_once()
        with patch.object(boot, 'kernel_probe') as builder:
            with self.assertRaisesRegex(RuntimeError, 'Unexpected existing boot policy service'):
                boot.image_replacements(graph, None, canonical, b'', Path('/unused'))
            builder.assert_not_called()


if __name__ == '__main__':
    unittest.main()
