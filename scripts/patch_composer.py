"""Keep ranchu HWC plane alpha separate from layer brightness."""
import hashlib

NATIVE = '/vendor/bin/hw/android.hardware.graphics.composer3-service.ranchu'
BEFORE = 'e5c002c43532b16250908eb125b1a43f40aade6e783800f82744f6528b47c8ea'
AFTER = 'b7fd0aa525ebcbf223a400452c0721e75d02a43a298094c4acc47d3053b26ad5'
SITES = ((0x3a178, bytes.fromhex('0028211e'), bytes.fromhex('0028201e')),)
MANIFEST = {'revision': 1, 'native': NATIVE, 'native_sha256': AFTER,
            'plane_alpha': 'unchanged', 'hardware_composition': True}


def patch(data):
    """Fix only the verified ARM64 HostFrameComposer alpha instruction.

    This vendor passes (planeAlpha + brightness) / 2 to ComposeLayer.alpha.
    With default brightness=1, fading a black dim layer to alpha=0 still
    leaves it 50% opaque on the physical display. Virtual display recording
    uses RenderEngine instead, so an Android screenrecord hides the defect.

    The preceding instruction halves planeAlpha. Replace fadd s0,s0,s1
    with fadd s0,s0,s0 to forward the original planeAlpha, retaining HWC,
    layer colors, blend modes and all animation timing.
    """
    checksum = hashlib.sha256(data).hexdigest()
    if checksum == AFTER:
        return data
    if checksum != BEFORE:
        raise RuntimeError('Unsupported ranchu composer SHA-256: ' + checksum)
    result = bytearray(data)
    offset, before, after = SITES[0]
    if result[offset:offset + 4] != before:
        raise RuntimeError('Unexpected ranchu composer alpha instruction.')
    result[offset:offset + 4] = after
    if len(result) != len(data) or hashlib.sha256(result).hexdigest() != AFTER:
        raise RuntimeError('Ranchu composer patch checksum mismatch.')
    return bytes(result)


def build_vendor(source, destination, work):
    """Recompress the patched ELF while retaining all vendor inode metadata."""
    from build_image import erofs
    from erofs_image import build
    path = NATIVE.removeprefix('/vendor/')
    data = patch(erofs(source, '/' + path))
    build(destination, [('', source)], work,
          {path: (data, 0o755, 'u:object_r:hal_graphics_composer_default_exec:s0')},
          preserve_replacement_metadata=True)
    if hashlib.sha256(erofs(destination, '/' + path)).hexdigest() != AFTER:
        raise RuntimeError('Baked ranchu composer checksum mismatch.')
    return dict(MANIFEST)
