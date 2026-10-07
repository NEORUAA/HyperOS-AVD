/* Test the configured Millet netlink protocol without registering a monitor. */
#include <errno.h>
#include <dlfcn.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char *end;
    if (argc != 2 || !argv[1][0]) return 3;
    if (!strcmp(argv[1], "gnss-extension")) {
        void *library = dlopen("libbinder_ndk.so", RTLD_NOW | RTLD_LOCAL);
        if (!library) return 3;
        bool (*declared)(const char *) = dlsym(library, "AServiceManager_isDeclared");
        if (!declared) { dlclose(library); return 3; }
        /* Confirm the standard declared HAL before testing the optional one. */
        int result = !declared("android.hardware.gnss.IGnss/default") ? 3 :
            (declared("vendor.qti.gnss.ILocAidlGnss/default") ? 0 : 2);
        dlclose(library);
        return result;
    }
    errno = 0;
    long protocol = strtol(argv[1], &end, 10);
    if (errno || *end || protocol < 1 || protocol > 31) return 3;
    int fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, (int)protocol);
    if (fd >= 0) {
        close(fd);
        return 0;
    }
    /* Permission or resource errors are not evidence of an unsupported HAL. */
    if (errno == EPROTONOSUPPORT) return 2;
    fprintf(stderr, "Netlink capability probe failed: errno=%d\n", errno);
    return 3;
}
