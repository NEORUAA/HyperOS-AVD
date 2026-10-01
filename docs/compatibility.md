# Compatibility notes

Validated platform: Apple Silicon macOS, Android Emulator 37.1.11, API 36
Google Play ARM64 system image revision 7, stock ranchu Android 15 / 6.6 kernel,
HyperOS 3.0.2.0.WMCCNXM MysticGSI, KernelSU v3.3.0 (32601), early-boot LKM.
Other host architectures and SDK base revisions are not established.

On the tested 16 GiB Mac, running the existing Pixel AVD together with two
HyperOS test AVDs caused severe swapping and ADB timeouts. Serializing the
HyperOS tests resolved those timeouts. Use one HyperOS instance at a time on
hosts with similar memory capacity.

## GPS

The previous GPS-enabled configuration could deadlock `system_server`:

```text
android.fg: GnssLocationProvider.stopNavigating()
  -> synchronized GnssHal.stop()
    -> ranchu Gnss::stop()
      -> synchronous SESSION_END callback
        -> GnssNative.reportStatus()
          -> GnssStatusProvider / ListenerMultiplexer lock
```

A concurrent listener operation can hold the listener lock while waiting for
`GnssHal`, creating the reverse lock order. The watchdog then kills
`system_server`; apps can stall or show blank content before boot animation.
Disabling GPS avoided that path but removed functionality.

The compatibility patch changes only four smali classes in `services.jar`'s
`classes2.dex`: `reportStatus()`, `reportSvStatus()` and `reportLocation()` post their existing
callback adapters to `FgThread.getHandler()`. The adapters implement `Runnable`
while retaining
`Binder.withCleanCallingIdentity()`. Queuing on the same foreground handler
lets the HAL call return and release its monitor before status delivery.
A second captured ANR showed a hardware callback holding the HAL mutex while
waiting for `GnssStatusProvider`, with Xiaomi DFS holding its listener lock and
waiting for HAL `stopSvStatus()`. Deferring satellite status and first-fix
notifications breaks that path too. The patch rejects a services.jar whose
SHA-256 differs from the pinned GSI.

The ranchu HAL, real GPS provider, and Xiaomi fused provider remain available.
Hardware coordinate injection is not Android's mock-location provider API.
Tests cover injected Hong Kong/London coordinates and repeated GNSS start/stop.
Xiaomi network geolocation, geocoding, physical satellite reception and all
Google fused-location use cases are not established by those tests.

## Enforcing SELinux

The GSI's `permissiver.rc` early-init action is removed. Three Xiaomi
CameraServiceProxy scene booleans are individually labeled
`exported_system_prop`, matching related Xiaomi scene properties:

- `persist.vendor.camera.3rdhighResolutionBlob.scenes`
- `persist.vendor.camera.3rdlive.scenes`
- `persist.vendor.camera.3rdvideocall.scenes`

Without these labels, property-setting failures abort framework boot.
The ranchu ADB setup script receives its `goldfish_system_setup_exec` label.
There is no broad `vendor_default_prop` allow rule.

The GSI mislabeled `logcat` as `logd_exec`; its label is corrected to
`logcat_exec` so ordinary ADB can execute it. Settings' log-persistence property
writes receive the narrow `system_app` → `logpersistd_logging_prop` permission
used by AOSP userdebug policy. The GSI enables debugging but originally omitted
that allow, which caused Developer Options to crash under enforcement.

Global enforcement and `/sys/fs/selinux/enforce=1` are checked with KernelSU state
hiding disabled. KernelSU's privileged `ksu` domain remains permissive by its
official policy. Some optional Xiaomi hardware/services still produce AVC
denials; this is not a full OEM policy port.

## Display and media

The Xiaomi overlay supplies 1080-pixel masks, explicit 440-dpi resources,
121-pixel top safe inset and 118-pixel corner radius. Running at 720 pixels wide
scaled only one axis of the mask, elongating corners and the hole. The AVD now
uses 1080 × 2400 at 440 dpi without changing the original overlay.

The stock vendor and kernel modules are retained. Separate product/system_ext
mounts are disabled because those directories are embedded in the GSI. Ranchu
radio/APN initialization and host modem profiles are included. HyperOS's 64-bit
mediaserver is selected and its ARM32-only drmserver is replaced by the ARM64
API 36 executable. Service startup does not establish DRM playback or audio.
AVB verification is disabled for this development image.

## Data and recovery

Ordinary starts preserve userdata. Image import/build must run while this
checkout's AVD is stopped. Never copy userdata, encryption keys or snapshots
from an unrelated AVD. If restoring a manual backup, restore the userdata base
and its qcow2 overlay together while the device is stopped.

## First boot

The clean-data release test completed boot and automatic KernelSU installation.
A one-time SystemUI exception was observed in Xiaomi's lock-screen magazine
`onDeviceProvisioned()` path. SystemUI recovered and system_server remained
running. This is retained as an experimental GSI limitation; the GPS test does
not establish full Xiaomi lock-screen/cloud-service compatibility.
