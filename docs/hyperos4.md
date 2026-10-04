# 官方 HyperOS 4 适配

将小米 18 Pro（`hongkong`）的 **OS4.0.17.0.XFRCNXM / Android 17（API 37）** 官方 OTA 转换为 AVD 镜像。硬件层使用 API 36 ARM64 revision 7 的 ranchu Vendor、4 KB 内核及 KernelSU 3.3.0。

## 构建与启动

使用发布的预构建镜像时，只需 Emulator、Platform-Tools 与 Python 3。v0.2.1 推荐双击 `Install.command` 自动下载、安装或升级；旧手动入口仍为 `./Setup.command --bundle /path/to/manifest.json` 和 `./Start-HyperOS4-Official.command`，使用 `work/os4-official/`。无需手机 OTA、NDK 或 Build-Tools。下述依赖仅用于重建镜像。

需要原始官方 OTA ZIP、下表中的原始签名 APK、Android Studio SDK、Python 3、JDK 17+，以及 `brew install erofs-utils e2fsprogs lz4`。天气兼容库还需 Android NDK 30。首次提取需要 `tools/payload-dumper-go`，建议预留至少 80 GiB 空间。

```sh
python3 scripts/build_os4_official.py --zip /path/to/hongkong-ota_full-OS4.0.17.0.XFRCNXM-user-17.0-9cf2afb0fc.zip
./Start-HyperOS4-Official.command
```

源分区默认保存到 `work/os4-official/input/hongkong-4.0.17/`，也可用 `--partitions` 指定已提取的 `system / system_ext / product / mi_ext / mi_product`。构建器将这些分区合并为 4 KB EROFS，保留文件权限、所有者、SELinux 与 capability 标签，并合入小米覆盖层和 OS4 软件版本属性。

OS4 默认分配 **6 GiB 内存**（`hw.ramSize=6144`）。原版负一屏会根据实际内存选择模式：≤4 GiB 时进入精简模式，将“添加小组件”替换为设置按钮；6 GiB 保留“＋”入口，但背景模糊按中档设备降级。需要完整背景模糊时，可在 `work/os4-official/config/avd.ini` 中设置 `hw.ramSize=8192`，关闭该 AVD 后通过项目启动器冷启动；8 GiB 已确认恢复 `BLUR_VIEW`、顶部玻璃和小组件选择页，保留应用原签名与用户卡片，此项无需修改 Flutter 引擎。

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

AVD 为 `HyperOS_4_Official_API_37`，ADB 序列号为 `emulator-5574`，默认 4 核、6 GiB 内存；镜像和用户数据均位于 `work/os4-official/`，与 OS3 独立。重建前需关闭此 AVD。

音频输出默认开启，声音通过 macOS 当前输出设备播放。启动器不再传入 `-no-audio`；AVD 模板使用 `hw.audioOutput=yes`，修改后需冷启动才能启用虚拟声卡。

持续播放后无声时，已抓到 ranchu HAL 的 `pcm_writei` 持续 `EIO`：原版 tinyalsa 只恢复欠载和挂起错误，HAL 随后反复丢弃音频。`patch_audio.py` 给已核验的播放入口加入有限恢复：重置同一 PCM 流并重试一次，持续故障仍返回实际错误；保留录音路径、库 ABI、其它错误和 `PCM_NORESTART` 行为。补丁固化到 Vendor，发布检查同时核验实际库与音频输出配置。13 项 AVD 原生故障测试通过；冷启动连续播放时，已捕获真实 `EIO` 并自动恢复写入 1088 帧，音频服务未重启。v0.2.1 发布候选已包含该改动。

主屏采用官方 Product 分区 `display_id_4630947121878579347.xml` 的 **1120×2436、480 dpi**；480 是 Android 逻辑密度，不是面板 PPI。模拟器的 `emu64a.xml` 完整复用原厂 `hongkong.xml`（含 `support_aod_fullscreen=true`），两者逐字节一致，原厂配置存放于 `config/hongkong.xml`。息屏显示默认开启、始终显示；镜像内的一次性启动服务写入初始设置，后续用户修改不会在每次启动时被覆盖。

保留原厂 `ro.debuggable=0` 并撤掉移植时加入的 console 启动触发器，避免串行控制台通知，以及开发者选项尝试写入 user 构建禁止的 `logd.logpersistd` 属性。ADB 仍需授权；KernelSU 的 shell root 使用独立授权机制。

显示默认使用自然模式（`display_color_mode=0`）与 `persist.sys.sf.color_saturation=1.0`，取消原有 1.1 的全屏增艳。仅修改饱和度属性会被色彩服务在下一次启动时恢复，必须同时保存模式设置。首次启动服务和启动器仅写入一次，保留后续手动调整；已有 AVD 可运行 `python3 scripts/os4_defaults.py --color-only` 立即应用。

macOS 模拟器窗口与 Metal 显示层另需标记为 sRGB，否则截图正常时，窗口仍可能偏艳。OS4 启动脚本加载专属色彩标记库，保留 GPU 加速与原始截图颜色，不修改 SDK 或 macOS 显示器设置；用户已确认画面接近参考截图。Metal 层未标记时默认不进行色彩匹配，见 [Apple 色彩空间说明](https://developer.apple.com/documentation/quartzcore/cametallayer/colorspace)。v0.2.1 发布候选包含预编译库，源码构建需要 Xcode Command Line Tools。直接点 Android Studio 的 Start 不会加载此库；使用项目启动脚本，或以 `--no-host-color-fix` 临时关闭。

刷新率修复采用 `config/lock_fps.sh` 与 `config/lock_refresh_rate.rc`：除关闭智能刷新率、设置 60 Hz 上下限，还将 SurfaceFlinger 的实际渲染帧率固定为 60 Hz。此前面板显示 60 Hz，但合成器实际按 20 Hz 渲染；用户已确认修复后明显流畅。脚本仅匹配本项目 OS4 AVD 的 60 Hz 模式 0；当前用户数据通过 KernelSU 启动服务保持，新构建通过 init 服务加载。已有 AVD 可运行 `python3 scripts/os4_defaults.py --refresh-only`。

渐进式模糊使用 `persist.sys.gradient_blur_perf=false` 选择原版 HWUI 的通用 Skia 路径。默认优化路径 `MiGradientBlurEffect` 在 AVD 上丢失渐变；控制中心上滑越过顶栏、主题商店内容滚入搜索栏时均已对照复现。保留原版 `libhwui.so`，无需修改应用或提高内存。已有 AVD 可运行 `python3 scripts/os4_defaults.py --blur-only` 后冷启动；新构建在 zygote 启动前设置该属性。

距离与光线传感器已启用。项目启动器将初值设为 `5 cm / 200 lux`，避免模拟器的 `1 cm / 0 lux` 默认读数触发小米相机防误触。之后可在模拟器扩展控制或 `adb -s emulator-5574 emu sensor set proximity 0` 中模拟遮挡；直接点 Android Studio Start 尚未包含这一步初始化。

`config/hongkong-identity.json` 保存官方 OTA 的公开身份信息与来源校验和：设备 `hongkong`、型号 `M610BB`、市场名 `Xiaomi 18 Pro`，包括原厂各分区身份和构建指纹。新构建写入系统属性；现有 AVD 的 Quickstep 10 模块在 zygote 启动前加载相同信息，避免应用继续缓存 Pixel 型号。`ro.hardware`、EGL、Vulkan 与 boot hardware 保留 ranchu 驱动所需值；这些身份属性不会提供真机的高通 ISP、传感器或相机 HAL。

Quickstep 9 为每份用户数据生成独立的随机模拟序列号，外观采用用户实机样例的“5 位数字 / 1 位大写字母 + 3 位日期码 + 5 位数字”，同时设置 Android 序列号与小米设置读取的 `ro.ril.oem.psno`。前缀、字母与尾号随机，日期码使用首次生成日期；这些字段仅用于模拟外观，不代表官方 SKU 或工厂。编号保存在模块清单中，重启和模块更新时保持不变；旧版十六进制编号只迁移一次，新建用户数据生成新编号，不在发布包中写入共用号码。

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
| 负一屏 | 默认 6 GiB，保留“＋”入口，背景模糊按中档设备降级；8 GiB 完整效果已冷启动验证 |
| 首次开机引导 | 空白用户数据已复测；查找设备状态查询禁用后，用户已完成引导并进入系统；兼容处理纳入镜像默认配置 |
| 默认常亮 | 系统默认资源与启动器均配置常亮；启动时接通虚拟 AC 电源，当前设备已确认 `mStayOn=true` |
| 已知问题 | 部分后台缩略图仍为白色；其它桌面设置未完成回归；切换镜像首次启动曾出现一次 `goldfish_sync` 内核崩溃，重启模拟器进程后恢复；首次着色器初始化可能较慢 |
| GPS | 已合入 Android 17 GNSS 回调补丁；官方镜像仍需完整回归 |
| 其它服务 | Google 登录、小米云服务与蓝牙尚未完整验证；相机为实验适配，音频输出及故障恢复已验证 |

启动器维护 Flutter 渲染、桌面导航、天气 EGL 桥接三个 KernelSU 模块；天气从预装目录加载时，直接验证并使用镜像内的兼容库。它们保留原 APK 签名，校验完整 SHA-256 后处理原生库，重启后仍生效。未知更新不会套用现有二进制偏移。当前桌面与天气补丁仅覆盖已核验版本，不能保证其它版本或后续更新适用。

新版预装镜像通常只显示 Flutter 和 Quickstep 两个模块。天气 ANGLE 桥接已固化到 `/product/app/MIUIWeather/lib/arm64`，不再需要独立模块；当天气改为加载 `/data/app` 中的更新包时，才可能使用天气运行时模块。模块数减少与桌面手势无关。

### 相机实验适配

本地调试 AVD 默认后摄 `virtualscene`、前摄 `emulated`，分别提供 ID `0` 与 `1`。后摄显示可编辑的三维房间，前摄显示模拟测试画面。通过模拟器侧栏 `… → Camera → Virtual scene images → Add Image` 可替换房间墙面和桌面图片。重建的 OS4 模板也使用这组配置，v0.2.1 发布候选已包含该改动。

虚拟场景 HAL 的兼容补丁已纳入镜像构建：后摄编号从 `10` 改为 `0`，补充小米角色与 YUV 元数据，并声明可渲染的 `1024×768` 预览尺寸，避免系统放大延迟创建的 Surface 后拒绝连接。后摄预览、前后摄切换、基础拍照与录像页预览已检查；录像质量仍遵循下述限制。

小米相机 `6.8.001960.0` 的实验适配已实现前后摄预览和基础拍照：将已核验 HAL 的厂商拍照模式 `0x9005`、录像模式 `0x8004` 映射为普通流模式，补充小米前后摄角色与 YUV 格式声明；应用专属 JNI 桥接把模拟器的 I420 平面转换为可供原相机 NV21 编码的布局。相机仍使用原 APK 和签名，ANGLE 仅对该包开启。已保存并解码 JPEG，连续两次拍照后可再次操作。

模拟器没有小米 ISP 服务，因此照片使用相机原有的 early JPEG 恢复流程，分辨率与算法能力受限。兼容库只在对应任务的 JPEG 已写完、没有打开的写入句柄后提前触发原恢复回调，避免等待原 40 秒超时。夜景、人像、变焦与真机算法尚未完整验证，不能标为完整相机支持。

revision 2 修复切换录像时的 HAL `BAD_VALUE` / “无法连接至相机”。前后摄均已进入录像并保存可解码的 720p MP4；当前相机设置已选择 H.264。前摄一次录制保存 212 帧、7.4 秒，约 28.6 fps。后摄 H.264 与 HEVC 仍有严重掉帧、音画时长不一致和尾帧缺失，录像质量尚未修好；此项不列为完整支持。编码选项位于“相机设置 → 录像 → 视频编码”。

安装到已有官方 OS4 AVD（v0.2.1 预编译包无需 NDK；源码构建需要 NDK API 36+）：

```sh
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-official" python3 scripts/apply_xiaomi_camera_fix.py
# Upgrade an existing revision 1 module without removing app data.
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-official" python3 scripts/apply_xiaomi_camera_fix.py --rebuild
```

该安装器校验完整 APK、Google HAL、libc++ 和 Android runtime 校验和，建立 `HyperOS AVD Xiaomi camera bridge` 模块。通过文件绑定保留系统文件与用户数据，重启后恢复；可在 KernelSU 中关闭该模块并重启以撤回。未知 APK 更新不会套用这些偏移。相机模块为可选实验项；v0.2.1 附带经过校验的预编译桥接，安装器可在启动时启用，无需 NDK。

谷歌相机 Parrot `8.2.300.368894857.16` 可安装应用专属兼容库（需要 SDK 内的 Android NDK，API 36+）：

```sh
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-official" python3 scripts/apply_camera_fix.py
# Remove only this project's library; preserve the APK and app data.
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-official" python3 scripts/apply_camera_fix.py --remove
```

脚本核验相机 APK、原生库及 Android runtime 的 SHA-256，使用 CPU 共享内存替代缺失的 AION 分配接口，并在该应用进程内补齐外部纹理占用单元的查询。Gfxstream 对 `GL_REQUIRED_TEXTURE_IMAGE_UNITS_OES` 的实现返回 `1`，但前置参数校验遗漏此枚举，造成切换动画 GL 线程退出；兼容层采用相同返回值，保留其它查询和错误。[Gfxstream 查询实现](https://github.com/google/gfxstream/blob/main/guest/GLESv2_enc/GL2Encoder.cpp)、[参数校验](https://github.com/google/gfxstream/blob/main/guest/GLESv2_enc/GLESv2Validation.cpp)。库保留进程生命周期引用，避免切换摄像头时卸载后留下失效回调。

已验证预览和连续两轮 `0 → 1 → 0` 切换，进程保持不变；独立 Camera2 测试可通过两颗摄像头保存 JPEG。Parrot 拍照尚未完整适配：HDR 自动模式曾保存明显噪点，关闭 HDR 时编码器要求色度像素步长为 `2`，而 AVD 提供 `1`。GPU 美颜与其它拍照模式未验证。兼容库不作为全局驱动或 KernelSU 模块安装；应用更新后需重新核验，未知版本会拒绝修改。

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

Quickstep revision 10 撤回此前的本地动画绕过，恢复 `persist.miui.home_sf_anim=true` 的原版 SF 动画。合成器从 20 Hz 修正为 60 Hz 后，用户已确认启动应用、回桌面不再反复缩放或跳动；旧镜像与模块升级时也恢复这一属性。

活动返回时的压暗不渐隐来自 ranchu 硬件合成器：它将图层 alpha 与亮度取平均，默认亮度为 1 时，alpha 降到 0 仍显示约 50% 黑色叠层。`patch_composer.py` 仅修正已核验 ELF 中的一条 alpha 指令，固化到 Vendor，保留硬件合成、60 Hz 和原版动画。Android 内部录屏走另一条合成路径，不能复现此问题；验证使用 macOS 模拟器窗口录制。构建器保留 Vendor 文件的权限、所有者与 SELinux 标签，Release 检查实际打包的 Vendor 与修补库校验和。

2026-10-04 保留用户数据冷启动后，已确认运行中的合成器来自修补镜像；设置二级页通过返回键、顶部按钮与侧滑返回时，模拟器窗口均连续渐隐至无压暗。合成器仍为 DEVICE / SOLID_COLOR 硬件合成、60 Hz，SELinux 为 Enforcing，原版桌面动画保持启用。

小爱 `8.2.61.3516` 的长按电源键光效使用独立 MGL2 引擎。AVD 实际 GL 驱动只支持 GLSL 3.00，而原引擎强制添加 3.20 头部；初始 EGL 配置还可能缺少透明通道。兼容补丁仅调整这两处已核验原生位点，保留原着色器、白色边缘光效、渐隐与背景透明度。原 APK、签名和应用数据保持不变。构建脚本会加入独立原生库；旧镜像通过 `apply_assistant_fix.py` 安装可在 KernelSU 中禁用并重启撤回的模块。未知小爱更新会拒绝修改，v0.2.1 发布候选已包含该改动。

2026-10-04 已在保留用户数据的冷启动后核验：小爱加载独立修补库，原 APK SHA-256 不变，录屏中白色边缘光效亮起并渐隐、桌面保持可见，未再出现着色器版本错误或整屏黑底；合成器仍为 60 Hz，SELinux 保持 Enforcing。

Flutter 模块 revision 6 为系统 v3 引擎增加三条阴影兼容指令：将扩散阴影矩形改为按周边顺序拼接的 triangle fan，保留原阴影着色器、透明度、模糊和玻璃。该系统库由 Rust 启动器预加载，升级后需重启此 AVD；仅重开相册可能仍在使用旧引擎。

需要禁用运行时覆盖时，将下列命令中的脚本名换为 `apply_flutter_fix.py`、`apply_navigation_fix.py` 或 `apply_weather_fix.py`。导航模块禁用后需冷启动；使用 `--enable` 可重新启用。禁用模块不会撤销已经固化到系统镜像的修复；撤销预装或镜像内兼容库需在关闭官方 OS4 AVD 后恢复备份镜像。

```sh
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-official" python3 scripts/apply_weather_fix.py --disable
```

OS4 首版使用 format 2 manifest，包含独立 AVD 模板和公开构建信息；旧 OS3 format 1 包仍可安装。v0.2.1 使用 format 3；新安装器与保数据升级见 [安装文档](installing.md)，打包见 [发布文档](releasing.md)。打包器核验镜像内的修复、预装应用和默认配置，重新创建空白 userdata，不读取现有 `avd/`。构建结果、日志和个人数据均被 Git 忽略。
