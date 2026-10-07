"""Exercise the native byte conversion without loading or modifying an AVD."""
import ctypes
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class PadCameraYuvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('clang')
        if not compiler:
            raise unittest.SkipTest('A host clang compiler is needed for native byte tests.')
        cls.temporary = tempfile.TemporaryDirectory(prefix='pad-camera-yuv-test-')
        cls.source = Path(__file__).resolve().parents[1] / 'native/camera_pad_yuv_planes.c'
        library = Path(cls.temporary.name) / 'conversion.so'
        subprocess.run([compiler, '-DAVD_YUV_HOST_TEST', '-shared', '-fPIC', '-O2',
                        '-Wall', '-Wextra', '-Werror', str(cls.source), '-o', str(library)],
                       check=True, capture_output=True)
        cls.library = ctypes.CDLL(str(library))
        cls.convert = cls.library.avd_test_interleave
        pointer = ctypes.POINTER(ctypes.c_ubyte)
        cls.convert.argtypes = [pointer, ctypes.c_size_t, pointer, ctypes.c_size_t,
                                ctypes.c_size_t, pointer, ctypes.c_size_t,
                                ctypes.c_size_t, ctypes.c_int, ctypes.c_int]
        cls.convert.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def conversion(self, u, v, width, height, urow=None, vrow=None, capacity=None):
        capacity = width * height // 2 if capacity is None else capacity
        output = (ctypes.c_ubyte * capacity)(*([0xa5] * capacity))
        uarray = (ctypes.c_ubyte * len(u))(*u)
        varray = (ctypes.c_ubyte * len(v))(*v)
        result = self.convert(output, capacity, uarray, len(u),
                              width // 2 if urow is None else urow,
                              varray, len(v), width // 2 if vrow is None else vrow,
                              width, height)
        return result, bytes(output)

    def test_exact_nv21_chroma_order(self):
        self.assertEqual(self.conversion([1, 2, 3, 4], [11, 12, 13, 14], 4, 4),
                         (1, bytes([11, 1, 12, 2, 13, 3, 14, 4])))

    def test_independent_row_padding_and_short_final_row(self):
        self.assertEqual(self.conversion([1, 2, 99, 99, 3, 4], [11, 12, 99, 13, 14],
                                         4, 4, urow=4, vrow=3),
                         (1, bytes([11, 1, 12, 2, 13, 3, 14, 4])))

    def test_minimum_even_image(self):
        self.assertEqual(self.conversion([23], [45], 2, 2), (1, bytes([45, 23])))

    def test_invalid_bounds_leave_output_untouched(self):
        cases = [([1, 2], [11, 12], 4, 4, None, None, 8),
                 ([1, 2, 3, 4], [11, 12, 13, 14], 4, 4, 1, 2, 8),
                 ([1, 2, 3, 4], [11, 12, 13, 14], 4, 4, 2, 1, 8),
                 ([1, 2, 3, 4], [11, 12, 13, 14], 4, 4, None, None, 7),
                 ([1, 2, 3, 4], [11, 12, 13, 14], 3, 4, None, None, 8),
                 ([1, 2, 3, 4], [11, 12, 13, 14], 4, 3, None, None, 8),
                 ([1], [11], 4, 4, ctypes.c_size_t(-1).value, 2, 8)]
        for u, v, width, height, urow, vrow, capacity in cases:
            with self.subTest(width=width, height=height, urow=urow, vrow=vrow, capacity=capacity):
                self.assertEqual(self.conversion(u, v, width, height, urow, vrow, capacity),
                                 (0, bytes([0xa5] * capacity)))

    def test_real_ranchu_photo_size(self):
        width, height = 1280, 960
        samples = width * height // 4
        result, output = self.conversion([97] * samples, [153] * samples, width, height)
        self.assertEqual(result, 1)
        self.assertEqual(output, bytes([153, 97]) * samples)


if __name__ == '__main__':
    unittest.main()
