"""Exercise the universal artifact and real shell isolated recipe scheduler."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import test_app_bridge_module as fixture
BUSYBOX, digest, REPO = fixture.BUSYBOX, fixture.digest, fixture.REPO
sys.path.insert(0, str(REPO / 'scripts'))
import app_compat_catalog as catalog
import apply_app_compat as consumer


class CatalogTests(unittest.TestCase):
    proc = fixture.AppBridgeTests.proc
    metadata = fixture.AppBridgeTests.metadata
    install_app = fixture.AppBridgeTests.install_app
    publish = fixture.AppBridgeTests.publish

    def setUp(self):
        fixture.AppBridgeTests.setUp(self)
        # The namespace double models both file and directory overlays.
        body = BUSYBOX.replace("if value in mounts.get(pid, {}): return mounts[pid][value]", """
    for target, source in sorted(mounts.get(pid, {}).items(), key=lambda item:-len(item[0])):
        if value == target or value.startswith(target+'/'): return source+value[len(target):]""")
        body = body.replace("('/data/', '/product/', '/system/')", "('/data/', '/product/', '/system/', '/system_ext/', '/vendor/')")
        body = body.replace("if value == '/data' or", "if value in ('/data','/product','/system','/system_ext','/vendor') or")
        body = body.replace("if args[0] == '-c':", """if not args:
        print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()+'  -'); sys.exit(0)
    if args[0] == '-c':""")
        body = body.replace("form.replace('%u:%g', owner)", "form.replace('%n', args[2]).replace('%u:%g', owner)")
        self.bb.write_text(body)
        self.pm_map = {'com.fixture.app': self.apk}
        self.write_pm()
        (self.bin / 'pm').write_text('#!' + sys.executable + '\n' + """
import json, os, pathlib, sys
value=json.loads((pathlib.Path(os.environ['BRIDGE_TEST_ROOT'])/'pm-map').read_text()).get(sys.argv[-1])
if value: print('package:'+value)
""")
        (self.bin / 'settings').write_text('#!' + sys.executable + '\n' + """
import json, os, pathlib, sys
p=pathlib.Path(os.environ['BRIDGE_TEST_ROOT'])/'settings.json'
v=json.loads(p.read_text())
if sys.argv[1]=='get': print(v.get(sys.argv[3],'null'))
else:
    v[sys.argv[3]]=sys.argv[4]; p.write_text(json.dumps(v))
""")
        (self.bin / 'settings').chmod(0o755)
        (self.root / 'settings.json').write_text('{}')
        self.env['HYPEROS_APP_COMPAT_PREFS'] = str(self.root / 'preferences')
        self.preference = self.root / 'preferences'
        self.preference.mkdir()
        self.recipes = [self.recipe('first', 'weather', 'com.fixture.app', True, self.apk_data)]
        self.outputs = {digest(self.after): self.payload}
        self.publish_catalog()

    def recipe(self, identity, feature, package, enabled, apk):
        return {'id': identity, 'feature': feature, 'package': package, 'default_enabled': enabled,
                'apk_sha256': digest(apk), 'system_libraries': {}, 'native_libraries': {},
                'libraries': [{'name':'libtest.so', 'before':digest(self.before),
                               'after':digest(self.after), 'placeholder':False}], 'system_targets':[]}

    def write_pm(self):
        (self.root / 'pm-map').write_text(json.dumps(self.pm_map))

    def publish_catalog(self):
        files = catalog.catalog_files(self.recipes, self.outputs)
        for name, data in files.items():
            path = self.module / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # The fixture's earlier single-workload files must not count as catalog assets.
        for path in (self.module / 'cache', self.module / 'state'):
            path.mkdir(exist_ok=True)

    def shell(self, body, *, check=True):
        source = ('MODDIR='+shlex.quote(str(self.module))+'\n. "$MODDIR/runtime.sh"\n'
                  '. "$MODDIR/catalog.sh"\ncatalog_assets || exit 19\n'+body)
        return subprocess.run(['sh','-c',source], env=self.env, text=True,
                              capture_output=True, check=check, timeout=60)

    def mounts(self):
        return json.loads((self.root / 'mounts.json').read_text())

    def statuses(self):
        return {row.split('|')[0]: row.split('|')[2] for row in
                (self.module / 'state/status.tsv').read_text().splitlines()}

    def second(self, enabled=False):
        package = 'com.fixture.other'
        apk = '/data/app/~~other/'+package+'-A/base.apk'
        previous = self.apk_data
        self.apk_data = b'other exact signed APK'
        self.install_app(apk)
        self.recipes.append(self.recipe('second','parrot-camera',package,enabled,self.apk_data))
        self.apk_data = previous
        self.pm_map[package] = apk
        self.write_pm()
        self.publish_catalog()
        return apk

    def test_complete_artifact_deduplicates_objects_and_preserves_independent_defaults(self):
        self.second()
        a,b = self.root/'a.zip',self.root/'b.zip'
        catalog.package_catalog(a,self.recipes,self.outputs)
        catalog.package_catalog(b,self.recipes,self.outputs)
        self.assertEqual(a.read_bytes(),b.read_bytes())
        with zipfile.ZipFile(a) as packed:
            self.assertEqual(len([name for name in packed.namelist() if name.startswith('objects/')]),1)
            self.assertIn('webroot/index.html',packed.namelist())
            self.assertIn('post-fs-data.sh',packed.namelist())
        self.shell('catalog_pass late')
        self.assertEqual(self.statuses(),{'weather':'verified','parrot-camera':'disabled'})
        self.assertEqual(set(self.mounts()['1']),{self.native+'/libtest.so'})

    def test_unknown_update_is_isolated_and_explicit_opt_in_survives_reinstall(self):
        second=self.second()
        (self.preference/'parrot-camera').write_text('on\n')
        self.shell('catalog_pass late')
        self.assertEqual(set(self.statuses().values()),{'verified'})
        (self.root/self.apk.lstrip('/')).write_bytes(b'unknown updated signed APK')
        self.shell('catalog_pass late')
        self.assertEqual(self.statuses()['weather'],'unsupported')
        self.assertEqual(self.statuses()['parrot-camera'],'verified')
        self.assertEqual(set(self.mounts()['1']),{second.rsplit('/',1)[0]+'/lib/arm64/libtest.so'})
        (self.preference/'parrot-camera').write_text('off\n')
        self.shell('catalog_pass late')
        self.assertEqual(self.mounts()['1'],{})
        self.assertEqual(self.statuses()['parrot-camera'],'disabled')

    def test_reinstall_path_and_new_namespace_reconcile_without_unchanged_hashing(self):
        self.proc(66)
        self.shell('catalog_pass late\n: > "$BRIDGE_TEST_ROOT/hash-trace"\ncatalog_pass late\n[ ! -s "$BRIDGE_TEST_ROOT/hash-trace" ]')
        second='/data/app/~~new/com.fixture.app-B/base.apk'
        self.install_app(second)
        self.pm_map['com.fixture.app']=second;self.write_pm()
        (self.root/'pids').write_text('66')
        self.shell('catalog_pass late')
        target=second.rsplit('/',1)[0]+'/lib/arm64/libtest.so'
        self.assertEqual(set(self.mounts()['1']),{target})
        self.assertIn(target,self.mounts()['66'])
        self.assertEqual((self.root/second.lstrip('/')).read_bytes(),self.apk_data)

    def test_global_flags_pending_and_single_scheduler_lease(self):
        for flag in ('disable','remove'):
            (self.module/flag).touch()
            result=self.shell('catalog_service',check=False)
            self.assertEqual(result.returncode,0)
            self.assertEqual(self.mounts(),{})
            (self.module/flag).unlink()
        self.shell('bridge_lock\n( bridge_lock ) && exit 1\nexit 0')
        self.shell('bridge_lock')
        # A cached recipe ID cannot escape its catalog context or select another feature.
        self.shell('catalog_pass late')
        selection=self.module/'state/select-late-weather'
        original=selection.read_text().splitlines()[0]
        selection.write_text(original+'\n../../other\n')
        self.shell('catalog_pass late')
        self.assertEqual(self.statuses()['weather'],'unsupported')

    def test_embedded_abi_and_early_provider_loaded_guard_preserve_targets(self):
        provider='/vendor/bin/hw/android.hardware.camera.provider@2.7-service-google'
        original=b'original exact provider';payload=b'patched exact provider'
        target=self.root/provider.lstrip('/');target.parent.mkdir(parents=True);target.write_bytes(original)
        target.chmod(0o755);self.metadata(target,context='u:object_r:hal_camera_default_exec:s0',owner='1000:1000')
        factory='/product/priv-app/Fixture/Fixture.apk';factory_path=self.root/factory.lstrip('/')
        factory_path.parent.mkdir(parents=True)
        with zipfile.ZipFile(factory_path,'w') as apk: apk.writestr('lib/arm64-v8a/libtest.so',self.before)
        recipe=self.recipes[0];recipe.update(apk_sha256=digest(factory_path.read_bytes()),factory_apk=factory,
            native='/product/priv-app/Fixture/lib/arm64',factory_overlay=True,
            apk_libraries={'lib/arm64-v8a/libtest.so':digest(self.before)},
            system_targets=[{'target':provider,'before':digest(original),'after':digest(payload),'mount_flags':'ro,suid,exec'}])
        recipe['libraries'][0]['placeholder']=True
        provider_payload=self.root/'provider.so';provider_payload.write_bytes(payload)
        self.outputs[digest(payload)]=provider_payload
        self.pm_map['com.fixture.app']=factory;self.write_pm();self.publish_catalog()
        (self.root/'proc/1/maps').write_text('0-1 r--p 0 1:2 12 '+provider+'\n')
        self.shell('catalog_pass early')
        self.assertEqual(self.statuses()['weather'],'reboot-required')
        self.assertEqual(self.mounts(),{})
        self.assertEqual(target.read_bytes(),original)
        (self.root/'proc/1/maps').write_text('')
        self.shell('catalog_pass early')
        self.assertEqual(self.statuses()['weather'],'verified')
        self.assertIn(provider,self.mounts()['1'])
        self.assertIn(factory.rsplit('/',1)[0],self.mounts()['1'])
        mirror=Path(self.mounts()['1'][factory.rsplit('/',1)[0]])
        self.assertEqual((mirror/'Fixture.apk').read_bytes(),factory_path.read_bytes())
        self.assertEqual((mirror/'lib/arm64/libtest.so').read_bytes(),self.after)
        self.shell('catalog_pass late')
        self.assertEqual(self.statuses()['weather'],'verified')
        self.assertIn(provider,self.mounts()['1'])
        (self.preference/'weather').write_text('off\n')
        self.shell('catalog_pass late')
        self.assertEqual(self.mounts()['1'],{})

    def factory_preopt(self):
        factory='/product/priv-app/Fixture/Fixture.apk'
        apk=self.root/factory.lstrip('/')
        apk.parent.mkdir(parents=True)
        with zipfile.ZipFile(apk,'w') as archive:
            archive.writestr('lib/arm64-v8a/libtest.so',self.before)
        apk.chmod(0o644)
        self.metadata(apk,owner='0:0',context='u:object_r:system_file:s0')
        passthrough={'oat/arm64/Fixture.odex':b'original precompiled code',
                     'oat/arm64/Fixture.vdex':b'original verified dex'}
        for name,body in passthrough.items():
            target=apk.parent/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(body);target.chmod(0o640)
            self.metadata(target,owner='1000:1015',context='u:object_r:dalvikcache_data_file:s0')
        for directory in (apk.parent,apk.parent/'oat',apk.parent/'oat/arm64'):
            directory.chmod(0o750)
            self.metadata(directory,owner='0:1015',context='u:object_r:system_file:s0')
        recipe=self.recipes[0]
        recipe.update(apk_sha256=digest(apk.read_bytes()),factory_apk=factory,
                      native=str(Path(factory).parent/'lib/arm64'),factory_overlay=True,
                      apk_libraries={'lib/arm64-v8a/libtest.so':digest(self.before)},
                      factory_passthrough={name:digest(body) for name,body in passthrough.items()})
        recipe['libraries'][0]['placeholder']=True
        self.pm_map['com.fixture.app']=factory;self.write_pm();self.publish_catalog()
        return factory,apk,passthrough

    def test_factory_preopt_mirror_preserves_exact_bytes_and_metadata(self):
        factory,apk,passthrough=self.factory_preopt()
        original=apk.read_bytes()
        self.shell('catalog_pass early')
        self.assertEqual(self.statuses()['weather'],'verified')
        mirror=Path(self.mounts()['1'][str(Path(factory).parent)])
        metadata=json.loads((self.root/'metadata.json').read_text())
        for name,body in passthrough.items():
            source,target=apk.parent/name,mirror/name
            self.assertEqual(source.read_bytes(),body)
            self.assertEqual(target.read_bytes(),body)
            self.assertEqual(target.stat().st_mode&0o777,0o640)
            self.assertEqual(target.stat().st_nlink,1)
            self.assertEqual(metadata[f'{target.stat().st_dev}:{target.stat().st_ino}'],
                             metadata[f'{source.stat().st_dev}:{source.stat().st_ino}'])
        for name in ('oat','oat/arm64'):
            source,target=apk.parent/name,mirror/name
            self.assertEqual(target.stat().st_mode&0o777,source.stat().st_mode&0o777)
            self.assertEqual(metadata[f'{target.stat().st_dev}:{target.stat().st_ino}'],
                             metadata[f'{source.stat().st_dev}:{source.stat().st_ino}'])
        self.assertEqual((mirror/'Fixture.apk').read_bytes(),original)
        self.assertEqual(apk.read_bytes(),original)
        self.shell('catalog_pass late\n: > "$BRIDGE_TEST_ROOT/hash-trace"\ncatalog_pass late\n[ ! -s "$BRIDGE_TEST_ROOT/hash-trace" ]')
        # New files invalidate the cheap inventory token even with unchanged APK.
        (mirror/'oat/arm64/unknown.art').write_bytes(b'foreign addition')
        self.shell('catalog_pass early')
        self.assertEqual(self.statuses()['weather'],'unsupported')
        self.assertTrue((mirror/'oat/arm64/unknown.art').is_file())

    def test_factory_preopt_unknown_content_and_aliases_fail_before_any_bind(self):
        _,apk,passthrough=self.factory_preopt()
        target=apk.parent/'oat/arm64/Fixture.vdex'
        original=target.read_bytes()
        target.write_bytes(b'unaudited preopt update')
        self.shell('catalog_pass early')
        self.assertEqual(self.statuses()['weather'],'unsupported')
        self.assertEqual(self.mounts(),{})
        target.write_bytes(original)
        alias=self.root/'external-vdex';os.link(target,alias)
        self.shell('catalog_pass early')
        self.assertEqual(self.statuses()['weather'],'unsupported')
        self.assertEqual(self.mounts(),{})
        alias.unlink()
        target.unlink();target.symlink_to(alias)
        alias.write_bytes(original)
        self.shell('catalog_pass early')
        self.assertEqual(self.statuses()['weather'],'unsupported')
        self.assertEqual(self.mounts(),{})
        target.unlink();target.write_bytes(original)
        unknown=apk.parent/'oat/arm64/foreign.art';unknown.write_bytes(b'foreign')
        self.shell('catalog_pass early')
        self.assertEqual(self.statuses()['weather'],'unsupported')
        self.assertEqual(self.mounts(),{})
        self.assertEqual(unknown.read_bytes(),b'foreign')

    def test_factory_passthrough_schema_requires_explicit_preopt_and_overlay(self):
        for asset in ('../Fixture.odex','oat/arm64/../Fixture.odex','oat/arm64/unknown.art',
                      'oat/x86_64/Fixture.odex','oat/arm64/sub/Fixture.odex','lib/arm64/libtest.so'):
            recipe=copy.deepcopy(self.recipes)
            recipe[0].update(factory_overlay=True,factory_apk='/product/priv-app/Fixture/Fixture.apk',
                             native='/product/priv-app/Fixture/lib/arm64',factory_passthrough={asset:digest(b'original')})
            with self.subTest(asset=asset),self.assertRaisesRegex(RuntimeError,'passthrough'):
                catalog.validate(recipe)
        recipe=copy.deepcopy(self.recipes)
        recipe[0]['factory_passthrough']={'oat/arm64/Fixture.odex':digest(b'original')}
        with self.assertRaisesRegex(RuntimeError,'passthrough'):catalog.validate(recipe)

    def test_owned_angle_cleanup_preserves_unrelated_package_policy(self):
        self.recipes[0]['policy']={'angle':{'driver':'angle','features':['exposeES32ForTesting']}}
        self.publish_catalog()
        self.shell('catalog_pass late')
        values=json.loads((self.root/'settings.json').read_text())
        self.assertEqual(values['angle_gl_driver_selection_pkgs'],'com.fixture.app')
        values['angle_gl_driver_selection_pkgs']+=',com.foreign.app'
        values['angle_gl_driver_selection_values']+=',native'
        (self.root/'settings.json').write_text(json.dumps(values))
        (self.preference/'weather').write_text('off\n')
        self.shell('catalog_pass late')
        values=json.loads((self.root/'settings.json').read_text())
        self.assertEqual(values['angle_gl_driver_selection_pkgs'],'com.foreign.app')
        self.assertEqual(values['angle_gl_driver_selection_values'],'native')
        self.assertEqual(self.statuses()['weather'],'disabled')

    def test_archive_and_recipe_aliases_are_rejected_without_guessing(self):
        self.second()
        unknown=copy.deepcopy(self.recipes)
        unknown[0]['native_libraries']=[]
        with self.assertRaisesRegex(RuntimeError,'dependency'):catalog.validate(unknown)
        unknown=copy.deepcopy(self.recipes);unknown[1]['feature']='weather'
        with self.assertRaisesRegex(RuntimeError,'one package'):catalog.validate(unknown)
        control=self.module/'contexts/first/embedded.tsv'
        alias=self.root/'alias';alias.write_bytes(control.read_bytes());control.unlink();control.symlink_to(alias)
        self.assertNotEqual(self.shell('catalog_pass late',check=False).returncode,0)
        self.assertEqual(self.mounts(),{})

    def test_shell_verified_paths_admit_provider_names_and_reject_all_traversal(self):
        self.shell('bridge_path /vendor/bin/hw/android.hardware.camera.provider@2.7-service-google || exit 1\n'
                   'for path in /data/app/pkg/.. /data/app/pkg/. /data/app/pkg/../base.apk /data/app//pkg/base.apk /data/app/pkg/; do\n'
                   ' bridge_path "$path" && exit 1\ndone\nexit 0')

    def test_prebuilt_consumer_stages_only_verified_complete_archive_and_preserves_flags(self):
        workspace=self.root/'workspace';folder=workspace/'tools/os4-app-compat';folder.mkdir(parents=True)
        receipt=catalog.package_catalog(folder/'app-compat.zip',self.recipes,self.outputs)
        (folder/'receipt.json').write_bytes(catalog.canonical(receipt))
        archive,manifest=catalog.verify_prebuilt(folder,self.recipes)
        commands=[]
        def root(config,command):
            commands.append(command)
            if command.startswith('getprop'):return 'ranchu\nOS4.999.0.TEST'
            return ''
        with patch.object(consumer,'root',side_effect=root),patch.object(consumer,'_inspect',return_value=None),patch.object(consumer,'adb') as adb:
            result=consumer.install_prebuilt({},workspace,catalog=self.recipes,migration=False)
        self.assertTrue(result['reboot_required'])
        self.assertEqual(adb.call_count,1)
        self.assertTrue(any('ksud module install' in command for command in commands))
        self.assertFalse(any('am force-stop' in command or 'nsenter' in command for command in commands))
        # A user-disabled module is preserved even if the consumer archive is missing.
        (folder/'app-compat.zip').unlink()
        with patch.object(consumer,'root',return_value='ranchu\nOS4.999.0.TEST'),patch.object(consumer,'_inspect',return_value={'flags':['disable']}),patch.object(consumer,'adb') as adb:
            result=consumer.install_prebuilt({},workspace,catalog=self.recipes,migration=False)
            self.assertTrue(result['preserved']);adb.assert_not_called()

    def test_reviewed_universal_history_authorizes_old_bytes_and_normal_update_only(self):
        old_folder=self.root/'old-prebuilt';old_folder.mkdir()
        saved=catalog.package_catalog(old_folder/'app-compat.zip',self.recipes,self.outputs)
        (old_folder/'receipt.json').write_bytes(catalog.canonical(saved))
        _,old=catalog.verify_prebuilt(old_folder,self.recipes)
        pin=digest(catalog.canonical(old))
        self.second()
        workspace=self.root/'next-workspace';folder=workspace/'tools/os4-app-compat';folder.mkdir(parents=True)
        current=catalog.package_catalog(folder/'app-compat.zip',self.recipes,self.outputs)
        (folder/'receipt.json').write_bytes(catalog.canonical(current))
        active={'directory':'/data/adb/modules/'+catalog.MODULE_ID,'properties':'known ownership','flags':[]}
        commands=[]
        def root(config,command):
            commands.append(command)
            return 'ranchu\nOS4.999.0.TEST' if command.startswith('getprop') else ''
        def saved(config,module,expected):
            if expected!=old:raise consumer.CatalogMismatch('Current content differs from prior release')
        with patch.object(catalog,'PREVIOUS_MANIFESTS',{pin:old}):
            self.assertEqual(catalog.verify_previous_prebuilt(old_folder),old)
            with patch.object(consumer,'root',side_effect=root),patch.object(consumer,'_inspect',side_effect=[active,None]),patch.object(consumer,'_saved',side_effect=saved),patch.object(consumer,'adb') as adb:
                result=consumer.install_prebuilt({},workspace,catalog=self.recipes,migration=False)
                self.assertTrue(result['reboot_required']);adb.assert_called_once()
            self.assertTrue(any('ksud module install' in command for command in commands))
            self.assertFalse(any('rm -rf' in command or 'mv /data/adb/modules_update' in command for command in commands))
        with self.assertRaisesRegex(RuntimeError,'Unknown previous'):
            catalog.verify_previous_prebuilt(old_folder)

    def test_pending_unknown_catalog_and_preexisting_feature_choices_are_preserved(self):
        workspace=self.root/'workspace';folder=workspace/'tools/os4-app-compat';folder.mkdir(parents=True)
        saved=catalog.package_catalog(folder/'app-compat.zip',self.recipes,self.outputs)
        (folder/'receipt.json').write_bytes(catalog.canonical(saved))
        pending={'directory':'/data/adb/modules_update/'+catalog.MODULE_ID,'flags':[]}
        with patch.object(consumer,'root',return_value='ranchu\nOS4.999.0.TEST'),patch.object(consumer,'_inspect',side_effect=[None,pending]),patch.object(consumer,'_saved',side_effect=consumer.CatalogMismatch('Unknown pending')),patch.object(consumer,'adb') as adb,patch.object(consumer,'set_features') as choices:
            result=consumer.install_prebuilt({},workspace,enabled_features={'weather':False},catalog=self.recipes,migration=False)
            self.assertTrue(result['deferred']);adb.assert_not_called();choices.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'choice'):
            consumer.set_features({}, {'weather':1},self.recipes)

    def test_full_production_catalog_asset_check_is_bounded_and_authenticates_every_file(self):
        from app_compat_producers import recipes
        complete=json.loads(catalog.catalog_files(recipes())['manifest.json'])
        directory='/data/adb/modules/'+catalog.MODULE_ID
        command=consumer._authentication_command(directory,complete)
        self.assertLess(len(command.encode()),16*1024)
        self.assertEqual(command.count('[ -d "$parent" ] && [ ! -L "$parent" ]'),1)
        names=set(complete['files_sha256'])-{'customize.sh'}|{'manifest.json','SHA256SUMS'}
        table=command.split("done <<'HYPEROS_CANONICAL_APP_ASSETS'\n",1)[1].rsplit('\nHYPEROS_CANONICAL_APP_ASSETS',1)[0]
        actual={line.split(' ',1)[1] for line in table.splitlines()}
        self.assertEqual(actual,names)
        self.assertIn('stat -c %h:%u "$path"',command)
        self.assertIn('[ -f "$path" ] && [ ! -L "$path" ]',command)
        self.assertIn('sha256sum "$path"',command)

    def test_catalog_asset_mismatch_and_adb_transport_failures_are_distinct(self):
        expected=json.loads(catalog.catalog_files(self.recipes,self.outputs)['manifest.json'])
        module={'directory':'/data/adb/modules/'+catalog.MODULE_ID}
        closed=RuntimeError('Flutter overlay command failed: error: closed')
        with patch.object(consumer,'root',side_effect=closed):
            with self.assertRaises(RuntimeError) as caught:consumer._saved({},module,expected)
            self.assertIs(caught.exception,closed)
        rejection=RuntimeError('Flutter overlay command failed: '+consumer.MISMATCH_MARKER+':/data/adb/module/runtime.sh')
        with patch.object(consumer,'root',side_effect=rejection):
            with self.assertRaises(consumer.CatalogMismatch):consumer._saved({},module,expected)

    def test_compact_asset_checker_executes_and_rejects_real_aliases_and_changed_bytes(self):
        expected=json.loads(catalog.catalog_files(self.recipes,self.outputs)['manifest.json'])
        for command in ('stat','sha256sum'):
            wrapper=self.bin/command
            wrapper.write_text('#!/bin/sh\nexec "$HYPEROS_BRIDGE_BB" '+command+' "$@"\n')
            wrapper.chmod(0o755)
        def root(config,command):
            result=subprocess.run(['sh','-c',command],env=self.env,text=True,capture_output=True,timeout=30)
            if result.returncode:raise RuntimeError('Guest validation failed: '+result.stdout+result.stderr)
            return result.stdout
        module={'directory':str(self.module)}
        with patch.object(consumer,'root',side_effect=root):
            consumer._saved({},module,expected)
            target=self.module/'contexts/first/package.txt'
            original=target.read_bytes()
            target.write_bytes(b'unknown local package\n')
            with self.assertRaises(consumer.CatalogMismatch):consumer._saved({},module,expected)
            target.write_bytes(original)
            self.metadata(target,owner='1000:1000')
            with self.assertRaises(consumer.CatalogMismatch):consumer._saved({},module,expected)
            self.metadata(target,owner='0:0')
            self.metadata(target.parent,owner='1000:1000')
            with self.assertRaises(consumer.CatalogMismatch):consumer._saved({},module,expected)
            self.metadata(target.parent,owner='0:0')
            alias=self.root/'hardlink';os.link(target,alias)
            with self.assertRaises(consumer.CatalogMismatch):consumer._saved({},module,expected)


if __name__=='__main__':unittest.main()
