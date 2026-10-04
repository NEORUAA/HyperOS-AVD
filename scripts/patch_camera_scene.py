"""Enable Xiaomi camera metadata on the pinned AVD virtual scene provider."""
import hashlib
from pathlib import Path
import struct
import subprocess

from common import REPO_ROOT, sha256

PROVIDER = '/vendor/bin/hw/android.hardware.camera.provider.ranchu'
LIBRARY = '/vendor/lib64/hw/libhyperos_avd_scene.so'
BEFORE = 'dea7b1a0623af97235c7b21577f53f4e4babe1e7d7856e69c3f98fa557b0e564'
AFTER = '4f43f58fc841af0056df9a41891451bd6331384cd123249f0bfbcc3ae1ce3e7a'
CPP = '2267f93b8b3c9d1967f1833d5f71c7312213c43bb291250cf772800763037fb9'


def patch(data):
    """Retain all ABI addresses and RELRO while adding the private dependency.

    The main() camera ID base is 10 in AOSP; Xiaomi requires rear ID 0.
    Reuse the NOTE program header for an appended read-only string table,
    and the optional DT_DEBUG entry for DT_NEEDED. Notes remain present in
    the original load segment. No code/data addresses or relocations move.
    """
    checksum = hashlib.sha256(data).hexdigest()
    if checksum == AFTER:
        return data
    if checksum != BEFORE:
        raise RuntimeError('Unsupported ranchu camera provider SHA-256: ' + checksum)
    result = bytearray(data)
    if result[0x34cd4:0x34cd8] != bytes.fromhex('40018052'):
        raise RuntimeError('Unexpected ranchu camera ID instruction.')
    result[0x34cd4:0x34cd8] = bytes.fromhex('00008052')
    phoff = struct.unpack_from('<Q', result, 32)[0]
    phsize, phnum = struct.unpack_from('<HH', result, 54)
    if (phoff, phsize, phnum) != (64, 56, 12):
        raise RuntimeError('Unexpected ranchu camera ELF program headers.')
    slot = phoff + (phnum - 1) * phsize
    if struct.unpack_from('<I', result, slot)[0] != 4:
        raise RuntimeError('Missing pinned ranchu NOTE header.')
    strings = bytes(result[0x209c:0x209c + 7542])
    dependency = LIBRARY.encode() + b'\0'
    end = (len(result) + 16383) & ~16383
    if end != 0x54000:
        raise RuntimeError('Unexpected ranchu camera ELF size.')
    result.extend(bytes(end - len(result)))
    result.extend(strings + dependency)
    struct.pack_into('<IIQQQQQQ', result, slot, 1, 4, end, end, end,
                     len(strings + dependency), len(strings + dependency), 16384)
    changes = {5: (0x209c, 5, end), 10: (7542, 10, len(strings + dependency)),
               21: (0, 1, len(strings))}
    for index in range(44):
        offset = 0x4ca98 + index * 16
        tag, value = struct.unpack_from('<QQ', result, offset)
        if tag in changes:
            expected, new_tag, new_value = changes.pop(tag)
            if value != expected:
                raise RuntimeError('Unexpected ranchu dynamic entry.')
            struct.pack_into('<QQ', result, offset, new_tag, new_value)
    if changes:
        raise RuntimeError('Missing ranchu dynamic entries.')
    shoff = struct.unpack_from('<Q', result, 40)[0]
    shsize, shnum = struct.unpack_from('<HH', result, 58)
    for index in range(shnum):
        offset = shoff + index * shsize
        if struct.unpack_from('<Q', result, offset + 24)[0] == 0x209c:
            struct.pack_into('<QQQ', result, offset + 16, end, end, len(strings + dependency))
    if hashlib.sha256(result).hexdigest() != AFTER:
        raise RuntimeError('Ranchu scene provider patch checksum mismatch.')
    return bytes(result)


def build_library(sdk, work, cpp):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    if hashlib.sha256(cpp).hexdigest() != CPP:
        raise RuntimeError('Unsupported vendor libc++ for scene camera ABI.')
    original = work / 'libc++.so'
    original.write_bytes(cpp)
    candidates = list((Path(sdk) / 'ndk').glob(
        '*/toolchains/llvm/prebuilt/darwin-*/bin/aarch64-linux-android36-clang++'))
    if not candidates:
        raise RuntimeError('Install Android NDK with API 36 to build the scene camera bridge.')
    compiler = max(candidates, key=lambda path: tuple(int(v) for v in path.parents[5].name.split('.')))
    library = work / 'libhyperos_avd_scene.so'
    subprocess.run([str(compiler), str(REPO_ROOT / 'native/camera_scene_roles.cpp'),
                    '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-nostdlib++', str(original), '-llog', '-ldl',
                    '-Wl,-z,max-page-size=16384', '-Wl,-soname,libhyperos_avd_scene.so',
                    '-o', str(library)], check=True)
    return library


def build_vendor(source, destination, work, sdk):
    """Bake the bridge, preserving every existing vendor inode's metadata."""
    from build_image import erofs
    from erofs_image import build
    work = Path(work)
    library = build_library(sdk, work / 'native', erofs(source, '/lib64/libc++.so'))
    intermediate = work / 'provider.img'
    build(intermediate, [('', source)], work / 'provider-tree',
          {PROVIDER.removeprefix('/vendor/'): (
              patch(erofs(source, PROVIDER.removeprefix('/vendor'))),
              0o755, 'u:object_r:hal_camera_default_exec:s0')},
          preserve_replacement_metadata=True)
    # A separate addition keeps the strict existing-inode replacement guard.
    build(destination, [('', intermediate)], work / 'library-tree',
          {LIBRARY.removeprefix('/vendor/'): (
              library.read_bytes(), 0o644, 'u:object_r:vendor_file:s0')})
    if hashlib.sha256(erofs(destination, PROVIDER.removeprefix('/vendor'))).hexdigest() != AFTER:
        raise RuntimeError('Baked scene provider verification failed.')
    if hashlib.sha256(erofs(destination, LIBRARY.removeprefix('/vendor'))).hexdigest() != sha256(library):
        raise RuntimeError('Baked scene bridge verification failed.')
    return {'revision': 1, 'provider_sha256': AFTER, 'library_sha256': sha256(library),
            'back_camera': 'virtualscene', 'front_camera': 'emulated',
            'back_camera_id': '0', 'deferred_preview_size': '1024x768'}
