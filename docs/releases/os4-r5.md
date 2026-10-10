# HyperOS 4.0.18.0 · v0.2.4 · Apple Silicon · r5

Tag：`v0.2.4-a17-hyperos4-hongkong-r5`，目标为 **Pre-release**。最终附件已在本地准备，尚未发布；r4 → r5 保数据升级与冷启动已验收，新装与冷启动已验收。继续使用小米 18 Pro（hongkong）官方 **OS4.0.18.0.XFRCNXM / Android 17**。

## 相比 r4

- OS4 运行时修复统一为 **Core 4 / Apps 3** 两个 KernelSU 模块，与 Pad 共用通用包。按完整 APK、原生库、ABI、来源和硬件能力匹配；动态跟随已审计应用的安装路径，未知可选内容局部跳过，初始化继续。
- 新增离线只读状态 WebUI；保留模块开关、待激活 / 待卸载状态和用户功能选择。经过认证的旧模块安全迁移，未知或自定义内容保留。状态核验通过不等于旧进程已经加载新代码。
- ART 编译按真实 CPU 拓扑设置范围，保留有效用户选择；启动服务按实际内核能力门控，减少缺失接口引起的无效循环。
- 修复桌面时钟小部件缩放后出现的字形错位，覆盖锁屏再解锁、长按移动等已复现场景。
- 针对已审计的手机 Vulkan 组合关闭 `VulkanQueueSubmitWithCommands`，保留 HWUI Vulkan、背屏与既有显示配置。宿主桌面 CPU 中位由 117% 降至 26.7%，三轮主屏相机 → 设置切换无 ANR，正常关机后约 8.2 秒自行退出；这些结果限定于本轮对照场景。
- 保留 Flutter 玻璃、桌面 / 小部件、锁屏编辑、小爱光效、天气、60 Hz、macOS sRGB 和音频适配，以及 AOD 与独立妙享背屏。

## 安装与升级

配套 **Installer 1.2.2**；默认 **4 核 / 6 GiB RAM / 32 GiB 存储**，资源可调整。需要 Apple Silicon Mac、Python 3、Android Emulator / Platform-Tools；新建或扩容用户分区需要 e2fsprogs。用户无需 NDK 或原始 OTA。

关闭目标 AVD 后选择同系列向前升级。安装器先备份完整 userdata、QCOW2、加密密钥和配置，通过兼容检查后保数据切换；失败可恢复。不支持跨手机 / Pad / OS3 升级或保数据降级。附件为同目录全部镜像分卷、`manifest.json` 与 `SHA256SUMS`，保留文件名；先发布安装器，再发布镜像。

## 验证边界

现有 Phone / Pad 已复测通用模块冷启动、天气同 APK 保数据重装后的路径恢复、状态 WebUI，以及虚拟场景相机预览。OEM / Parrot 拍照、HDR 和录像算法未完整验收；FindDevice 20.15.70 未提供 APK，不能视为已直接验证。未知更新仍需内容审计。

最终冻结安装器已完成两端 headless 新装、冷启动、32 GiB ext4 与模块状态检查；补丁核验后重开应用，实际加载的桌面 / 天气库哈希与 inode 匹配。手机已完成本地公开 r4 → r5 升级、备份恢复、认证旧模块迁移和实际加载检查；九项数据与用户选择比较一致。其它来源修订由通用兼容测试覆盖，未逐个运行来宾升级。

Google 登录、Play Integrity、蓝牙和小米云服务未完整验证，KSU root 域仍为 permissive。逐页 OOBE、完整相机算法与长期宿主稳定性仍需单独验收。

[安装指南](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/installing.md) · [r5 验证记录](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/releases/os4-r5-validation.md) · [补丁与运行证据](https://github.com/NEORUAA/HyperOS-AVD/blob/main/docs/os4-patches.md)
