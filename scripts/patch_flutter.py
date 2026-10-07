#!/usr/bin/env python3
"""Patch the verified HyperOS 4 Impeller engines for the ARM64 AVD."""
import argparse
import hashlib
from pathlib import Path

# Offsets are file offsets, not virtual addresses. Each profile is pinned to the
# complete input and output ELF hashes; never pattern-patch an unknown update.
PROFILES = {
    '7f88d7f4d9a464fdd56fb255642c9d300f28f50272ccf5fd31f082c1a52e17f4': {
        'name': 'system-hongkong-4.0.18',
        'output': '439bb47881f64431ba43dc4de5788e7f0a3a0c4e78102b47cc8f0daa6229b91b',
        'shadow_sites': (0xd3d9c0, 0xd3e61c, 0xd3e630),
        # Re-audited against this OTA's .gnu_debugdata, not a global offset.
        # GetEnabledDeviceFeatures, PipelineVK::Create, both HostBuffer::Emplace
        # overloads and MiDispersionShadowContents::Render retain their layout.
        # RenderPassVK's constructor grew; its clear and default viewport sites
        # moved independently. Both viewports load [0,1] at rodata 0x125100;
        # the existing [1,0] pair is at 0x125110, sixteen bytes later.
        # The 24-byte trampoline uses executable assembly padding between the
        # aes_gcm_dec_kernel RET and bn_mul_mont_words, outside both symbols.
        'sites': [
            (0xdae1c8, '184a9e52', '184a8152'),
            (0xdae1d8, 'f873a772', '9873a772'),
            (0xdae22c, 'ffbb02b9', 'ff5f01f9'),
            (0xdae370, '1f0500f9', '1f2003d5'),
            (0xdae3e4, 'ff5b01b9', 'ffaf00f9'),
            (0xdae524, '60010054', '1f2003d5'),
            (0xdae550, 'e9830091', 'f6ffff17'),
            (0xdd97e8, 'ac596cb8', '6c008052'),
            (0xde01a8, '0040621e', '00102e1e'),
            (0xde0630, '0a8140fd', '0a8940fd'),
            (0xde25d0, '008140fd', '008940fd'),
            (0x68a968, '000000000000000000000000000000000000000000000000', 'f30300aa304440f9103e009110ee7c92304400f9c0035fd6'),
            (0xb436d0, 'f30300aa', 'a61ced97'),
            (0xb43c64, 'f30300aa', '411bed97'),
            (0xd3d9c0, '3800c0f2', 'b800c0f2'),
            (0xd3e61c, '2009a00e', '2021022e'),
            (0xd3e630, '4108a00e', '4120092e'),
        ],
    },
    '9caf8bd3413b3093ae0855f86369f31ddf45b46c7ddbaa4c2f656e4f67bb5009': {
        'name': 'tablet-yingtian',
        'output': '30ab4c6adb2f0c6193e503a97f9e5f6407e902677c54db6f3896426812ddd509',
        'shadow_sites': (0xc9ecc4, 0xc9f794, 0xc9f7a8),
        # Audited against this engine's .gnu_debugdata function symbols.
        # The 24-byte trampoline occupies assembly alignment padding between
        # aes_gcm_dec_kernel's RET and bn_mul_mont_words, outside both symbols.
        # The viewport's reversed [1,0] pair is at 0x123790, not 0x123788.
        'sites': [
            (0xd0ded4, '184a9e52', '184a8152'),
            (0xd0dee4, 'f873a772', '9873a772'),
            (0xd0df38, 'ffbb02b9', 'ff5f01f9'),
            (0xd0e07c, '1f0500f9', '1f2003d5'),
            (0xd0e0f0, 'ff5b01b9', 'ffaf00f9'),
            (0xd0e230, '60010054', '1f2003d5'),
            (0xd0e25c, 'e9830091', 'f6ffff17'),
            (0xd38f6c, 'ac596cb8', '6c008052'),
            (0xd3f838, '0040621e', '00102e1e'),
            (0xd3fc84, '0ac143fd', '0ac943fd'),
            (0xd41b5c, '00c143fd', '00c943fd'),
            (0x613ca8, '000000000000000000000000000000000000000000000000', 'f30300aa304440f9103e009110ee7c92304400f9c0035fd6'),
            (0xac3db8, 'f30300aa', 'bc3fed97'),
            (0xac434c, 'f30300aa', '573eed97'),
            (0xc9ecc4, '3800c0f2', 'b800c0f2'),
            (0xc9f794, '2009a00e', '2021022e'),
            (0xc9f7a8, '4108a00e', '4120092e'),
        ],
    },
    '71caea24a7fec06ae7c1b7cdb93c99f45288154a9ca21bb634d8181a97dcef62': {
        'name': 'system-v3',
        'output': 'f3d11ed83da4840c2ab31462529044ea5b2a95b08f18b8114618bc8fbd6759fb',
        'legacy': ('bb7b54865f58607ea38cbc4483c6222cd6c08a08f8538a8a16170e489220d597',
                   '199531724621fb8281fce8b3ed9a848148749df3d3ec0c8ea72f2816f318ee3f'),
        'shadow_sites': (0xccf0b4, 0xccfd10, 0xccfd24),
        'sites': [
            (0xd3efb4, '184a9e52', '184a8152'),
            (0xd3efc4, 'f873a772', '9873a772'),
            (0xd3f018, 'ffbb02b9', 'ff5f01f9'),
            (0xd3f15c, '1f0500f9', '1f2003d5'),
            (0xd3f1d0, 'ff5b01b9', 'ffaf00f9'),
            (0xd3f310, '60010054', '1f2003d5'),
            (0xd3f33c, 'e9830091', 'f6ffff17'),
            (0xd6a5d4, 'ac596cb8', '6c008052'),
            (0xd70dac, '0040621e', '00102e1e'),
            (0xd711f8, '0ae141fd', '0ae941fd'),
            (0xd7317c, '00e141fd', '00e941fd'),
            (0x630968, '000000000000000000000000000000000000000000000000', 'f30300aa304440f9103e009110ee7c92304400f9c0035fd6'),
            (0xae50bc, 'f30300aa', '2b2eed97'),
            (0xae5650, 'f30300aa', 'c62ced97'),
            # Keep the analytic dispersion shader; use a perimeter-ordered fan
            # for its four-corner quad instead of the problematic AVD strip.
            (0xccf0b4, '3800c0f2', 'b800c0f2'),
            (0xccfd10, '2009a00e', '2021022e'),
            (0xccfd24, '4108a00e', '4120092e'),
        ],
    },
    '78769d2aad2dc2f2641b221dd3226e11051642d987572a3eef0e893d2df68cda': {
        'name': 'launcher-v5',
        'output': '6be8fb80d4682a12337cdc1e2c334c91a20c45d4acd7f55db4c7bb3e12aa0617',
        'legacy': ('2a3821e761e4a86a1d0c332587d718bb12b185ff54e01637f5490f100176a4f0',),
        'sites': [
            (0xd1d0a4, '184a9e52', '184a8152'),
            (0xd1d0b4, 'f873a772', '9873a772'),
            (0xd1d108, 'ffbb02b9', 'ff5f01f9'),
            (0xd1d24c, '1f0500f9', '1f2003d5'),
            (0xd1d2c0, 'ff5b01b9', 'ffaf00f9'),
            (0xd1d400, '60010054', '1f2003d5'),
            (0xd1d42c, 'e9830091', 'f6ffff17'),
            (0xd48540, 'ac596cb8', '6c008052'),
            (0xd4ed18, '0040621e', '00102e1e'),
            (0xd4f164, '0a7946fd', '0a8146fd'),
            (0xd510e8, '007946fd', '008146fd'),
            (0x61e0e8, '000000000000000000000000000000000000000000000000', 'f30300aa304440f9103e009110ee7c92304400f9c0035fd6'),
            (0xad09b4, 'f30300aa', 'cd35ed97'),
            (0xad0f48, 'f30300aa', '6834ed97'),
        ],
    },
    'd67e5c634800a97cd853fd425ded4752c43c2c879fe7269c8b9ffab6c5104836': {
        'name': 'weather',
        'output': '109fcc92d722321481e62d0bc5488b09d2e5ecbe3241b555d86fe681ddd84083',
        'legacy': ('e55b48ead0f72f66c282b2ed8056992ed1214a87e09f56b375973f24fd97d15c',),
        'sites': [
            (0xe98434, '184a9e52', '184a8152'),
            (0xe98444, 'f873a772', '9873a772'),
            (0xe98498, 'ffbb02b9', 'ff5f01f9'),
            (0xe985e8, '090500f9', '1f2003d5'),
            (0xe9865c, 'ff5b01b9', 'ffaf00f9'),
            (0xe987a8, 'c0000054', '1f2003d5'),
            (0xe987c0, 'e9830091', 'fbffff17'),
            (0xec0d1c, '287968b8', '68008052'),
            (0xec6e14, '0040621e', '00102e1e'),
            (0xec70f8, '0aa541fd', '0aad41fd'),
            (0xec8bb4, '00a541fd', '00ad41fd'),
            (0x68f9a8, '000000000000000000000000000000000000000000000000', 'f30308aa104440f9103e009110ee7c92104400f9c0035fd6'),
            (0xacd890, 'f30308aa', '4608ef97'),
            (0xacdc24, 'f30308aa', '6107ef97'),
        ],
    },
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def profile(data):
    checksum = digest(data)
    for original, item in PROFILES.items():
        if checksum in (original, item['output'], *item.get('legacy', ())):
            return original, item
    raise RuntimeError('Unsupported Flutter engine SHA-256: ' + checksum)


def patch(data):
    """Keep all effects and stencil clipping; patch only pinned native sites.

    The pinned 2D engine's depth pipelines use GREATER_OR_EQUAL. Reverse them
    to LESS_OR_EQUAL, clear to 1, and reverse BOTH default and explicit Vulkan
    viewports. Missing the default viewport produces intermittent glass holes.

    The ranchu device lacks image-compression-control. Reuse that unused 24-byte
    feature-chain slot for ShaderFloat16Int8: query shaderFloat16, copy its actual
    supported value into device creation, and keep shaderInt8 zero. Preserve
    original SPIR-V, precision, uniform layouts, blur and material shaders.

    Round HostBuffer allocation offsets to 16 bytes before both existing
    alignment paths. The measured AVD minStorageBufferOffsetAlignment is 16;
    caller alignments above 16 are still respected. The trampoline uses only
    scratch x16 and preserves each engine ABI, including its output register.

    The shared v3 engine's dispersion-shadow quad uses a triangle strip. AVD
    captures showed diagonal wedges in Gallery. Select a triangle fan and
    reorder the bottom corners together, preserving the same covered rectangle,
    fragment shader, alpha, blur and glass. Private v5/weather engines retain
    their previous patches.
    """
    original, item = profile(data)
    if digest(data) == item['output']:
        return data
    result = bytearray(data)
    for offset, before, after in item['sites']:
        expected = bytes.fromhex(before)
        replacement = bytes.fromhex(after)
        if result[offset:offset + len(expected)] == replacement:
            continue  # A pinned previous patch needs only the new sites.
        if result[offset:offset + len(expected)] != expected:
            raise RuntimeError(f"Unexpected {item['name']} instruction at {offset:#x}")
        result[offset:offset + len(expected)] = replacement
    if len(result) != len(data) or digest(result) != item['output']:
        raise RuntimeError('Flutter patch output checksum mismatch.')
    return bytes(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    data = patch(args.input.read_bytes())
    args.output.write_bytes(data)
    print(profile(data)[1]['name'] + ': ' + digest(data))


if __name__ == '__main__':
    main()
