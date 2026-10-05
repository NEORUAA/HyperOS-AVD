# Xiaomi Watch 5 启动实验

这是独立的测试 AVD，尚未进入小米手表桌面。OS3/OS4 手机和 Pixel AVD 不参与本实验。

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

原包已成功挂载 system、system_ext、product、mi_ext，并进入第二阶段 init，SELinux 保持 Enforcing。当前阻塞在加密数据初始化：Wear 硬件参考中的 KeyMint 等服务是 ARM64，要求 `/system/bin/linker64`，原包没有这个解释器。vold 持续等待 keystore2，尚未完成启动；图形、声音、传感器和配对功能均未验收。下一阶段需要适配兼容的硬件服务及原生运行库。

已检查 [Google 官方 Wear 镜像索引](https://dl.google.com/android/repository/sys-img/android-wear/sys-img2-3.xml)：目前提供的 ARM32 包是 `system-images;android-25;android-wear;armeabi-v7a` revision 3。已下载并验证官方 SHA1，解包到 `work/watch5/input/wear-api25`。镜像实际为 Android 7.1.1，没有独立 Vendor 镜像；已确认有 ARM32 EGL、gralloc、传感器和 `qemu-props`，可作为驱动移植来源，但尚未验证其在 Android 14 下的运行兼容性。

不能整体套用这份旧内核和硬件层：它启用了 `CONFIG_ANDROID_BINDER_IPC_32BIT`，未启用 ext4 加密，提供的是 Android 8 之前的 Legacy HAL，也没有现代 KeyMint 服务。[AOSP 的 Legacy HAL 文档](https://source.android.com/docs/core/architecture/hal/archive)说明这类接口没有严格的 ABI 稳定保证。拟保留支持 ARM32 用户进程的现代 ARM64 内核及小米 Android 14 系统，逐项验证和适配旧镜像中的 ARM32 驱动；密钥等现代服务需另外补齐兼容实现或运行库。使用 ARM32 镜像仍需要 TCG，不会恢复 Apple Silicon 的 HVF 加速。检查结果保存在 `work/watch5/local/arm32-wear-audit.json`。

构建前安装官方参考镜像，并将原始 OTA 放在仓库根目录：

```sh
sdkmanager --install 'system-images;android-34;android-wear;arm64-v8a'
python3 scripts/watch5.py build --diagnostic-adb
./Start-Watch5.command
```

`--diagnostic-adb` 仅用于这个本地实验，关闭 ADB 身份验证，不适合作为发布配置。脚本校验指定 OTA 的 SHA256，并拒绝替换正在运行的测试镜像。通过 `Start-Watch5.command` 启动：此阶段需要直接选用 ARM64 模拟器核心，Android Studio 的普通 Start 尚未适配。

启动日志在 `work/watch5/logs`，构建和启动状态在 `work/watch5/local`。Wear 参考系统的临时数据保存在 `work/watch5/backups`，与原包测试数据分开。

2026-10-05，在同一台 Mac、同一个 Wear 参考系统中，用相同的静态 ARM64 整数循环各测 3 次：HVF 中位耗时 0.04158 秒，TCG 为 0.33497 秒，约慢 8.06 倍。结果只反映该 CPU 测试的加速方式差异，不代表 ARM32 小米应用、界面 FPS 或真机性能。完整结果保存在 `work/watch5/local/performance.json`。

本次窄范围检查：`python3 -m unittest discover -s tests -p test_watch5.py`，覆盖属性替换、OTA metadata、拼接 ramdisk 的 fstab 修改及失败时保留原文件。
