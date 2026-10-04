// Tag the emulator's sRGB output without changing Android pixels or host settings.
#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#import <objc/runtime.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void (*originalViewDidMoveToWindow)(id, SEL);

static void srgbViewDidMoveToWindow(id object, SEL selector) {
    originalViewDidMoveToWindow(object, selector);
    NSView *view = object;
    const char *name = object_getClassName(view);
    if (strcmp(name, "EmuGLView") && strcmp(name, "EmuGLViewWithMetal")) return;
    NSWindow *window = view.window;
    if (!window) return;
    NSColorSpace *srgb = NSColorSpace.sRGBColorSpace;
    if (![window.colorSpace isEqual:srgb]) {
        fprintf(stderr, "HyperOSAVDColor: %s window color space %s -> sRGB\n",
                name, window.colorSpace.localizedName.UTF8String ?: "untagged");
        window.colorSpace = srgb;
    }
    if ([view.layer isKindOfClass:CAMetalLayer.class]) {
        CAMetalLayer *layer = (CAMetalLayer *)view.layer;
        layer.colorspace = srgb.CGColorSpace;
    }
}

__attribute__((constructor)) static void installSrgbTag(void) {
    const char *enabled = getenv("HYPEROS_AVD_SRGB");
    if (!enabled || strcmp(enabled, "1")) return;
    NSArray<NSString *> *arguments = NSProcessInfo.processInfo.arguments;
    NSUInteger index = [arguments indexOfObject:@"-avd"];
    if (index == NSNotFound || index + 1 >= arguments.count ||
        ![arguments[index + 1] isEqualToString:@"HyperOS_4_Official_API_37"]) return;
    Method method = class_getInstanceMethod(NSView.class, @selector(viewDidMoveToWindow));
    if (!method) return;
    originalViewDidMoveToWindow = (void (*)(id, SEL))method_getImplementation(method);
    method_setImplementation(method, (IMP)srgbViewDidMoveToWindow);
    fprintf(stderr, "HyperOSAVDColor: sRGB tagging enabled for the owned OS4 AVD\n");
}
