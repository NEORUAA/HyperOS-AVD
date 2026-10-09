import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import module_webui


class ModuleWebUIShellTests(unittest.TestCase):
    """Read actual lifecycle flags from disposable Android-like directories."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyperos-webui-status-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / 'adb'; self.bin = self.root / 'bin'; self.bin.mkdir()
        self.module = self.base / 'modules/hyperos_avd_native_compat'
        (self.module / 'state').mkdir(parents=True)
        (self.module / 'module.prop').write_text('id=hyperos_avd_native_compat\nauthor=HyperOS-AVD\n')
        (self.module / 'state/status.tsv').write_text('flutter|/system/lib.so|failed|patch-verification\n')
        self.collector = self.root / 'status.sh'; self.collector.write_bytes(module_webui.files('core')['webui-status.sh'])
        self.busybox = self.bin / 'busybox'
        self.busybox.write_text('#!' + sys.executable + '\n' + '''import os,pathlib,sys
args=sys.argv[1:]
if args[:3] == ['stat','-c','%h:%u']:
    print(str(pathlib.Path(args[3]).stat().st_nlink)+':0'); sys.exit(0)
os.execvp(args[0],args)
''')
        self.busybox.chmod(0o755)
        for name, value in (('getprop', 'skiavk'), ('getenforce', 'Enforcing')):
            path = self.bin / name; path.write_text('#!/bin/sh\necho ' + value + '\n'); path.chmod(0o755)

    def collect(self):
        environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                           HYPEROS_STATUS_BB=str(self.busybox), HYPEROS_STATUS_BASE=str(self.base))
        return subprocess.run(['sh', str(self.collector)], env=environment, check=True,
                              capture_output=True, text=True).stdout

    def snapshot(self):
        return {str(path.relative_to(self.base)): (path.read_bytes(), path.stat().st_ino, path.stat().st_mode)
                for path in self.base.rglob('*') if path.is_file() and not path.is_symlink()}

    def test_enabled_lifecycle_does_not_claim_all_patches_are_ready_and_is_read_only(self):
        before = self.snapshot(); output = self.collect()
        self.assertIn('state=enabled\n', output)
        self.assertIn('flutter|/system/lib.so|failed|patch-verification', output)
        self.assertEqual(before, self.snapshot())

    def test_disable_remove_and_pending_flags_have_distinct_lifecycle_states(self):
        for flag, state in (('disable', 'disabled'), ('remove', 'pending-removal')):
            for broken_alias in (False, True):
                with self.subTest(flag=flag, broken_alias=broken_alias):
                    marker = self.module / flag
                    if broken_alias: marker.symlink_to(self.root / 'missing')
                    else: marker.touch()
                    self.assertIn('state=' + state + '\n', self.collect())
                    if broken_alias: self.assertTrue(marker.is_symlink())
                    marker.unlink()
        stage = self.base / 'modules_update/hyperos_avd_native_compat'; stage.mkdir(parents=True)
        self.assertIn('state=pending\n', self.collect())
        (self.module / 'remove').touch(); (self.module / 'disable').touch()
        self.assertIn('state=pending-removal\n', self.collect())

    def test_unactivated_or_aliased_state_is_not_treated_as_verified(self):
        state = self.module / 'state/status.tsv'; state.unlink()
        self.assertIn('state=not-activated\n', self.collect())
        foreign = self.root / 'foreign-status'; foreign.write_text('SECRET_EXTERNAL_STATE\n')
        state.symlink_to(foreign)
        output = self.collect()
        self.assertIn('state=not-activated\n', output)
        self.assertNotIn('SECRET_EXTERNAL_STATE', output)
        self.assertTrue(state.is_symlink())


class ModuleWebUITests(unittest.TestCase):
    def test_both_modules_embed_offline_assets_with_fixed_commands(self):
        for family, identity in module_webui.FAMILIES.items():
            files = module_webui.files(family)
            self.assertEqual(set(files), {'webroot/index.html', 'webroot/app.js',
                                          'webroot/style.css', 'webui-status.sh'})
            self.assertIn(identity.encode(), files['webroot/index.html'])
            self.assertIn(('ID=' + identity).encode(), files['webui-status.sh'])
            self.assertNotIn(b'@MODULE_ID@', b''.join(files.values()))
            self.assertIn(b"connect-src 'none'", files['webroot/index.html'])
            self.assertNotIn(b'http', files['webroot/app.js'])
            self.assertNotIn(b'innerHTML', files['webroot/app.js'])
        with self.assertRaises(RuntimeError):
            module_webui.files('../../foreign')

    def test_collector_is_syntax_valid_and_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'status.sh'
            path.write_bytes(module_webui.files('core')['webui-status.sh'])
            subprocess.run(['sh', '-n', str(path)], check=True)
        data = module_webui.files('core')['webui-status.sh'].decode()
        self.assertIn('head -c 24576', data)
        self.assertIn('head -n 160', data)
        self.assertIn('tail -c 8192', data)
        self.assertIn('safe_file', data)
        for mutation in ('chmod ', 'setprop ', 'settings put ', 'mount ', 'rm ', 'cp ', 'eval '):
            self.assertNotIn(mutation, data)

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime is optional')
    def test_status_parser_and_bridge_cleanup(self):
        source = module_webui.ROOT / 'modules/compat-webui/app.js'
        javascript = r'''
const assert=require('node:assert/strict');
const view=require(process.argv[1]);
const data=view.parse('[module]\nstate=pending\n[health]\nrenderer=skiavk\n[status]\n'+
  'flutter|/system/lib.so|ready|verified-bind\nweather|com.miui.weather2|unsupported|unsupported-apk\n'+
  'foreign|x|unknown;rm|y\n[log]\n<script>unsafe</script>');
assert.equal(data.module.state,'pending');
assert.equal(data.patches.length,2);
assert.equal(data.patches[1].state,'unsupported');
assert.equal(data.logs[0],'<script>unsafe</script>');
(async()=>{
 let callback;
 const text=await view.run('fixed-read-only-command',{exec:(command,options,name)=>{
  assert.equal(command,'fixed-read-only-command');assert.equal(options,'{}');callback=name;
  globalThis[name](0,'status-output','');
 }});
 assert.equal(text,'status-output');assert.equal(globalThis[callback],undefined);
 await assert.rejects(view.run('fixed',undefined),/KernelSU/);
 await assert.rejects(view.run('fixed',{exec:()=>{throw new Error('transport');}}),/transport/);
 await assert.rejects(view.run('fixed',{exec:(_,__,name)=>globalThis[name](1,'','failed')}),/failed/);
 const names=Object.keys(globalThis).filter(x=>x.startsWith('hyperos_status_'));
 assert.equal(names.length,0);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        subprocess.run([shutil.which('node'), '-e', javascript, str(source)], check=True, timeout=20)

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime is optional')
    def test_render_treats_status_and_logs_as_text(self):
        source = module_webui.ROOT / 'modules/compat-webui/app.js'
        javascript = r'''
const assert=require('node:assert/strict');const view=require(process.argv[1]);
const nodes={};function node(){return {children:[],textContent:'',append(...items){this.children.push(...items);},replaceChildren(){this.children=[];}};}
const document={getElementById:id=>nodes[id]??=(node()),createElement:()=>node()};
view.render({module:{state:'disabled'},health:{renderer:'skiavk',selinux:'Enforcing'},
 patches:[{feature:'weather',state:'unsupported',reason:'<script>x</script>',target:'$(touch /data/pwn)'}],
 logs:['<img src=x onerror=alert(1)>']},document);
assert.equal(nodes['module-state'].textContent,'已停用');
assert.equal(nodes.patches.children[0].children[1].textContent,'<script>x</script>');
assert.equal(nodes.patches.children[0].children[2].textContent,'$(touch /data/pwn)');
assert.equal(nodes.logs.textContent,'<img src=x onerror=alert(1)>');
view.render({module:{state:'enabled'},health:{},patches:[],logs:[]},document);
assert.equal(nodes['module-state'].textContent,'已启用');
view.render({module:{state:'pending-removal'},health:{},patches:[],logs:[]},document);
assert.equal(nodes['module-state'].textContent,'待卸载');
'''
        subprocess.run([shutil.which('node'), '-e', javascript, str(source)], check=True, timeout=20)

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime is optional')
    def test_log_section_markers_are_literal_and_cannot_forge_actual_status(self):
        source = module_webui.ROOT / 'modules/compat-webui/app.js'
        javascript = r'''
const assert=require('node:assert/strict'),view=require(process.argv[1]);
const log='subprocess output\n[module]\nstate=ready\n[health]\nrenderer=forged\n[status]\nflutter|forged.so|ready|verified-bind\n[log]\nend';
const data=view.parse('[module]\nstate=disabled\n[health]\nrenderer=skiavk\n[status]\nflutter|real.so|disabled|user-choice\n[log]\n'+log);
assert.equal(data.module.state,'disabled');assert.equal(data.health.renderer,'skiavk');
assert.equal(data.patches.length,1);assert.equal(data.patches[0].target,'real.so');
assert.equal(data.logs.join('\n'),log);
'''
        subprocess.run([shutil.which('node'), '-e', javascript, str(source)], check=True, timeout=20)


if __name__ == '__main__':
    unittest.main()
