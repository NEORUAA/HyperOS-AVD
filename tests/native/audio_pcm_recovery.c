// Exercise the actual pinned ARM64 PCM ELF using a fake tinyalsa ioctl backend.
// No real sound devices or audio services are touched by this probe.
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct transfer { long result; void *buffer; unsigned long frames; };
struct context {
    unsigned state;
    int errors_left, error, writes, stops, prepares, stop_error, prepare_error;
    int partial;
};

static int fake_ioctl(void *opaque, unsigned request, void *argument) {
    struct context *c = opaque;
    switch (request) {
    case 0x40184150: {
        ++c->writes;
        if (c->errors_left != 0) {
            --c->errors_left;
            errno = c->error;
            return -1;
        }
        struct transfer *t = argument;
        t->result = c->partial ? c->partial : (long)t->frames;
        return 0;
    }
    case 0x4143:
        ++c->stops;
        if (c->stop_error) { errno = c->stop_error; return -1; }
        c->state = 1;
        return 0;
    case 0x4140:
        ++c->prepares;
        if (c->prepare_error) { errno = c->prepare_error; return -1; }
        c->state = 2;
        return 0;
    default:
        fprintf(stderr, "Unexpected ioctl: %#x\n", request);
        abort();
    }
}

typedef int (*writei_fn)(void *, const void *, unsigned);
static void check(writei_fn writei, const char *name, struct context c,
                  unsigned flags, unsigned frames, int result,
                  int writes, int stops, int prepares, int error) {
    // Offsets are pinned by the vendor SHA and verified disassembly.
    _Alignas(16) unsigned char pcm[4096] = {0};
    void *ops[3] = {NULL, NULL, (void *)fake_ioctl};
    *(unsigned *)(pcm + 4) = flags;
    *(void **)(pcm + 0xd8) = &c.state;
    *(void **)(pcm + 0x110) = ops;
    *(void **)(pcm + 0x118) = &c;
    int16_t samples[32] = {0};
    errno = EIO; // Catch accidental recovery of argument errors via stale errno.
    int actual = writei(pcm, samples, frames);
    if (actual != result || c.writes != writes || c.stops != stops
            || c.prepares != prepares || (error && errno != error)) {
        fprintf(stderr, "FAIL %s: result=%d writes=%d stop=%d prepare=%d errno=%d\n",
                name, actual, c.writes, c.stops, c.prepares, errno);
        exit(1);
    }
    printf("PASS %s\n", name);
}

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    void *baseline = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!baseline) { fprintf(stderr, "%s\n", dlerror()); return 2; }
    writei_fn original = (writei_fn)dlsym(baseline, "pcm_writei");
    if (!original) return 2;
    check(original, "baseline leaves EIO unrecovered",
          (struct context){.state=2, .errors_left=1, .error=EIO},
          0, 16, -1, 1, 0, 0, EIO);
    // Unload to prevent a shared SONAME from hiding the candidate library.
    dlclose(baseline);
    void *library = dlopen(argv[2], RTLD_NOW | RTLD_LOCAL);
    if (!library) { fprintf(stderr, "%s\n", dlerror()); return 2; }
    writei_fn fixed = (writei_fn)dlsym(library, "pcm_writei");
    if (!fixed) return 2;
    check(fixed, "normal playback", (struct context){.state=2},
          0, 16, 16, 1, 0, 0, 0);
    check(fixed, "one EIO recovers", (struct context){.state=2, .errors_left=1, .error=EIO},
          0, 16, 16, 2, 1, 1, 0);
    check(fixed, "permanent EIO has only one retry", (struct context){.state=2, .errors_left=100, .error=EIO},
          0, 16, -1, 2, 1, 1, EIO);
    check(fixed, "EAGAIN unchanged", (struct context){.state=2, .errors_left=1, .error=EAGAIN},
          0, 16, -1, 1, 0, 0, EAGAIN);
    check(fixed, "NORESTART respected", (struct context){.state=2, .errors_left=1, .error=EIO},
          0x40000000, 16, -1, 1, 0, 0, EIO);
    check(fixed, "stop failure preserves EIO", (struct context){.state=2, .errors_left=1, .error=EIO, .stop_error=ENODEV},
          0, 16, -1, 1, 1, 0, EIO);
    check(fixed, "prepare failure preserves EIO", (struct context){.state=2, .errors_left=1, .error=EIO, .prepare_error=EINVAL},
          0, 16, -1, 1, 1, 1, EIO);
    check(fixed, "capture direction unchanged", (struct context){.state=2},
          0x10000000, 16, -EINVAL, 0, 0, 0, 0);
    check(fixed, "invalid frame count unchanged", (struct context){.state=2},
          0, (unsigned)INT_MAX+1, -EINVAL, 0, 0, 0, 0);
    check(fixed, "partial write remains real", (struct context){.state=2, .partial=8},
          0, 16, 8, 1, 0, 0, 0);
    check(fixed, "zero frames unchanged", (struct context){.state=2},
          0, 0, 0, 1, 0, 0, 0);
    check(fixed, "initial prepare unchanged", (struct context){.state=1},
          0, 16, 16, 1, 0, 1, 0);
    dlclose(library);
    return 0;
}
