// Private EGL bridge for the hash-pinned Xiaomi Weather MGL workload.
#include <EGL/egl.h>
#include <EGL/eglext.h>

EGLDisplay eglGetDisplay(EGLNativeDisplayType display) {
    // This ANGLE revision caps MoltenVK to ES2 because provokingVertexLast is
    // unavailable. Weather's flat rain varyings are uniform-derived constants,
    // so selecting the first or last vertex gives the same value. The workload
    // uses no geometry shaders. Keep this override local to this EGL display;
    // never change a system property or another app's driver selection.
    static const char* enabled[] = {"exposeES32ForTesting", 0};
    static const EGLAttrib attributes[] = {
        0x3203, 0x3450, // EGL_PLATFORM_ANGLE_TYPE_ANGLE: Vulkan
        0x3466, (EGLAttrib)enabled,
        EGL_NONE,
    };
    return eglGetPlatformDisplay(0x3202, display, attributes);
}
