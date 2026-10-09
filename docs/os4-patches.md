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
| 相机 ANGLE / Parrot | Pad 相机专属 ANGLE 设置；Parrot CPU AION 分配与纹理查询。`apply_pad_camera_fix.py`、`apply_camera_fix.py` | ANGLE 设置保留其他包选项；Parrot 使用可选 KSU 私有桥接模块，核验完整 APK 与 Android runtime ABI，沿用既有安装选择。未知 APK/ABI 保留原文件。 |
| 天气 MGL / ANGLE | 私有 Vulkan EGL display、MGL 依赖重定向及固定 ANGLE workload。`patch_weather.py`、`apply_weather_fix.py` | 镜像基线 + KSU 私有桥接模块；从 PM 重新发现路径，核验已知 APK/依赖后绑定。避开首次提取阶段，不改写/重签原 APK。 |
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

模块通过 KernelSU 正常安装生命周期激活。early 阶段处理系统目标；late 阶段从包管理器解析当前原生库路径，每 30 秒检查包数据库变化，稳定后重新核验；无变化时不反复提取 APK。服务使用内核 `flock` 租约，崩溃后可自动恢复模块自己的空锁，同时保留仍存活的旧服务；运行状态只允许 root 访问。完整输入/历史输出 hash、每个原字节、完整输出 hash 均需匹配；未知内容仅跳过对应特性并记录状态，不能中断系统开机或回退为猜测 offset。已有八类专用模块及其关闭选择保留，不自动删除。

状态位于 `/data/adb/modules/hyperos_avd_native_compat/state/status.tsv`，每行是 `feature|target|state|reason`；运行日志位于 `/data/adb/modules/hyperos_avd_native_compat/runtime.log`。

| 状态 / 原因示例 | 含义 |
| --- | --- |
| `ready / verified-bind`、`ready / already-patched` | 文件挂载或已有输出已核验，不代表旧进程已重新加载。 |
| `skipped / unsupported-elf` | 内容未在 catalog 审计，保留原文件；缺包、无私有库、更新尚未稳定也会记录跳过原因。 |
| `failed / patch-verification`、`failed / namespace-mount` | 字节/输出或挂载检查未通过，检查日志与原始文件，不通过自动放宽校验修复。 |
| `pending / reboot-required` | 早期目标尚未加载新修复，需正常重启。 |
| `disabled`、`not-activated` | 用户关闭对应功能/模块，或尚未产生运行状态。 |

安装完成、挂载完成、当前进程加载新代码是不同状态：系统库可能已经由 zygote、system_server 或 HAL 缓存；**模块不会自动重启 zygote、系统服务或 AVD**。新暂存的早期补丁需正常重启激活；已有内容不同的 pending 包时，先保留并完成其激活，下一次启动器再暂存当前包，因此可能还需第二次正常重启。late 新挂载会结束对应应用的旧进程，重新打开应用即可，zygote 预加载代码仍需冷启动。KernelSU 中关闭模块并重启可撤回其挂载，镜像内已烘焙的基线修复仍保留。

恢复出厂会清除 `/data/adb`，因此纯 userdata 模块不保证重置后的第一次开机。现有可启动的镜像、必要的内核/SELinux/首次扫描修复仍必须保留。启动器会在基线启动后安装通用模块，可能还需一次重启；新的镜像内 KSU 自举尚未经过恢复出厂验证，不将它写成已完成能力。

本轮已在手机 `4.0.18.0.XFRCNXM` 上验证标准 KSU 安装、待重启复用和正常重启；临时关闭旧 Flutter 模块后，新模块独立生成并让桌面实际加载了修复库。保留宿主崩溃留下的空锁后再次冷启动，也确认服务自动恢复并持续持有租约。Android ID、序列号、引导状态、应用列表及抽查的用户设置保持一致，Vulkan 和 SELinux Enforcing 保留，原模块启用选择已恢复。Android 17 的 `legacyNativeLibraryDir`、停用出厂包与空 ZIP 原生条目也有回归覆盖；当前 Rust 桌面没有某些可选私有库，使用共享引擎并安全跳过这些目标。Pad cold-boot validation and the revision 2 compatibility fixes are recorded below. Unknown future binaries still require content and ABI review.

连续 Android 软重启还触发过一次宿主崩溃，堆栈位于 SDK 的 macOS OpenGL/gfxstream 合成路径；正常冷启动后已恢复，用户数据保留。该连续重启问题尚未定位根因或增加修复，不将 guest 模块核验扩大为宿主稳定性保证。

## Feedback verification — 2026-10-10

The feedback predates nine local commits after the Phone r4 / installer 1.2.1 tags (`5aac0a2` → `c5b8552`). Both retained Phone and Pad guests were tested with their existing userdata. This verification does not create a release or certify unknown future binaries.

| Item | Result and delivery |
| --- | --- |
| ART CPU affinity | Actual APK installation and compilation succeeded on both four-vCPU guests. Image init and native module revision 2 share one topology-aware policy; valid user subsets and thread choices are retained. Six-vCPU and non-contiguous topology are covered by fixtures. |
| Unsupported kernel / QTI services | Pinned init definitions are gated before startup using boot-local capability probes. Both final cold boots report capabilities `0`, helper exit `0`, and no iorapd / Millet / QTI display startup attempt or QTI execute denial. No persistent service opt-out is written. |
| Updated OEM packages | FindDevice changes require verified system-update origin, signer and Provider contract. Unknown optional private Flutter / Launcher code is retained and skipped locally, allowing other initialization to complete. The exact reported updated APKs were not available for live verification. |
| Pad native module regression | Revision 1 incorrectly selected Phone rear-display timing on Pad. Revision 2 checks actual display capabilities and preserves file metadata. A pinned HWC executable bind retains its owner and allows the required SELinux transition on that single read-only file. An actual HWC restart verified its HAL domain; normal v2 cold boots pass on both guests. |
| Legacy helper upgrades | Known module helpers and uninstall hooks are migrated atomically without running hooks, changing user lifecycle flags, or writing persistent service opt-outs. Unknown payloads are preserved with diagnostics. |
| Local encrypted storage | A locally built image can supply verified boot inputs for the existing encrypted-capacity check, without pretending to be a downloaded release. Installed manifests retain their strict validation. |
| Pad rendering / stability reports | Weather launch and scrolling, Settings, correctly oriented Recents, and the valid HTMLViewer privacy page were exercised. No matching MoltenVK shader failure, HTMLViewer ANR, or Gallery widget-provider database abort occurred in the final sampled logs. These historical failures are not declared fixed. Gallery's existing consent choice was retained; its provider was observed independently. |

Both final guests retain SELinux Enforcing and HWUI Vulkan. Serial/model/device identity, the package list, and seven sampled user choices remain unchanged. Android's own boot counters and runtime bookkeeping can change. No app data was cleared, no new AVD or OTA download was needed, and no push or release was performed.

The full regression suite passed 720 tests with 56 optional skips before the final conservative init-auditor refinement; 81 affected tests then passed with one optional skip. The final packed Phone / Pad graphs passed 17 / 7 integrity checks across 174 / 155 init entries, including conservative route and duplicate-service auditing. Private receipts and screenshots remain under `work/phone-feedback-audit-20261010/` and `work/pad-feedback-audit-20261010/`; they are not release assets. Intermediate firmware staging and duplicate test backups are removed after validation; the original rollback backups are retained.

## 新增兼容 profile

1. 取得并审计目标原始 ELF：架构、可执行段、函数/ABI、完整 SHA-256、每个原字节及预期输出；保留变更前后真实复现证据。
2. 在对应 `patch_*.py`（导航为 `apply_navigation_fix.py`）保留旧 profile 并增加已核验的新 profile，供镜像与 catalog 共用；不要另写一套模块 offset 或用 OTA 名称代替内容身份。Flutter/HWUI 的 `PROFILES` 自动进入 catalog，其余特性还需扩展 `native_patch_catalog.py` 的 profile 序列并引用同一份原始常量，不能直接替换旧常量。新目标/新特性另需更新白名单和生命周期。
3. 添加原始→输出、幂等、旧输出迁移、错误 hash/site 拒绝测试；重新打包，检查包/路径变更及重命名 AVD。编译桥接另外检查 JNI/C++/framework/HAL ABI，不能因源文件名相同就复用。
4. 验证 cold boot、实际加载库、用户数据/选择保留和对应功能；一次静态测试不能代替音频、动画、拍照或 UI 验收。仅发布实际验证过的组合，并保留未知特性跳过原因。

详细适配背景见 [手机](hyperos4.md)、[Pad](hyperos4-pad.md)；发布验证记录见 `docs/releases/`。这些记录描述具体版本的证据，不扩大为所有 OS4 的保证。

## 全量交付审计 — 2026-10-10

先按功能提交反馈修复，再审计上表中的全部补丁。新改造沿用现有实例，不按 AVD 名称、端口或工作区目录选择二进制 profile。

| 维护入口 | 改造内容 |
| --- | --- |
| `module_lifecycle.py` | 共用 pending / disable / remove 检查，包含悬空别名；已核验旧 hook 原子迁移，保留自定义脚本和用户开关。不会执行迁移中的 hook。 |
| `modules/app-bridge/`、`app_bridge_module.py` | Weather / 可选 Parrot 共用标准 KSU 安装与受控私有库绑定。Producer 提供已核验预编译 helper，最终用户不需要 NDK。已知 APK 重装后由 guest 服务重新发现路径；未知 workload 跳过。 |
| `launch.py`、`patch_outcome.py` | 先完成基础配置及原生模块，再检查可选应用。只有明确且尚未修改文件的“不支持”结果可以局部跳过；完整性、所有权、传输或激活错误仍须报告。 |
| `os4_pad.py` | HWUI 原生库只由统一 catalog 管理。Pad thermal / identity hook 保留职责边界；仅迁移精确已知旧脚本，不改写已加载 payload inode。 |
| `apply_rear_display_fix.py` | 背屏启动使用内核 `flock` 租约，异常退出后自动释放；保留活跃旧服务、外来文件及别名。 |
| `packed_source.py` | 默认配置与预装应用候选从当前已核验完整镜像提取；保留额外分区内容和顺序，发布前重查源及收据，删除中间文件。未知 LP 属性拒绝，已接受 RRO 保留原签名字节。 |
| Phone / Pad builder、`setup.py` | Phone 两版已知 OTA 均保护保数据固件边界；Pad 支持隔离工作区及自定义实例名。保护规则基于固件身份和已有数据，不基于默认名称。 |

镜像的首次启动、Java/DEX、内核和宿主补丁继续使用上表的交付层。将这些内容移入 userdata KSU 会丢失恢复出厂或 root 前的必要修复，因此不宣称它们已全部模块化。未来版本可以复用已审计的相同内容；新的 APK、ELF、framework 或内核需要增加 profile 和真实验证。

本轮收敛后的完整回归为 **801 项通过，56 项可选跳过**。Phone / Pad 均通过正常启动器冷启动，保持 root、SELinux Enforcing 和 `skiavk`；序列号、机型、Android ID、应用版本、七项抽查设置和模块开关与验证前一致。两版天气使用同一已签名 APK 保数据重装后，安装路径从系统目录变为 `/data/app`，guest 模块自行恢复全部已核验私有库绑定，未再运行宿主修复。Pad 天气画面已检查；Phone 的既有定位授权提示保持原选择，未扩大为完整页面验收。临时脚本和重复中间文件清理，原回滚备份保留。
