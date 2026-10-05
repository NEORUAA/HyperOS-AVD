// Private I420 -> NV21 planes for the pinned yingtian Camera/ImageReader ABI.
#define _GNU_SOURCE
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Keep this byte conversion independently testable without loading Android JNI.
static int interleave_i420(unsigned char *dst, size_t capacity,
                           const unsigned char *u, size_t usize, size_t urow,
                           const unsigned char *v, size_t vsize, size_t vrow,
                           int width, int height) {
    if (!dst || !u || !v || width <= 0 || height <= 0 ||
        width > 8192 || height > 8192 || (width & 1) || (height & 1)) return 0;
    size_t columns = (size_t)width / 2, rows = (size_t)height / 2;
    if (urow < columns || vrow < columns || capacity < (size_t)width * rows ||
        (rows > 1 && (urow > (SIZE_MAX-columns)/(rows-1) ||
                      vrow > (SIZE_MAX-columns)/(rows-1))) ||
        usize < (rows-1)*urow + columns || vsize < (rows-1)*vrow + columns) return 0;
    for (size_t y = 0; y < rows; ++y)
        for (size_t x = 0; x < columns; ++x) {
            dst[y*(size_t)width + 2*x] = v[y*vrow+x];
            dst[y*(size_t)width + 2*x+1] = u[y*urow+x];
        }
    return 1;
}

#ifdef AVD_YUV_HOST_TEST
int avd_test_interleave(unsigned char *dst, size_t capacity,
                        const unsigned char *u, size_t usize, size_t urow,
                        const unsigned char *v, size_t vsize, size_t vrow,
                        int width, int height) {
    return interleave_i420(dst, capacity, u, usize, urow, v, vsize, vrow, width, height);
}
#else
#include <android/dlext.h>
#include <android/log.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <jni.h>
#include <link.h>
#include <stdio.h>
#include <sys/system_properties.h>
#include <unistd.h>

extern const unsigned char avd_original_start[], avd_original_end[];
static void *original;
static jobjectArray (*original_planes)(JNIEnv *, jobject, jint, jint, jlong);
static const unsigned char plane_code[] = {
    0xff,0x03,0x04,0xd1,0xfd,0x7b,0x0a,0xa9,0xfb,0x5b,0x00,0xf9,0xfa,0x67,0x0c,0xa9,
    0xf8,0x5f,0x0d,0xa9,0xf6,0x57,0x0e,0xa9,0xf4,0x4f,0x0f,0xa9,0xfd,0x83,0x02,0x91
};
static const char plane_name[] = "nativeCreatePlanes";
static const char plane_signature[] = "(IIJ)[Landroid/media/ImageReader$SurfaceImage$SurfacePlane;";

static int mapped(const struct dl_phdr_info *info, uintptr_t address, size_t length,
                  unsigned flags) {
    for (unsigned i = 0; i < info->dlpi_phnum; ++i) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        uintptr_t start = info->dlpi_addr + ph->p_vaddr;
        if (ph->p_type == PT_LOAD && (ph->p_flags & flags) == flags &&
            address >= start && address-start <= ph->p_filesz &&
            length <= ph->p_filesz-(address-start)) return 1;
    }
    return 0;
}
static int find_planes(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size; (void)data;
    if (strcmp(info->dlpi_name, "/system/lib64/libandroid_runtime.so")) return 0;
    // Verified full-SHA source: 5aad1f635b6e8ca93a5bf190f07188d5b04e8c38d39d6a772a6ccdb0f02a13e3.
    uintptr_t address = info->dlpi_addr + 0x1a310c;
    uintptr_t table_address = info->dlpi_addr + 0x2cd0a8;
    uintptr_t name_address = info->dlpi_addr + 0x80531;
    uintptr_t signature_address = info->dlpi_addr + 0x932c5;
    if (!mapped(info, address, sizeof(plane_code), PF_R | PF_X) ||
        !mapped(info, table_address, sizeof(JNINativeMethod), PF_R) ||
        !mapped(info, name_address, sizeof(plane_name), PF_R) ||
        !mapped(info, signature_address, sizeof(plane_signature), PF_R)) return 1;
    const JNINativeMethod *method = (const void *)table_address;
    if ((uintptr_t)method->name == name_address &&
        (uintptr_t)method->signature == signature_address &&
        (uintptr_t)method->fnPtr == address &&
        !memcmp(method->name, plane_name, sizeof(plane_name)) &&
        !memcmp(method->signature, plane_signature, sizeof(plane_signature)) &&
        !memcmp((const void *)address, plane_code, sizeof(plane_code)))
        original_planes = (void *)address;
    return 1;
}
static jobject slice(JNIEnv *env, jobject buffer, jmethodID duplicate,
                     jmethodID position, jmethodID slice_method, jint offset) {
    jobject copy = (*env)->CallObjectMethod(env, buffer, duplicate);
    if (!copy || (*env)->ExceptionCheck(env)) return NULL;
    jobject positioned = (*env)->CallObjectMethod(env, copy, position, offset);
    if (!positioned || (*env)->ExceptionCheck(env)) return NULL;
    return (*env)->CallObjectMethod(env, copy, slice_method);
}
static jobjectArray planes(JNIEnv *env, jobject image, jint number, jint format, jlong usage) {
    jobjectArray result = original_planes(env, image, number, format, usage);
    if (!result || number != 3 || format != 35 || (*env)->ExceptionCheck(env)) return result;
    if ((*env)->PushLocalFrame(env, 24)) { (*env)->ExceptionClear(env); return result; }
    if ((*env)->GetArrayLength(env, result) != 3) goto done;
    jclass image_class = (*env)->GetObjectClass(env, image);
    if (!image_class || (*env)->ExceptionCheck(env)) goto done;
    jmethodID width_method = (*env)->GetMethodID(env, image_class, "getWidth", "()I");
    if (!width_method || (*env)->ExceptionCheck(env)) goto done;
    jmethodID height_method = (*env)->GetMethodID(env, image_class, "getHeight", "()I");
    if (!height_method || (*env)->ExceptionCheck(env)) goto done;
    jint width = (*env)->CallIntMethod(env, image, width_method);
    if ((*env)->ExceptionCheck(env)) goto done;
    jint height = (*env)->CallIntMethod(env, image, height_method);
    if ((*env)->ExceptionCheck(env) || width <= 0 || height <= 0 ||
        width > 8192 || height > 8192 || (width & 1) || (height & 1)) goto done;
    jobject u = (*env)->GetObjectArrayElement(env, result, 1);
    jobject v = (*env)->GetObjectArrayElement(env, result, 2);
    if (!u || !v || (*env)->ExceptionCheck(env)) goto done;
    jclass plane_class = (*env)->GetObjectClass(env, u);
    if (!plane_class || (*env)->ExceptionCheck(env)) goto done;
    jfieldID row_field = (*env)->GetFieldID(env, plane_class, "mRowStride", "I");
    if (!row_field || (*env)->ExceptionCheck(env)) goto done;
    jfieldID pixel_field = (*env)->GetFieldID(env, plane_class, "mPixelStride", "I");
    if (!pixel_field || (*env)->ExceptionCheck(env)) goto done;
    jfieldID buffer_field = (*env)->GetFieldID(env, plane_class, "mBuffer", "Ljava/nio/ByteBuffer;");
    if (!buffer_field || (*env)->ExceptionCheck(env)) goto done;
    if ((*env)->GetIntField(env, u, pixel_field) != 1 ||
        (*env)->GetIntField(env, v, pixel_field) != 1) goto done;
    jint urow = (*env)->GetIntField(env, u, row_field), vrow = (*env)->GetIntField(env, v, row_field);
    jobject ub = (*env)->GetObjectField(env, u, buffer_field), vb = (*env)->GetObjectField(env, v, buffer_field);
    if (!ub || !vb || (*env)->ExceptionCheck(env) || urow < width/2 || vrow < width/2) goto done;
    const unsigned char *usrc = (*env)->GetDirectBufferAddress(env, ub);
    const unsigned char *vsrc = (*env)->GetDirectBufferAddress(env, vb);
    jlong usize = (*env)->GetDirectBufferCapacity(env, ub), vsize = (*env)->GetDirectBufferCapacity(env, vb);
    if (!usrc || !vsrc || usize < 0 || vsize < 0 || (*env)->ExceptionCheck(env)) goto done;
    jclass byte_buffer = (*env)->FindClass(env, "java/nio/ByteBuffer");
    if (!byte_buffer || (*env)->ExceptionCheck(env)) goto done;
    jmethodID allocate = (*env)->GetStaticMethodID(env, byte_buffer, "allocateDirect", "(I)Ljava/nio/ByteBuffer;");
    if (!allocate || (*env)->ExceptionCheck(env)) goto done;
    jmethodID duplicate = (*env)->GetMethodID(env, byte_buffer, "duplicate", "()Ljava/nio/ByteBuffer;");
    if (!duplicate || (*env)->ExceptionCheck(env)) goto done;
    jmethodID position = (*env)->GetMethodID(env, byte_buffer, "position", "(I)Ljava/nio/ByteBuffer;");
    if (!position || (*env)->ExceptionCheck(env)) goto done;
    jmethodID slice_method = (*env)->GetMethodID(env, byte_buffer, "slice", "()Ljava/nio/ByteBuffer;");
    if (!slice_method || (*env)->ExceptionCheck(env)) goto done;
    jobject interleaved = (*env)->CallStaticObjectMethod(env, byte_buffer, allocate, width*height/2);
    if (!interleaved || (*env)->ExceptionCheck(env)) goto done;
    unsigned char *dst = (*env)->GetDirectBufferAddress(env, interleaved);
    if (!interleave_i420(dst, (size_t)width*(size_t)height/2,
                         usrc, (size_t)usize, (size_t)urow,
                         vsrc, (size_t)vsize, (size_t)vrow, width, height)) goto done;
    // Both slices retain the Java-owned direct allocation, including its cleaner.
    jobject new_u = slice(env, interleaved, duplicate, position, slice_method, 1);
    if (!new_u || (*env)->ExceptionCheck(env)) goto done;
    jobject new_v = slice(env, interleaved, duplicate, position, slice_method, 0);
    if (!new_v || (*env)->ExceptionCheck(env)) goto done;
    (*env)->SetObjectField(env, u, buffer_field, new_u);
    (*env)->SetObjectField(env, v, buffer_field, new_v);
    (*env)->SetIntField(env, u, pixel_field, 2); (*env)->SetIntField(env, v, pixel_field, 2);
    (*env)->SetIntField(env, u, row_field, width); (*env)->SetIntField(env, v, row_field, width);
    static unsigned count;
    if (__atomic_fetch_add(&count, 1, __ATOMIC_RELAXED) < 4)
        __android_log_print(ANDROID_LOG_INFO, "AVD-Pad-Camera-YUV", "private I420 -> NV21 planes: %dx%d", width, height);
done:
    // Only errors introduced by this optional conversion are cleared.
    if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
    (*env)->PopLocalFrame(env, NULL);
    return result;
}
static int find_original_offset(struct dl_phdr_info *info, size_t size, void *out) {
    (void)size;
    uintptr_t address = (uintptr_t)avd_original_start;
    for (unsigned i = 0; i < info->dlpi_phnum; ++i) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        uintptr_t start = info->dlpi_addr + ph->p_vaddr;
        if (ph->p_type == PT_LOAD && address >= start && address-start < ph->p_filesz) {
            *(off64_t *)out = ph->p_offset + address-start;
            return 1;
        }
    }
    return 0;
}
JNIEXPORT jint JNI_OnLoad(JavaVM *vm, void *reserved) {
    Dl_info own;
    off64_t offset = -1;
    dl_iterate_phdr(find_original_offset, &offset);
    if (!dladdr((void *)JNI_OnLoad, &own) || offset < 0) return JNI_ERR;
    char path[4096];
    if (snprintf(path, sizeof(path), "%s", own.dli_fname) >= (int)sizeof(path)) return JNI_ERR;
    char *zip = strchr(path, '!');
    if (zip) {
        *zip = 0;
        FILE *maps = fopen("/proc/self/maps", "r");
        if (!maps) return JNI_ERR;
        char line[8192], permissions[8];
        unsigned long start, end, file_offset;
        int found = 0;
        while (fgets(line, sizeof(line), maps)) {
            if (sscanf(line, "%lx-%lx %7s %lx", &start, &end, permissions, &file_offset) == 4 &&
                start == (uintptr_t)own.dli_fbase) { offset += file_offset; found = 1; break; }
        }
        fclose(maps);
        if (!found) return JNI_ERR;
    }
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return JNI_ERR;
    android_dlextinfo ext = {0};
    ext.flags = ANDROID_DLEXT_USE_LIBRARY_FD | ANDROID_DLEXT_USE_LIBRARY_FD_OFFSET | ANDROID_DLEXT_FORCE_LOAD;
    ext.library_fd = fd; ext.library_fd_offset = offset;
    original = android_dlopen_ext("libhyperos_avd_pad_yuv_original.so", RTLD_NOW | RTLD_LOCAL, &ext);
    close(fd);
    if (!original) { __android_log_print(ANDROID_LOG_ERROR, "AVD-Pad-Camera-YUV", "original load: %s", dlerror()); return JNI_ERR; }
    jint (*onload)(JavaVM *, void *) = dlsym(original, "JNI_OnLoad");
    jint version = onload ? onload(vm, reserved) : JNI_VERSION_1_6;
    if (version < 0) return version;
    char process[80] = {0}, device[PROP_VALUE_MAX] = {0};
    FILE *cmdline = fopen("/proc/self/cmdline", "r");
    if (cmdline) { fread(process, 1, sizeof(process)-1, cmdline); fclose(cmdline); }
    __system_property_get("ro.product.device", device);
    if (strcmp(process, "com.android.camera") || strcmp(device, "yingtian")) return version;
    dl_iterate_phdr(find_planes, NULL);
    JNIEnv *env;
    if (!original_planes || (*vm)->GetEnv(vm, (void **)&env, JNI_VERSION_1_6)) return version;
    if ((*env)->ExceptionCheck(env)) return version;
    jclass image = (*env)->FindClass(env, "android/media/ImageReader$SurfaceImage");
    JNINativeMethod method = {(char *)plane_name, (char *)plane_signature, (void *)planes};
    if (image && !(*env)->ExceptionCheck(env) && !(*env)->RegisterNatives(env, image, &method, 1))
        __android_log_print(ANDROID_LOG_INFO, "AVD-Pad-Camera-YUV", "private YUV plane bridge installed");
    if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
    if (image) (*env)->DeleteLocalRef(env, image);
    return version;
}
JNIEXPORT void JNI_OnUnload(JavaVM *vm, void *reserved) {
    void (*unload)(JavaVM *, void *) = original ? dlsym(original, "JNI_OnUnload") : NULL;
    if (unload) unload(vm, reserved);
}
#endif
