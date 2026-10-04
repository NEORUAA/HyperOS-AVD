/* CPU shared-memory and GL query bridges for the verified Parrot 8.2 library.
 * This is not an ION or GPU allocator. Unsupported GPU imports return failure
 * so the camera can retain its existing CPU fallback. Never install globally.
 */
#define _GNU_SOURCE
#include <android/sharedmem.h>
#include <android/log.h>
#include <GLES2/gl2.h>
#include <dlfcn.h>
#include <elf.h>
#include <errno.h>
#include <link.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

typedef struct Buffer {
    void *pointer;
    size_t size;
    int fd;
    struct Buffer *next;
} Buffer;
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static Buffer *buffers;

/* The emulator rejects REQUIRED_TEXTURE_IMAGE_UNITS_OES before reaching its
 * own implementation, which returns one for its RGBA-backed external texture.
 * Repair only the Java GL query in this process. The runtime SHA is checked by
 * the installer; its build ID and RELRO slot are checked again at runtime.
 * All other queries, GL errors and drawing operations retain their behavior.
 */
static void (*original_texture_query)(GLenum, GLenum, GLint *);
static pthread_once_t query_once = PTHREAD_ONCE_INIT;
static void *query_library;
static const unsigned char runtime_build_id[] = {
    0x40, 0xfc, 0x8b, 0x54, 0x16, 0x6f, 0x31, 0xa2,
    0xa7, 0xbe, 0x6a, 0x58, 0x5c, 0xd4, 0x51, 0x53,
};
static void texture_query(GLenum target, GLenum name, GLint *value) {
    if (name == 0x8d68 && (target == GL_TEXTURE_2D || target == 0x8d65) && value) {
        const char *renderer = (const char *)glGetString(GL_RENDERER);
        if (renderer && strstr(renderer, "Android Emulator")) {
            *value = 1;
            __android_log_print(ANDROID_LOG_INFO, "AVD-Camera-GL",
                                "external texture unit query repaired: target=%x", target);
            return;
        }
    }
    original_texture_query(target, name, value);
}
static int install_query(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size; (void)data;
    const char *name = strrchr(info->dlpi_name, '/');
    if (!name || strcmp(name + 1, "libandroid_runtime.so")) return 0;
    uintptr_t slot = info->dlpi_addr + 0x2e2118;
    int verified = 0, relro = 0;
    for (unsigned int i = 0; i < info->dlpi_phnum; ++i) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        uintptr_t start = info->dlpi_addr + ph->p_vaddr;
        if (ph->p_type == PT_GNU_RELRO && slot >= start &&
            slot + sizeof(void *) <= start + ph->p_memsz) relro = 1;
        if (ph->p_type != PT_NOTE) continue;
        const unsigned char *p = (const unsigned char *)start;
        const unsigned char *end = p + ph->p_memsz;
        while ((size_t)(end - p) >= sizeof(ElfW(Nhdr))) {
            const ElfW(Nhdr) *n = (const ElfW(Nhdr) *)p;
            size_t names = ((size_t)n->n_namesz + 3) & ~(size_t)3;
            size_t desc = ((size_t)n->n_descsz + 3) & ~(size_t)3;
            if (names + desc > (size_t)(end - p) - sizeof(*n)) break;
            if (n->n_type == NT_GNU_BUILD_ID && n->n_namesz == 4 &&
                !memcmp(p + sizeof(*n), "GNU", 4) &&
                n->n_descsz == sizeof(runtime_build_id) &&
                !memcmp(p + sizeof(*n) + names, runtime_build_id, sizeof(runtime_build_id)))
                verified = 1;
            p += sizeof(*n) + names + desc;
        }
    }
    if (!verified || !relro) return 1;
    void *previous = *(void **)slot;
    Dl_info symbol;
    if (!dladdr(previous, &symbol) || !symbol.dli_sname ||
        strcmp(symbol.dli_sname, "glGetTexParameteriv")) return 1;
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) return 1;
    void *page = (void *)(slot & ~((uintptr_t)page_size - 1));
    if (mprotect(page, page_size, PROT_READ | PROT_WRITE)) return 1;
    original_texture_query = (void (*)(GLenum, GLenum, GLint *))previous;
    __atomic_store_n((void **)slot, (void *)texture_query, __ATOMIC_RELEASE);
    if (mprotect(page, page_size, PROT_READ))
        __android_log_print(ANDROID_LOG_ERROR, "AVD-Camera-GL", "RELRO restore failed");
    __android_log_print(ANDROID_LOG_INFO, "AVD-Camera-GL", "private texture query bridge installed");
    return 1;
}
static void install_queries(void) {
    /* Gcam dlcloses its AION provider when changing cameras. The process-local
     * GL callback must remain mapped until process exit, including between
     * contexts. Hold one provider reference before publishing that callback.
     */
    Dl_info own;
    if (!dladdr((void *)texture_query, &own) || !own.dli_fname) return;
    query_library = dlopen(own.dli_fname, RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
    if (!query_library) {
        __android_log_print(ANDROID_LOG_ERROR, "AVD-Camera-GL", "provider pin failed: %s", dlerror());
        return;
    }
    dl_iterate_phdr(install_query, NULL);
}

/* The caller retains buffer ownership while borrowing its fd or pointer. */
static Buffer *find(void *pointer) {
    uintptr_t value = (uintptr_t)pointer;
    for (Buffer *b = buffers; b; b = b->next)
        if (value >= (uintptr_t)b->pointer && value - (uintptr_t)b->pointer < b->size)
            return b;
    return NULL;
}

/* Takes ownership of fd, including on failure. */
static void *map_fd(int fd, size_t size) {
    size_t available = fd >= 0 ? ASharedMemory_getSize(fd) : 0;
    struct stat status;
    /* Legacy ashmem is a character device with st_size=0. Query its real
     * allocation size through the Android API before falling back to memfd.
     */
    if (!available && fd >= 0 && fstat(fd, &status) == 0 && status.st_size > 0)
        available = status.st_size;
    if (fd < 0 || !size || size > 256 * 1024 * 1024 || available < size) {
        if (fd >= 0) close(fd);
        errno = EINVAL;
        return NULL;
    }
    void *pointer = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (pointer == MAP_FAILED) { close(fd); return NULL; }
    Buffer *b = calloc(1, sizeof(*b));
    if (!b) { munmap(pointer, size); close(fd); return NULL; }
    b->pointer = pointer; b->size = size; b->fd = fd;
    pthread_mutex_lock(&mutex);
    b->next = buffers; buffers = b;
    pthread_mutex_unlock(&mutex);
    return pointer;
}

void AIonInit(void) {
    pthread_once(&query_once, install_queries);
    __android_log_print(ANDROID_LOG_INFO, "AVD-AIon", "CPU shared-memory bridge initialized");
}
/* Buffers have explicit ownership; initialization may be shared by contexts. */
void AIonDeinit(void) {}

void *AIonAlloc(uint32_t size, uint32_t alignment_log2, uint32_t heap, int cached) {
    (void)heap; (void)cached;
    if (alignment_log2 > 12) { errno = ENOTSUP; return NULL; }
    if (!size || size > 256 * 1024 * 1024) { errno = EINVAL; return NULL; }
    return map_fd(ASharedMemory_create("hyperos-avd-camera", size), size);
}

void AIonFree(void *pointer) {
    pthread_mutex_lock(&mutex);
    Buffer **link = &buffers;
    while (*link && (*link)->pointer != pointer) link = &(*link)->next;
    Buffer *b = *link;
    if (b) *link = b->next;
    pthread_mutex_unlock(&mutex);
    if (b) { munmap(b->pointer, b->size); close(b->fd); free(b); }
}

int AIonGetFd(void *pointer) {
    pthread_mutex_lock(&mutex); Buffer *b = find(pointer);
    int fd = b ? b->fd : -1; pthread_mutex_unlock(&mutex); return fd;
}
uint32_t AIonGetSize(void *pointer) {
    pthread_mutex_lock(&mutex); Buffer *b = find(pointer);
    uint32_t size = b ? b->size - ((uintptr_t)pointer - (uintptr_t)b->pointer) : 0;
    pthread_mutex_unlock(&mutex); return size;
}
void *AIonFindFromFd(int fd) {
    pthread_mutex_lock(&mutex); void *pointer = NULL;
    for (Buffer *b = buffers; b; b = b->next)
        if (b->fd == fd) { pointer = b->pointer; break; }
    pthread_mutex_unlock(&mutex); return pointer;
}
void *AIonImportFd(int fd, uint32_t size) {
    size_t available = fd >= 0 ? ASharedMemory_getSize(fd) : 0;
    struct stat status;
    if (!available && fd >= 0 && fstat(fd, &status) == 0 && status.st_size > 0)
        available = status.st_size;
    if (!size && available <= UINT32_MAX) size = available;
    return map_fd(dup(fd), size);
}
/* A view borrows the parent mapping and must not outlive the parent. */
void *AIonImportView(void *pointer, uint32_t offset, uint32_t size) {
    uint32_t available = AIonGetSize(pointer);
    if (!available || offset >= available || size > available - offset) {
        errno = EINVAL; return NULL;
    }
    return (char *)pointer + offset;
}
int *AIonGetFileDescriptors(void *pointer, int *count) {
    if (!count) { errno = EINVAL; return NULL; }
    pthread_mutex_lock(&mutex); Buffer *b = find(pointer);
    *count = b ? 1 : 0; int *fds = b ? &b->fd : NULL;
    pthread_mutex_unlock(&mutex); return fds;
}
int AIonBufferSyncStart(void *pointer, int mode) {
    (void)mode;
    if (AIonGetFd(pointer) < 0) { errno = EINVAL; return -1; }
    /* CPU mappings are coherent and never imported into a GPU device. */
    atomic_thread_fence(memory_order_seq_cst); return 0;
}
int AIonBufferSyncEnd(void *pointer, int mode) { return AIonBufferSyncStart(pointer, mode); }
int AIonGetHeapId(int type) { (void)type; return 0; }
void AIonGetProviderString(const char **text, int *length) {
    if (text) *text = "AVD";
    if (length) *length = 3;
}
void *AIonMapNativeImageBuffer(void *pointer, uint32_t w, uint32_t h,
                             uint32_t a, uint32_t b, uint32_t c, uint32_t d) {
    (void)pointer; (void)w; (void)h; (void)a; (void)b; (void)c; (void)d;
    errno = ENOTSUP; return NULL;
}
void *AIonMapNativeImageBufferWithOffset(void *pointer, uint32_t w, uint32_t h,
                                       uint32_t a, uint32_t b, uint32_t c, uint32_t d) {
    return AIonMapNativeImageBuffer(pointer, w, h, a, b, c, d);
}
void AIonUnmapNativeImageBuffer(void *pointer) { (void)pointer; }
