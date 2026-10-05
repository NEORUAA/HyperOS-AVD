# Xiaomi Pad 9 Pro Max 测试镜像

从 `yingtian-ota_full-OS4.0.15.0.XBMCNXM-user-17.0-748c6d2437.zip` 官方全量包移植，独立于 OS3 和手机 OS4 镜像。当前用于本机适配测试，尚未准备 Release。

| 项目 | 配置 |
| --- | --- |
| AVD / ADB | `HyperOS_4_Pad9ProMax_API_37` / `emulator-5582` |
| 工作目录 | `work/os4-pad` |
| 系统 | Android 17 / HyperOS `OS4.0.15.0.XBMCNXM` |
| 原包身份 | `yingtian` / `M367FC` / Xiaomi Pad 9 Pro Max |
| 显示 | 横屏 `3408×2272`，`400 dpi` |
| 默认资源 | 4 核 / 4 GiB RAM / 32 GiB 用户分区；可在安装时自定义 |
| 机型配置 | 原包 `yingtian.xml`，不启用原机不支持的 AOD |

原厂面板参数为 `2272×3408`、270° 安装方向。模拟器保留纵向面板尺寸，由 WindowManager 以 rotation 3 生成横屏，再由启动器调整窗口方向；ranchu 不额外施加真机面板安装变换。设备身份与功能 XML 来自原包，内核、GPU、相机、音频等 HAL 使用 ARM64 AVD 的 ranchu 实现。

```text
2272x3408 panel -> rotation 3 -> 3408x2272 guest -> upright Mac window
```

启动：

```sh
./Start-HyperOS4-Pad.command
```

重新构建（OTA 放在仓库根目录，需要本机已有 API 36 ARM64 revision 7 硬件底包及构建工具）：

```sh
python3 scripts/build_os4_pad.py --weather-angle-libs /path/to/verified-angle-libs
```

`--weather-angle-libs` 提供现有手机 OS4 适配中已验证的 `libEGL_angle.so`、`libGLESv2_angle.so`，完整 SHA256 必须与 `patch_weather.py` 一致，保存在独立平板工作目录供启动器使用。平板原包自带的是另一版 ANGLE，不能直接套用此桥。已有该缓存时可省略此参数；构建本身不依赖该缓存，启动器完成天气初始化时需要它。

`--diagnostic-adb` 仅用于当前本机测试，关闭测试镜像的 ADB 授权验证；默认构建保留授权。初始化引导默认保留，只有显式传入 `--skip-oobe` 才跳过。

当前已合入对应引擎的 Flutter 深度、Float16、缓冲区对齐与阴影兼容修复，以及 GNSS 回调、AVD 合成、虚拟相机和音频底层修复。启动器为此平板单独应用 60 Hz 设置、传感器初始值与 macOS sRGB 标记，并修正 ranchu 温度节点的 SELinux 标签。首次引导中查找设备初始化阻塞并产生 ANR，因此在核验原始 APK 后禁用其状态查询组件；开机引导仍保留。天气使用独立 Flutter 引擎，另有要求 GLES 3.2 的 MGL 着色器；启动器核验原 APK 和原生库后应用私有渲染修复，保留签名 APK 与用户数据。运行日志和测试截图保存在 `work/os4-pad/logs`。

已进入桌面并确认 `3408×2272 / 400 dpi`、60 Hz 与 KernelSU / SELinux Enforcing。用户确认窗口正向；锁屏时钟已居中，新生成的设置与天气后台缩略图正常。天气城市搜索、预报文字及夜间背景已显示。旧任务卡片可能需要重新打开应用生成快照。相机、声音、其他旋转方向及全部手势仍未完成平板验收。

本次冷启动后再次确认方向与原生库修复生效，Android ID、引导完成状态、用户安装应用列表及共享目录清单与重启前一致；未重置用户分区。

macOS 色彩修复由启动器传入当前 AVD 名称，原生库核对实际 `-avd` 参数，不再依赖出厂名称。重启宿主后日志确认 `EmuGLViewWithMetal` 窗口从 `Display` 切至 `sRGB`，Metal 显示层同时使用 sRGB 标记；Android 保持自然模式和 `1.0` 饱和度。此次宿主重启前后，用户数据清单与天气定位权限一致；窗口观感仍需用户对照截图确认。

首次应用扫描与引导期间出现过 Watchdog 软重启，随后宿主进程在 `MoltenVK → vkUpdateDescriptorSets` 路径发生空指针崩溃；诊断记录保存在 macOS 的 `DiagnosticReports` 目录。后续启动发生过 goldfish_sync 内核异常；色彩修复后的宿主重启也复现相同调用链，随后自动重新启动并进入系统，不能据此认定镜像已长期稳定。

平板启动器单独添加 `-feature VulkanBatchedDescriptorSetUpdate`，继续使用 host GPU；实测日志确认该选项已启用。上游 [Gfxstream](https://android.googlesource.com/platform/hardware/google/gfxstream/+/refs/heads/main/host/vulkan/VkDecoderGlobalState.cpp#1291) 在此模式屏蔽 inline uniform block，作为上述宿主崩溃的规避方案；不修改 SDK 库或其他 AVD。此版模拟器未采用 `ANDROID_EMU_VK_ICD=swiftshader`，因此不将该环境变量作为有效切换方式。桌面、手势、天气、相机、声音等仍需逐项验收。

平板 HWUI 默认使用 `skiavk`，依赖 `patch_pad_hwui` 对原始 `libhwui.so` 严格核验版本后应用的单指令补丁：将 `0x94adec` 的 `BL peekRenderPipelineType` 改为 `B 0x94ae10`，跳过 Zygote 的 GPU driver 预加载，同时保留 OEM 的 `MiuiForceDarkConfigStub::preload`、`MiBlurBlendUtils::instance` 和 CPU `initShaders`。运行时安装路径及保存配置后的完整重启已通过：约 42 秒启动完成，目标库为补丁后哈希，SystemUI 与设置实际使用 Skia Vulkan，SELinux 保持 Enforcing。设置、天气与控制中心玻璃正常显示，重启后的 crash buffer 为空；Android ID、引导状态、应用列表、共享目录清单与天气定位权限和标记均与此次重启前一致。原厂玻璃着色器与动画仍保留，长期稳定性尚待观察。

在天气页面触发同一前台定位权限弹窗的受控对照中，`skiagl` 累计绘制 3 帧，弹窗停在背景透出、尺寸缩小的状态；`skiavk` 累计绘制 27 帧，弹窗完成入场后为不透明白底。补丁下经自然回调触发的前台定位及同一后台定位弹窗均为不透明白色背景、正常尺寸；后台弹窗标题已核对 XML 与截图一致，测试前后的权限 grant 与 flags 已精确恢复。此对照限定于进程内 HWUI 绘制的 Android View。SurfaceFlinger RenderEngine 是独立渲染路径；共享 Flutter 引擎和天气原生 ANGLE 渲染各自沿用对应兼容修复。

平板预装小爱 `8.2.53.3014` 与手机 APK 不同，但 MGL2 原生引擎完全相同。已补入平板专属 APK 校验及启动器／构建器接入，复用 GLSL 3.00 和 8-bit EGL alpha 修复。当前测试 AVD 通过 KernelSU 保存独立库；重启后再次核验 init、spawner、zygote 的挂载及小爱进程实际加载路径。录屏确认白色边缘光效亮起并渐隐、桌面保持可见，未再出现着色器版本错误；原签名 APK、安装时间及用户数据清单保持一致。记录在 `work/os4-pad/logs/assistant-coldboot-warm`。

平板原装相机 `6.8.001380.0` 的启动闪退来自 GLES 3.1 着色器编译失败。`apply_pad_camera_fix.py` 核验平板身份、原 APK 和系统 ANGLE 库后，为 `com.android.camera` 选择 ANGLE 并追加 `exposeES32ForTesting`；保留其他应用的驱动配置。

`apply_pad_camera_native_fix.py` 将三个固定版本补丁保存为独立 KernelSU 模块：Google HAL 只将小米私有模式 `0x9005`／`0x8004` 转为标准模式；HWL 为前摄补入小米 role `1`；相机私有 JNI 将模拟器的分离 I420 色度平面转换为应用需要的 NV21。Pad ImageReader 偏移、JNI 注册表及原始库均逐一核验，未复用手机 framework 的偏移或 ISP 逻辑。原 APK、签名及用户数据保留；启动器按当前实例配置应用，不依赖 AVD 名称。预编译库及校验收据位于测试工作区的 `tools/pad-camera-native`，避免后续启动重复编译。

前后摄预览及切换、后摄最终 JPEG 颜色已通过实际测试，前摄也生成了 JPEG。前摄 `emulated` 的绿色、橄榄色和紫色色块对应 [Google HWL 的合成场景](https://android.googlesource.com/platform/hardware/google/camera/+/refs/heads/android16-release/devices/EmulatedCamera/hwl/EmulatedScene.cpp#504)，不代表真实摄像头输入。原厂拍照后处理仍会超时并延迟保存，本次未声称该流程已完全适配。记录在 `work/os4-pad/logs/camera-native-candidate`；用于独立 Camera2 对照的临时应用被系统相机权限检查拒绝，已移除，不计入通过项。

完整重启后再次确认 init、spawner、zygote 中的模块挂载，前摄 ID `1`、后摄 ID `0` 均可切换并预览，crash buffer 为空。SELinux 保持 Enforcing；Android ID、引导状态及应用列表一致，共享目录仅清理了已删除测试照片对应的缓存缩略图，未重置用户数据。

冷启动测试曾发现，未应用上述补丁时，`ro.zygote.disable_gl_preload=0` 配合 `skiavk` 会使 Zygote 在 fork 前预加载 Vulkan，随后 `forkSystemServer` 因 `Not allowlisted (71): /dev/goldfish_pipe_dprctd` 中止。直接采用 ranchu ARM64 revision 7 默认的 `ro.zygote.disable_gl_preload=1` 又会跳过整个 OEM graphics preload，包括 CPU shader 初始化；后续 GL 和 Vulkan 测试均出现 SystemUI 在 `MiBlurBlendCustom::onMakeCustom` 内调用 `SkRuntimeEffectBuilder` 时反复 `SIGSEGV`。当前方案以固定版本补丁跳过 GPU driver 分支，并使用 `ro.zygote.disable_gl_preload=0` 保留 OEM 初始化。payload 以 `0644` 权限及 `system_lib_file` 标签原子安装；既有 AVD 检查及 KernelSU 温度节点早期脚本继续使用。该脚本通过 BusyBox `nsenter` 进入 init 的 mount namespace，核验源库为 `AFTER`、目标库为 `BEFORE` 或 `AFTER`，只对 `BEFORE` 执行 bind mount；重新核验目标为 `AFTER` 且 `/data/adb/ksu/bin/resetprop` 可执行后，才执行 `/data/adb/ksu/bin/resetprop -n ro.zygote.disable_gl_preload 0` 并配置 `skiavk`。

与手机版本的最后一轮配置对照补齐了公共身份、分区 fingerprint、共享日志级别、息屏超时和管理工具分类。48 项公开身份字段逐项来自已核验的 OTA 分区及 metadata；ranchu 的 HAL 选择属性继续保留。Pad 管理界面单独识别其类型，支持自定义内存、存储与 CPU。当前镜像已重建，HWUI 补丁、SettingsProvider 默认值 overlay 和日志脚本已写入镜像；默认永不息屏，色彩模式仅首次设置为自然模式，后续保留用户选择。

Pad 序列号按小米样式随机生成，保存在当前用户分区。启动前设置 `ro.serialno`、`ro.boot.serialno`、`ro.ril.oem.psno`，重启保持一致，恢复出厂后重新生成；它是模拟标识，不是实机有效序列号。脚本按注册实例及固件版本校验，不依赖出厂 AVD 名称。

补齐 fingerprint 后的应用重扫曾触发天气删除：旧 Flutter 模块在 PackageManager 扫描前挂载了天气解压目录的原生库，导致 native extraction 返回 `-18`，系统随后删除了天气记录及私有数据。原厂签名 APK 保留完整；普通重新安装恢复了应用，但未恢复原城市与偏好。现已将 Pad 天气的全部原生挂载移到系统启动完成之后，同时支持原预装路径与更新后的 `/data/app` 路径，保持 APK 字节及签名不变。随后按用户要求重置 Pad 用户分区，重新进行新用户检查。

2026-10-06 的新用户检查已完成：仅重置注册的 Pad 实例，旧用户分区离线保存在本次检查目录。逐页操作原版 OOBE，跳过账号、密码与唤醒词录制后进入桌面，未使用永久跳过引导开关。新的 Android ID 与序列号已生成；天气原厂 APK 自动注册，首次授权后显示上海预报、完整渐变与动态云层，权限弹窗不透明，后台缩略图方向正常。

新用户检查发现 SettingsProvider 默认值 overlay 与 OEM overlay 同为优先级 `999`，同优先级路径排序使 OEM 的两分钟超时胜出。Pad overlay 改为 `1000` 并沿用原签名；候选只替换此 APK，保留 HWUI、Flutter、机型 XML 与天气 APK。冷启动实际资源查询已解析到 `2147483647`。原始 `stay_on` 布尔默认值对应 AC 的 `1`，启动器再设置 AC、USB、无线供电合计 `7`；`sleep_timeout=-1`。

写入候选后的冷启动确认 Android ID、序列号、引导完成状态、用户应用列表、共享文件清单、天气安装时间及定位权限均与重启前一致；上海城市保留，天气进程实际加载补丁后的 Flutter 与五个私有 ANGLE/MGL 库。48 项公共身份逐项匹配 OTA 配置，原始机型 XML 哈希一致，合成与物理刷新率均为 60 Hz，SELinux Enforcing，crash buffer 为空。截图、六秒背景录屏及比较结果位于 `work/os4-pad/logs/fresh-user-20261006-051310`，候选校验收据位于 `work/os4-pad/work/priority-image/receipt.json`。本轮覆盖首次引导与上述天气场景；不代表所有天气状态、硬件与长期稳定性均已验收。

原包、构建缓存、系统镜像和个人用户数据均被 Git 忽略。测试进展以实际启动与操作结果为准，不能据此认定所有硬件及小米服务均可用。
