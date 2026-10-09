/* Offline status only. The command and module ID are fixed packaging constants. */
(function (scope) {
  'use strict';
  const titles = {flutter:'Flutter 渲染', hwui:'系统 View 渲染', assistant:'小爱光效',
    'lockscreen-video':'锁屏视频', navigation:'桌面与返回', composer:'窗口透明度',
    'rear-display':'背屏合成', identity:'设备身份', serial:'模拟序列号', thermal:'温控兼容',
    refresh:'显示帧率', 'boot-services':'启动服务', 'rear-wake':'背屏唤醒',
    weather:'天气桥接', 'oem-camera':'系统相机', camera:'系统相机',
    'parrot-camera':'Google 相机', parrot:'Google 相机'};
  const labels = {ready:'已就绪', verified:'已核验', failed:'检查失败', disabled:'已停用',
    skipped:'已跳过', unsupported:'未匹配', pending:'待激活', 'reboot-required':'待重启',
    'not-activated':'未激活', 'not-installed':'未安装'};
  const reasons = {'verified-bind':'挂载与文件已核验', 'already-patched':'当前文件已包含修复',
    'unsupported-elf':'此版本的原生库尚未适配，已保留原文件',
    'unsupported-apk':'此应用版本尚未匹配，已保留原文件',
    'reboot-required':'请正常重启以加载新代码', 'module-disabled':'模块已关闭',
    'missing-package':'未安装对应应用', 'missing-capability':'未检测到所需硬件能力'};
  function parse(text) {
    const data={module:{},health:{},patches:[],logs:[]}; let section='';
    for (const raw of String(text).slice(0,65536).split('\n')) {
      if (/^\[(module|health|status|log)\]$/.test(raw)) { section=raw.slice(1,-1); continue; }
      if (section==='status') {
        const row=raw.split('|');
        if (row.length===4 && /^[a-z0-9-]+$/.test(row[0]) && /^[a-z0-9-]+$/.test(row[2]))
          data.patches.push({feature:row[0],target:row[1],state:row[2],reason:row[3]});
      } else if (section==='log') data.logs.push(raw);
      else if (section==='module'||section==='health') {
        const i=raw.indexOf('='); if(i>0) data[section][raw.slice(0,i)]=raw.slice(i+1);
      }
    }
    return data;
  }
  function run(command, bridge) {
    return new Promise((resolve,reject)=>{
      if (!bridge || typeof bridge.exec!=='function') { reject(new Error('请在 KernelSU 管理器中打开此页面。')); return; }
      const callback='hyperos_status_'+Date.now()+'_'+Math.random().toString(36).slice(2);
      let timer;
      function finish() { clearTimeout(timer); delete scope[callback]; }
      scope[callback]=(errno,stdout,stderr)=>{ finish(); Number(errno)===0 ? resolve(String(stdout)) : reject(new Error(String(stderr)||'无法读取模块状态。')); };
      timer=setTimeout(()=>{ finish(); reject(new Error('读取超时，请稍后刷新。')); },12000);
      try { bridge.exec(command,'{}',callback); } catch(error) { finish(); reject(error); }
    });
  }
  function render(data, document) {
    const element=id=>document.getElementById(id);
    element('module-state').textContent=labels[data.module.state]||data.module.state||'状态未知';
    element('health').textContent=['SELinux '+(data.health.selinux||'未知'),'HWUI '+(data.health.renderer||'未知')].join(' · ');
    element('patches').replaceChildren();
    for(const patch of data.patches) {
      const card=document.createElement('article'),header=document.createElement('header'); card.className='patch';
      const title=document.createElement('h2');title.textContent=titles[patch.feature]||patch.feature;
      const badge=document.createElement('span');badge.className='badge '+patch.state;badge.textContent=labels[patch.state]||patch.state;
      header.append(title,badge);card.append(header);
      const reason=document.createElement('p');reason.textContent=reasons[patch.reason]||patch.reason||'未提供详情';card.append(reason);
      const target=document.createElement('code');target.textContent=patch.target;card.append(target);element('patches').append(card);
    }
    element('message').textContent=data.patches.length ? '已读取 '+data.patches.length+' 个目标的实际运行记录。' : '尚无运行记录；模块可能尚未激活。';
    element('logs').textContent=data.logs.join('\n').trim()||'暂无日志。';
    element('updated').textContent='读取时间 '+new Date().toLocaleTimeString()+' · 离线状态面板';
  }
  if(typeof module!=='undefined'&&module.exports) module.exports={parse,run,render};
  if(!scope.document) return;
  const document=scope.document,id=document.querySelector('meta[name="module-id"]').content;
  if(!['hyperos_avd_native_compat','hyperos_avd_app_compat'].includes(id)) return;
  document.getElementById('family').textContent=id==='hyperos_avd_native_compat'?'系统兼容模块':'应用兼容模块';
  const button=document.getElementById('refresh');let busy=false;
  async function refresh() {
    if(busy)return;busy=true;button.disabled=true;
    try { render(parse(await run('/system/bin/sh /data/adb/modules/'+id+'/webui-status.sh',scope.ksu)),document); }
    catch(error) { document.getElementById('message').textContent=error.message; document.getElementById('module-state').textContent='读取失败'; }
    finally {busy=false;button.disabled=false;}
  }
  button.addEventListener('click',refresh);refresh();
})(typeof window==='undefined'?globalThis:window);
