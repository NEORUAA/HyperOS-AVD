#!/usr/bin/env python3
"""Patch pinned hongkong OS4.0.18 boot services without changing app identity."""
import hashlib
from pathlib import Path
import re
import subprocess
import struct
import tempfile
import zipfile

from common import ROOT, sha256, sdk_path
from patch_gnss import ARTIFACTS, java

TARGETS = {
    'qualcomm': ('/system_ext/priv-app/com.qualcomm.location/com.qualcomm.location.apk',
                 '831b2995583514d09a5e040e74690171a91865e439831e3b0770d8f2c4858e78', 'classes.dex'),
    'registration': ('/product/priv-app/AutoRegistration/AutoRegistration.apk',
                     '780c62e43e985467905550c922eee563f110fb98fac244a3995570150b0c5770', 'classes.dex'),
    'services': ('/system/framework/services.jar',
                 '37cf6a7b69d9849e2f13142835218ffe250d9fc3dc51f3ecbe4b342834ccb536', 'classes.dex'),
    'miui-services': ('/system_ext/framework/miui-services.jar',
                      'f9975afd7cccee7b776103198c2878e424ecadfa42aeddff57899924a9cc4ec3', 'classes2.dex'),
}
AFTER = {
    'qualcomm': '66dce6b9f484ee6d3651d3577f536d57f12aef0adb4a1940ee6fa0118eeb5134',
    'registration': '2b6c3ddad65ac3c6f65667631c2ac75fd8724a1092250d809bb4d3e7d7d8b1f9',
    'services': '3b1c22fbae3262187af8e040e02962edd7f80ae7369d9bcd724457384a4fd6b1',
    'miui-services': '205bbc10beab0fcd668c79dc2b795c6115fe4b3eca1e3b8eb75f99f5f9702f56',
}
PROBE_SHA256 = 'af7486c1f98e01641c10d5089fd46348ce612c6fbf15c1581f50a6bacb6f402a'
PROBE_SOURCE_SHA256 = 'd5a1290eaeb63b1f3f9e5b2f72b12dd14239ba4441604d4dd1bff85d895d2fa0'
BOOT_SEPOLICY = b'''
; Fixed capability helper remains enforcing; grant only its observed interfaces.
(allow init su (process (transition)))
(allow su shell_exec (file (entrypoint)))
(allow su default_prop (file (read open getattr map)))
(allow su system_file (file (read open getattr map execute execute_no_trans)))
(allow su system_lib_file (file (read open getattr map execute)))
(allow su toolbox_exec (file (read open getattr map execute execute_no_trans)))
(allow su property_socket (sock_file (write)))
(allow su su (unix_stream_socket (create connect write read getattr getopt setopt shutdown)))
(allow su init (unix_stream_socket (connectto)))
(allow su su (netlink_socket (create)))
(allow su system_prop (property_service (set)))
(allow su ctl_stop_prop (property_service (set)))
(allow su servicemanager (binder (call)))
(allow su hal_gnss_service (service_manager (find)))
'''

BOOT_INIT = b'''
# Avoid restarting hardware-only daemons on kernels without their interfaces.
service hyperos-kernel-services /system/bin/sh /system/etc/hyperos-kernel-services.sh
    user root
    group root system
    seclabel u:r:su:s0
    disabled
    oneshot

on post-fs-data
    setprop sys.hyperos_avd.millet_supported 1
    start hyperos-kernel-services

on property:ro.persistent_properties.ready=true
    start hyperos-kernel-services

on property:sys.boot_completed=1
    start hyperos-kernel-services
'''



def zip_directory(data):
    """Validate a single-disk non-ZIP64 archive before locating its directory."""
    end = data.rfind(b'PK\x05\x06', max(0, len(data) - 65557))
    if end < 0 or end + 22 > len(data):
        raise RuntimeError('Missing ZIP end record.')
    disk, cd_disk, count_disk, count, size, offset, comment = struct.unpack_from('<4H2IH', data, end + 4)
    if (disk or cd_disk or count_disk != count or count == 65535
            or end + 22 + comment != len(data) or offset + size != end
            or data[offset:offset + 4] != b'PK\x01\x02'):
        raise RuntimeError('Unsupported ZIP directory layout.')
    return end, offset


def signing_block(data):
    """Extract the original signer metadata from a pinned system APK."""
    _, directory = zip_directory(data)
    if data[directory - 16:directory] != b'APK Sig Block 42':
        raise RuntimeError('Pinned system APK has no signing block.')
    length = struct.unpack_from('<Q', data, directory - 24)[0]
    start = directory - length - 8
    if length < 24 or start < 0 or struct.unpack_from('<Q', data, start)[0] != length:
        raise RuntimeError('Invalid APK signing block.')
    return data[start:directory]


def retain_system_signer(original, modified):
    """Retain identity for trusted read-only system scanning, not APK installation.

    Android collects certificates without checking content digests on trusted
    system partitions. Modified bytecode invalidates the original content
    digest; this output must never be advertised as a newly signed APK. Keep
    the complete original v2/v3 metadata and leave user APK verification intact.
    """
    block = signing_block(original)
    end, directory = zip_directory(modified)
    if modified[directory - 16:directory] == b'APK Sig Block 42':
        raise RuntimeError('Modified ZIP unexpectedly contains signing metadata.')
    result = bytearray(modified[:directory] + block + modified[directory:])
    struct.pack_into('<I', result, end + len(block) + 16, directory + len(block))
    if signing_block(result) != block:
        raise RuntimeError('System signer metadata changed.')
    return bytes(result)


def image_replacements(inputs, work):
    """Bake fixes so a factory reset retains them without a userdata module."""
    if set(inputs) != set(TARGETS):
        raise RuntimeError('Expected all pinned boot service inputs.')
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    # Refuse every input before creating any patched output.
    for name, data in inputs.items():
        if hashlib.sha256(data).hexdigest() != TARGETS[name][1]:
            raise RuntimeError('Unsupported image boot service: ' + name)
    replacements = {}
    for name, data in inputs.items():
        suffix = '.apk' if name in ('qualcomm', 'registration') else '.jar'
        source = work / (name + '-original' + suffix)
        source.write_bytes(data)
        output = patch(name, source, work / (name + '-fixed' + suffix))
        replacements[TARGETS[name][0].lstrip('/')] = (
            output.read_bytes(), 0o644, 'u:object_r:system_file:s0')
    replacements['system/bin/hyperos_kernel_probe'] = (
        kernel_probe(work / 'kernel-probe').read_bytes(), 0o755, 'u:object_r:system_file:s0')
    script = Path(__file__).resolve().parent.parent / 'config/check_kernel_services.sh'
    replacements['system/etc/hyperos-kernel-services.sh'] = (
        script.read_bytes(), 0o755, 'u:object_r:system_file:s0')
    return replacements, {'schema': 1, 'firmware': 'OS4.0.18.0.XFRCNXM',
                          'targets': {name: {'path': path, 'before': before, 'after': AFTER[name]}
                                      for name, (path, before, _) in TARGETS.items()},
                          'probe_sha256': PROBE_SHA256}


def kernel_probe(destination):
    """Build the source-pinned probe with the tested NDK toolchain."""
    source = Path(__file__).resolve().parent.parent / 'native/kernel_probe.c'
    if sha256(source) != PROBE_SOURCE_SHA256:
        raise RuntimeError('Unsupported kernel capability probe source.')
    clang = sdk_path() / 'ndk/29.0.14206865/toolchains/llvm/prebuilt/darwin-x86_64/bin/aarch64-linux-android26-clang'
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(clang), '-O2', '-Wall', '-Wextra', '-Werror', '-fPIE', '-pie',
                    str(source), '-ldl', '-o', str(destination)], check=True, capture_output=True)
    if sha256(destination) != PROBE_SHA256:
        raise RuntimeError('Kernel capability probe output mismatch.')
    return destination


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError('Unexpected boot service bytecode anchor.')
    return text.replace(old, new, 1)


def edit_method(text, signature, expected, edit):
    matches = list(re.finditer(r'^\.method [^\n]*' + re.escape(signature)
                              + r'\n.*?^\.end method', text, re.M | re.S))
    if len(matches) != 1:
        raise RuntimeError('Unexpected boot service method: ' + signature)
    match = matches[0]
    body = match.group()
    if hashlib.sha256(body.encode()).hexdigest() != expected:
        raise RuntimeError('Boot service method hash mismatch: ' + signature)
    return text[:match.start()] + edit(body) + text[match.end():]


def method_code(text, signature):
    """Compare round-trip instructions while ignoring renamed offset labels."""
    bodies = re.findall(r'^\.method [^\n]*' + re.escape(signature)
                        + r'\n.*?^\.end method', text, re.M | re.S)
    if len(bodies) != 1:
        raise RuntimeError('Missing round-trip service method: ' + signature)
    labels, statements = {}, []
    lines = [re.sub(r'^(goto/(?:16|32)|const-string/jumbo) ',
                    lambda match: 'goto ' if match.group(1).startswith('goto') else 'const-string ',
                    line.strip()) for line in bodies[0].splitlines()
             if line.strip() and not line.strip().startswith(('.line ', '#'))]
    for line in lines:
        if re.fullmatch(r':[a-zA-Z_][a-zA-Z_0-9]*', line):
            labels[line] = len(statements)
        else:
            statements.append(line)
    def label(match):
        if match.group() not in labels:
            raise RuntimeError('Unresolved service branch: ' + match.group())
        return ':instruction_' + str(labels[match.group()])
    pattern = r'(?<![\w$])(?:' + '|'.join(map(re.escape, labels)) + r')\b'
    return re.sub(pattern, label, '\n'.join(statements)) if labels else '\n'.join(statements)


def qualcomm_missing_hal(body):
    # Keep real HAL and RemoteException paths intact. The constructor already
    # checks a null extension before installing its emergency callback.
    old = '''    :cond_2f
    new-instance v1, Ljava/lang/RuntimeException;

    const-string v2, "gnssService is null!"

    invoke-direct {v1, v2}, Ljava/lang/RuntimeException;-><init>(Ljava/lang/String;)V

    throw v1'''
    return replace_once(body, old, '''    :cond_2f
    const-string v1, "EsStatusReceiverAidlClient"

    const-string v2, "Optional Qualcomm GNSS extension is unavailable"

    invoke-static {v1, v2}, Landroid/util/Log;->i(Ljava/lang/String;Ljava/lang/String;)I

    return-void''')


def qualcomm_declared_hal(body):
    # The Xiaomi framework waits/retries even for an undeclared vendor service.
    # Release existing waiters immediately when this HAL is absent from VINTF.
    return replace_once(body, '''    .registers 6

    .line 46''', '''    .registers 6

    const-string v0, "vendor.qti.gnss.ILocAidlGnss/default"

    invoke-static {v0}, Landroid/os/ServiceManager;->isDeclared(Ljava/lang/String;)Z

    move-result v0

    if-nez v0, :hyos_declared_hal

    sget-object v0, Lcom/qualcomm/location/idlclient/LocIDLClientBase;->mCountDownLatch:Ljava/util/concurrent/CountDownLatch;

    invoke-virtual {v0}, Ljava/util/concurrent/CountDownLatch;->countDown()V

    return-void

    :hyos_declared_hal
    .line 46''')


def qualcomm_disconnected_hal(body):
    return replace_once(body, '''    iget-object v0, p0, Lcom/qualcomm/location/izat/esstatusreceiver/EsStatusReceiver$EsStatusReceiverAidlClient;->mLocAidlEsStatusReceiver:Lvendor/qti/gnss/ILocAidlEsStatusReceiver;

    iget-object v1''', '''    iget-object v0, p0, Lcom/qualcomm/location/izat/esstatusreceiver/EsStatusReceiver$EsStatusReceiverAidlClient;->mLocAidlEsStatusReceiver:Lvendor/qti/gnss/ILocAidlEsStatusReceiver;

    if-eqz v0, :goto_27

    iget-object v1''')


def registration_null_result(body):
    # Preserve the existing dual-SIM FF fallback, then normalize a missing
    # subscription result just like the original literal "null" response.
    return replace_once(body, '''    :cond_14
    const-string p0, "null"''', '''    :cond_14
    if-nez p2, :hyos_result_present

    const-string p2, ""

    :hyos_result_present
    const-string p0, "null"''')


def provider_authority_guard(body):
    # ComponentResolver strips conflicting authorities. Such a component
    # cannot be installed; retain its valid owner and every valid provider.
    return replace_once(body, '''    aget-object v8, v3, v7

    .line 1605''', '''    aget-object v8, v3, v7

    iget-object v9, v8, Landroid/content/pm/ProviderInfo;->authority:Ljava/lang/String;

    invoke-static {v9}, Landroid/text/TextUtils;->isEmpty(Ljava/lang/CharSequence;)Z

    move-result v9

    if-nez v9, :goto_9e

    .line 1605''')


def thermal_node_guard(body):
    # Return the monitor's own INVALID_TEMPERATURE, never a fabricated reading.
    # A present node still uses the original parsing, close and error handling.
    return replace_once(body, '''    .registers 6

    .line 171''', '''    .registers 6

    new-instance v0, Ljava/io/File;

    const-string v1, "/sys/class/thermal/thermal_message/board_sensor_temp"

    invoke-direct {v0, v1}, Ljava/io/File;-><init>(Ljava/lang/String;)V

    invoke-virtual {v0}, Ljava/io/File;->exists()Z

    move-result v0

    if-eqz v0, :goto_39

    .line 171''')


def millet_kernel_guard(body):
    match = re.search(r'    const-string/jumbo (v\d+), "sys.millet.monitor"\n\n'
                      r'    const-string (v\d+), "1"\n\n'
                      r'    invoke-static \{\1, \2\}, Landroid/os/SystemProperties;->set'
                      r'\(Ljava/lang/String;Ljava/lang/String;\)V', body)
    if not match or body.count('"sys.millet.monitor"') != 1:
        raise RuntimeError('Unexpected Millet startup path.')
    name, value = match.groups()
    guard = f'''    const-string {name}, "sys.hyperos_avd.millet_supported"

    const/4 {value}, 0x1

    invoke-static {{{name}, {value}}}, Landroid/os/SystemProperties;->getBoolean(Ljava/lang/String;Z)Z

    move-result {name}

    if-eqz {name}, :hyos_skip_millet_start

'''
    return body[:match.start()] + guard + match.group() + '\n\n    :hyos_skip_millet_start' + body[match.end():]


EDITS = {
    'qualcomm': [('com/qualcomm/location/izat/esstatusreceiver/EsStatusReceiver$EsStatusReceiverAidlClient.smali',
                  'getEsStatusReceiverIface()V',
                  '3e75dd15740ecbb0b7f8eccbba0759ef4d7f3bb99830fb4c7a78a382d639b002', qualcomm_missing_hal),
                 ('com/qualcomm/location/idlclient/LocIDLClientBase$1.smali', 'run()V',
                  'd141e967dfb07c078f7a9da0d29142eaaf20bc1369257a0944c2454c9bdb8517', qualcomm_declared_hal),
                 ('com/qualcomm/location/izat/esstatusreceiver/EsStatusReceiver$EsStatusReceiverAidlClient.smali',
                  'onServiceDied()V', '7a3ddbf4924b1c1da1850c0a291b15ead123bfe88abecd0a7bff85af533f9a6d',
                  qualcomm_disconnected_hal)],
    'registration': [('j1/b.smali', 's(Landroid/os/Message;Ljava/lang/String;)V',
                      '227ed448693b3d8a6d71c4ab2804406e2e831c45879f3a27c4ea437e96cc004b', registration_null_result)],
    'services': [('com/android/server/am/ContentProviderHelper.smali',
                  'lambda$installEncryptionUnawareProviders$3(Lcom/android/server/am/ProcessRecord;Ljava/lang/String;)V',
                  'dc6aa80b2deb8deb76755d4d8f10b4103e22c83c50bd3dc2b1aa76d0da08b2e9', provider_authority_guard)],
    'miui-services': [('com/miui/server/mirim/scenario/customscenario/resourceredline/ThermalResourceMonitor.smali',
                       'readTemperature()I',
                       'c105328e13bc152959dadae0d1746e92d7974c95f83ea04fd0a8f8e91e984a2d', thermal_node_guard),
                      ('com/miui/server/greeze/GreezeManagerService.smali', 'init(Landroid/content/Context;)V',
                       'aff6d59287d2c8cdec413ee7fd6cf5b58c468f353111b85bea7f1c441d08e610', millet_kernel_guard),
                      ('com/miui/server/greeze/GreezeManagerService$AlarmListener.smali', 'onAlarm()V',
                       'f6822634b3d576d90aec136074e66b2630877e9be5abf11979889b985850ab86', millet_kernel_guard)],
}


def patch(kind, source, destination):
    source, destination = Path(source), Path(destination)
    _, expected, dex = TARGETS[kind]
    if sha256(source) != expected:
        raise RuntimeError('Unsupported boot service input: ' + kind)
    cache = ROOT / 'tools/smali'
    for name, (_, checksum) in ARTIFACTS.items():
        if not (cache / (name + '.jar')).is_file() or sha256(cache / (name + '.jar')) != checksum:
            raise RuntimeError('Verified smali tool is missing: ' + name)
    folder = ROOT / 'work/boot-service-patch'
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=kind + '-', dir=folder) as temp:
        temp = Path(temp)
        command = [java(), '-cp', str(cache / '*')]
        with zipfile.ZipFile(source) as archive:
            if archive.namelist().count(dex) != 1:
                raise RuntimeError('Missing or duplicate service dex.')
            (temp / 'input.dex').write_bytes(archive.read(dex))
            subprocess.run(command + ['org.jf.baksmali.Main', 'disassemble', str(temp / 'input.dex'),
                                      '-o', str(temp / 'smali')], check=True, capture_output=True)
            for relative, signature, checksum, edit in EDITS[kind]:
                path = temp / 'smali' / relative
                path.write_text(edit_method(path.read_text(), signature, checksum, edit))
            subprocess.run(command + ['org.jf.smali.Main', 'assemble', '-j', '1', str(temp / 'smali'),
                                      '-o', str(temp / 'fixed.dex')], check=True, capture_output=True)
            # Validate the emitted dex independently before writing any output.
            subprocess.run(command + ['org.jf.baksmali.Main', 'disassemble', str(temp / 'fixed.dex'),
                                      '-o', str(temp / 'verify')], check=True, capture_output=True)
            for relative, signature, _, _ in EDITS[kind]:
                expected_code = method_code((temp / 'smali' / relative).read_text(), signature)
                emitted_code = method_code((temp / 'verify' / relative).read_text(), signature)
                if expected_code != emitted_code:
                    raise RuntimeError('Service instructions changed on assembly: ' + signature)
            with zipfile.ZipFile(temp / 'fixed.zip', 'w') as output:
                for entry in archive.infolist():
                    output.writestr(entry, (temp / 'fixed.dex').read_bytes() if entry.filename == dex
                                    else archive.read(entry))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == '.apk':
            subprocess.run([str(sdk_path() / 'build-tools/37.0.0/zipalign'), '-P', '16', '-f', '4',
                            str(temp / 'fixed.zip'), str(destination)], check=True, capture_output=True)
            destination.write_bytes(retain_system_signer(source.read_bytes(), destination.read_bytes()))
        else:
            destination.write_bytes((temp / 'fixed.zip').read_bytes())
        with zipfile.ZipFile(source) as before, zipfile.ZipFile(destination) as after:
            if before.namelist() != after.namelist() or after.testzip() is not None:
                raise RuntimeError('Unexpected boot service archive structure.')
            for entry in before.namelist():
                if entry != dex and before.read(entry) != after.read(entry):
                    raise RuntimeError('Unexpected unrelated service entry change: ' + entry)
        if sha256(destination) != AFTER[kind]:
            raise RuntimeError('Boot service output checksum mismatch: ' + kind)
    return destination
