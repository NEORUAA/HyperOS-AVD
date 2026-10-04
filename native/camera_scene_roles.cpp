// Xiaomi metadata bridge for the SHA-256-pinned ARM64 ranchu scene provider.
#include <__config_site>
#undef _LIBCPP_ABI_NAMESPACE
#define _LIBCPP_ABI_NAMESPACE __1
#include <android/log.h>
#include <camera/NdkCameraMetadataTags.h>
#include <dlfcn.h>
#include <link.h>
#include <sys/mman.h>
#include <unistd.h>
#include <cstdint>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

// Layouts from camera common/device AIDL V1 and libcamera_metadata, guarded
// by the provider hash in the installer and the relocated function slots here.
struct VendorTag { int32_t id; std::string name; int32_t type; };
struct VendorSection { std::string name; std::vector<VendorTag> tags; };
struct Metadata { std::vector<uint8_t> metadata; };
struct Entry {
    size_t index; uint32_t tag; uint8_t type; size_t count;
    union { const uint8_t *u8; const int32_t *i32; const int64_t *i64; const void *raw; } data;
};
struct VendorOps {
    int (*count)(const VendorOps *);
    void (*all)(const VendorOps *, uint32_t *);
    const char *(*section)(const VendorOps *, uint32_t);
    const char *(*name)(const VendorOps *, uint32_t);
    int (*type)(const VendorOps *, uint32_t);
    void *reserved[8];
};
static void (*status_delete)(void *);
static bool (*status_ok)(const void *);
// ScopedAStatus contains one owned AStatus pointer. A nontrivial destructor
// retains its ARM64 indirect-return ABI without depending on private headers.
struct Status {
    void *value;
    Status(Status &&other) noexcept : value(std::exchange(other.value, nullptr)) {}
    ~Status() { if (value) status_delete(value); }
    bool isOk() const { return status_ok(value); }
};
static constexpr uint32_t ROLE = 0x90010000, ROLES = 0x90010001, YUV = 0x90010002;
static Status (*original_tags)(void *, std::vector<VendorSection> *);
static Status (*original_metadata)(void *, Metadata *);
static void *(*allocate_metadata)(size_t, size_t);
static void (*free_metadata)(void *);
static size_t (*metadata_size)(const void *);
static size_t (*entry_count)(const void *);
static size_t (*data_count)(const void *);
static int (*append_metadata)(void *, const void *);
static int (*find_entry)(const void *, uint32_t, Entry *);
static int (*add_entry)(void *, uint32_t, const void *, size_t);
static int (*update_entry)(void *, size_t, const void *, size_t, void *);

static int tag_count(const VendorOps *) { return 3; }
static void tag_all(const VendorOps *, uint32_t *out) { out[0] = ROLE; out[1] = ROLES; out[2] = YUV; }
static const char *tag_section(const VendorOps *, uint32_t tag) {
    return tag == ROLE || tag == ROLES ? "com.xiaomi.cameraid.role" : tag == YUV ? "xiaomi.yuv" : nullptr;
}
static const char *tag_name(const VendorOps *, uint32_t tag) {
    return tag == ROLE ? "cameraId" : tag == ROLES ? "cameraIds" : tag == YUV ? "format" : nullptr;
}
static int tag_type(const VendorOps *, uint32_t tag) { return tag >= ROLE && tag <= YUV ? 1 : -1; }
static const VendorOps vendor_ops{tag_count, tag_all, tag_section, tag_name, tag_type, {}};

static Status vendor_tags(void *self, std::vector<VendorSection> *out) {
    auto status = original_tags(self, out);
    if (!status.isOk() || !out) return status;
    *out = {{"com.xiaomi.cameraid.role", {{static_cast<int32_t>(ROLE), "cameraId", 1},
                                        {static_cast<int32_t>(ROLES), "cameraIds", 1}}},
            {"xiaomi.yuv", {{static_cast<int32_t>(YUV), "format", 1}}}};
    return status;
}

template<class T> static int add_preview_size(void *copy, uint32_t tag, uint8_t type) {
    Entry entry{};
    if (find_entry(copy, tag, &entry) || entry.type != type || entry.count % 4 || entry.count > 4096)
        return -1;
    const auto *data = static_cast<const T *>(entry.data.raw);
    std::vector<T> values(data, data + entry.count);
    std::vector<T> formats;
    for (size_t i = 0; i < entry.count; i += 4) {
        T format = data[i];
        bool known = false, present = false;
        for (T previous : formats) if (previous == format) known = true;
        if (known) continue;
        formats.push_back(format);
        for (size_t j = 0; j < entry.count; j += 4)
            if (data[j] == format && data[j + 1] == 1024 && data[j + 2] == 768) present = true;
        if (!present) values.insert(values.end(), {format, 1024, 768, data[i + 3]});
    }
    return update_entry(copy, entry.index, values.data(), values.size(), nullptr);
}

static Status characteristics(void *self, Metadata *out) {
    auto status = original_metadata(self, out);
    if (!status.isOk() || !out || out->metadata.empty()) return status;
    const void *old = out->metadata.data();
    Entry facing{}, keys{};
    if (find_entry(old, ACAMERA_LENS_FACING, &facing) || facing.count != 1 ||
        facing.type != 0 || facing.data.u8[0] > 1 ||
        find_entry(old, ACAMERA_REQUEST_AVAILABLE_CHARACTERISTICS_KEYS, &keys) ||
        keys.type != 1 || keys.count > 4096) return status;
    std::vector<int32_t> available(keys.data.i32, keys.data.i32 + keys.count);
    available.insert(available.end(), {static_cast<int32_t>(ROLE), static_cast<int32_t>(ROLES),
                                      static_cast<int32_t>(YUV)});
    void *copy = allocate_metadata(entry_count(old) + 3, data_count(old) + 4096);
    if (!copy) return status;
    int32_t role = facing.data.u8[0] == 0 ? 1 : 0;
    int32_t format = 2; // NV21, matching the existing private YUV plane bridge.
    int error = append_metadata(copy, old);
    if (!error) error = add_entry(copy, ROLE, &role, 1);
    if (!error) error = add_entry(copy, ROLES, &role, 1);
    if (!error) error = add_entry(copy, YUV, &format, 1);
    if (!error) error = update_entry(copy, keys.index, available.data(), available.size(), nullptr);
    // Xiaomi creates a deferred 1024x768 preview. Advertise that renderable
    // scene size so CameraService does not round it to 1280x960 and reject
    // the real SurfaceTexture during updateOutputConfiguration.
    if (!error) error = add_preview_size<int32_t>(copy, ACAMERA_SCALER_AVAILABLE_STREAM_CONFIGURATIONS, 1);
    if (!error) error = add_preview_size<int64_t>(copy, ACAMERA_SCALER_AVAILABLE_MIN_FRAME_DURATIONS, 3);
    if (!error) error = add_preview_size<int64_t>(copy, ACAMERA_SCALER_AVAILABLE_STALL_DURATIONS, 3);
    if (!error) {
        size_t size = metadata_size(copy);
        const auto *bytes = static_cast<const uint8_t *>(copy);
        out->metadata.assign(bytes, bytes + size);
    }
    free_metadata(copy);
    __android_log_print(error ? ANDROID_LOG_ERROR : ANDROID_LOG_INFO,
                        "AVD-Camera-Scene", "role=%d metadata result=%d", role, error);
    return status;
}

static int install_hooks(dl_phdr_info *info, size_t, void *) {
    if (info->dlpi_name && *info->dlpi_name &&
        strcmp(info->dlpi_name, "/vendor/bin/hw/android.hardware.camera.provider.ranchu")) return 0;
    uintptr_t base = info->dlpi_addr;
    auto **tags = reinterpret_cast<void **>(base + 0x4c198);
    auto **metadata = reinterpret_cast<void **>(base + 0x4c040);
    if (*tags != reinterpret_cast<void *>(base + 0x2abe0) ||
        *metadata != reinterpret_cast<void *>(base + 0x1c520)) return 1;
#define RESOLVE(variable, symbol) \
    variable = reinterpret_cast<decltype(variable)>(dlsym(RTLD_DEFAULT, symbol)); \
    if (!variable) return 1;
    RESOLVE(allocate_metadata, "allocate_camera_metadata")
    RESOLVE(free_metadata, "free_camera_metadata")
    RESOLVE(metadata_size, "get_camera_metadata_size")
    RESOLVE(entry_count, "get_camera_metadata_entry_count")
    RESOLVE(data_count, "get_camera_metadata_data_count")
    RESOLVE(append_metadata, "append_camera_metadata")
    RESOLVE(find_entry, "find_camera_metadata_ro_entry")
    RESOLVE(add_entry, "add_camera_metadata_entry")
    RESOLVE(update_entry, "update_camera_metadata_entry")
    RESOLVE(status_delete, "AStatus_delete")
    RESOLVE(status_ok, "AStatus_isOk")
#undef RESOLVE
    auto set_ops = reinterpret_cast<int (*)(const VendorOps *)>(
                   dlsym(RTLD_DEFAULT, "set_camera_metadata_vendor_ops"));
    if (!set_ops || set_ops(&vendor_ops)) return 1;
    long page = sysconf(_SC_PAGESIZE);
    if (page <= 0) return 1;
    uintptr_t address = reinterpret_cast<uintptr_t>(tags) & ~(static_cast<uintptr_t>(page) - 1);
    if ((reinterpret_cast<uintptr_t>(metadata) & ~(static_cast<uintptr_t>(page) - 1)) != address ||
        mprotect(reinterpret_cast<void *>(address), page, PROT_READ | PROT_WRITE)) return 1;
    original_tags = reinterpret_cast<decltype(original_tags)>(*tags);
    original_metadata = reinterpret_cast<decltype(original_metadata)>(*metadata);
    *tags = reinterpret_cast<void *>(vendor_tags);
    *metadata = reinterpret_cast<void *>(characteristics);
    mprotect(reinterpret_cast<void *>(address), page, PROT_READ);
    __android_log_print(ANDROID_LOG_INFO, "AVD-Camera-Scene", "scene metadata bridge active");
    return 1;
}

__attribute__((constructor)) static void initialize() { dl_iterate_phdr(install_hooks, nullptr); }
