# 官方 HyperOS 4 适配

将小米 18 Pro（`hongkong`）的 **OS4.0.17.0.XFRCNXM / Android 17（API 37）** 官方 OTA 转换为 AVD 镜像。硬件层使用 API 36 ARM64 revision 7 的 ranchu Vendor、4 KB 内核及 KernelSU 3.3.0。

## 构建与启动

使用发布的预构建镜像时，只需仓库 README 中的 Emulator、Platform-Tools 与 Python 3，运行 `./Setup.command --bundle /path/to/manifest.json` 后启动 `./Start-HyperOS4-Official.command`。安装器自动选择 `work/os4-official/`，无需手机 OTA、NDK 或 Build-Tools。下述依赖仅用于重建镜像。

需要原始官方 OTA ZIP、下表中的原始签名 APK、Android Studio SDK、Python 3、JDK 17+，以及 `brew install erofs-utils e2fsprogs lz4`。天气兼容库还需 Android NDK 30。首次提取需要 `tools/payload-dumper-go`，建议预留至少 80 GiB 空间。

```sh
python3 scripts/build_os4_official.py --zip /path/to/hongkong-ota_full-OS4.0.17.0.XFRCNXM-user-17.0-9cf2afb0fc.zip
./Start-HyperOS4-Official.command
```

源分区默认保存到 `work/os4-official/input/hongkong-4.0.17/`，也可用 `--partitions` 指定已提取的 `system / system_ext / product / mi_ext / mi_product`。构建器将这些分区合并为 4 KB EROFS，保留文件权限、所有者、SELinux 与 capability 标签，并合入小米覆盖层和 OS4 软件版本属性。

## 天气与相册预装

| 应用 | 固定版本 | 系统路径 |
| --- | --- | --- |
| 天气 | 18.0.0.27-R | `/product/app/MIUIWeather/MIUIWeather.apk` |
| 相册 | 5.4.2.10-0907-cn | `/product/app/MIUIGallery/MIUIGallery.apk` |

官方 OTA 原本将它们放在 `/product/data-app`，依赖手机的首次开机安装流程。AVD 改为标准系统预装目录，使用已核验的商店版本，保留原 APK 字节与小米签名；旧的两份待安装 APK 从合并镜像移除。ARM64 原生库单独预装，包含天气的 Flutter / ANGLE 修复及共享引擎的相册阴影修复。使用该镜像创建新用户数据时，无需从商店重新下载这两个应用；已有应用数据继续保留，之后仍可安装签名兼容的更新。

已有数据的冷启动已确认两者为 `SYSTEM / UPDATED_SYSTEM_APP`，预装底包路径正确，UID 与数据目录 inode 保持不变；相册页面、天气温度及动态天空已检查。已有数据仍加载 `/data/app` 的更新包属于正常行为。2026-10-03 清空 OS4 用户数据后，两者直接从上表的系统目录加载；禁用不可用的查找设备状态查询后，用户已完成首次开机引导并进入系统。

构建器默认读取 `work/os4-official/input/preinstalled-apps/MIUIWeather.apk` 与 `MIUIGallery.apk`，也可通过 `--preinstalled-apps /path/to/apks` 指定目录；完整 SHA-256 不符会停止构建。在已安装这两个固定版本的官方 OS4 AVD 中，可导出原始签名 APK：

```sh
python3 scripts/preinstall_os4_apps.py --export
```

为已有适配镜像构建独立预装候选（不关闭 AVD、不覆盖当前镜像）：

```sh
python3 scripts/preinstall_os4_apps.py --prepare-image
```

候选位于 `work/os4-official/work/preinstalled-candidate/`。替换镜像前必须关闭**官方 OS4 AVD**，备份 `images/system.img` 与 `work/hyperos-system.img`，保留 `avd/` 用户数据。APK 和候选镜像均被 Git 忽略；发布预构建系统镜像时，它已包含这两个应用，用户无需另行导入 APK。若发布构建材料，则需另附这两份签名 APK。

## 启动与状态

AVD 为 `HyperOS_4_Official_API_37`，ADB 序列号为 `emulator-5574`，默认 4 核、4 GiB 内存；镜像和用户数据均位于 `work/os4-official/`，与 OS3 独立。重建前需关闭此 AVD。

主屏采用官方 Product 分区 `display_id_4630947121878579347.xml` 的 **1120×2436、480 dpi**；480 是 Android 逻辑密度，不是面板 PPI。模拟器的 `emu64a.xml` 完整复用原厂 `hongkong.xml`（含 `support_aod_fullscreen=true`），两者逐字节一致，原厂配置存放于 `config/hongkong.xml`。息屏显示默认开启、始终显示；镜像内的一次性启动服务写入初始设置，后续用户修改不会在每次启动时被覆盖。

保留原厂 `ro.debuggable=0` 并撤掉移植时加入的 console 启动触发器，避免串行控制台通知，以及开发者选项尝试写入 user 构建禁止的 `logd.logpersistd` 属性。ADB 仍需授权；KernelSU 的 shell root 使用独立授权机制。

启动器默认保留小米首次开机引导。确需跳过时使用 `./Start-HyperOS4-Official.command --skip-oobe`；已完成的引导不会自动重置。测试首次开机需先关闭此 AVD 并备份原用户数据，再从空白 userdata 模板启动，系统镜像与预装修复保持不变。

镜像通过原生 `component-override` 默认禁用 `com.xiaomi.finddevice.v2.FindDeviceStatusManagerProvider`，保留查找设备的原签名 APK；这是已验证能够解除 OOBE 黑屏的处理。镜像还包含 SettingsProvider 默认资源覆盖：常亮、关闭睡眠倒计时、最长屏幕超时作为备用。启动器接通模拟器虚拟 AC 电源并对已有数据应用常亮设置，由 `PowerManager` 的常亮模式保持屏幕开启；手动电源键仍可正常关闭屏幕。

`ro.miui.product.home=com.miui.home` 在系统启动前加载，确保首次扫描原装 Quickstep 覆盖层时选用小米桌面，避免新建用户数据时把后台组件缓存成 Launcher3。重建现有镜像的默认配置候选可运行 `python3 scripts/os4_defaults.py --prepare-image`；候选验证完成后，需关闭官方 OS4 AVD 并备份原镜像，再替换 `images/system.img` 与 `work/hyperos-system.img`，保留用户数据。

```sh
adb -s emulator-5574 shell "su -W -c 'id; getenforce'"
adb -s emulator-5574 emu kill
```

## 当前状态

| 项目 | 状态 |
| --- | --- |
| 系统与原生桌面 | 已启动；OS4 版本属性与 Rust v3/v5 注册已确认 |
| KernelSU / SELinux | root 可用，全局 Enforcing；KSU root 域仍为 permissive |
| ADB | 保留授权，`ro.adb.secure=1` |
| Flutter 渲染 | 已修复已核验引擎的深度范围、Float16 feature 配置与 16 字节存储缓冲区对齐；桌面快捷菜单文字、图标、玻璃可显示；相册菜单与底部控件的阴影斜条已冷启动检查 |
| 天气 / 相册预装 | 商店固定版本已纳入构建流程；原签名 APK 与原生兼容库固化到系统镜像 |
| 天气 | 已核验版本使用专属 ANGLE Vulkan 桥接，温度、天空、文字与卡片可显示 |
| 导航 | 桌面身份已固化到启动属性；重置后的 Launcher3 误识别已修正，冷启动后台组件为小米桌面的 `RecentsActivity`，用户已验证生效；此前回桌面动画仍有延迟 |
| 首次开机引导 | 空白用户数据已复测；查找设备状态查询禁用后，用户已完成引导并进入系统；兼容处理纳入镜像默认配置 |
| 默认常亮 | 系统默认资源与启动器均配置常亮；启动时接通虚拟 AC 电源，当前设备已确认 `mStayOn=true` |
| 已知问题 | 部分后台缩略图仍为白色；其它桌面设置未完成回归；切换镜像首次启动曾出现一次 `goldfish_sync` 内核崩溃，重启模拟器进程后恢复；首次着色器初始化可能较慢 |
| GPS | 已合入 Android 17 GNSS 回调补丁；官方镜像仍需完整回归 |
| 其它服务 | Google 登录、小米服务、相机、音频与蓝牙尚未完整验证 |

启动器维护 Flutter 渲染、桌面导航、天气 EGL 桥接三个 KernelSU 模块；天气从预装目录加载时，直接验证并使用镜像内的兼容库。它们保留原 APK 签名，校验完整 SHA-256 后处理原生库，重启后仍生效。未知更新不会套用现有二进制偏移。当前桌面与天气补丁仅覆盖已核验版本，不能保证其它版本或后续更新适用。

新版预装镜像通常只显示 Flutter 和 Quickstep 两个模块。天气 ANGLE 桥接已固化到 `/product/app/MIUIWeather/lib/arm64`，不再需要独立模块；当天气改为加载 `/data/app` 中的更新包时，才可能使用天气运行时模块。模块数减少与桌面手势无关。

### 应用商店更新桌面后

已验证商店桌面 `RELEASE-8.01.02.7722-260904-09221533-R`（versionCode `801027722`）。它自带 Flutter v5 引擎，更新后不再使用镜像内的共享引擎，因此旧模块尚未刷新时会再次缺少文字和控件。启动器每次冷启动都会重新核对安装路径；在 AVD 已运行时，可直接刷新渲染与导航补丁：

```sh
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-official" python3 scripts/apply_flutter_fix.py
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-official" python3 scripts/apply_navigation_fix.py
```

脚本仅覆盖已核验的原生库并按需重启桌面进程，保留更新包签名和应用数据。2026-10-03 已确认该版本的“最近任务样式”标题、堆叠排列、开关和设置卡片恢复显示；新安装路径及库覆盖已保存到 KernelSU 模块，后续重启仍会应用。未知引擎会报 `Unsupported Flutter engine SHA-256` 并停止，需另行适配。

本次 OOBE 黑屏发生在 `com.android.provision/.activities.DefaultActivity`。其主线程在 `FindDeviceState.getLastSessionUserId()` 同步查询 `content://com.xiaomi.finddevice.provider/lastSessionUserId`；查找设备进程持有状态管理锁，持续等待 AVD 缺少的小米 MTD 存储服务，导致引导 ANR、下一页无窗口。官方 MTD 依赖高通 QSEE、RPMB 等手机硬件，无法直接补入 ranchu Vendor。系统拒绝整包禁用，因此仅禁用上述状态查询组件；引导应用保持启用，完成标记由正常引导流程写入。

2026-10-03 固化后的冷启动已核验：移除用户级禁用设置、恢复组件默认状态后，查找设备状态 Provider 仍不可解析，确认镜像配置独立生效；SettingsProvider 默认资源为最长超时与常亮，运行状态为 `mStayOn=true`；Quickstep 后台组件为 `com.miui.home/com.miui.home.recents.RecentsActivity`。用户数据与 OOBE 完成状态保持不变，桌面手势已由用户验收。

桌面导航还修正 ranchu 温度节点的 SELinux 标签，避免电量管理反复崩溃；将等待动画目标的期限由 800 毫秒延长至已有的 5 秒，并将提前移除应用页面的 watchdog 从 500 毫秒延长至 5 秒，让回桌面动画能接上窗口回调。两处均保留有限超时保护。天气的专属 EGL 桥接保留模糊、玻璃与动态天空，不修改其它应用的图形驱动。

Flutter 模块 revision 6 为系统 v3 引擎增加三条阴影兼容指令：将扩散阴影矩形改为按周边顺序拼接的 triangle fan，保留原阴影着色器、透明度、模糊和玻璃。该系统库由 Rust 启动器预加载，升级后需重启此 AVD；仅重开相册可能仍在使用旧引擎。

需要禁用运行时覆盖时，将下列命令中的脚本名换为 `apply_flutter_fix.py`、`apply_navigation_fix.py` 或 `apply_weather_fix.py`。导航模块禁用后需冷启动；使用 `--enable` 可重新启用。禁用模块不会撤销已经固化到系统镜像的修复；撤销预装或镜像内兼容库需在关闭官方 OS4 AVD 后恢复备份镜像。

```sh
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-official" python3 scripts/apply_weather_fix.py --disable
```

OS4 Release 使用 format 2 manifest，包含独立 AVD 模板和公开构建信息；旧 OS3 format 1 包仍可安装。运行 `python3 scripts/package_release.py --variant os4-official --version v0.2.0-a17-hyperos4-hongkong-r1` 生成分卷。打包器核验镜像内的修复、预装应用和默认配置，重新创建空白 userdata，不读取现有 `avd/`。构建结果、日志和个人数据均被 Git 忽略。
