// Process-local YUV plane bridge for the pinned Xiaomi camera and framework.
#define _GNU_SOURCE
#include <android/dlext.h>
#include <android/log.h>
#include <jni.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <pthread.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <dirent.h>

extern const unsigned char avd_original_start[], avd_original_end[];
static void *original;
static jobjectArray (*original_planes)(JNIEnv *, jobject, jint, jint, jlong);
static const unsigned char plane_code[] = {
  0x3f,0x23,0x03,0xd5,0xff,0x83,0x03,0xd1,0xfd,0x7b,0x09,0xa9,0xfa,0x67,0x0a,0xa9,
  0xf8,0x5f,0x0b,0xa9,0xf6,0x57,0x0c,0xa9,0xf4,0x4f,0x0d,0xa9,0xfd,0x43,0x02,0x91
};

// Use the app's existing early-JPEG recovery when ranchu cannot produce a vendor
// ISP final image. Never complete a task until its own thumbnail is a closed JPEG.
static JavaVM *java_vm;
static jclass manager_class, implementation_class;
static jfieldID manager_impl, task_map, task_capture, capture_timestamp;
static jfieldID task_request, request_frame, task_output, output_path;
static jmethodID map_values, map_contains, values_array, notify_failed;
static jlong last_task_timestamp;
struct Capture { jobject impl, task; jlong frame; char name[128]; };
static int closed_jpeg(const char *name) {
    char path[256];
    snprintf(path, sizeof(path), "/data/user/0/com.android.camera/files/thumbnails/%s", name);
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    struct stat st;
    unsigned char head[2], tail[65536];
    int valid = !fstat(fd, &st) && S_ISREG(st.st_mode) && st.st_size > 100 &&
        pread(fd, head, 2, 0) == 2 && head[0] == 0xff && head[1] == 0xd8;
    size_t size = valid ? (st.st_size < (off_t)sizeof(tail) ? (size_t)st.st_size : sizeof(tail)) : 0;
    if (size && pread(fd, tail, size, st.st_size-size) == (ssize_t)size) {
        valid = 0;
        // Xiaomi appends serialized capture metadata after JPEG's EOI marker.
        for (size_t i = 1; i < size; ++i)
            if (tail[i-1] == 0xff && tail[i] == 0xd9) { valid = 1; break; }
    } else valid = 0;
    DIR *descriptors = valid ? opendir("/proc/self/fd") : NULL;
    if (valid && !descriptors) valid = 0;
    if (descriptors) {
        struct dirent *entry;
        while ((entry = readdir(descriptors))) {
            char *end;
            long descriptor = strtol(entry->d_name, &end, 10);
            if (*end || descriptor < 0) continue;
            int flags = fcntl((int)descriptor, F_GETFL);
            if (flags < 0 || (flags & O_ACCMODE) == O_RDONLY) continue;
            char link[96], destination[512];
            snprintf(link, sizeof(link), "/proc/self/fd/%ld", descriptor);
            ssize_t length = readlink(link, destination, sizeof(destination)-1);
            if (length <= 0) continue;
            destination[length] = 0;
            if (!strcmp(path, destination)) { valid = 0; break; }
        }
        closedir(descriptors);
    }
    close(fd);
    return valid;
}
static void *finish_capture(void *arg) {
    struct Capture *capture = arg;
    JNIEnv *env = NULL;
    if ((*java_vm)->AttachCurrentThread(java_vm, &env, NULL)) { free(capture); return NULL; }
    for (int i = 0; i < 25; ++i) {
        usleep(200000);
        (*env)->PushLocalFrame(env, 12);
        jobject current = (*env)->GetStaticObjectField(env, manager_class, manager_impl);
        jobject map = (*env)->GetObjectField(env, capture->impl, task_map);
        jboolean present = map ? (*env)->CallBooleanMethod(env, map, map_contains, capture->task) : JNI_FALSE;
        if ((*env)->ExceptionCheck(env)) {
            (*env)->ExceptionClear(env); (*env)->PopLocalFrame(env, NULL); break;
        }
        if (!present || !(*env)->IsSameObject(env, current, capture->impl)) {
            (*env)->PopLocalFrame(env, NULL); break;
        }
        if (closed_jpeg(capture->name)) {
            jstring name = (*env)->NewStringUTF(env, capture->name);
            jstring reason = (*env)->NewStringUTF(env,
                "AVD: vendor ISP unavailable; retain completed early JPEG");
            (*env)->CallStaticVoidMethod(env, manager_class, notify_failed, name, capture->frame, reason);
            if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
            else __android_log_print(ANDROID_LOG_INFO, "AVD-Camera-YUV",
                        "finalized early JPEG through original recovery: %s", capture->name);
            (*env)->PopLocalFrame(env, NULL); break;
        }
        (*env)->PopLocalFrame(env, NULL);
    }
    (*env)->DeleteGlobalRef(env, capture->impl);
    (*env)->DeleteGlobalRef(env, capture->task);
    free(capture);
    (*java_vm)->DetachCurrentThread(java_vm);
    return NULL;
}
static void queue_capture(JNIEnv *env, jobject image) {
    if (!notify_failed || (*env)->PushLocalFrame(env, 24)) return;
    jclass image_class = (*env)->GetObjectClass(env, image);
    jmethodID timestamp_method = (*env)->GetMethodID(env, image_class, "getTimestamp", "()J");
    jlong timestamp = (*env)->CallLongMethod(env, image, timestamp_method);
    if ((*env)->ExceptionCheck(env) || timestamp <= 0 ||
        timestamp == __atomic_load_n(&last_task_timestamp, __ATOMIC_RELAXED)) goto done;
    jobject impl = (*env)->GetStaticObjectField(env, manager_class, manager_impl);
    if (!impl || !(*env)->IsInstanceOf(env, impl, implementation_class)) goto done;
    jobject map = (*env)->GetObjectField(env, impl, task_map);
    if (!map) goto done;
    jobject values = (*env)->CallObjectMethod(env, map, map_values);
    if (!values || (*env)->ExceptionCheck(env)) goto done;
    jobjectArray tasks = (*env)->CallObjectMethod(env, values, values_array);
    if (!tasks || (*env)->ExceptionCheck(env)) goto done;
    jsize count = (*env)->GetArrayLength(env, tasks);
    if (count > 64) goto done;
    for (jsize i = 0; i < count; ++i) {
        jobject task = (*env)->GetObjectArrayElement(env, tasks, i);
        if (!task) continue;
        jobject metadata = (*env)->GetObjectField(env, task, task_capture);
        jlong task_timestamp = metadata ? (*env)->GetLongField(env, metadata, capture_timestamp) : 0;
        (*env)->DeleteLocalRef(env, metadata);
        if (task_timestamp != timestamp) { (*env)->DeleteLocalRef(env, task); continue; }
        jobject request = (*env)->GetObjectField(env, task, task_request);
        jobject output = (*env)->GetObjectField(env, task, task_output);
        jstring path = output ? (*env)->GetObjectField(env, output, output_path) : NULL;
        if (!request || !path || (*env)->ExceptionCheck(env)) goto done;
        const char *text = (*env)->GetStringUTFChars(env, path, NULL);
        const char *name = text ? strrchr(text, '/') : NULL;
        struct Capture *capture = NULL;
        if (name && !strncmp(text, "/storage/emulated/0/DCIM/Camera/", sizeof("/storage/emulated/0/DCIM/Camera/")-1) &&
            !strncmp(name+1, "IMG_", 4) && strlen(name+1) < 128 && strstr(name+1, ".jpg")) {
            capture = calloc(1, sizeof(*capture));
            if (capture) snprintf(capture->name, sizeof(capture->name), "%s", name+1);
        }
        if (text) (*env)->ReleaseStringUTFChars(env, path, text);
        if (!capture) goto done;
        capture->frame = (*env)->GetLongField(env, request, request_frame);
        capture->impl = (*env)->NewGlobalRef(env, impl);
        capture->task = (*env)->NewGlobalRef(env, task);
        if (!capture->impl || !capture->task || (*env)->ExceptionCheck(env)) {
            (*env)->DeleteGlobalRef(env, capture->impl); (*env)->DeleteGlobalRef(env, capture->task);
            free(capture); goto done;
        }
        jlong previous = __atomic_exchange_n(&last_task_timestamp, timestamp, __ATOMIC_RELAXED);
        pthread_t worker;
        if (previous != timestamp && !pthread_create(&worker, NULL, finish_capture, capture)) {
            pthread_detach(worker);
        } else {
            (*env)->DeleteGlobalRef(env, capture->impl); (*env)->DeleteGlobalRef(env, capture->task);
            free(capture);
        }
        break;
    }
done:
    // This recovery is optional; never leak a lookup error into ImageReader.
    if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
    (*env)->PopLocalFrame(env, NULL);
}
static void prepare_recovery(JNIEnv *env, JavaVM *vm) {
    jclass manager = (*env)->FindClass(env, "com/xiaomi/camera/mivi/MIVICaptureManager");
    jclass impl = (*env)->FindClass(env, "com/xiaomi/camera/mivi/qcom/MIVICaptureManagerQcomImpl");
    jclass task = (*env)->FindClass(env, "Sh/t");
    jclass capture = (*env)->FindClass(env, "Sh/C");
    jclass request = (*env)->FindClass(env, "Sh/B");
    jclass output = (*env)->FindClass(env, "Sh/D");
    jclass map = (*env)->FindClass(env, "java/util/concurrent/ConcurrentHashMap");
    jclass values = (*env)->FindClass(env, "java/util/Collection");
    if ((*env)->ExceptionCheck(env) || !manager || !impl || !task || !capture ||
        !request || !output || !map || !values) goto error;
    manager_impl = (*env)->GetStaticFieldID(env, manager, "IMPL", "Lcom/xiaomi/camera/mivi/MIVICaptureManagerImpl;");
    task_map = (*env)->GetFieldID(env, impl, "mParallelTaskDataMap", "Ljava/util/concurrent/ConcurrentHashMap;");
    task_capture = (*env)->GetFieldID(env, task, "a", "LSh/C;");
    capture_timestamp = (*env)->GetFieldID(env, capture, "f", "J");
    task_request = (*env)->GetFieldID(env, task, "j", "LSh/B;");
    request_frame = (*env)->GetFieldID(env, request, "b", "J");
    task_output = (*env)->GetFieldID(env, task, "k", "LSh/D;");
    output_path = (*env)->GetFieldID(env, output, "g", "Ljava/lang/String;");
    map_values = (*env)->GetMethodID(env, map, "values", "()Ljava/util/Collection;");
    map_contains = (*env)->GetMethodID(env, map, "containsValue", "(Ljava/lang/Object;)Z");
    values_array = (*env)->GetMethodID(env, values, "toArray", "()[Ljava/lang/Object;");
    jmethodID method = (*env)->GetStaticMethodID(env, manager, "notifyCaptureFailed", "(Ljava/lang/String;JLjava/lang/String;)V");
    if ((*env)->ExceptionCheck(env) || !method) goto error;
    manager_class = (*env)->NewGlobalRef(env, manager);
    implementation_class = (*env)->NewGlobalRef(env, impl);
    if (!manager_class || !implementation_class) goto error;
    java_vm = vm; notify_failed = method;
    return;
error:
    if ((*env)->ExceptionCheck(env)) (*env)->ExceptionClear(env);
    __android_log_print(ANDROID_LOG_WARN, "AVD-Camera-YUV", "original recovery ABI unavailable; retaining vendor timeout");
}

static int find_planes(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size; (void)data;
    const char *name = strrchr(info->dlpi_name, '/');
    if (!name || strcmp(name + 1, "libandroid_runtime.so")) return 0;
    uintptr_t address = info->dlpi_addr + 0x1a39c4;
    if (!memcmp((void *)address, plane_code, sizeof(plane_code)))
        original_planes = (void *)address;
    return 1;
}
static jobject slice(JNIEnv *env, jobject buffer, jclass byte_buffer,
                     jmethodID duplicate, jmethodID position, jmethodID slice_method, jint offset) {
    jobject copy = (*env)->CallObjectMethod(env, buffer, duplicate);
    if (!copy) return NULL;
    (*env)->CallObjectMethod(env, copy, position, offset);
    jobject view = (*env)->CallObjectMethod(env, copy, slice_method);
    (*env)->DeleteLocalRef(env, copy);
    (void)byte_buffer;
    return view;
}
static jobjectArray planes(JNIEnv *env, jobject image, jint number, jint format, jlong usage) {
    jobjectArray result = original_planes(env, image, number, format, usage);
    if (!result || number != 3 || format != 35 || (*env)->ExceptionCheck(env)) return result;
    jclass image_class = (*env)->GetObjectClass(env, image);
    jmethodID width_method = (*env)->GetMethodID(env, image_class, "getWidth", "()I");
    jmethodID height_method = (*env)->GetMethodID(env, image_class, "getHeight", "()I");
    jint width = (*env)->CallIntMethod(env, image, width_method);
    jint height = (*env)->CallIntMethod(env, image, height_method);
    if ((*env)->ExceptionCheck(env) || width <= 0 || height <= 0 ||
        width > 8192 || height > 8192 || (width & 1) || (height & 1)) return result;
    jobject u = (*env)->GetObjectArrayElement(env, result, 1);
    jobject v = (*env)->GetObjectArrayElement(env, result, 2);
    if (!u || !v) return result;
    jclass plane_class = (*env)->GetObjectClass(env, u);
    jfieldID row_field = (*env)->GetFieldID(env, plane_class, "mRowStride", "I");
    jfieldID pixel_field = (*env)->GetFieldID(env, plane_class, "mPixelStride", "I");
    jfieldID buffer_field = (*env)->GetFieldID(env, plane_class, "mBuffer", "Ljava/nio/ByteBuffer;");
    if ((*env)->ExceptionCheck(env)) return result;
    jint upixel = (*env)->GetIntField(env, u, pixel_field), vpixel = (*env)->GetIntField(env, v, pixel_field);
    if (upixel != 1 || vpixel != 1) return result;
    jint urow = (*env)->GetIntField(env, u, row_field), vrow = (*env)->GetIntField(env, v, row_field);
    jobject ub = (*env)->GetObjectField(env, u, buffer_field), vb = (*env)->GetObjectField(env, v, buffer_field);
    const unsigned char *usrc = (*env)->GetDirectBufferAddress(env, ub);
    const unsigned char *vsrc = (*env)->GetDirectBufferAddress(env, vb);
    jlong usize = (*env)->GetDirectBufferCapacity(env, ub), vsize = (*env)->GetDirectBufferCapacity(env, vb);
    if (!usrc || !vsrc || urow < width/2 || vrow < width/2 ||
        usize < (jlong)(height/2-1)*urow + width/2 ||
        vsize < (jlong)(height/2-1)*vrow + width/2) return result;
    jclass byte_buffer = (*env)->FindClass(env, "java/nio/ByteBuffer");
    jmethodID allocate = (*env)->GetStaticMethodID(env, byte_buffer, "allocateDirect", "(I)Ljava/nio/ByteBuffer;");
    jmethodID duplicate = (*env)->GetMethodID(env, byte_buffer, "duplicate", "()Ljava/nio/ByteBuffer;");
    jmethodID position = (*env)->GetMethodID(env, byte_buffer, "position", "(I)Ljava/nio/ByteBuffer;");
    jmethodID slice_method = (*env)->GetMethodID(env, byte_buffer, "slice", "()Ljava/nio/ByteBuffer;");
    if ((*env)->ExceptionCheck(env)) return result;
    // Java owns this allocation; the two slices retain its cleaner attachment.
    jobject interleaved = (*env)->CallStaticObjectMethod(env, byte_buffer, allocate, width*height/2);
    if (!interleaved || (*env)->ExceptionCheck(env)) return result;
    unsigned char *dst = (*env)->GetDirectBufferAddress(env, interleaved);
    if (!dst) return result;
    for (int y = 0; y < height/2; ++y)
        for (int x = 0; x < width/2; ++x) {
            dst[y*width + 2*x] = vsrc[y*vrow+x];
            dst[y*width + 2*x+1] = usrc[y*urow+x];
        }
    jobject new_u = slice(env, interleaved, byte_buffer, duplicate, position, slice_method, 1);
    jobject new_v = slice(env, interleaved, byte_buffer, duplicate, position, slice_method, 0);
    if (!new_u || !new_v || (*env)->ExceptionCheck(env)) return result;
    (*env)->SetObjectField(env, u, buffer_field, new_u);
    (*env)->SetObjectField(env, v, buffer_field, new_v);
    (*env)->SetIntField(env, u, pixel_field, 2); (*env)->SetIntField(env, v, pixel_field, 2);
    (*env)->SetIntField(env, u, row_field, width); (*env)->SetIntField(env, v, row_field, width);
    queue_capture(env, image);
    static unsigned count;
    if (__atomic_fetch_add(&count, 1, __ATOMIC_RELAXED) < 4)
        __android_log_print(ANDROID_LOG_INFO, "AVD-Camera-YUV", "private I420 -> interleaved planes: %dx%d", width, height);
    return result;
}
static int find_original_offset(struct dl_phdr_info *info, size_t size, void *out) {
    (void)size;
    uintptr_t addr = (uintptr_t)avd_original_start;
    for (unsigned i = 0; i < info->dlpi_phnum; ++i) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        uintptr_t start = info->dlpi_addr + ph->p_vaddr;
        if (ph->p_type == PT_LOAD && addr >= start && addr < start + ph->p_filesz) {
            *(off64_t *)out = ph->p_offset + addr - start;
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
    snprintf(path, sizeof(path), "%s", own.dli_fname);
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
    original = android_dlopen_ext("libhyperos_avd_yuv_original.so", RTLD_NOW | RTLD_LOCAL, &ext);
    close(fd);
    if (!original) { __android_log_print(ANDROID_LOG_ERROR, "AVD-Camera-YUV", "original load: %s", dlerror()); return JNI_ERR; }
    jint (*onload)(JavaVM *, void *) = dlsym(original, "JNI_OnLoad");
    jint version = onload ? onload(vm, reserved) : JNI_VERSION_1_6;
    if (version < 0) return version;
    char process[80] = {0};
    FILE *cmdline = fopen("/proc/self/cmdline", "r");
    if (cmdline) { fread(process, 1, sizeof(process)-1, cmdline); fclose(cmdline); }
    if (strcmp(process, "com.android.camera")) return version;
    dl_iterate_phdr(find_planes, NULL);
    JNIEnv *env;
    if (!original_planes || (*vm)->GetEnv(vm, (void **)&env, JNI_VERSION_1_6)) return version;
    prepare_recovery(env, vm);
    jclass image = (*env)->FindClass(env, "android/media/ImageReader$SurfaceImage");
    JNINativeMethod method = {"nativeCreatePlanes", "(IIJ)[Landroid/media/ImageReader$SurfaceImage$SurfacePlane;", (void *)planes};
    if (image && !(*env)->RegisterNatives(env, image, &method, 1))
        __android_log_print(ANDROID_LOG_INFO, "AVD-Camera-YUV", "private YUV plane bridge installed");
    return version;
}
JNIEXPORT void JNI_OnUnload(JavaVM *vm, void *reserved) {
    void (*unload)(JavaVM *, void *) = original ? dlsym(original, "JNI_OnUnload") : NULL;
    if (unload) unload(vm, reserved);
}
