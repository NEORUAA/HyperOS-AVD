import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import module_webui


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
'''
        subprocess.run([shutil.which('node'), '-e', javascript, str(source)], check=True, timeout=20)


if __name__ == '__main__':
    unittest.main()
