"""Exercise the actual portable policy against isolated property/topology mocks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import dex2oat_cpu_policy as policy


class CpuPolicyTests(unittest.TestCase):
    def run_policy(self, online='0-3', possible='0-3', conf='4', properties=None,
                   hardware='ranchu', version='OS4.0', fail_property='', race_default=False):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            cpu = folder / 'cpu'; cpu.mkdir()
            (cpu / 'online').write_text(online)
            (cpu / 'possible').write_text(possible)
            original = {'ro.boot.hardware': hardware, 'ro.mi.os.version.name': version}
            original.update(properties or {})
            state = folder / 'properties.json'; state.write_text(json.dumps(original))
            events = folder / 'events'; events.write_text('')
            for name, body in {
                'getprop': '''import json, os, sys
p = os.environ['CPU_TEST_STATE']; values = json.load(open(p))
if os.environ.get('CPU_TEST_RACE') == '1' and sys.argv[1] == 'dalvik.vm.default-dex2oat-cpu-set':
    counter = p + '.reads'
    n = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, 'w') as output: output.write(str(n + 1))
    if n == 1:
        values[sys.argv[1]] = '1'
        with open(p, 'w') as output: json.dump(values, output)
print(values.get(sys.argv[1], ''))''',
                'setprop': '''import json, os, sys
if sys.argv[1] == os.environ.get('CPU_TEST_FAIL'): sys.exit(1)
p = os.environ['CPU_TEST_STATE']; values = json.load(open(p)); values[sys.argv[1]] = sys.argv[2]
with open(p, 'w') as output: json.dump(values, output)
with open(os.environ['CPU_TEST_EVENTS'], 'a') as output: output.write(sys.argv[1] + '=' + sys.argv[2] + '\\n')''',
                'getconf': '''import os
print(os.environ['CPU_TEST_CONF'])''',
            }.items():
                path = folder / name
                path.write_text('#!' + sys.executable + '\n' + body + '\n')
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(folder) + ':' + os.environ['PATH'],
                       DEX2OAT_CPU_ROOT=str(cpu), CPU_TEST_STATE=str(state),
                       CPU_TEST_EVENTS=str(events), CPU_TEST_CONF=conf,
                       CPU_TEST_FAIL=fail_property, CPU_TEST_RACE='1' if race_default else '0')
            result = subprocess.run(['sh', str(policy.POLICY), 'apply'], env=env,
                                    capture_output=True, text=True, timeout=20)
            first = events.read_text()
            final = json.loads(state.read_text())
            if result.returncode == 0 and not race_default:
                again = subprocess.run(['sh', str(policy.POLICY), 'apply'], env=env,
                                       capture_output=True, text=True, timeout=20)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertEqual(events.read_text(), first, 'Idempotent rerun wrote properties')
            return result, original, final, first

    def test_four_and_six_cpus_override_the_verified_eight_cpu_install_fallback(self):
        for count in (1, 2, 4, 6, 8, 12):
            with self.subTest(count=count):
                result, _, final, _ = self.run_policy(
                    online='0-' + str(count - 1), possible='0-' + str(count - 1), conf=str(count),
                    properties={'dalvik.vm.default-dex2oat-cpu-set': '0,1,2,3,4,5,6,7'})
                self.assertEqual(result.returncode, 0, result.stderr)
                # A valid explicit subset on larger machines remains intact.
                selected = ','.join(map(str, range(min(count, 8))))
                for name in policy.CPU_PROPERTIES:
                    self.assertEqual(final.get(name, ''), '' if name == 'dalvik.vm.dex2oat-cpu-set' else selected)
                for name in policy.THREAD_PROPERTIES: self.assertEqual(final[name], str(min(count, 8)))

    def test_empty_properties_use_the_actual_full_guest_topology(self):
        result, _, final, _ = self.run_policy(online='0-5', possible='0-5', conf='6')
        self.assertEqual(result.returncode, 0)
        for name in policy.CPU_PROPERTIES:
            self.assertEqual(final.get(name, ''), '' if name == 'dalvik.vm.dex2oat-cpu-set' else '0,1,2,3,4,5')
        for name in policy.THREAD_PROPERTIES: self.assertEqual(final[name], '6')

    def test_sparse_online_cpu_ids_and_possible_parser_bound(self):
        result, _, final, _ = self.run_policy(online='0,2,5', possible='0-7', conf='8')
        self.assertEqual(result.returncode, 0)
        for name in policy.CPU_PROPERTIES:
            self.assertEqual(final.get(name, ''), '' if name == 'dalvik.vm.dex2oat-cpu-set' else '0,2,5')
        for name in policy.THREAD_PROPERTIES: self.assertEqual(final[name], '3')

    def test_preserves_user_subsets_ordering_and_valid_thread_budgets(self):
        supplied = {'dalvik.vm.default-dex2oat-cpu-set': '2,0',
                    'dalvik.vm.dex2oat-cpu-set': '1,3',
                    'dalvik.vm.dex2oat-threads': '1',
                    'dalvik.vm.boot-dex2oat-threads': '2'}
        result, _, final, writes = self.run_policy(properties=supplied)
        self.assertEqual(result.returncode, 0)
        for name, value in supplied.items(): self.assertEqual(final[name], value)
        for name in supplied: self.assertNotIn(name + '=', writes)
        self.assertEqual(final['dalvik.vm.background-dex2oat-cpu-set'], '0,2')

    def test_ranges_duplicates_and_partial_out_of_range_lists(self):
        supplied = {'dalvik.vm.default-dex2oat-cpu-set': '0-99',
                    'dalvik.vm.dex2oat-cpu-set': '3,99',
                    'dalvik.vm.boot-dex2oat-cpu-set': '2,2',
                    'dalvik.vm.dex2oat-threads': '8'}
        result, _, final, _ = self.run_policy(properties=supplied)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(final['dalvik.vm.default-dex2oat-cpu-set'], '0,1,2,3')
        self.assertEqual(final['dalvik.vm.dex2oat-cpu-set'], '3')
        self.assertEqual(final['dalvik.vm.dex2oat-threads'], '1')
        self.assertEqual(final['dalvik.vm.boot-dex2oat-cpu-set'], '2,2')
        self.assertEqual(final['dalvik.vm.boot-dex2oat-threads'], '1')

    def test_empty_install_default_inherits_valid_general_user_affinity(self):
        supplied = {'dalvik.vm.dex2oat-cpu-set': '3,1', 'dalvik.vm.dex2oat-threads': '1'}
        result, _, final, events = self.run_policy(properties=supplied)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(final['dalvik.vm.dex2oat-cpu-set'], '3,1')
        self.assertEqual(final['dalvik.vm.dex2oat-threads'], '1')
        self.assertEqual(final['dalvik.vm.default-dex2oat-cpu-set'], '1,3')
        self.assertNotIn('dalvik.vm.dex2oat-cpu-set=', events)
        for name in policy.THREAD_PROPERTIES[1:]: self.assertEqual(final[name], '2')

    def test_invalid_cpu_and_thread_values_fall_back_without_executing_input(self):
        supplied = {'dalvik.vm.default-dex2oat-cpu-set': '99',
                    'dalvik.vm.dex2oat-cpu-set': '$(touch unexpected)',
                    'dalvik.vm.boot-dex2oat-cpu-set': '3-1',
                    'dalvik.vm.dex2oat-threads': '999',
                    'dalvik.vm.boot-dex2oat-threads': '0',
                    'dalvik.vm.background-dex2oat-threads': '-1',
                    'dalvik.vm.restore-dex2oat-threads': 'no'}
        result, _, final, _ = self.run_policy(properties=supplied)
        self.assertEqual(result.returncode, 0)
        for name in policy.CPU_PROPERTIES: self.assertEqual(final[name], '0,1,2,3')
        for name in policy.THREAD_PROPERTIES: self.assertEqual(final[name], '4')
        self.assertFalse((REPO / 'unexpected').exists())

    def test_large_topologies_fit_android_property_limits(self):
        result, _, final, _ = self.run_policy(online='0-255', possible='0-255', conf='256')
        self.assertEqual(result.returncode, 0)
        for name in policy.CPU_PROPERTIES:
            if name == 'dalvik.vm.dex2oat-cpu-set':
                self.assertNotIn(name, final)
                continue
            self.assertLessEqual(len(final[name]), 91)
            self.assertEqual(final[name], ','.join(map(str, range(34))))
        for name in policy.THREAD_PROPERTIES: self.assertEqual(final[name], '34')

    def test_unknown_or_inconsistent_topology_is_a_noop_before_writes(self):
        for online, possible, conf in (('', '0-3', '4'), ('0-3', '', '4'),
                                       ('0-4', '0-4', '4'), ('0,3', '0-2', '4'),
                                       ('bad', '0-3', '4'), ('0-3', '0-3', 'bad'),
                                       ('0-3', '0-3', '0'), ('4-2', '0-3', '4')):
            with self.subTest(online=online, possible=possible, conf=conf):
                result, original, final, events = self.run_policy(online, possible, conf)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(final, original)
                self.assertEqual(events, '')

    def test_scope_is_os4_emulator_not_name_device_or_firmware_pin(self):
        for hardware, version in (('qcom', 'OS4.0'), ('ranchu', 'OS3.0'), ('ranchu', '')):
            result, original, final, events = self.run_policy(hardware=hardware, version=version)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(final, original)
            self.assertEqual(events, '')
        result, _, final, _ = self.run_policy(hardware='goldfish', version='OS4.999')
        self.assertEqual(result.returncode, 0)
        self.assertIn('dalvik.vm.default-dex2oat-cpu-set', final)

    def test_property_write_failure_is_reported(self):
        result, _, _, _ = self.run_policy(fail_property='dalvik.vm.default-dex2oat-cpu-set')
        self.assertNotEqual(result.returncode, 0)

    def test_concurrent_user_change_is_not_overwritten_or_inherited_from_stale_snapshot(self):
        result, _, final, events = self.run_policy(race_default=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(final['dalvik.vm.default-dex2oat-cpu-set'], '1')
        self.assertEqual(events, '')
        self.assertNotIn('dalvik.vm.dex2oat-cpu-set', final)

    def test_image_delivery_has_synchronous_early_init_and_shared_receipt(self):
        self.assertEqual(policy.image_replacements()[policy.SYSTEM_TARGET.lstrip('/')],
                         (policy.policy_script(), 0o755, 'u:object_r:system_file:s0'))
        self.assertIn(b'on post-fs-data', policy.BOOT_INIT)
        self.assertIn(b'on post-fs\n', policy.BOOT_INIT)
        self.assertEqual(policy.BOOT_INIT.count(b'exec_start hyperos-dex2oat-cpu'), 4)
        self.assertIn(b'ro.persistent_properties.ready=true', policy.BOOT_INIT)
        self.assertIn(b'sys.boot_completed=1', policy.BOOT_INIT)
        self.assertEqual(policy.receipt()['properties'], list(policy.CPU_PROPERTIES + policy.THREAD_PROPERTIES))
        self.assertEqual(policy.receipt()['delivery'], 'shared-image-and-native-compat')
        self.assertNotIn('module_id', policy.receipt())


if __name__ == '__main__':
    unittest.main()
