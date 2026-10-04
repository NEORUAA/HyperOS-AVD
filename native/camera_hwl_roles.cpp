// Compatibility bridge for the hash-guarded ranchu HWL ABI.
#include <android/dlext.h>
#include <android/log.h>
#include <camera/NdkCameraMetadataTags.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <link.h>
#include <sys/mman.h>
#include <unistd.h>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

extern "C" const unsigned char avd_original_start[], avd_original_end[];
struct VendorTag { uint32_t id; std::string name; uint32_t type; };
struct VendorSection { std::string name; std::vector<VendorTag> tags; };
struct Entry {
    size_t index; uint32_t tag; uint8_t type; size_t count;
    union { const uint8_t *u8; const int32_t *i32; const void *raw; } data;
};
static constexpr uint32_t ROLE = 0x90010000, ROLES = 0x90010001, YUV_FORMAT = 0x90010002;
static void *original_library;
static int (*original_vendor_tags)(void *, std::vector<VendorSection> *);
static int (*original_create_device)(void *, uint32_t, void **);
static int (*original_characteristics)(const void *, void **);
static int (*metadata_get)(const void *, uint32_t, Entry *);
static int (*metadata_set_int)(void *, uint32_t, const int32_t *, uint32_t);
static void **device_table;

static int vendor_tags(void *self, std::vector<VendorSection> *out) {
    int result = original_vendor_tags(self, out);
    if (result || !out) return result;
    std::vector<VendorSection> sections{{"com.xiaomi.cameraid.role", {
        {ROLE, "cameraId", 1}, {ROLES, "cameraIds", 1}
    }}, {"xiaomi.yuv", {{YUV_FORMAT, "format", 1}}}};
    auto instance = reinterpret_cast<void *(*)()>(dlsym(original_library,
        "_ZN7android17google_camera_hal16VendorTagManager11GetInstanceEv"));
    auto add = reinterpret_cast<int (*)(void *, const std::vector<VendorSection> &)>(dlsym(original_library,
        "_ZN7android17google_camera_hal16VendorTagManager7AddTagsERKNSt3__16vectorINS0_16VendorTagSectionENS2_9allocatorIS4_EEEE"));
    if (!instance || !add) return -38;
    int registered = add(instance(), sections);
    if (registered) return registered;
    out->insert(out->end(), sections.begin(), sections.end());
    __android_log_print(ANDROID_LOG_INFO, "AVD-Camera-HWL", "registered Xiaomi camera role tags");
    return 0;
}
static int characteristics(const void *self, void **out) {
    int result = original_characteristics(self, out);
    if (result || !out || !*out) return result;
    Entry facing{}, keys{};
    if (metadata_get(*out, ACAMERA_LENS_FACING, &facing) || facing.count != 1 ||
        facing.type != 0 || facing.data.u8[0] > 1) return 0;
    // Xiaomi role 0 = rear; role 1 = front. Android lens-facing uses 1 and 0.
    int32_t role = facing.data.u8[0] == 0 ? 1 : 0;
    int a = metadata_set_int(*out, ROLE, &role, 1);
    int b = metadata_set_int(*out, ROLES, &role, 1);
    // The private plane bridge supports NV21 encoding, as required by CameraToolJNI.
    int32_t format = 2;
    metadata_set_int(*out, YUV_FORMAT, &format, 1);
    if (!metadata_get(*out, ACAMERA_REQUEST_AVAILABLE_CHARACTERISTICS_KEYS, &keys) &&
        keys.type == 1 && keys.count < 4096) {
        std::vector<int32_t> available(keys.data.i32, keys.data.i32 + keys.count);
        available.push_back(static_cast<int32_t>(ROLE));
        available.push_back(static_cast<int32_t>(ROLES));
        available.push_back(static_cast<int32_t>(YUV_FORMAT));
        metadata_set_int(*out, ACAMERA_REQUEST_AVAILABLE_CHARACTERISTICS_KEYS,
                         available.data(), available.size());
    }
    __android_log_print(ANDROID_LOG_INFO, "AVD-Camera-HWL", "role=%d metadata result=%d,%d", role, a, b);
    return 0;
}
static int create_device(void *self, uint32_t id, void **out) {
    int result = original_create_device(self, id, out);
    if (!result && out && *out && device_table) {
        // Verify the exact original table before replacing this new object's table.
        void **expected = static_cast<void **>(dlsym(original_library,
                            "_ZTVN7android27EmulatedCameraDeviceHwlImplE")) + 2;
        if (*static_cast<void ***>(*out) == expected)
            *static_cast<void ***>(*out) = device_table;
    }
    return result;
}
static void **clone_table(void **original, size_t entries) {
    long page = sysconf(_SC_PAGESIZE);
    if (page <= 0 || entries * sizeof(void *) > static_cast<size_t>(page)) return nullptr;
    void **copy = static_cast<void **>(mmap(nullptr, page, PROT_READ | PROT_WRITE,
                                           MAP_ANONYMOUS | MAP_PRIVATE, -1, 0));
    if (copy == MAP_FAILED) return nullptr;
    memcpy(copy, original, entries * sizeof(void *));
    return copy;
}
static int find_original_offset(dl_phdr_info *info, size_t, void *out) {
    uintptr_t addr = reinterpret_cast<uintptr_t>(avd_original_start);
    for (unsigned i = 0; i < info->dlpi_phnum; ++i) {
        const auto &ph = info->dlpi_phdr[i];
        uintptr_t start = info->dlpi_addr + ph.p_vaddr;
        if (ph.p_type == PT_LOAD && addr >= start && addr < start + ph.p_filesz) {
            *static_cast<off64_t *>(out) = ph.p_offset + addr - start;
            return 1;
        }
    }
    return 0;
}
extern "C" void *CreateCameraProviderHwl() {
    Dl_info own{};
    off64_t offset = -1;
    dl_iterate_phdr(find_original_offset, &offset);
    if (!dladdr(reinterpret_cast<void *>(CreateCameraProviderHwl), &own) ||
        offset < 0 || offset % 4096) return nullptr;
    int fd = open(own.dli_fname, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return nullptr;
    android_dlextinfo ext{};
    ext.flags = ANDROID_DLEXT_USE_LIBRARY_FD | ANDROID_DLEXT_USE_LIBRARY_FD_OFFSET |
                ANDROID_DLEXT_FORCE_LOAD;
    ext.library_fd = fd;
    ext.library_fd_offset = offset;
    original_library = android_dlopen_ext("libhyperos_avd_camera_original.so",
                                        RTLD_NOW | RTLD_LOCAL, &ext);
    close(fd);
    if (!original_library) {
        __android_log_print(ANDROID_LOG_ERROR, "AVD-Camera-HWL", "original load failed: %s", dlerror());
        return nullptr;
    }
    auto factory = reinterpret_cast<void *(*)()>(dlsym(original_library, "CreateCameraProviderHwl"));
    void *provider = factory ? factory() : nullptr;
    if (!provider) return nullptr;
    void **provider_original = static_cast<void **>(dlsym(original_library,
                         "_ZTVN7android29EmulatedCameraProviderHwlImplE"));
    void **device_original = static_cast<void **>(dlsym(original_library,
                         "_ZTVN7android27EmulatedCameraDeviceHwlImplE"));
    if (!provider_original || !device_original ||
        *static_cast<void ***>(provider) != provider_original + 2) return provider;
    original_vendor_tags = reinterpret_cast<decltype(original_vendor_tags)>(provider_original[6]);
    original_create_device = reinterpret_cast<decltype(original_create_device)>(provider_original[11]);
    original_characteristics = reinterpret_cast<decltype(original_characteristics)>(device_original[8]);
    metadata_get = reinterpret_cast<decltype(metadata_get)>(dlsym(original_library,
          "_ZNK7android17google_camera_hal17HalCameraMetadata3GetEjP24camera_metadata_ro_entry"));
    metadata_set_int = reinterpret_cast<decltype(metadata_set_int)>(dlsym(original_library,
          "_ZN7android17google_camera_hal17HalCameraMetadata3SetEjPKij"));
    if (!metadata_get || !metadata_set_int ||
        reinterpret_cast<void *>(original_characteristics) != dlsym(original_library,
          "_ZNK7android27EmulatedCameraDeviceHwlImpl24GetCameraCharacteristicsEPNSt3__110unique_ptrINS_17google_camera_hal17HalCameraMetadataENS1_14default_deleteIS4_EEEE")) return provider;
    void **p = clone_table(provider_original, 14);
    void **d = clone_table(device_original, 19);
    if (!p || !d) return provider;
    p[6] = reinterpret_cast<void *>(vendor_tags);
    p[11] = reinterpret_cast<void *>(create_device);
    d[8] = reinterpret_cast<void *>(characteristics);
    long page = sysconf(_SC_PAGESIZE);
    mprotect(p, page, PROT_READ); mprotect(d, page, PROT_READ);
    device_table = d + 2;
    *static_cast<void ***>(provider) = p + 2;
    __android_log_print(ANDROID_LOG_INFO, "AVD-Camera-HWL", "camera role bridge active");
    return provider;
}
