# Xiaomi Watch 5 启动实验

这是独立的测试 AVD，已完成 Android 启动，原版表盘、应用网格和设置页面均能显示和操作。OS3/OS4 手机和 Pixel AVD 不参与本实验。

| 项目 | 当前配置 |
| --- | --- |
| AVD / ADB | `HyperOS_Watch5_API_34` / `emulator-5576` |
| 工作目录 | `work/watch5`，生成内容不进入 Git |
| 原包 | `grasslte`，`OS3.0.190.0.VOFAECNXM` |
| Android | 原包 metadata 为 Android 14 / API 34；文件名中的 `15.0` 不作为判断依据 |
| 显示 | 圆屏 480 × 480；物理 PPI 312，沿用原包逻辑 density 320 |
| 内存 / 存储 | 2 GiB / 32 GiB |
| CPU | ARM64 内核 + 原包 ARM32 用户空间，使用 TCG 软件模拟 |
| 硬件参考 | 官方 Wear OS 5 API 34 ARM64 镜像 |

原包中的 init、桌面原生库和系统 APEX 都是 32 位 ARM。Apple Silicon 的 HVF 路径不能执行这些进程，不能仅改 ABI 属性来使用硬件加速。独立的 ARM32 测试程序已在 TCG 下成功执行。

原包已挂载 system、system_ext、product、mi_ext。已补齐 ARM64 硬件服务的运行库，KeyMint、keystore2、BPF 和 netd 正常启动；ADB 已连通，来宾确认 SELinux 为 Enforcing、用户分区约 31 GiB。已保留原包媒体配置，并编译 ARM32 Goldfish 图形驱动；EGL 和 Mapper 已连接 Apple M4 图形后端，原版开机动画和语言选择页均已绘制；来宾确认 `sys.boot_completed=1`、开机动画服务已停止。

已验证语言选择、原版教程、数字表盘、应用网格及滑动，点击心率和闹钟会打开各自的隐私说明页；设置主页面及“我的设备”子页面已实际操作并返回。框架可以枚举 12 个 Goldfish 模拟传感器，但应用层传感器数据、声音和手机配对尚未验收，心率、血氧等专用硬件没有等效模拟。高通 PSMService 依赖实体设备的专用休眠 HAL，在 AVD 中会循环崩溃并触发 RescueParty；测试镜像不加载这一个包，电源管理由 ranchu 提供，原始 OTA 保留不变。

保留用户分区的重启已通过：来宾再次报告启动完成，测试文件和教程状态保持一致。独立模式下，表冠键已验证从设置返回表盘、从表盘打开应用网格；长按表盘能看到原版预装表盘的选择和预览界面。配对入口的恢复已回读包状态和默认 HOME，尚未连接真实手机。

已检查 [Google 官方 Wear 镜像索引](https://dl.google.com/android/repository/sys-img/android-wear/sys-img2-3.xml)：目前提供的 ARM32 包是 `system-images;android-25;android-wear;armeabi-v7a` revision 3。已下载并验证官方 SHA1，解包到 `work/watch5/input/wear-api25`。镜像实际为 Android 7.1.1，没有独立 Vendor 镜像；已确认有 ARM32 EGL、gralloc、传感器和 `qemu-props`，可作为驱动移植来源，但尚未验证其在 Android 14 下的运行兼容性。

不能整体套用这份旧内核和硬件层：它启用了 `CONFIG_ANDROID_BINDER_IPC_32BIT`，未启用 ext4 加密，提供的是 Android 8 之前的 Legacy HAL，也没有现代 KeyMint 服务。[AOSP 的 Legacy HAL 文档](https://source.android.com/docs/core/architecture/hal/archive)说明这类接口没有严格的 ABI 稳定保证。当前保留支持 ARM32 用户进程的现代 ARM64 内核及小米 Android 14 系统，并编译 Android 14 Goldfish 图形源码为 ARM32；旧 Wear 镜像保留作兼容性参考。使用 ARM32 镜像仍需要 TCG，不会恢复 Apple Silicon 的 HVF 加速。检查结果保存在 `work/watch5/local/arm32-wear-audit.json`。

构建前安装官方参考镜像，并将原始 OTA 放在仓库根目录：

```sh
sdkmanager --install 'system-images;android-34;android-wear;arm64-v8a'
python3 scripts/watch5.py build --diagnostic-adb
./Start-Watch5.command
```

`--diagnostic-adb` 仅用于这个本地实验，关闭 ADB 身份验证，不适合作为发布配置。脚本校验指定 OTA 的 SHA256，并拒绝替换正在运行的测试镜像。通过 `Start-Watch5.command` 启动：此阶段需要直接选用 ARM64 模拟器核心，Android Studio 的普通 Start 尚未适配。

Android 启动完成后，可在当前工作区使用以下命令。它们核对本工作区的 Watch5 PID 和端口，并拒绝控制其他 AVD：

```sh
python3 scripts/watch5.py standalone  # Use the original watch face as HOME without phone pairing
python3 scripts/watch5.py home        # Open the original watch UI
python3 scripts/watch5.py crown       # Send the primary crown key (opens apps from the watch face)
python3 scripts/watch5.py back        # Send the Android back key
python3 scripts/watch5.py pairing     # Restore the original phone-pairing entry
python3 scripts/watch5.py stop        # Stop only this Watch5 instance and retain userdata
```

独立测试模式调整当前用户的 Android 引导标记和默认 HOME，并暂时停用配对引导应用；APK 和配对数据保留，`pairing` 命令会重新启用它，不代表已完成手机配对。原包的应用列表有三页新手教程，本次测试通过其 `settings_key_is_first_launch` 完成标记进入网格，其他应用的隐私说明没有代为接受。

当前 ARM32 图形编译工具和审计产物保存在 `work/watch5/work/graphics`，构建镜像时从该本地缓存注入；这部分仍处于实验阶段，尚未整理为可发布的自动构建流程。

软件模拟默认设置 `ro.hw_timeout_multiplier=10`，扩大框架的有限等待时间，避免慢速首次初始化被 Watchdog 当作卡死；并保留原包 ARM32 APEX，单独补齐 ARM64 硬件服务的崩溃诊断工具。

启动日志在 `work/watch5/logs`，构建和启动状态在 `work/watch5/local`。Wear 参考系统的临时数据保存在 `work/watch5/backups`，与原包测试数据分开。

2026-10-05，在同一台 Mac、同一个 Wear 参考系统中，用相同的静态 ARM64 整数循环各测 3 次：HVF 中位耗时 0.04158 秒，TCG 为 0.33497 秒，约慢 8.06 倍。结果只反映该 CPU 测试的加速方式差异，不代表 ARM32 小米应用、界面 FPS 或真机性能。完整结果保存在 `work/watch5/local/performance.json`。

本次窄范围检查：`python3 -m unittest discover -s tests -p test_watch5.py`，9 项通过，覆盖属性替换、OTA metadata、ramdisk 保留、原生库架构边界，以及拒绝控制其他 AVD 和未完成启动的实例。
