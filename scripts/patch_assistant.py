#!/usr/bin/env python3
"""Keep the verified XiaoAI wakeup effect compatible with the AVD GL driver."""
import hashlib
import io
import zipfile

PACKAGE = 'com.miui.voiceassist'
APK = '/product/priv-app/VoiceAssistAndroidT/VoiceAssistAndroidT.apk'
APK_SHA256 = '70b833c96947b17fc58a3cb7d84c06577b97b58798ce3a7d8f9dd585db5d061d'
PAD_APK_SHA256 = 'ea98e6d2fda6096dc93b6c8b33595a3aa0df209f564124e633f210452d4292aa'
PHONE_SOURCE = 'official-hongkong-ota'
PAD_SOURCE = 'official-yingtian-ota'
ENTRY = 'lib/arm64-v8a/libmglnative2.so'
NATIVE = '/product/priv-app/VoiceAssistAndroidT/lib/arm64/libmglnative2.so'
BEFORE = 'a78630eaa5e6991b888c2744e4f01de67e0079948def8cb60434c98130a1773e'
AFTER = '7a7b0b363c4222320c6ff7f741ea1559e9f1f9a7ad9f01b78b98b4dd35445374'
SITES = ((0x55275, b'#version 320 es', b'#version 300 es'),
         # lsl w8, w8, #3 -> mov w8, #8 (EGL_ALPHA_SIZE).
         (0xa017c, bytes.fromhex('08711d53'), bytes.fromhex('08018052')))
MANIFEST = {'revision': 1, 'package': PACKAGE, 'apk': APK,
            'apk_sha256': APK_SHA256, 'native': NATIVE, 'native_sha256': AFTER,
            'glsl': '300 es', 'egl_alpha_bits': 8}


def profile(source=PHONE_SOURCE, firmware=None):
    """Select one audited signed APK without widening the phone default."""
    if source == PHONE_SOURCE:
        if firmware is None:
            return dict(MANIFEST)
        from phone_profile import profile as phone_profile
        verified = phone_profile(firmware['hyperos'])
        if firmware['archive_sha256'] != verified['archive_sha256']:
            raise RuntimeError('Unsupported XiaoAI OTA identity.')
        return {**MANIFEST, 'apk_sha256': verified['pins']['assistant_apk']}
    if source == PAD_SOURCE:
        return {**MANIFEST, 'apk_sha256': PAD_APK_SHA256}
    raise RuntimeError('Unsupported XiaoAI firmware profile.')


def patch(data):
    """Patch only pinned native sites; leave shaders, timing and blending intact.

    The wakeup shader declares GLSL 300, but MGL prepends 320, which the host
    ES 3.0 translator rejects. Its initial EGL config can also lack alpha
    before the transparent swapchain is created. Require eight alpha bits
    for this private engine's configs, including the initial context.
    """
    checksum = hashlib.sha256(data).hexdigest()
    if checksum == AFTER:
        return data
    if checksum != BEFORE:
        raise RuntimeError('Unsupported XiaoAI MGL engine SHA-256: ' + checksum)
    result = bytearray(data)
    for offset, before, after in SITES:
        if result[offset:offset + len(before)] != before:
            raise RuntimeError(f'Unexpected XiaoAI MGL site at {offset:#x}')
        result[offset:offset + len(before)] = after
    if len(result) != len(data) or hashlib.sha256(result).hexdigest() != AFTER:
        raise RuntimeError('XiaoAI MGL patch checksum mismatch.')
    return bytes(result)


def native_from_apk(data, source=PHONE_SOURCE, firmware=None):
    selected = profile(source, firmware)
    if hashlib.sha256(data).hexdigest() != selected['apk_sha256']:
        raise RuntimeError('Unsupported XiaoAI APK; original signature is preserved.')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.namelist().count(ENTRY) != 1:
            raise RuntimeError('Expected exactly one XiaoAI MGL2 engine.')
        return patch(archive.read(ENTRY))


def image_replacements(apk, source=PHONE_SOURCE, firmware=None):
    """Add the preferred external native library beside the unchanged APK."""
    return {NATIVE.lstrip('/'): (native_from_apk(apk, source=source, firmware=firmware), 0o644,
                                'u:object_r:system_lib_file:s0')}, profile(source, firmware)
