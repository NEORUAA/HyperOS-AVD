# OS4 补丁维护与交付

OS4 的运行时兼容修复统一由 **Core、Apps 两个 KernelSU 模块**交付。Phone 与 Pad 使用同一套模块代码和相同的通用 ZIP；模块根据实际 APK、原生库、系统依赖和硬件能力选择已审计规则。AVD 名称、ADB 端口、工作区目录和 OTA 文件名不作为运行时版本身份。

相同内容可以在后续 OS4 基系统中复用。**未知的 APK、ELF、framework、HAL 或内核仍需审计**；匹配不到已审计内容时保留原文件并报告原因，不猜测偏移或放宽 ABI 校验。

## 两个通用模块

| 模块 | 统一职责 | 选择依据 |
| --- | --- | --- |
| **Core** · `hyperos_avd_native_compat` | 七类原生补丁：Flutter、HWUI、桌面导航、小爱、锁屏视频、composer alpha、背屏 timing；公开机型身份、稳定序列号、thermal label、刷新率、已核验启动服务及背屏唤醒。 | 原生库完整 hash、原字节与输出 hash；实际显示/内核能力；镜像上下文中已核验的属性和前置文件。 |
| **Apps** · `hyperos_avd_app_compat` | 两个 Weather workload、三个 OEM Camera workload、一个可选 Parrot workload，共 **六条内容规则、十一份按 SHA-256 去重的对象**；私有库、相机早期 HAL 目标及按包 ANGLE 策略。 | 完整已签名 APK、嵌入 JNI、私有调用库、Android runtime、libc++、provider/HWL 等联合内容校验。 |

Core 的原生规则由 `native_patch_catalog.py` 从现有补丁常量生成；Apps 的规则由 `app_compat_producers.py` 统一收集。Phone / Pad 的构建器可以取得各自已核验的输入，但不生成另一套模块实现，也不让 guest 按设备名分支。

每份镜像另带 `tools/os4-core/context.json` 与收据，由 `core_context.prepare()` 从已核验的完整镜像提取；`core_context.load()` 在安装时重查来源。上下文位于通用 Core ZIP **之外**，描述实际镜像的身份和能力前置条件，不复制原生补丁代码。

Apps 每个模块只有一个调度器，为匹配的应用维护隔离的内容上下文。`weather`、`oem-camera`、`parrot-camera` 的本地偏好为 `auto` / `on` / `off`，保存于 `/data/adb/hyperos_app_compat/features/`。`auto` 采用匹配内容规则的默认值：两版天气默认启用，已审计 Pad 相机内容默认启用，已审计 Phone 相机与 Parrot 默认关闭；已有明确选择优先保留。这些默认值是规则数据，不是运行时 Phone / Pad 选择器。

## 完整补丁清单

| 补丁族 | 作用与维护入口 | 交付边界 |
| --- | --- | --- |
| Flutter | 深度范围、Float16、缓冲区对齐、阴影及已审计共享引擎的大字号字形图集阈值。`patch_flutter.py` | Core：系统共享引擎早期处理，应用私有引擎按当前安装路径发现；相册通过共享引擎受益。 |
| HWUI / Vulkan | 跳过 fork 前 GPU 驱动初始化，保留小米 CPU shader preload，配合 `skiavk` 与 RenderEngine 配置。`patch_pad_hwui.py` | Core 原生补丁 + 镜像 renderer 基线；未知 HWUI 不能仅靠切换属性宣称兼容。 |
| 桌面导航 | Recents AOT 等待时限、route watchdog；`ro.miui.product.home=com.miui.home` 防止误认 Launcher3。`apply_navigation_fix.py` | Core 原生规则与身份配置。已撤销的缩放动画规避保持撤销。 |
| 渐进式模糊 | `persist.sys.gradient_blur_perf=false` 选用原有通用 Skia 路径。`os4_defaults.py` | 已核验 HWUI 的早期能力配置，不替换应用 UI。 |
| 合成器 / 背屏 timing | plane alpha 与亮度分离，让返回压暗渐隐；背屏独立 60 Hz timing。`patch_composer.py`、`patch_rear_display.py` | Core，保留两步 hash 链；背屏规则需实际显示能力，已加载 HAL 需正常冷启动。 |
| 小爱边缘光效 | MGL GLSL 版本与 8-bit EGL alpha，保留背景和原动画。`patch_assistant.py` | Core 按已核验 APK/库内容及当前应用路径处理。 |
| 锁屏视频预览 | fastplayer 使用 packed RGBA 与正确 stride，避开异常 YV12 导入。`patch_lockscreen_video.py` | Core 原生规则 / 镜像外置库；不改写原 APK，不给未知播放器套用偏移。 |
| OEM Camera | 私有流模式、front role、I420→NV21；已审计 Phone JPEG 恢复及不同 ImageReader ABI。`apply_xiaomi_camera_fix.py`、`apply_pad_camera_native_fix.py` | Apps 联合核验 APK、JNI、runtime、HAL、libc++。provider/HWL 和出厂目录镜像在早期映射，更新后的私有库走晚期 PM 路径；基础功能不等于真机 ISP/录像算法完整支持。 |
| 相机 ANGLE / Parrot | OEM 相机的按包 ANGLE 策略；可选 Parrot CPU AION 分配与纹理查询。`apply_pad_camera_fix.py`、`apply_camera_fix.py` | Apps 保留其他应用选项，Parrot 另核验完整 APK、调用库和 runtime；未知 workload 保留原内容。 |
| Weather MGL / ANGLE | 私有 Vulkan EGL display、MGL 依赖重定向和已核验 ANGLE workload。`patch_weather.py` | Apps 两条内容规则，从 PM 动态发现路径，避开安装提取阶段，不重签 APK；镜像仍负责预装基线。 |
| 音频恢复 | tinyalsa 播放 `EIO` 有限重置并重试，保留其他错误和录音路径。`patch_audio.py` | Vendor 镜像，含机器码和布局变化，不能转成任意库的等长规则。 |
| 虚拟场景相机 | rear ID、Xiaomi role、YUV 元数据、预览尺寸。`patch_camera_scene.py` | 镜像 provider + 编译后的 scene helper，核验 provider、C++ ABI 和依赖布局。 |
| GNSS framework | 在释放 HAL monitor 后回调，避免同步回调死锁。`patch_gnss.py` | 已核验 `services.jar` 的 Java/DEX 镜像补丁。 |
| ART 编译 CPU 策略 | 根据可用 CPU 拓扑配置 dex2oat，保留有效用户 CPU 子集和线程选择。`dex2oat_cpu_policy.py` | 镜像 init 与 Core 共用策略，不写死四核 affinity；安装和编译需实际验证。 |
| 硬件服务 / 内核 | 缺失 QTI HAL/thermal/provider 的 guard，Millet/iorapd/GNSS extension 能力 probe，窄 SELinux 权限与 goldfish_sync。`patch_boot_services.py`、`check_kernel_services.sh`、`patch_goldfish_sync.py` | 镜像 Java/init/probe/SELinux/内核；Core 管理已核验前置条件和能力探测，不能修补未知内核或替代 root 前基线。 |
| FindDevice / OOBE | 禁用阻塞状态 Provider，保留 APK 和正常引导；系统更新包需来源、签名和 Provider 合约校验。`os4_defaults.py`、`os4_pad.py` | 镜像首次 PM 扫描及受控初始化，不能依赖用户进系统后手动修复。 |
| 机型 / 默认值 | 原机 XML、公开身份、稳定随机小米格式序列号、thermal label、60 Hz；按硬件能力配置 AOD，一次性常亮/自然色彩/日志默认值。`config/`、`os4_defaults.py`、`os4_pad.py` | Core + 镜像默认值，迁移旧标记和序列号并保留后续用户选择。Pad 不启用原机不支持的 AOD。 |
| 背屏资源 / 唤醒 | 物理显示 ID alias、最小 RRO、原几何/cutout/corner、双击唤醒 Java bridge。`rear_display_config.py`、`rear_display_wake.py` | 镜像资源/bridge；Core 校验前置文件并管理服务。物理屏创建和独立窗口仍由 Emulator 提供。 |
| macOS / Emulator / 存储 | sRGB window/Metal 标记、传感器/虚拟 AC 初值、音频后端、Pad 窗口方向、独立背屏窗口及用户分区扩容。`host_color.py`、`launch.py`、`manage.py` | 宿主层。扩容同时校验虚拟磁盘和 `/data` ext4；guest 模块无法修改宿主窗口、音频设备或离线 QCOW2。 |

## 激活、状态与迁移

正常启动器安装 Core 和 Apps，不再依次调用各版天气/相机的专用安装器。二者遵循标准 KernelSU 的 active / pending / disable / remove 生命周期；安装器复用完全核验的当前版本，保留已有 pending 包和用户开关，不执行旧 uninstall hook。

早期系统库与相机 HAL 在加载前绑定。Apps 的出厂相机目录也在早期映射，使用原 APK 字节和已核验私有库构造模块自有镜像目录；原始文件保持不动。更新后的应用私有库从 PM 解析当前 APK/native 路径，安装事务稳定后才重新核验，包数据库无变化时不重复提取。ANGLE 修改限定于已匹配包，并保留无关包和选项。

两模块使用内核 `flock` 租约和不可变内容缓存，不改写已加载 inode。Core 跟踪本次新建绑定并在失败时回滚；Apps 另有模块自有的持久事务和恢复记录，尚被进程使用或外来挂载遮盖的记录保留到可安全恢复。已加载 provider/HWL 的修复报告待重启，相机桥接不主动停止 HAL、zygote 或系统服务；Core 能力探测可停止当前内核不支持的已核验服务。私有库新映射可以结束对应应用的旧进程，仍须检查重新打开后的实际加载结果。

已知旧模块只有在控制脚本、载荷及必要 ABI 都经过认证后才迁移；先导入尚未存在的功能选择，待新模块确实激活后再把旧目录移到模块自有归档，保留原 inode。未知/自定义、待激活或带生命周期标记的旧模块保留并报告。不会仅因模块名或 manifest 自称匹配就移除内容，也不会运行旧卸载脚本或重启 provider。

Core 状态为 `/data/adb/modules/hyperos_avd_native_compat/state/status.tsv`，每行 `feature|target|state|reason`；Apps 为 `/data/adb/modules/hyperos_avd_app_compat/state/status.tsv`，每行 `feature|package|state|reason`。各模块的 `runtime.log` 保留诊断。

两者共用 `modules/compat-webui/` 的 **离线、只读 KernelSU WebUI**，显示模块生命周期、SELinux、renderer、有限条状态和日志；页面不联网，也不接受任意 shell 输入或修改系统设置。可从 KernelSU 对应模块卡片的 WebUI 入口打开，点击刷新读取最新记录。

| 显示状态 | 含义 |
| --- | --- |
| Core `ready` / Apps `verified` | 已有输出或模块自有映射通过内容核验；**不等于当前旧进程已加载新代码**。 |
| `pending` / `reboot-required` | 安装尚待激活、包事务未稳定、旧映射仍被使用，或早期目标需正常冷启动；查看具体原因。 |
| `skipped` / `unsupported` | 缺少目标、未知内容或 ABI/前置条件不同，保留原文件；其他匹配特性继续工作。 |
| `failed` | 内容、所有权、输出、命名空间或挂载核验失败，应排查原因，不自动放宽校验。 |
| `disabled` / `not-activated` | 用户/匹配规则关闭功能，或模块尚未产生可信运行状态；与不支持区分。 |
| 模块 `pending-removal` | 用户已选择卸载，等待正常重启；即使旧状态文件仍在，也显示待卸载。 |

恢复出厂会清除 `/data/adb`。可启动镜像、首次扫描、Java/SELinux/内核等必要基线必须留在镜像；userdata 模块不能保证重置后的第一次启动。启动器在基线可用后恢复两个模块，需要时完成正常重启。镜像内 KSU 自举的恢复出厂路径未完成验证前，不写成已实现能力。

## 构建与维护

最终用户只消费已核验的预构建包，安装和启动不需要 NDK、编译器或外部 donor 工作区。发布者用显式本地 cache roots 收集完整规则集合；所有输入、编译源和输出均需收据校验，目录顺序不会改变结果。任意缺少已审计载荷的构建应失败，不能按当前设备悄悄删减规则。

```sh
python3 scripts/package_native_module.py --output /path/to/native-compat.zip
```

```python
from pathlib import Path
import sys

sys.path.insert(0, 'scripts')
from core_context import prepare
from apply_app_compat import prepare_prebuilt

# Explicit audited producer inputs; directory names are not device selectors.
cache_roots = [Path('/path/to/audited-source-a'), Path('/path/to/audited-source-b')]
for workspace in cache_roots:
    prepare(workspace)
    prepare_prebuilt(workspace, cache_roots=cache_roots)
```

每份工作区产生自身 Core 上下文及 `tools/os4-app-compat/{app-compat.zip,receipt.json}`，其中 Apps ZIP 是同一完整规则集合。消费者通过 `core_context.load()` 和 `apply_app_compat.install_prebuilt()` 校验这些文件；release runtime 包含共用模板、规则和旧模块认证记录，不依赖生产者机器上的缓存路径。

新增内容按以下步骤维护：

1. 审计原始 APK/ELF、架构、函数和 ABI、完整 hash、原字节与预期输出；编译桥接同时核验 JNI/C++/framework/HAL 的全部调用边界。硬件能力配置与二进制兼容规则分开。
2. 在原补丁入口保留旧规则并增加已审计内容，由 Core catalog 或 Apps producer 引用同一份常量。新增规则进入完整通用包，不另建 Phone / Pad 模块或用 OTA 名称替代内容身份。仅版本元数据变化且相关内容/合约仍完全相同，也须核验后才复用。
3. 测试原始→输出、幂等、旧输出迁移、错误 hash/site 拒绝、动态 PM 路径、挂载失败恢复、用户开关和保数据升级。修改模板时重建对应收据，不能沿用旧认证结果。
4. 验证正常冷启动、实际加载库、用户数据/选择保留及对应功能。静态校验不能代替动画、音频、相机或 UI 验收；只对实测组合作结论，未知内容保留明确原因。

镜像准备继续通过 `packed_source.py` 从当前完整、已核验镜像取候选；保留分区内容/顺序，拒绝未知 LP 属性，保留已接受 RRO 的原签名字节，并在发布前复核源和收据。Phone / Pad builder 与安装器的保数据保护基于固件身份和已有用户数据，不基于默认 AVD 名称。

## 验证证据

2026-10-10，Phone 与 Pad 已通过两个通用模块的正常 KernelSU 升级、冷启动和启动器初始化验证。最终激活 **Core 4 / Apps 3**；两版 ZIP 字节一致，Apps 为六条规则、十一份去重对象。Core ZIP SHA-256 为 `c62f5834424a0292d36ea0a41d8fd34297bb62e739dbc68ca7b96b72b97a86a0`，Apps ZIP 为 `62991ff994f4341a027ac6666a0a54d68e6059a7ed9e29fe1d90ded7c88a0612`。

- Phone 的 Weather、OEM Camera、Parrot 三个旧模块以及 Pad 的 Weather、Camera 旧模块已认证迁移；两端只有 Core、Apps 两个项目模块，无 pending 或旧 worker，Phone 原有的 LSPosed / Zygisk 保留。
- root、SELinux Enforcing、HWUI `skiavk`、主屏 active/render 60 Hz 正常；Phone 背屏与 Pad 270° 方向保持。机型、稳定序列号、Android ID、全部包版本、七项抽查设置及 Apps 功能选择保持原样。
- 两版天气分别使用各自原先的同一已签名 APK 保数据重装，PM 路径确实改变；guest 模块自行重新发现、核验和绑定。旧进程结束后重新打开，温度、背景、预报正常，私有 patched ELF 的内容和实际 loaded backing inode 均吻合；Phone 既有定位授权弹窗未代答。
- 两版 OEM 相机的虚拟场景预览正常，实际 provider/HWL 的已加载 inode 与预期输出吻合；Pad 私有 YUV JNI 同样实载。Phone 预览未加载该 JNI，不能据此宣称其拍照或完整录像算法已验收。
- Phone Parrot 的虚拟预览正常，首次配置弹窗未代答；预览路径未加载可选 allocator bridge，其映射核验不作为 HDR/录像算法验收。
- Core / Apps WebUI 在两版均已实际打开并刷新，读取 guest 状态；未安装 Parrot 的 Pad 明确显示未匹配。生命周期标记优先于旧状态，日志不参与结构化状态解析。
- 完整回归在 Core 4 / Apps 2 阶段共 **884 项，其中 56 项可选跳过，828 项执行通过**；最后的 Apps 3 文件名边界与宿主版本迁移修正分别通过完整 18 项定向回归。相关启动、上下文和 Phone/Pad 交付集成检查共 73 项，其中一项可选跳过，执行项通过。
- 实际 release runtime 的 106 个文件及 Core / Apps 资产复制到改名、含空格的独立工作区，在空 PATH、无 SDK/NDK/编译器、禁止网络和原工作区读取的消费环境下均通过认证；Apps 1 / 2 的严格历史认证资料随包完整交付。

已清理无进程引用的五个任务迁移归档（两端共约 504 MiB）及约 39 MiB 临时生产者缓存，未创建或删除用户 AVD、未清除用户数据。Pad 保留原有 6 GiB 用户盘配置，清理后约有 1.3 GiB 空闲；此检查未调整用户容量。

此前连续 Android 软重启曾触发 SDK macOS OpenGL/gfxstream 路径的宿主崩溃，正常冷启动恢复且用户数据保留；根因尚未确认，不把 guest 核验扩大为宿主稳定性保证。历史反馈中未在最终抽样日志复现的 MoltenVK、HTMLViewer、Gallery 问题同样不直接声明已修复。

## 反馈终检与性能

2026-10-10，复核用户反馈目录的两份分析及五份日志包中的 74 份原始文本；范围为 Phone OS4.0.18.0 与 Pad OS4.0.15.0 的当前保留用户数据。以下把已验证修复、场景复测和未闭环问题分开，不宣称所有历史问题均已解决。

| 反馈 | Phone | Pad |
| --- | --- | --- |
| dex2oat CPU 越界 | 本轮实际安装编译通过，使用真实四核集合；测试包已移除。 | 本轮同样实际安装通过，ART 为 `reason=install`、四核集合、三个 worker。六核、稀疏拓扑由单元测试覆盖，未作本轮实机证明。 |
| QTI 显示脚本执行失败 | ranchu 能力门控通过，未执行不支持的路径。 | 无对应原始反馈。 |
| iorapd / Millet 循环、Core 冷启动失败 | 共用能力探测与正常冷启动通过。 | 正常冷启动通过；180 秒观察未见循环。 |
| 更新 FindDevice 阻断初始化 | 未知或不满足来源/Provider 合约的内容局部跳过，其他初始化继续。 | 相同处理；反馈中的 20.15.70 APK 未提供，不能代替该更新版本的直接验收。 |
| 更新桌面引擎 / deadline 警告 | 未知可选原生内容不再中断全部初始化；原警告未证明卡顿因果。 | 当前预装 5490；反馈的 6325 / `0266…` 引擎尚无内容规则，渲染仍待适配。 |
| MoltenVK shader / pipeline 失败 | 无对应原始反馈。 | 当前冷启动及有界操作未复现；首次 OOBE 的原场景未重放，未认定根因修复。 |
| HTMLViewer 协议详情 ANR | 无对应原始反馈。 | 本轮正确打开 LicenseActivity：正文实际显示、可滚动，无 ANR。 |
| Gallery widgetProvider 数据库路径崩溃 | 无对应原始反馈。 | Provider 存活；相册停在首次同意页，未代答，具体小部件业务尚未验收。 |
| FindDevice CoreWorkJobService ANR | 无对应原始反馈。 | 旧日志有 onStartJob 超时；本轮未触发对应 Job，仍待验证。 |
| nits mapping / XQos / mapper / AVC 等日志 | 未证明这些诊断导致功能故障。 | XQos 仍重复探测缺失节点，mapper 扩展仍不支持；保持真实失败与 SELinux 拒绝，未伪造成功或全局静音。 |
| 正常关机后宿主占用 CPU | 本轮复现并完成下述窄修复验证。 | 本轮正常关机可自行退出；历史一次宿主崩溃/不退出的全部路径尚未闭环。 |

两端连续 180 秒桌面待机时，guest 四核总 CPU 中位约 4.0% / 4.6%，可用内存最低约 2.77 / 1.51 GiB，无 OOM、swap-out 或新增 ANR。12 次设置页滚动测得 HWUI jank 为 0.11% / 0.18%；该结果仅覆盖设置页，不等于所有 Flutter 动画或宿主窗口的帧率验收。宿主为 16 GiB Mac，已有约 13.5 GiB swap 用量，不能把 QEMU RSS 当作完整内存占用；未修改用户 RAM、CPU 或存储配置。

Phone 宿主待机 CPU 中位原为 117%（单核满载记为 100%）。三秒线程采样与实际 SDK 反汇编定位到 Vulkan decoder 的命令序号忙等；该机制也见于 [gfxstream 的 decoder 源码](https://chromium.googlesource.com/external/gitlab.freedesktop.org/mesa/mesa/+/74b5819d5ff2d5a9435c9ff4bad44a70f18b6780/src/gfxstream/codegen/scripts/cereal/decoder.py#974)。因此仅在原有完整 Phone Vulkan 身份/HWUI 校验通过的分支关闭 `VulkanQueueSubmitWithCommands`，保留 Vulkan、descriptor workaround、`-GLAsyncSwap` 与背屏。

关闭后，实际解锁桌面 60 秒 CPU 中位为 26.7%，采样中原忙等位置消失；相机虚拟场景预览正常，三轮明确指定主屏的 Camera → Settings 焦点切换无 ANR，正常 Android 关机后宿主约 8.2 秒自行退出。此前另发现 CameraFocus 10 秒图形等待 ANR，此对照场景已通过，但不扩大为完整拍照、录像算法或所有相机焦点路径保证。首次冷启动仍处锁屏、或输入被背屏接收的样本不计作桌面/主屏相机验收。

Pad 关闭同一传输优化的试验未证明性能收益（桌面 CPU 中位 25.55%，原为 18.65%），故保持原默认参数。其 XQos 缺节点在 60 秒观察中仍出现 93 组失败，可考虑已认证可选 QoS 初始化的缺失能力缓存；尚无 CPU 收益证据，本次没有新增二进制补丁或扩大 SELinux 权限。

最终两端均由正式启动器恢复运行；Phone 使用上述修复，Pad 恢复原传输参数。再次冷启动后的稳定标识、全部包版本、七项抽查设置与 Apps 选择摘要一致，当前用户 CPU/RAM/存储配置保持。Pad 试验关机约 9.3 秒自然退出，未强杀。

本轮宿主启动参数与受影响初始化回归共 28 项，26 项执行通过，2 项因未分发专有原始库跳过。实际安装探针无权限或交互组件，两端测试包、远端 APK 和一次性宿主构建/签名资料均已移除；没有下载旧发行版、创建额外 AVD、清除用户数据或修改 SDK 二进制。

详细适配背景见 [手机](hyperos4.md)、[Pad](hyperos4-pad.md)；具体发布证据见 `docs/releases/`。私有诊断与回滚资料不作为 release 附件。
