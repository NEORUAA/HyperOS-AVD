# Rebuilding the firmware

The prebuilt release is the normal installation path. Source builds require:

- Apple Silicon macOS, Python 3 and Android Studio's JBR/JDK 17+
- `brew install e2fsprogs erofs-utils`
- SDK system image `system-images;android-36;google_apis_playstore;arm64-v8a`, **revision 7**
- Source archive `Hyperos-fuxi-16-OS3.0.2.0.WMCCNXM-AB-20251210-MysticGSI.zip`
- Internet on the first build for official KernelSU and Maven smali tools

Place the source ZIP at the repository root, or specify `--zip`. SDK paths
are discovered automatically; `--base` can select the pinned hardware image.
Set `JAVA_HOME` if Android Studio is installed outside `/Applications`.

```sh
adb -s emulator-5566 emu kill
python3 scripts/build_image.py --zip /path/to/Hyperos-fuxi-16-OS3.0.2.0.WMCCNXM-AB-20251210-MysticGSI.zip
./Setup.command
./Start-HyperOS.command
```

The builder rejects an active project console/ADB port. It modifies build copies,
not SDK-installed images or the source archive, and preserves existing AVD data.
SDK revision and KernelSU/smali asset digests are pinned. The GNSS patch also
checks the exact original services.jar digest.

`lp_image.py` reads and writes GPT/liblp metadata, checks hashes, and combines
HyperOS system with ranchu vendor/system_dlkm. `patch_gnss.py` rebuilds only the
DEX containing the affected callback. `init_userdata.py` creates a clean empty
release template; it never uses installed AVD data.

Build artifacts remain in ignored `input/`, `work/`, `images/`, `tools/` and
`logs/`. This build is specific to the stated GSI/kernel/base combination.

OS4 uses an [audited patch catalog and delivery boundaries](os4-patches.md).
Its portable KernelSU module shares native profiles with the image patchers;
kernel, framework and macOS corrections retain their required boot layers.

## Regression checks

```sh
python3 -m unittest discover -s tests -v
python3 scripts/verify_gps.py
```

The GPS check needs SDK platform/build-tools 36.0.0 and JDK tools. It installs a
temporary diagnostic app, wakes/unlocks the project AVD, grants that app location
permissions using KernelSU, injects two coordinates, and observes four GNSS
start/stop cycles. It checks system_server's PID and removes its app afterward.
Its logs and signing key remain in ignored build directories.
