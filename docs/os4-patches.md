# OS4 补丁维护与交付边界

补丁按实际二进制内容和硬件能力选择，AVD 名称、ADB 端口、工作区路径不作为版本身份。通用模块可以识别多个 OS4 基系统中的相同原生库；**未知的未来 ELF、APK、framework 或内核仍需审计，不能直接套用偏移或声明全部兼容**。

| 交付层 | 可以自动完成 | 必须保留的边界 |
| --- | --- | --- |
| 原生 KSU catalog | 已审计 ELF 的等长编辑、已知旧输出迁移、动态应用路径和挂载。 | 内容 hash/site 不匹配即跳过，不能自动推断未来 ABI。 |
| 现有 KSU 编译桥接 | 已核验的相机、天气、背屏等私有 helper 与受控启动脚本。 | 预编译库仍需 source/payload 收据及所有宿主库 ABI 校验，不能只包一层通用 wrapper 就解除版本限制。 |
| 镜像 / root 前启动 / 内核 | 可启动基线、首次扫描配置、framework/SELinux/内核及有布局改动的库。 | 新系统进入可用 root 前就需要的修复，和恢复出厂后的首次启动，仍由镜像负责。 |
| macOS / Emulator | 色彩标记、启动参数、窗口、传感器、音频与离线存储管理。 | Guest 模块无法改变宿主窗口、SDK 后端或未启动的磁盘文件。 |

## 现有补丁清单

| 补丁族 | 作用与维护入口 | 当前交付边界 |
| --- | --- | --- |
| Flutter | 深度范围、Float16、缓冲区对齐、阴影；已审计共享引擎的大字号字形图集阈值。`patch_flutter.py` | 原生 KSU catalog；系统共享引擎早期加载，桌面/天气私有引擎按当前安装路径发现；相册通过共享引擎受益。 |
| HWUI / Vulkan | 跳过 fork 前 GPU 驱动初始化，保留小米 CPU shader preload；配合 `skiavk` 与 RenderEngine 配置。`patch_pad_hwui.py` | 原生 KSU catalog + 已核验镜像/属性配置；不能在未知 HWUI 上只改 renderer 属性。 |
| 桌面导航 | Recents AOT 等待时限、route watchdog；`ro.miui.product.home=com.miui.home` 避免 Launcher3 误识别。`apply_navigation_fix.py` | AOT 原生编辑进入 catalog；桌面身份、序列号与原模块生命周期仍单独管理。已撤销的缩放动画规避不应重新加入。 |
| 渐进式模糊 | `persist.sys.gradient_blur_perf=false` 选用原有通用 Skia 路径。`os4_defaults.py` | 已核验 HWUI 的早期属性修复；属于系统能力配置，不是应用 UI 替换。 |
| 音频恢复 | tinyalsa 播放 `EIO` 有限重置并重试，保留其他错误/录音路径。`patch_audio.py` | 当前 Vendor 镜像；含恢复机器码及布局约束，不能作为任意库的等长编辑。 |
| 合成器 | plane alpha 与亮度分离，活动返回压暗正常渐隐；背屏独立 timing 改为 60 Hz。`patch_composer.py`、`patch_rear_display.py` | 原生 KSU catalog；两步 hash 链保留 alpha 修复，已启动的 HAL 需冷启动重新加载。 |
| 虚拟场景相机 | rear ID、Xiaomi role、YUV 元数据及预览尺寸。`patch_camera_scene.py` | 镜像内 provider + 编译后的 scene helper；需校验 provider、C++ ABI 和依赖布局。 |
| 手机 / Pad 相机 | 私有流模式映射、front role、I420→NV21；手机 early JPEG 恢复，Pad 使用独立 ImageReader ABI。`apply_xiaomi_camera_fix.py`、`apply_pad_camera_native_fix.py` | 现有专用 KSU 编译桥接；APK、JNI、framework、HAL、libc++ 联合校验，手机偏移不能复用于 Pad。基础功能仍不等于真机 ISP/录像算法完整支持。 |
| 相机 ANGLE / Parrot | Pad 相机专属 ANGLE 设置；Parrot CPU AION 分配与纹理查询。`apply_pad_camera_fix.py`、`apply_camera_fix.py` | ANGLE 设置保留其他包选项；Parrot 目前是包内专用库安装器，未自动转入 catalog，未知 APK/ABI 拒绝。 |
| 天气 MGL / ANGLE | 私有 Vulkan EGL display、MGL 依赖重定向及固定 ANGLE workload。`patch_weather.py`、`apply_weather_fix.py` | 镜像或现有专用 KSU 编译桥接；Weather 原生绑定必须避开首次 PackageManager 提取阶段，不改写/重签原 APK。 |
| 小爱边缘光效 | MGL GLSL 版本与 8-bit EGL alpha，保留背景及原动画。`patch_assistant.py` | 原生 KSU catalog；已有专用模块保留，APK 来源校验与安装路径发现继续有效。 |
| 锁屏视频预览 | fastplayer 使用 packed RGBA 与正确 stride，绕过异常 YV12 导入。`patch_lockscreen_video.py` | 原生 KSU catalog / 镜像外置库；未知播放器不套用 offset，APK 保留原字节。 |
| GNSS framework | 将回调延迟到释放 HAL monitor 后，避免同步回调死锁。`patch_gnss.py` | 已核验 `services.jar` 的镜像构建；Java/DEX 补丁不进入 ELF catalog。 |
| 硬件服务与内核 | 缺失 Qualcomm HAL/thermal/provider 的 guard、Millet/iorapd/GNSS extension 能力 probe、窄 SELinux 权限。`patch_boot_services.py`、`check_kernel_services.sh`、`patch_goldfish_sync.py` | 镜像内 Java/服务/probe/SELinux 基线与 goldfish_sync 内核修复；现有 scoped module 可做诊断/已核验修复，不能替代未知内核或早于 root 的启动修复。 |
| FindDevice / OOBE | 禁用阻塞的状态 Provider，保留查找设备 APK 与正常引导。`os4_defaults.py`、`os4_pad.py` | 镜像 sysconfig/受控组件状态；必须覆盖首次 PM 扫描和引导，不能依赖用户进系统后手动运行。 |
| 机型与默认值 | 原机 XML、公开身份、稳定随机序列号、按机型启用 AOD、一次性常亮/自然色彩、thermal label、指定日志与 60 Hz。`config/`、`os4_defaults.py`、`os4_pad.py` | 镜像基线 + 受控启动脚本；设备能力与用户选项分开。Pad 不启用原机未支持的 AOD；旧初始化标记/序列号应迁移，后续用户选择应保留。 |
| 背屏 | 物理显示 ID alias、最小 RRO、原几何/cutout/corner、双击唤醒 Java bridge。`rear_display_config.py`、`rear_display_wake.py` | 机型资源及 bridge 可由镜像/专用 KSU 提供；物理屏创建和独立窗口仍需 Emulator 启动参数。 |
| macOS / Emulator | sRGB window/Metal 标记、传感器/虚拟 AC 初值、音频输出、Pad 窗口旋转、副屏窗口、用户分区扩容。`host_color.py`、`launch.py`、`manage.py` | 宿主层；KSU 不能替 macOS 标记颜色、创建窗口/声卡或扩容离线 QCOW2/ext4。扩容须同时检查虚拟磁盘与 `/data` 文件系统。 |

## 通用原生 KSU 模块

`modules/native-compat/` 提供标准 KernelSU 模块 `hyperos_avd_native_compat`。`native_patch_catalog.py` 从各补丁入口的现有常量生成唯一 catalog；`package_native_module.py` 打包脚本、hash 与等长字节编辑，不打包小米 ELF/APK，也不需要在 guest 编译。

当前 catalog 覆盖 **Flutter、HWUI、小爱、锁屏视频、导航 AOT、composer alpha、背屏 timing**。相机/天气编译桥接、Java、内核与宿主补丁继续使用表中入口；“统一交付”不表示所有补丁都能变成同一种运行时字节编辑。

```sh
# 可在 KernelSU 中手动安装；打包过程不连接或修改 AVD。
python3 scripts/package_native_module.py --output /path/to/native-compat.zip

# 使用当前工作区注册的实例安装；不会自动重启。
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-official" python3 scripts/apply_native_compat.py
HYPEROS_AVD_WORKSPACE="$PWD/work/os4-official" python3 scripts/apply_native_compat.py --status
```

模块通过 KernelSU 正常安装生命周期激活。early 阶段处理系统目标；late 阶段从包管理器解析当前原生库路径，每 30 秒检查包数据库变化，稳定后重新核验；无变化时不反复提取 APK。完整输入/历史输出 hash、每个原字节、完整输出 hash 均需匹配；未知内容仅跳过对应特性并记录状态，不能中断系统开机或回退为猜测 offset。已有八类专用模块及其关闭选择保留，不自动删除。

状态位于 `/data/adb/modules/hyperos_avd_native_compat/state/status.tsv`，每行是 `feature|target|state|reason`；运行日志为同目录的 `runtime.log`。

| 状态 / 原因示例 | 含义 |
| --- | --- |
| `ready / verified-bind`、`ready / already-patched` | 文件挂载或已有输出已核验，不代表旧进程已重新加载。 |
| `skipped / unsupported-elf` | 内容未在 catalog 审计，保留原文件；缺包、无私有库、更新尚未稳定也会记录跳过原因。 |
| `failed / patch-verification`、`failed / namespace-mount` | 字节/输出或挂载检查未通过，检查日志与原始文件，不通过自动放宽校验修复。 |
| `pending / reboot-required` | 早期目标尚未加载新修复，需正常重启。 |
| `disabled`、`not-activated` | 用户关闭对应功能/模块，或尚未产生运行状态。 |

安装完成、挂载完成、当前进程加载新代码是不同状态：系统库可能已经由 zygote、system_server 或 HAL 缓存；**模块不会自动重启 zygote、系统服务或 AVD**。首次安装/更新早期补丁后需正常重启一次；late 新挂载会结束对应应用的旧进程，重新打开应用即可，zygote 预加载代码仍需冷启动。KernelSU 中关闭模块并重启可撤回其挂载，镜像内已烘焙的基线修复仍保留。

恢复出厂会清除 `/data/adb`，因此纯 userdata 模块不保证重置后的第一次开机。现有可启动的镜像、必要的内核/SELinux/首次扫描修复仍必须保留。启动器会在基线启动后安装通用模块，可能还需一次重启；新的镜像内 KSU 自举尚未经过恢复出厂验证，不将它写成已完成能力。

本轮已在手机 `4.0.18.0.XFRCNXM` 上验证标准 KSU 安装、待重启复用和正常重启；临时关闭旧 Flutter 模块后，新模块独立生成并让桌面实际加载了修复库。Android ID、序列号、引导状态、应用列表及抽查的用户设置保持一致，Vulkan 和 SELinux Enforcing 保留，原模块启用选择已恢复。Android 17 的 `legacyNativeLibraryDir`、停用出厂包与空 ZIP 原生条目也有回归覆盖；当前 Rust 桌面没有某些可选私有库，使用共享引擎并安全跳过这些目标。Pad 本轮未做实际冷启动验收，未知未来版本仍需上述内容和 ABI 审计。

连续 Android 软重启还触发过一次宿主崩溃，堆栈位于 SDK 的 macOS OpenGL/gfxstream 合成路径；正常冷启动后已恢复，用户数据保留。该连续重启问题尚未定位根因或增加修复，不将 guest 模块核验扩大为宿主稳定性保证。

## 新增兼容 profile

1. 取得并审计目标原始 ELF：架构、可执行段、函数/ABI、完整 SHA-256、每个原字节及预期输出；保留变更前后真实复现证据。
2. 在对应 `patch_*.py`（导航为 `apply_navigation_fix.py`）增加一个已核验 profile，供镜像与 catalog 共用；不要另写一套模块 offset 或用 OTA 名称代替内容身份。需要新目标/新特性时再更新 `native_patch_catalog.py` 的白名单和生命周期。
3. 添加原始→输出、幂等、旧输出迁移、错误 hash/site 拒绝测试；重新打包，检查包/路径变更及重命名 AVD。编译桥接另外检查 JNI/C++/framework/HAL ABI，不能因源文件名相同就复用。
4. 验证 cold boot、实际加载库、用户数据/选择保留和对应功能；一次静态测试不能代替音频、动画、拍照或 UI 验收。仅发布实际验证过的组合，并保留未知特性跳过原因。

详细适配背景见 [手机](hyperos4.md)、[Pad](hyperos4-pad.md)；发布验证记录见 `docs/releases/`。这些记录描述具体版本的证据，不扩大为所有 OS4 的保证。
